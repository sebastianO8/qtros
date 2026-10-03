"""Performance metrics from a run (ARCH-001 C-1, C-5).

Everything here is computed from the run's own artifacts — the trade list and the equity
curve — in :class:`decimal.Decimal`, so a reported figure can be recomputed from a stored
ledger and compared exactly. The Sharpe convention is C-5: daily closed-equity returns,
arithmetic, 252 periods, risk-free zero, stamped into the report so that two results
scored under different conventions cannot be mistaken for comparable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final

from qtros.configuration.models.reporting import SharpeConventionConfig
from qtros.domain.enums import ReturnBasis, ReturnType
from qtros.domain.execution import EquitySnapshot, Trade
from qtros.domain.money import quantize_money
from qtros.domain.timekeeping import SessionCalendar

ZERO: Final[Decimal] = Decimal("0")
ONE: Final[Decimal] = Decimal("1")
HUNDRED: Final[Decimal] = Decimal("100")
METRIC_EXPONENT: Final[Decimal] = Decimal("0.000001")
"""Precision reported for ratios such as Sharpe, win rate, and profit factor."""


def quantize_metric(value: Decimal) -> Decimal:
    """Round a ratio to the reporting precision."""
    return value.quantize(METRIC_EXPONENT)


# --------------------------------------------------------------------------------------
# Trade statistics
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TradeStatistics:
    """Counts and sums over closed trades.

    Attributes:
        trade_count: Closed trades (open-at-end excluded, per ASS-005).
        winners: Trades with positive net PnL.
        losers: Trades with negative net PnL.
        scratches: Trades with exactly zero net PnL.
        win_rate: ``winners / trade_count``, or ``None`` with no trades.
        gross_profit: Sum of winning trades' net PnL.
        gross_loss: Sum of losing trades' net PnL, as a positive figure.
        net_pnl: Sum of every closed trade's net PnL.
        profit_factor: ``gross_profit / gross_loss``, or ``None`` if there were no losses.
        expectancy: Mean net PnL per trade, or ``None``.
        average_win: Mean net PnL of winners, or ``None``.
        average_loss: Mean net PnL of losers (positive), or ``None``.
        largest_win: Best single trade.
        largest_loss: Worst single trade, as a positive figure.
        total_costs: Friction across all closed trades.
        average_bars_held: Mean holding period, or ``None``.
        open_at_end: Whether a position was open when the run ended.
    """

    trade_count: int
    winners: int
    losers: int
    scratches: int
    win_rate: Decimal | None
    gross_profit: Decimal
    gross_loss: Decimal
    net_pnl: Decimal
    profit_factor: Decimal | None
    expectancy: Decimal | None
    average_win: Decimal | None
    average_loss: Decimal | None
    largest_win: Decimal
    largest_loss: Decimal
    total_costs: Decimal
    average_bars_held: Decimal | None
    open_at_end: bool


def trade_statistics(trades: Sequence[Trade]) -> TradeStatistics:
    """Summarise closed trades.

    Args:
        trades: Every trade the run produced, including one flagged open at end.

    Returns:
        The statistics over closed trades only.
    """
    closed: list[Trade] = [t for t in trades if not t.is_open_at_backtest_end]
    wins: list[Decimal] = [t.net_pnl for t in closed if t.net_pnl > ZERO]
    losses: list[Decimal] = [-t.net_pnl for t in closed if t.net_pnl < ZERO]
    count: int = len(closed)
    gross_profit: Decimal = sum(wins, ZERO)
    gross_loss: Decimal = sum(losses, ZERO)
    net: Decimal = sum((t.net_pnl for t in closed), ZERO)

    return TradeStatistics(
        trade_count=count,
        winners=len(wins),
        losers=len(losses),
        scratches=count - len(wins) - len(losses),
        win_rate=quantize_metric(Decimal(len(wins)) / count) if count else None,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        net_pnl=net,
        profit_factor=quantize_metric(gross_profit / gross_loss) if gross_loss > ZERO else None,
        expectancy=quantize_money(net / count) if count else None,
        average_win=quantize_money(gross_profit / len(wins)) if wins else None,
        average_loss=quantize_money(gross_loss / len(losses)) if losses else None,
        largest_win=max(wins, default=ZERO),
        largest_loss=max(losses, default=ZERO),
        total_costs=sum((t.total_costs for t in closed), ZERO),
        average_bars_held=(
            quantize_metric(Decimal(sum(t.bars_held for t in closed)) / count) if count else None
        ),
        open_at_end=any(t.is_open_at_backtest_end for t in trades),
    )


# --------------------------------------------------------------------------------------
# Daily returns and Sharpe (C-5)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DailyPoint:
    """End-of-trading-day equity.

    Attributes:
        trading_day: Per the instrument's rollover rule, not the UTC date.
        equity: Equity on the configured basis at the day's last bar.
        ts_utc: Timestamp of that bar.
    """

    trading_day: date
    equity: Decimal
    ts_utc: datetime


def daily_equity(
    curve: Sequence[EquitySnapshot],
    calendar: SessionCalendar,
    basis: ReturnBasis,
) -> tuple[DailyPoint, ...]:
    """Collapse a per-bar equity curve to one point per trading day.

    Args:
        curve: Snapshots in chronological order.
        calendar: Supplies the trading-day boundary.
        basis: Closed or mark-to-market equity.

    Returns:
        The last snapshot of each trading day. Days with no bars are absent, which is
        what "exclude non-trading days" means here.
    """
    points: list[DailyPoint] = []
    for snapshot in curve:
        day = calendar.trading_day_for(snapshot.ts_utc)
        equity = snapshot.closed_equity if basis is ReturnBasis.DAILY_CLOSED_EQUITY else snapshot.mtm_equity
        point = DailyPoint(day, equity, snapshot.ts_utc)
        if points and points[-1].trading_day == day:
            points[-1] = point
        else:
            points.append(point)
    return tuple(points)


def periodic_returns(points: Sequence[DailyPoint], return_type: ReturnType) -> tuple[Decimal, ...]:
    """Return period-over-period returns from daily equity points.

    Args:
        points: Daily equity, chronological.
        return_type: Arithmetic ``E_t / E_{t-1} - 1`` or log ``ln(E_t / E_{t-1})``.

    Returns:
        One return per consecutive pair. Empty with fewer than two points.
    """
    out: list[Decimal] = []
    for previous, current in zip(points, points[1:], strict=False):
        if previous.equity <= ZERO:
            continue
        ratio = current.equity / previous.equity
        out.append(ratio.ln() if return_type is ReturnType.LOG else ratio - ONE)
    return tuple(out)


@dataclass(frozen=True, slots=True)
class SharpeResult:
    """An annualised Sharpe ratio with the convention that produced it.

    Attributes:
        sharpe: The ratio, or ``None`` when undefined.
        periods: Returns used.
        mean_return: Mean excess return per period.
        std_return: Sample standard deviation of excess returns.
        annualization_factor: Periods per year.
        risk_free_rate: Annual rate subtracted.
        return_basis: Equity series used.
        return_type: Return arithmetic used.
        reason: Why the ratio is undefined, when it is.
    """

    sharpe: Decimal | None
    periods: int
    mean_return: Decimal | None
    std_return: Decimal | None
    annualization_factor: int
    risk_free_rate: Decimal
    return_basis: ReturnBasis
    return_type: ReturnType
    reason: str = ""


def sharpe_ratio(returns: Sequence[Decimal], convention: SharpeConventionConfig) -> SharpeResult:
    """Compute an annualised Sharpe ratio under a stated convention (C-5).

    Args:
        returns: Periodic returns.
        convention: Annualisation, risk-free rate, and basis labels.

    Returns:
        The result. Undefined with fewer than two returns or zero dispersion.
    """
    factor = Decimal(convention.annualization_factor)
    per_period_rf = convention.risk_free_rate / factor
    excess = [r - per_period_rf for r in returns]
    n = len(excess)
    if n < 2:
        return SharpeResult(
            None, n, None, None, convention.annualization_factor, convention.risk_free_rate,
            convention.return_basis, convention.return_type, reason="fewer than two periods",
        )
    mean = sum(excess, ZERO) / n
    variance = sum(((r - mean) ** 2 for r in excess), ZERO) / (n - 1)
    if variance <= ZERO:
        return SharpeResult(
            None, n, quantize_metric(mean), ZERO, convention.annualization_factor,
            convention.risk_free_rate, convention.return_basis, convention.return_type,
            reason="zero return dispersion",
        )
    std = variance.sqrt()
    sharpe = (mean / std) * factor.sqrt()
    return SharpeResult(
        quantize_metric(sharpe), n, quantize_metric(mean), quantize_metric(std),
        convention.annualization_factor, convention.risk_free_rate,
        convention.return_basis, convention.return_type,
    )


# --------------------------------------------------------------------------------------
# Drawdown (C-1: mark-to-market basis)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DrawdownResult:
    """Maximum peak-to-trough decline on the mark-to-market curve.

    Attributes:
        peak_equity: Equity at the peak preceding the worst trough.
        trough_equity: Equity at the worst trough.
        max_drawdown: ``peak - trough``.
        max_drawdown_pct: As a percentage of that peak.
        peak_ts_utc: When the peak occurred.
        trough_ts_utc: When the trough occurred.
        bars: Snapshots examined.
    """

    peak_equity: Decimal
    trough_equity: Decimal
    max_drawdown: Decimal
    max_drawdown_pct: Decimal
    peak_ts_utc: datetime | None
    trough_ts_utc: datetime | None
    bars: int


def drawdown(curve: Sequence[EquitySnapshot]) -> DrawdownResult:
    """Return the worst drawdown on the mark-to-market basis.

    Args:
        curve: Snapshots in chronological order.

    Returns:
        The result. Zero everywhere for an empty curve.
    """
    if not curve:
        return DrawdownResult(ZERO, ZERO, ZERO, ZERO, None, None, 0)

    peak = curve[0]
    worst_peak, worst_trough = curve[0], curve[0]
    worst_pct = ZERO
    for snapshot in curve:
        if snapshot.mtm_equity > peak.mtm_equity:
            peak = snapshot
        if peak.mtm_equity > ZERO:
            pct = (peak.mtm_equity - snapshot.mtm_equity) / peak.mtm_equity * HUNDRED
            if pct > worst_pct:
                worst_pct, worst_peak, worst_trough = pct, peak, snapshot

    return DrawdownResult(
        peak_equity=worst_peak.mtm_equity,
        trough_equity=worst_trough.mtm_equity,
        max_drawdown=quantize_money(worst_peak.mtm_equity - worst_trough.mtm_equity),
        max_drawdown_pct=quantize_metric(worst_pct),
        peak_ts_utc=worst_peak.ts_utc,
        trough_ts_utc=worst_trough.ts_utc,
        bars=len(curve),
    )
