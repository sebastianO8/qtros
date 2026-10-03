"""The two named concentration tests (ARCH-001 C-3; ASS-010).

Both ask the same question at different horizons: what share of the profit came from the
top *N* trades? The quarterly form is a **gate**; the lifetime form is a **kill criterion**
that requires a written ``mechanism_note`` before the strategy may pass.

Trades are the unit (C-4), so splitting one large winner across several partial exits
cannot reduce measured concentration — the exits are one trade.

Definition (ASS-010): the denominator is **gross profit** — the sum of winning trades' net
PnL — and the numerator is the sum of the *N* largest winners. A period with no gross
profit is not evaluable rather than trivially passing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from qtros.analytics.metrics import HUNDRED, ZERO, quantize_metric
from qtros.configuration.models.reporting import ConcentrationConfig
from qtros.domain.enums import PeriodAttribution
from qtros.domain.execution import Trade
from qtros.domain.timekeeping import SessionCalendar

LIFETIME: Final[str] = "lifetime"


def quarter_key(day: date) -> str:
    """Return a sortable quarter label such as ``2024Q3``."""
    return f"{day.year}Q{(day.month - 1) // 3 + 1}"


def attribution_day(trade: Trade, attribution: PeriodAttribution, calendar: SessionCalendar) -> date:
    """Return the trading day a trade is attributed to.

    Args:
        trade: A closed trade.
        attribution: Close-date or open-date rule.
        calendar: Supplies the trading-day boundary.

    Returns:
        The trading day. A trade still open at end attributes to its open date.
    """
    if attribution is PeriodAttribution.CLOSE_DATE and trade.closed_at_utc is not None:
        return calendar.trading_day_for(trade.closed_at_utc)
    return calendar.trading_day_for(trade.opened_at_utc)


@dataclass(frozen=True, slots=True)
class PeriodConcentration:
    """Concentration within one period.

    Attributes:
        period: Quarter label or ``lifetime``.
        trade_count: Closed trades in the period.
        winner_count: Winning trades in the period.
        gross_profit: Sum of winners' net PnL.
        top_n_profit: Sum of the top *N* winners' net PnL.
        top_n_share_pct: ``top_n_profit / gross_profit × 100``, or ``None``.
        threshold_pct: The threshold applied.
        breached: Whether the share exceeded the threshold; ``None`` if not evaluable.
        top_trade_ids: The trades that make up the numerator.
    """

    period: str
    trade_count: int
    winner_count: int
    gross_profit: Decimal
    top_n_profit: Decimal
    top_n_share_pct: Decimal | None
    threshold_pct: Decimal
    breached: bool | None
    top_trade_ids: tuple[str, ...]

    @property
    def evaluable(self) -> bool:
        """Return whether the period had any gross profit to concentrate."""
        return self.top_n_share_pct is not None


@dataclass(frozen=True, slots=True)
class ConcentrationResult:
    """Both tests, with the parameters that produced them.

    Attributes:
        quarterly: One entry per quarter with at least one closed trade.
        lifetime: The whole-run test.
        top_n: Trades summed in the numerator.
        attribution: Period attribution rule used.
        boundary_is_inclusive: Whether exactly-at-threshold breaches.
    """

    quarterly: tuple[PeriodConcentration, ...]
    lifetime: PeriodConcentration
    top_n: int
    attribution: PeriodAttribution
    boundary_is_inclusive: bool

    @property
    def quarterly_breaches(self) -> tuple[str, ...]:
        """Return the quarters that breached the gate."""
        return tuple(q.period for q in self.quarterly if q.breached)

    @property
    def lifetime_breached(self) -> bool:
        """Return whether the kill criterion fired."""
        return bool(self.lifetime.breached)


def _evaluate(
    period: str,
    trades: Sequence[Trade],
    *,
    top_n: int,
    threshold_pct: Decimal,
    inclusive: bool,
) -> PeriodConcentration:
    """Evaluate one period."""
    winners = sorted((t for t in trades if t.net_pnl > ZERO), key=lambda t: (-t.net_pnl, str(t.trade_id)))
    gross = sum((t.net_pnl for t in winners), ZERO)
    top = winners[:top_n]
    top_profit = sum((t.net_pnl for t in top), ZERO)
    if gross <= ZERO:
        share: Decimal | None = None
        breached: bool | None = None
    else:
        share = quantize_metric(top_profit / gross * HUNDRED)
        breached = share >= threshold_pct if inclusive else share > threshold_pct
    return PeriodConcentration(
        period=period,
        trade_count=len(trades),
        winner_count=len(winners),
        gross_profit=gross,
        top_n_profit=top_profit,
        top_n_share_pct=share,
        threshold_pct=threshold_pct,
        breached=breached,
        top_trade_ids=tuple(str(t.trade_id) for t in top),
    )


def concentration(
    trades: Sequence[Trade],
    config: ConcentrationConfig,
    calendar: SessionCalendar,
) -> ConcentrationResult:
    """Run both concentration tests over the closed trades.

    Args:
        trades: Every trade; open-at-end trades are excluded.
        config: Top-N, thresholds, boundary rule, and attribution.
        calendar: Supplies trading-day attribution.

    Returns:
        Quarterly and lifetime results.
    """
    closed = [t for t in trades if not t.is_open_at_backtest_end]
    by_quarter: dict[str, list[Trade]] = {}
    for trade in closed:
        key = quarter_key(attribution_day(trade, config.trade_period_attribution, calendar))
        by_quarter.setdefault(key, []).append(trade)

    quarterly = tuple(
        _evaluate(
            key, by_quarter[key],
            top_n=config.top_n_trades,
            threshold_pct=config.quarterly_threshold_pct,
            inclusive=config.boundary_is_inclusive,
        )
        for key in sorted(by_quarter)
    )
    lifetime = _evaluate(
        LIFETIME, closed,
        top_n=config.top_n_trades,
        threshold_pct=config.lifetime_threshold_pct,
        inclusive=config.boundary_is_inclusive,
    )
    return ConcentrationResult(
        quarterly=quarterly,
        lifetime=lifetime,
        top_n=config.top_n_trades,
        attribution=config.trade_period_attribution,
        boundary_is_inclusive=config.boundary_is_inclusive,
    )
