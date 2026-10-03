"""DuckDB warehouse access.

This is the only module in the system permitted to import ``duckdb``. The restriction is
enforced by an import-linter contract in ``pyproject.toml``, not by convention, and it
exists so that the storage engine remains a replaceable detail rather than a fact that
has leaked into eight packages.

Storage conventions:
    * Timestamps are stored as ``TIMESTAMPTZ`` and every connection pins its session
      timezone to UTC. DuckDB renders ``TIMESTAMPTZ`` according to the session zone, so
      an unpinned connection on a machine set to ``Australia/Sydney`` returns correct
      instants with misleading wall-clock text. Pinning removes the ambiguity at the
      source rather than at every call site.
    * Prices are ``DOUBLE``. Decimal arithmetic is reserved for the cash ledger, where
      exact reconciliation is required; bar prices feed the vectorised feature layer and
      float is the correct trade-off there.
    * ``(instrument_id, timeframe, ts_utc)`` is the primary key. Deduplication is a
      property of the schema, not of the ingestion code, so no future code path can
      write a second bar for the same instant.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Self

import duckdb
import structlog

from qtros.domain.enums import IngestMode, Timeframe
from qtros.domain.errors import WarehouseError
from qtros.domain.identifiers import InstrumentId, SourceId

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import TracebackType

    import polars as pl

_LOGGER: Final = structlog.get_logger(__name__)

WAREHOUSE_SCHEMA_VERSION: Final[int] = 1
"""Bumped whenever the physical schema changes. Drives migration on open."""

BARS_TABLE: Final[str] = "bars"
INGESTION_LOG_TABLE: Final[str] = "ingestion_log"
SCHEMA_META_TABLE: Final[str] = "schema_meta"

BAR_COLUMNS: Final[tuple[str, ...]] = (
    "instrument_id",
    "timeframe",
    "ts_utc",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "is_synthetic",
    "source_id",
    "ingested_at_utc",
)
"""Canonical column order. Insert statements name columns explicitly rather than
relying on this order, but fingerprinting and frame construction depend on it."""

_DDL_SCHEMA_META: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA_META_TABLE} (
    key            VARCHAR PRIMARY KEY,
    value          VARCHAR NOT NULL,
    updated_at_utc TIMESTAMPTZ NOT NULL
);
"""

_DDL_BARS: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {BARS_TABLE} (
    instrument_id   VARCHAR     NOT NULL,
    timeframe       VARCHAR     NOT NULL,
    ts_utc          TIMESTAMPTZ NOT NULL,
    open            DOUBLE      NOT NULL,
    high            DOUBLE      NOT NULL,
    low             DOUBLE      NOT NULL,
    close           DOUBLE      NOT NULL,
    volume          DOUBLE,
    is_synthetic    BOOLEAN     NOT NULL DEFAULT FALSE,
    source_id       VARCHAR     NOT NULL,
    ingested_at_utc TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (instrument_id, timeframe, ts_utc)
);
"""

_DDL_INGESTION_LOG: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {INGESTION_LOG_TABLE} (
    ingestion_id      VARCHAR PRIMARY KEY,
    source_id         VARCHAR     NOT NULL,
    instrument_id     VARCHAR     NOT NULL,
    timeframe         VARCHAR     NOT NULL,
    mode              VARCHAR     NOT NULL,
    files_processed   VARCHAR     NOT NULL,
    rows_read         BIGINT      NOT NULL,
    rows_written      BIGINT      NOT NULL,
    rows_skipped      BIGINT      NOT NULL,
    rows_rejected     BIGINT      NOT NULL,
    rows_deleted      BIGINT      NOT NULL,
    first_ts_utc      TIMESTAMPTZ,
    last_ts_utc       TIMESTAMPTZ,
    started_at_utc    TIMESTAMPTZ NOT NULL,
    finished_at_utc   TIMESTAMPTZ NOT NULL
);
"""


