"""Instrument configuration (ARCH-001 C-7, ARCH-002 §4.2)."""

from __future__ import annotations

from datetime import time

from pydantic import Field, model_validator

from qtros.configuration.models.base import QtrosConfig, StrictDecimal, TimeZoneName
from qtros.domain.enums import AssetClass
from qtros.domain.identifiers import InstrumentId
from qtros.domain.instrument import InstrumentSpec


class InstrumentConfig(QtrosConfig):
    """Declarative form of :class:`~qtros.domain.instrument.InstrumentSpec`.

    Attributes:
        instrument_id: Canonical key; matches the file name under ``config/instruments/``.
        display_name: Human-readable name.
        asset_class: Broad category.
        base_currency: ISO code of the base.
        quote_currency: ISO code PnL is denominated in.
        tick_size: Smallest price increment. Quoted in YAML (D-2).
        contract_size: Units of base per lot.
        min_lot: Smallest tradeable size.
        lot_step: Size increment.
        typical_spread_ticks: Default spread input for the friction model.
        commission_per_lot: Round-turn commission per lot, halved per side.
        calendar_id: Session calendar key.
        rollover_time_local: Wall-clock time at which the trading day rolls.
        rollover_tz: IANA zone of ``rollover_time_local``. For FX this is New York time,
            not UTC, so the boundary moves with US DST.
    """

    instrument_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    asset_class: AssetClass
    base_currency: str = Field(min_length=3, max_length=3)
    quote_currency: str = Field(min_length=3, max_length=3)
    tick_size: StrictDecimal
    contract_size: StrictDecimal
    min_lot: StrictDecimal
    lot_step: StrictDecimal
    typical_spread_ticks: StrictDecimal
    commission_per_lot: StrictDecimal
    calendar_id: str = Field(min_length=1)
    rollover_time_local: time
    rollover_tz: TimeZoneName

    @model_validator(mode="after")
    def _positive_contract_terms(self) -> InstrumentConfig:
        """Reject non-positive sizes early, with a config-level message.

        Returns:
            The validated model.

        Raises:
            ValueError: If any size or increment is non-positive or a cost is negative.
        """
        for name in ("tick_size", "contract_size", "min_lot", "lot_step"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.typical_spread_ticks < 0 or self.commission_per_lot < 0:
            raise ValueError("typical_spread_ticks and commission_per_lot cannot be negative")
        return self

    def to_spec(self) -> InstrumentSpec:
        """Build the domain object the engine consumes.

        Returns:
            An :class:`InstrumentSpec` with identical values.
        """
        return InstrumentSpec(
            instrument_id=InstrumentId(self.instrument_id),
            display_name=self.display_name,
            asset_class=self.asset_class,
            base_currency=self.base_currency,
            quote_currency=self.quote_currency,
            tick_size=self.tick_size,
            contract_size=self.contract_size,
            min_lot=self.min_lot,
            lot_step=self.lot_step,
            typical_spread_ticks=self.typical_spread_ticks,
            commission_per_lot=self.commission_per_lot,
            calendar_id=self.calendar_id,
            rollover_time_local=self.rollover_time_local,
            rollover_tz=self.rollover_tz,
        )
