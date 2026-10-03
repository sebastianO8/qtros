"""Strategy layer tests (ARCH-002 D-4; DIR-001; PI ruling G-4)."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import polars as pl
import pytest

from qtros.alpha import (
    StrategyBase,
    StrategyProvenanceError,
    register_strategy,
    verify_feature_provenance,
)
from qtros.alpha.strategies.vwap_pullback import VwapPullback
from qtros.configuration import strategy_registry
from qtros.domain.errors import ConfigurationError
from qtros.domain.feature_contracts import (
    Causality,
    FeatureFrame,
    SealedFeature,
    SealedFeatureSet,
)
from qtros.domain.signals import BarContext, StrategyDecision, StrategyState

if TYPE_CHECKING:
    from collections.abc import Callable


def seal(*columns: str, non_causal: tuple[str, ...] = ()) -> SealedFeatureSet:
    return SealedFeatureSet(
        {
            column: SealedFeature(
                column=column,
                transform_name=f"t_{column}",
                transform_version="1.0.0",
                spec_hash="0" * 64,
                warmup_bars=0,
                causality=Causality.NON_CAUSAL_CONTROL
                if column in non_causal
                else Causality.CAUSAL,
            )
            for column in columns
        }
    )


class Reader(StrategyBase):
    """Minimal strategy declaring a fixed feature set."""

    def __init__(self, *features: str) -> None:
        super().__init__({})
        self._features = frozenset(features)

    @property
    def required_features(self) -> frozenset[str]:
        return self._features

    def evaluate(self, ctx: BarContext, state: StrategyState) -> StrategyDecision:
        return StrategyDecision(state=state.advanced())


class TestRegistration:
    def test_decorator_stamps_identity_and_registers(self) -> None:
        @register_strategy("test_registered", "0.1.0")
        class Registered(Reader):
            pass

        assert Registered.strategy_id == "test_registered"
        assert Registered.strategy_version == "0.1.0"
        assert strategy_registry.resolve("test_registered", "0.1.0").factory is Registered

    def test_decorator_refuses_non_strategies(self) -> None:
        with pytest.raises(ConfigurationError, match="StrategyBase subclasses only"):
            register_strategy("not_a_strategy", "1.0.0")(object)

    def test_reference_strategy_is_registered(self) -> None:
        record = strategy_registry.resolve("vwap_pullback", "1.0.0")
        assert record.factory is VwapPullback

    def test_parameters_are_read_only_and_exact(self) -> None:
        strategy = VwapPullback(
            {
                "vwap_column": "v",
                "zscore_column": "z",
                "atr_column": "a",
                "entry_z": "-1.5",
                "exit_z": 0,
                "stop_atr": "2",
                "target_atr": "3",
            }
        )
        assert str(strategy.param_decimal("entry_z")) == "-1.5"
        assert strategy.param_decimal("exit_z") == 0
        with pytest.raises(TypeError):
            strategy.parameters["entry_z"] = "0"  # type: ignore[index]

    def test_missing_parameter_is_a_configuration_error(self) -> None:
        with pytest.raises(ConfigurationError, match="missing"):
            _ = VwapPullback({}).required_features


class TestProvenanceCheck:
    @pytest.fixture
    def frame(self, build_feature_frame: Callable[..., FeatureFrame]) -> FeatureFrame:
        base = build_feature_frame(rows=6, feature_values={"alpha": [1.0] * 6, "beta": [2.0] * 6})
        return replace(base, sealed_features=seal("alpha", "beta"))

    def test_sealed_frame_covering_the_declaration_passes(self, frame: FeatureFrame) -> None:
        sealed = verify_feature_provenance(Reader("alpha"), frame)
        assert "alpha" in sealed

    def test_unsealed_frame_is_refused(self, frame: FeatureFrame) -> None:
        with pytest.raises(StrategyProvenanceError, match="no SealedFeatureSet"):
            verify_feature_provenance(Reader("alpha"), replace(frame, sealed_features=None))

    def test_undeclared_feature_not_in_seal_is_refused(self, frame: FeatureFrame) -> None:
        with pytest.raises(StrategyProvenanceError) as excinfo:
            verify_feature_provenance(Reader("alpha", "gamma"), frame)
        assert excinfo.value.context["unsealed"] == ["gamma"]

    def test_non_causal_control_is_refused(self, frame: FeatureFrame) -> None:
        tainted = replace(frame, sealed_features=seal("alpha", "beta", non_causal=("beta",)))
        with pytest.raises(StrategyProvenanceError) as excinfo:
            verify_feature_provenance(Reader("beta"), tainted)
        assert excinfo.value.context["non_causal"] == ["beta"]

    def test_injected_column_is_refused_even_when_not_declared(self, frame: FeatureFrame) -> None:
        """A column added between the pipeline and the engine has no seal and fails."""
        injected = replace(frame, frame=frame.frame.with_columns(pl.lit(9.0).alias("rogue")))
        with pytest.raises(StrategyProvenanceError, match="no registered transform"):
            verify_feature_provenance(Reader("alpha"), injected)

    def test_sealed_but_absent_from_frame_is_refused(self, frame: FeatureFrame) -> None:
        wider = replace(frame, sealed_features=seal("alpha", "beta", "gamma"))
        with pytest.raises(StrategyProvenanceError) as excinfo:
            verify_feature_provenance(Reader("gamma"), wider)
        assert excinfo.value.context["not_in_frame"] == ["gamma"]
