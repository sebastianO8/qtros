"""Exposure rules (PI ruling G-1; ASS-008).

Phase 1 has no margin model. In its place, two independent caps bound every entry:

* :class:`MaxLotRule` - a hard ceiling on any single order's size.
* :class:`MaxNotionalExposureRule` - total open notional may not exceed a declared share
  of closed equity.

A breach is a **rejection**, never a resize. Resizing down would let the strategy's
realised risk drift silently from its declared risk, and the rejection stream exists so
that mis-sizing is visible rather than absorbed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

from qtros.domain.enums import OrderRejectionCode
from qtros.domain.errors import ConfigurationError
from qtros.domain.money import quantize_money

if TYPE_CHECKING:
    from qtros.domain.instrument import InstrumentSpec
    from qtros.domain.orders import TradeIntent
    from qtros.domain.signals import PortfolioView

ZERO: Final[Decimal] = Decimal("0")


@dataclass(frozen=True, slots=True)
class RuleVerdict:
    """One rule's decision on one proposed order.

    Attributes:
        allowed: Whether the order may proceed.
        code: Enumerated rejection reason when not allowed.
        detail: Human-readable context for the rejection stream.
    """

    allowed: bool
    code: OrderRejectionCode | None = None
    detail: str = ""

    @classmethod
    def ok(cls) -> RuleVerdict:
        """Return an allowing verdict."""
        return cls(allowed=True)

    @classmethod
    def reject(cls, code: OrderRejectionCode, detail: str) -> RuleVerdict:
        """Return a rejecting verdict."""
        return cls(allowed=False, code=code, detail=detail)


@runtime_checkable
class IRiskRule(Protocol):
    """A pre-approval check on a sized entry."""

    @property
    def name(self) -> str:
        """Return a stable rule name for logs and events."""
        ...

    def evaluate(
        self,
        *,
        intent: TradeIntent,
        quantity: Decimal,
        reference_price: Decimal,
        view: PortfolioView,
        spec: InstrumentSpec,
    ) -> RuleVerdict:
        """Decide whether a sized entry may proceed."""
        ...


def notional_value(quantity: Decimal, price: Decimal, spec: InstrumentSpec) -> Decimal:
    """Return the quote-currency notional of a position.

    Args:
        quantity: Lots.
        price: Price per unit of base.
        spec: Contract specification.

    Returns:
        ``quantity x contract_size x price``, quantized to money precision.
    """
    return quantize_money(quantity * spec.contract_size * price)


@dataclass(frozen=True, slots=True)
class MaxLotRule:
    """Hard cap on a single order's size (G-1).

    Args:
        max_lot: Largest permitted order, in lots.

    Raises:
        ConfigurationError: If the cap is not positive.
    """

    max_lot: Decimal

    def __post_init__(self) -> None:
        """Validate the cap."""
        if self.max_lot <= ZERO:
            raise ConfigurationError("max_lot must be positive", max_lot=str(self.max_lot))

    @property
    def name(self) -> str:
        """Return the rule name."""
        return "max_lot"

    def evaluate(
        self,
        *,
        intent: TradeIntent,
        quantity: Decimal,
        reference_price: Decimal,
        view: PortfolioView,
        spec: InstrumentSpec,
    ) -> RuleVerdict:
        """Reject any order larger than the cap.

        Args:
            intent: The originating intent. Unused; part of the protocol.
            quantity: Sized order quantity.
            reference_price: Decision-bar close. Unused.
            view: Account state. Unused.
            spec: Contract specification. Unused.

        Returns:
            The verdict.
        """
        del intent, reference_price, view, spec
        if quantity > self.max_lot:
            return RuleVerdict.reject(
                OrderRejectionCode.EXCEEDS_MAX_LOT,
                f"quantity {quantity} exceeds the hard lot cap {self.max_lot}",
            )
        return RuleVerdict.ok()


@dataclass(frozen=True, slots=True)
class MaxNotionalExposureRule:
    """Cap on total open notional as a fraction of closed equity (G-1).

    The proposed order's notional is added to whatever is already open - regardless of
    direction, because a reversal briefly carries both legs' risk at the fill - and the
    sum is compared against the cap.

    Args:
        max_notional_fraction: Permitted open notional as a multiple of closed equity.
            ``3`` means three times equity, the equivalent of 3:1 leverage.

    Raises:
        ConfigurationError: If the fraction is not positive.
    """

    max_notional_fraction: Decimal

    def __post_init__(self) -> None:
        """Validate the cap."""
        if self.max_notional_fraction <= ZERO:
            raise ConfigurationError(
                "max_notional_fraction must be positive",
                max_notional_fraction=str(self.max_notional_fraction),
            )

    @property
    def name(self) -> str:
        """Return the rule name."""
        return "max_notional_exposure"

    def evaluate(
        self,
        *,
        intent: TradeIntent,
        quantity: Decimal,
        reference_price: Decimal,
        view: PortfolioView,
        spec: InstrumentSpec,
    ) -> RuleVerdict:
        """Reject an entry that would push open notional past the cap.

        Args:
            intent: The originating intent. Unused; part of the protocol.
            quantity: Sized order quantity.
            reference_price: Price the notional is valued at - the decision-bar close.
            view: Account state, for closed equity and the open position.
            spec: Contract specification.

        Returns:
            The verdict.
        """
        del intent
        proposed: Decimal = notional_value(quantity, reference_price, spec)
        open_notional: Decimal = ZERO
        if view.open_position is not None:
            open_notional = notional_value(
                view.open_position.quantity, view.open_position.avg_entry_price, spec
            )
        cap: Decimal = quantize_money(view.closed_equity * self.max_notional_fraction)
        total: Decimal = open_notional + proposed
        if total > cap:
            return RuleVerdict.reject(
                OrderRejectionCode.EXCEEDS_NOTIONAL_CAP,
                f"open notional {open_notional} + proposed {proposed} = {total} exceeds "
                f"cap {cap} ({self.max_notional_fraction}x closed equity {view.closed_equity})",
            )
        return RuleVerdict.ok()
