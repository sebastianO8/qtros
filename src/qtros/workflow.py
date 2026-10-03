"""Run orchestration: warehouse → features → engine → analytics → persistence → render.

This module is the one place that joins every layer end to end, which is why it sits
above ``analytics`` and ``persistence`` in the import contract and below ``cli``. The CLI
is a thin shell over it; tests drive it directly.

Two decisions live here rather than in the layers below:

* **Coverage is a precondition, not a warning.** A run refuses to start on data with
  unexpected gaps, conflicting duplicates, or impossible bars unless the caller explicitly
  allows it — and an allowed run carries a watermark, so the number it produces cannot
  later be mistaken for a validated one.
* **Only base columns reach the feature pipeline.** The warehouse returns bookkeeping
  columns (``source_id``, ``ingested_at_utc``); the G-4 provenance check would rightly
  refuse a frame carrying unsealed columns, so they are dropped at the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import TYPE_CHECKING, Final

import structlog

from qtros.analytics import PerformanceReport, analyze
from qtros.analytics.render import render_report
from qtros.configuration.registry import StrategyRegistry, strategy_registry
from qtros.data.coverage import analyze_coverage
from qtros.data.fingerprint import compute_fingerprint
from qtros.data.ingestion import IngestionService, default_path_guard
from qtros.data.store import WarehouseStore
from qtros.domain.enums import IngestMode
from qtros.domain.errors import CoverageError
from qtros.domain.identifiers import InstrumentId, RunId
from qtros.engine.builder import RunComponents, run_from_config
from qtros.persistence.writer import RunWriter, WrittenRun, run_id_for

if TYPE_CHECKING:
    from pathlib import Path

    import polars as pl

    from qtros.configuration.loader import ResolvedBacktestConfig
    from qtros.configuration.models.source import DataSourceConfig
    from qtros.domain.data_contracts import CoverageReport, DatasetFingerprint, IngestionReport
    from qtros.domain.timekeeping import SessionCalendar
    from qtros.engine.backtester import BacktestResult

_LOGGER: Final = structlog.get_logger(__name__)

BASE_INPUT_COLUMNS: Final[tuple[str, ...]] = (
    "ts_utc",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "is_synthetic",
)
"""The Layer 0 columns the feature pipeline may see. Everything else is bookkeeping."""

WATERMARK_COVERAGE: Final[str] = "data coverage not clean (gaps or anomalies allowed by operator)"


@dataclass(frozen=True, slots=True)
class LoadedBars:
    """Bars ready for the feature pipeline, with their provenance.

    Attributes:
        frame: Base columns only, ascending by timestamp.
        fingerprint: Content hash of the loaded range (Directive 10).
        coverage: Quality assessment of the range.
    """

    frame: pl.DataFrame
    fingerprint: DatasetFingerprint
    coverage: CoverageReport


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """Everything one invocation produced.

    Attributes:
        run_id: Run identifier.
        components: The built run graph.
        result: The run.
        report: Layer 5 scoring.
        loaded: The bars and their provenance.
        written: Where artifacts landed, or ``None`` if writing was disabled.
        html: The rendered report, or ``None`` if rendering was disabled.
    """

    run_id: RunId
    components: RunComponents
    result: BacktestResult
    report: PerformanceReport
    loaded: LoadedBars
    written: WrittenRun | None
    html: str | None


def _window(resolved: ResolvedBacktestConfig) -> tuple[datetime, datetime]:
    """Return the half-open UTC window the date range describes."""
    start = datetime.combine(resolved.backtest.date_range.start, time.min, tzinfo=UTC)
    end = datetime.combine(resolved.backtest.date_range.end, time.min, tzinfo=UTC)
    return start, end


def load_bars(
    resolved: ResolvedBacktestConfig,
    *,
    warehouse_path: Path,
    calendar: SessionCalendar,
    allow_gaps: bool = False,
) -> LoadedBars:
    """Load, assess, and fingerprint the bars a run needs.

    Args:
        resolved: The flattened configuration.
        warehouse_path: DuckDB file.
        calendar: For gap attribution.
        allow_gaps: Proceed on unclean coverage. The run is watermarked.

    Returns:
        The loaded bars.

    Raises:
        CoverageError: If nothing is stored for the window, or coverage is unclean and
            not explicitly allowed.
    """
    instrument_id = InstrumentId(resolved.instrument.instrument_id)
    timeframe = resolved.source.timeframe
    start, end = _window(resolved)

    with WarehouseStore(warehouse_path, read_only=True) as store:
        frame = store.query_bars(
            instrument_id=instrument_id, timeframe=timeframe, start_utc=start, end_utc=end
        )
    if frame.height == 0:
        raise CoverageError(
            "no bars stored for the requested window; ingest the source first",
            instrument_id=str(instrument_id),
            timeframe=str(timeframe),
            start=start.isoformat(),
            end=end.isoformat(),
            warehouse=str(warehouse_path),
        )

    coverage = analyze_coverage(
        frame, instrument_id=instrument_id, timeframe=timeframe, calendar=calendar
    )
    if not coverage.is_clean:
        detail = {
            "unexpected_gaps": len(coverage.unexpected_gaps),
            "duplicates": len(coverage.duplicates),
            "ohlc_violations": len(coverage.ohlc_violations),
            "out_of_order_rows": coverage.out_of_order_rows,
        }
        if not allow_gaps:
            raise CoverageError(
                "stored bars do not meet coverage requirements; re-run with gaps allowed "
                "to proceed under a watermark",
                **detail,
            )
        _LOGGER.warning("workflow.coverage_unclean_allowed", **detail)

    fingerprint = compute_fingerprint(frame, instrument_id=instrument_id, timeframe=timeframe)
    _LOGGER.info(
        "workflow.bars_loaded",
        rows=frame.height,
        first=str(coverage.first_ts_utc),
        last=str(coverage.last_ts_utc),
        fingerprint=fingerprint.short_hash,
        clean=coverage.is_clean,
    )
    return LoadedBars(
        frame=frame.select(BASE_INPUT_COLUMNS), fingerprint=fingerprint, coverage=coverage
    )


def execute(
    resolved: ResolvedBacktestConfig,
    *,
    warehouse_path: Path,
    results_root: Path,
    repo_root: Path | None = None,
    write: bool = True,
    render: bool = True,
    allow_gaps: bool = False,
    run_at_utc: datetime | None = None,
    registry: StrategyRegistry = strategy_registry,
) -> RunOutcome:
    """Run a backtest end to end from the warehouse.

    Args:
        resolved: The flattened configuration.
        warehouse_path: DuckDB file.
        results_root: Where the run directory is written.
        repo_root: Repository to stamp the git revision from; ``None`` skips the lookup.
        write: Persist artifacts.
        render: Render ``report.html``.
        allow_gaps: Proceed on unclean coverage, under a watermark.
        run_at_utc: Override the run timestamp for reproducible identifiers.
        registry: Strategy registry.

    Returns:
        The outcome.
    """
    run_at = run_at_utc or datetime.now(UTC)
    calendar = resolved.calendar.to_calendar(
        rollover_time_local=resolved.instrument.rollover_time_local,
        rollover_tz=resolved.instrument.rollover_tz,
    )
    loaded = load_bars(
        resolved, warehouse_path=warehouse_path, calendar=calendar, allow_gaps=allow_gaps
    )
    components, result = run_from_config(resolved, loaded.frame, registry=registry)
    extra = (WATERMARK_COVERAGE,) if not loaded.coverage.is_clean else ()
    report = analyze(
        result, resolved, components.calendar, scored_at_utc=run_at, extra_watermarks=extra
    )
    run_id = run_id_for(resolved.strategy.experiment_id, components.hashes, run_at)

    html: str | None = None
    if render:
        html = render_report(
            report,
            result,
            run_id=str(run_id),
            hashes={
                "run_hash": components.hashes.run_hash,
                "report_hash": components.hashes.report_hash,
                "config_hash": components.hashes.config_hash,
            },
        )

    written: WrittenRun | None = None
    if write:
        written = RunWriter(results_root, repo_root=repo_root).write(
            components,
            result,
            report,
            dataset_fingerprint=loaded.fingerprint,
            run_at_utc=run_at,
            lookup_git=repo_root is not None,
            extra_text_files={"report.html": html} if html is not None else {},
        )

    _LOGGER.info(
        "workflow.run_complete",
        run_id=str(run_id),
        trades=report.trades.trade_count,
        overall=str(report.gates.overall),
        publishable=report.gates.publishable,
        written=str(written.directory) if written else None,
    )
    return RunOutcome(
        run_id=run_id,
        components=components,
        result=result,
        report=report,
        loaded=loaded,
        written=written,
        html=html,
    )


def ingest_source(
    source: DataSourceConfig,
    *,
    warehouse_path: Path,
    input_root: Path,
    results_root: Path,
    mode: IngestMode = IngestMode.APPEND_ONLY,
) -> IngestionReport:
    """Register and ingest one configured source into the warehouse.

    Args:
        source: The source configuration.
        warehouse_path: DuckDB file; created if missing.
        input_root: Raw data tree the source must live under.
        results_root: Forbidden as an input (Cycle 1 self-ingestion guard).
        mode: Write semantics.

    Returns:
        The ingestion report.
    """
    guard = default_path_guard(
        input_root=input_root, results_root=results_root, warehouse_path=warehouse_path
    )
    warehouse_path.parent.mkdir(parents=True, exist_ok=True)
    with WarehouseStore(warehouse_path) as store:
        store.initialize_schema()
        service = IngestionService(store, guard)
        handle = service.register_source(source.to_spec())
        return service.ingest(handle, mode=mode)


__all__ = [
    "BASE_INPUT_COLUMNS",
    "LoadedBars",
    "RunOutcome",
    "execute",
    "ingest_source",
    "load_bars",
]
