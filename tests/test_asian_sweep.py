"""audjpy_asian_sweep 1.0.0: the session-range and ADX transforms, and the strategy rules.

Hand-built bars with round prices make every expected value checkable on paper. Winter
dates are used where the arithmetic matters (New York = UTC-5); one test pins the summer
offset (UTC-4) because the Cycle 1 script got exactly that wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import polars as pl
import pytest

from qtros.alpha.strategies.audjpy_asian_sweep import AudJpyAsianSweep, in_window
from qtros.configuration import ConfigLoader
from qtros.domain.enums import Direction, ExitReason
from qtros.domain.execution import PositionState
from qtros.domain.identifiers import InstrumentId, TradeId
from qtros.domain.market import BarData
from qtros.domain.signals import BarContext, FeatureRow, PortfolioView, SessionInfo, StrategyState
from qtros.engine.builder import run_from_config
from qtros.features.lag_audit import LagAuditor
from qtros.features.transforms import AverageDirectionalIndex, SessionRange, build_transform
from tests.test_builder import CONFIG_ROOT, synthetic_bars

if TYPE_CHECKING:
    from collections.abc import Sequence

    from qtros.domain.instrument import InstrumentSpec

FIVE = timedelta(minutes=5)
ZERO = Decimal("0")


# --------------------------------------------------------------------------------------
# SessionRange
# --------------------------------------------------------------------------------------


def session_frame(first_utc: datetime, rows: list[tuple[float, float]]) -> pl.DataFrame:
    """Five-minute bars from (high, low) pairs; open and close sit between them."""
    return pl.DataFrame(
        {
            "ts_utc": [first_utc + FIVE * i for i in range(len(rows))],
            "open": [(h + lo) / 2 for h, lo in rows],
            "high": [h for h, _ in rows],
            "low": [lo for _, lo in rows],
            "close": [(h + lo) / 2 for h, lo in rows],
        }
    ).with_columns(pl.col("ts_utc").dt.cast_time_unit("us"))


def run_session_range(frame: pl.DataFrame) -> pl.DataFrame:
    return SessionRange().apply(frame.lazy()).collect()


class TestSessionRange:
    @pytest.fixture
    def winter(self) -> pl.DataFrame:
        # 18:50 New York on Tue 2 Jan 2024 = 23:50 UTC. Two bars before the session, 108 in
        # it (19:00-03:55), then three after it.
        rows = [(95.0, 95.0)] * 2
        session = [(95.20, 94.80)] * 108
        session[10] = (95.30, 94.80)
        session[50] = (95.20, 94.70)
        rows += session
        rows += [(95.00, 94.60), (95.00, 94.65), (95.00, 94.50)]
        return session_frame(datetime(2024, 1, 2, 23, 50, tzinfo=UTC), rows)

    def test_by_hand(self, winter: pl.DataFrame) -> None:
        out = run_session_range(winter)
        ids = out.get_column("asian_session_id").to_list()
        assert ids[:2] == [0.0, 0.0] and set(ids[2:]) == {1.0}
        assert out.get_column("asian_range_high").to_list()[:2] == [None, None]
        post = out.tail(3)
        assert post.get_column("asian_range_high").to_list() == [95.30] * 3
        assert post.get_column("asian_range_low").to_list() == [94.70] * 3
        assert post.get_column("asian_bars_since_end").to_list() == [0.0, 1.0, 2.0]
        assert post.get_column("asian_sweep_low").to_list() == [94.60, 94.60, 94.50]
        assert post.get_column("asian_sweep_high").to_list() == [None, None, None]

    def test_running_range_while_open(self, winter: pl.DataFrame) -> None:
        out = run_session_range(winter)
        highs = out.get_column("asian_range_high").to_list()
        assert highs[2] == 95.20 and highs[12] == 95.30
        assert out.get_column("asian_bars_since_end").to_list()[50] is None

    def test_first_session_bar_follows_new_york_daylight_time(self) -> None:
        """19:00 New York is 23:00 UTC in July, not 00:00 UTC as fixed EST would claim."""
        rows = [(160.0, 160.0)] * 4
        out = run_session_range(session_frame(datetime(2024, 7, 2, 22, 50, tzinfo=UTC), rows))
        # 22:50, 22:55 UTC are 18:50, 18:55 EDT; 23:00 UTC opens the session.
        assert out.get_column("asian_session_id").to_list() == [0.0, 0.0, 1.0, 1.0]

    def test_session_range_and_adx_pass_the_look_ahead_audit(self) -> None:
        transforms = [SessionRange(), AverageDirectionalIndex(period=14, smoothing=14)]
        report = LagAuditor().audit(synthetic_bars(900), transforms, probe_count=15)
        assert report.passed, report.summary()


# --------------------------------------------------------------------------------------
# ADX against an independent implementation of the Cycle 1 formula
# --------------------------------------------------------------------------------------


def reference_rma(values: Sequence[float | None], length: int) -> list[float | None]:
    """Wilder's average as pandas ewm(alpha=1/n, adjust=False, min_periods=n) computes it."""
    out: list[float | None] = []
    level: float | None = None
    seen = 0
    for value in values:
        if value is not None:
            level = value if level is None else level + (value - level) / length
            seen += 1
        out.append(level if seen >= length and value is not None else None)
    return out


