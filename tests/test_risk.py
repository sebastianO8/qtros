"""Risk gate tests (ARCH-001 §1.5; PI rulings G-1, G-2; ASS-008, ASS-009)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal

import pytest

from qtros.configuration import RiskLimitsConfig
from qtros.domain.enums import (
    BreachAction,
    Direction,
    DrawdownBasis,
    ExitReason,
    OrderRejectionCode,
    RiskEventKind,
    SubMinLotPolicy,
)
from qtros.domain.errors import ConfigurationError
from qtros.domain.execution import EquitySnapshot, PositionState
from qtros.domain.identifiers import SequentialIdFactory, TradeId
from qtros.domain.instrument import InstrumentSpec
from qtros.domain.market import BarData
from qtros.domain.orders import ApprovalError, ApprovedOrder, TradeIntent
from qtros.domain.signals import PortfolioView
from qtros.risk import (
    FixedFractionalSizer,
    MaxLotRule,
    MaxNotionalExposureRule,
    RiskEngine,
    notional_value,
)

from tests.conftest import BAR_INTERVAL, EPOCH, INITIAL_CASH

ZERO = Decimal("0")


def limits(**overrides: object) -> RiskLimitsConfig:
    base: dict[str, object] = {
        "risk_limits_id": "test",
        "max_drawdown_pct": "10",
        "risk_per_trade_pct": "1",
        "atr_stop_multiple": "2",
        "atr_lookback_bars": 14,
        "max_lot": "5",
        "max_notional_pct": "1000",
        "on_limit_breach": "HALT_RUN",
    }
    base.update(overrides)
    return RiskLimitsConfig(**base)  # type: ignore[arg-type]


def flat_view(cash: Decimal = INITIAL_CASH) -> PortfolioView:
    return PortfolioView(
        cash=cash,
        closed_equity=cash,
        mtm_equity=cash,
        open_position=None,
        realized_pnl_to_date=ZERO,
        trade_count_to_date=0,
    )


def open_view(position: PositionState, cash: Decimal = INITIAL_CASH) -> PortfolioView:
    return replace(flat_view(cash), open_position=position)


def position(
    direction: Direction, quantity: str, entry: str, trade_id: str = "t-1"
) -> PositionState:
    return PositionState(
        trade_id=TradeId(trade_id),
        instrument_id=__import__("qtros.domain.identifiers", fromlist=["InstrumentId"]).InstrumentId("AUDJPY"),
        direction=direction,
        quantity=Decimal(quantity),
        avg_entry_price=Decimal(entry),
        opened_at_utc=EPOCH,
    )


def entry_intent(
    ids: SequentialIdFactory,
    instrument: InstrumentSpec,
    direction: Direction = Direction.LONG,
    *,
    stop: str | None = "94.000",
    requested: str | None = None,
    index: int = 0,
) -> TradeIntent:
    return TradeIntent(
        intent_id=ids.next_intent_id(),
        ts_utc=EPOCH + BAR_INTERVAL * index,
        instrument_id=instrument.instrument_id,
        direction=direction,
        stop_loss=Decimal(stop) if stop is not None else None,
        requested_quantity=Decimal(requested) if requested is not None else None,
        mechanism_tag="test_mechanism",
    )


def exit_intent(
    ids: SequentialIdFactory, instrument: InstrumentSpec, direction: Direction, *, requested: str | None = None
) -> TradeIntent:
    return TradeIntent(
        intent_id=ids.next_intent_id(),
        ts_utc=EPOCH,
        instrument_id=instrument.instrument_id,
        direction=direction,
        is_exit=True,
        requested_quantity=Decimal(requested) if requested is not None else None,
        exit_reason=ExitReason.SIGNAL_EXIT,
    )


def snapshot(cash: Decimal, mtm: Decimal, index: int = 0) -> EquitySnapshot:
    return EquitySnapshot(
        ts_utc=EPOCH + BAR_INTERVAL * index,
        cash=cash,
        closed_equity=cash,
        mtm_equity=mtm,
        unrealized_pnl=mtm - cash,
        open_position_count=0 if mtm == cash else 1,
    )


@pytest.fixture
def engine(instrument: InstrumentSpec, ids: SequentialIdFactory) -> RiskEngine:
    return RiskEngine(limits(), instrument, ids, initial_equity=INITIAL_CASH)


# --------------------------------------------------------------------------------------
# The gate is non-bypassable
# --------------------------------------------------------------------------------------


class TestApprovalGate:
    def test_only_the_risk_engine_mints_approved_orders(
        self, engine: RiskEngine, instrument: InstrumentSpec, ids: SequentialIdFactory, make_bar: Callable[..., BarData]
    ) -> None:
        intent = entry_intent(ids, instrument)
        result = engine.approve(intent, view=flat_view(), bar=make_bar(), trade_id=TradeId("t-1"))
        assert result.approved and result.order is not None
        assert result.order.trade_id == TradeId("t-1")

    def test_forged_approval_is_refused_by_the_type(self, instrument: InstrumentSpec, ids: SequentialIdFactory) -> None:
        intent = entry_intent(ids, instrument)
        with pytest.raises(ApprovalError):
            ApprovedOrder(
                order_id="o", intent=intent, quantity=Decimal("1"), approved_at_utc=EPOCH,
                trade_id=TradeId("t"), approval=None,
            )

    def test_rejections_are_retained_as_data(
        self, engine: RiskEngine, instrument: InstrumentSpec, ids: SequentialIdFactory, make_bar: Callable[..., BarData]
    ) -> None:
        intent = entry_intent(ids, instrument, stop=None)
        result = engine.approve(intent, view=flat_view(), bar=make_bar(), trade_id=TradeId("t-1"))
        assert not result.approved and result.rejection is not None
        assert result.rejection.code is OrderRejectionCode.NO_STOP_FOR_SIZING
        assert result.rejection.intent is intent


# --------------------------------------------------------------------------------------
# Sizing
# --------------------------------------------------------------------------------------


class TestSizing:
    def test_fixed_fractional_sizing_is_hand_checkable(
        self, engine: RiskEngine, instrument: InstrumentSpec, ids: SequentialIdFactory, make_bar: Callable[..., BarData]
    ) -> None:
        # 1% of 1,000,000 = 10,000 at risk. Stop 1.000 below a 95.000 close on a 100,000
        # contract = 100,000 per lot at the stop. 10,000 / 100,000 = 0.10 lots.
        intent = entry_intent(ids, instrument, stop="94.000")
        result = engine.approve(intent, view=flat_view(), bar=make_bar(price="95.000"), trade_id=TradeId("t"))
        assert result.order is not None
        assert result.order.quantity == Decimal("0.10")

    def test_quantity_rounds_down_to_the_lot_grid(
        self, engine: RiskEngine, instrument: InstrumentSpec, ids: SequentialIdFactory, make_bar: Callable[..., BarData]
    ) -> None:
        # Stop 0.300 away: 10,000 / 30,000 = 0.3333… → 0.33, never 0.34.
        intent = entry_intent(ids, instrument, stop="94.700")
        result = engine.approve(intent, view=flat_view(), bar=make_bar(price="95.000"), trade_id=TradeId("t"))
        assert result.order is not None
        assert result.order.quantity == Decimal("0.33")

    def test_stop_on_the_wrong_side_is_rejected(
        self, engine: RiskEngine, instrument: InstrumentSpec, ids: SequentialIdFactory, make_bar: Callable[..., BarData]
    ) -> None:
        intent = entry_intent(ids, instrument, Direction.LONG, stop="96.000")
        result = engine.approve(intent, view=flat_view(), bar=make_bar(price="95.000"), trade_id=TradeId("t"))
        assert result.rejection is not None
        assert result.rejection.code is OrderRejectionCode.NO_STOP_FOR_SIZING

    def test_short_sizing_uses_a_stop_above(
        self, engine: RiskEngine, instrument: InstrumentSpec, ids: SequentialIdFactory, make_bar: Callable[..., BarData]
    ) -> None:
        intent = entry_intent(ids, instrument, Direction.SHORT, stop="96.000")
        result = engine.approve(intent, view=flat_view(), bar=make_bar(price="95.000"), trade_id=TradeId("t"))
        assert result.order is not None and result.order.quantity == Decimal("0.10")

    def test_sub_minimum_lot_is_rejected_by_default(
        self, instrument: InstrumentSpec, ids: SequentialIdFactory, make_bar: Callable[..., BarData]
    ) -> None:
        small = RiskEngine(limits(), instrument, ids, initial_equity=Decimal("500"))
        intent = entry_intent(ids, instrument, stop="94.000")
        result = small.approve(intent, view=flat_view(Decimal("500")), bar=make_bar(), trade_id=TradeId("t"))
        assert result.rejection is not None
        assert result.rejection.code is OrderRejectionCode.SIZED_TO_ZERO

    def test_sub_minimum_lot_can_be_raised_only_by_explicit_policy(
        self, instrument: InstrumentSpec, ids: SequentialIdFactory, make_bar: Callable[..., BarData]
    ) -> None:
        small = RiskEngine(
            limits(), instrument, ids, initial_equity=Decimal("500"),
            sub_min_lot_policy=SubMinLotPolicy.ROUND_UP,
            rules=(),  # isolate the policy from the notional cap
        )
        intent = entry_intent(ids, instrument, stop="94.000")
        result = small.approve(intent, view=flat_view(Decimal("500")), bar=make_bar(), trade_id=TradeId("t"))
        assert result.order is not None
        assert result.order.quantity == instrument.min_lot
        assert result.order.resize_reason is not None

    def test_explicit_quantity_bypasses_the_risk_calculation_but_not_the_grid(
        self, engine: RiskEngine, instrument: InstrumentSpec, ids: SequentialIdFactory, make_bar: Callable[..., BarData]
    ) -> None:
        intent = entry_intent(ids, instrument, stop=None, requested="0.237")
        result = engine.approve(intent, view=flat_view(), bar=make_bar(), trade_id=TradeId("t"))
        assert result.order is not None and result.order.quantity == Decimal("0.23")

    def test_sizer_refuses_a_zero_or_excessive_fraction(self) -> None:
        with pytest.raises(ConfigurationError):
            FixedFractionalSizer(ZERO)
        with pytest.raises(ConfigurationError):
            FixedFractionalSizer(Decimal("1.5"))


# --------------------------------------------------------------------------------------
# G-1: exposure rules (ASS-008)
# --------------------------------------------------------------------------------------


class TestExposureRules:
    def test_max_lot_rejects_rather_than_resizes(
        self, instrument: InstrumentSpec, ids: SequentialIdFactory, make_bar: Callable[..., BarData]
    ) -> None:
        engine = RiskEngine(limits(max_lot="0.05"), instrument, ids, initial_equity=INITIAL_CASH)
        intent = entry_intent(ids, instrument, stop="94.000")  # sizes to 0.10
        result = engine.approve(intent, view=flat_view(), bar=make_bar(), trade_id=TradeId("t"))
        assert result.rejection is not None
        assert result.rejection.code is OrderRejectionCode.EXCEEDS_MAX_LOT

    def test_notional_cap_counts_the_open_position(self, instrument: InstrumentSpec) -> None:
        rule = MaxNotionalExposureRule(Decimal("1"))  # 1× equity
        held = position(Direction.LONG, "0.08", "95.000")  # 760,000 notional
        verdict = rule.evaluate(
            intent=None,  # type: ignore[arg-type]
            quantity=Decimal("0.03"),  # +285,000 → 1,045,000 > 1,000,000
            reference_price=Decimal("95.000"),
            view=open_view(held),
            spec=instrument,
        )
        assert not verdict.allowed and verdict.code is OrderRejectionCode.EXCEEDS_NOTIONAL_CAP

    def test_notional_cap_allows_at_the_boundary(self, instrument: InstrumentSpec) -> None:
        rule = MaxNotionalExposureRule(Decimal("1"))
        verdict = rule.evaluate(
            intent=None,  # type: ignore[arg-type]
            quantity=Decimal("0.10"),  # exactly 950,000 ≤ 1,000,000
            reference_price=Decimal("95.000"),
            view=flat_view(),
            spec=instrument,
        )
        assert verdict.allowed

    def test_notional_cap_through_the_engine(
        self, instrument: InstrumentSpec, ids: SequentialIdFactory, make_bar: Callable[..., BarData]
    ) -> None:
        engine = RiskEngine(limits(max_notional_pct="50"), instrument, ids, initial_equity=INITIAL_CASH)
        intent = entry_intent(ids, instrument, stop=None, requested="0.60")  # 5,700,000 > 500,000
        result = engine.approve(intent, view=flat_view(), bar=make_bar(), trade_id=TradeId("t"))
        assert result.rejection is not None
        assert result.rejection.code is OrderRejectionCode.EXCEEDS_NOTIONAL_CAP

    def test_notional_value_is_quantized_money(self, instrument: InstrumentSpec) -> None:
        assert notional_value(Decimal("0.01"), Decimal("95.0005"), instrument) == Decimal("95000.50")

    def test_rules_reject_non_positive_caps(self) -> None:
        with pytest.raises(ConfigurationError):
            MaxLotRule(ZERO)
        with pytest.raises(ConfigurationError):
            MaxNotionalExposureRule(ZERO)


# --------------------------------------------------------------------------------------
# G-2: drawdown halt (ASS-009)
# --------------------------------------------------------------------------------------


class TestDrawdownHalt:
    def test_gate_basis_must_be_mark_to_market(self, instrument: InstrumentSpec, ids: SequentialIdFactory) -> None:
        with pytest.raises(ConfigurationError, match="G-2"):
            RiskEngine(
                limits(max_drawdown_basis=DrawdownBasis.CLOSED_EQUITY), instrument, ids,
                initial_equity=INITIAL_CASH,
            )

    def test_intrabar_low_triggers_the_halt_even_when_the_close_recovers(
        self, engine: RiskEngine, instrument: InstrumentSpec, make_bar: Callable[..., BarData]
    ) -> None:
        # Long 1.00 lot from 95.000 on a 100,000 contract: 1.000 of price = 100,000 of PnL.
        # Peak 1,000,000. A low of 93.900 is -110,000 (11%) intrabar; the close at 95.500
        # is +50,000. Close-only evaluation would see a new high and no breach.
        held = position(Direction.LONG, "1.00", "95.000")
        bar = make_bar(index=1, price="95.000", high="95.600", low="93.900", close="95.500")
        event = engine.on_bar_close(
            snapshot=snapshot(INITIAL_CASH, INITIAL_CASH + Decimal("50000"), 1),
            bar=bar, position=held, bar_index=1,
        )
        assert event is not None and event.kind is RiskEventKind.DRAWDOWN_HALT
        assert event.extreme_equity == INITIAL_CASH - Decimal("110000")
        assert engine.is_halted

    def test_close_only_drawdown_below_the_limit_does_not_halt(
        self, engine: RiskEngine, make_bar: Callable[..., BarData]
    ) -> None:
        held = position(Direction.LONG, "1.00", "95.000")
        bar = make_bar(index=1, price="95.000", high="95.100", low="94.100", close="94.200")  # -9%
        event = engine.on_bar_close(
            snapshot=snapshot(INITIAL_CASH, INITIAL_CASH - Decimal("80000"), 1),
            bar=bar, position=held, bar_index=1,
        )
        assert event is None and not engine.is_halted

    def test_short_position_uses_the_bar_high(self, engine: RiskEngine, make_bar: Callable[..., BarData]) -> None:
        held = position(Direction.SHORT, "1.00", "95.000")
        bar = make_bar(index=1, price="95.000", high="96.100", low="94.900", close="95.000")
        event = engine.on_bar_close(
            snapshot=snapshot(INITIAL_CASH, INITIAL_CASH, 1), bar=bar, position=held, bar_index=1,
        )
        assert event is not None and event.kind is RiskEventKind.DRAWDOWN_HALT

    def test_peak_is_evaluated_before_it_advances(self, engine: RiskEngine, make_bar: Callable[..., BarData]) -> None:
        # Bar closes at a new high but its low breached the limit from the prior peak.
        held = position(Direction.LONG, "1.00", "95.000")
        bar = make_bar(index=1, price="95.000", high="97.000", low="93.900", close="97.000")
        event = engine.on_bar_close(
            snapshot=snapshot(INITIAL_CASH, INITIAL_CASH + Decimal("200000"), 1),
            bar=bar, position=held, bar_index=1,
        )
        assert event is not None and event.peak_equity == INITIAL_CASH
        assert engine.peak_equity == INITIAL_CASH + Decimal("200000")

    def test_halt_blocks_entries_but_not_exits(
        self, engine: RiskEngine, instrument: InstrumentSpec, ids: SequentialIdFactory, make_bar: Callable[..., BarData]
    ) -> None:
        held = position(Direction.LONG, "1.00", "95.000")
        bar = make_bar(index=1, price="95.000", high="95.000", low="93.000", close="94.000")
        engine.on_bar_close(snapshot=snapshot(INITIAL_CASH, INITIAL_CASH - Decimal("100000"), 1), bar=bar, position=held, bar_index=1)
        assert engine.is_halted

        entry = engine.approve(entry_intent(ids, instrument), view=open_view(held), bar=bar, trade_id=TradeId("t-1"))
        assert entry.rejection is not None and entry.rejection.code is OrderRejectionCode.RISK_HALTED

        close = engine.approve(exit_intent(ids, instrument, Direction.SHORT), view=open_view(held), bar=bar, trade_id=TradeId("t-1"))
        assert close.order is not None and close.order.quantity == Decimal("1.00")
        assert close.order.is_exit

    def test_run_halt_does_not_release(self, engine: RiskEngine, make_bar: Callable[..., BarData]) -> None:
        held = position(Direction.LONG, "1.00", "95.000")
        bar = make_bar(index=1, price="95.000", high="95.000", low="93.000", close="94.000")
        engine.on_bar_close(snapshot=snapshot(INITIAL_CASH, INITIAL_CASH - Decimal("100000"), 1), bar=bar, position=held, bar_index=1, session_index=0)
        engine.on_bar_close(snapshot=snapshot(INITIAL_CASH, INITIAL_CASH, 2), bar=make_bar(index=2), position=None, bar_index=2, session_index=1)
        assert engine.is_halted

    def test_session_halt_releases_at_the_next_session(
        self, instrument: InstrumentSpec, ids: SequentialIdFactory, make_bar: Callable[..., BarData]
    ) -> None:
        engine = RiskEngine(limits(on_limit_breach=BreachAction.HALT_SESSION), instrument, ids, initial_equity=INITIAL_CASH)
        held = position(Direction.LONG, "1.00", "95.000")
        bar = make_bar(index=1, price="95.000", high="95.000", low="93.000", close="94.000")
        engine.on_bar_close(snapshot=snapshot(INITIAL_CASH, INITIAL_CASH - Decimal("100000"), 1), bar=bar, position=held, bar_index=1, session_index=0)
        assert engine.is_halted
        same = engine.on_bar_close(snapshot=snapshot(INITIAL_CASH, INITIAL_CASH, 2), bar=make_bar(index=2), position=None, bar_index=2, session_index=0)
        assert same is None and engine.is_halted
        released = engine.on_bar_close(snapshot=snapshot(INITIAL_CASH, INITIAL_CASH, 3), bar=make_bar(index=3), position=None, bar_index=3, session_index=1)
        assert released is not None and released.kind is RiskEventKind.HALT_RELEASED
        assert not engine.is_halted

    def test_exit_larger_than_the_position_is_clipped(
        self, engine: RiskEngine, instrument: InstrumentSpec, ids: SequentialIdFactory, make_bar: Callable[..., BarData]
    ) -> None:
        held = position(Direction.LONG, "0.50", "95.000")
        result = engine.approve(
            exit_intent(ids, instrument, Direction.SHORT, requested="0.80"),
            view=open_view(held), bar=make_bar(), trade_id=TradeId("t-1"),
        )
        assert result.order is not None
        assert result.order.quantity == Decimal("0.50")
        assert result.order.resize_reason is not None

    def test_exit_while_flat_is_rejected(
        self, engine: RiskEngine, instrument: InstrumentSpec, ids: SequentialIdFactory, make_bar: Callable[..., BarData]
    ) -> None:
        result = engine.approve(exit_intent(ids, instrument, Direction.SHORT), view=flat_view(), bar=make_bar(), trade_id=TradeId("t"))
        assert result.rejection is not None and result.rejection.code is OrderRejectionCode.NO_OPEN_POSITION
