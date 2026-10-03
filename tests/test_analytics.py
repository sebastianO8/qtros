"""Layer 5 tests (ARCH-001 C-1, C-3, C-5; ASS-010).

Every expected value is derived by hand from small, round inputs.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest

from qtros.analytics import (
    PerformanceReport,
    analyze,
    concentration,
    daily_equity,
    drawdown,
    evaluate_gates,
    periodic_returns,
    sharpe_ratio,
    trade_statistics,
)
from qtros.analytics.attribution import attribute
from qtros.analytics.metrics import DrawdownResult, SharpeResult, TradeStatistics
from qtros.configuration import ReportingConfig, SharpeConventionConfig
from qtros.configuration.models import ConcentrationConfig, SignificancePolicyConfig
from qtros.domain.enums import (
    Direction,
    ExitReason,
    GateVerdict,
    PeriodAttribution,
    ReturnBasis,
    ReturnType,
)
from qtros.domain.execution import EquitySnapshot, ExecutionFill, Trade
from qtros.domain.identifiers import FillId, InstrumentId, TradeId
from qtros.domain.timekeeping import SessionCalendar, WeekBoundary

ZERO = Decimal("0")
EPOCH = datetime(2024, 1, 8, 0, 0, tzinfo=UTC)  # Monday
NY = "America/New_York"


@pytest.fixture(scope="module")
def calendar() -> SessionCalendar:
    return SessionCalendar(
        calendar_id="fx",
        week_open=WeekBoundary(6, time(17, 0), NY),
        week_close=WeekBoundary(4, time(17, 0), NY),
        rollover_time_local=time(17, 0),
        rollover_tz=NY,
    )


def fill(
    fill_id: str,
    trade_id: str,
    *,
    is_entry: bool,
    price: str,
    commission: str = "3",
    spread: str = "100",
    slippage: str = "50",
    ts: datetime = EPOCH,
) -> ExecutionFill:
    return ExecutionFill(
        fill_id=FillId(fill_id),
        order_id="o",
        trade_id=TradeId(trade_id),
        instrument_id=InstrumentId("AUDJPY"),
        ts_utc=ts,
        direction=Direction.LONG if is_entry else Direction.SHORT,
        quantity=Decimal("0.10"),
        reference_price=Decimal(price),
        fill_price=Decimal(price),
        commission=Decimal(commission),
        spread_cost=Decimal(spread),
        slippage_cost=Decimal(slippage),
        is_entry=is_entry,
        realized_pnl=ZERO,
        cash_delta=-Decimal(commission),
    )


def trade(
    trade_id: str,
    net: str,
    *,
    opened: datetime = EPOCH,
    closed: datetime | None = None,
    mechanism: str = "m1",
    exit_reason: ExitReason = ExitReason.SIGNAL_EXIT,
    direction: Direction = Direction.LONG,
    bars: int = 3,
    costs: str = "10",
    open_at_end: bool = False,
) -> Trade:
    net_pnl = Decimal(net)
    cost = Decimal(costs)
    closed_at = closed or opened + timedelta(hours=1)
    return Trade(
        trade_id=TradeId(trade_id),
        instrument_id=InstrumentId("AUDJPY"),
        direction=direction,
        fills=(
            fill(f"{trade_id}-e", trade_id, is_entry=True, price="95", ts=opened),
            fill(f"{trade_id}-x", trade_id, is_entry=False, price="95", ts=closed_at),
        ),
        opened_at_utc=opened,
        closed_at_utc=closed_at,
        entry_quantity=Decimal("0.10"),
        avg_entry_price=Decimal("95"),
        avg_exit_price=Decimal("95"),
        gross_pnl=net_pnl + cost,
        total_costs=cost,
        net_pnl=net_pnl,
        bars_held=bars,
        max_adverse_excursion=ZERO,
        max_favorable_excursion=ZERO,
        exit_reason=exit_reason,
        mechanism_tag=mechanism,
        is_open_at_backtest_end=open_at_end,
    )


def snap(
    index: int, cash: str, mtm: str | None = None, *, step: timedelta = timedelta(days=1)
) -> EquitySnapshot:
    c = Decimal(cash)
    m = Decimal(mtm) if mtm is not None else c
    return EquitySnapshot(
        ts_utc=EPOCH + step * index,
        cash=c,
        closed_equity=c,
        mtm_equity=m,
        unrealized_pnl=m - c,
        open_position_count=0 if m == c else 1,
    )


# --------------------------------------------------------------------------------------


class TestTradeStatistics:
    def test_hand_checked_sums(self) -> None:
        stats = trade_statistics(
            [
                trade("a", "300"),
                trade("b", "-100"),
                trade("c", "200"),
                trade("d", "-50"),
                trade("e", "0"),
                trade("f", "999", open_at_end=True),
            ]
        )
        assert (
            stats.trade_count == 5
            and stats.winners == 2
            and stats.losers == 2
            and stats.scratches == 1
        )
        assert stats.gross_profit == Decimal("500") and stats.gross_loss == Decimal("150")
        assert stats.net_pnl == Decimal("350")
        assert stats.profit_factor == Decimal("3.333333")
        assert stats.win_rate == Decimal("0.4")
        assert stats.expectancy == Decimal("70.00")
        assert stats.largest_win == Decimal("300") and stats.largest_loss == Decimal("100")
        assert stats.open_at_end

    def test_no_losses_leaves_profit_factor_undefined(self) -> None:
        stats = trade_statistics([trade("a", "10")])
        assert stats.profit_factor is None

    def test_empty(self) -> None:
        stats = trade_statistics([])
        assert stats.trade_count == 0 and stats.win_rate is None and stats.expectancy is None


class TestSharpe:
    def test_daily_equity_takes_the_last_bar_of_each_trading_day(
        self, calendar: SessionCalendar
    ) -> None:
        five_min = timedelta(minutes=5)
        curve = [
            snap(0, "100", step=five_min),
            snap(1, "101", step=five_min),
            snap(300, "102", step=five_min),
            snap(301, "103", step=five_min),
        ]  # bar 300 is next day
        points = daily_equity(curve, calendar, ReturnBasis.DAILY_CLOSED_EQUITY)
        assert [p.equity for p in points] == [Decimal("101"), Decimal("103")]

    def test_arithmetic_and_log_returns(self, calendar: SessionCalendar) -> None:
        points = daily_equity(
            [snap(0, "100"), snap(1, "110"), snap(2, "99")],
            calendar,
            ReturnBasis.DAILY_CLOSED_EQUITY,
        )
        arithmetic = periodic_returns(points, ReturnType.ARITHMETIC)
        assert arithmetic == (Decimal("0.1"), Decimal("-0.1"))
        log = periodic_returns(points, ReturnType.LOG)
        assert log[0] == Decimal("1.1").ln()

    def test_sharpe_by_hand(self) -> None:
        # returns 1%, 3%, 2%: mean 2%, sample std 1%, sharpe = 2 x sqrt(252) = 31.749016
        result = sharpe_ratio(
            [Decimal("0.01"), Decimal("0.03"), Decimal("0.02")], SharpeConventionConfig()
        )
        assert result.sharpe == Decimal("31.749016")
        assert result.periods == 3 and result.mean_return == Decimal("0.02")

    def test_risk_free_rate_is_subtracted_per_period(self) -> None:
        convention = SharpeConventionConfig(risk_free_rate=Decimal("2.52"))  # 1% per period
        result = sharpe_ratio([Decimal("0.02"), Decimal("0.04"), Decimal("0.03")], convention)
        assert result.sharpe == Decimal("31.749016")

    def test_undefined_cases(self) -> None:
        assert (
            sharpe_ratio([Decimal("0.01")], SharpeConventionConfig()).reason
            == "fewer than two periods"
        )
        assert (
            sharpe_ratio([Decimal("0.01")] * 5, SharpeConventionConfig()).reason
            == "zero return dispersion"
        )

    def test_mtm_basis_uses_mark_to_market_equity(self, calendar: SessionCalendar) -> None:
        points = daily_equity([snap(0, "100", "105")], calendar, ReturnBasis.DAILY_MTM_EQUITY)
        assert points[0].equity == Decimal("105")


class TestDrawdown:
    def test_measured_from_the_running_peak_on_mtm(self) -> None:
        curve = [
            snap(0, "100"),
            snap(1, "100", "120"),
            snap(2, "100", "90"),
            snap(3, "130"),
            snap(4, "130", "117"),
        ]
        result = drawdown(curve)
        assert result.peak_equity == Decimal("120") and result.trough_equity == Decimal("90")
        assert result.max_drawdown == Decimal("30.00") and result.max_drawdown_pct == Decimal("25")
        assert result.peak_ts_utc == curve[1].ts_utc and result.trough_ts_utc == curve[2].ts_utc

    def test_monotonic_curve_has_zero_drawdown(self) -> None:
        assert drawdown([snap(0, "100"), snap(1, "110")]).max_drawdown_pct == ZERO


class TestConcentration:
    def test_top_n_share_by_hand(self, calendar: SessionCalendar) -> None:
        config = ConcentrationConfig(
            top_n_trades=2,
            quarterly_threshold_pct=Decimal("60"),
            lifetime_threshold_pct=Decimal("60"),
        )
        trades = [
            trade("a", "500"),
            trade("b", "300"),
            trade("c", "100"),
            trade("d", "100"),
            trade("e", "-400"),
        ]
        result = concentration(trades, config, calendar)
        # winners 500+300+100+100 = 1000; top 2 = 800 → 80% > 60 → breached
        assert result.lifetime.top_n_share_pct == Decimal("80")
        assert result.lifetime_breached
        assert result.lifetime.top_trade_ids == ("a", "b")
        assert result.quarterly_breaches == ("2024Q1",)

    def test_boundary_rule_is_explicit(self, calendar: SessionCalendar) -> None:
        trades = [trade("a", "60"), trade("b", "40")]  # top-1 share exactly 60%
        exclusive = concentration(trades, ConcentrationConfig(top_n_trades=1), calendar)
        inclusive = concentration(
            trades, ConcentrationConfig(top_n_trades=1, boundary_is_inclusive=True), calendar
        )
        assert not exclusive.lifetime_breached
        assert inclusive.lifetime_breached

    def test_no_gross_profit_is_not_evaluable(self, calendar: SessionCalendar) -> None:
        result = concentration([trade("a", "-10")], ConcentrationConfig(), calendar)
        assert result.lifetime.breached is None and not result.lifetime.evaluable

    def test_quarters_follow_the_close_date_by_default(self, calendar: SessionCalendar) -> None:
        q1_open = datetime(2024, 3, 28, 12, tzinfo=UTC)
        q2_close = datetime(2024, 4, 2, 12, tzinfo=UTC)
        trades = [trade("a", "100", opened=q1_open, closed=q2_close)]
        close_dated = concentration(trades, ConcentrationConfig(), calendar)
        open_dated = concentration(
            trades,
            ConcentrationConfig(trade_period_attribution=PeriodAttribution.OPEN_DATE),
            calendar,
        )
        assert [q.period for q in close_dated.quarterly] == ["2024Q2"]
        assert [q.period for q in open_dated.quarterly] == ["2024Q1"]

    def test_partial_exits_cannot_dilute_concentration(self, calendar: SessionCalendar) -> None:
        """One trade with three exit fills is one trade (C-4)."""
        big = trade("a", "900")
        result = concentration(
            [big, trade("b", "100")], ConcentrationConfig(top_n_trades=1), calendar
        )
        assert result.lifetime.top_n_share_pct == Decimal("90")


class TestAttribution:
    def test_buckets_and_friction(self, calendar: SessionCalendar) -> None:
        trades = [
            trade("a", "100", mechanism="sweep"),
            trade("b", "-50", mechanism="sweep", exit_reason=ExitReason.STOP_LOSS),
            trade("c", "25", mechanism="pullback", direction=Direction.SHORT),
        ]
        fills = [f for t in trades for f in t.fills]
        report = attribute(trades, fills, calendar, ConcentrationConfig().trade_period_attribution)
        by_mech = {b.key: b for b in report.by_mechanism}
        assert by_mech["sweep"].net_pnl == Decimal("50") and by_mech["sweep"].win_rate == Decimal(
            "0.5"
        )
        assert {b.key for b in report.by_exit_reason} == {"SIGNAL_EXIT", "STOP_LOSS"}
        assert {b.key for b in report.by_direction} == {"LONG", "SHORT"}
        assert report.friction.commission == Decimal("18") and report.friction.spread == Decimal(
            "600"
        )
        assert report.friction.total == Decimal("918") and report.friction.fill_count == 6


class TestGates:
    @staticmethod
    def _report(
        trade_count: int, net_each: str = "100", sharpe_value: Decimal | None = Decimal("2")
    ) -> tuple[TradeStatistics, SharpeResult, DrawdownResult, list[Trade]]:
        trades = [trade(f"t{i}", net_each if i % 3 else "-20") for i in range(trade_count)]
        stats = trade_statistics(trades)
        sharpe = SharpeResult(
            sharpe_value,
            10,
            Decimal("0.01"),
            Decimal("0.005"),
            252,
            ZERO,
            ReturnBasis.DAILY_CLOSED_EQUITY,
            ReturnType.ARITHMETIC,
            "" if sharpe_value else "x",
        )
        dd = DrawdownResult(
            Decimal("100"), Decimal("95"), Decimal("5"), Decimal("5"), None, None, 10
        )
        return stats, sharpe, dd, trades

    def test_insufficient_sample_is_not_a_fail(self, calendar: SessionCalendar) -> None:
        stats, sharpe, dd, trades = self._report(30)
        report = evaluate_gates(
            stats=stats,
            sharpe=sharpe,
            drawdown=dd,
            concentration=concentration(trades, ConcentrationConfig(top_n_trades=50), calendar),
            reporting=ReportingConfig(reporting_id="r"),
            significance=SignificancePolicyConfig.model_validate(
                {"tier": "STANDARD", "min_trades": 400}
            ),
        )
        assert report.overall is GateVerdict.INSUFFICIENT_SAMPLE
        assert {c.name: c.verdict for c in report.checks}["max_drawdown_pct"] is GateVerdict.PASS
        assert not report.publishable

    def test_drawdown_breach_fails_regardless_of_sample(self, calendar: SessionCalendar) -> None:
        stats, sharpe, _, trades = self._report(10)
        dd = DrawdownResult(
            Decimal("100"), Decimal("80"), Decimal("20"), Decimal("20"), None, None, 10
        )
        report = evaluate_gates(
            stats=stats,
            sharpe=sharpe,
            drawdown=dd,
            concentration=concentration(trades, ConcentrationConfig(), calendar),
            reporting=ReportingConfig(reporting_id="r"),
            significance=SignificancePolicyConfig.model_validate(
                {"tier": "STANDARD", "min_trades": 400}
            ),
        )
        assert report.overall is GateVerdict.FAIL

    def test_full_sample_passes_when_every_gate_holds(self, calendar: SessionCalendar) -> None:
        stats, sharpe, dd, trades = self._report(400)
        report = evaluate_gates(
            stats=stats,
            sharpe=sharpe,
            drawdown=dd,
            concentration=concentration(trades, ConcentrationConfig(top_n_trades=5), calendar),
            reporting=ReportingConfig(reporting_id="r"),
            significance=SignificancePolicyConfig.model_validate(
                {"tier": "STANDARD", "min_trades": 400}
            ),
        )
        assert report.overall is GateVerdict.PASS and report.publishable
        assert report.not_evaluated == ("oos_sharpe_decay_max_pct", "wfo_min_profit_factor")

    def test_watermark_blocks_publication(self, calendar: SessionCalendar) -> None:
        stats, sharpe, dd, trades = self._report(400)
        report = evaluate_gates(
            stats=stats,
            sharpe=sharpe,
            drawdown=dd,
            concentration=concentration(trades, ConcentrationConfig(), calendar),
            reporting=ReportingConfig(reporting_id="r"),
            significance=SignificancePolicyConfig.model_validate(
                {"tier": "STANDARD", "min_trades": 400}
            ),
            watermarks=("SAME_BAR_CLOSE execution timing",),
        )
        assert report.overall is GateVerdict.PASS and not report.publishable

    def test_lifetime_kill_criterion_blocks_publication(self, calendar: SessionCalendar) -> None:
        trades = [trade("big", "100000")] + [trade(f"t{i}", "10") for i in range(400)]
        stats = trade_statistics(trades)
        _, sharpe, dd, _ = self._report(1)
        report = evaluate_gates(
            stats=stats,
            sharpe=sharpe,
            drawdown=dd,
            concentration=concentration(trades, ConcentrationConfig(top_n_trades=1), calendar),
            reporting=ReportingConfig(reporting_id="r"),
            significance=SignificancePolicyConfig.model_validate(
                {"tier": "STANDARD", "min_trades": 400}
            ),
        )
        assert report.kill_criterion.verdict is GateVerdict.FAIL
        assert not report.publishable


class TestAnalyzeEndToEnd:
    def test_scores_a_real_run(self) -> None:
        from pathlib import Path

        import qtros.alpha.strategies.vwap_pullback  # noqa: F401
        from qtros.configuration import ConfigLoader
        from qtros.engine.builder import run_from_config
        from tests.test_builder import CONFIG_ROOT, synthetic_bars

        resolved = ConfigLoader(Path(CONFIG_ROOT)).load_backtest("phase1_mvp")
        components, result = run_from_config(resolved, synthetic_bars())
        report = analyze(result, resolved, components.calendar, scored_at_utc=EPOCH)
        assert isinstance(report, PerformanceReport)
        assert report.reporting_id == "constitution_v3_2"
        assert report.trades.trade_count == len(result.closed_trades)
        assert report.gates.sample_size == report.trades.trade_count
        assert report.attribution.friction.fill_count == len(result.fills)
        assert report.trusted
