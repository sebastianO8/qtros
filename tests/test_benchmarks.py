"""Known-truth benchmarks.

A benchmark differs from a unit test in what it is checked against. A unit test asserts
that a component behaves as its author intended; a benchmark asserts that the system
agrees with an answer derived independently, on paper, from the arithmetic of the
instrument. The two catch different things — and this benchmark caught something real.

**What this file found.** Written against the first implementation of the execution path,
the first assertion here failed. Spread and slippage were being applied twice: once by
worsening the fill price, and again as a separate cash debit. Every component test passed,
because each was checking its own behaviour rather than the total. Only a figure computed
outside the system could reveal that the total was wrong. The convention that resolved it
is recorded as ASS-006.

**The arithmetic, derived on paper.** Instrument: tick 0.001, contract 100,000, spread 2
ticks, commission 6 per lot round turn. Friction model: 0.5 ticks of slippage.

    half-spread              = 2 x 0.001 / 2       = 0.001
    slippage                 = 0.5 x 0.001         = 0.0005
    intended adjustment      = 0.001 + 0.0005      = 0.0015

    buy  at reference 95.000 -> 95.0015 -> tick-rounded adversely up   -> 95.002
    sell at reference 100.000 -> 99.9985 -> tick-rounded adversely down -> 99.998

    actual degradation per side = 0.002 (0.0015 intended + 0.0005 rounding)
    per side, 1 lot             = 0.002 x 100,000  = 200
    implicit friction, both     = 400
    commission, both sides      = 6 / 2 x 1 x 2    = 6

    reference-price PnL = (100.000 - 95.000) x 100,000 = 500,000
    net cash            = 500,000 - 400 - 6            = 499,594

Every figure below is one of those, and none was read out of a program.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest

from qtros.domain.enums import Direction, ExitReason
from qtros.domain.execution import ExecutionFill, Trade
from qtros.domain.identifiers import SequentialIdFactory
from qtros.domain.instrument import InstrumentSpec
from qtros.domain.market import BarData
from qtros.domain.orders import ApprovedOrder
from qtros.engine.portfolio import Portfolio
from qtros.execution.broker import SimulatedBroker
from qtros.execution.friction import StandardFrictionModel

from tests.conftest import INITIAL_CASH

ZERO = Decimal("0")

ENTRY_REFERENCE = Decimal("95.000")
EXIT_REFERENCE = Decimal("100.000")
QUANTITY = Decimal("1.00")

EXPECTED_ENTRY_FILL = Decimal("95.002")
EXPECTED_EXIT_FILL = Decimal("99.998")
EXPECTED_REFERENCE_PNL = Decimal("500000")
EXPECTED_IMPLICIT_FRICTION = Decimal("400")
EXPECTED_COMMISSION = Decimal("6")
EXPECTED_NET_PNL = Decimal("499594")


def _run_buy_and_hold(
    instrument: InstrumentSpec,
    friction: StandardFrictionModel,
    make_bar: Callable[..., BarData],
    make_order: Callable[..., ApprovedOrder],
    ids: SequentialIdFactory,
    portfolio: Portfolio,
) -> tuple[Portfolio, Trade, ExecutionFill, ExecutionFill]:
    """Buy one lot at bar 0 and sell it at bar 10.

    Args:
        instrument: Contract specification.
        friction: Cost model.
        make_bar: Bar factory.
        make_order: Approved order factory.
        ids: Identifier factory.
        portfolio: Funded, flat portfolio.

    Returns:
        The portfolio, the completed trade, and the **booked** entry and exit fills. The
        booked versions are taken from the ledger rather than from the broker results,
        because a broker fill carries no cash effect until the portfolio books it — and an
        assertion about ``cash_delta`` on an unbooked fill would be comparing against
        ``None``.
    """
    broker = SimulatedBroker(friction, ids)
    trade_id = portfolio.allocate_trade_id()

    entry_bar = make_bar(index=0, price=str(ENTRY_REFERENCE))
    entry_result = broker.submit(
        make_order(Direction.LONG, QUANTITY, trade_id, index=0), entry_bar, instrument
    )
    assert entry_result.fill is not None
    portfolio.apply_fill(entry_result.fill, bar_index=0)

    exit_bar = make_bar(index=10, price=str(EXIT_REFERENCE))
    exit_result = broker.submit(
        make_order(
            Direction.SHORT, QUANTITY, trade_id,
            is_exit=True, exit_reason=ExitReason.SIGNAL_EXIT, index=10,
        ),
        exit_bar,
        instrument,
    )
    assert exit_result.fill is not None
    outcome = portfolio.apply_fill(exit_result.fill, bar_index=10)

    assert outcome.closed_trade is not None
    return portfolio, outcome.closed_trade, portfolio.ledger[0], portfolio.ledger[-1]


@pytest.fixture
def buy_and_hold(
    instrument: InstrumentSpec,
    friction: StandardFrictionModel,
    make_bar: Callable[..., BarData],
    make_order: Callable[..., ApprovedOrder],
    ids: SequentialIdFactory,
    portfolio: Portfolio,
) -> tuple[Portfolio, Trade, ExecutionFill, ExecutionFill]:
    """Return the completed buy-and-hold trade and its two fills."""
    return _run_buy_and_hold(instrument, friction, make_bar, make_order, ids, portfolio)


class TestBuyAndHoldFillPrices:
    """Fill prices must match the hand-derived values exactly."""

    def test_the_buy_fills_above_the_reference(
        self, buy_and_hold: tuple[Portfolio, Trade, ExecutionFill, ExecutionFill]
    ) -> None:
        """A buyer crosses to the ask and then pays adverse tick rounding."""
        _, _, entry, _ = buy_and_hold
        assert entry.reference_price == ENTRY_REFERENCE
        assert entry.fill_price == EXPECTED_ENTRY_FILL

    def test_the_sell_fills_below_the_reference(
        self, buy_and_hold: tuple[Portfolio, Trade, ExecutionFill, ExecutionFill]
    ) -> None:
        """Symmetry: the seller crosses to the bid and rounds adversely too."""
        _, _, _, exit_fill = buy_and_hold
        assert exit_fill.reference_price == EXIT_REFERENCE
        assert exit_fill.fill_price == EXPECTED_EXIT_FILL

    def test_degradation_is_symmetric_across_the_two_sides(
        self, buy_and_hold: tuple[Portfolio, Trade, ExecutionFill, ExecutionFill]
    ) -> None:
        """Neither side is favoured; an asymmetry here would be a directional bias."""
        _, _, entry, exit_fill = buy_and_hold
        assert entry.fill_price - entry.reference_price == Decimal("0.002")
        assert exit_fill.reference_price - exit_fill.fill_price == Decimal("0.002")


class TestBuyAndHoldPnl:
    """The trade's PnL must equal the price difference minus configured friction."""

    def test_gross_pnl_reconstructs_the_reference_price_return(
        self, buy_and_hold: tuple[Portfolio, Trade, ExecutionFill, ExecutionFill]
    ) -> None:
        """The assertion that found the double-charge.

        Gross PnL is defined as the return at reference prices. Since realised PnL is
        measured at fill prices, adding back the implicit friction must reproduce the
        reference figure exactly. If spread and slippage were also being debited from cash,
        this identity would not close.
        """
        _, trade, _, _ = buy_and_hold
        assert trade.gross_pnl == EXPECTED_REFERENCE_PNL
        assert trade.gross_pnl == (EXIT_REFERENCE - ENTRY_REFERENCE) * QUANTITY * Decimal(
            "100000"
        )

    def test_net_pnl_equals_gross_minus_all_itemised_friction(
        self, buy_and_hold: tuple[Portfolio, Trade, ExecutionFill, ExecutionFill]
    ) -> None:
        """Directive 2: every cost is accounted for and none is double-counted."""
        _, trade, _, _ = buy_and_hold
        assert trade.total_costs == EXPECTED_IMPLICIT_FRICTION + EXPECTED_COMMISSION
        assert trade.net_pnl == trade.gross_pnl - trade.total_costs
        assert trade.net_pnl == EXPECTED_NET_PNL

    def test_net_pnl_equals_the_sum_of_fill_cash_deltas(
        self, buy_and_hold: tuple[Portfolio, Trade, ExecutionFill, ExecutionFill]
    ) -> None:
        """The trade-level figure and the ledger agree by identity, not coincidence."""
        _, trade, _, _ = buy_and_hold
        assert trade.net_pnl == sum((fill.cash_delta or ZERO for fill in trade.fills), ZERO)

    def test_final_cash_matches_the_hand_computed_balance(
        self, buy_and_hold: tuple[Portfolio, Trade, ExecutionFill, ExecutionFill]
    ) -> None:
        """End-to-end: the account balance is the number derived on paper."""
        portfolio, _, _, _ = buy_and_hold
        assert portfolio.cash == INITIAL_CASH + EXPECTED_NET_PNL

    def test_the_benchmark_reconciles(
        self, buy_and_hold: tuple[Portfolio, Trade, ExecutionFill, ExecutionFill]
    ) -> None:
        """Directive 6 on the simplest possible trade."""
        portfolio, _, _, _ = buy_and_hold
        report = portfolio.reconcile()
        assert report.passed
        assert report.discrepancy == ZERO
        assert report.trade_count == 1


