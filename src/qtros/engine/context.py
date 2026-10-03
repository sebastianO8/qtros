"""The strategy boundary: construction of :class:`BarContext`.

This module is where the availability shift lives, and it is the *only* place it lives
(ARCH-004 F-1, ruled). Transforms emit unshifted causal features; the engine reads
feature values from row ``index - 1`` when building the context for bar ``index``.

The enforcement is structural rather than procedural. A strategy receives a
:class:`BarContext` and nothing else — no frame, no index into one, no accessor for an
adjacent row. It cannot read a future feature because there is no expression it could
write that would reach one. Compare this with a convention that features "should" be
shifted: the convention is correct exactly as often as everyone remembers it, and its
violations are invisible in review because the resulting backtest looks fine.

A second guard covers the opposite failure. If a feature frame arrives already carrying
an availability shift, applying another here would silently serve T-2 features. The
factory refuses such a frame rather than compounding the shift.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Final

import structlog

from qtros.domain.errors import ConfigurationError, SchemaContractError
from qtros.domain.feature_contracts import FeatureFrame, LagPolicy
from qtros.domain.market import BarData
from qtros.domain.signals import BarContext, FeatureRow, PortfolioView, SessionInfo

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from qtros.domain.instrument import InstrumentSpec

_LOGGER: Final = structlog.get_logger(__name__)

REQUIRED_BAR_COLUMNS: Final[tuple[str, ...]] = ("ts_utc", "open", "high", "low", "close")
AVAILABILITY_LAG_BARS: Final[int] = 1
"""Bars of lag between the feature row and the bar being decided.

