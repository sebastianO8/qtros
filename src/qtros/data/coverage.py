"""Bar series quality assessment.

This module is **purely descriptive**. It detects and reports; it never sorts, fills,
deduplicates, or repairs the frame it is given. Repair is a decision with consequences
for backtest validity, so it belongs to ingestion and loading where the policy governing
it is explicit, not to the component whose job is to tell the truth about the data.

The vectorised predicates here are shared with ingestion, which uses them to reject
malformed rows at the boundary. Sharing the expressions rather than duplicating them
means the definition of "impossible bar" exists exactly once.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Final

import polars as pl
import structlog

from qtros.domain.data_contracts import (
    CoverageReport,
    DuplicateRecord,
    GapRecord,
    OhlcViolationRecord,
    SessionCalendarLike,
)
from qtros.domain.enums import GapKind, OhlcViolation, Timeframe
from qtros.domain.errors import SchemaContractError
from qtros.domain.identifiers import InstrumentId

_LOGGER: Final = structlog.get_logger(__name__)

TS_COLUMN: Final[str] = "ts_utc"
PRICE_COLUMNS: Final[tuple[str, str, str, str]] = ("open", "high", "low", "close")
VOLUME_COLUMN: Final[str] = "volume"
SYNTHETIC_COLUMN: Final[str] = "is_synthetic"

MAX_REPORTED_VIOLATIONS: Final[int] = 500
"""Cap on individually reported bad rows.