class TestFrictionAttribution:
    """Costs must be attributable to their source, not netted into one figure.

    When a strategy's edge dies under realistic friction, the first question is which
    friction killed it. A single aggregate cost cannot answer that, so Directive 2 requires
    the breakdown and these tests require it to add up.
    """

    def test_every_component_is_present_and_non_zero(
        self, buy_and_hold: tuple[Portfolio, Trade, ExecutionFill, ExecutionFill]
    ) -> None:
        """A silently zeroed component would look like a cheaper strategy."""
        _, _, entry, exit_fill = buy_and_hold
        for fill in (entry, exit_fill):
            assert fill.spread_cost > ZERO
            assert fill.slippage_cost > ZERO
            assert fill.commission > ZERO

    def test_the_components_sum_to_total_costs(
        self, buy_and_hold: tuple[Portfolio, Trade, ExecutionFill, ExecutionFill]
    ) -> None:
        """No unattributed residual is permitted to hide in the total."""
        _, _, entry, exit_fill = buy_and_hold
        for fill in (entry, exit_fill):
            assert (
                fill.spread_cost + fill.slippage_cost + fill.commission == fill.total_costs
            )
            assert fill.implicit_costs + fill.cash_costs == fill.total_costs

    def test_implicit_costs_equal_the_cash_value_of_the_price_degradation(
        self, buy_and_hold: tuple[Portfolio, Trade, ExecutionFill, ExecutionFill]
    ) -> None:
        """ASS-006: what is reported as implicit friction is what the price actually cost.

        Reporting the *intended* adjustment would leave the adverse tick rounding
        unattributed. Over four hundred trades an unattributed fraction of a tick per fill
        is not a rounding footnote.
        """
        _, _, entry, exit_fill = buy_and_hold
        for fill in (entry, exit_fill):
            degradation = (fill.fill_price - fill.reference_price) * Decimal(
                fill.direction.sign
            )
            assert fill.implicit_costs == degradation * fill.quantity * Decimal("100000")

    def test_only_commission_moves_cash_directly(
        self, buy_and_hold: tuple[Portfolio, Trade, ExecutionFill, ExecutionFill]
    ) -> None:
        """The cash identity charges commission only; the rest is inside the price."""
        _, _, entry, _ = buy_and_hold
        assert entry.cash_costs == entry.commission
        assert entry.cash_delta == (entry.realized_pnl or ZERO) - entry.commission


