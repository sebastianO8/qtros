"""Wire a resolved configuration into a ready-to-run :class:`Backtester`.

This is the only place configuration meets construction. Every component the run loop
needs — instrument, calendar, strategy, portfolio, broker, risk engine, identifier
factory — is built here from the flattened :class:`ResolvedBacktestConfig`, so that the
object graph of a run is a pure function of its config hash. Two runs with the same
``run_hash`` build identical graphs; that is what makes Directive 10's reproducibility
claim checkable.

Feature computation is also driven from here (``config/features/*.yaml`` → registered
transforms → :class:`FeaturePipeline`), because ``configuration/`` may not import
``features/`` and the engine is the layer that may import both.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

import polars as pl
import structlog

from qtros.alpha.base import IStrategy
from qtros.configuration.hashing import ConfigHashes
from qtros.configuration.loader import ResolvedBacktestConfig
from qtros.configuration.models.features import FeatureSetConfig
from qtros.configuration.registry import StrategyRegistry, strategy_registry
from qtros.domain.errors import ConfigurationError
from qtros.domain.feature_contracts import FeatureFrame
from qtros.domain.identifiers import SequentialIdFactory
from qtros.domain.instrument import InstrumentSpec
from qtros.domain.timekeeping import SessionCalendar
from qtros.engine.backtester import Backtester, BacktestResult
from qtros.engine.portfolio import Portfolio
from qtros.execution.broker import SimulatedBroker
from qtros.execution.friction import StandardFrictionModel
from qtros.features.pipeline import FeaturePipeline
from qtros.features.transforms import build_transform
from qtros.features.transforms.base import TransformBase
from qtros.risk.engine import RiskEngine

_LOGGER: Final = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RunComponents:
    """Everything built for one run, kept so the persistence layer can describe it.

    Attributes:
        resolved: The flattened configuration.
        hashes: Its three Directive 10 hashes.
        instrument: Contract specification with friction overrides applied.
        calendar: Session calendar.
        strategy: Constructed strategy.
        ids: Identifier factory, prefixed by the run hash.
        portfolio: Funded, flat book.
        broker: Matching engine.
        risk: Risk gate.
        feature_frame: The frame the run executes against.
        backtester: The assembled run loop.
    """

    resolved: ResolvedBacktestConfig
    hashes: ConfigHashes
    instrument: InstrumentSpec
    calendar: SessionCalendar
    strategy: IStrategy
    ids: SequentialIdFactory
    portfolio: Portfolio
    broker: SimulatedBroker
    risk: RiskEngine
    feature_frame: FeatureFrame
    backtester: Backtester

    def run(self) -> BacktestResult:
        """Execute the run."""
        return self.backtester.run()


# --------------------------------------------------------------------------------------
# Features
# --------------------------------------------------------------------------------------


def _plain(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Return transform parameters as plain Python values.

    Decimals are passed through unchanged; transforms that need a float convert at their
    own boundary, which keeps the exact value in the spec hash.
    """
    return {key: value for key, value in parameters.items()}


def build_transforms(features: FeatureSetConfig) -> tuple[TransformBase, ...]:
    """Construct every transform a feature set names.

    Args:
        features: The feature set configuration.

    Returns:
        Conformance-checked transforms, in declaration order.

    Raises:
        ConfigurationError: From the registry, for an unknown transform name.
    """
    return tuple(
        build_transform(ref.transform, _plain(ref.parameters)) for ref in features.transforms
    )


def compute_features(resolved: ResolvedBacktestConfig, bars: pl.DataFrame) -> FeatureFrame:
    """Run the configured feature set over a bar frame.

    Args:
        resolved: The flattened configuration.
        bars: Layer 0 output, ascending by timestamp.

    Returns:
        An audited, sealed feature frame.
    """
    features = resolved.features
    return FeaturePipeline().compute(
        bars,
        build_transforms(features),
        lag_policy=features.lag_policy,
        audit_probe_count=features.audit_probe_count,
        audit_seed=features.audit_seed,
    )


# --------------------------------------------------------------------------------------
# Run graph
# --------------------------------------------------------------------------------------


