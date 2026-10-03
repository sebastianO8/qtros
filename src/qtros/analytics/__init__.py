"""Layer 5 — metrics, concentration, attribution, and constitutional gates.

Everything here is **report-affecting** (ARCH-002 D-1): it scores a fixed ledger and
never changes which fills occurred. A stored run can be re-scored under a new reporting
config without being re-run, and the result carries the ``reporting_id`` that scored it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from qtros.analytics.attribution import AttributionReport, attribute
from qtros.analytics.concentration import ConcentrationResult, concentration
from qtros.analytics.gates import (
    WATERMARK_FAVOURABLE_FIRST,
    WATERMARK_HALTED,
    WATERMARK_SAME_BAR_CLOSE,
    WATERMARK_UNTRUSTED,
    GateCheck,
    GateReport,
    evaluate_gates,
)
from qtros.analytics.metrics import (
    DailyPoint,
    DrawdownResult,
    SharpeResult,
    TradeStatistics,
    daily_equity,
    drawdown,
    periodic_returns,
    sharpe_ratio,
    trade_statistics,
)
from qtros.configuration.loader import ResolvedBacktestConfig
from qtros.domain.enums import ExecutionTiming, PathAssumption
from qtros.domain.timekeeping import SessionCalendar
from qtros.engine.backtester import BacktestResult


@dataclass(frozen=True, slots=True)
class PerformanceReport:
    """Everything Layer 5 says about one run.

    Attributes:
        strategy_id: Strategy that ran.
        strategy_version: Its version.
        reporting_id: Rulebook that scored it.
        trades: Trade statistics.
        sharpe: Sharpe result with its convention.
        drawdown: Mark-to-market drawdown.
        concentration: Both concentration tests.
        attribution: PnL and friction attribution.
        gates: Gate verdicts.
        daily_points: Daily equity used for returns.
        trusted: Whether the run reconciled.
        scored_at_utc: When the scoring ran.
    """

    strategy_id: str
    strategy_version: str
    reporting_id: str
    trades: TradeStatistics
    sharpe: SharpeResult
    drawdown: DrawdownResult
    concentration: ConcentrationResult
    attribution: AttributionReport
    gates: GateReport
    daily_points: int
    trusted: bool
    scored_at_utc: datetime


def watermarks_for(result: BacktestResult, resolved: ResolvedBacktestConfig) -> tuple[str, ...]:
    """Return every condition that disqualifies a run from publication."""
    marks: list[str] = []
    if not result.trusted:
        marks.append(WATERMARK_UNTRUSTED)
    execution = resolved.backtest.execution
    if execution.timing is ExecutionTiming.SAME_BAR_CLOSE:
        marks.append(WATERMARK_SAME_BAR_CLOSE)
    if execution.path_assumption is PathAssumption.FAVOURABLE_FIRST:
        marks.append(WATERMARK_FAVOURABLE_FIRST)
    if result.halted_at_end:
        marks.append(WATERMARK_HALTED)
    return tuple(marks)


def analyze(
    result: BacktestResult,
    resolved: ResolvedBacktestConfig,
    calendar: SessionCalendar,
    *,
    scored_at_utc: datetime | None = None,
) -> PerformanceReport:
    """Score a run under its reporting configuration.

    Args:
        result: The run.
        resolved: Its configuration; only ``reporting`` and ``strategy.significance``
            are read.
        calendar: For trading-day attribution.
        scored_at_utc: Override the scoring timestamp, for reproducible artifacts.

    Returns:
        The report.
    """
    reporting = resolved.reporting
    stats = trade_statistics(result.trades)
    points = daily_equity(result.equity_curve, calendar, reporting.sharpe.return_basis)
    sharpe = sharpe_ratio(periodic_returns(points, reporting.sharpe.return_type), reporting.sharpe)
    dd = drawdown(result.equity_curve)
    conc = concentration(result.trades, reporting.concentration, calendar)
    attr = attribute(result.trades, result.fills, calendar, reporting.concentration.trade_period_attribution)
    gates = evaluate_gates(
        stats=stats,
        sharpe=sharpe,
        drawdown=dd,
        concentration=conc,
        reporting=reporting,
        significance=resolved.strategy.significance,
        watermarks=watermarks_for(result, resolved),
    )
    return PerformanceReport(
        strategy_id=result.strategy_id,
        strategy_version=result.strategy_version,
        reporting_id=reporting.reporting_id,
        trades=stats,
        sharpe=sharpe,
        drawdown=dd,
        concentration=conc,
        attribution=attr,
        gates=gates,
        daily_points=len(points),
        trusted=result.trusted,
        scored_at_utc=scored_at_utc or datetime.now(UTC),
    )


__all__ = [
    "AttributionReport",
    "ConcentrationResult",
    "DailyPoint",
    "DrawdownResult",
    "GateCheck",
    "GateReport",
    "PerformanceReport",
    "SharpeResult",
    "TradeStatistics",
    "analyze",
    "attribute",
    "concentration",
    "daily_equity",
    "drawdown",
    "evaluate_gates",
    "periodic_returns",
    "sharpe_ratio",
    "trade_statistics",
    "watermarks_for",
]
