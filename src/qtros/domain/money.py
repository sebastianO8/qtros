"""Monetary and price precision.

Directive 6 requires reconciliation to hold *exactly*, with no tolerance. That is only
achievable if precision is bounded, and Decimal precision is not bounded by default:
division produces up to 28 significant digits, and every weighted-average entry price and
every apportioned cost involves a division.

The failure this prevents is subtle and was found by the property-based ledger test rather
than by any scenario test. Once a running balance carries more significant digits than the
Decimal context allows, ``cash += delta`` rounds — and summing the same deltas in a
different order rounds differently. The two totals then disagree by an amount too small to
notice and too real to ignore, and reconciliation fails for a reason that looks like a
logic bug but is not.

The rule: **every value that enters the ledger is quantized before it is stored.** After
that, all ledger arithmetic is addition of bounded-precision values, which is exact and
order-independent.

Rounding is half-even (banker's rounding). Half-up would bias every marginal cent in one
direction, and over four hundred trades a systematic bias is a phantom edge rather than a
rounding artefact.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

MONEY_EXPONENT: Final[Decimal] = Decimal("0.01")
"""Smallest representable cash amount: the quote currency's minor unit.

Two decimal places suits JPY-quoted and USD-quoted instruments alike. Instruments whose
quote currency has no minor unit still reconcile exactly; they simply never populate the
final two places.
"""

PRICE_EXPONENT: Final[Decimal] = Decimal("0.0000000001")
"""Precision retained for derived prices such as weighted-average entry.

Ten decimal places is far finer than any instrument's tick, so the quantization is
economically invisible, while still bounding the digit growth that division would otherwise
produce. Quantizing derived prices to the tick grid instead would be wrong: an average of
two on-grid prices legitimately falls between ticks, and snapping it would misstate the
cost basis.
"""

QUANTITY_EXPONENT: Final[Decimal] = Decimal("0.00000001")
"""Precision retained for lot quantities after apportionment."""


def quantize_money(value: Decimal) -> Decimal:
    """Round a cash amount to the ledger's storage precision.

    Args:
        value: Unbounded cash amount.

    Returns:
        The amount at :data:`MONEY_EXPONENT`, half-even.
    """
    return value.quantize(MONEY_EXPONENT, rounding=ROUND_HALF_EVEN)


def quantize_price(value: Decimal) -> Decimal:
    """Round a derived price to the system's price precision.

    Args:
        value: Unbounded price, typically the result of a weighted average.

    Returns:
        The price at :data:`PRICE_EXPONENT`, half-even.
    """
    return value.quantize(PRICE_EXPONENT, rounding=ROUND_HALF_EVEN)


def quantize_quantity(value: Decimal) -> Decimal:
    """Round a lot quantity to the system's quantity precision.

    Args:
        value: Unbounded quantity, typically the result of apportionment.

    Returns:
        The quantity at :data:`QUANTITY_EXPONENT`, half-even.
    """
    return value.quantize(QUANTITY_EXPONENT, rounding=ROUND_HALF_EVEN)


def apportion(total: Decimal, share: Decimal) -> tuple[Decimal, Decimal]:
    """Split a cash amount in two so the parts sum to the original exactly.

    The first part is computed from the share and quantized; the second is the arithmetic
    remainder rather than an independently quantized figure. Quantizing both halves
    separately would leave a sub-cent residual unaccounted for, and a reversal that loses a
    fraction of a cent of friction is a reconciliation failure waiting for a busy evening.

    Args:
        total: Amount to divide.
        share: Fraction assigned to the first part, between zero and one.

    Returns:
        A pair whose sum equals ``total`` exactly.
    """
    first: Decimal = quantize_money(total * share)
    return first, total - first
