"""Raw market data ingestion.

This module owns the boundary between the outside world and the system. Two guarantees
are established here and relied upon everywhere downstream:

**One door.** Only ``qtros.data`` reads raw files, and within it only this module. The
Cycle 1 failure — a backtest script globbing ``*.csv`` and eventually parsing its own
log output — is prevented structurally rather than by convention: ingestion accepts a
:class:`SourceHandle`, handles are produced only by :meth:`IngestionService.register_source`,
and registration refuses any path that resolves inside an output directory or that lies
beneath an output-directory marker file.

**UTC at the boundary.** Every timestamp is converted to a timezone-aware UTC instant
before it reaches the warehouse, using an offset the source *declares*. Nothing is
inferred. Under the default :attr:`DstPolicy.STRICT_RAISE`, a local time made ambiguous
or nonexistent by a DST transition aborts the ingestion with the offending timestamps
named, rather than resolving to a plausible instant an hour away from the truth.

The module makes one deliberate, narrow repair: it drops byte-identical duplicate rows.
Everything else it finds, it rejects and reports.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import polars as pl
import structlog

from qtros.data import coverage
from qtros.data.store import WarehouseStore
from qtros.domain.data_contracts import (
    DuplicateRecord,
    IngestionReport,
    SourceHandle,
    SourceSpec,
)
from qtros.domain.enums import (
    DstPolicy,
    FileFormat,
    IngestMode,
    TimestampPolicy,
)
from qtros.domain.errors import (
    IngestionError,
    SourceValidationError,
    TimezoneError,
)
_LOGGER: Final = structlog.get_logger(__name__)

OUTPUT_DIR_MARKER: Final[str] = ".qtros_output"
"""Marker file written into every directory the system writes to.

Registration refuses any source file with this marker in its ancestry. The directory
allow-list is the primary guard; the marker is the backstop that survives a
misconfigured root, which is the form the Cycle 1 bug actually took.

