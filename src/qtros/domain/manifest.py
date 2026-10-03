"""Run manifest (Directive 10; ARCH-002 §5–§6).

The manifest is the claim that a result is reproducible, written in enough detail that
the claim can be tested: configuration hashes, dataset fingerprint, code revision,
library versions, and the seed. A dirty working tree does not block a run but stamps it
``UNTRUSTED`` — the same treatment as an unreconciled ledger, for the same reason: it
must be impossible to later mistake an exploratory number for a validated one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from qtros.domain.data_contracts import DatasetFingerprint
from qtros.domain.identifiers import RunId

MANIFEST_SCHEMA_VERSION: int = 1


@dataclass(frozen=True, slots=True)
class RunManifest:
    """Provenance record written once to ``results/{run_id}/manifest.json``.

    Attributes:
        run_id: ``{utc_date}-{experiment_id}-{run_hash[:8]}``.
        experiment_id: Registry key.
        strategy_id: Strategy that ran.
        strategy_version: Its version.
        run_hash: Digest of the run-affecting configuration.
        report_hash: Digest of the report-affecting configuration.
        config_hash: Directive 10's combined identity.
        feature_set_hash: Hash of the transform set that produced the features.
        sealed_columns: Feature columns whose provenance was verified (G-4).
        dataset_fingerprint: Content hash of the bars, when supplied.
        git_revision: Commit SHA, or ``None`` outside a repository.
        git_dirty: Whether the working tree had uncommitted changes.
        library_versions: Resolved versions of the data-layer libraries.
        python_version: Interpreter version.
        rng_seed: Seed recorded even though Phase 1 has no stochastic component.
        run_at_utc: When the run executed.
        trusted: ``False`` if unreconciled or run from a dirty tree.
        watermarks: Every reason the result is not publishable.
        bars_processed: Bars stepped.
        fill_count: Fills booked.
        trade_count: Closed trades.
        manifest_schema_version: Format version of this record.
    """

    run_id: RunId
    experiment_id: str
    strategy_id: str
    strategy_version: str
    run_hash: str
    report_hash: str
    config_hash: str
    feature_set_hash: str
    sealed_columns: tuple[str, ...]
    dataset_fingerprint: DatasetFingerprint | None
    git_revision: str | None
    git_dirty: bool | None
    library_versions: Mapping[str, str]
    python_version: str
    rng_seed: int
    run_at_utc: datetime
    trusted: bool
    watermarks: tuple[str, ...] = field(default_factory=tuple)
    bars_processed: int = 0
    fill_count: int = 0
    trade_count: int = 0
    manifest_schema_version: int = MANIFEST_SCHEMA_VERSION
