"""Configuration schema, loading, hashing, and the strategy registry (ARCH-002 §4–§5).

This package describes a run; it never executes one. It imports ``domain/`` and nothing
else inside ``qtros`` (import-linter contract "Config describes, never executes").

Three rulings shape it:

* **D-1** — configuration is partitioned into run-affecting, report-affecting, and
  operational fields, hashed separately, so that re-scoring a stored ledger never
  requires re-running the backtest.
* **D-2** — every ``Decimal`` field rejects ``float`` input. YAML authors quote their
  decimals; the schema makes the alternative a validation error rather than a
  silently imprecise number.
* **G-5** — the significance policy is run-affecting and lives in the run hash.
"""

from __future__ import annotations

from qtros.configuration.hashing import ConfigHashes, canonical_json, compute_hashes, partition
from qtros.configuration.loader import (
    CONFIG_FAMILIES,
    ConfigLoader,
    ResolvedBacktestConfig,
    load_yaml_document,
)
from qtros.configuration.models import (
    BacktestConfig,
    CalendarConfig,
    ConcentrationConfig,
    DataSourceConfig,
    DateRangeConfig,
    ExecutionConfig,
    FeatureSetConfig,
    FrictionConfig,
    GateThresholdsConfig,
    InstrumentConfig,
    PathsConfig,
    ProvenanceClass,
    QtrosConfig,
    ReportingConfig,
    RiskLimitsConfig,
    RuntimeConfig,
    SharpeConventionConfig,
    SignificancePolicyConfig,
    StrategyConfig,
    StrategyRef,
    StrictDecimal,
    provenance,
)
from qtros.configuration.registry import (
    RegisteredStrategy,
    StrategyRegistry,
    StrategyRegistryError,
    strategy_registry,
)

__all__ = [
    "CONFIG_FAMILIES",
    "BacktestConfig",
    "CalendarConfig",
    "ConcentrationConfig",
    "ConfigHashes",
    "ConfigLoader",
    "DataSourceConfig",
    "DateRangeConfig",
    "ExecutionConfig",
    "FeatureSetConfig",
    "FrictionConfig",
    "GateThresholdsConfig",
    "InstrumentConfig",
    "PathsConfig",
    "ProvenanceClass",
    "QtrosConfig",
    "RegisteredStrategy",
    "ReportingConfig",
    "ResolvedBacktestConfig",
    "RiskLimitsConfig",
    "RuntimeConfig",
    "SharpeConventionConfig",
    "SignificancePolicyConfig",
    "StrategyConfig",
    "StrategyRef",
    "StrategyRegistry",
    "StrategyRegistryError",
    "StrictDecimal",
    "canonical_json",
    "compute_hashes",
    "load_yaml_document",
    "partition",
    "provenance",
    "strategy_registry",
]