def build_run(
    resolved: ResolvedBacktestConfig,
    feature_frame: FeatureFrame,
    *,
    registry: StrategyRegistry = strategy_registry,
    session_column: str | None = None,
    allow_unreconciled: bool = False,
) -> RunComponents:
    """Assemble a run from configuration and a computed feature frame.

    Args:
        resolved: The flattened configuration.
        feature_frame: Output of :func:`compute_features` or an equivalent pipeline call.
        registry: Strategy registry to resolve against.
        session_column: Session index column in the frame, if any.
        allow_unreconciled: Pass through to the backtester. Diagnosis only.

    Returns:
        The components and the assembled backtester.

    Raises:
        ConfigurationError: If the registered strategy does not identify itself as the
            configuration claims, or a schema-only friction model is selected.
    """
    hashes: ConfigHashes = resolved.hashes()
    ids = SequentialIdFactory(hashes.run_id_fragment)

    instrument: InstrumentSpec = resolved.friction.apply_to_instrument(resolved.instrument.to_spec())
    calendar: SessionCalendar = resolved.calendar.to_calendar(
        rollover_time_local=resolved.instrument.rollover_time_local,
        rollover_tz=resolved.instrument.rollover_tz,
    )

    record = registry.resolve(resolved.strategy.strategy_id, resolved.strategy.strategy_version)
    strategy: IStrategy = record.factory(resolved.strategy.parameters)
    if (strategy.strategy_id, strategy.strategy_version) != (
        resolved.strategy.strategy_id,
        resolved.strategy.strategy_version,
    ):
        raise ConfigurationError(
            "registered strategy does not identify itself as the configuration claims",
            expected=(resolved.strategy.strategy_id, resolved.strategy.strategy_version),
            actual=(strategy.strategy_id, strategy.strategy_version),
        )

    friction = StandardFrictionModel(
        slippage_ticks=resolved.friction.slippage_value,
        acknowledge_zero_friction=resolved.friction.acknowledge_zero_friction,
    )
    execution = resolved.backtest.execution
    broker = SimulatedBroker(
        friction,
        ids,
        timing=execution.timing,
        path_assumption=execution.path_assumption,
        zero_volume_policy=execution.zero_volume_policy,
    )
    initial_cash: Decimal = resolved.backtest.initial_cash
    portfolio = Portfolio(instrument, initial_cash, ids)
    risk = RiskEngine(
        resolved.risk,
        instrument,
        ids,
        initial_equity=initial_cash,
        sub_min_lot_policy=execution.sub_min_lot_policy,
    )
    backtester = Backtester(
        feature_frame=feature_frame,
        strategy=strategy,
        instrument=instrument,
        portfolio=portfolio,
        broker=broker,
        risk=risk,
        ids=ids,
        session_column=session_column,
        allow_unreconciled=allow_unreconciled,
    )

    _LOGGER.info(
        "engine.run_built",
        run_hash=hashes.run_hash[:16],
        config_hash=hashes.display,
        strategy=strategy.strategy_id,
        version=strategy.strategy_version,
        instrument=str(instrument.instrument_id),
        bars=feature_frame.height,
        watermarked=execution.is_watermarked,
    )
    return RunComponents(
        resolved=resolved,
        hashes=hashes,
        instrument=instrument,
        calendar=calendar,
        strategy=strategy,
        ids=ids,
        portfolio=portfolio,
        broker=broker,
        risk=risk,
        feature_frame=feature_frame,
        backtester=backtester,
    )


def run_from_config(
    resolved: ResolvedBacktestConfig,
    bars: pl.DataFrame,
    *,
    registry: StrategyRegistry = strategy_registry,
    session_column: str | None = None,
) -> tuple[RunComponents, BacktestResult]:
    """Compute features, build the run, and execute it.

    Args:
        resolved: The flattened configuration.
        bars: Layer 0 bar frame.
        registry: Strategy registry.
        session_column: Session index column, if the feature set emits one.

    Returns:
        The components and the result.
    """
    frame = compute_features(resolved, bars)
    components = build_run(resolved, frame, registry=registry, session_column=session_column)
    return components, components.run()


__all__: Sequence[str] = (
    "RunComponents",
    "build_run",
    "build_transforms",
    "compute_features",
    "run_from_config",
)
