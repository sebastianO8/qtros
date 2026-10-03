"""HTML report and derived registry tests (ARCH-002 §6.1–§6.2)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from qtros.analytics import analyze
from qtros.analytics.render import equity_curve_svg, render_report
from qtros.configuration import ConfigLoader
from qtros.domain.errors import ConfigurationError
from qtros.engine.builder import run_from_config
from qtros.persistence import RunWriter
from qtros.persistence.registry import (
    export_registry,
    load_decisions,
    render_registry_markdown,
    scan_results,
)

from tests.test_builder import CONFIG_ROOT, synthetic_bars

RUN_AT = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def scored():  # noqa: ANN201
    import qtros.alpha.strategies.vwap_pullback  # noqa: F401

    resolved = ConfigLoader(CONFIG_ROOT).load_backtest("phase1_mvp")
    components, result = run_from_config(resolved, synthetic_bars())
    report = analyze(result, resolved, components.calendar, scored_at_utc=RUN_AT)
    return resolved, components, result, report


class TestRender:
    def test_report_is_self_contained_html(self, scored) -> None:  # noqa: ANN001
        _, components, result, report = scored
        html = render_report(report, result, run_id="r1", hashes={"run_hash": components.hashes.run_hash})
        assert html.startswith("<!DOCTYPE html>")
        assert "<script" not in html and "http://" not in html and "https://" not in html
        assert "<svg" in html and "constitution_v3_2" in html
        assert components.hashes.run_hash in html
        for check in report.gates.checks:
            assert check.name in html

    def test_untrusted_and_watermarked_runs_are_bannered(self, scored) -> None:  # noqa: ANN001
        from dataclasses import replace

        _, _, result, report = scored
        marked = replace(report, trusted=False, gates=replace(report.gates, watermarks=("SAME_BAR_CLOSE execution timing",)))
        html = render_report(marked, result, run_id="r1", hashes={})
        assert "UNTRUSTED" in html and "WATERMARK — SAME_BAR_CLOSE" in html

    def test_values_are_escaped(self, scored) -> None:  # noqa: ANN001
        _, _, result, report = scored
        html = render_report(report, result, run_id="<img src=x>", hashes={})
        assert "<img src=x>" not in html and "&lt;img" in html

    def test_equity_svg_handles_empty_and_flat_curves(self, scored) -> None:  # noqa: ANN001
        assert equity_curve_svg([]).startswith("<svg")
        _, _, result, _ = scored
        svg = equity_curve_svg(result.equity_curve[:1])
        assert "polyline" in svg

    def test_rendering_is_deterministic(self, scored) -> None:  # noqa: ANN001
        _, _, result, report = scored
        assert render_report(report, result, run_id="r", hashes={}) == render_report(report, result, run_id="r", hashes={})


class TestRegistry:
    @pytest.fixture
    def results_root(self, scored, tmp_path: Path) -> Path:  # noqa: ANN001
        _, components, result, report = scored
        writer = RunWriter(tmp_path / "results")
        writer.write(components, result, report, run_at_utc=RUN_AT, lookup_git=False)
        (tmp_path / "results" / "incomplete-run").mkdir()  # skipped, not fatal
        return tmp_path / "results"

    def test_scan_reads_manifest_and_performance(self, results_root: Path, scored) -> None:  # noqa: ANN001
        _, components, result, report = scored
        entries = scan_results(results_root)
        assert len(entries) == 1
        entry = entries[0]
        assert entry.run_hash == components.hashes.run_hash
        assert entry.trade_count == len(result.closed_trades)
        assert entry.overall == str(report.gates.overall)
        assert entry.reporting_id == "constitution_v3_2"
        assert entry.decision is None

    def test_decisions_join_by_run_id(self, results_root: Path, tmp_path: Path) -> None:
        entry = scan_results(results_root)[0]
        decisions = tmp_path / "decisions.yaml"
        decisions.write_text(
            f"decisions:\n  - run_id: {entry.run_id}\n    decision: hold\n    mechanism_note: \"needs more data\"\n"
        )
        joined = scan_results(results_root, load_decisions(decisions))[0]
        assert joined.decision == "HOLD" and joined.mechanism_note == "needs more data"

    def test_malformed_decision_is_refused(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("decisions:\n  - run_id: x\n    decision: maybe\n")
        with pytest.raises(ConfigurationError, match="unknown registry decision"):
            load_decisions(bad)

    def test_export_writes_markdown_and_json(self, results_root: Path, tmp_path: Path) -> None:
        exported = export_registry(results_root, tmp_path / "docs" / "registry.md")
        text = exported.markdown_path.read_text()
        assert text.startswith("# Strategy Registry")
        assert "| Run | Experiment |" in text
        rows = json.loads(exported.json_path.read_text())
        assert rows[0]["run_id"] == exported.entries[0].run_id
        assert "outlier_check" in rows[0]

    def test_empty_results_tree_renders_a_placeholder(self, tmp_path: Path) -> None:
        assert "no completed runs" in render_registry_markdown(scan_results(tmp_path / "nothing"))

    def test_committed_decisions_file_loads(self) -> None:
        assert load_decisions(CONFIG_ROOT / "registry" / "decisions.yaml") == {}
