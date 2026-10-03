"""One module per configuration family (ARCH-002 §4)."""

from __future__ import annotations

from qtros.configuration.models.backtest import (
    BacktestConfig,
    DateRangeConfig,
    PathsConfig,
    RuntimeConfig,
    StrategyRef,
)
from qtros.configuration.models.base import (
    ProvenanceClass,
    QtrosConfig,
    StrictDecimal,
    TimeZoneName,
    provenance,
    reject_float_input,
)
from qtros.configuration.models.execution import ExecutionConfig
from qtros.configuration.models.friction import FrictionConfig
from qtros.configuration.models.instrument import InstrumentConfig
from qtros.configuration.models.reporting import (
    ConcentrationConfig,
    GateThresholdsConfig,
    ReportingConfig,
    SharpeConventionConfig,
)
from qtros.configuration.models.risk import RiskLimitsConfig
from qtros.configuration.models.source import ColumnMappingConfig, DataSourceConfig
from qtros.configuration.models.strategy import SignificancePolicyConfig, StrategyConfig

__all__ = [
    "BacktestConfig",
    "ColumnMappingConfig",
    "ConcentrationConfig",
    "DataSourceConfig",
    "DateRangeConfig",
    "ExecutionConfig",
    "FrictionConfig",
    "GateThresholdsConfig",
    "InstrumentConfig",
    "PathsConfig",
    "ProvenanceClass",
    "QtrosConfig",
    "ReportingConfig",
    "RiskLimitsConfig",
    "RuntimeConfig",
    "SharpeConventionConfig",
    "SignificancePolicyConfig",
    "StrategyConfig",
    "StrategyRef",
    "StrictDecimal",
    "TimeZoneName",
    "provenance",
    "reject_float_input",
]
