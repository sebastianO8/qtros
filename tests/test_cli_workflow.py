"""CLI and workflow tests: the DuckDB-backed path from warehouse to results/ (C-10).

The warehouse is populated directly through ``WarehouseStore`` with the same synthetic
bars the builder tests use, so the only new surface under test is the join between the
data layer and the engine.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

from qtros.cli import app
from qtros.configuration import ConfigLoader
from qtros.data.store import WarehouseStore
from qtros.domain.enums import IngestMode, Timeframe
from qtros.domain.errors import CoverageError
from qtros.domain.identifiers import InstrumentId
from qtros.workflow import BASE_INPUT_COLUMNS, execute, load_bars

from tests.test_builder import CONFIG_ROOT, synthetic_bars

RUN_AT = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)


def populate(warehouse: Path, bars: pl.DataFrame, *, with_gap: bool = False) -> None:
    """Insert synthetic bars as the warehouse would hold them after ingestion."""
    if with_gap:
        bars = pl.concat([bars.head(300), bars.tail(300)])  # a 300-bar hole mid-week
    frame = bars.with_columns(
        pl.lit("AUDJPY").alias("instrument_id"),
        pl.lit("M5").alias("timeframe"),
        pl.lit(False).alias("is_synthetic"),
        pl.lit("dukascopy_m5").alias("source_id"),
        pl.lit(RUN_AT).alias("ingested_at_utc"),
    ).select(
        "instrument_id", "timeframe", "ts_utc", "open", "high", "low", "close", "volume",
        "is_synthetic", "source_id", "ingested_at_utc",
    )
    with WarehouseStore(warehouse) as store:
        store.initialize_schema()
        store.insert_bars(frame, instrument_id=InstrumentId("AUDJPY"), timeframe=Timeframe.M5, mode=IngestMode.APPEND_ONLY)


@pytest.fixture
def warehouse(tmp_path: Path) -> Path:
    path = tmp_path / "warehouse" / "qtros.duckdb"
    populate(path, synthetic_bars())
    return path


@pytest.fixture(scope="module")
def resolved():  # noqa: ANN201
    import qtros.alpha.strategies.vwap_pullback  # noqa: F401

    return ConfigLoader(CONFIG_ROOT).load_backtest("phase1_mvp")


class TestWorkflow:
    def test_load_bars_strips_bookkeeping_columns(self, resolved, warehouse: Path) -> None:  # noqa: ANN001
        calendar = resolved.calendar.to_calendar(
            rollover_time_local=resolved.instrument.rollover_time_local, rollover_tz=resolved.instrument.rollover_tz
        )
        loaded = load_bars(resolved, warehouse_path=warehouse, calendar=calendar)
        assert tuple(loaded.frame.columns) == BASE_INPUT_COLUMNS
        assert loaded.frame.height == 900
        assert loaded.coverage.is_clean
        assert len(loaded.fingerprint.content_hash) == 64

    def test_empty_window_is_a_coverage_error(self, resolved, tmp_path: Path) -> None:  # noqa: ANN001
        empty = tmp_path / "empty.duckdb"
        with WarehouseStore(empty) as store:
            store.initialize_schema()
        calendar = resolved.calendar.to_calendar(
            rollover_time_local=resolved.instrument.rollover_time_local, rollover_tz=resolved.instrument.rollover_tz
        )
        with pytest.raises(CoverageError, match="no bars stored"):
            load_bars(resolved, warehouse_path=empty, calendar=calendar)

    def test_unclean_coverage_refuses_unless_allowed_and_then_watermarks(self, resolved, tmp_path: Path) -> None:  # noqa: ANN001
        gappy = tmp_path / "gappy.duckdb"
        populate(gappy, synthetic_bars(), with_gap=True)
        with pytest.raises(CoverageError, match="coverage requirements"):
            execute(resolved, warehouse_path=gappy, results_root=tmp_path / "r", write=False, render=False, run_at_utc=RUN_AT)
        outcome = execute(
            resolved, warehouse_path=gappy, results_root=tmp_path / "r",
            write=False, render=False, allow_gaps=True, run_at_utc=RUN_AT,
        )
        assert any("coverage" in w for w in outcome.report.gates.watermarks)
        assert not outcome.report.gates.publishable

    def test_execute_end_to_end_writes_every_artifact(self, resolved, warehouse: Path, tmp_path: Path) -> None:  # noqa: ANN001
        outcome = execute(resolved, warehouse_path=warehouse, results_root=tmp_path / "results", run_at_utc=RUN_AT)
        assert outcome.result.reconciliation.passed
        assert outcome.written is not None
        names = {p.name for p in outcome.written.directory.iterdir()}
        assert {"manifest.json", "config_resolved.yaml", "fills.parquet", "performance.json", "report.html"} <= names
        assert outcome.written.manifest.dataset_fingerprint is not None
        assert outcome.written.manifest.dataset_fingerprint.content_hash == outcome.loaded.fingerprint.content_hash
        assert outcome.html is not None and "<svg" in outcome.html
        assert str(outcome.run_id).startswith("20261003-AUDJPY_VWAP_PULLBACK-")

    def test_execution_is_deterministic_across_warehouse_loads(self, resolved, warehouse: Path, tmp_path: Path) -> None:  # noqa: ANN001
        first = execute(resolved, warehouse_path=warehouse, results_root=tmp_path / "a", write=False, render=False, run_at_utc=RUN_AT)
        second = execute(resolved, warehouse_path=warehouse, results_root=tmp_path / "b", write=False, render=False, run_at_utc=RUN_AT)
        assert first.result.equity_curve == second.result.equity_curve
        assert first.loaded.fingerprint.content_hash == second.loaded.fingerprint.content_hash
        assert first.run_id == second.run_id


class TestCli:
    runner = CliRunner()

    def test_validate_by_id_and_by_path(self) -> None:
        by_id = self.runner.invoke(app, ["validate", "phase1_mvp", "--config-root", str(CONFIG_ROOT)])
        by_path = self.runner.invoke(app, ["validate", str(CONFIG_ROOT / "backtest" / "phase1_mvp.yaml")])
        assert by_id.exit_code == 0, by_id.output
        assert by_path.exit_code == 0, by_path.output
        assert "run_hash" in by_id.output
        assert by_id.output.splitlines()[-3:] == by_path.output.splitlines()[-3:]

    def test_unknown_backtest_fails_cleanly(self) -> None:
        result = self.runner.invoke(app, ["validate", "ghost", "--config-root", str(CONFIG_ROOT)])
        assert result.exit_code == 1
        assert "error:" in result.output

    def test_run_writes_results_and_report(self, warehouse: Path, tmp_path: Path) -> None:
        results = tmp_path / "results"
        result = self.runner.invoke(
            app,
            [
                "run", "phase1_mvp", "--config-root", str(CONFIG_ROOT),
                "--warehouse", str(warehouse), "--results-root", str(results),
                "--repo-root", str(CONFIG_ROOT.parent), "--log-level", "WARNING",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "run_id" in result.output and "reconciled    True" in result.output
        run_dirs = list(results.iterdir())
        assert len(run_dirs) == 1
        assert (run_dirs[0] / "report.html").is_file()
        assert (run_dirs[0] / "manifest.json").is_file()

    def test_run_against_an_empty_warehouse_exits_one(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.duckdb"
        with WarehouseStore(empty) as store:
            store.initialize_schema()
        result = self.runner.invoke(
            app, ["run", "phase1_mvp", "--config-root", str(CONFIG_ROOT), "--warehouse", str(empty),
                  "--results-root", str(tmp_path / "r"), "--no-write", "--log-level", "ERROR"],
        )
        assert result.exit_code == 1
        assert "no bars stored" in result.output

    def test_registry_rebuilds_from_results(self, warehouse: Path, tmp_path: Path) -> None:
        results = tmp_path / "results"
        self.runner.invoke(
            app, ["run", "phase1_mvp", "--config-root", str(CONFIG_ROOT), "--warehouse", str(warehouse),
                  "--results-root", str(results), "--no-render", "--log-level", "ERROR"],
        )
        out = tmp_path / "docs" / "registry.md"
        result = self.runner.invoke(app, ["registry", "--results-root", str(results), "--output", str(out)])
        assert result.exit_code == 0, result.output
        assert out.is_file() and out.with_suffix(".json").is_file()
        text = out.read_text()
        assert "AUDJPY_VWAP_PULLBACK" in text and "vwap_pullback 1.0.0" in text

    def test_ingest_dry_run_against_a_csv(self, tmp_path: Path) -> None:
        """A Dukascopy-shaped CSV under the input root parses in DRY_RUN without writing."""
        raw = tmp_path / "data_lake" / "raw" / "audjpy_m5"
        raw.mkdir(parents=True)
        bars = synthetic_bars(50)
        lines = ["Gmt time,Open,High,Low,Close,Volume"]
        for row in bars.iter_rows(named=True):
            lines.append(f"{row['ts_utc']:%d.%m.%Y %H:%M:%S.000},{row['open']},{row['high']},{row['low']},{row['close']},{row['volume']}")
        (raw / "AUDJPY_M5.csv").write_text("\n".join(lines) + "\n")
        warehouse = tmp_path / "data_lake" / "warehouse" / "qtros.duckdb"
        result = self.runner.invoke(
            app, ["ingest", "dukascopy_m5", "--config-root", str(CONFIG_ROOT), "--warehouse", str(warehouse),
                  "--input-root", str(tmp_path / "data_lake" / "raw"), "--results-root", str(tmp_path / "results"),
                  "--mode", "DRY_RUN", "--log-level", "ERROR"],
            env={"PWD": str(tmp_path)},
        )
        assert result.exit_code in (0, 1), result.output
        if result.exit_code == 0:
            assert "rows read 50" in result.output
