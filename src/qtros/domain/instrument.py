"""Instrument specification and contract arithmetic (ARCH-001 C-7).

Every number that turns a price move into cash lives here: tick size, contract size, lot
increments, commission. Nothing else in the system is permitted to hold these as
literals, which is what makes the cross-asset validation from Section 7 of the
constitution possible — running identical rules across gold, silver, copper, and crude
requires instruments to be data rather than code.

All monetary and price quantities are :class:`decimal.Decimal`. Bar prices are floats in
the vectorised feature layer, where that is the right trade-off; the moment a price is
used to move cash it crosses into exact arithmetic, so that reconciliation can assert
exact equality rather than tolerate a tolerance that a real bug could hide beneath.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_FLOOR, ROUND_UP, Decimal
from typing import TYPE_CHECKING

from qtros.domain.enums import AssetClass, Direction
from qtros.domain.errors import ConfigurationError

if TYPE_CHECKING:
    from datetime import time

    from qtros.domain.identifiers import InstrumentId

ZERO: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """Everything needed to turn a price move into cash for one instrument.

    Attributes:
        instrument_id: Canonical key.
        display_name: Human-readable name.
        asset_class: Broad category.
        base_currency: Base currency of the pair, or the instrument's own currency.
        quote_currency: Currency in which PnL is denominated.
        tick_size: Smallest price increment.
        contract_size: Units of the base per one lot.
        min_lot: Smallest tradeable size.
        lot_step: Size increment above the minimum.
        typical_spread_ticks: Default half-spread input for the friction model.
        commission_per_lot: Round-turn commission charged per lot, halved per side.
        calendar_id: Session calendar key.
        rollover_time_local: Local wall-clock time at which the trading day rolls.
        rollover_tz: IANA zone of ``rollover_time_local``. For FX this is normally a New
            York time, so the UTC boundary shifts twice a year — writing it as a UTC
            constant would be both a magic constant and wrong for most of the year.
    """

    instrument_id: InstrumentId
    display_name: str
    asset_class: AssetClass
    base_currency: str
    quote_currency: str
    tick_size: Decimal
    contract_size: Decimal
    min_lot: Decimal
    lot_step: Decimal
    typical_spread_ticks: Decimal
    commission_per_lot: Decimal
    calendar_id: str
    rollover_time_local: time
    rollover_tz: str

    def __post_init__(self) -> None:
        """Reject an instrument whose contract arithmetic cannot work.

        Raises:
            ConfigurationError: If any size or increment is non-positive, or the lot step
                exceeds the minimum lot.
        """
        for name, value in (
            ("tick_size", self.tick_size),
            ("contract_size", self.contract_size),
            ("min_lot", self.min_lot),
            ("lot_step", self.lot_step),
        ):
            if value <= ZERO:
                raise ConfigurationError(
                    f"InstrumentSpec.{name} must be positive",
                    instrument_id=str(self.instrument_id),
                    value=str(value),
                )
        if self.commission_per_lot < ZERO or self.typical_spread_ticks < ZERO:
            raise ConfigurationError(
                "Commission and spread cannot be negative",
                instrument_id=str(self.instrument_id),
            )

    # -- contract arithmetic -----------------------------------------------------------

    def notional_pnl(
        self,
        entry_price: Decimal,
        exit_price: Decimal,
        quantity: Decimal,
        direction: Direction,
    ) -> Decimal:
        """Return the realised PnL of closing ``quantity`` lots.

        Args:
            entry_price: Average price at which the closed portion was opened.
            exit_price: Price at which it was closed.
            quantity: Lots closed. Always positive; side is carried by ``direction``.
            direction: Side of the position being closed.

        Returns:
            PnL in the quote currency. This is the single definition of profit in the
            system; no other module reimplements it.
        """
        move: Decimal = (exit_price - entry_price) * Decimal(direction.sign)
        return move * quantity * self.contract_size

    def round_price(self, price: Decimal, *, direction: Direction, adverse: bool) -> Decimal:
        """Round a price to the instrument's tick grid.

        Args:
            price: Unrounded price.
            direction: Side the price applies to.
            adverse: Round against the trader when ``True``. Sub-tick precision is not
                obtainable in a real market, and rounding in the trader's favour
                manufactures a fraction of a tick of edge on every single fill — which
                compounds into a meaningful phantom advantage over four hundred trades.

        Returns:
            The price snapped to the tick grid.
        """
        quantum: Decimal = self.tick_size
        steps: Decimal = price / quantum
        buying: bool = direction is Direction.LONG
        round_up: bool = buying if adverse else not buying
        rounded_steps: Decimal = steps.to_integral_value(
            rounding=ROUND_UP if round_up else ROUND_DOWN
        )
        return rounded_steps * quantum

    def round_quantity(self, quantity: Decimal) -> Decimal:
        """Snap a size down to the instrument's lot grid.

        Rounds down, never up: rounding up would take more risk than the sizing decision
        authorised, and the risk engine's output is not a suggestion.

        Args:
            quantity: Unrounded size in lots.

        Returns:
            The size snapped down to a valid lot increment. May be zero, which the caller
            must handle explicitly rather than filling.
        """
        if quantity <= ZERO:
            return ZERO
        steps: Decimal = (quantity / self.lot_step).to_integral_value(rounding=ROUND_FLOOR)
        return steps * self.lot_step

    def is_tradeable_quantity(self, quantity: Decimal) -> bool:
        """Return whether a size is at or above the minimum lot.

        Args:
            quantity: Size in lots.

        Returns:
            ``True`` if the size can actually be sent to a market.
        """
        return quantity >= self.min_lot

    def half_spread(self) -> Decimal:
        """Return half the typical spread, in price units.

        Returns:
            The distance from mid to either side of the book. Buys cross to the ask and
            sells to the bid, so each side pays this once.
        """
        return (self.typical_spread_ticks * self.tick_size) / Decimal("2")

    def commission_for(self, quantity: Decimal) -> Decimal:
        """Return the commission for one side of a trade.

        Args:
            quantity: Lots transacted.

        Returns:
            Half the round-turn rate, since entry and exit are charged separately and
            each pays its own side.
        """
        return (self.commission_per_lot * quantity) / Decimal("2")