def reference_adx(
    high: list[float], low: list[float], close: list[float], n: int
) -> list[float | None]:
    plus_dm, minus_dm, tr = [], [], []
    for i in range(len(high)):
        up = high[i] - high[i - 1] if i else None
        down = low[i - 1] - low[i] if i else None
        plus_dm.append(up if up is not None and down is not None and up > down and up > 0 else 0.0)
        minus_dm.append(
            down if up is not None and down is not None and down > up and down > 0 else 0.0
        )
        candidates = [high[i] - low[i]]
        if i:
            candidates += [abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])]
        tr.append(max(candidates))
    s_tr, s_p, s_m = reference_rma(tr, n), reference_rma(plus_dm, n), reference_rma(minus_dm, n)
    dx: list[float | None] = []
    for t, p, m in zip(s_tr, s_p, s_m, strict=True):
        if t is None or p is None or m is None or t == 0:
            dx.append(None)
            continue
        pdi, mdi = 100 * p / t, 100 * m / t
        dx.append(None if pdi + mdi == 0 else 100 * abs(pdi - mdi) / (pdi + mdi))
    return reference_rma(dx, n)


def test_adx_matches_the_cycle1_formula() -> None:
    bars = synthetic_bars(400)
    out = AverageDirectionalIndex(period=14, smoothing=14).apply(bars.lazy()).collect()
    expected = reference_adx(
        bars.get_column("high").to_list(),
        bars.get_column("low").to_list(),
        bars.get_column("close").to_list(),
        14,
    )
    got = out.get_column("adx_14").to_list()
    assert sum(v is not None for v in got) > 300
    for g, e in zip(got, expected, strict=True):
        assert (g is None) == (e is None)
        if g is not None and e is not None:
            assert abs(g - e) < 1e-9


# --------------------------------------------------------------------------------------
# Strategy rules
# --------------------------------------------------------------------------------------

PARAMS: dict[str, object] = {
    "timezone": "America/New_York",
    "range_start": "19:00",
    "range_end": "04:00",
    "trade_window_start": "04:00",
    "trade_window_end": "17:00",
    "bar_minutes": 5,
    "session_id_column": "sid",
    "range_high_column": "rh",
    "range_low_column": "rl",
    "bars_since_end_column": "since",
    "sweep_low_column": "swl",
    "sweep_high_column": "swh",
    "atr_column": "atr",
    "adx_column": "adx",
    "min_stop_atr": "0.8",
    "max_stop_atr": "2.5",
    "rr_multiple": "2",
    "cooldown_bars": 5,
    "adx_min": "15",
    "adx_max": "40",
    "freshness_bars": 90,
}


def ny_winter(hour: int, minute: int = 0) -> datetime:
    """A Wednesday in January, New York local time, as UTC (UTC-5)."""
    return datetime(2024, 1, 10, hour, minute, tzinfo=UTC) + timedelta(hours=5)


def context(
    instrument: InstrumentSpec,
    ts: datetime,
    *,
    close: str,
    low: str | None = None,
    high: str | None = None,
    features: dict[str, float | None] | None = None,
    position: PositionState | None = None,
    trades: int = 0,
    bar_index: int = 100,
) -> BarContext:
    c = Decimal(close)
    lo = Decimal(low) if low is not None else c
    hi = Decimal(high) if high is not None else c
    values: dict[str, float | None] = {
        "sid": 1.0,
        "rh": 96.0,
        "rl": 95.0,
        "since": 10.0,
        "swl": None,
        "swh": None,
        "atr": 0.05,
        "adx": 20.0,
    }
    values.update(features or {})
    cash = Decimal("100000")
    return BarContext(
        bar=BarData(ts_utc=ts, open=c, high=max(hi, c), low=min(lo, c), close=c),
        features=FeatureRow(as_of_ts_utc=ts - FIVE, values=values),
        instrument=instrument,
        portfolio=PortfolioView(
            cash=cash,
            closed_equity=cash,
            mtm_equity=cash,
            open_position=position,
            realized_pnl_to_date=ZERO,
            trade_count_to_date=trades,
        ),
        session=SessionInfo(
            session_index=0, bar_index_in_session=0, is_session_open=False, is_session_close=False
        ),
        bar_index=bar_index,
    )


