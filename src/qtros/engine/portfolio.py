"""Position tracking, cash accounting, and trade assembly.

The single invariant this module exists to protect: **cash moves only through a booked
fill.** There is no ``adjust_cash``, no direct balance setter, and no path by which
equity changes without a corresponding row in the fill ledger. Cycle 1's bug — partial
exit PnL added to running equity but never logged as a trade row — is not caught here by
a check; it has no code path through which to occur.

Everything monetary is :class:`decimal.Decimal`, so reconciliation asserts exact equality.
A tolerance would be a place where a small real bug could hide beneath the threshold, and
"the discrepancy is within tolerance" is precisely the sentence that lets one through.

**Margin model.** Opening a position debits friction only, not notional. This is correct
for margin-traded instruments — FX spot, CFDs, futures — which is the whole of Phase 1's
scope. Cash-settled equities, where the full notional leaves the account on purchase,
would need a different cash model, and adding equities without changing this would
overstate available capital for the entire holding period.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

import structlog

from qtros.domain.enums import ExitReason
from qtros.domain.errors import QtrosError
from qtros.domain.execution import (
    EquitySnapshot,
    ExecutionFill,
    PositionState,
    ReconciliationReport,
    Trade,
    weighted_average_price,
)
from qtros.domain.identifiers import FillId, SequentialIdFactory, TradeId
from qtros.domain.instrument import InstrumentSpec
from qtros.domain.market import BarData
from qtros.domain.money import apportion, quantize_money, quantize_price
from qtros.domain.signals import PortfolioView

_LOGGER: Final = structlog.get_logger(__name__)

ZERO: Final[Decimal] = Decimal("0")


class PortfolioError(QtrosError):
    """The portfolio was asked to do something inconsistent with its state."""


@dataclass(frozen=True, slots=True)
class FillOutcome:
    """Result of booking one broker fill.

    Attributes:
        booked_fills: Fills as recorded in the ledger. Usually one. A reversal — a fill
            larger than the open position and opposing it — produces two: the portion
            that closed the old position and the portion that opened the new one. They
            carry different trade identifiers because they belong to different position
            lifecycles, which is what keeps the C-4 definition of a trade intact through
            a flip.
        closed_trade: The trade completed by this fill, if any.
        realized_pnl: PnL crystallised by this fill.
        cash_delta: Net cash movement.
    """

    booked_fills: tuple[ExecutionFill, ...]
    closed_trade: Trade | None
    realized_pnl: Decimal
    cash_delta: Decimal


class Portfolio:
    """Tracks one instrument's position, cash, and completed trades.

    Phase 1 is single-instrument by design: Layer 4 owns cross-instrument allocation and
    is not required until Phase 2. The interface takes an instrument on every call rather
    than assuming one, so the multi-instrument extension does not require rewriting call
    sites.

    Args:
        instrument: Contract specification.
        initial_cash: Starting balance.
        ids: Deterministic identifier factory.

    Raises:
        PortfolioError: If the starting balance is not positive.
    """

    __slots__ = (
        "_cash",
        "_closed_trades",
        "_current_fills",
        "_ids",
        "_initial_cash",
        "_instrument",
        "_ledger",
        "_opened_bar_index",
        "_position",
        "_realized_pnl",
        "_total_costs",
    )

    def __init__(
        self,
        instrument: InstrumentSpec,
        initial_cash: Decimal,
        ids: SequentialIdFactory,
    ) -> None:
        if initial_cash <= ZERO:
            raise PortfolioError("Initial cash must be positive", initial_cash=str(initial_cash))

        self._instrument: InstrumentSpec = instrument
        self._initial_cash: Decimal = initial_cash
        self._cash: Decimal = initial_cash
        self._ids: SequentialIdFactory = ids
        self._position: PositionState | None = None
        self._current_fills: list[ExecutionFill] = []
        self._opened_bar_index: int = 0
        self._closed_trades: list[Trade] = []
        self._ledger: list[ExecutionFill] = []
        self._realized_pnl: Decimal = ZERO
        self._total_costs: Decimal = ZERO

    # -- read-only state ---------------------------------------------------------------

    @property
    def cash(self) -> Decimal:
        """Return settled cash."""
        return self._cash

    @property
    def position(self) -> PositionState | None:
        """Return the open position, if any."""
        return self._position

    @property
    def is_flat(self) -> bool:
        """Return whether no position is open."""
        return self._position is None

    @property
    def closed_trades(self) -> tuple[Trade, ...]:
        """Return every completed trade, in completion order."""
        return tuple(self._closed_trades)

    @property
    def ledger(self) -> tuple[ExecutionFill, ...]:
        """Return every booked fill, in execution order."""
        return tuple(self._ledger)

    @property
    def realized_pnl(self) -> Decimal:
        """Return cumulative realised PnL before friction."""
        return self._realized_pnl

    @property
    def closed_equity(self) -> Decimal:
        """Return equity counting only closed positions.

        Returns:
            Settled cash. Under the margin model, opening a position moves no notional,
            so cash and closed equity are the same figure by construction rather than by
            coincidence.
        """
        return self._cash

    def allocate_trade_id(self) -> TradeId:
        """Return the trade identifier for the next position lifecycle.

        Returns:
            The open position's identifier when one exists, so that adds and partial
            exits join the lifecycle they belong to; a fresh identifier otherwise.
        """
        if self._position is not None:
            return self._position.trade_id
        return self._ids.next_trade_id()

    def view(self, *, mark_price: Decimal | None = None, is_halted: bool = False) -> PortfolioView:
        """Return a read-only projection for a strategy.

        Args:
            mark_price: Price to mark open positions against. When ``None``, the average
                entry price is used, which reports zero unrealised PnL.
            is_halted: Whether the risk engine has halted trading.

        Returns:
            The projection handed to strategies inside :class:`BarContext`.
        """
        unrealized: Decimal = self._unrealized(mark_price)
        return PortfolioView(
            cash=self._cash,
            closed_equity=self._cash,
            mtm_equity=self._cash + unrealized,
            open_position=self._position,
            realized_pnl_to_date=self._realized_pnl,
            trade_count_to_date=len(self._closed_trades),
            is_halted=is_halted,
        )

    # -- fills -------------------------------------------------------------------------

    def apply_fill(self, fill: ExecutionFill, *, bar_index: int) -> FillOutcome:
        """Book a broker fill against the position and the ledger.

        Args:
            fill: Unbooked fill from the broker.
            bar_index: Index of the execution bar, used to compute holding period.

        Returns:
            The booked fills, any completed trade, and the cash effect.

        Raises:
            PortfolioError: If the fill is already booked, names another instrument, or
                carries a non-positive quantity.
        """
        self._validate_fill(fill)

        if self._position is None:
            return self._open_position(fill, bar_index=bar_index)

        if fill.direction is self._position.direction:
            return self._increase_position(fill)

        if fill.quantity <= self._position.quantity:
            return self._reduce_position(fill, bar_index=bar_index)

        return self._reverse_position(fill, bar_index=bar_index)

    def _validate_fill(self, fill: ExecutionFill) -> None:
        """Reject a fill the portfolio cannot legitimately book.

        Args:
            fill: Fill to check.

        Raises:
            PortfolioError: On a double-booking, instrument mismatch, or non-positive
                quantity. A zero-quantity fill is a trade that did not happen being
                recorded as though it did, and it would inflate the trade count that
                Directive 7 gates on.
        """
        if fill.is_booked:
            raise PortfolioError("Fill has already been booked", fill_id=str(fill.fill_id))
        if fill.instrument_id != self._instrument.instrument_id:
            raise PortfolioError(
                "Fill names a different instrument than this portfolio tracks",
                fill_instrument=str(fill.instrument_id),
                portfolio_instrument=str(self._instrument.instrument_id),
            )
        if fill.quantity <= ZERO:
            raise PortfolioError(
                "Fill carries a non-positive quantity",
                fill_id=str(fill.fill_id),
                quantity=str(fill.quantity),
            )

    def _book(
        self,
        fill: ExecutionFill,
        *,
        realized_pnl: Decimal,
        trade_id: TradeId | None = None,
        is_partial_exit: bool = False,
    ) -> ExecutionFill:
        """Attach the cash effect to a fill and record it in the ledger.

        This is the only function in the system that moves cash.

        Args:
            fill: Unbooked fill.
            realized_pnl: PnL crystallised by this fill. Zero for entries.
            trade_id: Override the fill's lifecycle identifier, used when a reversal
                splits one broker fill across two lifecycles.
            is_partial_exit: Whether this exit left the position open.

        Returns:
            The booked fill.
        """
        # Quantized before storage: every ledger value must be bounded-precision, or the
        # running balance and the sum of deltas round differently and Directive 6 fails
        # for a reason that is arithmetic rather than logical (see domain.money).
        realized_pnl = quantize_money(realized_pnl)
        cash_delta: Decimal = realized_pnl - quantize_money(fill.cash_costs)
        booked: ExecutionFill = replace(
            fill,
            trade_id=trade_id if trade_id is not None else fill.trade_id,
            realized_pnl=realized_pnl,
            cash_delta=cash_delta,
            is_partial_exit=is_partial_exit,
        )
        booked.assert_internally_consistent()

        self._cash += cash_delta
        self._realized_pnl += realized_pnl
        self._total_costs += fill.total_costs
        self._ledger.append(booked)
        self._current_fills.append(booked)
        return booked

    def _open_position(self, fill: ExecutionFill, *, bar_index: int) -> FillOutcome:
        """Open a new position from an entry fill.

        Args:
            fill: Entry fill.
            bar_index: Execution bar index.

        Returns:
            The fill outcome.
        """
        self._current_fills = []
        booked: ExecutionFill = self._book(fill, realized_pnl=ZERO)
        self._opened_bar_index = bar_index
        self._position = PositionState(
            trade_id=booked.trade_id,
            instrument_id=self._instrument.instrument_id,
            direction=booked.direction,
            quantity=booked.quantity,
            avg_entry_price=booked.fill_price,
            opened_at_utc=booked.ts_utc,
            costs_paid=booked.total_costs,
            entry_fill_count=1,
        )
        return FillOutcome((booked,), None, ZERO, booked.cash_delta or ZERO)

    def _increase_position(self, fill: ExecutionFill) -> FillOutcome:
        """Add to an existing position, re-weighting the average entry price.

        Args:
            fill: Entry fill in the same direction as the open position.

        Returns:
            The fill outcome.
        """
        position: PositionState = self._require_position()
        booked: ExecutionFill = self._book(fill, realized_pnl=ZERO, trade_id=position.trade_id)

        combined_quantity: Decimal = position.quantity + booked.quantity
        weighted: Decimal = quantize_price(
            (
                position.avg_entry_price * position.quantity
                + booked.fill_price * booked.quantity
            )
            / combined_quantity
        )

        self._position = replace(
            position,
            quantity=combined_quantity,
            avg_entry_price=weighted,
            costs_paid=position.costs_paid + booked.total_costs,
            entry_fill_count=position.entry_fill_count + 1,
        )
        return FillOutcome((booked,), None, ZERO, booked.cash_delta or ZERO)

    def _reduce_position(self, fill: ExecutionFill, *, bar_index: int) -> FillOutcome:
        """Reduce or close a position, realising PnL on the closed portion.

        Args:
            fill: Exit fill opposing the open position, no larger than it.
            bar_index: Execution bar index.

        Returns:
            The fill outcome, including a completed trade when the position went flat.
        """
        position: PositionState = self._require_position()
        realized: Decimal = self._instrument.notional_pnl(
            entry_price=position.avg_entry_price,
            exit_price=fill.fill_price,
            quantity=fill.quantity,
            direction=position.direction,
        )
        closes_position: bool = fill.quantity == position.quantity
        booked: ExecutionFill = self._book(
            fill,
            realized_pnl=realized,
            trade_id=position.trade_id,
            is_partial_exit=not closes_position,
        )

        updated: PositionState = replace(
            position,
            quantity=position.quantity - booked.quantity,
            realized_pnl=position.realized_pnl + realized,
            costs_paid=position.costs_paid + booked.total_costs,
            exit_fill_count=position.exit_fill_count + 1,
        )

        if not closes_position:
            self._position = updated
            return FillOutcome((booked,), None, realized, booked.cash_delta or ZERO)

        trade: Trade = self._assemble_trade(
            updated,
            closed_at=booked.ts_utc,
            bar_index=bar_index,
            exit_reason=booked.exit_reason,
        )
        self._finalize_trade(trade)
        return FillOutcome((booked,), trade, realized, booked.cash_delta or ZERO)

    def _reverse_position(self, fill: ExecutionFill, *, bar_index: int) -> FillOutcome:
        """Close the open position and open an opposing one from a single fill.

        A broker fill larger than the open position and opposing it spans two position
        lifecycles. It is split into two booked fills so that each trade's PnL is
        attributable to its own lifecycle. Treating the reversal as one trade would merge
        two distinct entries into a single record and distort both the trade count and the
        holding-period statistics.

        Args:
            fill: Opposing fill larger than the open position.
            bar_index: Execution bar index.

        Returns:
            The fill outcome, carrying both booked fills and the completed trade.
        """
        position: PositionState = self._require_position()
        closing_quantity: Decimal = position.quantity
        opening_quantity: Decimal = fill.quantity - closing_quantity

        closing_share: Decimal = closing_quantity / fill.quantity
        # Each cost is split so the two halves sum to the original exactly; the opening
        # side takes the remainder rather than being quantized independently.
        closing_commission, opening_commission = apportion(fill.commission, closing_share)
        closing_spread, opening_spread = apportion(fill.spread_cost, closing_share)
        closing_slippage, opening_slippage = apportion(fill.slippage_cost, closing_share)

        closing_fill: ExecutionFill = replace(
            fill,
            fill_id=FillId(f"{fill.fill_id}c"),
            quantity=closing_quantity,
            commission=closing_commission,
            spread_cost=closing_spread,
            slippage_cost=closing_slippage,
            is_entry=False,
            exit_reason=fill.exit_reason or ExitReason.REVERSAL,
        )
        realized: Decimal = self._instrument.notional_pnl(
            entry_price=position.avg_entry_price,
            exit_price=fill.fill_price,
            quantity=closing_quantity,
            direction=position.direction,
        )
        booked_close: ExecutionFill = self._book(
            closing_fill, realized_pnl=realized, trade_id=position.trade_id
        )

        closed_state: PositionState = replace(
            position,
            quantity=ZERO,
            realized_pnl=position.realized_pnl + realized,
            costs_paid=position.costs_paid + booked_close.total_costs,
            exit_fill_count=position.exit_fill_count + 1,
        )
        trade: Trade = self._assemble_trade(
            closed_state,
            closed_at=booked_close.ts_utc,
            bar_index=bar_index,
            exit_reason=ExitReason.REVERSAL,
        )
        self._finalize_trade(trade)

        opening_fill: ExecutionFill = replace(
            fill,
            fill_id=FillId(f"{fill.fill_id}o"),
            trade_id=self._ids.next_trade_id(),
            quantity=opening_quantity,
            commission=opening_commission,
            spread_cost=opening_spread,
            slippage_cost=opening_slippage,
            is_entry=True,
            exit_reason=None,
        )
        outcome: FillOutcome = self._open_position(opening_fill, bar_index=bar_index)

        return FillOutcome(
            booked_fills=(booked_close, *outcome.booked_fills),
            closed_trade=trade,
            realized_pnl=realized,
            cash_delta=(booked_close.cash_delta or ZERO) + outcome.cash_delta,
        )

    # -- marking and trade assembly ----------------------------------------------------

    def mark_to_market(self, bar: BarData) -> EquitySnapshot:
        """Mark the open position against a bar and update excursion statistics.

        Args:
            bar: Bar to mark against. The close is used as the mark price; the high and
                low update the excursion extremes, since maximum adverse excursion is a
                property of the path rather than of the closes.

        Returns:
            Equity on both bases at this bar.
        """
        if self._position is not None:
            self._update_excursions(bar)

        unrealized: Decimal = self._unrealized(bar.close)
        return EquitySnapshot(
            ts_utc=bar.ts_utc,
            cash=self._cash,
            closed_equity=self._cash,
            mtm_equity=self._cash + unrealized,
            unrealized_pnl=unrealized,
            open_position_count=0 if self._position is None else 1,
        )

    def _update_excursions(self, bar: BarData) -> None:
        """Record the best and worst unrealised PnL reached within a bar.

        Args:
            bar: Bar to evaluate.
        """
        position: PositionState = self._require_position()
        contract: Decimal = self._instrument.contract_size

        at_high: Decimal = position.unrealized_pnl(bar.high, contract)
        at_low: Decimal = position.unrealized_pnl(bar.low, contract)
        worst: Decimal = min(at_high, at_low)
        best: Decimal = max(at_high, at_low)

        self._position = replace(
            position,
            max_adverse_excursion=min(position.max_adverse_excursion, worst),
            max_favorable_excursion=max(position.max_favorable_excursion, best),
        )

    def _assemble_trade(
        self,
        position: PositionState,
        *,
        closed_at: datetime,
        bar_index: int,
        exit_reason: ExitReason | None,
        is_open_at_end: bool = False,
    ) -> Trade:
        """Build the completed trade from the fills of one position lifecycle.

        Args:
            position: The position as it stood at closure.
            closed_at: Final exit timestamp.
            bar_index: Execution bar index of the final exit.
            exit_reason: Reason recorded on the final exit.
            is_open_at_end: Whether the run ended with this position still open.

        Returns:
            The assembled trade.
        """
        fills: tuple[ExecutionFill, ...] = tuple(self._current_fills)
        entry_quantity: Decimal = sum(
            (fill.quantity for fill in fills if fill.is_entry), ZERO
        )
        # realized_pnl is measured at fill prices, which already carry spread and
        # slippage. Adding the implicit costs back reconstructs the PnL that would have
        # been earned at the reference prices, which is what "gross" should mean.
        # Subtracting all friction from that then returns exactly the cash figure:
        #     net = (realized + implicit) - (implicit + commission) = realized - commission
        # so Trade.net_pnl equals the sum of this trade's fill cash deltas by identity
        # rather than by coincidence.
        realized: Decimal = sum(((fill.realized_pnl or ZERO) for fill in fills), ZERO)
        implicit: Decimal = sum((fill.implicit_costs for fill in fills), ZERO)
        costs: Decimal = sum((fill.total_costs for fill in fills), ZERO)
        gross: Decimal = realized + implicit

        return Trade(
            trade_id=position.trade_id,
            instrument_id=position.instrument_id,
            direction=position.direction,
            fills=fills,
            opened_at_utc=position.opened_at_utc,
            closed_at_utc=closed_at,
            entry_quantity=entry_quantity,
            avg_entry_price=weighted_average_price(fills, entries=True) or position.avg_entry_price,
            avg_exit_price=weighted_average_price(fills, entries=False),
            gross_pnl=gross,
            total_costs=costs,
            net_pnl=gross - costs,
            bars_held=max(bar_index - self._opened_bar_index, 0),
            max_adverse_excursion=position.max_adverse_excursion,
            max_favorable_excursion=position.max_favorable_excursion,
            exit_reason=exit_reason,
            mechanism_tag=position.mechanism_tag,
            is_open_at_backtest_end=is_open_at_end,
        )

    def _finalize_trade(self, trade: Trade) -> None:
        """Record a completed trade and reset the position book.

        Args:
            trade: The completed trade.
        """
        self._closed_trades.append(trade)
        self._position = None
        self._current_fills = []

        _LOGGER.info(
            "portfolio.trade_closed",
            trade_id=str(trade.trade_id),
            direction=str(trade.direction),
            fills=trade.fill_count,
            partial_exits=trade.partial_exit_count,
            net_pnl=str(trade.net_pnl),
            costs=str(trade.total_costs),
            bars_held=trade.bars_held,
            exit_reason=str(trade.exit_reason),
        )

    # -- end of run --------------------------------------------------------------------

    def mark_open_position_at_end(self, bar: BarData, *, bar_index: int) -> Trade | None:
        """Record any still-open position as an explicitly flagged trade.

        The position is *not* closed and no cash moves — doing so would invent a fill at a
        price nobody traded. The trade is recorded with ``is_open_at_backtest_end`` set so
        that reporting can exclude it from performance metrics while still showing it.
        Silently discarding it, or silently letting its mark-to-market into the results,
        are the two classic ways an unclosed position leaks PnL.

        Args:
            bar: Final bar of the run.
            bar_index: Index of the final bar.

        Returns:
            The flagged trade, or ``None`` if the run ended flat.
        """
        if self._position is None:
            return None

        position: PositionState = self._position
        trade: Trade = self._assemble_trade(
            position,
            closed_at=bar.ts_utc,
            bar_index=bar_index,
            exit_reason=None,
            is_open_at_end=True,
        )
        self._closed_trades.append(trade)

        _LOGGER.warning(
            "portfolio.position_open_at_backtest_end",
            trade_id=str(trade.trade_id),
            quantity=str(position.quantity),
            unrealized_pnl=str(position.unrealized_pnl(bar.close, self._instrument.contract_size)),
            note="excluded from performance metrics; reported separately",
        )
        return trade

    # -- reconciliation ----------------------------------------------------------------

    def reconcile(self) -> ReconciliationReport:
        """Verify Directive 6: booked cash flows must equal the change in equity.

        Returns:
            The reconciliation report. Under exact decimal arithmetic the discrepancy is
            zero or there is a bug; there is no third outcome and no tolerance to argue
            about.
        """
        fill_sum: Decimal = sum(((fill.cash_delta or ZERO) for fill in self._ledger), ZERO)
        equity_delta: Decimal = self._cash - self._initial_cash
        discrepancy: Decimal = fill_sum - equity_delta

        known_trades: set[TradeId] = {trade.trade_id for trade in self._closed_trades}
        if self._position is not None:
            known_trades.add(self._position.trade_id)

        orphaned: tuple[FillId, ...] = tuple(
            fill.fill_id for fill in self._ledger if fill.trade_id not in known_trades
        )
        unbooked: tuple[FillId, ...] = tuple(
            fill.fill_id for fill in self._ledger if not fill.is_booked
        )
        open_trades: tuple[TradeId, ...] = tuple(
            trade.trade_id for trade in self._closed_trades if trade.is_open_at_backtest_end
        )

        report = ReconciliationReport(
            passed=discrepancy == ZERO and not orphaned and not unbooked,
            sum_fill_cash_flows=fill_sum,
            final_equity_delta=equity_delta,
            discrepancy=discrepancy,
            fill_count=len(self._ledger),
            trade_count=len([t for t in self._closed_trades if not t.is_open_at_backtest_end]),
            orphaned_fills=orphaned,
            unbooked_fills=unbooked,
            open_trades_at_end=open_trades,
            checked_at_utc=datetime.now(UTC),
        )

        log = _LOGGER.info if report.passed else _LOGGER.error
        log(
            "portfolio.reconciliation",
            passed=report.passed,
            fill_sum=str(fill_sum),
            equity_delta=str(equity_delta),
            discrepancy=str(discrepancy),
            fills=report.fill_count,
            trades=report.trade_count,
            orphaned=len(orphaned),
        )
        return report

    # -- helpers -----------------------------------------------------------------------

    def _require_position(self) -> PositionState:
        """Return the open position.

        Returns:
            The open position.

        Raises:
            PortfolioError: If no position is open.
        """
        if self._position is None:
            raise PortfolioError("Operation requires an open position, but the book is flat")
        return self._position

    def _unrealized(self, mark_price: Decimal | None) -> Decimal:
        """Return unrealised PnL at a mark price.

        Args:
            mark_price: Price to mark against, or ``None`` to mark at cost.

        Returns:
            Unrealised PnL, or zero when flat.
        """
        if self._position is None:
            return ZERO
        price: Decimal = mark_price if mark_price is not None else self._position.avg_entry_price
        return self._position.unrealized_pnl(price, self._instrument.contract_size)


def equity_curve_bounds(snapshots: Sequence[EquitySnapshot]) -> tuple[Decimal, Decimal]:
    """Return peak mark-to-market equity and the maximum drawdown from it.

    Args:
        snapshots: Equity snapshots in chronological order.

    Returns:
        A pair of (peak equity, maximum drawdown as a positive amount). Computed on the
        mark-to-market basis per the C-1 ruling, since that is the basis every gate and
        every prop firm loss limit evaluates against.
    """
    if not snapshots:
        return ZERO, ZERO

    peak: Decimal = snapshots[0].mtm_equity
    max_drawdown: Decimal = ZERO
    for snapshot in snapshots:
        peak = max(peak, snapshot.mtm_equity)
        max_drawdown = max(max_drawdown, peak - snapshot.mtm_equity)
    return peak, max_drawdown
