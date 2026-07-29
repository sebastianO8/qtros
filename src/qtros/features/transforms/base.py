"""Shared machinery for feature transforms.

Holds the transform registry and the small helpers every transform needs. Transforms are
resolved by registry name rather than by import path, for the same reason strategies are
(ARCH-002 D-4): a config archived in ``results/`` must still resolve after a refactor,
and a YAML file must never be able to name an arbitrary Python callable.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final, TypeVar

import polars as pl
import structlog

from qtros.domain.errors import ConfigurationError
from qtros.domain.feature_contracts import IFeatureTransform

_LOGGER: Final = structlog.get_logger(__name__)

# Columns supplied by Layer 0. A transform may read these but must never overwrite one.
BASE_COLUMNS: Final[frozenset[str]] = frozenset(
    {"ts_utc", "open", "high", "low", "close", "volume", "is_synthetic"}
)

TYPICAL_PRICE_EXPR: Final[pl.Expr] = (
    (pl.col("high") + pl.col("low") + pl.col("close")) / 3.0
)
"""Typical price, the standard VWAP weighting input."""

_REGISTRY: dict[str, type[IFeatureTransform]] = {}

_T = TypeVar("_T", bound=type)


def register_transform(name: str) -> Callable[[_T], _T]:
    """Register a transform class under a stable name.

    Args:
        name: Registry key referenced from ``config/features/*.yaml``.

    Returns:
        A class decorator that registers and returns the class unchanged.

    Raises:
        ConfigurationError: If the name is already taken. Silently shadowing a
            registration would let a config file resolve to a different computation than
            the one that produced an archived result.
    """

    def decorator(cls: _T) -> _T:
        if name in _REGISTRY:
            raise ConfigurationError(
                "Duplicate feature transform registration",
                name=name,
                existing=_REGISTRY[name].__name__,
                attempted=cls.__name__,
            )
        _REGISTRY[name] = cls  # type: ignore[assignment]
        _LOGGER.debug("features.transform_registered", name=name, cls=cls.__name__)
        return cls

    return decorator


def build_transform(name: str, parameters: Mapping[str, object] | None = None) -> IFeatureTransform:
    """Construct a registered transform from its name and parameters.

    Args:
        name: Registry key.
        parameters: Keyword arguments for the transform's constructor.

    Returns:
        The constructed transform.

    Raises:
        ConfigurationError: If the name is unknown or the parameters do not fit the
            constructor. An unknown name lists the available options, because the usual
            cause is a typo and the usual next question is "what are the valid names".
    """
    if name not in _REGISTRY:
        raise ConfigurationError(
            "Unknown feature transform",
            name=name,
            available=sorted(_REGISTRY),
        )
    try:
        return _REGISTRY[name](**dict(parameters or {}))  # type: ignore[operator]
    except TypeError as exc:
        raise ConfigurationError(
            "Feature transform parameters do not match its constructor",
            name=name,
            parameters=dict(parameters or {}),
            cause=str(exc),
        ) from exc


def registered_transforms() -> tuple[str, ...]:
    """Return every registered transform name, sorted."""
    return tuple(sorted(_REGISTRY))


def guard_column_name(column: str, transform_name: str) -> str:
    """Reject an output column that would shadow a Layer 0 column.

    Args:
        column: Proposed output column name.
        transform_name: Transform proposing it, for error context.

    Returns:
        The column name, unchanged.

    Raises:
        ConfigurationError: If the name collides with a base column. Overwriting ``close``
            with a derived value would corrupt every downstream feature and every
            execution price simultaneously, and would look like a market data problem.
    """
    if column in BASE_COLUMNS:
        raise ConfigurationError(
            "Feature output column would shadow a base market data column",
            column=column,
            transform=transform_name,
        )
    return column


def safe_divide(numerator: pl.Expr, denominator: pl.Expr) -> pl.Expr:
    """Divide, yielding null rather than infinity when the denominator is zero.

    A null propagates visibly and is trimmed or handled explicitly downstream. An
    infinity silently poisons every rolling statistic it touches and surfaces much later
    as an inexplicable signal.

    Args:
        numerator: Dividend expression.
        denominator: Divisor expression.

    Returns:
        An expression yielding the quotient, or null where the divisor is zero or null.
    """
    return (
        pl.when((denominator.is_not_null()) & (denominator != 0.0))
        .then(numerator / denominator)
        .otherwise(None)
    )