Requires the persistence layer to write this file when creating an output directory
(ARCH-002 amendment A-3).
"""

RAW_TS_COLUMN: Final[str] = "_raw_ts"
TS_COLUMN: Final[str] = "ts_utc"
CANONICAL_PRICE_COLUMNS: Final[tuple[str, ...]] = ("open", "high", "low", "close")
VOLUME_COLUMN: Final[str] = "volume"

_DST_SAMPLE_LIMIT: Final[int] = 10
"""How many offending timestamps to name in a DST failure message."""


@dataclass(frozen=True, slots=True)
class PathGuardPolicy:
    """Filesystem boundaries enforced at source registration.

    Attributes:
        input_root: Directory tree from which raw data may be read. Any source resolving
            outside it is refused.
        forbidden_roots: Directory trees the system writes to — ``results/``, the
            warehouse directory, log directories. Reading from any of them is refused
            regardless of what the input root permits.
        marker_filename: Backstop marker; see :data:`OUTPUT_DIR_MARKER`.
    """

    input_root: Path
    forbidden_roots: tuple[Path, ...]
    marker_filename: str = OUTPUT_DIR_MARKER

    def resolved_input_root(self) -> Path:
        """Return the input root with symlinks and relative segments resolved."""
        return self.input_root.resolve()

    def resolved_forbidden_roots(self) -> tuple[Path, ...]:
        """Return the forbidden roots with symlinks and relative segments resolved."""
        return tuple(root.resolve() for root in self.forbidden_roots)


class IngestionService:
    """Validates, parses, and loads raw market data into the warehouse.

    Args:
        store: Warehouse to write into. Injected rather than constructed so that the
            service can be unit-tested against an in-memory warehouse.
        guard: Filesystem boundary policy.
        clock: Returns the current UTC instant. Injected so that ingestion reports are
            deterministic under test.

    Raises:
        SourceValidationError: If the guard policy is itself incoherent — specifically,
            if the input root lies inside a forbidden root or vice versa. Catching this
            at construction is better than discovering it on the first ingest.
    """

    def __init__(
        self,
        store: WarehouseStore,
        guard: PathGuardPolicy,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store: WarehouseStore = store
        self._guard: PathGuardPolicy = guard
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._validate_guard_policy()

    # -- registration ------------------------------------------------------------------

    def register_source(self, spec: SourceSpec) -> SourceHandle:
        """Validate a source without ingesting it.

        Checks, in order: the timezone declaration is complete and internally
        consistent; the root path resolves inside the input root; the root path does not
        resolve inside any output directory; the glob matches at least one file; no
        matched file lies beneath an output marker; and every matched file's header
        carries the columns the mapping requires.

        Args:
            spec: Source description to validate.

        Returns:
            A handle authorising ingestion of the matched files.

        Raises:
            SourceValidationError: On any failed check. The error names the specific
                failure rather than reporting a generic validation failure, because the
                whole point of validating early is to make the fix obvious.
        """
        self._validate_timezone_declaration(spec)

        root: Path = spec.root_path.resolve()
        self._assert_within_input_root(root, label="source root")
        self._assert_not_output_location(root, label="source root")

        files: tuple[Path, ...] = self._resolve_files(spec, root)
        if not files:
            raise SourceValidationError(
                "Source glob matched no files",
                source_id=str(spec.source_id),
                root_path=str(root),
                glob_pattern=spec.glob_pattern,
            )

        for path in files:
            self._assert_not_output_location(path, label="source file")
            self._assert_no_output_marker(path)

        if spec.file_format is FileFormat.CSV:
            for path in files:
                self._validate_csv_header(path, spec)

        total_bytes: int = sum(path.stat().st_size for path in files)
        handle = SourceHandle(
            spec=spec,
            files=files,
            total_bytes=total_bytes,
            validated_at_utc=self._clock(),
        )

        _LOGGER.info(
            "ingestion.source_registered",
            source_id=str(spec.source_id),
            instrument_id=str(spec.instrument_id),
            timeframe=str(spec.timeframe),
            file_count=len(files),
            total_bytes=total_bytes,
            timestamp_policy=str(spec.timestamp_policy),
        )
        return handle

    # -- ingestion ---------------------------------------------------------------------

    def ingest(
        self,
        handle: SourceHandle,
        *,
        mode: IngestMode = IngestMode.APPEND_ONLY,
    ) -> IngestionReport:
        """Parse a registered source and load it into the warehouse.

        Args:
            handle: Handle returned by :meth:`register_source`.
            mode: Write semantics. ``DRY_RUN`` performs every parse and validation step
                and writes nothing, which is the correct way to inspect a new vendor
                file before trusting it.

        Returns:
            A report describing what was read, written, skipped, and rejected.

        Raises:
            IngestionError: If a file cannot be parsed, if duplicate timestamps carry
                conflicting values, or if the rejection ratio exceeds the source's
                declared tolerance.
            TimezoneError: If a local timestamp is ambiguous or nonexistent under
                :attr:`DstPolicy.STRICT_RAISE`.
        """
        spec: SourceSpec = handle.spec
        started_at: datetime = self._clock()
        ingestion_id: str = uuid.uuid4().hex

        frames: list[pl.DataFrame] = []
        rows_read: int = 0
        ambiguous_total: int = 0
        nonexistent_total: int = 0

        for path in handle.files:
            raw: pl.DataFrame = self._read_file(path, spec)
            rows_read += raw.height
            converted, ambiguous, nonexistent = self._to_utc(raw, spec, path)
            ambiguous_total += ambiguous
            nonexistent_total += nonexistent
            frames.append(converted)

        combined: pl.DataFrame = (
            pl.concat(frames, how="vertical") if frames else _empty_canonical_frame()
        )

        accepted, rejections = self._reject_invalid_rows(combined, spec)
        rows_rejected: int = combined.height - accepted.height

        if rows_read > 0 and (rows_rejected / rows_read) > spec.max_reject_ratio:
            raise IngestionError(
                "Rejection ratio exceeded the source's declared tolerance; this is a "
                "broken download or a wrong column mapping, not a dirty file",
                source_id=str(spec.source_id),
                rows_read=rows_read,
                rows_rejected=rows_rejected,
                reject_ratio=round(rows_rejected / rows_read, 6),
                max_reject_ratio=spec.max_reject_ratio,
                rejections=dict(rejections),
            )

        accepted, duplicates_dropped = self._deduplicate(accepted, spec)
        accepted = self._attach_metadata(accepted, spec, ingested_at=started_at)
        accepted = accepted.sort(TS_COLUMN)

        outcome = self._store.insert_bars(
            accepted,
            instrument_id=spec.instrument_id,
            timeframe=spec.timeframe,
            mode=mode,
        )

        first_ts: datetime | None = accepted.get_column(TS_COLUMN).min() if accepted.height else None  # type: ignore[assignment]
        last_ts: datetime | None = accepted.get_column(TS_COLUMN).max() if accepted.height else None  # type: ignore[assignment]
        finished_at: datetime = self._clock()

        rejection_tally: dict[str, int] = dict(rejections)
        if duplicates_dropped:
            rejection_tally["IDENTICAL_DUPLICATE"] = duplicates_dropped

        report = IngestionReport(
            source_id=spec.source_id,
            mode=mode,
            files_processed=handle.files,
            rows_read=rows_read,
            rows_written=outcome.rows_written,
            rows_skipped_duplicate=outcome.rows_skipped_duplicate + duplicates_dropped,
            rows_rejected=rows_rejected,
            rows_deleted=outcome.rows_deleted,
            rejections=rejection_tally,
            first_ts_utc=first_ts,
            last_ts_utc=last_ts,
            ambiguous_timestamps=ambiguous_total,
            nonexistent_timestamps=nonexistent_total,
            started_at_utc=started_at,
            finished_at_utc=finished_at,
        )

        if mode is not IngestMode.DRY_RUN:
            self._store.record_ingestion(
                ingestion_id=ingestion_id,
                source_id=spec.source_id,
                instrument_id=spec.instrument_id,
                timeframe=spec.timeframe,
                mode=mode,
                files_processed=tuple(path.as_posix() for path in handle.files),
                rows_read=report.rows_read,
                rows_written=report.rows_written,
                rows_skipped=report.rows_skipped_duplicate,
                rows_rejected=report.rows_rejected,
                rows_deleted=report.rows_deleted,
                first_ts_utc=report.first_ts_utc,
                last_ts_utc=report.last_ts_utc,
                started_at_utc=report.started_at_utc,
                finished_at_utc=report.finished_at_utc,
            )

        _LOGGER.info(
            "ingestion.completed",
            ingestion_id=ingestion_id,
            source_id=str(spec.source_id),
            mode=str(mode),
            rows_read=report.rows_read,
            rows_written=report.rows_written,
            rows_rejected=report.rows_rejected,
            rows_skipped_duplicate=report.rows_skipped_duplicate,
            reject_ratio=round(report.reject_ratio, 6),
            restated_history=report.restated_history,
        )
        return report

    # -- guard -------------------------------------------------------------------------

    def _validate_guard_policy(self) -> None:
        """Reject a guard policy whose input and output trees overlap.

        Raises:
            SourceValidationError: If the input root lies inside a forbidden root, or a
                forbidden root lies inside the input root.
        """
        input_root: Path = self._guard.resolved_input_root()
        for forbidden in self._guard.resolved_forbidden_roots():
            if input_root == forbidden or forbidden in input_root.parents:
                raise SourceValidationError(
                    "Input root lies inside an output directory; this is the Cycle 1 "
                    "self-ingestion configuration and is refused",
                    input_root=str(input_root),
                    forbidden_root=str(forbidden),
                )
            if input_root in forbidden.parents:
                raise SourceValidationError(
                    "An output directory lies inside the input root; outputs would "
                    "become ingestible on the next glob",
                    input_root=str(input_root),
                    forbidden_root=str(forbidden),
                )

    def _assert_within_input_root(self, path: Path, *, label: str) -> None:
        """Assert a path lies inside the declared input root.

        Args:
            path: Resolved path to check.
            label: Human label used in the error context.

        Raises:
            SourceValidationError: If the path is outside the input root.
        """
        root: Path = self._guard.resolved_input_root()
        if path != root and root not in path.parents:
            raise SourceValidationError(
                f"{label} lies outside the declared input root",
                path=str(path),
                input_root=str(root),
            )

    def _assert_not_output_location(self, path: Path, *, label: str) -> None:
        """Assert a path does not resolve inside any output directory.

        Args:
            path: Resolved path to check.
            label: Human label used in the error context.

        Raises:
            SourceValidationError: If the path is at or beneath a forbidden root.
        """
        for forbidden in self._guard.resolved_forbidden_roots():
            if path == forbidden or forbidden in path.parents:
                raise SourceValidationError(
                    f"{label} resolves inside an output directory (self-ingestion guard)",
                    path=str(path),
                    forbidden_root=str(forbidden),
                )

    def _assert_no_output_marker(self, path: Path) -> None:
        """Assert no ancestor directory is marked as an output directory.

        Args:
            path: Resolved file path to check.

        Raises:
            SourceValidationError: If a marker file is found in the path's ancestry.
        """
        root: Path = self._guard.resolved_input_root()
        for ancestor in path.parents:
            if (ancestor / self._guard.marker_filename).exists():
                raise SourceValidationError(
                    "Source file lies beneath an output-directory marker",
                    path=str(path),
                    marker_directory=str(ancestor),
                    marker_filename=self._guard.marker_filename,
                )
            if ancestor == root:
                break

    @staticmethod
    def _validate_timezone_declaration(spec: SourceSpec) -> None:
        """Assert the source's timezone declaration is complete and consistent.

        Args:
            spec: Source description to check.

        Raises:
            SourceValidationError: If the declaration is missing or contradictory.
        """
        policy: TimestampPolicy = spec.timestamp_policy

        if policy is TimestampPolicy.DECLARED_ZONE and not spec.declared_zone:
            raise SourceValidationError(
                "DECLARED_ZONE sources must name an IANA timezone",
                source_id=str(spec.source_id),
            )
        if policy is TimestampPolicy.DECLARED_FIXED_OFFSET and spec.declared_offset is None:
            raise SourceValidationError(
                "DECLARED_FIXED_OFFSET sources must declare an offset",
                source_id=str(spec.source_id),
            )
        if policy is TimestampPolicy.EMBEDDED_OFFSET and (
            spec.declared_zone or spec.declared_offset is not None
        ):
            raise SourceValidationError(
                "EMBEDDED_OFFSET sources must not also declare a zone or offset; two "
                "sources of truth for the same fact is how an hour goes missing",
                source_id=str(spec.source_id),
                declared_zone=spec.declared_zone,
            )

    def _resolve_files(self, spec: SourceSpec, root: Path) -> tuple[Path, ...]:
        """Resolve the source's glob to a sorted tuple of regular files.

        Args:
            spec: Source description.
            root: Resolved root directory.

        Returns:
            Sorted absolute paths of matched regular files.

        Raises:
            SourceValidationError: If the root does not exist or is not a directory.
        """
        if not root.is_dir():
            raise SourceValidationError(
                "Source root does not exist or is not a directory",
                source_id=str(spec.source_id),
                root_path=str(root),
            )
        matches = root.rglob(spec.glob_pattern) if spec.recursive_glob else root.glob(
            spec.glob_pattern
        )
        return tuple(sorted(path.resolve() for path in matches if path.is_file()))

    @staticmethod
    def _validate_csv_header(path: Path, spec: SourceSpec) -> None:
        """Assert a CSV file's header carries every column the mapping requires.

        Args:
            path: File to inspect.
            spec: Source description supplying the column mapping.

        Raises:
            SourceValidationError: If the file is unreadable or a column is missing.
        """
        try:
            header: list[str] = pl.read_csv(
                path,
                n_rows=0,
                separator=spec.csv_separator,
                has_header=spec.has_header,
            ).columns
        except Exception as exc:  # noqa: BLE001 - polars raises several unrelated types
            raise SourceValidationError(
                "Could not read CSV header",
                path=str(path),
                cause=str(exc),
            ) from exc

        missing: tuple[str, ...] = tuple(
            name for name in spec.columns.all_columns() if name not in header
        )
        if missing:
            raise SourceValidationError(
                "CSV header is missing mapped columns",
                path=str(path),
                missing=list(missing),
                header=header,
            )

    # -- parsing -----------------------------------------------------------------------

    def _read_file(self, path: Path, spec: SourceSpec) -> pl.DataFrame:
        """Read one raw file and rename its columns to the canonical schema.

        Prices are read as ``Float64`` explicitly rather than inferred. Inference across
        a multi-file source can assign different dtypes to different files, and the
        resulting concat either fails loudly or coerces quietly; neither is acceptable
        at the boundary.

        Args:
            path: File to read.
            spec: Source description.

        Returns:
            A frame with the canonical column names and a raw timestamp column.

        Raises:
            IngestionError: If the file cannot be parsed.
        """
        mapping = spec.columns
        wanted: list[str] = list(mapping.all_columns())
        rename: dict[str, str] = {
            mapping.timestamp: RAW_TS_COLUMN,
            mapping.open: "open",
            mapping.high: "high",
            mapping.low: "low",
            mapping.close: "close",
        }
        if mapping.volume is not None:
            rename[mapping.volume] = VOLUME_COLUMN

        try:
            if spec.file_format is FileFormat.CSV:
                overrides: dict[str, pl.DataType] = {
                    mapping.timestamp: pl.String(),
                    mapping.open: pl.Float64(),
                    mapping.high: pl.Float64(),
                    mapping.low: pl.Float64(),
                    mapping.close: pl.Float64(),
                }
                if mapping.volume is not None:
                    overrides[mapping.volume] = pl.Float64()
                frame: pl.DataFrame = pl.read_csv(
                    path,
                    columns=wanted,
                    separator=spec.csv_separator,
                    has_header=spec.has_header,
                    schema_overrides=overrides,
                    try_parse_dates=False,
                )
            else:
                frame = pl.read_parquet(path, columns=wanted)
        except Exception as exc:  # noqa: BLE001 - polars raises several unrelated types
            raise IngestionError(
                "Failed to parse source file",
                path=str(path),
                source_id=str(spec.source_id),
                file_format=str(spec.file_format),
                cause=str(exc),
            ) from exc

        renamed: pl.DataFrame = frame.rename(rename)
        if mapping.volume is None:
            renamed = renamed.with_columns(pl.lit(None, dtype=pl.Float64).alias(VOLUME_COLUMN))
        return renamed

    def _to_utc(
        self,
        frame: pl.DataFrame,
        spec: SourceSpec,
        path: Path,
    ) -> tuple[pl.DataFrame, int, int]:
        """Convert the raw timestamp column to a timezone-aware UTC instant.

        This is the mandatory UTC conversion at the boundary. Nothing downstream ever
        performs a timezone conversion on bar data, so a fault here is a fault
        everywhere; correspondingly, this method refuses rather than guesses.

        Args:
            frame: Frame carrying the raw timestamp column.
            spec: Source description supplying the timestamp policy.
            path: File the frame came from, used for error context.

        Returns:
            A triple of (converted frame, ambiguous count, nonexistent count). Both
            counts are zero unless the DST policy tolerates such timestamps.

        Raises:
            TimezoneError: If a local time is ambiguous or nonexistent under
                ``STRICT_RAISE``, or if the declared zone is unknown.
            IngestionError: If the timestamp column cannot be parsed with the declared
                format.
        """
        parsed: pl.Expr = self._parse_expression(frame, spec)
        policy: TimestampPolicy = spec.timestamp_policy
        ambiguous_count: int = 0
        nonexistent_count: int = 0

        if policy is TimestampPolicy.EMBEDDED_OFFSET:
            utc_expr: pl.Expr = parsed.dt.convert_time_zone("UTC")

        elif policy is TimestampPolicy.DECLARED_FIXED_OFFSET:
            offset: timedelta = spec.declared_offset or timedelta(0)
            # A constant offset has no DST, therefore no ambiguity: subtract and stamp.
            utc_expr = (parsed - pl.duration(microseconds=int(offset.total_seconds() * 1e6))
                        ).dt.replace_time_zone("UTC")

        else:
            zone: str = spec.declared_zone or "UTC"
            ambiguous, non_existent = _dst_resolution_arguments(spec.dst_policy)
            utc_expr = (
                parsed.dt.replace_time_zone(
                    zone, ambiguous=ambiguous, non_existent=non_existent
                ).dt.convert_time_zone("UTC")
            )

        try:
            converted: pl.DataFrame = frame.with_columns(utc_expr.alias(TS_COLUMN))
        except Exception as exc:  # noqa: BLE001 - polars raises ComputeError subtypes
            if policy is TimestampPolicy.DECLARED_ZONE:
                raise self._diagnose_dst_failure(frame, spec, path, exc) from exc
            raise IngestionError(
                "Failed to convert timestamps to UTC",
                path=str(path),
                source_id=str(spec.source_id),
                timestamp_policy=str(policy),
                timestamp_format=spec.timestamp_format,
                cause=str(exc),
            ) from exc

        if spec.dst_policy is DstPolicy.DROP_AMBIGUOUS:
            before: int = converted.height
            converted = converted.filter(pl.col(TS_COLUMN).is_not_null())
            ambiguous_count = before - converted.height

        converted = converted.drop(RAW_TS_COLUMN)

        if not spec.timestamps_are_bar_open:
            # Canonical internal representation is the bar's opening instant. A
            # close-labelled source shifted back one interval; leaving it unshifted
            # moves the whole series forward by one bar, which is look-ahead bias that
            # the feature-level prefix-invariance audit is structurally unable to see.
            interval: timedelta = spec.timeframe.duration
            converted = converted.with_columns(
                (
                    pl.col(TS_COLUMN)
                    - pl.duration(microseconds=int(interval.total_seconds() * 1e6))
                ).alias(TS_COLUMN)
            )

        return converted, ambiguous_count, nonexistent_count

    @staticmethod
    def _parse_expression(frame: pl.DataFrame, spec: SourceSpec) -> pl.Expr:
        """Build the expression that turns the raw timestamp column into a datetime.

        Args:
            frame: Frame carrying the raw timestamp column.
            spec: Source description supplying the parse format.

        Returns:
            An expression producing a Datetime column at microsecond resolution.
        """
        column: pl.Expr = pl.col(RAW_TS_COLUMN)
        dtype: pl.DataType = frame.schema[RAW_TS_COLUMN]

        if dtype.base_type() is pl.Datetime:
            # Parquet sources may already carry a datetime dtype; no text parse needed.
            return column.dt.cast_time_unit("us")
        return column.str.to_datetime(format=spec.timestamp_format, time_unit="us")

    @staticmethod
    def _diagnose_dst_failure(
        frame: pl.DataFrame,
        spec: SourceSpec,
        path: Path,
        cause: Exception,
    ) -> TimezoneError:
        """Re-run the conversion permissively to name the offending timestamps.

        A bare "ambiguous local time" message costs an evening to track down in a
        multi-year file. Re-running with null resolution isolates exactly which rows are
        at fault, and those timestamps go straight into the error context.

        Args:
            frame: Frame that failed conversion.
            spec: Source description.
            path: File the frame came from.
            cause: Original exception.

        Returns:
            A :class:`TimezoneError` carrying sample offending timestamps.
        """
        zone: str = spec.declared_zone or "UTC"
        samples: list[str] = []
        offending: int = 0
        try:
            probe: pl.DataFrame = frame.select(
                pl.col(RAW_TS_COLUMN),
                pl.col(RAW_TS_COLUMN)
                .str.to_datetime(format=spec.timestamp_format, time_unit="us")
                .dt.replace_time_zone(zone, ambiguous="null", non_existent="null")
                .alias("_resolved"),
            ).filter(pl.col("_resolved").is_null())
            offending = probe.height
            samples = [
                str(value) for value in probe.head(_DST_SAMPLE_LIMIT).get_column(RAW_TS_COLUMN)
            ]
        except Exception:  # noqa: BLE001 - diagnostics must never mask the real failure
            _LOGGER.warning("ingestion.dst_diagnosis_failed", path=str(path))

        return TimezoneError(
            "Local timestamps are ambiguous or nonexistent under the declared timezone; "
            "resolve the source's DST policy explicitly rather than letting the parser "
            "choose an hour",
            path=str(path),
            source_id=str(spec.source_id),
            declared_zone=zone,
            dst_policy=str(spec.dst_policy),
            offending_rows=offending,
            sample_timestamps=samples,
            cause=str(cause),
        )

    # -- validation and shaping --------------------------------------------------------

    def _reject_invalid_rows(
        self,
        frame: pl.DataFrame,
        spec: SourceSpec,
    ) -> tuple[pl.DataFrame, Mapping[str, int]]:
        """Drop rows that are internally impossible or have no usable timestamp.

        Args:
            frame: Converted frame.
            spec: Source description, used for logging context.

        Returns:
            A pair of (accepted frame, rejection counts keyed by reason).
        """
        if frame.height == 0:
            return frame, {}

        tally: dict[str, int] = {
            str(kind): count for kind, count in coverage.violation_counts(frame).items()
        }

        null_timestamps: int = int(frame.get_column(TS_COLUMN).null_count())
        if null_timestamps:
            tally["NULL_TIMESTAMP"] = null_timestamps

        bad_ohlc: pl.Series = coverage.ohlc_violation_mask(frame)
        accepted: pl.DataFrame = frame.filter(
            (~bad_ohlc) & pl.col(TS_COLUMN).is_not_null()
        )

        if tally:
            _LOGGER.warning(
                "ingestion.rows_rejected",
                source_id=str(spec.source_id),
                rejected=frame.height - accepted.height,
                reasons=tally,
            )
        return accepted, tally

    def _deduplicate(
        self,
        frame: pl.DataFrame,
        spec: SourceSpec,
    ) -> tuple[pl.DataFrame, int]:
        """Drop byte-identical duplicate timestamps; refuse conflicting ones.

        Identical re-delivery is a vendor artefact and is safely collapsed. Two rows
        claiming the same instant with different prices means two sources disagree about
        what the market did, and there is no defensible automatic winner — so the
        ingestion stops and names the timestamps.

        Args:
            frame: Accepted frame.
            spec: Source description.

        Returns:
            A pair of (deduplicated frame, number of identical duplicates dropped).

        Raises:
            IngestionError: If any duplicated timestamp carries conflicting values.
        """
        if frame.height == 0:
            return frame, 0

        duplicates: tuple[DuplicateRecord, ...] = coverage.find_duplicates(frame)
        if not duplicates:
            return frame, 0

        conflicting: tuple[DuplicateRecord, ...] = tuple(
            record for record in duplicates if record.values_conflict
        )
        if conflicting:
            raise IngestionError(
                "Duplicate timestamps carry conflicting values; two sources disagree "
                "about what the market did and this cannot be resolved automatically",
                source_id=str(spec.source_id),
                conflicting_count=len(conflicting),
                sample_timestamps=[
                    record.ts_utc.isoformat() for record in conflicting[:_DST_SAMPLE_LIMIT]
                ],
            )

        before: int = frame.height
        deduplicated: pl.DataFrame = frame.unique(
            subset=[TS_COLUMN], keep="first", maintain_order=True
        )
        dropped: int = before - deduplicated.height

        _LOGGER.info(
            "ingestion.identical_duplicates_dropped",
            source_id=str(spec.source_id),
            dropped=dropped,
            distinct_timestamps=len(duplicates),
        )
        return deduplicated, dropped

    @staticmethod
    def _attach_metadata(
        frame: pl.DataFrame,
        spec: SourceSpec,
        *,
        ingested_at: datetime,
    ) -> pl.DataFrame:
        """Add the warehouse's provenance columns to a frame.

        Args:
            frame: Frame to annotate.
            spec: Source description.
            ingested_at: UTC instant recorded on every row.

        Returns:
            The frame with ``instrument_id``, ``timeframe``, ``is_synthetic``,
            ``source_id``, and ``ingested_at_utc`` populated.
        """
        return frame.with_columns(
            pl.lit(str(spec.instrument_id)).alias("instrument_id"),
            pl.lit(str(spec.timeframe)).alias("timeframe"),
            pl.lit(value=False).alias("is_synthetic"),
            pl.lit(str(spec.source_id)).alias("source_id"),
            pl.lit(ingested_at).alias("ingested_at_utc"),
        )


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _dst_resolution_arguments(policy: DstPolicy) -> tuple[str, str]:
    """Translate a DST policy into Polars' ``ambiguous`` and ``non_existent`` arguments.

    Args:
        policy: Declared resolution policy.

    Returns:
        A pair of Polars argument values.
    """
    match policy:
        case DstPolicy.STRICT_RAISE:
            return "raise", "raise"
        case DstPolicy.EARLIEST:
            return "earliest", "raise"
        case DstPolicy.LATEST:
            return "latest", "raise"
        case DstPolicy.DROP_AMBIGUOUS:
            return "null", "null"


def _empty_canonical_frame() -> pl.DataFrame:
    """Return an empty frame with the canonical post-conversion schema.

    Returns:
        An empty frame carrying the timestamp, price, and volume columns, so that a
        source matching zero rows flows through the same code path as any other.
    """
    return pl.DataFrame(
        schema={
            TS_COLUMN: pl.Datetime(time_unit="us", time_zone="UTC"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            VOLUME_COLUMN: pl.Float64,
        }
    )


def default_path_guard(
    *,
    input_root: Path,
    results_root: Path,
    warehouse_path: Path,
    log_root: Path | None = None,
) -> PathGuardPolicy:
    """Build the standard guard policy from the project's directory layout.

    Args:
        input_root: Raw data tree, typically ``data_lake/raw``.
        results_root: Immutable run artifact tree, typically ``results``.
        warehouse_path: DuckDB file. Its containing directory becomes forbidden, since
            the warehouse and its write-ahead files must never be ingested.
        log_root: Optional log directory to forbid as well.

    Returns:
        A configured guard policy.
    """
    forbidden: list[Path] = [results_root, warehouse_path.parent]
    if log_root is not None:
        forbidden.append(log_root)
    return PathGuardPolicy(input_root=input_root, forbidden_roots=tuple(forbidden))


def summarize_report(report: IngestionReport) -> Mapping[str, object]:
    """Flatten an ingestion report into a log-friendly mapping.

    Args:
        report: Report to summarise.

    Returns:
        A mapping suitable for structured logging or a CLI table.
    """
    return {
        "source_id": str(report.source_id),
        "mode": str(report.mode),
        "files": len(report.files_processed),
        "rows_read": report.rows_read,
        "rows_written": report.rows_written,
        "rows_skipped_duplicate": report.rows_skipped_duplicate,
        "rows_rejected": report.rows_rejected,
        "rows_deleted": report.rows_deleted,
        "reject_ratio": round(report.reject_ratio, 6),
        "restated_history": report.restated_history,
        "first_ts_utc": report.first_ts_utc.isoformat() if report.first_ts_utc else None,
        "last_ts_utc": report.last_ts_utc.isoformat() if report.last_ts_utc else None,
        "ambiguous_timestamps": report.ambiguous_timestamps,
        "nonexistent_timestamps": report.nonexistent_timestamps,
        "duration_seconds": (report.finished_at_utc - report.started_at_utc).total_seconds(),
    }
