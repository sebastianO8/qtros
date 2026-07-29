"""The strategy-facing surface.

:class:`BarContext` is deliberately the *entire* world a strategy can see. It carries a
closed bar, a read-only portfolio view, session information, and a
:class:`FeatureRow` — and nothing else. There is no reference to the feature frame, no
index, and no way to reach an adjacent row. Look-ahead is not forbidden here; it is
unreachable, which is a stronger property and does not depend on anyone remembering a
rule.

:class:`StrategyState` makes the functional-core ruling (ARCH-001 C-9) concrete. A
strategy holds configuration only; its evolving state is data the engine owns, which is
what makes a strategy testable at an arbitrary bar without replaying history and what
prevents state leaking across walk-forward folds.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from qtros.domain.errors import QtrosError
from qtros.domain.execution import PositionState
from qtros.domain.identifiers import InstrumentId
from qtros.domain.instrument import InstrumentSpec
from qtros.domain.market import BarData
from qtros.domain.orders import TradeIntent

ZERO: Final[Decimal] = Decimal("0")


class UnknownFeatureError(QtrosError):
    """A strategy requested a feature that is not in its declared feature set."""


@dataclass(frozen=True, slots=True)
class FeatureRow:
    """Feature values as of a single bar.

    Attributes:
        as_of_ts_utc: The bar these values were computed on. This is *not* the bar being
            decided: under the availability-lag convention it is one bar earlier. Carrying
            it explicitly means a strategy or a log line can state which bar it reasoned
            from, instead of that being an assumption the reader has to trust.
        values: Feature name to value. Values may be ``None`` where a feature is
            legitimately undefined, such as a z-score over a flat window.
    """

    as_of_ts_utc: datetime
    values: Mapping[str, float | None]

    def __getitem__(self, name: str) -> float | None:
        """Return a feature value by name.

        Args:
            name: Feature column name.

        Returns:
            The value, which may be ``None``.

        Raises:
            UnknownFeatureError: If the feature is not present. Deliberately not a
                ``None``-returning lookup: a typo'd feature name that silently reads as
                ``None`` produces a strategy that never trades and looks like a strategy
                with no edge.
        """
        if name not in self.values:
            raise UnknownFeatureError(
                "Strategy requested a feature outside its declared feature set",
                requested=name,
                available=sorted(self.values),
            )
        return self.values[name]

    def require(self, name: str) -> float:
        """Return a feature value, refusing null.

        Args:
            name: Feature column name.

        Returns:
            The value.

        Raises:
            UnknownFeatureError: If the feature is absent or null. Use this when a null
                would make the decision meaningless rather than merely cautious.
        """
        value: float | None = self[name]
        if value is None:
            raise UnknownFeatureError(
                "Required feature is null at this bar",
                requested=name,
                as_of=self.as_of_ts_utc.isoformat(),
            )
        return value

    def has(self, name: str) -> bool:
        """Return whether a feature is present and non-null."""
        return self.values.get(name) is not None


@dataclass(frozen=True, slots=True)
class PortfolioView:
    """Read-only projection of account state, safe to hand to a strategy.

    Attributes:
        cash: Settled cash.
        closed_equity: Equity counting only closed positions.
        mtm_equity: Equity including open mark-to-market.
        open_position: The current position, if any.
        realized_pnl_to_date: Cumulative realised PnL for the run.
        trade_count_to_date: Closed trades so far.
        is_halted: Whether the risk engine has halted trading.
    """

    cash: Decimal
    closed_equity: Decimal
    mtm_equity: Decimal
    open_position: PositionState | None
    realized_pnl_to_date: Decimal
    trade_count_to_date: int
    is_halted: bool = False

    @property
    def is_flat(self) -> bool:
        """Return whether no position is currently open."""
        return self.open_position is None


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Where the current bar sits within the trading session.

    Attributes:
        session_index: Monotonic session counter within the run.
        bar_index_in_session: Position of this bar within its session.
        is_session_open: Whether this is the session's first bar.
        is_session_close: Whether this is the session's last bar.
        trading_day: The trading day this bar belongs to, per the instrument's rollover
            rule. For FX this follows a New York wall-clock boundary and is therefore not
            the UTC date — the daily loss limit and the 50% consistency rule both depend
            on getting this right.
    """

    session_index: int
    bar_index_in_session: int
    is_session_open: bool
    is_session_close: bool
    trading_day: datetime | None = None


@dataclass(frozen=True, slots=True)
class BarContext:
    """Everything a strategy may see when deciding on one bar.

    Attributes:
        bar: The bar being decided. Fully closed, so its close is known.
        features: Feature values as of the *previous* bar.
        instrument: Contract specification.
        portfolio: Read-only account state.
        session: Session position of this bar.
        bar_index: Index of this bar within the run, for logging only.
    """

    bar: BarData
    features: FeatureRow
    instrument: InstrumentSpec
    portfolio: PortfolioView
    session: SessionInfo
    bar_index: int

    @property
    def ts_utc(self) -> datetime:
        """Return the bar's timestamp."""
        return self.bar.ts_utc

    @property
    def instrument_id(self) -> InstrumentId:
        """Return the instrument being traded."""
        return self.instrument.instrument_id

    @property
    def feature_lag_bars(self) -> int:
        """Return how many bars stale the feature row is.

        Returns:
            One under the standard convention. Exposed so a strategy can assert on it
            rather than assume it, and so a log line can state it.
        """
        return 0 if self.features.as_of_ts_utc == self.bar.ts_utc else 1


@dataclass(frozen=True, slots=True)
class StrategyState:
    """A strategy's evolving state, owned by the engine rather than the strategy.

    Attributes:
        bars_seen: Bars evaluated so far.
        values: Arbitrary strategy-defined state. Kept as a plain mapping so the engine
            can snapshot and diff it without knowing what any strategy stores.
        last_signal_ts_utc: When the strategy last emitted an intent, for cooldowns.
    """

    bars_seen: int = 0
    values: Mapping[str, Any] = field(default_factory=dict)
    last_signal_ts_utc: datetime | None = None

    def advanced(self, **updates: Any) -> StrategyState:  # noqa: ANN401
        """Return a successor state with the bar counter incremented.

        Args:
            **updates: Strategy-defined values to merge into the state.

        Returns:
            A new state. The original is unchanged, which is what makes a backtest
            re-runnable and a walk-forward fold unable to inherit its predecessor's state.
        """
        merged: dict[str, Any] = dict(self.values)
        merged.update(updates)
        return replace(self, bars_seen=self.bars_seen + 1, values=merged)


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    """A strategy's output for one bar.

    Attributes:
        state: The successor state. Always returned, never mutated in place.
        intents: Trade intents emitted this bar, in priority order. Exits should precede
            entries so a reversal closes before it opens.
        diagnostics: Values recorded to the run log for later attribution analysis. Cheap
            to write now and the only way to answer "why did it not trade here" later.
    """

    state: StrategyState
    intents: tuple[TradeIntent, ...] = ()
    diagnostics: Mapping[str, float] = field(default_factory=dict)

    @property
    def has_intents(self) -> bool:
        """Return whether the strategy wants to trade this bar."""
        return bool(self.intents)
