"""Engine orchestration tests: scenarios and invariants.

Maps to the ARCH-002 test tree as follows — the ``regression`` marker covers
``tests/regression/cycle1/``, ``property`` covers ``tests/property/``, and the remainder
is ``tests/unit/engine/``. Markers rather than directories so a flat file still supports
``pytest -m regression``.

Every test in :class:`TestCycle1PartialExits` pins a finding from the Empirical Lessons
Log. **None of them may be deleted.** If one becomes inconvenient, the correct response is
to establish that the lesson no longer applies and record that reasoning — not to remove
the test that encodes it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from qtros.domain.enums import Direction, ExitReason
from qtros.domain.identifiers import InstrumentId, SequentialIdFactory, TradeId
from qtros.domain.market import BarData
from qtros.domain.orders import RISK_APPROVAL, ApprovedOrder, TradeIntent
from qtros.engine.portfolio import FillOutcome, Portfolio, PortfolioError
from qtros.execution.broker import SimulatedBroker
from qtros.execution.friction import StandardFrictionModel
from tests.conftest import BAR_INTERVAL, EPOCH, INITIAL_CASH

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from qtros.domain.execution import ExecutionFill, Trade
    from qtros.domain.instrument import InstrumentSpec

ZERO = Decimal("0")


def _approved(
    ids: SequentialIdFactory,
    instrument: InstrumentSpec,
    direction: Direction,
    quantity: str | Decimal,
    trade_id: TradeId,
    *,
    is_exit: bool = False,
    exit_reason: ExitReason | None = None,
    index: int = 0,
) -> ApprovedOrder:
    """Build a risk-approved order for tests that drive the real broker.

    A module-level helper rather than a fixture because the tests that need it construct
    their own identifier factories, and a fixture-bound factory would hand them the wrong
    sequence.

    Args:
        ids: Identifier factory for this run.
        instrument: Contract specification.
        direction: Side to trade.
        quantity: Lots.
        trade_id: Position lifecycle the order belongs to.
        is_exit: Whether the order reduces or closes a position.
        exit_reason: Reason recorded on the resulting fill.
        index: Bar index, used to derive the timestamp.

    Returns:
        An approved order.
    """
    intent = TradeIntent(
        intent_id=ids.next_intent_id(),
        ts_utc=EPOCH + BAR_INTERVAL * index,
        instrument_id=instrument.instrument_id,
        direction=direction,
        is_exit=is_exit,
        exit_reason=exit_reason,
        mechanism_tag="" if is_exit else "test_mechanism",
    )
    return ApprovedOrder(
        order_id=ids.next_order_id(),
        intent=intent,
        quantity=Decimal(quantity),
        approved_at_utc=intent.ts_utc,
        trade_id=trade_id,
        approval=RISK_APPROVAL,
    )


# --------------------------------------------------------------------------------------
# Cycle 1: one entry, three partial exits
# --------------------------------------------------------------------------------------


@pytest.fixture
def cycle1_outcomes(
    portfolio: Portfolio,
    make_fill: Callable[..., ExecutionFill],
) -> tuple[Portfolio, list[FillOutcome]]:
    """Replay the Cycle 1 scenario: one entry of 0.30 lots, exited in three parts.

    This is the shape that produced the original bug — partial exit PnL reaching running
    equity without a corresponding trade row. Prices are chosen so the three exits are not
    all winners, because a scenario where every leg profits would not distinguish correct
    apportionment from a sign error.
    """
    trade_id = portfolio.allocate_trade_id()
    outcomes: list[FillOutcome] = [
        portfolio.apply_fill(
            make_fill("0.30", "95.000", Direction.LONG, trade_id, is_entry=True, index=0),
            bar_index=0,
        ),
        portfolio.apply_fill(
            make_fill(
                "0.10",
                "95.500",
                Direction.SHORT,
                trade_id,
                is_entry=False,
                exit_reason=ExitReason.TAKE_PROFIT,
                index=1,
            ),
            bar_index=1,
        ),
        portfolio.apply_fill(
            make_fill(
                "0.10",
                "96.000",
                Direction.SHORT,
                trade_id,
                is_entry=False,
                exit_reason=ExitReason.TAKE_PROFIT,
                index=2,
            ),
            bar_index=2,
        ),
        portfolio.apply_fill(
            make_fill(
                "0.10",
                "94.000",
                Direction.SHORT,
                trade_id,
                is_entry=False,
                exit_reason=ExitReason.STOP_LOSS,
                index=5,
            ),
            bar_index=5,
        ),
    ]
    return portfolio, outcomes


@pytest.mark.regression
class TestCycle1PartialExits:
    """Pins the Cycle 1 partial-exit findings (Constitution §7, ARCH-001 C-4)."""

    def test_one_entry_three_partial_exits_is_exactly_one_trade(
        self, cycle1_outcomes: tuple[Portfolio, list[FillOutcome]]
    ) -> None:
        """A position opened once and closed in stages is one lifecycle, not three."""
        portfolio, _ = cycle1_outcomes
        assert len(portfolio.closed_trades) == 1

    def test_the_trade_contains_all_four_fills(
        self, cycle1_outcomes: tuple[Portfolio, list[FillOutcome]]
    ) -> None:
        """Fills are the atomic events; all four belong to the single trade."""
        portfolio, _ = cycle1_outcomes
        trade: Trade = portfolio.closed_trades[0]
        assert trade.fill_count == 4
        assert trade.partial_exit_count == 2
        assert len(portfolio.ledger) == 4

    def test_only_the_final_exit_is_not_partial(
        self, cycle1_outcomes: tuple[Portfolio, list[FillOutcome]]
    ) -> None:
        """The closing exit is flagged as final; the two before it are partial."""
        portfolio, _ = cycle1_outcomes
        exits = [fill for fill in portfolio.ledger if not fill.is_entry]
        assert [fill.is_partial_exit for fill in exits] == [True, True, False]

    def test_cash_discrepancy_is_exactly_zero(
        self, cycle1_outcomes: tuple[Portfolio, list[FillOutcome]]
    ) -> None:
        """Directive 6, with no tolerance to hide behind."""
        portfolio, _ = cycle1_outcomes
        report = portfolio.reconcile()
        assert report.passed
        assert report.discrepancy == ZERO

    def test_sum_of_fill_cash_deltas_equals_change_in_cash(
        self, cycle1_outcomes: tuple[Portfolio, list[FillOutcome]]
    ) -> None:
        """The ledger is the only thing that may move cash."""
        portfolio, _ = cycle1_outcomes
        ledger_total = sum((fill.cash_delta or ZERO for fill in portfolio.ledger), ZERO)
        assert ledger_total == portfolio.cash - INITIAL_CASH

    def test_every_fill_satisfies_its_own_accounting_identity(
        self, cycle1_outcomes: tuple[Portfolio, list[FillOutcome]]
    ) -> None:
        """Reconciliation at fill granularity, so a failure localises to one row."""
        portfolio, _ = cycle1_outcomes
        for fill in portfolio.ledger:
            fill.assert_internally_consistent()

    def test_partial_exits_do_not_inflate_the_trade_count(
        self, cycle1_outcomes: tuple[Portfolio, list[FillOutcome]]
    ) -> None:
        """Directive 7 gates on trade count; scale-outs must not manufacture trades.

        Counting each partial exit as its own trade would let a strategy reach the
        400-trade threshold by exiting in stages rather than by finding more signals.
        """
        portfolio, _ = cycle1_outcomes
        assert portfolio.reconcile().trade_count == 1

    def test_trade_net_pnl_equals_the_sum_of_its_fill_cash_deltas(
        self, cycle1_outcomes: tuple[Portfolio, list[FillOutcome]]
    ) -> None:
        """The trade-level figure and the ledger cannot disagree."""
        portfolio, _ = cycle1_outcomes
        trade = portfolio.closed_trades[0]
        assert trade.net_pnl == sum((fill.cash_delta or ZERO for fill in trade.fills), ZERO)

    def test_trade_pnl_matches_hand_computation(
        self, cycle1_outcomes: tuple[Portfolio, list[FillOutcome]]
    ) -> None:
        """Independently derived expected value, not a recorded output.

        Legs: +0.500, +1.000, -1.000 on 0.10 lots at 100,000 contract size, so realised
        PnL at fill prices is 0.10 x 0.500 x 100,000 = 5,000. Four fills at 3 commission
        each gives 12 of cash cost, so net cash is 4,988.
        """
        portfolio, _ = cycle1_outcomes
        trade = portfolio.closed_trades[0]
        assert trade.net_pnl == Decimal("4988")
        assert portfolio.cash == INITIAL_CASH + Decimal("4988")

    def test_splitting_a_winner_across_exits_cannot_dilute_concentration(
        self,
        instrument: InstrumentSpec,
        friction: StandardFrictionModel,
        make_bar: Callable[..., BarData],
    ) -> None:
        """Directive 8 must not be gameable by exit accounting.

        The same winning position, closed in one exit and in three, yields one trade with
        identical PnL either way. If partial exits produced separate trade rows, a single
        large winner would appear as three medium ones and measured top-five concentration
        would fall without anything about the strategy changing.

        Routed through the real broker rather than synthetic fills, because friction scales
        with quantity: three 0.10-lot exits must cost exactly what one 0.30-lot exit costs,
        and only the real cost path establishes that.
        """
        results: list[tuple[int, Decimal, Decimal]] = []
        for exit_quantities in (["0.30"], ["0.10", "0.10", "0.10"]):
            run_ids = SequentialIdFactory("split001")
            run_broker = SimulatedBroker(friction, run_ids)
            run_portfolio = Portfolio(instrument, INITIAL_CASH, run_ids)
            trade_id = run_portfolio.allocate_trade_id()

            entry = run_broker.submit(
                _approved(run_ids, instrument, Direction.LONG, "0.30", trade_id, index=0),
                make_bar(index=0, price="95.000"),
                instrument,
            )
            assert entry.fill is not None
            run_portfolio.apply_fill(entry.fill, bar_index=0)

            for index, quantity in enumerate(exit_quantities, start=1):
                leg = run_broker.submit(
                    _approved(
                        run_ids,
                        instrument,
                        Direction.SHORT,
                        quantity,
                        trade_id,
                        is_exit=True,
                        exit_reason=ExitReason.TAKE_PROFIT,
                        index=index,
                    ),
                    make_bar(index=index, price="96.000"),
                    instrument,
                )
                assert leg.fill is not None
                run_portfolio.apply_fill(leg.fill, bar_index=index)

            trade = run_portfolio.closed_trades[0]
            results.append((len(run_portfolio.closed_trades), trade.gross_pnl, trade.net_pnl))

        assert results[0][0] == results[1][0] == 1
        assert results[0][1] == results[1][1]
        assert results[0][2] == results[1][2]


# --------------------------------------------------------------------------------------
# Reversal
# --------------------------------------------------------------------------------------


@pytest.fixture
def reversal(
    portfolio: Portfolio,
    make_fill: Callable[..., ExecutionFill],
) -> tuple[Portfolio, FillOutcome]:
    """Open 0.20 long, then sell 0.50 — closing the long and opening 0.30 short."""
    trade_id = portfolio.allocate_trade_id()
    portfolio.apply_fill(
        make_fill("0.20", "95.000", Direction.LONG, trade_id, is_entry=True, index=0),
        bar_index=0,
    )
    outcome = portfolio.apply_fill(
        make_fill("0.50", "95.200", Direction.SHORT, trade_id, is_entry=False, index=1),
        bar_index=1,
    )
    return portfolio, outcome


class TestReversal:
    """One opposing fill larger than the open position spans two lifecycles (ASS-004)."""

    def test_reversal_produces_two_booked_fills(
        self, reversal: tuple[Portfolio, FillOutcome]
    ) -> None:
        """The closing portion and the opening portion are separate ledger rows."""
        _, outcome = reversal
        assert len(outcome.booked_fills) == 2

    def test_the_two_fills_carry_distinct_trade_ids(
        self, reversal: tuple[Portfolio, FillOutcome]
    ) -> None:
        """Each half belongs to its own position lifecycle.

        Sharing a trade identifier would merge two entries into one record and corrupt
        both trade count and holding-period statistics.
        """
        _, outcome = reversal
        closing, opening = outcome.booked_fills
        assert closing.trade_id != opening.trade_id
        assert closing.is_entry is False
        assert opening.is_entry is True

    def test_fill_ids_are_distinct_and_derived_from_the_original(
        self, reversal: tuple[Portfolio, FillOutcome]
    ) -> None:
        """Split fills remain traceable to the broker fill they came from."""
        _, outcome = reversal
        closing, opening = outcome.booked_fills
        assert closing.fill_id != opening.fill_id
        assert str(closing.fill_id).endswith("c")
        assert str(opening.fill_id).endswith("o")

    def test_the_prior_trade_closes_with_a_reversal_exit_reason(
        self, reversal: tuple[Portfolio, FillOutcome]
    ) -> None:
        """The closed trade records *why* it closed, not merely that it did."""
        portfolio, outcome = reversal
        assert outcome.closed_trade is not None
        assert outcome.closed_trade.exit_reason is ExitReason.REVERSAL
        assert len(portfolio.closed_trades) == 1

    def test_the_new_position_holds_only_the_residual(
        self, reversal: tuple[Portfolio, FillOutcome]
    ) -> None:
        """0.50 sold against 0.20 long leaves 0.30 short."""
        portfolio, _ = reversal
        position = portfolio.position
        assert position is not None
        assert position.direction is Direction.SHORT
        assert position.quantity == Decimal("0.30")

    def test_costs_are_apportioned_not_duplicated(
        self, reversal: tuple[Portfolio, FillOutcome]
    ) -> None:
        """Splitting a fill must not double-charge its friction.

        The two halves' costs sum to the original fill's costs exactly. Charging each half
        the full amount would silently inflate friction on every reversal — plausible
        enough to survive review and material over hundreds of trades.
        """
        _, outcome = reversal
        closing, opening = outcome.booked_fills
        assert closing.commission + opening.commission == Decimal("3")
        assert closing.spread_cost + opening.spread_cost == Decimal("100")
        assert closing.slippage_cost + opening.slippage_cost == Decimal("50")

    def test_quantities_sum_to_the_original_fill(
        self, reversal: tuple[Portfolio, FillOutcome]
    ) -> None:
        """No size is created or lost by the split."""
        _, outcome = reversal
        closing, opening = outcome.booked_fills
        assert closing.quantity + opening.quantity == Decimal("0.50")

    def test_the_reversal_reconciles_exactly(self, reversal: tuple[Portfolio, FillOutcome]) -> None:
        """Directive 6 holds across the split."""
        portfolio, _ = reversal
        report = portfolio.reconcile()
        assert report.discrepancy == ZERO
        assert not report.orphaned_fills


# --------------------------------------------------------------------------------------
# Unclosed position at run end
# --------------------------------------------------------------------------------------


class TestUnclosedPosition:
    """A position open when the run ends is flagged, never invented shut (ASS-005)."""

    @pytest.fixture
    def open_at_end(
        self,
        portfolio: Portfolio,
        make_fill: Callable[..., ExecutionFill],
        make_bar: Callable[..., BarData],
    ) -> tuple[Portfolio, Trade, BarData]:
        """Open a position and reach the end of the run without closing it."""
        trade_id = portfolio.allocate_trade_id()
        portfolio.apply_fill(
            make_fill("0.20", "95.000", Direction.LONG, trade_id, is_entry=True),
            bar_index=0,
        )
        final_bar = make_bar(index=9, price="96.000")
        trade = portfolio.mark_open_position_at_end(final_bar, bar_index=9)
        assert trade is not None
        return portfolio, trade, final_bar

    def test_the_open_position_is_flagged(
        self, open_at_end: tuple[Portfolio, Trade, BarData]
    ) -> None:
        """The flag is how reporting knows to exclude it."""
        _, trade, _ = open_at_end
        assert trade.is_open_at_backtest_end is True

    def test_no_synthetic_closing_fill_is_created(
        self, open_at_end: tuple[Portfolio, Trade, BarData]
    ) -> None:
        """Closing the position would invent a fill at a price nobody traded.

        The ledger holds exactly the one entry fill. An exit here would be fabricated
        market data entering the results through the accounting layer.
        """
        portfolio, trade, _ = open_at_end
        assert len(portfolio.ledger) == 1
        assert all(fill.is_entry for fill in trade.fills)
        assert trade.avg_exit_price is None
        assert trade.exit_reason is None

    def test_no_cash_moves(self, open_at_end: tuple[Portfolio, Trade, BarData]) -> None:
        """Only the entry's commission has been charged; nothing was realised."""
        portfolio, _, _ = open_at_end
        assert portfolio.cash == INITIAL_CASH - Decimal("3")
        assert portfolio.realized_pnl == ZERO

    def test_it_is_excluded_from_the_reported_trade_count(
        self, open_at_end: tuple[Portfolio, Trade, BarData]
    ) -> None:
        """Directive 7's count covers completed trades only."""
        portfolio, trade, _ = open_at_end
        report = portfolio.reconcile()
        assert report.trade_count == 0
        assert report.open_trades_at_end == (trade.trade_id,)

    def test_reconciliation_still_passes(
        self, open_at_end: tuple[Portfolio, Trade, BarData]
    ) -> None:
        """An open position is a reporting caveat, not a ledger inconsistency."""
        portfolio, _, _ = open_at_end
        report = portfolio.reconcile()
        assert report.passed
        assert report.discrepancy == ZERO

    def test_mark_to_market_still_reflects_the_open_exposure(
        self, open_at_end: tuple[Portfolio, Trade, BarData]
    ) -> None:
        """Equity on the MTM basis includes the open PnL; closed equity does not.

        This is the C-1 distinction in miniature: a gate reading closed equity would see
        no movement at all here.
        """
        portfolio, _, final_bar = open_at_end
        assert portfolio.position is not None
        snapshot = portfolio.mark_to_market(final_bar)
        assert snapshot.unrealized_pnl > ZERO
        assert snapshot.mtm_equity == snapshot.cash + snapshot.unrealized_pnl
        assert snapshot.closed_equity == portfolio.cash

    def test_flat_run_produces_no_open_trade(
        self, portfolio: Portfolio, make_bar: Callable[..., BarData]
    ) -> None:
        """A run that ends flat has nothing to flag."""
        assert portfolio.mark_open_position_at_end(make_bar(index=9), bar_index=9) is None


