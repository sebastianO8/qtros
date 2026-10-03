"""Writes one run's artifacts to ``results/{run_id}/`` (ARCH-002 §6.1).

Layout::

    results/{run_id}/
    ├── manifest.json              Directive 10
    ├── config_resolved.yaml       the flattened config that actually ran
    ├── fills.parquet              every cash movement (C-4)
    ├── trades.parquet             flat-to-flat lifecycles (C-4)
    ├── equity.parquet             closed_equity + mtm_equity per bar (C-1)
    ├── risk_events.json
    ├── rejected_intents.parquet   rejections are data
    ├── reconciliation.json        C-10
    ├── performance.json           Layer 5
    └── lag_audit.json

Directories are written once. A second write to the same ``run_id`` is refused; a
re-scored run creates a new directory that shares its ``run_hash`` with the original,
which is how D-1's benefit becomes visible on disk.

Monetary columns in Parquet are stored as **strings**. Polars' Decimal support is
not stable across versions, and a figure that round-trips through a float is no longer
the figure the ledger reconciled. Readers parse with ``Decimal(str)``.
"""

from __future__ import annotations

import json
import platform
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Final, Protocol

import polars as pl
import structlog
import yaml

from qtros.configuration.hashing import ConfigHashes, canonical_json
from qtros.domain.data_contracts import DatasetFingerprint
from qtros.domain.errors import QtrosError
from qtros.domain.identifiers import RunId
from qtros.domain.manifest import RunManifest
from qtros.engine.backtester import BacktestResult
from qtros.engine.builder import RunComponents
from qtros.persistence.serialize import to_jsonable

_LOGGER: Final = structlog.get_logger(__name__)

TRACKED_LIBRARIES: Final[tuple[str, ...]] = (
    "polars", "duckdb", "numpy", "pydantic", "pyyaml", "structlog",
)

WATERMARK_DIRTY_TREE: Final[str] = "UNTRUSTED: run from a dirty git working tree"


class PersistenceError(QtrosError):
    """A run artifact could not be written."""


class _HasWatermarks(Protocol):
    @property
    def watermarks(self) -> tuple[str, ...]: ...


class ScoredReport(Protocol):
    """What the writer needs from a Layer 5 report.

    Declared structurally so ``persistence`` does not import ``analytics``: the two are
    siblings in the layer contract and may not depend on each other. Any dataclass with
    a ``gates.watermarks`` tuple qualifies; the rest is serialised generically.
    """

    @property
    def gates(self) -> _HasWatermarks: ...


@dataclass(frozen=True, slots=True)
class WrittenRun:
    """Where a run landed.

    Attributes:
        run_id: The run identifier.
        directory: The run directory.
        manifest: The manifest as written.
        files: Every file written, relative to the directory.
    """

    run_id: RunId
    directory: Path
    manifest: RunManifest
    files: tuple[str, ...]


# --------------------------------------------------------------------------------------
# Provenance helpers
# --------------------------------------------------------------------------------------


def run_id_for(experiment_id: str, hashes: ConfigHashes, run_at_utc: datetime) -> RunId:
    """Return ``{utc_date}-{experiment_id}-{run_hash[:8]}`` (ARCH-002 §6.1)."""
    return RunId(f"{run_at_utc.astimezone(UTC):%Y%m%d}-{experiment_id}-{hashes.run_id_fragment}")


