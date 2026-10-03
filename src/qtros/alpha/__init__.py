"""Layer 1 — strategies (ARCH-001 C-9; ARCH-002 D-4; DIR-001; PI ruling G-4).

A strategy is a functional core: configuration in, decision out, state owned by the
engine. It sees a :class:`~qtros.domain.signals.BarContext` and nothing else, and it may
only read features a registered Layer 0.5 transform produced.
"""

from __future__ import annotations

from qtros.alpha.base import (
    IStrategy,
    StrategyBase,
    StrategyProvenanceError,
    register_strategy,
    verify_feature_provenance,
)

__all__ = [
    "IStrategy",
    "StrategyBase",
    "StrategyProvenanceError",
    "register_strategy",
    "verify_feature_provenance",
]