# --------------------------------------------------------------------------------------
# Property-based ledger invariant
# --------------------------------------------------------------------------------------


def replay_operations(
    operations: Sequence[tuple[bool, int, int]],
    instrument: InstrumentSpec,
) -> Portfolio:
    """Drive a portfolio through a sequence of broker-matched orders.

    Extracted as a plain function so it can be driven by Hypothesis, by a deterministic
    loop, or by hand during debugging without reaching into a test body.

    Args:
        operations: Tuples of (buy, quantity in hundredths of a lot, price in millis).
        instrument: Contract specification.

    Returns:
        The portfolio after every operation has been applied.
    """
    ids = SequentialIdFactory("prop0001")
    broker = SimulatedBroker(StandardFrictionModel(slippage_ticks=Decimal("0.5")), ids)
    portfolio = Portfolio(instrument, INITIAL_CASH, ids)

    for index, (buy, quantity_hundredths, price_millis) in enumerate(operations):
        direction = Direction.LONG if buy else Direction.SHORT
        quantity = Decimal(quantity_hundredths) / Decimal("100")
        price = Decimal(price_millis) / Decimal("1000")

        position = portfolio.position
        is_exit = position is not None and position.direction is not direction

        intent = TradeIntent(
            intent_id=ids.next_intent_id(),
            ts_utc=EPOCH + BAR_INTERVAL * index,
            instrument_id=instrument.instrument_id,
            direction=direction,
            is_exit=is_exit,
            mechanism_tag="" if is_exit else "property_test",
        )
        order = ApprovedOrder(
            order_id=ids.next_order_id(),
            intent=intent,
            quantity=quantity,
            approved_at_utc=intent.ts_utc,
            trade_id=portfolio.allocate_trade_id(),
            approval=RISK_APPROVAL,
        )
        bar = BarData(
            ts_utc=EPOCH + BAR_INTERVAL * index,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=Decimal("1000"),
        )
        result = broker.submit(order, bar, instrument)
        if result.fill is not None:
            portfolio.apply_fill(result.fill, bar_index=index)

    return portfolio


