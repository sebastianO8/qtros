"""Single-bar market data.

Bar prices are :class:`decimal.Decimal` here even though the feature layer works in
floats. The conversion happens once, at the point a bar enters the execution path, so
that every price which touches cash is exact. Doing it later would mean the conversion
happens inside fill arithmetic, where a float would already have contaminated the result.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from qtros.domain.errors import SchemaContractError

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True, slots=True)
class BarData:
    """One completed bar.

    Attributes:
        ts_utc: Bar opening instant, timezone-aware UTC. The system stores bar-open
            timestamps throughout; close-labelled sources are shifted at ingestion.
        open: Opening price.
        high: Highest traded price.
        low: Lowest traded price.
        close: Closing price.
        volume: Traded volume, or ``None`` when the source supplies none. ``None`` is not
            zero: a bar with unknown volume and a bar with no trading are different facts.
        is_synthetic: Whether this bar was gap-filled rather than observed.
    """

    ts_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None
    is_synthetic: bool = False

    def __post_init__(self) -> None:
        """Verify the bar is internally possible and carries a UTC timestamp.

        Raises:
            SchemaContractError: If the timestamp is naive or the OHLC relationships are
                impossible. Layer 0 rejects such bars at ingestion; repeating the check
                here catches anything that reached the execution path by another route.
        """
        if self.ts_utc.tzinfo is None or self.ts_utc.tzinfo.utcoffset(self.ts_utc) is None:
            raise SchemaContractError(
                "BarData timestamp is timezone-naive",
                ts=self.ts_utc.isoformat(),
            )
        if (
            self.high < self.low
            or self.high < max(self.open, self.close)
            or self.low > min(self.open, self.close)
        ):
            raise SchemaContractError(
                "BarData violates OHLC relationships",
                ts=self.ts_utc.isoformat(),
                open=str(self.open),
                high=str(self.high),
                low=str(self.low),
                close=str(self.close),
            )

    @property
    def has_volume(self) -> bool:
        """Return whether the bar reports positive traded volume."""
        return self.volume is not None and self.volume > Decimal("0")

    @property
    def is_flat(self) -> bool:
        """Return whether the bar has no range at all."""
        return self.high == self.low

    def contains(self, price: Decimal) -> bool:
        """Return whether a price lies within the bar's traded range.

        Args:
            price: Price to test.

        Returns:
            ``True`` if the price is between the low and the high inclusive.
        """
        return self.low <= price <= self.high
