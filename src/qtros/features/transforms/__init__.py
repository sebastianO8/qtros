"""Feature transform library.

Importing this package registers every built-in transform. Registration is a side effect
of import, so any new transform module must be imported here or it will be invisible to
``build_transform`` and a config referencing it will fail with "unknown transform" — which
is the correct failure, but only if the cause is documented where someone will read it.
"""

from __future__ import annotations

from qtros.features.transforms._control import CenteredMeanControl
from qtros.features.transforms.atr import (
    TRUE_RANGE_COLUMN,
    AtrMethod,
    AverageTrueRange,
    TrueRange,
)
from qtros.features.transforms.base import (
    BASE_COLUMNS,
    build_transform,
    register_transform,
    registered_transforms,
    safe_divide,
)
from qtros.features.transforms.vwap import AnchoredVwap
from qtros.features.transforms.zscore import RollingZScore

__all__ = [
    "BASE_COLUMNS",
    "TRUE_RANGE_COLUMN",
    "AnchoredVwap",
    "AtrMethod",
    "AverageTrueRange",
    "CenteredMeanControl",
    "RollingZScore",
    "TrueRange",
    "build_transform",
    "register_transform",
    "registered_transforms",
    "safe_divide",
]
