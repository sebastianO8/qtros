"""Enumerations shared across the system.

This module currently holds the Layer 0 subset. Later layers append to it; nothing is
renamed once written, because enum member names appear in archived configs and in
``results/`` manifests that Directive 10 requires to remain readable years later.

All enums are :class:`enum.StrEnum` so they serialise to their own names in YAML, JSON,
and DuckDB without a conversion table.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Final


class Timeframe(StrEnum):
    """Bar interval.

    The interval is load-bearing beyond labelling: Cycle 1 established that a strategy
    profitable on 5-minute bars was decisively unprofitable on 15-minute bars, so the
    timeframe is part of a strategy's identity, not a display detail.
    """

    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"

    @property
    def duration(self) -> timedelta:
        """Return the nominal wall-clock duration of one bar.

        Returns:
            The bar interval as a :class:`datetime.timedelta`.

        Note:
            ``D1`` returns 24 hours. This is the *nominal* duration and is deliberately
            not DST-aware: a daily bar spanning a DST transition is 23 or 25 hours long
            in local time. Gap detection must therefore consult a session calendar for
            daily data rather than relying on this value alone.
        """
        return _TIMEFRAME_DURATIONS[self]


_TIMEFRAME_DURATIONS: Final[dict[Timeframe, timedelta]] = {
    Timeframe.M1: timedelta(minutes=1),
    Timeframe.M5: timedelta(minutes=5),
    Timeframe.M15: timedelta(minutes=15),
    Timeframe.M30: timedelta(minutes=30),
    Timeframe.H1: timedelta(hours=1),
    Timeframe.H4: timedelta(hours=4),
    Timeframe.D1: timedelta(days=1),
}


class FileFormat(StrEnum):
    """Supported raw source file formats."""

    CSV = "CSV"
    PARQUET = "PARQUET"


class TimestampPolicy(StrEnum):
    """How a source's raw timestamp column is to be interpreted.

    ARCH-002 schema amendment A-1. ``DataSourceConfig`` originally declared only
    ``declared_tz``, which cannot express the common case of a vendor whose timestamps
    already carry an explicit UTC offset. Inferring the difference at parse time would
    reintroduce exactly the ambiguity that cost Cycle 1 an evening, so the source
    declares which case applies and parsing never guesses.
    """

    EMBEDDED_OFFSET = "EMBEDDED_OFFSET"
    """Timestamp string carries its own offset (``%z``). Converted directly to UTC."""

    DECLARED_ZONE = "DECLARED_ZONE"
    """Naive local timestamps in a named IANA zone. Subject to DST resolution."""

    DECLARED_FIXED_OFFSET = "DECLARED_FIXED_OFFSET"
    """Naive timestamps at a constant offset. No DST, therefore no ambiguity."""


class DstPolicy(StrEnum):
    """How to resolve local timestamps that DST makes ambiguous or nonexistent.

    ``STRICT_RAISE`` is the Phase 1 default. A loud failure at ingestion is recoverable;
    a plausible off-by-one-hour bar series produces a backtest that looks correct and
    is not.
    """

    STRICT_RAISE = "STRICT_RAISE"
    EARLIEST = "EARLIEST"
    LATEST = "LATEST"
    DROP_AMBIGUOUS = "DROP_AMBIGUOUS"


class IngestMode(StrEnum):
    """Write semantics for an ingestion run."""

    APPEND_ONLY = "APPEND_ONLY"
    """Insert new bars; silently skip any (instrument, timeframe, ts) already present."""

    REPLACE_RANGE = "REPLACE_RANGE"
    """Delete the incoming timestamp range for this instrument/timeframe, then insert.

    Used when a vendor reissues corrected history. The deleted row count is reported so
    a silent restatement is impossible.
    """

    DRY_RUN = "DRY_RUN"
    """Parse, validate, and report. Write nothing."""


class GapKind(StrEnum):
    """Classification of a discontinuity in a bar series.

    Only ``UNEXPECTED`` is a data quality problem. The others are the market being
    closed, and treating them as missing data produces phantom gap-fill bars that can
    generate signals on prices that never existed.
    """

    WEEKEND = "WEEKEND"
    HOLIDAY = "HOLIDAY"
    SESSION_BREAK = "SESSION_BREAK"
    UNEXPECTED = "UNEXPECTED"
    UNCLASSIFIED = "UNCLASSIFIED"
    """No session calendar was supplied, so the gap could not be attributed."""


class GapPolicy(StrEnum):
    """What ``load_bars`` does when it encounters an unexpected gap."""

    RAISE = "RAISE"
    """Abort the load. The default: a strategy should not silently run on partial data."""

    FILL_FORWARD = "FILL_FORWARD"
    """Synthesise bars flagged ``is_synthetic=True`` so downstream code can exclude them."""

    LEAVE = "LEAVE"
    """Return the frame as-is, gaps included, recorded in the coverage report."""


class OhlcViolation(StrEnum):
    """Ways a bar can be internally impossible.

    These are corruption, not gaps. The system rejects such rows rather than repairing
    them, because a repaired bar is an invented price.
    """

    HIGH_BELOW_OPEN = "HIGH_BELOW_OPEN"
    HIGH_BELOW_CLOSE = "HIGH_BELOW_CLOSE"
    LOW_ABOVE_OPEN = "LOW_ABOVE_OPEN"
    LOW_ABOVE_CLOSE = "LOW_ABOVE_CLOSE"
    HIGH_BELOW_LOW = "HIGH_BELOW_LOW"
    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    NEGATIVE_VOLUME = "NEGATIVE_VOLUME"
    NULL_PRICE = "NULL_PRICE"