A frame with more than five hundred impossible bars has a systemic problem — usually a
wrong column mapping or a misparsed decimal separator — and enumerating fifty thousand
of them helps nobody. The full count is always reported even when the detail is capped.
"""


# --------------------------------------------------------------------------------------
# Shared predicates
# --------------------------------------------------------------------------------------


def _violation_expressions(has_volume: bool) -> Mapping[OhlcViolation, pl.Expr]:
    """Return the boolean expression defining each OHLC violation.

    Args:
        has_volume: Whether the frame carries a volume column.

    Returns:
        Mapping from violation kind to a Polars predicate that is ``True`` for offending
        rows. Null-safety matters here: a comparison against null yields null in Polars,
        which is not ``True``, so null prices are caught by their own dedicated predicate
        rather than slipping past the inequality checks.
    """
    open_, high, low, close = (pl.col(name) for name in PRICE_COLUMNS)

    expressions: dict[OhlcViolation, pl.Expr] = {
        OhlcViolation.NULL_PRICE: pl.any_horizontal(
            [pl.col(name).is_null() for name in PRICE_COLUMNS]
        ),
        OhlcViolation.HIGH_BELOW_LOW: high < low,
        OhlcViolation.HIGH_BELOW_OPEN: high < open_,
        OhlcViolation.HIGH_BELOW_CLOSE: high < close,
        OhlcViolation.LOW_ABOVE_OPEN: low > open_,
        OhlcViolation.LOW_ABOVE_CLOSE: low > close,
        OhlcViolation.NON_POSITIVE_PRICE: pl.any_horizontal(
            [pl.col(name) <= 0.0 for name in PRICE_COLUMNS]
        ),
    }
    if has_volume:
        expressions[OhlcViolation.NEGATIVE_VOLUME] = pl.col(VOLUME_COLUMN) < 0.0
    return expressions


def ohlc_violation_mask(frame: pl.DataFrame) -> pl.Series:
    """Return a boolean mask that is ``True`` for every internally impossible bar.

    Used by ingestion to reject rows before they are written, and by
    :func:`find_ohlc_violations` to locate rows worth describing in detail.

    Args:
        frame: Frame carrying the canonical price columns.

    Returns:
        Boolean series aligned with ``frame``.

    Raises:
        SchemaContractError: If a required price column is absent.
    """
    _require_columns(frame, PRICE_COLUMNS)
    expressions: Mapping[OhlcViolation, pl.Expr] = _violation_expressions(
        VOLUME_COLUMN in frame.columns
    )
    combined: pl.DataFrame = frame.select(
        pl.any_horizontal(list(expressions.values())).fill_null(True).alias("_violates")
    )
    return combined.get_column("_violates")


def violation_counts(frame: pl.DataFrame) -> Mapping[OhlcViolation, int]:
    """Count rows breaching each OHLC rule, uncapped.

    Ingestion uses this to build its rejection tally, so that a report says *why* rows
    were discarded rather than only how many. A thousand ``NULL_PRICE`` rejections and a
    thousand ``NON_POSITIVE_PRICE`` rejections indicate completely different upstream
    faults, and the aggregate number hides that distinction.

    Args:
        frame: Frame carrying the canonical price columns.

    Returns:
        Mapping from violation kind to row count. Kinds with no occurrences are omitted.

    Raises:
        SchemaContractError: If a required price column is absent.
    """
    _require_columns(frame, PRICE_COLUMNS)
    expressions: Mapping[OhlcViolation, pl.Expr] = _violation_expressions(
        VOLUME_COLUMN in frame.columns
    )
    if frame.height == 0:
        return {}

    totals: dict[str, int] = frame.select(
        [
            expression.fill_null(kind is OhlcViolation.NULL_PRICE)
            .sum()
            .alias(str(kind))
            for kind, expression in expressions.items()
        ]
    ).row(0, named=True)

    return {
        OhlcViolation(name): int(count) for name, count in totals.items() if int(count) > 0
    }


def find_ohlc_violations(frame: pl.DataFrame) -> tuple[tuple[OhlcViolationRecord, ...], int]:
    """Describe every internally impossible bar, up to the reporting cap.

    Args:
        frame: Frame carrying ``ts_utc`` and the canonical price columns.

    Returns:
        A pair of (detailed records, total violation count). The record tuple is capped
        at :data:`MAX_REPORTED_VIOLATIONS`; the count never is.

    Raises:
        SchemaContractError: If a required column is absent.
    """
    _require_columns(frame, (TS_COLUMN, *PRICE_COLUMNS))
    expressions: Mapping[OhlcViolation, pl.Expr] = _violation_expressions(
        VOLUME_COLUMN in frame.columns
    )

    flagged: pl.DataFrame = frame.select(
        pl.col(TS_COLUMN),
        *[
            expression.fill_null(kind is OhlcViolation.NULL_PRICE).alias(str(kind))
            for kind, expression in expressions.items()
        ],
    )
    kind_names: list[str] = [str(kind) for kind in expressions]
    offenders: pl.DataFrame = flagged.filter(pl.any_horizontal([pl.col(n) for n in kind_names]))

    total: int = offenders.height
    records: list[OhlcViolationRecord] = []
    for row in offenders.head(MAX_REPORTED_VIOLATIONS).iter_rows(named=True):
        breaches: tuple[OhlcViolation, ...] = tuple(
            OhlcViolation(name) for name in kind_names if bool(row[name])
        )
        records.append(
            OhlcViolationRecord(ts_utc=row[TS_COLUMN], violations=breaches)
        )
    return tuple(records), total


# --------------------------------------------------------------------------------------
# Duplicates and ordering
# --------------------------------------------------------------------------------------


def find_duplicates(frame: pl.DataFrame) -> tuple[DuplicateRecord, ...]:
    """Locate repeated timestamps and determine whether their rows disagree.

    The conflict flag matters more than the duplicate count. A vendor re-delivering an
    identical bar is harmless; two rows claiming the same instant with different prices
    means two sources disagree about what the market did, and no automatic rule can
    legitimately pick a winner.

    Args:
        frame: Frame carrying ``ts_utc`` and the canonical price columns.

    Returns:
        One record per repeated timestamp, ordered chronologically, capped at
        :data:`MAX_REPORTED_VIOLATIONS`.

    Raises:
        SchemaContractError: If a required column is absent.
    """
    _require_columns(frame, (TS_COLUMN, *PRICE_COLUMNS))

    value_columns: list[str] = [
        name for name in (*PRICE_COLUMNS, VOLUME_COLUMN) if name in frame.columns
    ]
    grouped: pl.DataFrame = (
        frame.group_by(TS_COLUMN)
        .agg(
            pl.len().alias("occurrences"),
            pl.struct(value_columns).n_unique().alias("distinct_values"),
        )
        .filter(pl.col("occurrences") > 1)
        .sort(TS_COLUMN)
    )

    return tuple(
        DuplicateRecord(
            ts_utc=row[TS_COLUMN],
            occurrences=int(row["occurrences"]),
            values_conflict=int(row["distinct_values"]) > 1,
        )
        for row in grouped.head(MAX_REPORTED_VIOLATIONS).iter_rows(named=True)
    )


def count_out_of_order_rows(frame: pl.DataFrame) -> int:
    """Count rows whose timestamp precedes their predecessor's in the frame's own order.

    Run this before sorting. Once a frame is sorted the answer is always zero, which is
    why loading sorts but ingestion measures first: a source that arrives shuffled is
    telling you something about the vendor's export.

    Args:
        frame: Frame carrying ``ts_utc``, in its original row order.

    Returns:
        Number of descending steps found.

    Raises:
        SchemaContractError: If ``ts_utc`` is absent.
    """
    _require_columns(frame, (TS_COLUMN,))
    if frame.height < 2:
        return 0
    descending: pl.DataFrame = frame.select(
        (pl.col(TS_COLUMN).diff() < timedelta(0)).fill_null(False).alias("_back")
    )
    return int(descending.get_column("_back").sum())


# --------------------------------------------------------------------------------------
# Gaps
# --------------------------------------------------------------------------------------


def find_gaps(
    frame: pl.DataFrame,
    timeframe: Timeframe,
    *,
    calendar: SessionCalendarLike | None = None,
    max_tolerable_gap: timedelta = timedelta(0),
) -> tuple[GapRecord, ...]:
    """Locate discontinuities in a bar series and attribute them where possible.

    Args:
        frame: Frame carrying ``ts_utc``. Sorted internally for this computation; the
            caller's frame is not modified.
        timeframe: Interval used to derive the expected spacing.
        calendar: Session calendar used to attribute each gap. When absent, every gap is
            reported as :attr:`GapKind.UNCLASSIFIED` rather than assumed benign — an
            unattributed weekend and an unattributed outage look identical, and guessing
            in favour of the market being closed is how missing data becomes invisible.
        max_tolerable_gap: Discontinuities at or below this span are not reported.

    Returns:
        Chronologically ordered gap records.

    Raises:
        SchemaContractError: If ``ts_utc`` is absent.
    """
    _require_columns(frame, (TS_COLUMN,))
    if frame.height < 2:
        return ()

    interval: timedelta = timeframe.duration
    threshold: timedelta = interval + max_tolerable_gap

    spans: pl.DataFrame = (
        frame.select(pl.col(TS_COLUMN))
        .sort(TS_COLUMN)
        .select(
            pl.col(TS_COLUMN).shift(1).alias("prev_ts"),
            pl.col(TS_COLUMN).alias("next_ts"),
        )
        .drop_nulls()
        .with_columns((pl.col("next_ts") - pl.col("prev_ts")).alias("delta"))
        .filter(pl.col("delta") > threshold)
    )

    records: list[GapRecord] = []
    for row in spans.iter_rows(named=True):
        previous: datetime = row["prev_ts"]
        following: datetime = row["next_ts"]
        duration: timedelta = row["delta"]
        kind: GapKind = (
            calendar.classify_gap(previous, following)
            if calendar is not None
            else GapKind.UNCLASSIFIED
        )
        missing: int = max(int(duration // interval) - 1, 0)
        records.append(
            GapRecord(
                start_ts_utc=previous,
                end_ts_utc=following,
                duration=duration,
                missing_bars=missing,
                kind=kind,
            )
        )
    return tuple(records)


# --------------------------------------------------------------------------------------
# Top-level assessment
# --------------------------------------------------------------------------------------


def analyze_coverage(
    frame: pl.DataFrame,
    *,
    instrument_id: InstrumentId,
    timeframe: Timeframe,
    calendar: SessionCalendarLike | None = None,
    max_tolerable_gap: timedelta = timedelta(0),
) -> CoverageReport:
    """Produce a full quality assessment of a bar series.

    Args:
        frame: Bars to assess, in their original row order.
        instrument_id: Instrument the bars describe.
        timeframe: Interval the bars describe.
        calendar: Optional session calendar for gap attribution.
        max_tolerable_gap: Discontinuities at or below this span are ignored.

    Returns:
        A populated coverage report. An empty frame yields a report with zero counts and
        null bounds rather than an error; "no data" is a legitimate finding.

    Raises:
        SchemaContractError: If a required column is absent.
    """
    _require_columns(frame, (TS_COLUMN, *PRICE_COLUMNS))

    if frame.height == 0:
        return CoverageReport(
            instrument_id=instrument_id,
            timeframe=timeframe,
            row_count=0,
            first_ts_utc=None,
            last_ts_utc=None,
            analyzed_at_utc=datetime.now(UTC),
        )

    timestamps: pl.Series = frame.get_column(TS_COLUMN)
    violations, _violation_total = find_ohlc_violations(frame)

    zero_volume: int = 0
    null_volume: int = 0
    if VOLUME_COLUMN in frame.columns:
        volume: pl.Series = frame.get_column(VOLUME_COLUMN)
        zero_volume = int((volume == 0.0).sum())
        null_volume = int(volume.null_count())

    flat_bars: int = int(
        frame.select((pl.col("high") == pl.col("low")).fill_null(False).alias("_flat"))
        .get_column("_flat")
        .sum()
    )
    synthetic_bars: int = (
        int(frame.get_column(SYNTHETIC_COLUMN).sum())
        if SYNTHETIC_COLUMN in frame.columns
        else 0
    )

    report = CoverageReport(
        instrument_id=instrument_id,
        timeframe=timeframe,
        row_count=frame.height,
        first_ts_utc=timestamps.min(),  # type: ignore[arg-type]
        last_ts_utc=timestamps.max(),  # type: ignore[arg-type]
        gaps=find_gaps(
            frame,
            timeframe,
            calendar=calendar,
            max_tolerable_gap=max_tolerable_gap,
        ),
        duplicates=find_duplicates(frame),
        ohlc_violations=violations,
        zero_volume_bars=zero_volume,
        flat_bars=flat_bars,
        out_of_order_rows=count_out_of_order_rows(frame),
        synthetic_bars=synthetic_bars,
        null_volume_bars=null_volume,
        analyzed_at_utc=datetime.now(UTC),
    )

    _LOGGER.info(
        "coverage.analyzed",
        instrument_id=str(instrument_id),
        timeframe=str(timeframe),
        rows=report.row_count,
        unexpected_gaps=len(report.unexpected_gaps),
        duplicates=len(report.duplicates),
        conflicting_duplicates=len(report.conflicting_duplicates),
        ohlc_violations=len(report.ohlc_violations),
        out_of_order=report.out_of_order_rows,
        calendar_supplied=calendar is not None,
        is_clean=report.is_clean,
    )
    return report


def _require_columns(frame: pl.DataFrame, required: tuple[str, ...]) -> None:
    """Assert that a frame carries every named column.

    Args:
        frame: Frame to check.
        required: Column names that must be present.

    Raises:
        SchemaContractError: If any column is missing.
    """
    missing: tuple[str, ...] = tuple(name for name in required if name not in frame.columns)
    if missing:
        raise SchemaContractError(
            "Frame is missing columns required for coverage analysis",
            missing=list(missing),
            present=frame.columns,
        )
