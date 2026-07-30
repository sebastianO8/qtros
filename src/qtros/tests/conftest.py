"""Shared fixtures for the engine and execution test suites.

Fixture design rules for this project:

* **Numbers are chosen to be hand-checkable.** A tick size of 0.001, a contract size of
  100,000, and a two-tick spread mean every expected value in these tests can be derived
  on paper. A fixture with awkward constants produces tests that can only be verified by
  running the code they are meant to check, which is circular.
* **Nothing is randomised outside the property tests.** Deterministic fixtures mean a
  failure reproduces on the first attempt.
* **Identifiers come from the run's own factory.** The same deterministic sequence the
  engine uses, so tests exercise the real identifier path rather than a stand-in.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import polars as pl
import pytest

from qtros.domain.enums import AssetClass, Direction, ExitReason, Timeframe
from qtros.domain.execution import ExecutionFill
from qtros.domain.feature_contracts import (
    Causality,
    FeatureFrame,
    FeatureSpec,
    LagAuditReport,
    LagPolicy,
    WarmupPlan,
)
from qtros.domain.identifiers import FillId, InstrumentId, SequentialIdFactory, TradeId
from qtros.domain.instrument import InstrumentSpec
from qtros.domain.market import BarData
from qtros.domain.orders import RISK_APPROVAL, ApprovedOrder, TradeIntent
from qtros.engine.portfolio import Portfolio
from qtros.execution.broker import SimulatedBroker
from qtros.execution.friction import StandardFrictionModel

INITIAL_CASH: Decimal = Decimal("1000000")
"""Deliberately large so that no test fails for lack of capital rather than for the
reason it was written to check."""

EPOCH: datetime = datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
BAR_INTERVAL: timedelta = Timeframe.M5.duration


# --------------------------------------------------------------------------------------
# Instruments
# --------------------------------------------------------------------------------------


@pytest.fixture
def instrument() -> InstrumentSpec:
    """Return an AUD/JPY specification with hand-checkable contract arithmetic.

    A two-tick spread gives a one-tick half-spread of 0.001, and a 100,000 contract size
    makes one tick worth 100 units of quote currency per lot. Every friction figure in
    these tests follows from those two numbers.
    """
    return InstrumentSpec(
        instrument_id=InstrumentId("AUDJPY"),
        display_name="Australian Dollar / Japanese Yen",
        asset_class=AssetClass.FX_SPOT,
        base_currency="AUD",
        quote_currency="JPY",
        tick_size=Decimal("0.001"),
        contract_size=Decimal("100000"),
        min_lot=Decimal("0.01"),
        lot_step=Decimal("0.01"),
        typical_spread_ticks=Decimal("2"),
        commission_per_lot=Decimal("6"),
        calendar_id="fx_sunday_open",
        rollover_time_local=time(17, 0),
        rollover_tz="America/New_York",
    )


@pytest.fixture
def frictionless_instrument(instrument: InstrumentSpec) -> InstrumentSpec:
    """Return the same instrument with all costs zeroed.

    Used only by the benchmark that isolates pure price return. Never a default: a
    frictionless configuration violates Directive 2 and exists here solely so that the
    friction-inclusive benchmark can be compared against a known baseline.
    """
    from dataclasses import replace

    return replace(
        instrument,
        typical_spread_ticks=Decimal("0"),
        commission_per_lot=Decimal("0"),
    )


# --------------------------------------------------------------------------------------
# Engine components
# --------------------------------------------------------------------------------------


@pytest.fixture
def ids() -> SequentialIdFactory:
    """Return a deterministic identifier factory."""
    return SequentialIdFactory("test0001")


@pytest.fixture
def portfolio(instrument: InstrumentSpec, ids: SequentialIdFactory) -> Portfolio:
    """Return a funded, flat portfolio."""
    return Portfolio(instrument, INITIAL_CASH, ids)


@pytest.fixture
def friction() -> StandardFrictionModel:
    """Return the standard friction model with half a tick of slippage."""
    return StandardFrictionModel(slippage_ticks=Decimal("0.5"))


@pytest.fixture
def broker(friction: StandardFrictionModel, ids: SequentialIdFactory) -> SimulatedBroker:
    """Return a broker with Phase 1 defaults: next-bar open, adverse-first."""
    return SimulatedBroker(friction, ids)


# --------------------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------------------


@pytest.fixture
def make_bar() -> Callable[..., BarData]:
    """Return a factory building a bar at a given index.

    The default bar is flat — open, high, low, and close all equal — so a test that cares
    only about a fill price does not have to invent a range, and a test that does care
    states its own.
    """

    def factory(
        index: int = 0,
        price: str | Decimal = "95.000",
        *,
        high: str | Decimal | None = None,
        low: str | Decimal | None = None,
        close: str | Decimal | None = None,
        volume: str | Decimal | None = "1000",
    ) -> BarData:
        open_price = Decimal(price)
        return BarData(
            ts_utc=EPOCH + BAR_INTERVAL * index,
            open=open_price,
            high=Decimal(high) if high is not None else max(open_price, Decimal(close or price)),
            low=Decimal(low) if low is not None else min(open_price, Decimal(close or price)),
            close=Decimal(close) if close is not None else open_price,
            volume=Decimal(volume) if volume is not None else None,
        )

    return factory


@pytest.fixture
def make_order(
    instrument: InstrumentSpec, ids: SequentialIdFactory
) -> Callable[..., ApprovedOrder]:
    """Return a factory building risk-approved orders.

    The factory holds the only reference to :data:`RISK_APPROVAL` in the test suite, which
    mirrors production: the risk engine is the sole minter of approvals, and everything
    else receives one already granted.
    """

    def factory(
        direction: Direction,
        quantity: str | Decimal,
        trade_id: TradeId,
        *,
        is_exit: bool = False,
        exit_reason: ExitReason | None = None,
        index: int = 0,
    ) -> ApprovedOrder:
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

    return factory


@pytest.fixture
def make_fill(
    instrument: InstrumentSpec, ids: SequentialIdFactory
) -> Callable[..., ExecutionFill]:
    """Return a factory building fills directly, bypassing the broker.

    Ledger tests use this so that a portfolio failure cannot be masked or caused by
    matching-engine behaviour. Costs default to values that are obviously not zero, since
    a ledger test that happens to pass with zero friction proves very little.
    """

    def factory(
        quantity: str | Decimal,
        price: str | Decimal,
        direction: Direction,
        trade_id: TradeId,
        *,
        is_entry: bool,
        exit_reason: ExitReason | None = None,
        index: int = 0,
        commission: str | Decimal = "3",
        spread_cost: str | Decimal = "100",
        slippage_cost: str | Decimal = "50",
    ) -> ExecutionFill:
        return ExecutionFill(
            fill_id=ids.next_fill_id(),
            order_id=f"order-{index}",
            trade_id=trade_id,
            instrument_id=instrument.instrument_id,
            ts_utc=EPOCH + BAR_INTERVAL * index,
            direction=direction,
            quantity=Decimal(quantity),
            reference_price=Decimal(price),
            fill_price=Decimal(price),
            commission=Decimal(commission),
            spread_cost=Decimal(spread_cost),
            slippage_cost=Decimal(slippage_cost),
            is_entry=is_entry,
            exit_reason=exit_reason,
        )

    return factory


# --------------------------------------------------------------------------------------
# Feature frames (for the T-1 boundary tests)
# --------------------------------------------------------------------------------------


def passing_audit(rows: int, columns: Sequence[str]) -> LagAuditReport:
    """Return a report standing in for a passed prefix-invariance audit.

    Args:
        rows: Rows in the audited frame.
        columns: Feature columns checked.

    Returns:
        A passing report. Fabricated deliberately: these tests verify the *context
        boundary*, and the audit itself is verified by its own suite plus the non-causal
        control. Running a real audit here would couple two independent guarantees.
    """
    return LagAuditReport(
        passed=True,
        transforms_audited=("stub",),
        probe_indices=tuple(range(min(rows, 5))),
        findings=(),
        columns_checked=tuple(columns),
        tolerance=0.0,
        rows_in_frame=rows,
        audited_at_utc=EPOCH,
    )


def failing_audit(rows: int, columns: Sequence[str]) -> LagAuditReport:
    """Return a report standing in for a failed prefix-invariance audit."""
    from qtros.domain.feature_contracts import LagAuditFinding

    return LagAuditReport(
        passed=False,
        transforms_audited=("stub",),
        probe_indices=(1,),
        findings=(
            LagAuditFinding(
                transform_name="stub",
                column=columns[0],
                probe_index=1,
                full_frame_value="1.0",
                prefix_value="2.0",
                detail="deliberate control failure",
            ),
        ),
        columns_checked=tuple(columns),
        tolerance=0.0,
        rows_in_frame=rows,
        audited_at_utc=EPOCH,
    )


@pytest.fixture
def build_feature_frame() -> Callable[..., FeatureFrame]:
    """Return a factory building feature frames for context tests.

    Feature values default to the bar's own index, which is what makes the T-1 assertions
    unambiguous: if the context at bar 7 reports a feature value of 6, the shift is
    correct, and if it reports 7 the boundary has failed. No arithmetic or tolerance is
    involved in reading that result.
    """

    def factory(
        rows: int = 10,
        *,
        feature_values: Mapping[str, Sequence[float | None]] | None = None,
        prices: Sequence[str] | None = None,
        shift_applied: int = 0,
        lag_policy: LagPolicy = LagPolicy.DEFER_TO_ENGINE,
        audit: LagAuditReport | None = None,
        session_index: Sequence[int] | None = None,
    ) -> FeatureFrame:
        features: Mapping[str, Sequence[float | None]] = feature_values or {
            "bar_marker": [float(index) for index in range(rows)]
        }
        opens: list[str] = list(prices) if prices else [f"{95 + index}.000" for index in range(rows)]

        data: dict[str, object] = {
            "ts_utc": [EPOCH + BAR_INTERVAL * index for index in range(rows)],
            "open": [float(value) for value in opens],
            "high": [float(value) + 0.5 for value in opens],
            "low": [float(value) - 0.5 for value in opens],
            "close": [float(value) + 0.25 for value in opens],
            "volume": [1000.0] * rows,
            "is_synthetic": [False] * rows,
        }
        data.update({name: list(values) for name, values in features.items()})
        if session_index is not None:
            data["session_index"] = list(session_index)

        frame = pl.DataFrame(data).with_columns(
            pl.col("ts_utc").dt.replace_time_zone("UTC")
            if pl.DataFrame(data).schema["ts_utc"].time_zone is None
            else pl.col("ts_utc")
        )

        specs = tuple(
            FeatureSpec(
                name=name,
                version="1.0.0",
                parameters={"column": name},
                output_columns=(name,),
                depends_on=("close",),
                causality=Causality.CAUSAL,
            )
            for name in features
        )
        columns = tuple(features)
        return FeatureFrame(
            frame=frame,
            specs=specs,
            warmup=WarmupPlan(total_bars=0, per_transform={}, session_columns=()),
            rows_trimmed=0,
            lag_policy=lag_policy,
            availability_shift_applied=shift_applied,
            lag_audit=audit if audit is not None else passing_audit(rows, columns),
            feature_columns=columns,
        )

    return factory