order_operations = st.lists(
    st.tuples(
        st.booleans(),
        st.integers(min_value=1, max_value=50),  # 0.01 .. 0.50 lots
        st.integers(min_value=90_000, max_value=110_000),  # 90.000 .. 110.000
    ),
    min_size=1,
    max_size=40,
)


@pytest.mark.property
class TestLedgerInvariant:
    """The cash identity must hold for every reachable sequence of orders.

    Scenario tests confirm the identity on shapes someone thought to write down. These
    confirm it on shapes nobody did — including the ones that produce reversals, repeated
    adds, and full closes at a loss, which is where an accounting error would hide.
    """

    @settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(operations=order_operations)
    def test_cash_equals_initial_plus_sum_of_fill_deltas(
        self, operations: Sequence[tuple[bool, int, int]], instrument: InstrumentSpec
    ) -> None:
        """Directive 6, universally quantified over order sequences."""
        portfolio = replay_operations(operations, instrument)
        ledger_total = sum((fill.cash_delta or ZERO for fill in portfolio.ledger), ZERO)
        assert portfolio.cash == INITIAL_CASH + ledger_total

    @settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(operations=order_operations)
    def test_reconciliation_reports_zero_discrepancy(
        self, operations: Sequence[tuple[bool, int, int]], instrument: InstrumentSpec
    ) -> None:
        """The engine's own check agrees with the external one."""
        portfolio = replay_operations(operations, instrument)
        report = portfolio.reconcile()
        assert report.discrepancy == ZERO
        assert report.passed

    @settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(operations=order_operations)
    def test_every_fill_is_booked_and_internally_consistent(
        self, operations: Sequence[tuple[bool, int, int]], instrument: InstrumentSpec
    ) -> None:
        """No fill reaches the ledger without a cash effect attached."""
        portfolio = replay_operations(operations, instrument)
        for fill in portfolio.ledger:
            assert fill.is_booked
            fill.assert_internally_consistent()

    @settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(operations=order_operations)
    def test_closed_trade_pnl_reconciles_against_the_ledger(
        self, operations: Sequence[tuple[bool, int, int]], instrument: InstrumentSpec
    ) -> None:
        """Trade-level and fill-level accounting must not drift apart.

        Metrics count trades and reconciliation sums fills (C-4). If the two views could
        disagree, every reported performance figure would be unverifiable.
        """
        portfolio = replay_operations(operations, instrument)
        for trade in portfolio.closed_trades:
            if trade.is_open_at_backtest_end:
                continue
            assert trade.net_pnl == sum((fill.cash_delta or ZERO for fill in trade.fills), ZERO)
            assert trade.net_pnl == trade.gross_pnl - trade.total_costs

    @settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(operations=order_operations)
    def test_no_fill_is_orphaned_from_a_trade(
        self, operations: Sequence[tuple[bool, int, int]], instrument: InstrumentSpec
    ) -> None:
        """Every cash movement is attributable to a position lifecycle."""
        portfolio = replay_operations(operations, instrument)
        assert portfolio.reconcile().orphaned_fills == ()

    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(operations=order_operations)
    def test_open_position_quantity_is_never_negative(
        self, operations: Sequence[tuple[bool, int, int]], instrument: InstrumentSpec
    ) -> None:
        """Quantity is magnitude; side is carried by direction.

        A negative quantity would mean the sign convention had leaked into the size field,
        which would flip PnL on that position without raising anything.
        """
        portfolio = replay_operations(operations, instrument)
        if portfolio.position is not None:
            assert portfolio.position.quantity > ZERO


