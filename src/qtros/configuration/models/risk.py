"""Risk limits configuration (ARCH-001 C-1; ARCH-002 §4.6; PI rulings G-1, G-2).

G-1 adds two exposure caps in place of a margin model (ASS-008): a hard ``max_lot`` and a
``max_notional_pct`` of equity. G-2 fixes the drawdown basis to the adverse-first intrabar
mark-to-market extreme (ASS-009); ``max_drawdown_basis`` exists so a comparison run can be
labelled, but the engine refuses to gate on anything but mark-to-market.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field, model_validator

from qtros.configuration.models.base import QtrosConfig, StrictDecimal
from qtros.domain.enums import BreachAction, DrawdownBasis

_HUNDRED = Decimal("100")


class RiskLimitsConfig(QtrosConfig):
    """Every limit the risk engine enforces.

    Attributes:
        risk_limits_id: Key; matches the file under ``config/risk/``.
        max_drawdown_pct: Peak-to-trough limit, percent of peak equity.
        max_drawdown_basis: Basis the limit is measured on. Mark-to-market per C-1.
        risk_per_trade_pct: Equity fraction at risk between entry and stop.
        atr_stop_multiple: Stop distance in ATR multiples, for strategies that size from
            ATR. Informational to the engine; the stop itself arrives on the intent.
        atr_lookback_bars: ATR window the multiple refers to.
        max_concurrent_positions: Open positions allowed at once. Phase 1 engine is
            single-position, so this is 1.
        max_lot: Hard cap on any single order's size (G-1).
        max_notional_pct: Cap on total open notional as a percent of closed equity (G-1).
        max_daily_loss_pct: Phase 2 prop-firm daily loss limit. Schema only in Phase 1.
        daily_consistency_pct: Phase 2 prop-firm consistency rule. Schema only.
        on_limit_breach: Whether a halt lasts the session or the run.
    """

    risk_limits_id: str = Field(min_length=1)
    max_drawdown_pct: StrictDecimal
    max_drawdown_basis: DrawdownBasis = DrawdownBasis.MARK_TO_MARKET
    risk_per_trade_pct: StrictDecimal
    atr_stop_multiple: StrictDecimal
    atr_lookback_bars: int = Field(gt=0)
    max_concurrent_positions: int = Field(gt=0, default=1)
    max_lot: StrictDecimal
    max_notional_pct: StrictDecimal
    max_daily_loss_pct: StrictDecimal | None = None
    daily_consistency_pct: StrictDecimal | None = None
    on_limit_breach: BreachAction = BreachAction.HALT_RUN

    @model_validator(mode="after")
    def _ranges(self) -> RiskLimitsConfig:
        """Keep every percentage in a sane range.

        Returns:
            The validated model.

        Raises:
            ValueError: On a percentage outside (0, 100], a non-positive multiple, or a
                non-positive lot cap.
        """
        for name in ("max_drawdown_pct", "risk_per_trade_pct"):
            value: Decimal = getattr(self, name)
            if not 0 < value <= _HUNDRED:
                raise ValueError(f"{name} must be in (0, 100]")
        if self.max_notional_pct <= 0:
            raise ValueError("max_notional_pct must be positive")
        if self.atr_stop_multiple <= 0:
            raise ValueError("atr_stop_multiple must be positive")
        if self.max_lot <= 0:
            raise ValueError("max_lot must be positive")
        for name in ("max_daily_loss_pct", "daily_consistency_pct"):
            optional: Decimal | None = getattr(self, name)
            if optional is not None and not 0 < optional <= _HUNDRED:
                raise ValueError(f"{name} must be in (0, 100] when set")
        return self

    @property
    def max_drawdown_fraction(self) -> Decimal:
        """Return the drawdown limit as a fraction of peak equity."""
        return self.max_drawdown_pct / _HUNDRED

    @property
    def risk_per_trade_fraction(self) -> Decimal:
        """Return the per-trade risk as a fraction of equity."""
        return self.risk_per_trade_pct / _HUNDRED

    @property
    def max_notional_fraction(self) -> Decimal:
        """Return the notional cap as a fraction of closed equity."""
        return self.max_notional_pct / _HUNDRED
