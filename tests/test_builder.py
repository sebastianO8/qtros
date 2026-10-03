"""End-to-end: the committed ``config/`` tree → features → run → reconciliation.

This is the first test that exercises the whole Phase 1 path from YAML to a reconciled
ledger. Bars are synthetic — a sine wave with noise-free structure — so the test asserts
on invariants (reconciliation, determinism, provenance) rather than on a PnL figure.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from qtros.configuration import ConfigLoader, ResolvedBacktestConfig, strategy_registry
from qtros.domain.enums import Timeframe
from qtros.domain.errors import ConfigurationError
from qtros.engine.builder import build_run, build_transforms, compute_features, run_from_config

CONFIG_ROOT = Path(__file__).resolve().parent.parent / "config"


@pytest.fixture(scope="module")
def resolved() -> ResolvedBacktestConfig:
    return ConfigLoader(CONFIG_ROOT).load_backtest("phase1_mvp")


def synthetic_bars(count: int = 900, start: datetime | None = None) -> pl.DataFrame:
    """M5 bars on a slow sine so the z-score strategy actually trades."""
    first = start or datetime(2024, 1, 8, 0, 0, tzinfo=UTC)  # Monday, inside the FX week
    step = Timeframe.M5.duration
    ts, o, h, lo, c, v = [], [], [], [], [], []
    # Amplitude and bar range are chosen so a 2×ATR stop sizes to roughly the minimum
    # lot under 1% risk: a flat sine would size to several lots and the G-1 notional cap
    # would (correctly) reject every entry, leaving nothing to reconcile.
    for i in range(count):
        price = 95.0 + 2.0 * math.sin(i / 25.0) + 0.3 * math.sin(i / 3.0)
        nxt = 95.0 + 2.0 * math.sin((i + 1) / 25.0) + 0.3 * math.sin((i + 1) / 3.0)
        ts.append(first + step * i)
        o.append(round(price, 3))
        c.append(round(nxt, 3))
        h.append(round(max(price, nxt) + 0.15, 3))
        lo.append(round(min(price, nxt) - 0.15, 3))
        v.append(1000.0 + (i % 7) * 10)
    return pl.DataFrame(
        {"ts_utc": ts, "open": o, "high": h, "low": lo, "close": c, "volume": v}
    ).with_columns(pl.col("ts_utc").dt.replace_time_zone("UTC"))


class TestConfigTree:
    def test_every_committed_backtest_resolves(self) -> None:
        loader = ConfigLoader(CONFIG_ROOT)
        for path in sorted((CONFIG_ROOT / "backtest").glob("*.yaml")):
            resolved_config = loader.load_backtest(path.stem)
            assert resolved_config.hashes().run_hash

    def test_resolved_tree_is_internally_consistent(self, resolved: ResolvedBacktestConfig) -> None:
        assert resolved.calendar.calendar_id == resolved.instrument.calendar_id
        assert resolved.source.instrument_id == resolved.instrument.instrument_id
        assert resolved.source.timeframe in resolved.strategy.supported_timeframes
        feature_outputs = {
            str(ref.parameters.get("output_column", "")) for ref in resolved.features.transforms
        }
        for name in ("vwap_column", "zscore_column", "atr_column"):
            assert resolved.strategy.parameters[name] in feature_outputs

    def test_referenced_strategy_is_registered(self, resolved: ResolvedBacktestConfig) -> None:
        import qtros.alpha.strategies.vwap_pullback  # noqa: F401 — registration side effect

        assert (resolved.strategy.strategy_id, resolved.strategy.strategy_version) in strategy_registry

    def test_hashes_are_stable_across_loads(self, resolved: ResolvedBacktestConfig) -> None:
        again = ConfigLoader(CONFIG_ROOT).load_backtest("phase1_mvp")
        assert again.hashes() == resolved.hashes()


class TestBuilder:
    def test_transforms_build_from_the_feature_set(self, resolved: ResolvedBacktestConfig) -> None:
        transforms = build_transforms(resolved.features)
        assert [t.spec.name for t in transforms] == ["true_range", "atr", "anchored_vwap", "rolling_zscore"]

    def test_features_are_sealed_and_audited(self, resolved: ResolvedBacktestConfig) -> None:
        frame = compute_features(resolved, synthetic_bars())
        assert frame.sealed_features is not None
        assert frame.lag_audit is not None and frame.lag_audit.passed
        assert frame.availability_shift_applied == 0
        assert {"atr_14", "vwap_session", "close_zscore_50"} <= set(frame.feature_columns)

    def test_run_graph_is_a_function_of_the_config(self, resolved: ResolvedBacktestConfig) -> None:
        import qtros.alpha.strategies.vwap_pullback  # noqa: F401

        frame = compute_features(resolved, synthetic_bars())
        components = build_run(resolved, frame)
        assert components.strategy.strategy_id == "vwap_pullback"
        assert components.instrument.commission_per_lot == resolved.friction.commission_value
        assert components.ids.issued() == {}
        assert components.hashes == resolved.hashes()

    def test_wrong_strategy_identity_is_refused(self, resolved: ResolvedBacktestConfig) -> None:
        from qtros.configuration import StrategyRegistry
        from qtros.alpha.strategies.vwap_pullback import VwapPullback

        rogue = StrategyRegistry()
        rogue.register("vwap_pullback", "1.0.0", lambda params: VwapPullback(params))
        VwapPullback.strategy_version = "9.9.9"
        try:
            with pytest.raises(ConfigurationError, match="does not identify itself"):
                build_run(resolved, compute_features(resolved, synthetic_bars(300)), registry=rogue)
        finally:
            VwapPullback.strategy_version = "1.0.0"


@pytest.mark.integration
class TestEndToEnd:
    def test_phase1_mvp_runs_and_reconciles(self, resolved: ResolvedBacktestConfig) -> None:
        import qtros.alpha.strategies.vwap_pullback  # noqa: F401

        components, result = run_from_config(resolved, synthetic_bars())
        assert result.reconciliation.passed and result.trusted
        assert result.bars_processed == components.feature_frame.height
        assert result.decisions_taken == result.bars_processed - 2
        assert len(result.fills) > 0, "the reference strategy should trade on a sine wave"
        assert sum((f.cash_delta or Decimal("0")) for f in result.fills) == result.final_cash - resolved.backtest.initial_cash
        assert result.sealed_columns == tuple(sorted(components.feature_frame.sealed_features.features))  # type: ignore[union-attr]

    def test_two_runs_from_the_same_config_are_identical(self, resolved: ResolvedBacktestConfig) -> None:
        import qtros.alpha.strategies.vwap_pullback  # noqa: F401

        _, first = run_from_config(resolved, synthetic_bars())
        _, second = run_from_config(resolved, synthetic_bars())
        assert [f.fill_id for f in first.fills] == [f.fill_id for f in second.fills]
        assert first.final_cash == second.final_cash
        assert first.equity_curve == second.equity_curve
        assert first.fills and first.fills[0].fill_id.startswith(resolved.hashes().run_id_fragment)

    def test_every_fill_is_adverse_to_its_reference(self, resolved: ResolvedBacktestConfig) -> None:
        import qtros.alpha.strategies.vwap_pullback  # noqa: F401

        _, result = run_from_config(resolved, synthetic_bars())
        for fill in result.fills:
            degradation = (fill.fill_price - fill.reference_price) * fill.direction.sign
            assert degradation >= 0
