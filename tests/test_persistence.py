"""Results writer tests (ARCH-002 §6.1; Directive 10)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import polars as pl
import pytest
import yaml

from qtros.configuration import ResolvedBacktestConfig
from qtros.persistence import RunWriter, WrittenRun, to_jsonable
from qtros.persistence.writer import PersistenceError, read_manifest, run_id_for
from tests.test_builder import CONFIG_ROOT, ScoredRun, scored_run

if TYPE_CHECKING:
    from pathlib import Path

RUN_AT = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def run() -> ScoredRun:
    return scored_run(RUN_AT)


@pytest.fixture
def written(run: ScoredRun, tmp_path: Path) -> WrittenRun:
    writer = RunWriter(tmp_path / "results")
    return writer.write(run.components, run.result, run.report, run_at_utc=RUN_AT, lookup_git=False)


class TestSerialisation:
    def test_decimals_are_exact_strings(self) -> None:
        assert to_jsonable({"x": Decimal("1.10"), "when": RUN_AT}) == {
            "x": "1.10",
            "when": "2026-10-03T12:00:00.000000+00:00",
        }

    def test_diagnostic_floats_pass_through_unchanged(self) -> None:
        """Feature values in a rationale are floats by design; money never is."""
        assert to_jsonable({"zscore": -1.5}) == {"zscore": -1.5}

    def test_none_is_preserved(self) -> None:
        assert to_jsonable({"undefined": None}) == {"undefined": None}


class TestRunWriter:
    def test_run_id_format(self, run: ScoredRun) -> None:
        resolved, components = run.resolved, run.components
        run_id = run_id_for(resolved.strategy.experiment_id, components.hashes, RUN_AT)
        assert run_id == f"20261003-AUDJPY_VWAP_PULLBACK-{components.hashes.run_hash[:8]}"

    def test_every_artifact_is_written(self, written: WrittenRun) -> None:
        expected = {
            "manifest.json",
            "config_resolved.yaml",
            "fills.parquet",
            "trades.parquet",
            "equity.parquet",
            "rejected_intents.parquet",
            "risk_events.json",
            "reconciliation.json",
            "performance.json",
            "lag_audit.json",
        }
        assert set(written.files) == expected
        assert {p.name for p in written.directory.iterdir()} == expected

    def test_manifest_records_the_three_hashes_and_provenance(
        self, written: WrittenRun, run: ScoredRun
    ) -> None:
        components, result = run.components, run.result
        manifest = read_manifest(written.directory)
        assert manifest["run_hash"] == components.hashes.run_hash
        assert manifest["report_hash"] == components.hashes.report_hash
        assert manifest["config_hash"] == components.hashes.config_hash
        assert manifest["feature_set_hash"] == result.feature_set_hash
        assert manifest["sealed_columns"] == list(result.sealed_columns)
        assert manifest["trusted"] is True
        assert manifest["git_revision"] is None  # lookup disabled
        assert "polars" in manifest["library_versions"]
        assert manifest["fill_count"] == len(result.fills)

    def test_config_resolved_round_trips_to_the_same_hashes(
        self, written: WrittenRun, run: ScoredRun
    ) -> None:
        resolved, components = run.resolved, run.components
        document = yaml.safe_load((written.directory / "config_resolved.yaml").read_text())
        reloaded = ResolvedBacktestConfig.model_validate(document)
        assert reloaded.hashes() == components.hashes
        assert reloaded.risk.max_lot == resolved.risk.max_lot

    def test_config_resolved_contains_no_floats(self, written: WrittenRun) -> None:
        def walk(node: object) -> None:
            if isinstance(node, float):
                raise AssertionError("float in config_resolved.yaml")
            if isinstance(node, dict):
                for v in node.values():
                    walk(v)
            if isinstance(node, list):
                for v in node:
                    walk(v)

        walk(yaml.safe_load((written.directory / "config_resolved.yaml").read_text()))

    def test_fills_parquet_preserves_exact_money(self, written: WrittenRun, run: ScoredRun) -> None:
        result = run.result
        fills = pl.read_parquet(written.directory / "fills.parquet")
        assert fills.height == len(result.fills)
        assert fills.schema["cash_delta"] == pl.Utf8
        total = sum(Decimal(v) for v in fills.get_column("cash_delta").to_list())
        assert total == result.final_cash - Decimal("100000")

    def test_trades_parquet_links_fills(self, written: WrittenRun, run: ScoredRun) -> None:
        result = run.result
        trades = pl.read_parquet(written.directory / "trades.parquet")
        assert trades.height == len(result.trades)
        if result.trades:
            first = trades.row(0, named=True)
            assert first["fill_ids"].split(",") == [str(f.fill_id) for f in result.trades[0].fills]

    def test_performance_json_is_exact(self, written: WrittenRun, run: ScoredRun) -> None:
        report = run.report
        performance = json.loads((written.directory / "performance.json").read_text())
        assert performance["reporting_id"] == "constitution_v3_2"
        assert performance["trades"]["net_pnl"] == str(report.trades.net_pnl)
        assert performance["gates"]["overall"] in {"PASS", "FAIL", "INSUFFICIENT_SAMPLE"}

    def test_run_directories_are_immutable(self, written: WrittenRun, run: ScoredRun) -> None:
        components, result, report = run.components, run.result, run.report
        writer = RunWriter(written.directory.parent)
        with pytest.raises(PersistenceError, match="immutable"):
            writer.write(components, result, report, run_at_utc=RUN_AT, lookup_git=False)

    def test_git_state_stamps_the_repository(self, run: ScoredRun, tmp_path: Path) -> None:
        components, result, report = run.components, run.result, run.report
        writer = RunWriter(tmp_path / "results", repo_root=CONFIG_ROOT.parent)
        landed = writer.write(components, result, report, run_at_utc=RUN_AT)
        assert landed.manifest.git_revision is not None and len(landed.manifest.git_revision) == 40
        assert landed.manifest.git_dirty in (True, False)
        if landed.manifest.git_dirty:
            assert not landed.manifest.trusted
            assert any("dirty" in w for w in landed.manifest.watermarks)
