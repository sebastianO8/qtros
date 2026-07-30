"""Execution and accounting types.

Implements ARCH-001 C-4's two-level model, which exists because "trade" was undefined
under partial exits and both Directive 7 (trade count) and Directive 8 (top five trades)
depend on the answer:

* **:class:`ExecutionFill`** — an atomic execution event. Fills are the only things that
  move cash. There is no other code path by which the ledger changes, which is why the
  Cycle 1 partial-exit bug has no route to recur.
* **:class:`Trade`** — one complete position lifecycle, flat to flat, composed of an
  ordered tuple of fills. Exactly one realised PnL.

Reconciliation sums fills. Metrics count trades. Splitting one large winner across three
exit rows therefore cannot reduce measured concentration, because concentration is
computed over trades and the three exits are one trade.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Final

from qtros.domain.enums import Direction, ExitReason
from qtros.domain.identifiers import FillId, InstrumentId, TradeId
from qtros.domain.money import quantize_money

ZERO: Final[Decimal] = Decimal("0")


@dataclass(frozen=True, slots=True)
class ExecutionFill:
    """One atomic execution event.

    A fill is created unbooked by the broker, which knows the price and the costs but not
    the position's cost basis. The portfolio books it, computing ``realized_pnl`` and
    ``cash_delta``. Only booked fills enter the ledger.

    Attributes:
        fill_id: Deterministic identifier, sequential within a run. Deterministic rather
            than random so that two runs of the same configuration produce identical
            fill and trade identifiers, which is what makes the Directive 10 claim of
            reproducibility checkable rather than rhetorical.
        order_id: Order that produced this fill.
        trade_id: Position lifecycle this fill belongs to.
        instrument_id: Instrument transacted.
        ts_utc: Execution instant.
        direction: Side of this fill, not of the position.
        quantity: Lots transacted. Always positive.
        reference_price: Price before spread and slippage — normally the execution bar's
            open. Retained so friction is attributable rather than merely deducted.
        fill_price: Price actually paid or received.
        commission: Commission charged.
        spread_cost: Cost of crossing the spread.
        slippage_cost: Additional adverse price impact.
        is_entry: Whether this fill opened or increased exposure.
        is_partial_exit: Whether this fill reduced but did not close a position.
        exit_reason: Why the position was reduced, for exit fills.
        realized_pnl: PnL crystallised by this fill. Zero for entries. ``None`` until the
            portfolio books it.
        cash_delta: Net cash movement. ``None`` until booked. The reconciliation identity
            is per-fill and exact: ``cash_delta == realized_pnl - cash_costs``, where
            cash costs are commission only — spread and slippage are already embedded
            in ``fill_price``.
    """

    fill_id: FillId
    order_id: str
    trade_id: TradeId
    instrument_id: InstrumentId
    ts_utc: datetime
    direction: Direction
    quantity: Decimal
    reference_price: Decimal
    fill_price: Decimal
    commission: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    is_entry: bool
    is_partial_exit: bool = False
    exit_reason: ExitReason | None = None
    realized_pnl: Decimal | None = None
    cash_delta: Decimal | None = None

    @property
    def implicit_costs(self) -> Decimal:
        """Return friction already embedded in the fill price.

        Spread and slippage are not debited from the balance; they are paid by receiving a
        worse price, and that worse price is already inside ``realized_pnl``. They are
        reported here for Directive 2 attribution and are deliberately excluded from the
        cash identity, because deducting them again would charge them twice — once in the
        price and once in the ledger.
        """
        return self.spread_cost + self.slippage_cost

    @property
    def cash_costs(self) -> Decimal:
        """Return friction charged as a separate cash debit.

        Commission only. This is the figure the cash identity uses.
        """
        return self.commission

    @property
    def total_costs(self) -> Decimal:
        """Return every friction component, implicit and explicit.

        Used for reporting and for a trade's total cost line. Not used in the cash
        identity; see :attr:`cash_costs`.
        """
        return self.implicit_costs + self.commission

    @property
    def is_booked(self) -> bool:
        """Return whether the portfolio has assigned this fill a cash movement."""
        return self.cash_delta is not None and self.realized_pnl is not None

    def assert_internally_consistent(self) -> None:
        """Verify this fill's own accounting identity.

        Raises:
            ValueError: If the fill is unbooked, or if ``cash_delta`` does not equal
                ``realized_pnl - cash_costs`` exactly. Under Decimal arithmetic the
                comparison is exact, so any discrepancy is a logic error rather than a
                possible rounding artefact.

        Note:
            The identity uses :attr:`cash_costs`, not :attr:`total_costs`. Spread and
            slippage are already reflected in ``fill_price`` and therefore in
            ``realized_pnl``; subtracting them again double-charges them. This distinction
            was surfaced by the buy-and-hold benchmark, which is precisely the class of
            error a known-truth test exists to catch.
        """
        if not self.is_booked:
            raise ValueError(f"Fill {self.fill_id} is not booked")
        expected: Decimal = (self.realized_pnl or ZERO) - self.cash_costs
        if self.cash_delta != expected:
            raise ValueError(
                f"Fill {self.fill_id} violates its accounting identity: "
                f"cash_delta={self.cash_delta} but realized_pnl - cash_costs = {expected}"
            )


@dataclass(frozen=True, slots=True)
class PositionState:
    """An open position, flat to flat.

    Attributes:
        trade_id: Lifecycle identifier shared with every fill in this position.
        instrument_id: Instrument held.
        direction: Side held.
        quantity: Open lots. Always positive.
        avg_entry_price: Weighted average price of the open portion.
        opened_at_utc: First entry fill's timestamp.
        current_stop: Active protective stop, if any.
        current_target: Active protective target, if any.
        realized_pnl: PnL crystallised by partial exits so far.
        costs_paid: Friction charged so far across this lifecycle.
        max_adverse_excursion: Worst unrealised loss seen while open. Recorded because a
            trade that won after a large adverse excursion is a different trade from one
            that won cleanly, and only one of them survives a tighter stop.
        max_favorable_excursion: Best unrealised gain seen while open.
        entry_fill_count: Entry fills so far.
        exit_fill_count: Exit fills so far.
        mechanism_tag: Mechanism claimed by the opening intent.
    """

    trade_id: TradeId
    instrument_id: InstrumentId
    direction: Direction
    quantity: Decimal
    avg_entry_price: Decimal
    opened_at_utc: datetime
    current_stop: Decimal | None = None
    current_target: Decimal | None = None
    realized_pnl: Decimal = ZERO
    costs_paid: Decimal = ZERO
    max_adverse_excursion: Decimal = ZERO
    max_favorable_excursion: Decimal = ZERO
    entry_fill_count: int = 0
    exit_fill_count: int = 0
    mechanism_tag: str = ""

    @property
    def is_flat(self) -> bool:
        """Return whether the position holds no open exposure."""
        return self.quantity <= ZERO

    def unrealized_pnl(self, mark_price: Decimal, contract_size: Decimal) -> Decimal:
        """Return open PnL at a given mark price.

        Args:
            mark_price: Price to mark against, normally the current bar's close.
            contract_size: Units of the base per lot.

        Returns:
            Unrealised PnL in the quote currency.
        """
        move: Decimal = (mark_price - self.avg_entry_price) * Decimal(self.direction.sign)
        return quantize_money(move * self.quantity * contract_size)


@dataclass(frozen=True, slots=True)
class Trade:
    """One complete position lifecycle. The unit every metric counts.

    Attributes:
        trade_id: Lifecycle identifier.
        instrument_id: Instrument traded.
        direction: Side held.
        fills: Every fill in this lifecycle, in execution order.
        opened_at_utc: First entry fill's timestamp.
        closed_at_utc: Final exit fill's timestamp. ``None`` while still open.
        entry_quantity: Total lots opened.
        avg_entry_price: Size-weighted entry price.
        avg_exit_price: Size-weighted exit price.
        gross_pnl: PnL before friction.
        total_costs: All friction across the lifecycle.
        net_pnl: PnL after friction. The figure every metric uses.
        bars_held: Bars between first entry and final exit.
        max_adverse_excursion: Worst unrealised loss during the lifecycle.
        max_favorable_excursion: Best unrealised gain during the lifecycle.
        exit_reason: Reason recorded on the final exit fill.
        mechanism_tag: Mechanism claimed by the opening intent.
        is_open_at_backtest_end: Whether the run ended with this position still open.
            Such trades are excluded from performance metrics and reported separately —
            an unclosed position silently carrying its mark-to-market into the results is
            a classic and invisible PnL leak.
    """

    trade_id: TradeId
    instrument_id: InstrumentId
    direction: Direction
    fills: tuple[ExecutionFill, ...]
    opened_at_utc: datetime
    closed_at_utc: datetime | None
    entry_quantity: Decimal
    avg_entry_price: Decimal
    avg_exit_price: Decimal | None
    gross_pnl: Decimal
    total_costs: Decimal
    net_pnl: Decimal
    bars_held: int
    max_adverse_excursion: Decimal
    max_favorable_excursion: Decimal
    exit_reason: ExitReason | None
    mechanism_tag: str
    is_open_at_backtest_end: bool = False

    @property
    def fill_count(self) -> int:
        """Return the number of fills composing this trade."""
        return len(self.fills)

    @property
    def is_winner(self) -> bool:
        """Return whether the trade closed profitably after friction."""
        return self.net_pnl > ZERO

    @property
    def partial_exit_count(self) -> int:
        """Return how many exits were partial.

        Useful when reading concentration results: a trade with several partial exits is
        one trade, and this is the number that explains why its fill count is high.
        """
        return sum(1 for fill in self.fills if fill.is_partial_exit)


@dataclass(frozen=True, slots=True)
class EquitySnapshot:
    """Account equity at one instant, on both bases (ARCH-001 C-1).

    Attributes:
        ts_utc: Instant of the snapshot.
        cash: Settled cash.
        closed_equity: Equity counting only closed positions.
        mtm_equity: Equity including open-position mark-to-market. Every drawdown gate
            evaluates against this, because prop firms measure their loss limits on
            intraday mark-to-market and a strategy can pass a closed-equity gate while
            breaching the firm's.
        unrealized_pnl: Open PnL at this instant.
        open_position_count: Positions open at this instant.
    """

    ts_utc: datetime
    cash: Decimal
    closed_equity: Decimal
    mtm_equity: Decimal
    unrealized_pnl: Decimal
    open_position_count: int


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """Directive 6: the sum of logged fill cash flows must equal the equity change.

    Attributes:
        passed: Whether the identity held exactly.
        sum_fill_cash_flows: Total of every booked fill's ``cash_delta``.
        final_equity_delta: Closing equity minus starting equity.
        discrepancy: Difference between the two. Required to be exactly zero.
        fill_count: Fills examined.
        trade_count: Trades assembled.
        orphaned_fills: Fills with no parent trade.
        unbooked_fills: Fills the portfolio never booked.
        open_trades_at_end: Trades still open when the run finished.
        checked_at_utc: When the check ran.
    """

    passed: bool
    sum_fill_cash_flows: Decimal
    final_equity_delta: Decimal
    discrepancy: Decimal
    fill_count: int
    trade_count: int
    orphaned_fills: tuple[FillId, ...] = field(default_factory=tuple)
    unbooked_fills: tuple[FillId, ...] = field(default_factory=tuple)
    open_trades_at_end: tuple[TradeId, ...] = field(default_factory=tuple)
    checked_at_utc: datetime | None = None

    def summary(self) -> str:
        """Return a one-line verdict for logs and CLI output."""
        if self.passed:
            return (
                f"RECONCILED — {self.fill_count} fills, {self.trade_count} trades, "
                f"delta {self.final_equity_delta}"
            )
        return (
            f"RECONCILIATION FAILED — discrepancy {self.discrepancy} across "
            f"{self.fill_count} fills; this is a blocking bug, not a rounding footnote"
        )


def weighted_average_price(
    fills: Sequence[ExecutionFill],
    *,
    entries: bool,
) -> Decimal | None:
    """Return the size-weighted average price of a fill subset.

    Args:
        fills: Fills to average.
        entries: Average entry fills when ``True``, exit fills when ``False``.

    Returns:
        The weighted average, or ``None`` if the subset is empty or has zero size.
    """
    selected: list[ExecutionFill] = [fill for fill in fills if fill.is_entry is entries]
    total_quantity: Decimal = sum((fill.quantity for fill in selected), ZERO)
    if total_quantity <= ZERO:
        return None
    weighted: Decimal = sum(
        (fill.fill_price * fill.quantity for fill in selected), ZERO
    )
    return weighted / total_quantity
