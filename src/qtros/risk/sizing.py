"""Position sizing.

Fixed-fractional sizing from the stop distance: the strategy says where it is wrong, the
risk engine says how much it may lose finding out, and the quantity follows. A strategy
that supplies no stop cannot be sized and is rejected rather than given a default — a
default stop is a risk decision nobody made.

Sizing basis: **closed equity**. Marking an open position's unrealised gain into the
sizing basis would let a winning open trade finance a larger next entry, which compounds
exposure precisely when the account is most extended. Flagged for PI confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

from qtros.domain.enums import Direction, OrderRejectionCode
from qtros.domain.errors import ConfigurationError
from qtros.domain.money import quantize_money

if TYPE_CHECKING:
    from qtros.domain.instrument import InstrumentSpec
    from qtros.domain.orders import TradeIntent
    from qtros.domain.signals import PortfolioView

ZERO: Final[Decimal] = Decimal("0")


@dataclass(frozen=True, slots=True)
class SizingResult:
    """Outcome of sizing one entry.

    Attributes:
        quantity: Lot-grid quantity, rounded **down**. May be zero.
        risk_amount: Cash the position would lose at the stop.
        stop_distance: Price distance from reference to stop.
        code: Rejection reason when sizing was impossible.
        detail: Context for the rejection stream.
    """

    quantity: Decimal
    risk_amount: Decimal
    stop_distance: Decimal
    code: OrderRejectionCode | None = None
    detail: str = ""

    @property
    def sized(self) -> bool:
        """Return whether a quantity was produced."""
        return self.code is None


@runtime_checkable
class ISizer(Protocol):
    """Turns an entry intent into a quantity."""

    def size(
        self,
        *,
        intent: TradeIntent,
        reference_price: Decimal,
        view: PortfolioView,
        spec: InstrumentSpec,
    ) -> SizingResult:
        """Size an entry."""
        ...


@dataclass(frozen=True, slots=True)
class FixedFractionalSizer:
    """Risk a fixed fraction of closed equity between entry and stop.

    Args:
        risk_fraction: Fraction of closed equity at risk per trade, e.g. ``0.01``.

    Raises:
        ConfigurationError: If the fraction is outside (0, 1].
    """

    risk_fraction: Decimal

    def __post_init__(self) -> None:
        """Validate the fraction."""
        if not ZERO < self.risk_fraction <= Decimal("1"):
            raise ConfigurationError(
                "risk_fraction must be in (0, 1]", risk_fraction=str(self.risk_fraction)
            )

    def size(
        self,
        *,
        intent: TradeIntent,
        reference_price: Decimal,
        view: PortfolioView,
        spec: InstrumentSpec,
    ) -> SizingResult:
        """Size from the stop distance, or honour an explicit request.

        Args:
            intent: Entry intent. ``requested_quantity`` bypasses the risk calculation but
                not the lot grid or any downstream rule; ``stop_loss`` is otherwise
                required.
            reference_price: Decision-bar close.
            view: Account state.
            spec: Contract specification.

        Returns:
            The sizing result.
        """
        if intent.requested_quantity is not None:
            quantity: Decimal = spec.round_quantity(intent.requested_quantity)
            return SizingResult(quantity=quantity, risk_amount=ZERO, stop_distance=ZERO)

        stop: Decimal | None = intent.stop_loss
        if stop is None:
            return SizingResult(
                ZERO,
                ZERO,
                ZERO,
                code=OrderRejectionCode.NO_STOP_FOR_SIZING,
                detail="entry intent carries no stop_loss; fixed-fractional sizing needs one",
            )
        wrong_side: bool = (
            stop >= reference_price
            if intent.direction is Direction.LONG
            else stop <= reference_price
        )
        if wrong_side:
            return SizingResult(
                ZERO,
                ZERO,
                ZERO,
                code=OrderRejectionCode.NO_STOP_FOR_SIZING,
                detail=f"stop {stop} is not on the losing side of reference {reference_price}",
            )

        distance: Decimal = abs(reference_price - stop)
        risk_amount: Decimal = quantize_money(view.closed_equity * self.risk_fraction)
        raw: Decimal = risk_amount / (distance * spec.contract_size)
        return SizingResult(
            quantity=spec.round_quantity(raw),
            risk_amount=risk_amount,
            stop_distance=distance,
        )