One, per Directive 1. Named rather than inlined so that changing it is a visible,
reviewable act rather than an off-by-one edit somewhere in a slicing expression.
"""


def to_decimal(value: float | int | Decimal | None) -> Decimal | None:
    """Convert a numeric value to :class:`~decimal.Decimal` without float contamination.

    Args:
        value: Value to convert.

    Returns:
        The exact decimal equivalent of the value's shortest text representation, or
        ``None``.

    Note:
        Conversion goes through ``str`` deliberately. ``Decimal(0.1)`` yields
        ``0.1000000000000000055511151231257827…`` — the binary float's true value — which
        then propagates into every cash figure derived from it. ``Decimal("0.1")`` is the
        price the vendor actually published.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class BarContextFactory:
    """Builds a :class:`BarContext` for each bar of a run.

    Column data is materialised into Python lists once at construction. Per-row access
    into a Polars frame is comparatively slow, and the run loop touches every row of a
    multi-year dataset; paying the extraction cost once keeps the loop from dominating
    backtest wall-clock time.

    Args:
        feature_frame: Output of Layer 0.5, warmup-trimmed and audited.
        instrument: Contract specification for the traded instrument.
        session_column: Column carrying a monotonic session index. When ``None``, the run
            is treated as one continuous session.
        require_audit: Refuse a feature frame that has not passed the prefix-invariance
            audit. True by default: an unaudited frame's causality is a matter of belief,
            and this is the last point at which that can be checked.

    Raises:
        ConfigurationError: If the frame already carries an availability shift, or if
            ``require_audit`` is set and the frame was not audited or failed its audit.
        SchemaContractError: If a required bar column is missing.
    """

    __slots__ = (
        "_bar_index_in_session",
        "_close",
        "_feature_columns",
        "_feature_values",
        "_high",
        "_instrument",
        "_is_session_close",
        "_is_session_open",
        "_is_synthetic",
        "_low",
        "_open",
        "_row_count",
        "_session_index",
        "_timestamps",
        "_volume",
    )

    def __init__(
        self,
        feature_frame: FeatureFrame,
        instrument: InstrumentSpec,
        *,
        session_column: str | None = None,
        require_audit: bool = True,
    ) -> None:
        self._guard_shift(feature_frame)
        self._guard_audit(feature_frame, require_audit=require_audit)

        frame = feature_frame.frame
        missing: tuple[str, ...] = tuple(
            column for column in REQUIRED_BAR_COLUMNS if column not in frame.columns
        )
        if missing:
            raise SchemaContractError(
                "Feature frame is missing bar columns required to build a BarContext",
                missing=list(missing),
                present=frame.columns,
            )

        self._instrument: InstrumentSpec = instrument
        self._row_count: int = frame.height
        self._timestamps: list[datetime] = frame.get_column("ts_utc").to_list()
        self._open: list[Decimal | None] = [to_decimal(v) for v in frame.get_column("open")]
        self._high: list[Decimal | None] = [to_decimal(v) for v in frame.get_column("high")]
        self._low: list[Decimal | None] = [to_decimal(v) for v in frame.get_column("low")]
        self._close: list[Decimal | None] = [to_decimal(v) for v in frame.get_column("close")]
        self._volume: list[Decimal | None] = (
            [to_decimal(v) for v in frame.get_column("volume")]
            if "volume" in frame.columns
            else [None] * self._row_count
        )
        self._is_synthetic: list[bool] = (
            [bool(v) for v in frame.get_column("is_synthetic")]
            if "is_synthetic" in frame.columns
            else [False] * self._row_count
        )

        self._feature_columns: tuple[str, ...] = feature_frame.feature_columns
        self._feature_values: dict[str, list[float | None]] = {
            column: frame.get_column(column).to_list()
            for column in self._feature_columns
            if column in frame.columns
        }

        self._session_index: list[int] = self._resolve_sessions(frame, session_column)
        self._is_session_open, self._is_session_close, self._bar_index_in_session = (
            self._session_positions(self._session_index)
        )

        _LOGGER.info(
            "engine.context_factory_ready",
            rows=self._row_count,
            features=len(self._feature_columns),
            first_decision_index=self.first_decision_index,
            availability_lag_bars=AVAILABILITY_LAG_BARS,
            sessions=len(set(self._session_index)) if self._session_index else 0,
        )

    # -- guards ------------------------------------------------------------------------

    @staticmethod
    def _guard_shift(feature_frame: FeatureFrame) -> None:
        """Refuse a frame that already carries an availability shift.

        Args:
            feature_frame: Frame to check.

        Raises:
            ConfigurationError: If the frame was shifted in the pipeline. Applying a
                second shift here would serve T-2 features to every strategy, and nothing
                downstream would fail — the backtest would run clean on stale data.
        """
        if feature_frame.availability_shift_applied != 0:
            raise ConfigurationError(
                "Feature frame already carries an availability shift; shifting again "
                "here would serve T-2 features and no test downstream would notice",
                shift_applied=feature_frame.availability_shift_applied,
                lag_policy=str(feature_frame.lag_policy),
                expected_policy=str(LagPolicy.DEFER_TO_ENGINE),
            )

    @staticmethod
    def _guard_audit(feature_frame: FeatureFrame, *, require_audit: bool) -> None:
        """Refuse a frame whose causality has not been mechanically verified.

        Args:
            feature_frame: Frame to check.
            require_audit: Whether verification is mandatory.

        Raises:
            ConfigurationError: If the frame was never audited, or failed its audit.
        """
        if not require_audit:
            return
        report = feature_frame.lag_audit
        if report is None:
            raise ConfigurationError(
                "Feature frame was not audited for look-ahead bias; Directive 1 is not "
                "satisfied by a frame whose causality was never tested"
            )
        if not report.passed:
            raise ConfigurationError(
                "Feature frame failed its prefix-invariance audit",
                failing_columns=list(report.failing_columns),
            )

    # -- sessions ----------------------------------------------------------------------

    @staticmethod
    def _resolve_sessions(frame: object, session_column: str | None) -> list[int]:
        """Return a session index per row.

        Args:
            frame: The feature frame's underlying Polars frame.
            session_column: Column carrying a monotonic session index, if any.

        Returns:
            One session index per row. All zeros when no session column is supplied,
            which treats the run as a single continuous session.
        """
        columns: Sequence[str] = getattr(frame, "columns", ())
        height: int = int(getattr(frame, "height", 0))
        if session_column is None or session_column not in columns:
            return [0] * height
        return [int(value) for value in frame.get_column(session_column)]  # type: ignore[attr-defined]

    @staticmethod
    def _session_positions(
        session_index: Sequence[int],
    ) -> tuple[list[bool], list[bool], list[int]]:
        """Derive per-bar session position flags.

        Args:
            session_index: Session index per row.

        Returns:
            A triple of (is_session_open, is_session_close, bar_index_in_session).
        """
        count: int = len(session_index)
        opens: list[bool] = [False] * count
        closes: list[bool] = [False] * count
        positions: list[int] = [0] * count

        for index in range(count):
            previous: int | None = session_index[index - 1] if index > 0 else None
            following: int | None = session_index[index + 1] if index + 1 < count else None
            opens[index] = previous is None or session_index[index] != previous
            closes[index] = following is None or session_index[index] != following
            positions[index] = 0 if opens[index] else positions[index - 1] + 1

        return opens, closes, positions

    # -- public API --------------------------------------------------------------------

    @property
    def bar_count(self) -> int:
        """Return the number of bars in the run."""
        return self._row_count

    @property
    def first_decision_index(self) -> int:
        """Return the first bar index at which a decision may be taken.

        Returns:
            The availability lag, since bar zero has no preceding feature row. Bars below
            this index are not decided on at all rather than being decided on with
            improvised feature values.
        """
        return AVAILABILITY_LAG_BARS

    @property
    def last_decision_index(self) -> int:
        """Return the last bar index at which a decision may be taken.

        Returns:
            The second-to-last bar under next-bar-open execution, because a decision on
            the final bar has no bar to fill against. Deciding there and quietly dropping
            the order would understate trade count and overstate the strategy's
            selectivity.
        """
        return self._row_count - 2

    def bar_at(self, index: int) -> BarData:
        """Return the bar at an index.

        Args:
            index: Row index.

        Returns:
            The bar, with prices as exact decimals.

        Raises:
            IndexError: If the index is out of range.
            SchemaContractError: If the row carries a null price.
        """
        if not 0 <= index < self._row_count:
            raise IndexError(f"Bar index {index} outside 0..{self._row_count - 1}")

        open_, high, low, close = (
            self._open[index],
            self._high[index],
            self._low[index],
            self._close[index],
        )
        if open_ is None or high is None or low is None or close is None:
            raise SchemaContractError(
                "Bar carries a null price at the execution boundary",
                index=index,
                ts=self._timestamps[index].isoformat(),
            )
        return BarData(
            ts_utc=self._timestamps[index],
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=self._volume[index],
            is_synthetic=self._is_synthetic[index],
        )

    def feature_row_for(self, index: int) -> FeatureRow:
        """Return the feature row a decision at ``index`` may see.

        Args:
            index: Index of the bar being decided.

        Returns:
            Feature values from bar ``index - AVAILABILITY_LAG_BARS``, stamped with the
            timestamp they were computed on.

        Raises:
            IndexError: If the lagged row does not exist.
        """
        source: int = index - AVAILABILITY_LAG_BARS
        if source < 0:
            raise IndexError(
                f"Bar {index} has no feature row at lag {AVAILABILITY_LAG_BARS}; "
                f"decisions start at index {self.first_decision_index}"
            )
        values: Mapping[str, float | None] = {
            column: series[source] for column, series in self._feature_values.items()
        }
        return FeatureRow(as_of_ts_utc=self._timestamps[source], values=values)

    def session_at(self, index: int) -> SessionInfo:
        """Return session position information for a bar.

        Args:
            index: Row index.

        Returns:
            Where the bar sits within its session.
        """
        return SessionInfo(
            session_index=self._session_index[index],
            bar_index_in_session=self._bar_index_in_session[index],
            is_session_open=self._is_session_open[index],
            is_session_close=self._is_session_close[index],
        )

    def build(self, index: int, portfolio: PortfolioView) -> BarContext:
        """Assemble the context for one decision.

        Args:
            index: Index of the bar being decided.
            portfolio: Read-only account state at this bar.

        Returns:
            The strategy's entire view of the world for this bar.

        Raises:
            IndexError: If the index precedes :attr:`first_decision_index`.
        """
        if index < self.first_decision_index:
            raise IndexError(
                f"Cannot build a context at index {index}; the availability lag means "
                f"decisions start at index {self.first_decision_index}"
            )
        return BarContext(
            bar=self.bar_at(index),
            features=self.feature_row_for(index),
            instrument=self._instrument,
            portfolio=portfolio,
            session=self.session_at(index),
            bar_index=index,
        )
