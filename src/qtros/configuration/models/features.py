"""Feature set configuration (``config/features/*.yaml``).

A feature set names registered transforms and their parameters. Transforms resolve by
registry name, never by import path (ARCH-002 D-4 applied to Layer 0.5). Construction
happens in the engine builder, because ``configuration/`` may not import ``features/``.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import Field, model_validator

from qtros.configuration.models.base import QtrosConfig, StrictDecimal
from qtros.domain.feature_contracts import LagPolicy


class TransformRefConfig(QtrosConfig):
    """One transform by registry name.

    Attributes:
        transform: Registry key, e.g. ``atr``.
        parameters: Constructor keyword arguments. Decimals quoted; floats rejected.
    """

    transform: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    parameters: Mapping[str, StrictDecimal | int | bool | str] = Field(default_factory=dict)


class FeatureSetConfig(QtrosConfig):
    """A named, ordered set of transforms.

    Attributes:
        feature_set_id: Key; matches the file under ``config/features/``.
        transforms: Transforms to apply. Execution order is resolved by dependency.
        lag_policy: Where the T-1 shift is applied. ``DEFER_TO_ENGINE`` for any run.
        audit_probe_count: Prefix-invariance probes.
        audit_seed: Seed for deterministic probe selection.
    """

    feature_set_id: str = Field(min_length=1)
    transforms: tuple[TransformRefConfig, ...]
    lag_policy: LagPolicy = LagPolicy.DEFER_TO_ENGINE
    audit_probe_count: int = Field(gt=0, default=25)
    audit_seed: int = 0

    @model_validator(mode="after")
    def _non_empty(self) -> FeatureSetConfig:
        """Require at least one transform."""
        if not self.transforms:
            raise ValueError("a feature set needs at least one transform")
        return self
