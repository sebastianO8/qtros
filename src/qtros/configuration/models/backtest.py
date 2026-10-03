"""Top-level run composition (ARCH-002 §4.8, D-5).

A backtest names its parts by identifier. The loader resolves each reference exactly once
and the fully flattened result is what gets hashed and archived. No YAML anchors, no merge
keys, no overlay chains — one resolution step, one artifact.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from pydantic import Field, model_validator

from qtros.configuration.models.base import ProvenanceClass, QtrosConfig, StrictDecimal, provenance
from qtros.configuration.models.execution import ExecutionConfig

_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})
_LOG_FORMATS = frozenset({"json", "console"})


class DateRangeConfig(QtrosConfig):
    """Inclusive start, exclusive end, so adjacent ranges tile without overlap.

    Attributes:
        start: First trading date included.
        end: First date excluded.
    """

    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> DateRangeConfig:
        """Require a non-empty range."""
        if self.end <= self.start:
            raise ValueError("date_range.end must be after date_range.start")
        return self


class StrategyRef(QtrosConfig):
    """Reference to a registered strategy (D-4).

    Attributes:
        strategy_id: Registry key.
        strategy_version: Registered version.
    """

    strategy_id: str = Field(min_length=1)
    strategy_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")


class PathsConfig(QtrosConfig):
    """Filesystem locations. Operational: never hashed.

    Attributes:
        results_root: Directory run artifacts are written under.
        warehouse: DuckDB file.
    """

    __provenance__ = ProvenanceClass.OPERATIONAL

    results_root: Path = Path("./results")
    warehouse: Path = Path("./data_lake/warehouse/qtros.duckdb")


class RuntimeConfig(QtrosConfig):
    """Process settings. Operational: never hashed.

    Attributes:
        log_level: structlog level name.
        log_format: ``json`` or ``console``.
        workers: Parallel workers for batch runs.
        progress: Whether a progress display is shown.
    """

    __provenance__ = ProvenanceClass.OPERATIONAL

    log_level: str = "INFO"
    log_format: str = "json"
    workers: int = Field(gt=0, default=1)
    progress: bool = False

    @model_validator(mode="after")
    def _known_values(self) -> RuntimeConfig:
        """Validate the level and format names."""
        if self.log_level not in _LOG_LEVELS:
            raise ValueError(f"log_level must be one of {sorted(_LOG_LEVELS)}")
        if self.log_format not in _LOG_FORMATS:
            raise ValueError(f"log_format must be one of {sorted(_LOG_FORMATS)}")
        return self


class BacktestConfig(QtrosConfig):
    """A run, composed by reference.

    Attributes:
        run_label: Human label. Operational.
        instrument_id: → ``config/instruments/{id}.yaml``.
        source_id: → ``config/sources/{id}.yaml``.
        date_range: Bars to load.
        feature_set_id: → ``config/features/{id}.yaml`` (Phase 1: identifier only).
        strategy_ref: → ``config/strategies/`` entry with matching id and version.
        risk_ref: → ``config/risk/{ref}.yaml``.
        friction_ref: → ``config/friction/{ref}.yaml``.
        execution: Inline execution settings.
        reporting_ref: → ``config/reporting/{ref}.yaml``. Report-affecting.
        rng_seed: Seed for any stochastic component. Run-affecting even though Phase 1
            has none, so adding one later does not change the hash of old runs.
        initial_cash: Starting balance in the quote currency.
        paths: Operational.
        runtime: Operational.
    """

    run_label: str = Field(min_length=1, **provenance(ProvenanceClass.OPERATIONAL))
    instrument_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    date_range: DateRangeConfig
    feature_set_id: str = Field(min_length=1)
    strategy_ref: StrategyRef
    risk_ref: str = Field(min_length=1)
    friction_ref: str = Field(min_length=1)
    execution: ExecutionConfig = ExecutionConfig()
    reporting_ref: str = Field(min_length=1, **provenance(ProvenanceClass.REPORT_AFFECTING))
    rng_seed: int = 0
    initial_cash: StrictDecimal
    paths: PathsConfig = Field(
        default_factory=PathsConfig, **provenance(ProvenanceClass.OPERATIONAL)
    )
    runtime: RuntimeConfig = Field(
        default_factory=RuntimeConfig, **provenance(ProvenanceClass.OPERATIONAL)
    )

    @model_validator(mode="after")
    def _funded(self) -> BacktestConfig:
        """Require a positive starting balance."""
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        return self
