"""Backtest orchestration.

Context construction, the position book, the run loop, the config-driven builder, and
reconciliation.
"""

from __future__ import annotations

from qtros.engine.backtester import (
    Backtester,
    BacktestError,
    BacktestResult,
    ProtectiveLevels,
    ReconciliationFailedError,
)
from qtros.engine.context import AVAILABILITY_LAG_BARS, BarContextFactory
from qtros.engine.portfolio import FillOutcome, Portfolio, equity_curve_bounds

__all__ = [
    "AVAILABILITY_LAG_BARS",
    "BacktestError",
    "BacktestResult",
    "Backtester",
    "BarContextFactory",
    "FillOutcome",
    "Portfolio",
    "ProtectiveLevels",
    "ReconciliationFailedError",
    "equity_curve_bounds",
]
