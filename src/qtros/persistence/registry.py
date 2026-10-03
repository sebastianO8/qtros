"""The Strategy Registry as a derived index (ARCH-002 §6.2; Constitution §5).

The registry is rebuilt by scanning ``results/*/manifest.json`` and ``performance.json``.
It is always regenerable and therefore can never be stale or wrong. The only hand-authored
fields are the ones that need human judgement — ``mechanism_note`` and the accept/reject
``decision`` — which live in ``config/registry/decisions.yaml`` keyed by ``run_id`` and
are joined in at export time.

``registry.duckdb`` from the blueprint is deliberately not produced: only ``data/store``
may import DuckDB, and a JSON index serves the same purpose for a solo PI.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

import structlog
import yaml

from qtros.domain.errors import ConfigurationError

_LOGGER: Final = structlog.get_logger(__name__)

DECISIONS: Final[frozenset[str]] = frozenset({"ACCEPT", "REJECT", "HOLD"})


@dataclass(frozen=True, slots=True)
class RegistryDecision:
    """A hand-authored judgement about one run.

    Attributes:
        run_id: The run it applies to.
        decision: ``ACCEPT``, ``REJECT``, or ``HOLD``.
        mechanism_note: Required by C-3 before a lifetime-concentrated strategy may pass.
    """

    run_id: str
    decision: str
    mechanism_note: str = ""


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    """One row of the registry.

    Attributes:
        run_id: Run identifier.
        experiment_id: Experiment key.
        strategy_id: Strategy.
        strategy_version: Version.
        run_at_utc: ISO timestamp.
        run_hash: Full run hash.
        report_hash: Full report hash.
        trusted: Manifest trust flag.
        watermarks: Manifest watermarks.
        trade_count: Closed trades.
        sharpe: Sharpe as a string, or ``None``.
        profit_factor: As a string, or ``None``.
        max_drawdown_pct: As a string.
        net_pnl: As a string.
        overall: Gate verdict.
        publishable: Whether every gate passed without watermark.
        lifetime_concentration_pct: Top-N lifetime share, or ``None``.
        kill_criterion: Verdict of the lifetime test.
        reporting_id: Rulebook that scored the run.
        decision: Hand-authored decision, if any.
        mechanism_note: Hand-authored note, if any.
    """

    run_id: str
    experiment_id: str
    strategy_id: str
    strategy_version: str
    run_at_utc: str
    run_hash: str
    report_hash: str
    trusted: bool
    watermarks: tuple[str, ...]
    trade_count: int
    sharpe: str | None
    profit_factor: str | None
    max_drawdown_pct: str
    net_pnl: str
    overall: str
    publishable: bool
    lifetime_concentration_pct: str | None
    kill_criterion: str
    reporting_id: str
    decision: str | None = None
    mechanism_note: str = ""

    @property
    def outlier_check(self) -> str:
        """Return the constitution's "Outlier Check" column."""
        if self.kill_criterion == "FAIL":
            return "CONCENTRATED" + (" (note on file)" if self.mechanism_note else " (needs mechanism_note)")
        if self.kill_criterion == "PASS":
            return "ok"
        return "n/a"


def load_decisions(path: Path | None) -> Mapping[str, RegistryDecision]:
    """Read ``config/registry/decisions.yaml``.

    Args:
        path: The decisions file, or ``None`` / a missing file for no decisions.

    Returns:
        Decisions keyed by ``run_id``.

    Raises:
        ConfigurationError: On a malformed entry.
    """
    if path is None or not path.is_file():
        return {}
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = document.get("decisions", []) if isinstance(document, dict) else document
    out: dict[str, RegistryDecision] = {}
    for raw in entries or []:
        if not isinstance(raw, dict) or "run_id" not in raw or "decision" not in raw:
            raise ConfigurationError("registry decision needs run_id and decision", entry=raw)
        decision = str(raw["decision"]).upper()
        if decision not in DECISIONS:
            raise ConfigurationError("unknown registry decision", run_id=raw["run_id"], decision=decision)
        out[str(raw["run_id"])] = RegistryDecision(
            run_id=str(raw["run_id"]), decision=decision, mechanism_note=str(raw.get("mechanism_note", "") or "")
        )
    return out