# --------------------------------------------------------------------------------------
# Ledger guards
# --------------------------------------------------------------------------------------


class TestLedgerGuards:
    """A fill the portfolio cannot legitimately book must be refused, not absorbed."""

    def test_double_booking_is_refused(
        self, portfolio: Portfolio, make_fill: Callable[..., ExecutionFill]
    ) -> None:
        """Booking the same fill twice would move cash twice for one execution."""
        trade_id = portfolio.allocate_trade_id()
        fill = make_fill("0.10", "95.000", Direction.LONG, trade_id, is_entry=True)
        booked = portfolio.apply_fill(fill, bar_index=0).booked_fills[0]
        with pytest.raises(PortfolioError, match="already been booked"):
            portfolio.apply_fill(booked, bar_index=0)

    def test_zero_quantity_fill_is_refused(
        self, portfolio: Portfolio, make_fill: Callable[..., ExecutionFill]
    ) -> None:
        """A zero-size fill is a trade that did not happen; recording it inflates counts."""
        trade_id = portfolio.allocate_trade_id()
        with pytest.raises(PortfolioError, match="non-positive quantity"):
            portfolio.apply_fill(
                make_fill("0", "95.000", Direction.LONG, trade_id, is_entry=True),
                bar_index=0,
            )

    def test_foreign_instrument_fill_is_refused(
        self, portfolio: Portfolio, make_fill: Callable[..., ExecutionFill]
    ) -> None:
        """A portfolio must not silently absorb another instrument's execution."""
        from dataclasses import replace

        trade_id = portfolio.allocate_trade_id()
        fill = make_fill("0.10", "95.000", Direction.LONG, trade_id, is_entry=True)
        with pytest.raises(PortfolioError, match="different instrument"):
            portfolio.apply_fill(replace(fill, instrument_id=InstrumentId("CADJPY")), bar_index=0)

    def test_adding_to_a_position_reweights_the_entry_price(
        self, portfolio: Portfolio, make_fill: Callable[..., ExecutionFill]
    ) -> None:
        """0.10 at 95.000 plus 0.10 at 97.000 averages to 96.000."""
        trade_id = portfolio.allocate_trade_id()
        portfolio.apply_fill(
            make_fill("0.10", "95.000", Direction.LONG, trade_id, is_entry=True),
            bar_index=0,
        )
        portfolio.apply_fill(
            make_fill("0.10", "97.000", Direction.LONG, trade_id, is_entry=True, index=1),
            bar_index=1,
        )
        position = portfolio.position
        assert position is not None
        assert position.avg_entry_price == Decimal("96.000")
        assert position.quantity == Decimal("0.20")
        assert position.entry_fill_count == 2

    def test_adds_stay_within_one_trade_lifecycle(
        self, portfolio: Portfolio, make_fill: Callable[..., ExecutionFill]
    ) -> None:
        """Scaling in is one trade, closed once."""
        trade_id = portfolio.allocate_trade_id()
        for index in range(3):
            portfolio.apply_fill(
                make_fill("0.10", "95.000", Direction.LONG, trade_id, is_entry=True, index=index),
                bar_index=index,
            )
        assert portfolio.allocate_trade_id() == trade_id
        portfolio.apply_fill(
            make_fill(
                "0.30",
                "96.000",
                Direction.SHORT,
                trade_id,
                is_entry=False,
                exit_reason=ExitReason.SIGNAL_EXIT,
                index=3,
            ),
            bar_index=3,
        )
        assert len(portfolio.closed_trades) == 1
        assert portfolio.closed_trades[0].fill_count == 4