@pytest.fixture
def strategy() -> AudJpyAsianSweep:
    return AudJpyAsianSweep(PARAMS)  # type: ignore[arg-type]


class TestEntries:
    def test_long_on_reclaim_with_stop_clamped_to_max_risk(
        self, strategy: AudJpyAsianSweep, instrument: InstrumentSpec
    ) -> None:
        # Sweep low 94.90, close 95.05: raw risk 0.15 > 2.5 x 0.05 = 0.125, so the stop is
        # 95.05 - 0.125 = 94.925 and the target 95.05 + 0.25 = 95.30.
        ctx = context(instrument, ny_winter(6), close="95.05", features={"swl": 94.90})
        (intent,) = strategy.evaluate(ctx, StrategyState()).intents
        assert intent.direction is Direction.LONG and not intent.is_exit
        assert intent.stop_loss == Decimal("94.925") and intent.take_profit == Decimal("95.300")
        assert intent.mechanism_tag == "asian_range_sweep_reclaim"

    def test_stop_clamped_to_min_risk(
        self, strategy: AudJpyAsianSweep, instrument: InstrumentSpec
    ) -> None:
        # Sweep low 95.04 is only 0.01 away; min risk 0.8 x 0.05 = 0.04 -> stop 95.01, target 95.13.
        ctx = context(instrument, ny_winter(6), close="95.05", low="95.04", features={"swl": 95.04})
        (intent,) = strategy.evaluate(ctx, StrategyState()).intents
        assert intent.stop_loss == Decimal("95.010") and intent.take_profit == Decimal("95.130")

    def test_the_current_bar_can_be_the_sweep(
        self, strategy: AudJpyAsianSweep, instrument: InstrumentSpec
    ) -> None:
        ctx = context(instrument, ny_winter(6), close="95.02", low="94.95")
        (intent,) = strategy.evaluate(ctx, StrategyState()).intents
        assert intent.direction is Direction.LONG
        assert intent.rationale["sweep_extreme"] == 94.95

    def test_short_is_the_mirror_image(
        self, strategy: AudJpyAsianSweep, instrument: InstrumentSpec
    ) -> None:
        ctx = context(instrument, ny_winter(6), close="95.95", features={"swh": 96.10})
        (intent,) = strategy.evaluate(ctx, StrategyState()).intents
        assert intent.direction is Direction.SHORT
        assert intent.stop_loss == Decimal("96.075") and intent.take_profit == Decimal("95.700")

    def test_first_bar_after_the_session_is_fresh(
        self, strategy: AudJpyAsianSweep, instrument: InstrumentSpec
    ) -> None:
        ctx = context(
            instrument, ny_winter(4), close="95.02", low="94.95", features={"since": None}
        )
        assert strategy.evaluate(ctx, StrategyState()).has_intents

    @pytest.mark.parametrize(
        ("label", "ts", "features"),
        [
            ("inside the Asian session", ny_winter(20), {}),
            ("after the trade window", ny_winter(17, 30), {}),
            ("ADX too low", ny_winter(6), {"adx": 14.9}),
            ("ADX too high", ny_winter(6), {"adx": 40.1}),
            ("stale: 91 bars after the session", ny_winter(6), {"since": 90.0}),
            ("no range yet", ny_winter(6), {"rh": None, "rl": None}),
        ],
    )
    def test_no_entry_when(
        self,
        strategy: AudJpyAsianSweep,
        instrument: InstrumentSpec,
        label: str,
        ts: datetime,
        features: dict[str, float | None],
    ) -> None:
        ctx = context(instrument, ts, close="95.05", features={"swl": 94.90, **features})
        assert not strategy.evaluate(ctx, StrategyState()).has_intents, label

    def test_no_entry_without_a_reclaim(
        self, strategy: AudJpyAsianSweep, instrument: InstrumentSpec
    ) -> None:
        ctx = context(instrument, ny_winter(6), close="94.98", features={"swl": 94.90})
        assert not strategy.evaluate(ctx, StrategyState()).has_intents

    def test_one_long_per_session_until_the_session_changes(
        self, strategy: AudJpyAsianSweep, instrument: InstrumentSpec
    ) -> None:
        first = strategy.evaluate(
            context(instrument, ny_winter(6), close="95.05", features={"swl": 94.90}),
            StrategyState(),
        )
        again = strategy.evaluate(
            context(
                instrument, ny_winter(7), close="95.05", features={"swl": 94.90}, bar_index=112
            ),
            first.state,
        )
        assert first.has_intents and not again.has_intents
        next_day = strategy.evaluate(
            context(
                instrument,
                ny_winter(6) + timedelta(days=1),
                close="95.05",
                features={"swl": 94.90, "sid": 2.0},
                bar_index=400,
            ),
            again.state,
        )
        assert next_day.has_intents

    def test_cooldown_after_an_exit(
        self, strategy: AudJpyAsianSweep, instrument: InstrumentSpec
    ) -> None:
        # A closed trade appears at bar 100: entries are blocked for bars 100-104.
        state = strategy.evaluate(
            context(instrument, ny_winter(5), close="95.50", trades=1, bar_index=100),
            StrategyState(),
        ).state
        blocked = strategy.evaluate(
            context(
                instrument,
                ny_winter(6),
                close="95.05",
                features={"swl": 94.90},
                trades=1,
                bar_index=104,
            ),
            state,
        )
        allowed = strategy.evaluate(
            context(
                instrument,
                ny_winter(6),
                close="95.05",
                features={"swl": 94.90},
                trades=1,
                bar_index=105,
            ),
            state,
        )
        assert not blocked.has_intents and allowed.has_intents