def _read_json(path: Path) -> Mapping[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def scan_results(results_root: Path, decisions: Mapping[str, RegistryDecision] | None = None) -> tuple[RegistryEntry, ...]:
    """Build registry rows from every complete run directory.

    Args:
        results_root: The results tree.
        decisions: Hand-authored decisions to join in.

    Returns:
        Entries sorted by run timestamp, newest first. Directories lacking either
        artifact are skipped with a log line rather than failing the export.
    """
    decisions = decisions or {}
    entries: list[RegistryEntry] = []
    if not results_root.is_dir():
        return ()
    for directory in sorted(results_root.iterdir()):
        manifest_path = directory / "manifest.json"
        performance_path = directory / "performance.json"
        if not (manifest_path.is_file() and performance_path.is_file()):
            _LOGGER.warning("registry.incomplete_run", directory=str(directory))
            continue
        manifest = _read_json(manifest_path)
        performance = _read_json(performance_path)
        gates = performance.get("gates", {})
        lifetime = performance.get("concentration", {}).get("lifetime", {})
        decision = decisions.get(str(manifest["run_id"]))
        entries.append(
            RegistryEntry(
                run_id=str(manifest["run_id"]),
                experiment_id=str(manifest["experiment_id"]),
                strategy_id=str(manifest["strategy_id"]),
                strategy_version=str(manifest["strategy_version"]),
                run_at_utc=str(manifest["run_at_utc"]),
                run_hash=str(manifest["run_hash"]),
                report_hash=str(manifest["report_hash"]),
                trusted=bool(manifest.get("trusted", False)),
                watermarks=tuple(manifest.get("watermarks", ())),
                trade_count=int(manifest.get("trade_count", 0)),
                sharpe=performance.get("sharpe", {}).get("sharpe"),
                profit_factor=performance.get("trades", {}).get("profit_factor"),
                max_drawdown_pct=str(performance.get("drawdown", {}).get("max_drawdown_pct", "")),
                net_pnl=str(performance.get("trades", {}).get("net_pnl", "")),
                overall=str(gates.get("overall", "")),
                publishable=bool(gates.get("overall") == "PASS" and not gates.get("watermarks") and gates.get("kill_criterion", {}).get("verdict") != "FAIL"),
                lifetime_concentration_pct=lifetime.get("top_n_share_pct"),
                kill_criterion=str(gates.get("kill_criterion", {}).get("verdict", "")),
                reporting_id=str(performance.get("reporting_id", "")),
                decision=decision.decision if decision else None,
                mechanism_note=decision.mechanism_note if decision else "",
            )
        )
    entries.sort(key=lambda e: (e.run_at_utc, e.run_id), reverse=True)
    return tuple(entries)


def render_registry_markdown(entries: Sequence[RegistryEntry]) -> str:
    """Render the registry as the constitution's table.

    Args:
        entries: Registry rows.

    Returns:
        Markdown text.
    """
    lines = [
        "# Strategy Registry",
        "",
        "Derived index rebuilt from `results/*/manifest.json` and `performance.json` (ARCH-002 §6.2).",
        "Do not edit by hand: decisions and mechanism notes live in `config/registry/decisions.yaml`.",
        "",
        "| Run | Experiment | Strategy | Trades | Sharpe | PF | Max DD % | Net PnL | Result | Outlier Check | Trusted | Decision |",
        "|---|---|---|---:|---:|---:|---:|---:|---|---|---|---|",
    ]
    for e in entries:
        lines.append(
            f"| `{e.run_id}` | {e.experiment_id} | {e.strategy_id} {e.strategy_version} | {e.trade_count} | "
            f"{e.sharpe or 'n/a'} | {e.profit_factor or 'n/a'} | {e.max_drawdown_pct} | {e.net_pnl} | "
            f"{e.overall}{' (publishable)' if e.publishable else ''} | {e.outlier_check} | "
            f"{'yes' if e.trusted else 'NO'}{' ⚠' if e.watermarks else ''} | {e.decision or '—'} |"
        )
    if not entries:
        lines.append("| — | no completed runs | | | | | | | | | | |")
    lines.append("")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class RegistryExport:
    """Where the registry landed.

    Attributes:
        markdown_path: The committed Markdown table.
        json_path: The machine-readable index beside it.
        entries: Rows exported.
    """

    markdown_path: Path
    json_path: Path
    entries: tuple[RegistryEntry, ...]


def export_registry(results_root: Path, markdown_path: Path, *, decisions_path: Path | None = None) -> RegistryExport:
    """Scan results and write ``docs/registry.md`` plus ``docs/registry.json``.

    Args:
        results_root: The results tree.
        markdown_path: Destination Markdown file.
        decisions_path: Optional hand-authored decisions file.

    Returns:
        The export record.
    """
    entries = scan_results(results_root, load_decisions(decisions_path))
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_registry_markdown(entries), encoding="utf-8")
    json_path = markdown_path.with_suffix(".json")
    json_path.write_text(
        json.dumps([asdict(e) | {"outlier_check": e.outlier_check} for e in entries], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _LOGGER.info("registry.exported", runs=len(entries), markdown=str(markdown_path))
    return RegistryExport(markdown_path=markdown_path, json_path=json_path, entries=entries)


__all__ = [
    "RegistryDecision",
    "RegistryEntry",
    "RegistryExport",
    "export_registry",
    "load_decisions",
    "render_registry_markdown",
    "scan_results",
]
