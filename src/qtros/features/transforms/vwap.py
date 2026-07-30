"""Session-anchored VWAP.

VWAP is anchored, not rolling: it accumulates from a session boundary and resets. That makes
its warmup a *session* requirement rather than a bar count, which is why
:class:`WarmupRequirement` carries a session column at all. A VWAP computed from the middle
of a session — because that is where the dataset happens to begin — is not a slightly-noisy
VWAP, it is a different statistic, and a strategy trading against it in the first session is
trading against an artefact of the download boundary.

The anchor timezone is declared explicitly and never defaults to UTC midnight. FX sessions
anchor to local market opens; UTC midnight would place the boundary in the middle of the
Asian session, which is precisely where the AUD/JPY sweep strategy operates.
"""

from __future__ import annotations

from datetime import time
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import polars as pl

from qtros.domain.errors import ConfigurationError
from qtros.domain.feature_contracts import Causality, FeatureSpec, WarmupRequirement
from qtros.features.transforms.base import (
    TYPICAL_PRICE_EXPR,
    TransformBase,
    register_transform,
    safe_divide,
)

_MICROSECONDS_PER_SECOND: Final[int] = 1_000_000


@register_transform("anchored_vwap")
class AnchoredVwap(TransformBase):
    """Volume-weighted average price, reset at a declared session boundary.

    Emits two columns, in this order: the session index, then the VWAP itself. The order
    matters — :class:`TransformBase` compares the columns actually added against
    ``output_columns`` positionally, so a declaration that disagrees with the build is caught
    rather than silently tolerated.

    Args:
        anchor_timezone: IANA zone in which the anchor time is expressed.
        anchor_time: Local wall-clock time at which a new session begins.
        output_column: Name of the emitted VWAP column.
        session_column: Name of the emitted session index column. Consumed by the pipeline to
            drop the first, incomplete session.

    Raises:
        ConfigurationError: If the timezone is not a known IANA zone. Failing at construction
            beats failing three hundred thousand rows into a computation.
    """

    def __init__(
        self,
        anchor_timezone: str = "Asia/Tokyo",
        anchor_time: time = time(0, 0),
        output_column: str = "vwap_session",
        session_column: str = "vwap_session_index",
    ) -> None:
        try:
            ZoneInfo(anchor_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigurationError(
                "Unknown anchor timezone for anchored VWAP",
                anchor_timezone=anchor_timezone,
            ) from exc

        self._timezone: str = anchor_timezone
        self._anchor_time: time = anchor_time
        self._column: str = output_column
        self._session_column: str = session_column

    @property
    def spec(self) -> FeatureSpec:
        """Return this transform's identity."""
        return FeatureSpec(
            name="anchored_vwap",
            version="1.0.0",
            parameters={
                "anchor_timezone": self._timezone,
                "anchor_time": self._anchor_time.isoformat(),
                "output_column": self._column,
                "session_column": self._session_column,
            },
            output_columns=(self._session_column, self._column),
            depends_on=("ts_utc", "high", "low", "close", "volume"),
            causality=Causality.CAUSAL,
        )

    @property
    def warmup(self) -> WarmupRequirement:
        """Return the session-scoped warmup requirement.

        Returns:
            Zero bars, plus the session column so the pipeline drops the first session.
        """
        return WarmupRequirement(bars=0, session_column=self._session_column)

    def _build(self, frame: pl.LazyFrame) -> pl.LazyFrame:
        """Append the session index and the anchored VWAP.

        The session index is built from a strictly causal comparison against the previous row
        rather than from a rank over all rows, so that removing later rows cannot change the
        index of an earlier one. Dense ranking would give the same answer here, but only by an
        argument the reader has to reconstruct; a cumulative sum of boundary crossings is
        prefix-invariant by inspection.

        Args:
            frame: Lazy frame carrying ``ts_utc``, prices, and volume.

        Returns:
            The frame with the session index and VWAP columns appended.

        Note:
            Rows with null volume yield null VWAP rather than being treated as zero volume. A
            bar with unknown volume contributes nothing knowable to a volume-weighted average,
            and silently weighting it at zero would bias the result toward whatever else
            traded that session.
        """
        anchor_offset_us: int = (
            self._anchor_time.hour * 3600
            + self._anchor_time.minute * 60
            + self._anchor_time.second
        ) * _MICROSECONDS_PER_SECOND

        local_shifted: pl.Expr = pl.col("ts_utc").dt.convert_time_zone(
            self._timezone
        ) - pl.duration(microseconds=anchor_offset_us)
        session_date: pl.Expr = local_shifted.dt.date()

        with_session: pl.LazyFrame = frame.with_columns(
            (session_date != session_date.shift(1))
            .fill_null(value=True)
            .cum_sum()
            .cast(pl.Int64)
            .alias(self._session_column)
        )

        cumulative_notional: pl.Expr = (
            (TYPICAL_PRICE_EXPR * pl.col("volume")).cum_sum().over(self._session_column)
        )
        cumulative_volume: pl.Expr = pl.col("volume").cum_sum().over(self._session_column)

        return with_session.with_columns(
            safe_divide(cumulative_notional, cumulative_volume).alias(self._column)
        )