class TestSessionCloseExit:
    @pytest.fixture
    def long_position(self) -> PositionState:
        return PositionState(
            trade_id=TradeId("t-1"),
            instrument_id=InstrumentId("AUDJPY"),
            direction=Direction.LONG,
            quantity=Decimal("0.10"),
            avg_entry_price=Decimal("95.05"),
            opened_at_utc=ny_winter(6),
        )

    def test_exit_on_the_bar_before_the_next_session(
        self, strategy: AudJpyAsianSweep, instrument: InstrumentSpec, long_position: PositionState
    ) -> None:
        (intent,) = strategy.evaluate(
            context(instrument, ny_winter(18, 55), close="95.10", position=long_position),
            StrategyState(),
        ).intents
        assert intent.is_exit and intent.direction is Direction.SHORT
        assert intent.exit_reason is ExitReason.SESSION_CLOSE

    def test_hold_until_then(
        self, strategy: AudJpyAsianSweep, instrument: InstrumentSpec, long_position: PositionState
    ) -> None:
        assert not strategy.evaluate(
            context(instrument, ny_winter(18, 50), close="95.10", position=long_position),
            StrategyState(),
        ).has_intents


def test_in_window_crosses_midnight() -> None:
    from datetime import time

    assert in_window(time(23, 0), time(19, 0), time(4, 0))
    assert in_window(time(3, 55), time(19, 0), time(4, 0))
    assert not in_window(time(4, 0), time(19, 0), time(4, 0))
    assert in_window(time(4, 0), time(4, 0), time(17, 0))


# --------------------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------------------


def test_committed_config_runs_and_reconciles() -> None:
    import qtros.alpha.strategies  # noqa: F401

    resolved = ConfigLoader(CONFIG_ROOT).load_backtest("histdata_audjpy_sweep_2023_2025")
    assert (
        resolved.strategy.parameters["timezone"]
        == resolved.features.transforms[3].parameters["timezone"]
    )
    components, result = run_from_config(resolved, synthetic_bars(2000))
    assert result.reconciliation.passed
    assert (
        components.feature_frame.lag_audit is not None and components.feature_frame.lag_audit.passed
    )
    assert set(result.sealed_columns) >= {"asian_range_low", "adx_14", "atr_wilder_14"}


def test_feature_set_builds() -> None:
    resolved = ConfigLoader(CONFIG_ROOT).load_backtest("histdata_audjpy_sweep_2023_2025")
    names = [
        build_transform(ref.transform, dict(ref.parameters)).spec.name
        for ref in resolved.features.transforms
    ]
    assert names == ["true_range", "atr", "adx", "session_range"]
