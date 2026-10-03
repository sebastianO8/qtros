"""Friction modelling (Directive 2).

Every cost is computed separately and carried separately onto the fill: commission,
spread, slippage. Netting them into a single "costs" figure would make the most useful
diagnostic in Phase 1 unavailable — when a strategy's edge dies under realistic friction,
the first question is *which* friction killed it, and a single number cannot answer it.

Slippage is always adverse. There is no configuration that makes it favourable, because a
model in which slippage sometimes helps is a model that quietly manufactures edge on high
trade counts, and 400 trades is the floor this project gates on.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

from qtros.domain.errors import ConfigurationError

if TYPE_CHECKING:
    from qtros.domain.enums import Direction
    from qtros.domain.instrument import InstrumentSpec
    from qtros.domain.market import BarData

ZERO: Final[Decimal] = Decimal("0")


@dataclass(frozen=True, slots=True)
class FrictionCharges:
    """Cost components for one fill.

    Spread and slippage are returned in **price units**, not cash. They are embedded in
    the fill price rather than deducted from the balance, which is how they actually
    occur: a buyer does not pay mid and then receive a spread invoice, they pay the ask.
    The broker converts them to cash *after* tick rounding, so the reported cost equals
    the price degradation that really happened rather than the degradation that was
    intended.

    Commission is genuine cash and is charged separately, because it genuinely is a
    separate debit.

    Attributes:
        spread_price: Adverse price move attributable to crossing the spread.
        slippage_price: Adverse price move beyond the spread.
        commission: Cash commission charged for this side.
    """

    spread_price: Decimal
    slippage_price: Decimal
    commission: Decimal

    @property
    def price_adjustment(self) -> Decimal:
        """Return the total adverse price move before tick rounding."""
        return self.spread_price + self.slippage_price


@runtime_checkable
class IFrictionModel(Protocol):
    """Computes the cost of executing an order."""

    def charges(
        self,
        *,
        direction: Direction,
        quantity: Decimal,
        reference_price: Decimal,
        bar: BarData,
        spec: InstrumentSpec,
    ) -> FrictionCharges:
        """Return every cost component for one fill."""
        ...


@dataclass(frozen=True, slots=True)
class StandardFrictionModel:
    """Fixed-spread, fixed-slippage friction. Adequate and honest for Phase 1.

    Args:
        slippage_ticks: Adverse ticks applied beyond the spread.
        spread_ticks_override: Spread in ticks. When ``None``, the instrument's typical
            spread is used.
        acknowledge_zero_friction: Required to be ``True`` before any zero-cost
            configuration is accepted. Frictionless backtests are the single most common
            source of a phantom edge and Directive 2 forbids them, so disabling costs is
            possible but cannot happen by accident or by omission.

    Raises:
        ConfigurationError: If slippage is negative, or costs are zeroed without the
            explicit acknowledgement.
    """

    slippage_ticks: Decimal = Decimal("0.5")
    spread_ticks_override: Decimal | None = None
    acknowledge_zero_friction: bool = False

    def __post_init__(self) -> None:
        """Validate the configuration.

        Raises:
            ConfigurationError: On negative slippage or unacknowledged zero friction.
        """
        if self.slippage_ticks < ZERO:
            raise ConfigurationError(
                "Slippage cannot be negative; slippage that helps the trader is not a "
                "conservative assumption, it is manufactured edge",
                slippage_ticks=str(self.slippage_ticks),
            )
        zeroed: bool = self.slippage_ticks == ZERO and self.spread_ticks_override == ZERO
        if zeroed and not self.acknowledge_zero_friction:
            raise ConfigurationError(
                "Zero-friction configuration requires acknowledge_zero_friction=True; "
                "Directive 2 requires realistic costs and this must be a deliberate act"
            )

    def charges(
        self,
        *,
        direction: Direction,
        quantity: Decimal,
        reference_price: Decimal,
        bar: BarData,
        spec: InstrumentSpec,
    ) -> FrictionCharges:
        """Return the cost components for one fill.

        Args:
            direction: Side of the fill. Unused: this model's costs are symmetric, and the
                broker applies the sign.
            quantity: Lots transacted.
            reference_price: Price before friction, normally the execution bar's open.
            bar: Execution bar. Unused by this model but part of the protocol, since a
                volatility-scaled slippage model needs it and widening the signature later
                would touch every call site.
            spec: Contract specification.

        Returns:
            Spread and slippage in price units, and commission in cash.
        """
        del bar, reference_price, direction  # Fixed, symmetric model; retained for protocol.

        spread_ticks: Decimal = (
            spec.typical_spread_ticks
            if self.spread_ticks_override is None
            else self.spread_ticks_override
        )
        return FrictionCharges(
            spread_price=(spread_ticks * spec.tick_size) / Decimal("2"),
            slippage_price=self.slippage_ticks * spec.tick_size,
            commission=spec.commission_for(quantity),
        )