class TestFrictionlessBaseline:
    """With costs removed, the result must be the pure price return.

    This isolates the friction model: if the frictionless run does not reproduce the
    reference PnL exactly, the discrepancy is in the execution path rather than in the cost
    configuration, and the friction-inclusive benchmark above cannot be trusted to be
    measuring what it claims.
    """

    def test_zero_friction_reproduces_the_exact_price_return(
        self,
        frictionless_instrument: InstrumentSpec,
        make_bar: Callable[..., BarData],
        make_order: Callable[..., ApprovedOrder],
        ids: SequentialIdFactory,
    ) -> None:
        """No spread, no slippage, no commission: PnL is the price difference."""
        frictionless = StandardFrictionModel(
            slippage_ticks=ZERO,
            spread_ticks_override=ZERO,
            acknowledge_zero_friction=True,
        )
        portfolio = Portfolio(frictionless_instrument, INITIAL_CASH, ids)
        _, trade, entry, exit_fill = _run_buy_and_hold(
            frictionless_instrument, frictionless, make_bar, make_order, ids, portfolio
        )

        assert entry.fill_price == ENTRY_REFERENCE
        assert exit_fill.fill_price == EXIT_REFERENCE
        assert trade.total_costs == ZERO
        assert trade.gross_pnl == trade.net_pnl == EXPECTED_REFERENCE_PNL

    def test_friction_is_the_only_difference_between_the_two_runs(
        self,
        frictionless_instrument: InstrumentSpec,
        make_bar: Callable[..., BarData],
        make_order: Callable[..., ApprovedOrder],
        buy_and_hold: tuple[Portfolio, Trade, ExecutionFill, ExecutionFill],
    ) -> None:
        """The gap between the two runs is exactly the itemised friction.

        This is the property that makes friction sensitivity analysis meaningful: the drag
        is a clean subtraction rather than an entangled effect.
        """
        _, friction_trade, _, _ = buy_and_hold
        frictionless = StandardFrictionModel(
            slippage_ticks=ZERO,
            spread_ticks_override=ZERO,
            acknowledge_zero_friction=True,
        )
        clean_ids = SequentialIdFactory("clean001")
        clean_portfolio = Portfolio(frictionless_instrument, INITIAL_CASH, clean_ids)

        def clean_order(
            direction: Direction,
            quantity: Decimal,
            trade_id: object,
            *,
            is_exit: bool = False,
            exit_reason: ExitReason | None = None,
            index: int = 0,
        ) -> ApprovedOrder:
            from qtros.domain.orders import RISK_APPROVAL, TradeIntent

            from tests.conftest import BAR_INTERVAL, EPOCH

            intent = TradeIntent(
                intent_id=clean_ids.next_intent_id(),
                ts_utc=EPOCH + BAR_INTERVAL * index,
                instrument_id=frictionless_instrument.instrument_id,
                direction=direction,
                is_exit=is_exit,
                exit_reason=exit_reason,
                mechanism_tag="" if is_exit else "benchmark",
            )
            return ApprovedOrder(
                order_id=clean_ids.next_order_id(),
                intent=intent,
                quantity=quantity,
                approved_at_utc=intent.ts_utc,
                trade_id=trade_id,  # type: ignore[arg-type]
                approval=RISK_APPROVAL,
            )

        _, clean_trade, _, _ = _run_buy_and_hold(
            frictionless_instrument, frictionless, make_bar, clean_order,
            clean_ids, clean_portfolio,
        )
        assert clean_trade.net_pnl - friction_trade.net_pnl == friction_trade.total_costs