def git_state(repo_root: Path | None = None) -> tuple[str | None, bool | None]:
    """Return the current commit and whether the tree is dirty.

    Args:
        repo_root: Directory to query. Defaults to the current working directory.

    Returns:
        ``(sha, dirty)``; ``(None, None)`` outside a repository or without git.
    """
    cwd = str(repo_root) if repo_root is not None else None
    try:
        sha = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True, timeout=5
        ).stdout.strip()
        status = subprocess.run(  # noqa: S603
            ["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, check=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None, None
    return sha or None, bool(status.strip())


def library_versions(names: Sequence[str] = TRACKED_LIBRARIES) -> dict[str, str]:
    """Return installed versions of the tracked libraries."""
    out: dict[str, str] = {}
    for name in names:
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = "not-installed"
    return out


# --------------------------------------------------------------------------------------
# Tabular artifacts
# --------------------------------------------------------------------------------------


def _rows(records: Sequence[object], exclude: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """Flatten dataclasses to JSON-compatible row dicts, dropping nested sequences."""
    rows: list[dict[str, Any]] = []
    for record in records:
        flat = to_jsonable(record)
        rows.append({k: v for k, v in flat.items() if k not in exclude and not isinstance(v, (list, dict))})
    return rows


def _write_table(path: Path, rows: list[dict[str, Any]], schema_hint: Sequence[str]) -> None:
    """Write rows as Parquet; an empty table still gets the declared columns."""
    frame = pl.DataFrame(rows) if rows else pl.DataFrame({name: pl.Series(name, [], dtype=pl.Utf8) for name in schema_hint})
    frame.write_parquet(path)


# --------------------------------------------------------------------------------------
# Writer
# --------------------------------------------------------------------------------------


class RunWriter:
    """Writes immutable run directories under a results root.

    Args:
        results_root: Directory run directories are created in. Created if missing.
        repo_root: Repository to stamp the git revision from. ``None`` disables the
            git lookup (for tests and non-repository deployments).
    """

    __slots__ = ("_repo_root", "_root")

    def __init__(self, results_root: Path, *, repo_root: Path | None = None) -> None:
        self._root: Path = results_root
        self._repo_root: Path | None = repo_root
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        """Return the results root."""
        return self._root

    def write(
        self,
        components: RunComponents,
        result: BacktestResult,
        report: ScoredReport,
        *,
        dataset_fingerprint: DatasetFingerprint | None = None,
        run_at_utc: datetime | None = None,
        lookup_git: bool = True,
        extra_text_files: Mapping[str, str] | None = None,
    ) -> WrittenRun:
        """Write every artifact for one run.

        Args:
            components: The built run graph.
            result: The run's output.
            report: Layer 5 scoring.
            dataset_fingerprint: Content hash of the bars, when known.
            run_at_utc: Override the run timestamp for reproducible identifiers.
            lookup_git: Whether to stamp the git revision.
            extra_text_files: Additional text artifacts by file name, e.g. ``report.html``.

        Returns:
            Where the run landed.

        Raises:
            PersistenceError: If the run directory already exists.
        """
        run_at = run_at_utc or datetime.now(UTC)
        resolved = components.resolved
        run_id = run_id_for(resolved.strategy.experiment_id, components.hashes, run_at)
        directory = self._root / run_id
        if directory.exists():
            raise PersistenceError(
                "run directory already exists; run artifacts are immutable",
                run_id=str(run_id),
                directory=str(directory),
            )

        sha, dirty = git_state(self._repo_root) if lookup_git else (None, None)
        watermarks: list[str] = list(report.gates.watermarks)
        if dirty:
            watermarks.append(WATERMARK_DIRTY_TREE)
        manifest = RunManifest(
            run_id=run_id,
            experiment_id=resolved.strategy.experiment_id,
            strategy_id=result.strategy_id,
            strategy_version=result.strategy_version,
            run_hash=components.hashes.run_hash,
            report_hash=components.hashes.report_hash,
            config_hash=components.hashes.config_hash,
            feature_set_hash=result.feature_set_hash,
            sealed_columns=result.sealed_columns,
            dataset_fingerprint=dataset_fingerprint,
            git_revision=sha,
            git_dirty=dirty,
            library_versions=library_versions(),
            python_version=platform.python_version(),
            rng_seed=resolved.backtest.rng_seed,
            run_at_utc=run_at,
            trusted=result.trusted and not dirty,
            watermarks=tuple(watermarks),
            bars_processed=result.bars_processed,
            fill_count=len(result.fills),
            trade_count=len(result.closed_trades),
        )

        directory.mkdir(parents=True, exist_ok=False)
        written: list[str] = []

        def dump_json(name: str, payload: object) -> None:
            (directory / name).write_text(
                json.dumps(to_jsonable(payload), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            written.append(name)

        dump_json("manifest.json", manifest)
        (directory / "config_resolved.yaml").write_text(
            "# Fully resolved configuration — the reproduction input (ARCH-002 D-5).\n"
            "# Decimal fields are quoted strings; floats never appear here.\n"
            + yaml.safe_dump(json.loads(canonical_json(resolved)), sort_keys=True, allow_unicode=True),
            encoding="utf-8",
        )
        written.append("config_resolved.yaml")

        _write_table(directory / "fills.parquet", _rows(result.fills), ("fill_id",))
        written.append("fills.parquet")
        trade_rows = _rows(result.trades, exclude=frozenset({"fills"}))
        for row, trade in zip(trade_rows, result.trades, strict=True):
            row["fill_ids"] = ",".join(str(f.fill_id) for f in trade.fills)
        _write_table(directory / "trades.parquet", trade_rows, ("trade_id",))
        written.append("trades.parquet")
        _write_table(directory / "equity.parquet", _rows(result.equity_curve), ("ts_utc",))
        written.append("equity.parquet")
        rejected_rows = [
            {**to_jsonable(r.intent), "code": to_jsonable(r.code), "detail": r.detail,
             "rejected_at_utc": to_jsonable(r.rejected_at_utc)}
            for r in result.rejected_intents
        ]
        _write_table(
            directory / "rejected_intents.parquet",
            [{k: v for k, v in row.items() if not isinstance(v, (list, dict))} for row in rejected_rows],
            ("intent_id",),
        )
        written.append("rejected_intents.parquet")

        dump_json("risk_events.json", result.risk_events)
        dump_json("reconciliation.json", result.reconciliation)
        dump_json("performance.json", report)
        dump_json("lag_audit.json", components.feature_frame.lag_audit)
        for name, text in (extra_text_files or {}).items():
            if "/" in name or name.startswith("."):
                raise PersistenceError("extra artifact names must be plain file names", name=name)
            (directory / name).write_text(text, encoding="utf-8")
            written.append(name)

        _LOGGER.info(
            "persistence.run_written",
            run_id=str(run_id),
            directory=str(directory),
            files=len(written),
            trusted=manifest.trusted,
            watermarks=list(watermarks),
        )
        return WrittenRun(run_id=run_id, directory=directory, manifest=manifest, files=tuple(written))


def read_manifest(directory: Path) -> Mapping[str, Any]:
    """Read a run's manifest back as a plain mapping.

    Args:
        directory: Run directory.

    Returns:
        The parsed JSON.
    """
    return json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
