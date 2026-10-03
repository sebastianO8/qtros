"""Layer 0.5 — the feature vector pipeline.

Importing this package registers every built-in transform, so ``build_transform`` can
resolve a config-declared feature set without any further imports.
"""

from __future__ import annotations

from qtros.features import transforms
from qtros.features.lag_audit import LagAuditor, verify_detector
from qtros.features.pipeline import (
    FeaturePipeline,
    describe_plan,
    resolve_execution_order,
    resolve_warmup,
)

__all__ = [
    "FeaturePipeline",
    "LagAuditor",
    "describe_plan",
    "resolve_execution_order",
    "resolve_warmup",
    "transforms",
    "verify_detector",
]
