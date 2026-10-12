"""The range of a recurring session, and how far price has swept beyond it since.

Built for the Asian-session sweep (``audjpy_asian_sweep``). The session is a wall-clock
window in a named time zone, so its UTC position moves with daylight saving; the window may
cross midnight (19:00-04:00 New York). For every bar the transform emits:

* ``{prefix}_session_id`` - increments at the first bar of each session. Bars before the
  first session are 0. Declared as the warmup session column, so the pipeline drops the
  first, possibly partial, session.
* ``{prefix}_range_high`` / ``{prefix}_range_low`` - the running high and low of the
  current session while it is open, then held fixed until the next session starts.
* ``{prefix}_bars_since_end`` - for bars after the session, 0 on the first such bar and
  counting up; null while the session is open.
* ``{prefix}_sweep_low`` / ``{prefix}_sweep_high`` - after the session, the lowest low
  below the range low (highest high above the range high) seen so far; null until price
  first trades beyond the range.

Every column is a cumulative operation within one session, so the value at bar *t* depends
only on bars at or before *t*. Bars, not clock time, are counted, so a weekend inside the
post-session stretch adds nothing.
"""

from __future__ import annotations

from datetime import time
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import polars as pl

from qtros.domain.errors import ConfigurationError
from qtros.domain.feature_contracts import Causality, FeatureSpec, WarmupRequirement
from qtros.features.transforms.base import TransformBase, register_transform

_IN: Final[str] = "__session_range_in"
_ID: Final[str] = "__session_range_id"
_HIGH: Final[str] = "__session_range_high"
_LOW: Final[str] = "__session_range_low"


def _parse_clock(value: str, name: str) -> time:
    """Parse ``HH:MM`` into a time, refusing anything else."""
    try:
        hours, minutes = (int(part) for part in value.split(":"))
        return time(hours, minutes)
    except (ValueError, TypeError) as exc:
        raise ConfigurationError(f"{name} must be HH:MM", value=value) from exc


@register_transform("session_range")
class SessionRange(TransformBase):
    """Session range and post-session sweep extremes.

    Args:
        timezone: IANA zone the window is expressed in.
        range_start: Window open, ``HH:MM`` local. Bars whose open time is at or after it
            belong to the session.
        range_end: Window close, ``HH:MM`` local, exclusive.
        prefix: Prefix for every emitted column.

    Raises:
        ConfigurationError: On an unknown zone, a malformed time, or an empty window.
    """

    def __init__(
        self,
        timezone: str = "America/New_York",
        range_start: str = "19:00",
        range_end: str = "04:00",
        prefix: str = "asian",
    ) -> None:
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigurationError("Unknown session timezone", timezone=timezone) from exc
        start = _parse_clock(range_start, "range_start")
        end = _parse_clock(range_end, "range_end")
        if start == end:
            raise ConfigurationError("Session window cannot be empty", start=range_start)
        if not prefix or not prefix.replace("_", "").isalnum():
            raise ConfigurationError("prefix must be alphanumeric", prefix=prefix)
        self._timezone: str = timezone
        self._start: time = start
        self._end: time = end
        self._prefix: str = prefix

    def _column(self, suffix: str) -> str:
        return f"{self._prefix}_{suffix}"

    @property
    def spec(self) -> FeatureSpec:
        """Return this transform's identity."""
        return FeatureSpec(
            name="session_range",
            version="1.0.0",
            parameters={
                "timezone": self._timezone,
                "range_start": self._start.strftime("%H:%M"),
                "range_end": self._end.strftime("%H:%M"),
                "prefix": self._prefix,
            },
            output_columns=(
                self._column("session_id"),
                self._column("range_high"),
                self._column("range_low"),
                self._column("bars_since_end"),
                self._column("sweep_low"),
                self._column("sweep_high"),
            ),
            depends_on=("ts_utc", "high", "low"),
            causality=Causality.CAUSAL,
        )

    @property
    def warmup(self) -> WarmupRequirement:
        """Drop the first session: the data may have joined it partway through."""
        return WarmupRequirement(bars=0, session_column=self._column("session_id"))

    def _build(self, frame: pl.LazyFrame) -> pl.LazyFrame:
        """Append the session columns.

        Args:
            frame: Lazy frame carrying ``ts_utc``, ``high``, and ``low``.

        Returns:
            The frame with the six declared columns appended, all ``Float64``.
        """
        local = pl.col("ts_utc").dt.convert_time_zone(self._timezone)
        minute = local.dt.hour().cast(pl.Int32) * 60 + local.dt.minute().cast(pl.Int32)
        start = self._start.hour * 60 + self._start.minute
        end = self._end.hour * 60 + self._end.minute
        in_window = (
            (minute >= start) | (minute < end)
            if start > end
            else (minute >= start) & (minute < end)
        )
        starts = pl.col(_IN) & ~pl.col(_IN).shift(1).fill_null(value=False)
        post = ~pl.col(_IN) & (pl.col(_ID) > 0)

        staged = (
            frame.with_columns(in_window.alias(_IN))
            .with_columns(starts.cast(pl.Int64).cum_sum().alias(_ID))
            .with_columns(
                pl.when(pl.col(_IN))
                .then(pl.col("high"))
                .otherwise(None)
                .cum_max()
                .forward_fill()
                .over(_ID)
                .alias(_HIGH),
                pl.when(pl.col(_IN))
                .then(pl.col("low"))
                .otherwise(None)
                .cum_min()
                .forward_fill()
                .over(_ID)
                .alias(_LOW),
            )
        )
        return staged.with_columns(
            pl.col(_ID).cast(pl.Float64).alias(self._column("session_id")),
            pl.col(_HIGH).cast(pl.Float64).alias(self._column("range_high")),
            pl.col(_LOW).cast(pl.Float64).alias(self._column("range_low")),
            pl.when(post)
            .then(post.cast(pl.Int64).cum_sum().over(_ID) - 1)
            .otherwise(None)
            .cast(pl.Float64)
            .alias(self._column("bars_since_end")),
            pl.when(post & (pl.col("low") < pl.col(_LOW)))
            .then(pl.col("low"))
            .otherwise(None)
            .cum_min()
            .forward_fill()
            .over(_ID)
            .cast(pl.Float64)
            .alias(self._column("sweep_low")),
            pl.when(post & (pl.col("high") > pl.col(_HIGH)))
            .then(pl.col("high"))
            .otherwise(None)
            .cum_max()
            .forward_fill()
            .over(_ID)
            .cast(pl.Float64)
            .alias(self._column("sweep_high")),
        ).drop(_IN, _ID, _HIGH, _LOW)
