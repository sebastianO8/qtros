"""Data transfer objects exchanged across the Layer 0 boundary.

These live in ``domain/`` rather than ``data/`` because the ``IDataEngine`` protocol
references them, and ``domain/`` is forbidden from importing any other ``qtros``
package. Placing them in ``data/`` would invert the dependency and break the layering
contract in ``pyproject.toml``.

Every model here is frozen. A coverage report that can be edited after the fact is not
evidence of anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from qtros.domain.enums import (
    DstPolicy,
    FileFormat,
    GapKind,
    GapPolicy,
    IngestMode,
    OhlcViolation,
    Timeframe,
    TimestampPolicy,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from qtros.domain.identifiers import CalendarId, InstrumentId, SourceId

# --------------------------------------------------------------------------------------
# Source description
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ColumnMapping:
    """Maps this vendor's column names onto the canonical bar schema.

    Attributes:
        timestamp: Source column holding the bar timestamp.
        open: Source column holding the opening price.
        high: Source column holding the high price.
        low: Source column holding the low price.
        close: Source column holding the closing price.
        volume: Source column holding volume, or ``None`` if the vendor supplies none.
            A missing volume column is recorded as null, never as zero — zero volume is
            a meaningful market condition and must not be manufactured.
    """

    timestamp: str
    open: str
    high: str
    low: str
    close: str
    volume: str | None = None

    def price_columns(self) -> tuple[str, str, str, str]:
        """Return the four source price column names in OHLC order."""
        return (self.open, self.high, self.low, self.close)

    def all_columns(self) -> tuple[str, ...]:
        """Return every source column name this mapping requires to be present."""
        columns: list[str] = [self.timestamp, *self.price_columns()]
        if self.volume is not None:
            columns.append(self.volume)
        return tuple(columns)


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """Full description of a raw market data source.

    Constructed from ``config/sources/*.yaml`` by the configuration layer. Layer 0 never
    infers any field on this object; anything not declared here is an error rather than
    a default, because every silent default in a data pipeline eventually becomes a
    silent wrong answer.

    Attributes:
        source_id: Stable key for this source.
        instrument_id: Instrument the files describe.
        timeframe: Bar interval of the files.
        root_path: Directory containing the raw files.
        glob_pattern: Pattern matched within ``root_path``. Never recursive by default.
        file_format: Parser to use.
        columns: Vendor-to-canonical column mapping.
        timestamp_format: ``strftime``-style parse format. Required even for Parquet
            sources that store text timestamps.
        timestamp_policy: How to interpret the parsed timestamp (A-1).
        declared_zone: IANA zone name. Required iff policy is ``DECLARED_ZONE``.
        declared_offset: Constant offset. Required iff policy is
            ``DECLARED_FIXED_OFFSET``.
        dst_policy: Resolution rule for ambiguous or nonexistent local times.
        timestamps_are_bar_open: ``True`` if a timestamp labels the bar's opening
            instant, ``False`` if it labels the close. Internally the system always
            stores bar-open; close-labelled sources are shifted back one interval at
            ingestion. Getting this wrong shifts the whole series by one bar, which
            produces look-ahead bias that the feature-level prefix-invariance audit
            cannot detect, because the frame itself is already wrong.
        max_reject_ratio: Fraction of rows that may be rejected before the ingestion
            aborts. A source that is 40% malformed is a broken download, not a dirty
            file, and importing the good 60% of it hides that.
    """

    source_id: SourceId
    instrument_id: InstrumentId
    timeframe: Timeframe
    root_path: Path
    glob_pattern: str
    file_format: FileFormat
    columns: ColumnMapping
    timestamp_format: str
    timestamp_policy: TimestampPolicy
    declared_zone: str | None = None
    declared_offset: timedelta | None = None
    dst_policy: DstPolicy = DstPolicy.STRICT_RAISE
    timestamps_are_bar_open: bool = True
    csv_separator: str = ","
    has_header: bool = True
    recursive_glob: bool = False
    max_reject_ratio: float = 0.001


@dataclass(frozen=True, slots=True)
class SourceHandle:
    """A source that has passed validation and may be ingested.

    Only :meth:`IDataEngine.register_source` produces one. Ingestion accepts a handle
    and never a raw path, so the self-ingestion and input-root guards cannot be skipped
    by calling ingestion directly.

    Attributes:
        spec: The validated source description.
        files: Resolved, sorted, absolute paths that matched the glob.
        total_bytes: Combined size of the matched files at validation time.
        validated_at_utc: When validation succeeded.
    """

    spec: SourceSpec
    files: tuple[Path, ...]
    total_bytes: int
    validated_at_utc: datetime


# --------------------------------------------------------------------------------------
# Ingestion results
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IngestionReport:
    """Outcome of one ingestion run.

    Attributes:
        source_id: Source that was ingested.
        mode: Write semantics used.
        files_processed: Files actually read.
        rows_read: Rows parsed from disk before any filtering.
        rows_written: Rows newly persisted.
        rows_skipped_duplicate: Rows already present at the same
            (instrument, timeframe, timestamp) key.
        rows_rejected: Rows discarded as invalid.
        rows_deleted: Rows removed under ``REPLACE_RANGE``. Non-zero here means history
            was restated; the number exists so that fact cannot pass unnoticed.
        rejections: Reject count keyed by reason.
        first_ts_utc: Earliest timestamp in the accepted rows.
        last_ts_utc: Latest timestamp in the accepted rows.
        ambiguous_timestamps: Local times that DST made ambiguous.
        nonexistent_timestamps: Local times that DST made nonexistent.
        started_at_utc: Run start.
        finished_at_utc: Run finish.
    """

    source_id: SourceId
    mode: IngestMode
    files_processed: tuple[Path, ...]
    rows_read: int
    rows_written: int
    rows_skipped_duplicate: int
    rows_rejected: int
    rows_deleted: int
    rejections: Mapping[str, int]
    first_ts_utc: datetime | None
    last_ts_utc: datetime | None
    ambiguous_timestamps: int
    nonexistent_timestamps: int
    started_at_utc: datetime
    finished_at_utc: datetime

    @property
    def reject_ratio(self) -> float:
        """Return the fraction of parsed rows that were rejected."""
        if self.rows_read == 0:
            return 0.0
        return self.rows_rejected / self.rows_read

    @property
    def restated_history(self) -> bool:
        """Return whether this run deleted previously stored bars."""
        return self.rows_deleted > 0


# --------------------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GapRecord:
    """A discontinuity between two consecutive stored bars.

    Attributes:
        start_ts_utc: Timestamp of the bar preceding the gap.
        end_ts_utc: Timestamp of the bar following the gap.
        duration: Wall-clock span between them.
        missing_bars: Bars that would fit in the gap at the nominal interval.
        kind: Attribution of the gap.
    """

    start_ts_utc: datetime
    end_ts_utc: datetime
    duration: timedelta
    missing_bars: int
    kind: GapKind

    @property
    def is_problem(self) -> bool:
        """Return whether this gap represents missing data rather than a closed market."""
        return self.kind in (GapKind.UNEXPECTED, GapKind.UNCLASSIFIED)


@dataclass(frozen=True, slots=True)
class DuplicateRecord:
    """A timestamp appearing more than once.

    Attributes:
        ts_utc: The repeated timestamp.
        occurrences: How many rows carry it.
        values_conflict: ``True`` if the duplicated rows disagree on OHLCV. Identical
            duplicates are a harmless re-delivery; conflicting duplicates mean two
            sources disagree about what the market did, which no dedup rule can
            legitimately resolve on its own.
    """

    ts_utc: datetime
    occurrences: int
    values_conflict: bool


@dataclass(frozen=True, slots=True)
class OhlcViolationRecord:
    """A bar that is internally impossible.

    Attributes:
        ts_utc: Timestamp of the offending bar.
        violations: Every rule the bar breaks.
    """

    ts_utc: datetime
    violations: tuple[OhlcViolation, ...]


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """Quality assessment of a bar series. Purely descriptive: nothing is repaired here.

    Attributes:
        instrument_id: Instrument assessed.
        timeframe: Bar interval assessed.
        row_count: Rows examined.
        first_ts_utc: Earliest timestamp, or ``None`` for an empty frame.
        last_ts_utc: Latest timestamp, or ``None`` for an empty frame.
        gaps: Every discontinuity found, classified where a calendar was available.
        duplicates: Repeated timestamps.
        ohlc_violations: Internally impossible bars.
        zero_volume_bars: Bars reporting exactly zero volume.
        flat_bars: Bars where high equals low.
        out_of_order_rows: Rows appearing before their predecessor in file order.
        synthetic_bars: Bars flagged as gap-fill rather than observed.
        null_volume_bars: Bars with no volume figure at all.
        analyzed_at_utc: When the assessment ran.
    """

    instrument_id: InstrumentId
    timeframe: Timeframe
    row_count: int
    first_ts_utc: datetime | None
    last_ts_utc: datetime | None
    gaps: tuple[GapRecord, ...] = field(default_factory=tuple)
    duplicates: tuple[DuplicateRecord, ...] = field(default_factory=tuple)
    ohlc_violations: tuple[OhlcViolationRecord, ...] = field(default_factory=tuple)
    zero_volume_bars: int = 0
    flat_bars: int = 0
    out_of_order_rows: int = 0
    synthetic_bars: int = 0
    null_volume_bars: int = 0
    analyzed_at_utc: datetime | None = None

    @property
    def unexpected_gaps(self) -> tuple[GapRecord, ...]:
        """Return only the gaps that represent genuinely missing data."""
        return tuple(gap for gap in self.gaps if gap.is_problem)

    @property
    def conflicting_duplicates(self) -> tuple[DuplicateRecord, ...]:
        """Return duplicates whose rows disagree on price or volume."""
        return tuple(dup for dup in self.duplicates if dup.values_conflict)

    @property
    def is_clean(self) -> bool:
        """Return whether the series is fit to backtest without further intervention."""
        return (
            not self.unexpected_gaps
            and not self.duplicates
            and not self.ohlc_violations
            and self.out_of_order_rows == 0
        )

    @property
    def largest_gap(self) -> GapRecord | None:
        """Return the longest discontinuity found, or ``None`` if there were none."""
        if not self.gaps:
            return None
        return max(self.gaps, key=lambda gap: gap.duration)


# --------------------------------------------------------------------------------------
# Requests and fingerprints
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BarRequest:
    """A request for a specific slice of stored history.

    Attributes:
        instrument_id: Instrument to load.
        timeframe: Bar interval to load.
        start_utc: Inclusive lower bound.
        end_utc: Exclusive upper bound. Exclusive so that adjacent requests tile without
            overlapping, which matters for walk-forward folds.
        gap_policy: What to do about unexpected gaps.
        max_tolerable_gap: Discontinuities shorter than this are not reported as gaps.
        calendar_id: Session calendar used to classify gaps, if any.
        include_synthetic: Whether previously gap-filled bars are returned.
    """

    instrument_id: InstrumentId
    timeframe: Timeframe
    start_utc: datetime
    end_utc: datetime
    gap_policy: GapPolicy = GapPolicy.RAISE
    max_tolerable_gap: timedelta = timedelta(0)
    calendar_id: CalendarId | None = None
    include_synthetic: bool = True


@dataclass(frozen=True, slots=True)
class DatasetFingerprint:
    """Deterministic content hash of an exact bar range (Directive 10).

    Two fingerprints are equal if and only if the underlying bars are byte-identical in
    canonical form, regardless of machine, library version, or file layout.

    Attributes:
        content_hash: Full lowercase hex SHA-256 digest.
        algorithm_version: Version of the canonicalisation scheme. Recorded because a
            future change to the scheme must not silently make old fingerprints look
            like mismatches; comparison across differing versions is refused rather
            than guessed.
        row_count: Rows covered.
        first_ts_utc: Earliest timestamp covered.
        last_ts_utc: Latest timestamp covered.
        instrument_id: Instrument covered.
        timeframe: Interval covered.
        column_order: Columns hashed, in hash order.
        source_files: Contributing raw files, sorted, for human traceability only —
            these do not participate in the hash, since the same bars re-delivered
            under a different filename are the same bars.
    """

    content_hash: str
    algorithm_version: int
    row_count: int
    first_ts_utc: datetime | None
    last_ts_utc: datetime | None
    instrument_id: InstrumentId
    timeframe: Timeframe
    column_order: tuple[str, ...]
    source_files: tuple[str, ...] = field(default_factory=tuple)

    @property
    def short_hash(self) -> str:
        """Return the first 12 hex characters, for log lines and run identifiers."""
        return self.content_hash[:12]

    def matches(self, other: DatasetFingerprint) -> bool:
        """Return whether two fingerprints describe identical data.

        Args:
            other: Fingerprint to compare against.

        Returns:
            ``True`` if both hashes and algorithm versions agree.

        Raises:
            ValueError: If the algorithm versions differ, since the comparison would be
                meaningless and a ``False`` result would be misread as a data change.
        """
        if self.algorithm_version != other.algorithm_version:
            raise ValueError(
                "Cannot compare fingerprints produced by different algorithm versions: "
                f"{self.algorithm_version} vs {other.algorithm_version}"
            )
        return self.content_hash == other.content_hash


# --------------------------------------------------------------------------------------
# Structural dependency on Layer 0's calendar consumer
# --------------------------------------------------------------------------------------


@runtime_checkable
class SessionCalendarLike(Protocol):
    """Minimal session calendar surface required by coverage analysis.

    Declared structurally so that ``data/`` depends on a shape rather than on the
    concrete ``domain.timekeeping`` implementation (ARCH-002 D-3). Coverage analysis
    works without a calendar; it simply cannot attribute gaps, and says so by reporting
    them as ``UNCLASSIFIED`` rather than guessing.
    """

    @property
    def calendar_id(self) -> CalendarId:
        """Return the calendar's stable key."""
        ...

    def is_open_at(self, instant_utc: datetime) -> bool:
        """Return whether the market is open at the given UTC instant."""
        ...

    def expected_bar_count(
        self,
        start_utc: datetime,
        end_utc: datetime,
        timeframe: Timeframe,
    ) -> int:
        """Return how many bars should exist in a window, excluding closed periods."""
        ...

    def classify_gap(
        self,
        start_utc: datetime,
        end_utc: datetime,
    ) -> GapKind:
        """Attribute a discontinuity to a weekend, holiday, session break, or fault."""
        ...


def sorted_source_files(paths: Sequence[Path]) -> tuple[str, ...]:
    """Return file paths as a sorted tuple of strings for stable reporting.

    Args:
        paths: Paths to normalise.

    Returns:
        Sorted POSIX-style path strings.
    """
    return tuple(sorted(path.as_posix() for path in paths))
