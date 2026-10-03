"""Friction configuration (Directive 2; ARCH-002 §4.6; ASS-003, ASS-006).

The schema has no zero default and requires an explicit acknowledgement before costs can
be disabled. Frictionless backtests are the single most common source of a phantom edge,
so they are awkward to express on purpose and impossible to express by omission.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from pydantic import Field, model_validator

from qtros.configuration.models.base import QtrosConfig, StrictDecimal
from qtros.domain.enums import CommissionModel, SlippageModel, SpreadModel
from qtros.domain.instrument import InstrumentSpec

ZERO = Decimal("0")


class FrictionConfig(QtrosConfig):
    """Cost model parameters.

    Attributes:
        friction_id: Key; matches the file under ``config/friction/``.
        commission_model: How commission is charged. ``PER_NOTIONAL`` is schema-only in
            Phase 1 and refused at build time.
        commission_value: Round-turn commission per lot under ``PER_LOT``.
        spread_model: Fixed ticks, or the instrument's declared typical spread.
        spread_value_ticks: Spread in ticks when ``FIXED_TICKS``.
        slippage_model: Fixed ticks, or an ATR fraction (schema-only in Phase 1).
        slippage_value: Ticks under ``FIXED_TICKS``.
        acknowledge_zero_friction: Must be ``True`` for any configuration whose costs are
            all zero.
    """

    friction_id: str = Field(min_length=1)
    commission_model: CommissionModel
    commission_value: StrictDecimal = Decimal("0")
    spread_model: SpreadModel
    spread_value_ticks: StrictDecimal | None = None
    slippage_model: SlippageModel
    slippage_value: StrictDecimal
    acknowledge_zero_friction: bool = False

    @model_validator(mode="after")
    def _rules(self) -> FrictionConfig:
        """Enforce the Directive 2 acknowledgement and model/value pairing.

        Returns:
            The validated model.

        Raises:
            ValueError: On negative values, a missing spread figure, or unacknowledged
                zero friction.
        """
        if self.slippage_value < ZERO or self.commission_value < ZERO:
            raise ValueError("friction values cannot be negative")
        if self.spread_model is SpreadModel.FIXED_TICKS:
            if self.spread_value_ticks is None:
                raise ValueError("spread_model FIXED_TICKS requires spread_value_ticks")
            if self.spread_value_ticks < ZERO:
                raise ValueError("spread_value_ticks cannot be negative")
        elif self.spread_value_ticks is not None:
            raise ValueError("spread_value_ticks is only meaningful under FIXED_TICKS")
        if self.commission_model is CommissionModel.NONE and self.commission_value != ZERO:
            raise ValueError("commission_model NONE requires commission_value 0")

        zero_commission = (
            self.commission_model is CommissionModel.NONE or self.commission_value == ZERO
        )
        zero_spread = (
            self.spread_model is SpreadModel.FIXED_TICKS and self.spread_value_ticks == ZERO
        )
        if zero_commission and zero_spread and self.slippage_value == ZERO:
            if not self.acknowledge_zero_friction:
                raise ValueError(
                    "zero-friction configuration requires acknowledge_zero_friction: true; "
                    "Directive 2 requires realistic costs and this must be a deliberate act"
                )
        elif self.acknowledge_zero_friction:
            raise ValueError(
                "acknowledge_zero_friction is set but the configuration is not frictionless"
            )
        return self

    def apply_to_instrument(self, spec: InstrumentSpec) -> InstrumentSpec:
        """Return the instrument with this configuration's commission and spread applied.

        The friction model reads commission and typical spread from the instrument, so a
        friction config that overrides either does so by handing the engine an adjusted
        spec rather than by altering the friction model's arithmetic.

        Args:
            spec: Instrument as declared.

        Returns:
            A copy with ``commission_per_lot`` and, under ``FIXED_TICKS``,
            ``typical_spread_ticks`` replaced.

        Raises:
            ValueError: If a schema-only model is selected.
        """
        if self.commission_model is CommissionModel.PER_NOTIONAL:
            raise ValueError("PER_NOTIONAL commission is not implemented in Phase 1")
        if self.slippage_model is SlippageModel.ATR_FRACTION:
            raise ValueError("ATR_FRACTION slippage is not implemented in Phase 1")

        commission = ZERO if self.commission_model is CommissionModel.NONE else self.commission_value
        spread = (
            self.spread_value_ticks
            if self.spread_model is SpreadModel.FIXED_TICKS and self.spread_value_ticks is not None
            else spec.typical_spread_ticks
        )
        return replace(spec, commission_per_lot=commission, typical_spread_ticks=spread)
