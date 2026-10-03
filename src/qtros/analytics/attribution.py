"""PnL attribution (Directive 2 friction itemisation; Directive 5 mechanism tagging).

Every bucket is computed from trades or fills the run already produced. Nothing here is a
new measurement; it is the same ledger sliced along the axes that explain a result —
mechanism, exit reason, direction, quarter — plus the friction breakdown that says *which*
cost killed an edge.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from qtros.analytics.concentration import attribution_day, quarter_key
from qtros.analytics.metrics import HUNDRED, ZERO, quantize_metric
from qtros.domain.enums import PeriodAttribution
from qtros.domain.execution import ExecutionFill, Trade
from qtros.domain.money import quantize_money
from qtros.domain.timekeeping import SessionCalendar


@dataclass(frozen=True, slots=True)
class Bucket:
    """Aggregate over the trades sharing one key.

    Attributes:
        key: Bucket label.
        trade_count: Closed trades in the bucket.
        winners: Winning trades.
        net_pnl: Sum of net PnL.
        gross_pnl: Sum of gross PnL.
        total_costs: Sum of friction.
        win_rate: ``winners / trade_count``.
    """

    key: str
    trade_count: int
    winners: int
    net_pnl: Decimal
    gross_pnl: Decimal
    total_costs: Decimal
    win_rate: Decimal | None


def bucketize(trades: Sequence[Trade], key_of: Callable[[Trade], str]) -> tuple[Bucket, ...]:
    """Group closed trades by a key function.

    Args:
        trades: Trades to group; open-at-end trades are excluded.
        key_of: Label for a trade.

    Returns:
        Buckets sorted by key.
    """
    groups: dict[str, list[Trade]] = {}
    for trade in trades:
        if trade.is_open_at_backtest_end:
            continue
        groups.setdefault(key_of(trade), []).append(trade)
    out: list[Bucket] = []
    for key in sorted(groups):
        members = groups[key]
        winners = sum(1 for t in members if t.net_pnl > ZERO)
        out.append(
            Bucket(
                key=key,
                trade_count=len(members),
                winners=winners,
                net_pnl=sum((t.net_pnl for t in members), ZERO),
                gross_pnl=sum((t.gross_pnl for t in members), ZERO),
                total_costs=sum((t.total_costs for t in members), ZERO),
                win_rate=quantize_metric(Decimal(winners) / len(members)) if members else None,
            )
        )
    return tuple(out)


@dataclass(frozen=True, slots=True)
class FrictionAttribution:
    """Friction itemised across every fill (Directive 2).

    Attributes:
        commission: Total commission.
        spread: Total spread cost.
        slippage: Total slippage cost.
        total: Sum of the three.
        fill_count: Fills charged.
        per_fill: Mean cost per fill.
        pct_of_gross_pnl: Friction as a share of absolute gross PnL, or ``None``.
    """

    commission: Decimal
    spread: Decimal
    slippage: Decimal
    total: Decimal
    fill_count: int
    per_fill: Decimal | None
    pct_of_gross_pnl: Decimal | None


def friction_attribution(fills: Sequence[ExecutionFill], trades: Sequence[Trade]) -> FrictionAttribution:
    """Itemise friction over the ledger.

    Args:
        fills: The complete fill ledger.
        trades: Trades, for the gross PnL denominator.

    Returns:
        The breakdown.
    """
    commission = sum((f.commission for f in fills), ZERO)
    spread = sum((f.spread_cost for f in fills), ZERO)
    slippage = sum((f.slippage_cost for f in fills), ZERO)
    total = commission + spread + slippage
    gross_abs = sum((abs(t.gross_pnl) for t in trades if not t.is_open_at_backtest_end), ZERO)
    return FrictionAttribution(
        commission=commission,
        spread=spread,
        slippage=slippage,
        total=total,
        fill_count=len(fills),
        per_fill=quantize_money(total / len(fills)) if fills else None,
        pct_of_gross_pnl=quantize_metric(total / gross_abs * HUNDRED) if gross_abs > ZERO else None,
    )


@dataclass(frozen=True, slots=True)
class AttributionReport:
    """Every attribution axis for one run.

    Attributes:
        by_mechanism: Keyed by ``mechanism_tag`` (Directive 5).
        by_exit_reason: Keyed by the final exit reason.
        by_direction: Long versus short.
        by_quarter: Keyed by quarter under the configured period attribution.
        friction: Itemised friction.
        attribution: Period attribution rule used for ``by_quarter``.
    """

    by_mechanism: tuple[Bucket, ...]
    by_exit_reason: tuple[Bucket, ...]
    by_direction: tuple[Bucket, ...]
    by_quarter: tuple[Bucket, ...]
    friction: FrictionAttribution
    attribution: PeriodAttribution


def attribute(
    trades: Sequence[Trade],
    fills: Sequence[ExecutionFill],
    calendar: SessionCalendar,
    attribution: PeriodAttribution,
) -> AttributionReport:
    """Slice a run's ledger along every attribution axis.

    Args:
        trades: Every trade.
        fills: Every fill.
        calendar: For quarter attribution by trading day.
        attribution: Close-date or open-date rule.

    Returns:
        The report.
    """
    return AttributionReport(
        by_mechanism=bucketize(trades, lambda t: t.mechanism_tag or "(untagged)"),
        by_exit_reason=bucketize(trades, lambda t: str(t.exit_reason) if t.exit_reason else "(none)"),
        by_direction=bucketize(trades, lambda t: str(t.direction)),
        by_quarter=bucketize(trades, lambda t: quarter_key(attribution_day(t, attribution, calendar))),
        friction=friction_attribution(fills, trades),
        attribution=attribution,
    )
