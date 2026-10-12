"""Aggregation of fine bars into coarser bars at the ingestion boundary.

Some vendors publish only one-minute bars while a strategy is defined on five-minute
bars. Aggregation is done exactly once, inside Layer 0, before the bars are stored. Doing
it anywhere later would let two consumers aggregate the same data two different ways.

Rules:

* **Windows are left-closed and left-labelled.** A five-minute bar stamped 10:05 covers
  10:05:00 up to but excluding 10:10:00, which matches the system-wide convention that a
  timestamp labels a bar's opening instant.
* **Windows align to the UTC epoch.** Five-minute windows start on :00, :05, :10, ...
  regardless of where the data begins, so two ingestions of overlapping files produce
  identical bars.
* **Missing constituent bars are not invented.** A window built from three of its five
  minutes is still the true open, high, low, and close of the trades that happened. A
  window with no constituent bars produces no bar at all, and coverage analysis reports
  the gap.
* **Unknown volume stays unknown.** If every constituent volume is null the aggregate is
  null, not zero.
"""

from __future__ import annotations

from typing import Final

import polars as pl

from qtros.domain.enums import Timeframe
from qtros.domain.errors import ConfigurationError, SchemaContractError

AGGREGATION_INPUT_COLUMNS: Final[tuple[str, ...]] = (
    "ts_utc",
    "open",
    "high",
    "low",
    "close",
    "volume",
)

_MAX_INTRADAY: Final[Timeframe] = Timeframe.H4
"""Coarsest target supported. Daily bars need the session calendar to place their
boundary (the FX day rolls at 17:00 New York, not at UTC midnight), so they are refused
here rather than silently aligned to UTC midnight."""


def validate_aggregation(source: Timeframe, target: Timeframe) -> None:
    """Refuse an aggregation that cannot be done exactly.

    Args:
        source: Interval of the raw bars.
        target: Interval to aggregate into.

    Raises:
        ConfigurationError: If the target is not coarser than the source, not an exact
            multiple of it, or is daily or coarser.
    """
    if target.duration <= source.duration:
        raise ConfigurationError(
            "aggregation target must be coarser than the source interval",
            source=str(source),
            target=str(target),
        )
    if target.duration % source.duration:
        raise ConfigurationError(
            "aggregation target must be an exact multiple of the source interval",
            source=str(source),
            target=str(target),
        )
    if target.duration > _MAX_INTRADAY.duration:
        raise ConfigurationError(
            "daily aggregation needs the session calendar's trading-day boundary and is "
            "not supported at ingestion",
            target=str(target),
        )


def aggregate_bars(frame: pl.DataFrame, *, source: Timeframe, target: Timeframe) -> pl.DataFrame:
    """Aggregate validated bars into a coarser interval.

    Args:
        frame: Bars with at least :data:`AGGREGATION_INPUT_COLUMNS`, UTC-aware
            timestamps, already validated and deduplicated.
        source: Interval of ``frame``.
        target: Interval to produce.

    Returns:
        One row per non-empty target window, sorted ascending, with the same columns as
        :data:`AGGREGATION_INPUT_COLUMNS`.

    Raises:
        ConfigurationError: If the aggregation is not exact.
        SchemaContractError: If a required column is missing.
    """
    validate_aggregation(source, target)
    missing = [c for c in AGGREGATION_INPUT_COLUMNS if c not in frame.columns]
    if missing:
        raise SchemaContractError(
            "frame is missing columns required for aggregation", missing=missing
        )
    if frame.height == 0:
        return frame.select(AGGREGATION_INPUT_COLUMNS)

    every = f"{int(target.duration.total_seconds())}s"
    return (
        frame.sort("ts_utc")
        .group_by_dynamic("ts_utc", every=every, closed="left", label="left", start_by="window")
        .agg(
            pl.col("open").first(),
            pl.col("high").max(),
            pl.col("low").min(),
            pl.col("close").last(),
            pl.when(pl.col("volume").is_not_null().any())
            .then(pl.col("volume").sum())
            .otherwise(None)
            .cast(pl.Float64)
            .alias("volume"),
        )
        .select(AGGREGATION_INPUT_COLUMNS)
    )
