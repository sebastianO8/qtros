"""Layer 3 — the risk gate (ARCH-001 §1.5; PI rulings G-1, G-2; ASS-008, ASS-009).

This is the only package permitted to import :data:`qtros.domain.orders.RISK_APPROVAL`,
and therefore the only place an :class:`~qtros.domain.orders.ApprovedOrder` can come
from. A strategy emits intents; the broker accepts orders; this layer is the one-way
door between them.
"""

from __future__ import annotations

from qtros.risk.engine import ApprovalResult, RiskEngine, RiskEvent
from qtros.risk.limits import (
    IRiskRule,
    MaxLotRule,
    MaxNotionalExposureRule,
    RuleVerdict,
    notional_value,
)
from qtros.risk.sizing import FixedFractionalSizer, ISizer, SizingResult

__all__ = [
    "ApprovalResult",
    "FixedFractionalSizer",
    "IRiskRule",
    "ISizer",
    "MaxLotRule",
    "MaxNotionalExposureRule",
    "RiskEngine",
    "RiskEvent",
    "RuleVerdict",
    "SizingResult",
    "notional_value",
]
