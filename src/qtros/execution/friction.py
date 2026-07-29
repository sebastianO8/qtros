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
from typing import Final, Protocol, runtime_checkable

from qtros.domain.enums import Direction
from qtros.domain.errors import ConfigurationError
from qtros.domain.instrument import InstrumentSpec
from qtros.domain.market import BarData

ZERO: Final[Decimal] = Decimal("0")


@dataclass(frozen=True, slots=True)
class FrictionCharges:
    """Cost components for one fill.

    Attributes:
        price_adjustment: Total adverse move applied to the reference price, in price
            units. Always non-negative; the sign is applied by direction.
        spread_cost: Cash cost of crossing the spread.
        slippage_cost: Cash cost of adverse price impact beyond the spread.
        commission: Cash commission charged.
    """

    price_adjustment: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    commission: Decimal

    @property
    def total(self) -> Decimal:
        """Return the sum of all cash costs."""
        return self.spread_cost + self.slippage_cost + self.commission


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
            direction: Side of the fill.
            quantity: Lots transacted.
            reference_price: Price before friction, normally the execution bar's open.
            bar: Execution bar. Unused by this model but part of the protocol, since a
                volatility-scaled slippage model needs it and changing the signature
                later would touch every call site.
            spec: Contract specification.

        Returns:
            The itemised charges.
        """
        del bar, reference_price  # Fixed-cost model; retained for protocol compatibility.

        spread_ticks: Decimal = (
            spec.typical_spread_ticks
            if self.spread_ticks_override is None
            else self.spread_ticks_override
        )
        half_spread: Decimal = (spread_ticks * spec.tick_size) / Decimal("2")
        slippage: Decimal = self.slippage_ticks * spec.tick_size

        del direction  # Costs are symmetric; the sign is applied by the broker.

        contract_units: Decimal = quantity * spec.contract_size
        return FrictionCharges(
            price_adjustment=half_spread + slippage,
            spread_cost=half_spread * contract_units,
            slippage_cost=slippage * contract_units,
            commission=spec.commission_for(quantity),
        )