def _as_int(value: object) -> int:
    """Coerce a DuckDB scalar to ``int`` without trusting its static type."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return int(str(value))


def _as_datetime(value: object) -> datetime:
    """Require a scalar to be a datetime.

    Raises:
        WarehouseError: If it is not, which means the frame's timestamp column is not
            what the schema promises.
    """
    if isinstance(value, datetime):
        return value
    raise WarehouseError("Timestamp bound is not a datetime", value=repr(value))


@dataclass(frozen=True, slots=True)
class InsertOutcome:
    """Result of writing a batch of bars.

    Attributes:
        rows_offered: Rows presented to the warehouse.
        rows_written: Rows newly persisted.
        rows_skipped_duplicate: Rows rejected by the primary key as already present.
        rows_deleted: Rows removed beforehand under ``REPLACE_RANGE``.
    """

    rows_offered: int
    rows_written: int
    rows_skipped_duplicate: int
    rows_deleted: int


@dataclass(frozen=True, slots=True)
class StoredRange:
    """The extent of stored history for one instrument and timeframe.

    Attributes:
        instrument_id: Instrument described.
        timeframe: Interval described.
        row_count: Bars stored.
        first_ts_utc: Earliest stored bar, or ``None`` if none are stored.
        last_ts_utc: Latest stored bar, or ``None`` if none are stored.
    """

    instrument_id: InstrumentId
    timeframe: Timeframe
    row_count: int
    first_ts_utc: datetime | None
    last_ts_utc: datetime | None


class WarehouseStore:
    """Owns the DuckDB connection and all SQL in the system.

    The store is deliberately thin: it persists and retrieves bars and records what was
    ingested. It performs no validation, no gap filling, and no timezone conversion —
    data arriving here is already correct, because correctness is established at the
    ingestion boundary where the raw file is still in view.

    Args:
        warehouse_path: Path to the DuckDB file. Its parent directory is created if
            absent. Use ``":memory:"`` for ephemeral use in tests.
        read_only: Open without write capability. Analytics and reporting should use
            this, so that a reporting bug cannot mutate the warehouse.

    Raises:
        WarehouseError: If the database cannot be opened or migrated.

    Example:
        >>> with WarehouseStore(Path("data_lake/warehouse/qtros.duckdb")) as store:
        ...     store.initialize_schema()
    """

    def __init__(self, warehouse_path: Path | str, *, read_only: bool = False) -> None:
        self._warehouse_path: Path | str = (
            warehouse_path if warehouse_path == ":memory:" else Path(warehouse_path)
        )
        self._read_only: bool = read_only
        self._conn: duckdb.DuckDBPyConnection | None = None

    # -- lifecycle ---------------------------------------------------------------------

    def connect(self) -> Self:
        """Open the connection and pin the session timezone to UTC.

        Returns:
            This store, connected.

        Raises:
            WarehouseError: If the file cannot be opened.
        """
        if self._conn is not None:
            return self

        if isinstance(self._warehouse_path, Path) and not self._read_only:
            self._warehouse_path.parent.mkdir(parents=True, exist_ok=True)

        target: str = (
            self._warehouse_path
            if isinstance(self._warehouse_path, str)
            else str(self._warehouse_path)
        )
        try:
            self._conn = duckdb.connect(target, read_only=self._read_only)
            # Non-negotiable: TIMESTAMPTZ rendering follows the session zone.
            self._conn.execute("SET TimeZone='UTC';")
        except duckdb.Error as exc:  # pragma: no cover - environment dependent
            raise WarehouseError(
                "Failed to open the DuckDB warehouse",
                path=target,
                read_only=self._read_only,
            ) from exc

        _LOGGER.info("warehouse.connected", path=target, read_only=self._read_only)
        return self

    def close(self) -> None:
        """Close the connection if it is open. Safe to call repeatedly."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            _LOGGER.info("warehouse.closed")

    def __enter__(self) -> Self:
        """Open the connection on entering a ``with`` block."""
        return self.connect()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the connection on leaving a ``with`` block."""
        self.close()

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        """Return the live connection.

        Returns:
            The open DuckDB connection.

        Raises:
            WarehouseError: If the store has not been connected.
        """
        if self._conn is None:
            raise WarehouseError("Warehouse is not connected; call connect() first")
        return self._conn

    @contextmanager
    def _transaction(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """Run a block inside an explicit transaction, rolling back on any exception.

        Yields:
            The open connection, inside a transaction.

        Raises:
            WarehouseError: If the write fails. The original exception is chained.
        """
        conn: duckdb.DuckDBPyConnection = self.connection
        conn.execute("BEGIN TRANSACTION;")
        try:
            yield conn
        except Exception as exc:
            conn.execute("ROLLBACK;")
            if isinstance(exc, WarehouseError):
                raise
            raise WarehouseError("Warehouse transaction rolled back", cause=str(exc)) from exc
        else:
            conn.execute("COMMIT;")

    # -- schema ------------------------------------------------------------------------

    def initialize_schema(self) -> None:
        """Create tables if absent and record the schema version.

        Idempotent. Safe to call on every startup.

        Raises:
            WarehouseError: If the stored schema version is newer than this build
                understands. Refusing to run against a future schema is safer than
                writing rows a later version will misread.
        """
        with self._transaction() as conn:
            conn.execute(_DDL_SCHEMA_META)
            conn.execute(_DDL_BARS)
            conn.execute(_DDL_INGESTION_LOG)

            existing: int | None = self._read_schema_version(conn)
            if existing is None:
                conn.execute(
                    f"INSERT INTO {SCHEMA_META_TABLE} (key, value, updated_at_utc) "
                    "VALUES ('schema_version', ?, now());",
                    [str(WAREHOUSE_SCHEMA_VERSION)],
                )
            elif existing > WAREHOUSE_SCHEMA_VERSION:
                raise WarehouseError(
                    "Warehouse was written by a newer build of qtros",
                    stored_version=existing,
                    supported_version=WAREHOUSE_SCHEMA_VERSION,
                )

        _LOGGER.info("warehouse.schema_ready", schema_version=WAREHOUSE_SCHEMA_VERSION)

    @staticmethod
    def _read_schema_version(conn: duckdb.DuckDBPyConnection) -> int | None:
        """Return the stored schema version, or ``None`` on a fresh warehouse.

        Args:
            conn: Open connection.

        Returns:
            The stored version number, if present.
        """
        row: tuple[object, ...] | None = conn.execute(
            f"SELECT value FROM {SCHEMA_META_TABLE} WHERE key = 'schema_version';"
        ).fetchone()
        return None if row is None else int(str(row[0]))

    # -- writes ------------------------------------------------------------------------

    def insert_bars(
        self,
        frame: pl.DataFrame,
        *,
        instrument_id: InstrumentId,
        timeframe: Timeframe,
        mode: IngestMode,
    ) -> InsertOutcome:
        """Persist a validated bar frame.

        The frame must already carry every column in :data:`BAR_COLUMNS`, with a
        UTC-aware ``ts_utc``. Validation belongs to ingestion; by the time a frame
        reaches this method it is assumed correct, and a schema mismatch here is a
        programming error rather than a data error.

        Args:
            frame: Bars to write.
            instrument_id: Instrument the bars belong to.
            timeframe: Interval the bars belong to.
            mode: Write semantics. ``DRY_RUN`` writes nothing and reports zeroes.

        Returns:
            Counts of rows written, skipped, and deleted.

        Raises:
            WarehouseError: If required columns are missing or the write fails.
        """
        missing: tuple[str, ...] = tuple(
            column for column in BAR_COLUMNS if column not in frame.columns
        )
        if missing:
            raise WarehouseError(
                "Bar frame is missing required columns",
                missing=list(missing),
                present=frame.columns,
            )

        if mode is IngestMode.DRY_RUN or frame.height == 0:
            return InsertOutcome(
                rows_offered=frame.height,
                rows_written=0,
                rows_skipped_duplicate=0,
                rows_deleted=0,
            )

        ordered: pl.DataFrame = frame.select(BAR_COLUMNS)
        column_list: str = ", ".join(BAR_COLUMNS)
        rows_deleted: int = 0

        with self._transaction() as conn:
            before: int = self._count_bars(conn, instrument_id, timeframe)

            if mode is IngestMode.REPLACE_RANGE:
                bounds: tuple[datetime, datetime] = (
                    _as_datetime(ordered["ts_utc"].min()),
                    _as_datetime(ordered["ts_utc"].max()),
                )
                deleted_before: int = before
                conn.execute(
                    f"DELETE FROM {BARS_TABLE} "
                    "WHERE instrument_id = ? AND timeframe = ? "
                    "AND ts_utc >= ? AND ts_utc <= ?;",
                    [str(instrument_id), str(timeframe), bounds[0], bounds[1]],
                )
                after_delete: int = self._count_bars(conn, instrument_id, timeframe)
                rows_deleted = deleted_before - after_delete
                before = after_delete

            conn.register("_incoming_bars", ordered)
            try:
                conn.execute(
                    f"INSERT INTO {BARS_TABLE} ({column_list}) "
                    f"SELECT {column_list} FROM _incoming_bars "
                    "ON CONFLICT DO NOTHING;"
                )
            finally:
                conn.unregister("_incoming_bars")

            after: int = self._count_bars(conn, instrument_id, timeframe)

        written: int = after - before
        skipped: int = ordered.height - written

        if rows_deleted:
            _LOGGER.warning(
                "warehouse.history_restated",
                instrument_id=str(instrument_id),
                timeframe=str(timeframe),
                rows_deleted=rows_deleted,
            )

        _LOGGER.info(
            "warehouse.bars_inserted",
            instrument_id=str(instrument_id),
            timeframe=str(timeframe),
            mode=str(mode),
            offered=ordered.height,
            written=written,
            skipped_duplicate=skipped,
        )
        return InsertOutcome(
            rows_offered=ordered.height,
            rows_written=written,
            rows_skipped_duplicate=skipped,
            rows_deleted=rows_deleted,
        )

    def record_ingestion(
        self,
        *,
        ingestion_id: str,
        source_id: SourceId,
        instrument_id: InstrumentId,
        timeframe: Timeframe,
        mode: IngestMode,
        files_processed: tuple[str, ...],
        rows_read: int,
        rows_written: int,
        rows_skipped: int,
        rows_rejected: int,
        rows_deleted: int,
        first_ts_utc: datetime | None,
        last_ts_utc: datetime | None,
        started_at_utc: datetime,
        finished_at_utc: datetime,
    ) -> None:
        """Append an audit row describing one ingestion run.

        The log is append-only and is never consulted by the engine. It exists so that
        "where did this bar come from and when" is answerable months later.

        Args:
            ingestion_id: Unique key for the run.
            source_id: Source that was ingested.
            instrument_id: Instrument written.
            timeframe: Interval written.
            mode: Write semantics used.
            files_processed: Files read, as POSIX strings.
            rows_read: Rows parsed from disk.
            rows_written: Rows newly persisted.
            rows_skipped: Rows already present.
            rows_rejected: Rows discarded as invalid.
            rows_deleted: Rows removed under ``REPLACE_RANGE``.
            first_ts_utc: Earliest accepted timestamp.
            last_ts_utc: Latest accepted timestamp.
            started_at_utc: Run start.
            finished_at_utc: Run finish.
        """
        with self._transaction() as conn:
            conn.execute(
                f"INSERT INTO {INGESTION_LOG_TABLE} VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                [
                    ingestion_id,
                    str(source_id),
                    str(instrument_id),
                    str(timeframe),
                    str(mode),
                    "\n".join(files_processed),
                    rows_read,
                    rows_written,
                    rows_skipped,
                    rows_rejected,
                    rows_deleted,
                    first_ts_utc,
                    last_ts_utc,
                    started_at_utc,
                    finished_at_utc,
                ],
            )

    # -- reads -------------------------------------------------------------------------

    def query_bars(
        self,
        *,
        instrument_id: InstrumentId,
        timeframe: Timeframe,
        start_utc: datetime,
        end_utc: datetime,
        include_synthetic: bool = True,
    ) -> pl.DataFrame:
        """Return stored bars for a half-open window, sorted ascending by timestamp.

        Args:
            instrument_id: Instrument to load.
            timeframe: Interval to load.
            start_utc: Inclusive lower bound. Must be timezone-aware.
            end_utc: Exclusive upper bound. Must be timezone-aware.
            include_synthetic: Whether to return gap-filled bars.

        Returns:
            A Polars frame with the canonical bar columns. Empty if nothing matches.

        Raises:
            WarehouseError: If either bound is timezone-naive, or the query fails.
        """
        self._require_aware(start_utc, "start_utc")
        self._require_aware(end_utc, "end_utc")

        clause: str = "" if include_synthetic else " AND is_synthetic = FALSE"
        sql: str = (
            f"SELECT {', '.join(BAR_COLUMNS)} FROM {BARS_TABLE} "
            "WHERE instrument_id = ? AND timeframe = ? "
            f"AND ts_utc >= ? AND ts_utc < ?{clause} "
            "ORDER BY ts_utc ASC;"
        )
        try:
            return self.connection.execute(
                sql, [str(instrument_id), str(timeframe), start_utc, end_utc]
            ).pl()
        except duckdb.Error as exc:
            raise WarehouseError(
                "Bar query failed",
                instrument_id=str(instrument_id),
                timeframe=str(timeframe),
            ) from exc

    def stored_range(
        self,
        instrument_id: InstrumentId,
        timeframe: Timeframe,
    ) -> StoredRange:
        """Return the extent of stored history for one instrument and timeframe.

        Args:
            instrument_id: Instrument to describe.
            timeframe: Interval to describe.

        Returns:
            Row count and timestamp bounds. Bounds are ``None`` when nothing is stored.
        """
        row: tuple[object, ...] | None = self.connection.execute(
            f"SELECT COUNT(*), MIN(ts_utc), MAX(ts_utc) FROM {BARS_TABLE} "
            "WHERE instrument_id = ? AND timeframe = ?;",
            [str(instrument_id), str(timeframe)],
        ).fetchone()

        if row is None:
            return StoredRange(instrument_id, timeframe, 0, None, None)

        first: datetime | None = row[1] if isinstance(row[1], datetime) else None
        last: datetime | None = row[2] if isinstance(row[2], datetime) else None
        return StoredRange(instrument_id, timeframe, _as_int(row[0]), first, last)

    def known_series(self) -> tuple[tuple[InstrumentId, Timeframe], ...]:
        """Return every (instrument, timeframe) pair present in the warehouse.

        Returns:
            Sorted pairs, suitable for a coverage sweep across the whole warehouse.
        """
        rows: list[tuple[object, ...]] = self.connection.execute(
            f"SELECT DISTINCT instrument_id, timeframe FROM {BARS_TABLE} "
            "ORDER BY instrument_id, timeframe;"
        ).fetchall()
        return tuple((InstrumentId(str(row[0])), Timeframe(str(row[1]))) for row in rows)

    # -- helpers -----------------------------------------------------------------------

    @staticmethod
    def _count_bars(
        conn: duckdb.DuckDBPyConnection,
        instrument_id: InstrumentId,
        timeframe: Timeframe,
    ) -> int:
        """Return the number of stored bars for one instrument and timeframe.

        Args:
            conn: Open connection, typically inside a transaction.
            instrument_id: Instrument to count.
            timeframe: Interval to count.

        Returns:
            The row count.
        """
        row: tuple[object, ...] | None = conn.execute(
            f"SELECT COUNT(*) FROM {BARS_TABLE} WHERE instrument_id = ? AND timeframe = ?;",
            [str(instrument_id), str(timeframe)],
        ).fetchone()
        return 0 if row is None else _as_int(row[0])

    @staticmethod
    def _require_aware(value: datetime, field_name: str) -> None:
        """Reject timezone-naive datetimes at the warehouse boundary.

        Args:
            value: Datetime to check.
            field_name: Name used in the error message.

        Raises:
            WarehouseError: If the datetime carries no timezone information.
        """
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise WarehouseError(
                "Timezone-naive datetime reached the warehouse boundary",
                field=field_name,
                value=value.isoformat(),
            )
