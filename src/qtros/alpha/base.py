"""Strategy protocol, base class, registration decorator, and the G-4 provenance check.

**DIR-001 at the consumer (G-4).** The producer side of DIR-001 is the pipeline's
:class:`~qtros.domain.feature_contracts.SealedFeatureSet`: every column a registered
transform produced carries a seal. This module closes the consumer side. A strategy
declares the features it reads; before a run starts, :func:`verify_feature_provenance`
refuses any frame whose seal does not cover every declared feature, any frame carrying an
unsealed non-base column, and any sealed feature that is a non-causal control. After that
check, the only values a strategy can read through ``ctx.features`` are values whose
origin is known and whose causality was audited.

The residual risk ASS-002 names — inline arithmetic on ``ctx.bar`` — remains a review
matter. This check makes the *feature* path mechanical; it cannot see arithmetic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, TypeVar, runtime_checkable

import structlog

from qtros.configuration.registry import strategy_registry
from qtros.domain.errors import ConfigurationError
from qtros.domain.feature_contracts import Causality, FeatureFrame, SealedFeatureSet
from qtros.domain.signals import BarContext, StrategyDecision, StrategyState

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOGGER = structlog.get_logger(__name__)

ParameterValue = Decimal | int | bool | str
"""The value types a strategy parameter may take (ARCH-002 §4.5)."""


class StrategyProvenanceError(ConfigurationError):
    """A strategy's declared features are not covered by the frame's seal (G-4)."""


@runtime_checkable
class IStrategy(Protocol):
    """What the engine requires of a strategy."""

    @property
    def strategy_id(self) -> str:
        """Return the registry key."""
        ...

    @property
    def strategy_version(self) -> str:
        """Return the registered version."""
        ...

    @property
    def required_features(self) -> frozenset[str]:
        """Return every feature column this strategy reads."""
        ...

    def initial_state(self) -> StrategyState:
        """Return the state the engine starts the run with."""
        ...

    def evaluate(self, ctx: BarContext, state: StrategyState) -> StrategyDecision:
        """Decide on one bar. Pure: same inputs, same decision."""
        ...


class StrategyBase(ABC):
    """Convenience base: parameter storage, registry metadata, pure evaluation contract.

    Subclasses set ``strategy_id`` and ``strategy_version`` via :func:`register_strategy`
    and implement :meth:`evaluate` and :attr:`required_features`.

    Args:
        parameters: Strategy parameters from configuration. Stored read-only.
    """

    strategy_id: ClassVar[str] = ""
    strategy_version: ClassVar[str] = ""

    def __init__(self, parameters: Mapping[str, ParameterValue] | None = None) -> None:
        self._parameters: Mapping[str, ParameterValue] = MappingProxyType(dict(parameters or {}))

    @property
    def parameters(self) -> Mapping[str, ParameterValue]:
        """Return the read-only parameter mapping."""
        return self._parameters

    @property
    @abstractmethod
    def required_features(self) -> frozenset[str]:
        """Return every feature column this strategy reads."""

    def initial_state(self) -> StrategyState:
        """Return an empty state. Override to seed strategy-defined values."""
        return StrategyState()

    @abstractmethod
    def evaluate(self, ctx: BarContext, state: StrategyState) -> StrategyDecision:
        """Decide on one bar."""

    # -- parameter helpers -------------------------------------------------------------

    def param_decimal(self, name: str, default: Decimal | None = None) -> Decimal:
        """Return a parameter as an exact Decimal.

        Args:
            name: Parameter key.
            default: Value when the key is absent.

        Returns:
            The parameter. Strings are parsed exactly; ints are widened; floats never
            arrive because the schema refuses them.

        Raises:
            ConfigurationError: If the key is absent with no default, or is a bool.
        """
        value = self._parameters.get(name, default)
        if value is None:
            raise ConfigurationError(
                "missing strategy parameter", strategy=self.strategy_id, name=name
            )
        if isinstance(value, bool):
            raise ConfigurationError("boolean parameter used as a number", name=name)
        if isinstance(value, Decimal):
            return value
        if isinstance(value, int):
            return Decimal(value)
        try:
            return Decimal(str(value))
        except ArithmeticError as exc:
            raise ConfigurationError("parameter is not a decimal", name=name, value=value) from exc

    def param_int(self, name: str, default: int | None = None) -> int:
        """Return an integer parameter."""
        value = self._parameters.get(name, default)
        if value is None or isinstance(value, bool) or not isinstance(value, int):
            raise ConfigurationError("missing or non-integer strategy parameter", name=name)
        return value

    def param_str(self, name: str, default: str | None = None) -> str:
        """Return a string parameter."""
        value = self._parameters.get(name, default)
        if not isinstance(value, str) or not value:
            raise ConfigurationError("missing or empty string strategy parameter", name=name)
        return value

    def param_bool(self, name: str, default: bool | None = None) -> bool:
        """Return a boolean parameter."""
        value = self._parameters.get(name, default)
        if not isinstance(value, bool):
            raise ConfigurationError("missing or non-boolean strategy parameter", name=name)
        return value


_S = TypeVar("_S", bound=type[StrategyBase])


def register_strategy(strategy_id: str, strategy_version: str) -> Any:
    """Class decorator: stamp identity and register with the strategy registry (D-4).

    Args:
        strategy_id: Stable ``snake_case`` key referenced from YAML.
        strategy_version: ``MAJOR.MINOR.PATCH`` of the logic.

    Returns:
        The decorator.
    """

    def decorator(cls: _S) -> _S:
        if not (isinstance(cls, type) and issubclass(cls, StrategyBase)):
            raise ConfigurationError(
                "register_strategy applies to StrategyBase subclasses only",
                target=getattr(cls, "__qualname__", repr(cls)),
            )
        cls.strategy_id = strategy_id
        cls.strategy_version = strategy_version
        strategy_registry.register(
            strategy_id,
            strategy_version,
            cls,
            qualified_name=f"{cls.__module__}.{cls.__qualname__}",
        )
        return cls

    return decorator


def verify_feature_provenance(strategy: IStrategy, feature_frame: FeatureFrame) -> SealedFeatureSet:
    """Refuse to run a strategy against features whose origin is not sealed (G-4).

    Args:
        strategy: The strategy about to run.
        feature_frame: The frame it will run against.

    Returns:
        The frame's seal, for the engine to carry into the manifest.

    Raises:
        StrategyProvenanceError: If the frame carries no seal, carries an unsealed
            non-base column, lacks a declared feature, or seals a declared feature as a
            non-causal control.
    """
    sealed = feature_frame.sealed_features
    if sealed is None:
        raise StrategyProvenanceError(
            "feature frame carries no SealedFeatureSet; under DIR-001 a strategy may only "
            "read features whose producing transform is known",
            strategy=strategy.strategy_id,
        )
    try:
        sealed.verify_frame_columns(feature_frame.frame.columns)
    except Exception as exc:  # SchemaContractError carries the column list
        raise StrategyProvenanceError(
            "feature frame contains columns no registered transform produced",
            strategy=strategy.strategy_id,
            cause=str(exc),
        ) from exc

    missing: list[str] = []
    non_causal: list[str] = []
    absent_from_frame: list[str] = []
    for column in sorted(strategy.required_features):
        if column not in sealed:
            missing.append(column)
            continue
        if sealed.features[column].causality is not Causality.CAUSAL:
            non_causal.append(column)
        if column not in feature_frame.feature_columns:
            absent_from_frame.append(column)
    if missing or non_causal or absent_from_frame:
        raise StrategyProvenanceError(
            "strategy declares features the frame's seal does not legitimately provide",
            strategy=strategy.strategy_id,
            unsealed=missing,
            non_causal=non_causal,
            not_in_frame=absent_from_frame,
            sealed=sorted(sealed.features),
        )

    _LOGGER.info(
        "alpha.provenance_verified",
        strategy=strategy.strategy_id,
        version=strategy.strategy_version,
        features=sorted(strategy.required_features),
    )
    return sealed
