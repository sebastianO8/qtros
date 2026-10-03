"""Configuration layer tests (ARCH-002 §4–§5, D-1, D-2, D-4, D-5; PI ruling G-5).

Two schema tests carry the layer: every ``Decimal`` field in every model rejects float
input, and every field in every model resolves to exactly one provenance partition.
Both are the kind of guard that rots silently if it is only a convention.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from qtros.configuration import (
    BacktestConfig,
    CalendarConfig,
    ConfigLoader,
    DataSourceConfig,
    ExecutionConfig,
    FeatureSetConfig,
    FrictionConfig,
    InstrumentConfig,
    ProvenanceClass,
    ReportingConfig,
    ResolvedBacktestConfig,
    RiskLimitsConfig,
    StrategyConfig,
    StrategyRegistry,
    StrategyRegistryError,
    canonical_json,
    compute_hashes,
    partition,
)
from qtros.configuration.hashing import FloatInHashError, field_provenance
from qtros.configuration.models import SignificancePolicyConfig, reject_float_input
from qtros.domain.enums import Timeframe
from qtros.domain.errors import ConfigurationError

# --------------------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------------------

INSTRUMENT: dict[str, Any] = {
    "instrument_id": "AUDJPY",
    "display_name": "Australian Dollar / Japanese Yen",
    "asset_class": "FX_SPOT",
    "base_currency": "AUD",
    "quote_currency": "JPY",
    "tick_size": "0.001",
    "contract_size": "100000",
    "min_lot": "0.01",
    "lot_step": "0.01",
    "typical_spread_ticks": "2",
    "commission_per_lot": "6",
    "calendar_id": "fx_sunday_open",
    "rollover_time_local": "17:00:00",
    "rollover_tz": "America/New_York",
}

SOURCE: dict[str, Any] = {
    "source_id": "dukascopy_m5",
    "instrument_id": "AUDJPY",
    "timeframe": "M5",
    "root_path": "./data_lake/raw/audjpy",
    "glob_pattern": "*.csv",
    "file_format": "CSV",
    "columns": {"timestamp": "ts", "open": "o", "high": "h", "low": "l", "close": "c"},
    "timestamp_format": "%Y-%m-%d %H:%M:%S%z",
    "timestamp_policy": "EMBEDDED_OFFSET",
}

STRATEGY: dict[str, Any] = {
    "strategy_id": "vwap_pullback",
    "strategy_version": "1.0.0",
    "experiment_id": "AUDJPY_VWAP_PULLBACK",
    "mechanism_hypothesis": (
        "Session VWAP acts as a magnet for intraday mean reversion in AUD/JPY during the "
        "Asian session because of option-hedging flow."
    ),
    "supported_timeframes": ["M5", "M15"],
    "parameters": {"zscore_entry": "-1.5", "lookback": 20, "trail": False, "note": "x"},
    "significance": {"tier": "STANDARD", "min_trades": 400},
    "declared_at_utc": "2026-01-01T00:00:00+00:00",
}

RISK: dict[str, Any] = {
    "risk_limits_id": "phase1_baseline",
    "max_drawdown_pct": "10",
    "risk_per_trade_pct": "1",
    "atr_stop_multiple": "2",
    "atr_lookback_bars": 14,
    "max_concurrent_positions": 1,
    "max_lot": "5",
    "max_notional_pct": "300",
    "on_limit_breach": "HALT_RUN",
}

FRICTION: dict[str, Any] = {
    "friction_id": "retail_fx_typical",
    "commission_model": "PER_LOT",
    "commission_value": "6",
    "spread_model": "INSTRUMENT_TYPICAL",
    "slippage_model": "FIXED_TICKS",
    "slippage_value": "0.5",
}

REPORTING: dict[str, Any] = {"reporting_id": "constitution_v3_2"}

CALENDAR: dict[str, Any] = {
    "calendar_id": "fx_sunday_open",
    "week_open": {"weekday": 6, "time_local": "17:00:00", "timezone": "America/New_York"},
    "week_close": {"weekday": 4, "time_local": "17:00:00", "timezone": "America/New_York"},
    "sessions": [
        {"name": "asian", "open_local": "09:00:00", "close_local": "15:00:00",
         "timezone": "Asia/Tokyo", "weekdays": [0, 1, 2, 3, 4]},
    ],
    "holidays": ["2024-01-01", "2024-12-25"],
}

FEATURES: dict[str, Any] = {
    "feature_set_id": "baseline_v1",
    "transforms": [
        {"transform": "true_range"},
        {"transform": "atr", "parameters": {"period": 14, "output_column": "atr_14"}},
    ],
}

BACKTEST: dict[str, Any] = {
    "run_label": "phase1-mvp-audjpy-baseline",
    "instrument_id": "AUDJPY",
    "source_id": "dukascopy_m5",
    "date_range": {"start": "2024-01-01", "end": "2024-12-31"},
    "feature_set_id": "baseline_v1",
    "strategy_ref": {"strategy_id": "vwap_pullback", "strategy_version": "1.0.0"},
    "risk_ref": "phase1_baseline",
    "friction_ref": "retail_fx_typical",
    "reporting_ref": "constitution_v3_2",
    "initial_cash": "100000",
}


def _carries_float_guard(info: Any) -> bool:
    """Return whether a field's Decimal type carries ``reject_float_input``.

    Pydantic hoists top-level ``Annotated`` metadata into ``info.metadata``; inside a
    union or a ``Mapping`` value type the ``Annotated`` stays nested, so the annotation
    is walked recursively.
    """
    if any(getattr(meta, "func", None) is reject_float_input for meta in info.metadata):
        return True

    def guarded(annotation: Any) -> bool:
        if any(
            getattr(meta, "func", None) is reject_float_input
            for meta in getattr(annotation, "__metadata__", ())
        ):
            return True
        return any(guarded(arg) for arg in get_args(annotation))

    return guarded(info.annotation)


def resolved() -> ResolvedBacktestConfig:
    return ResolvedBacktestConfig(
        backtest=BacktestConfig(**BACKTEST),
        instrument=InstrumentConfig(**INSTRUMENT),
        source=DataSourceConfig(**SOURCE),
        strategy=StrategyConfig(**STRATEGY),
        risk=RiskLimitsConfig(**RISK),
        friction=FrictionConfig(**FRICTION),
        reporting=ReportingConfig(**REPORTING),
        calendar=CalendarConfig(**CALENDAR),
        features=FeatureSetConfig(**FEATURES),
    )


def _yaml(mapping: dict[str, Any], indent: int = 0) -> str:
    """Emit a minimal YAML document with every scalar quoted where it matters."""
    lines: list[str] = []
    pad = " " * indent
    for key, value in mapping.items():
        if isinstance(value, dict):
            lines.append(f"{pad}{key}:")
            lines.append(_yaml(value, indent + 2))
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            lines.append(f"{pad}{key}:")
            for item in value:
                body = _yaml(item, indent + 4).splitlines()
                lines.append(f"{pad}  - {body[0].strip()}")
                lines.extend(body[1:])
        elif isinstance(value, list):
            lines.append(f"{pad}{key}: [{', '.join(str(v) for v in value)}]")
        elif isinstance(value, bool):
            lines.append(f"{pad}{key}: {'true' if value else 'false'}")
        elif isinstance(value, int):
            lines.append(f"{pad}{key}: {value}")
        else:
            lines.append(f'{pad}{key}: "{value}"')
    return "\n".join(lines)


@pytest.fixture
def config_root(tmp_path: Path) -> Path:
    """Write a complete, consistent config tree."""
    files = {
        "instruments/AUDJPY.yaml": INSTRUMENT,
        "sources/dukascopy_m5.yaml": SOURCE,
        "strategies/audjpy_vwap_pullback_v1_0.yaml": STRATEGY,
        "risk/phase1_baseline.yaml": RISK,
        "friction/retail_fx_typical.yaml": FRICTION,
        "reporting/constitution_v3_2.yaml": REPORTING,
        "calendars/fx_sunday_open.yaml": CALENDAR,
        "features/baseline_v1.yaml": FEATURES,
        "backtest/phase1_mvp.yaml": BACKTEST,
    }
    for relative, mapping in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_yaml(mapping) + "\n", encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------------------
# D-2: StrictDecimal
# --------------------------------------------------------------------------------------

DECIMAL_CASES: list[tuple[type, dict[str, Any], str]] = [
    (InstrumentConfig, INSTRUMENT, "tick_size"),
    (InstrumentConfig, INSTRUMENT, "commission_per_lot"),
    (RiskLimitsConfig, RISK, "max_drawdown_pct"),
    (RiskLimitsConfig, RISK, "max_lot"),
    (FrictionConfig, FRICTION, "slippage_value"),
    (BacktestConfig, BACKTEST, "initial_cash"),
    (DataSourceConfig, SOURCE, "max_reject_ratio"),
]


class TestStrictDecimal:
    @pytest.mark.parametrize(("model", "base", "field"), DECIMAL_CASES)
    def test_float_input_is_rejected(self, model: type, base: dict[str, Any], field: str) -> None:
        with pytest.raises(ValidationError, match="float input is not accepted"):
            model(**{**base, field: 0.001})

    @pytest.mark.parametrize(("model", "base", "field"), DECIMAL_CASES)
    def test_string_input_is_exact(self, model: type, base: dict[str, Any], field: str) -> None:
        instance = model(**{**base, field: "0.001"})
        assert getattr(instance, field) == Decimal("0.001")

    def test_boolean_is_rejected_as_decimal(self) -> None:
        with pytest.raises(ValidationError, match="boolean"):
            RiskLimitsConfig(**{**RISK, "max_lot": True})

    def test_every_decimal_field_in_every_model_rejects_float(self) -> None:
        """Schema-wide guard: no model may grow a Decimal field without the validator."""
        models = (
            InstrumentConfig,
            DataSourceConfig,
            StrategyConfig,
            RiskLimitsConfig,
            FrictionConfig,
            ReportingConfig,
            BacktestConfig,
        )
        seen = 0
        for model in models:
            for name, info in model.model_fields.items():
                if "Decimal" not in repr(info.annotation):
                    continue
                seen += 1
                assert _carries_float_guard(info), (
                    f"{model.__name__}.{name} is a Decimal field without the float guard"
                )
        assert seen >= 15

    def test_strategy_parameters_reject_float(self) -> None:
        with pytest.raises(ValidationError):
            StrategyConfig(**{**STRATEGY, "parameters": {"threshold": 1.5}})


# --------------------------------------------------------------------------------------
# Model rules
# --------------------------------------------------------------------------------------


class TestModelRules:
    def test_unknown_key_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            RiskLimitsConfig(**{**RISK, "max_drawdwon_pct": "5"})

    def test_models_are_frozen(self) -> None:
        risk = RiskLimitsConfig(**RISK)
        with pytest.raises(ValidationError):
            risk.max_lot = Decimal("1")  # type: ignore[misc]

    def test_zero_friction_requires_acknowledgement(self) -> None:
        zeroed = {
            **FRICTION,
            "commission_model": "NONE",
            "commission_value": "0",
            "spread_model": "FIXED_TICKS",
            "spread_value_ticks": "0",
            "slippage_value": "0",
        }
        with pytest.raises(ValidationError, match="acknowledge_zero_friction"):
            FrictionConfig(**zeroed)
        FrictionConfig(**{**zeroed, "acknowledge_zero_friction": True})

    def test_acknowledgement_without_zero_friction_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="not frictionless"):
            FrictionConfig(**{**FRICTION, "acknowledge_zero_friction": True})

    def test_friction_applies_to_instrument_without_touching_the_model(self) -> None:
        spec = InstrumentConfig(**INSTRUMENT).to_spec()
        adjusted = FrictionConfig(
            **{**FRICTION, "commission_value": "4", "spread_model": "FIXED_TICKS",
               "spread_value_ticks": "3"}
        ).apply_to_instrument(spec)
        assert adjusted.commission_per_lot == Decimal("4")
        assert adjusted.typical_spread_ticks == Decimal("3")
        assert spec.commission_per_lot == Decimal("6")

    def test_same_bar_close_requires_the_flag(self) -> None:
        with pytest.raises(ValidationError, match="allow_same_bar_close_fill"):
            ExecutionConfig(timing="SAME_BAR_CLOSE")
        assert ExecutionConfig(timing="SAME_BAR_CLOSE", allow_same_bar_close_fill=True).is_watermarked

    def test_stacked_optimism_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="stacks two optimistic"):
            ExecutionConfig(
                timing="SAME_BAR_CLOSE",
                allow_same_bar_close_fill=True,
                path_assumption="FAVOURABLE_FIRST",
            )

    def test_standard_tier_requires_400(self) -> None:
        with pytest.raises(ValidationError, match="400"):
            SignificancePolicyConfig(tier="STANDARD", min_trades=200)

    def test_low_frequency_requires_pooling(self) -> None:
        with pytest.raises(ValidationError, match="pooled"):
            SignificancePolicyConfig(tier="LOW_FREQUENCY", min_trades=60)
        policy = SignificancePolicyConfig(
            tier="LOW_FREQUENCY", min_trades=60, pooling_instruments=["A", "B", "C", "D"]
        )
        assert policy.is_low_frequency

    def test_mechanism_hypothesis_is_mandatory_and_substantive(self) -> None:
        with pytest.raises(ValidationError, match="mechanism_hypothesis"):
            StrategyConfig(**{**STRATEGY, "mechanism_hypothesis": "it works"})

    def test_declared_at_must_be_aware(self) -> None:
        with pytest.raises(ValidationError, match="timezone-aware"):
            StrategyConfig(**{**STRATEGY, "declared_at_utc": "2026-01-01T00:00:00"})

    def test_unknown_timezone_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="unknown IANA timezone"):
            InstrumentConfig(**{**INSTRUMENT, "rollover_tz": "Mars/Olympus_Mons"})

    def test_source_policy_requires_matching_field(self) -> None:
        with pytest.raises(ValidationError, match="declared_zone"):
            DataSourceConfig(**{**SOURCE, "timestamp_policy": "DECLARED_ZONE"})

    def test_instrument_round_trips_to_spec(self) -> None:
        spec = InstrumentConfig(**INSTRUMENT).to_spec()
        assert spec.tick_size == Decimal("0.001")
        assert spec.rollover_tz == "America/New_York"

    def test_source_round_trips_to_spec(self) -> None:
        spec = DataSourceConfig(**SOURCE).to_spec()
        assert spec.timeframe is Timeframe.M5
        assert spec.columns.volume is None


# --------------------------------------------------------------------------------------
# D-1 / G-5: partitioning and hashing
# --------------------------------------------------------------------------------------


class TestProvenancePartition:
    def test_every_field_resolves_to_exactly_one_class(self) -> None:
        mapping = field_provenance(resolved())
        assert mapping, "no fields walked"
        for dotted, kind in mapping.items():
            assert isinstance(kind, ProvenanceClass), dotted

    def test_operational_fields_land_in_the_operational_partition(self) -> None:
        parts = partition(resolved())
        operational = parts[ProvenanceClass.OPERATIONAL]["backtest"]
        assert set(operational) == {"run_label", "paths", "runtime"}
        assert "run_label" not in parts[ProvenanceClass.RUN_AFFECTING]["backtest"]

    def test_reporting_is_report_affecting(self) -> None:
        parts = partition(resolved())
        assert "reporting" in parts[ProvenanceClass.REPORT_AFFECTING]
        assert "reporting" not in parts[ProvenanceClass.RUN_AFFECTING]
        assert parts[ProvenanceClass.REPORT_AFFECTING]["backtest"] == {
            "reporting_ref": "constitution_v3_2"
        }

    def test_g5_significance_policy_is_run_affecting(self) -> None:
        mapping = field_provenance(resolved())
        assert mapping["strategy.significance.tier"] is ProvenanceClass.RUN_AFFECTING
        assert mapping["strategy.significance.min_trades"] is ProvenanceClass.RUN_AFFECTING
        assert mapping["strategy.significance.pooling_instruments"] is ProvenanceClass.RUN_AFFECTING


class TestHashes:
    def test_hashing_is_deterministic(self) -> None:
        assert compute_hashes(resolved()) == compute_hashes(resolved())

    def test_report_change_alters_only_the_report_hash(self) -> None:
        base = resolved()
        rescored = base.model_copy(
            update={"reporting": ReportingConfig(reporting_id="constitution_v3_3")}
        )
        before, after = compute_hashes(base), compute_hashes(rescored)
        assert after.run_hash == before.run_hash
        assert after.report_hash != before.report_hash
        assert after.config_hash != before.config_hash

    def test_operational_change_alters_no_hash(self) -> None:
        base = resolved()
        relabelled = base.model_copy(
            update={"backtest": base.backtest.model_copy(update={"run_label": "other"})}
        )
        assert compute_hashes(relabelled) == compute_hashes(base)

    def test_run_change_alters_the_run_hash(self) -> None:
        base = resolved()
        tighter = base.model_copy(
            update={"risk": base.risk.model_copy(update={"max_lot": Decimal("1")})}
        )
        before, after = compute_hashes(base), compute_hashes(tighter)
        assert after.run_hash != before.run_hash
        assert after.report_hash == before.report_hash

    def test_g5_significance_change_mints_a_new_run_hash(self) -> None:
        base = resolved()
        pooled = SignificancePolicyConfig(
            tier="LOW_FREQUENCY", min_trades=60, pooling_instruments=["A", "B", "C", "D"]
        )
        retiered = base.model_copy(
            update={"strategy": base.strategy.model_copy(update={"significance": pooled})}
        )
        assert compute_hashes(retiered).run_hash != compute_hashes(base).run_hash

    def test_canonical_json_sorts_keys_and_stringifies_decimals(self) -> None:
        text = canonical_json({"b": Decimal("1.10"), "a": datetime(2026, 1, 1, tzinfo=UTC)})
        assert text == '{"a":"2026-01-01T00:00:00.000000+00:00","b":"1.10"}'

    def test_canonical_json_omits_none_and_sorts_sets(self) -> None:
        assert canonical_json({"s": frozenset({"b", "a"}), "n": None}) == '{"s":["a","b"]}'

    def test_floats_are_forbidden_in_hash_input(self) -> None:
        with pytest.raises(FloatInHashError):
            canonical_json({"x": 0.1})

    def test_decimal_trailing_zeros_are_hash_relevant(self) -> None:
        """``1.10`` and ``1.1`` are different declarations; the hash must see that."""
        assert canonical_json({"x": Decimal("1.10")}) != canonical_json({"x": Decimal("1.1")})


# --------------------------------------------------------------------------------------
# D-5: loader
# --------------------------------------------------------------------------------------


class TestLoader:
    def test_resolves_a_full_composition(self, config_root: Path) -> None:
        resolved_config = ConfigLoader(config_root).load_backtest("phase1_mvp")
        assert resolved_config.instrument.instrument_id == "AUDJPY"
        assert resolved_config.strategy.strategy_version == "1.0.0"
        assert resolved_config.risk.max_lot == Decimal("5")
        assert resolved_config.hashes().run_hash == resolved().hashes().run_hash

    def test_strategy_is_matched_by_content_not_file_name(self, config_root: Path) -> None:
        path = config_root / "strategies" / "audjpy_vwap_pullback_v1_0.yaml"
        path.rename(config_root / "strategies" / "anything.yaml")
        assert ConfigLoader(config_root).load_backtest("phase1_mvp").strategy.strategy_id == (
            "vwap_pullback"
        )

    def test_missing_reference_is_a_configuration_error(self, config_root: Path) -> None:
        (config_root / "risk" / "phase1_baseline.yaml").unlink()
        with pytest.raises(ConfigurationError, match="not found"):
            ConfigLoader(config_root).load_backtest("phase1_mvp")

    def test_unquoted_decimal_in_yaml_is_refused(self, config_root: Path) -> None:
        path = config_root / "risk" / "phase1_baseline.yaml"
        path.write_text(path.read_text().replace('max_lot: "5"', "max_lot: 5.0"))
        with pytest.raises(ConfigurationError, match="float input"):
            ConfigLoader(config_root).load_backtest("phase1_mvp")

    def test_anchors_and_merge_keys_are_refused(self, config_root: Path) -> None:
        path = config_root / "risk" / "phase1_baseline.yaml"
        path.write_text("base: &base\n  x: 1\nmerged:\n  <<: *base\n" + path.read_text())
        with pytest.raises(ConfigurationError, match="not permitted"):
            ConfigLoader(config_root).load_backtest("phase1_mvp")

    def test_source_instrument_must_match_backtest(self, config_root: Path) -> None:
        path = config_root / "sources" / "dukascopy_m5.yaml"
        path.write_text(path.read_text().replace('instrument_id: "AUDJPY"', 'instrument_id: "CADJPY"'))
        with pytest.raises(ConfigurationError, match="different instrument"):
            ConfigLoader(config_root).load_backtest("phase1_mvp")

    def test_newer_schema_version_is_refused(self, config_root: Path) -> None:
        path = config_root / "risk" / "phase1_baseline.yaml"
        path.write_text("schema_version: 99\n" + path.read_text())
        with pytest.raises(ConfigurationError, match="newer schema"):
            ConfigLoader(config_root).load_backtest("phase1_mvp")

    def test_identifier_cannot_escape_the_family_directory(self, config_root: Path) -> None:
        with pytest.raises(ConfigurationError, match="path separators"):
            ConfigLoader(config_root).load_one("risk", "../backtest/phase1_mvp", RiskLimitsConfig)


# --------------------------------------------------------------------------------------
# D-4: registry
# --------------------------------------------------------------------------------------


class TestRegistry:
    def test_register_and_resolve(self) -> None:
        registry = StrategyRegistry()

        def factory() -> str:
            return "s"

        registry.register("vwap_pullback", "1.0.0", factory)
        assert registry.resolve("vwap_pullback", "1.0.0").factory is factory
        assert ("vwap_pullback", "1.0.0") in registry

    def test_same_object_may_register_twice(self) -> None:
        registry = StrategyRegistry()

        def factory() -> None: ...

        registry.register("x_one", "1.0.0", factory)
        registry.register("x_one", "1.0.0", factory)
        assert len(registry) == 1

    def test_conflicting_registration_is_refused(self) -> None:
        registry = StrategyRegistry()
        registry.register("x_one", "1.0.0", lambda: 1)
        with pytest.raises(StrategyRegistryError, match="different object"):
            registry.register("x_one", "1.0.0", lambda: 2)

    def test_deleted_version_is_named_explicitly(self) -> None:
        registry = StrategyRegistry()
        registry.register("x_one", "1.0.0", lambda: 1)
        with pytest.raises(StrategyRegistryError) as excinfo:
            registry.resolve("x_one", "2.0.0")
        assert excinfo.value.context["available"] == ["1.0.0"]

    def test_unknown_id_is_distinguished_from_unknown_version(self) -> None:
        registry = StrategyRegistry()
        with pytest.raises(StrategyRegistryError, match="id is not registered"):
            registry.resolve("ghost", "1.0.0")

    def test_import_paths_are_not_valid_ids(self) -> None:
        with pytest.raises(StrategyRegistryError, match="snake_case"):
            StrategyRegistry().register("qtros.alpha.Thing", "1.0.0", lambda: 1)
