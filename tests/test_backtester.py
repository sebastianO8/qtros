"""Run-loop tests (ARCH-001 C-10; ASS-001; PI rulings G-2, G-3, G-4).

Bars are built with explicit OHLC so every fill price and stop trigger is hand-checkable.
The scripted strategy emits exactly the intents a test asks for, at the bars it names, so
the loop — not the strategy — is what is under test.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal

import polars as pl
import pytest

from qtros.alpha import StrategyBase, StrategyProvenanceError
from qtros.configuration import RiskLimitsConfig
from qtros.domain.enums import Direction, ExitReason, OrderRejectionCode, RiskEventKind
from qtros.domain.feature_contracts import (
    Causality,
    FeatureFrame,
    FeatureSpec,
    LagPolicy,
    SealedFeature,
    SealedFeatureSet,
    WarmupPlan,
)
from qtros.domain.identifiers import SequentialIdFactory
from qtros.domain.instrument import InstrumentSpec
from qtros.domain.orders import TradeIntent
from qtros.domain.signals import BarContext, StrategyDecision, StrategyState
from qtros.engine import Backtester, BacktestResult, Portfolio
from qtros.execution.broker import SimulatedBroker
from qtros.execution.friction import StandardFrictionModel
from qtros.risk import RiskEngine

from tests.conftest import BAR_INTERVAL, EPOCH, INITIAL_CASH, passing_audit

ZERO = Decimal("0")
FEATURE = "bar_marker"

# --------------------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------------------


def frame_from_bars(
    bars: Sequence[tuple[str, str, str, str]],
    *,
    sealed: bool = True,
    extra_columns: Mapping[str, Sequence[float]] | None = None,
) -> FeatureFrame:
    """Build a sealed feature frame from explicit (open, high, low, close) tuples."""
    rows = len(bars)
    data: dict[str, object] = {
        "ts_utc": [EPOCH + BAR_INTERVAL * i for i in range(rows)],
        "open": [float(b[0]) for b in bars],
        "high": [float(b[1]) for b in bars],
        "low": [float(b[2]) for b in bars],
        "close": [float(b[3]) for b in bars],
        "volume": [1000.0] * rows,
        "is_synthetic": [False] * rows,
        FEATURE: [float(i) for i in range(rows)],
    }
    if extra_columns:
        data.update({k: list(v) for k, v in extra_columns.items()})
    frame = pl.DataFrame(data).with_columns(pl.col("ts_utc").dt.replace_time_zone("UTC"))
    spec = FeatureSpec(
        name=FEATURE, version="1.0.0", parameters={}, output_columns=(FEATURE,),
        depends_on=("close",), causality=Causality.CAUSAL,
    )
    seal = SealedFeatureSet(
        {
            FEATURE: SealedFeature(
                column=FEATURE, transform_name=FEATURE, transform_version="1.0.0",
                spec_hash=spec.spec_hash(), warmup_bars=0, causality=Causality.CAUSAL,
            )
        }
    )
    return FeatureFrame(
        frame=frame,
        specs=(spec,),
        warmup=WarmupPlan(total_bars=0, per_transform={}, session_columns=()),
        rows_trimmed=0,
        lag_policy=LagPolicy.DEFER_TO_ENGINE,
        availability_shift_applied=0,
        lag_audit=passing_audit(rows, (FEATURE,)),
        feature_columns=(FEATURE,),
        sealed_features=seal if sealed else None,
    )


def flat_bars(count: int, price: str = "95.000") -> list[tuple[str, str, str, str]]:
    return [(price, price, price, price)] * count


@dataclass(frozen=True)
class Script:
    """What a scripted strategy emits at a bar."""

    direction: Direction = Direction.LONG
    is_exit: bool = False
    stop: str | None = None
    target: str | None = None
    quantity: str | None = None


class Scripted(StrategyBase):
    """Emits exactly the scripted intents at the scripted bar indices."""

    strategy_id = "scripted"
    strategy_version = "0.0.1"

    def __init__(self, script: Mapping[int, Sequence[Script]], features: frozenset[str] = frozenset({FEATURE})) -> None:
        super().__init__({})
        self._script = dict(script)
        self._features = features
        self.seen: list[tuple[int, bool, int]] = []  # (bar_index, is_flat, feature as int)

    @property
    def required_features(self) -> frozenset[str]:
        return self._features

    def evaluate(self, ctx: BarContext, state: StrategyState) -> StrategyDecision:
        marker = ctx.features[FEATURE]
        self.seen.append((ctx.bar_index, ctx.portfolio.is_flat, int(marker) if marker is not None else -1))
        intents = tuple(
            TradeIntent(
                intent_id=f"{ctx.bar_index}-{n}",
                ts_utc=ctx.ts_utc,
                instrument_id=ctx.instrument_id,
                direction=s.direction,
                is_exit=s.is_exit,
                requested_quantity=Decimal(s.quantity) if s.quantity else None,
                stop_loss=Decimal(s.stop) if s.stop else None,
                take_profit=Decimal(s.target) if s.target else None,
                exit_reason=ExitReason.SIGNAL_EXIT if s.is_exit else None,
                mechanism_tag="" if s.is_exit else "scripted",
            )
            for n, s in enumerate(self._script.get(ctx.bar_index, ()))
        )
        return StrategyDecision(state=state.advanced(), intents=intents)


def limits(**overrides: object) -> RiskLimitsConfig:
    base: dict[str, object] = {
        "risk_limits_id": "test", "max_drawdown_pct": "10", "risk_per_trade_pct": "1",
        "atr_stop_multiple": "2", "atr_lookback_bars": 14, "max_lot": "5",
        "max_notional_pct": "1000", "on_limit_breach": "HALT_RUN",
    }
    base.update(overrides)
    return RiskLimitsConfig(**base)  # type: ignore[arg-type]


def run(
    instrument: InstrumentSpec,
    bars: Sequence[tuple[str, str, str, str]],
    script: Mapping[int, Sequence[Script]],
    *,
    risk_limits: RiskLimitsConfig | None = None,
    prefix: str = "run00001",
) -> tuple[BacktestResult, Scripted]:
    ids = SequentialIdFactory(prefix)
    strategy = Scripted(script)
    engine = Backtester(
        feature_frame=frame_from_bars(bars),
        strategy=strategy,
        instrument=instrument,
        portfolio=Portfolio(instrument, INITIAL_CASH, ids),
        broker=SimulatedBroker(StandardFrictionModel(slippage_ticks=Decimal("0.5")), ids),
        risk=RiskEngine(risk_limits or limits(), instrument, ids, initial_equity=INITIAL_CASH),
        ids=ids,
    )
    return engine.run(), strategy


# --------------------------------------------------------------------------------------
# G-3 ordering and ASS-001 next-bar-open
# --------------------------------------------------------------------------------------


class TestRunLoopOrder:
    def test_decision_at_t_fills_at_the_open_of_t_plus_one(self, instrument: InstrumentSpec) -> None:
        bars = [("95.000",) * 4, ("95.000",) * 4, ("95.000",) * 4, ("96.000", "96.000", "96.000", "96.000"), ("96.000",) * 4]
        result, _ = run(instrument, bars, {2: [Script(quantity="0.10")]})
        assert len(result.fills) == 1
        fill = result.fills[0]
        assert fill.ts_utc == EPOCH + BAR_INTERVAL * 3
        assert fill.reference_price == Decimal("96.000")
        assert fill.fill_price > fill.reference_price  # adverse friction on a buy

    def test_the_strategy_sees_the_fill_from_the_open_of_the_same_bar(self, instrument: InstrumentSpec) -> None:
        """Step 1 (execute) precedes step 5 (evaluate): at bar 3 the book is already long."""
        _, strategy = run(instrument, flat_bars(6), {2: [Script(quantity="0.10")]})
        flat_by_bar = {index: is_flat for index, is_flat, _ in strategy.seen}
        assert flat_by_bar[2] is True
        assert flat_by_bar[3] is False

    def test_features_are_t_minus_one(self, instrument: InstrumentSpec) -> None:
        _, strategy = run(instrument, flat_bars(6), {})
        assert all(marker == index - 1 for index, _, marker in strategy.seen)
        assert [index for index, _, _ in strategy.seen] == [1, 2, 3, 4]  # bar 0 lagless, bar 5 unfillable

    def test_no_decision_on_the_final_bar(self, instrument: InstrumentSpec) -> None:
        result, strategy = run(instrument, flat_bars(5), {4: [Script(quantity="0.10")]})
        assert result.fills == ()
        assert all(index != 4 for index, _, _ in strategy.seen)
        assert result.decisions_taken == 3

    def test_signal_exit_closes_the_lifecycle(self, instrument: InstrumentSpec) -> None:
        result, _ = run(
            instrument, flat_bars(8),
            {1: [Script(quantity="0.10")], 4: [Script(direction=Direction.SHORT, is_exit=True)]},
        )
        assert len(result.closed_trades) == 1
        trade = result.closed_trades[0]
        assert trade.exit_reason is ExitReason.SIGNAL_EXIT
        assert trade.fills[1].ts_utc == EPOCH + BAR_INTERVAL * 5
        assert result.open_trade_at_end is None

    def test_exit_then_entry_in_one_bar_starts_a_new_lifecycle(self, instrument: InstrumentSpec) -> None:
        result, _ = run(
            instrument, flat_bars(8),
            {
                1: [Script(quantity="0.10")],
                3: [Script(direction=Direction.SHORT, is_exit=True), Script(quantity="0.10")],
            },
        )
        assert len(result.closed_trades) == 1
        assert result.open_trade_at_end is not None
        assert result.open_trade_at_end.trade_id != result.closed_trades[0].trade_id
        assert len({f.trade_id for f in result.fills}) == 2


# --------------------------------------------------------------------------------------
# Protective exits (G-3 step 2)
# --------------------------------------------------------------------------------------


class TestProtectiveExits:
    def test_stop_fires_at_the_level_before_the_strategy_is_consulted(self, instrument: InstrumentSpec) -> None:
        bars = [
            ("95.000", "95.000", "95.000", "95.000"),
            ("95.000", "95.000", "95.000", "95.000"),  # decide here → fill at bar 2 open
            ("95.000", "95.100", "94.900", "95.000"),
            ("95.000", "95.050", "94.400", "94.600"),  # low through the 94.500 stop
            ("94.600", "94.600", "94.600", "94.600"),
            ("94.600", "94.600", "94.600", "94.600"),
        ]
        result, strategy = run(instrument, bars, {1: [Script(quantity="0.10", stop="94.500")]})
        trade = result.closed_trades[0]
        assert trade.exit_reason is ExitReason.STOP_LOSS
        exit_fill = trade.fills[-1]
        assert exit_fill.ts_utc == EPOCH + BAR_INTERVAL * 3
        assert exit_fill.reference_price == Decimal("94.500")
        assert exit_fill.fill_price < exit_fill.reference_price  # sell degraded adversely
        # The strategy's view at bar 3 is already flat: step 2 ran before step 5.
        assert dict((i, f) for i, f, _ in strategy.seen)[3] is True

    def test_gap_through_the_stop_fills_at_the_open(self, instrument: InstrumentSpec) -> None:
        bars = [*flat_bars(2), ("95.000",) * 4, ("94.000", "94.100", "93.900", "94.000"), *flat_bars(2, "94.000")]
        result, _ = run(instrument, bars, {1: [Script(quantity="0.10", stop="94.500")]})
        exit_fill = result.closed_trades[0].fills[-1]
        assert exit_fill.reference_price == Decimal("94.000")

    def test_target_fires(self, instrument: InstrumentSpec) -> None:
        bars = [*flat_bars(2), ("95.000",) * 4, ("95.000", "95.700", "95.000", "95.500"), *flat_bars(2, "95.500")]
        result, _ = run(instrument, bars, {1: [Script(quantity="0.10", target="95.600")]})
        trade = result.closed_trades[0]
        assert trade.exit_reason is ExitReason.TAKE_PROFIT
        assert trade.fills[-1].reference_price == Decimal("95.600")

    def test_ambiguous_bar_resolves_to_the_stop(self, instrument: InstrumentSpec) -> None:
        bars = [*flat_bars(2), ("95.000",) * 4, ("95.000", "95.700", "94.400", "95.000"), *flat_bars(2)]
        result, _ = run(instrument, bars, {1: [Script(quantity="0.10", stop="94.500", target="95.600")]})
        assert result.closed_trades[0].exit_reason is ExitReason.STOP_LOSS

    def test_entry_filled_at_open_can_be_stopped_in_the_same_bar(self, instrument: InstrumentSpec) -> None:
        bars = [*flat_bars(2), ("95.000", "95.000", "94.300", "94.400"), *flat_bars(3, "94.400")]
        result, _ = run(instrument, bars, {1: [Script(quantity="0.10", stop="94.500")]})
        trade = result.closed_trades[0]
        assert trade.fills[0].ts_utc == trade.fills[-1].ts_utc
        assert trade.exit_reason is ExitReason.STOP_LOSS


# --------------------------------------------------------------------------------------
# G-2 halt inside the loop
# --------------------------------------------------------------------------------------


class TestRiskHalt:
    def test_intrabar_breach_halts_and_blocks_later_entries_but_not_the_exit(self, instrument: InstrumentSpec) -> None:
        # 1.00 lot long from 95.000; a low of 93.800 is -120,000 (12%) intrabar.
        bars = [
            *flat_bars(2),
            ("95.000",) * 4,                               # entry fills here (bar 2)
            ("95.000", "95.100", "93.800", "95.000"),      # breach intrabar, close flat
            ("95.000",) * 4,                               # entry attempt → RISK_HALTED
            ("95.000",) * 4,                               # exit attempt → approved
            ("95.000",) * 4,
            ("95.000",) * 4,
        ]
        result, _ = run(
            instrument, bars,
            {1: [Script(quantity="1.00")], 4: [Script(quantity="0.10")], 5: [Script(direction=Direction.SHORT, is_exit=True)]},
        )
        halts = [e for e in result.risk_events if e.kind is RiskEventKind.DRAWDOWN_HALT]
        assert len(halts) == 1 and halts[0].bar_index == 3
        assert result.halted_at_end
        codes = [r.code for r in result.rejected_intents]
        assert codes == [OrderRejectionCode.RISK_HALTED]
        assert len(result.closed_trades) == 1
        assert result.closed_trades[0].exit_reason is ExitReason.SIGNAL_EXIT


# --------------------------------------------------------------------------------------
# C-10 reconciliation, determinism, G-4
# --------------------------------------------------------------------------------------


class TestRunInvariants:
    def test_every_run_reconciles_exactly(self, instrument: InstrumentSpec) -> None:
        bars = [
            *flat_bars(2), ("95.000",) * 4, ("95.300", "95.600", "95.100", "95.400"),
            ("95.400", "95.400", "94.900", "95.000"), ("95.000",) * 4, ("96.000",) * 4, ("96.000",) * 4,
        ]
        result, _ = run(
            instrument, bars,
            {
                1: [Script(quantity="0.30")],
                3: [Script(direction=Direction.SHORT, is_exit=True, quantity="0.10")],
                4: [Script(direction=Direction.SHORT, quantity="0.50")],  # reversal
                6: [Script(direction=Direction.LONG, is_exit=True)],
            },
        )
        assert result.reconciliation.passed and result.trusted
        assert sum((f.cash_delta or ZERO) for f in result.fills) == result.final_cash - INITIAL_CASH

    def test_identical_inputs_produce_identical_runs(self, instrument: InstrumentSpec) -> None:
        bars = [*flat_bars(2), ("95.000",) * 4, ("95.000", "95.700", "94.400", "95.000"), *flat_bars(3)]
        script = {1: [Script(quantity="0.10", stop="94.500", target="95.600")]}
        first, _ = run(instrument, bars, script)
        second, _ = run(instrument, bars, script)
        assert [f.fill_id for f in first.fills] == [f.fill_id for f in second.fills]
        assert [t.trade_id for t in first.trades] == [t.trade_id for t in second.trades]
        assert first.final_cash == second.final_cash
        assert first.equity_curve == second.equity_curve

    def test_open_position_at_end_is_flagged_not_closed(self, instrument: InstrumentSpec) -> None:
        result, _ = run(instrument, flat_bars(5), {1: [Script(quantity="0.10")]})
        assert result.closed_trades == ()
        assert result.open_trade_at_end is not None
        assert len(result.fills) == 1
        assert result.reconciliation.passed

    def test_unsealed_frame_is_refused_before_the_run_starts(self, instrument: InstrumentSpec) -> None:
        ids = SequentialIdFactory("x")
        with pytest.raises(StrategyProvenanceError):
            Backtester(
                feature_frame=frame_from_bars(flat_bars(4), sealed=False),
                strategy=Scripted({}),
                instrument=instrument,
                portfolio=Portfolio(instrument, INITIAL_CASH, ids),
                broker=SimulatedBroker(StandardFrictionModel(), ids),
                risk=RiskEngine(limits(), instrument, ids, initial_equity=INITIAL_CASH),
                ids=ids,
            )

    def test_undeclared_feature_is_refused_before_the_run_starts(self, instrument: InstrumentSpec) -> None:
        ids = SequentialIdFactory("x")
        with pytest.raises(StrategyProvenanceError):
            Backtester(
                feature_frame=frame_from_bars(flat_bars(4)),
                strategy=Scripted({}, features=frozenset({FEATURE, "ghost"})),
                instrument=instrument,
                portfolio=Portfolio(instrument, INITIAL_CASH, ids),
                broker=SimulatedBroker(StandardFrictionModel(), ids),
                risk=RiskEngine(limits(), instrument, ids, initial_equity=INITIAL_CASH),
                ids=ids,
            )

    def test_result_records_provenance(self, instrument: InstrumentSpec) -> None:
        result, _ = run(instrument, flat_bars(4), {})
        assert result.sealed_columns == (FEATURE,)
        assert len(result.feature_set_hash) == 64
        assert result.bars_processed == 4
        assert len(result.equity_curve) == 4
