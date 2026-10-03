"""Backtest orchestration: context construction, the position book, the run loop, and
reconciliation."""

from __future__ import annotations

from qtros.engine.backtester import (
    BacktestError,
    Backtester,
    BacktestResult,
    ProtectiveLevels,
    ReconciliationFailed,
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
    "ReconciliationFailed",
    "equity_curve_bounds",
]
