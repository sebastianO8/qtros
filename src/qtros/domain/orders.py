"""Order lifecycle types and the risk-approval gate.

Three types in one direction, which is how ARCH-001 §1.5 makes the risk engine
non-bypassable rather than merely mandatory:

1. A strategy emits a :class:`TradeIntent`. It is inert — nothing accepts it for
   execution.
2. Only the risk engine can produce an :class:`ApprovedOrder`, because construction
   requires a sentinel that only ``qtros.risk`` imports.
3. The broker's ``submit`` accepts **only** an :class:`ApprovedOrder`.

A strategy therefore cannot construct something executable, and the broker cannot fill
something unapproved. Both halves hold at type-check time.

The honest limit: Python has no private constructors, so a determined caller could import
the sentinel. The guarantee is the *type signature* plus a test,
``tests/test_risk.py::TestApprovalTokenIsConfinedToRisk``, that walks the source tree and
fails if anything outside ``qtros.risk`` uses the approval symbol. Import-linter cannot do
this: it restricts whole modules, and every layer legitimately imports this module for
:class:`TradeIntent`. The test fails the build rather than failing silently, which is the
property that matters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from qtros.domain.enums import Direction, ExitReason, OrderRejectionCode, OrderType
from qtros.domain.errors import QtrosError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from qtros.domain.identifiers import InstrumentId, TradeId


class ApprovalError(QtrosError):
    """An order was constructed without a valid risk approval."""


class _ApprovalSentinel:
    """Opaque marker proving an order passed the risk engine."""

    __slots__ = ()

    def __repr__(self) -> str:
        """Return a representation that cannot be mistaken for a value."""
        return "<risk-approval>"


RISK_APPROVAL: Final[_ApprovalSentinel] = _ApprovalSentinel()
"""The single approval sentinel.

Only ``qtros.risk`` may import this. Enforced by
``tests/test_risk.py::TestApprovalTokenIsConfinedToRisk``, because a convention that anyone
can quietly ignore is not a control.
"""


@dataclass(frozen=True, slots=True)
class TradeIntent:
    """A strategy's request to trade. Inert until the risk engine approves it.

    Attributes:
        intent_id: Deterministic identifier, sequential within a run.
        ts_utc: Bar close at which the intent was formed.
        instrument_id: Instrument to trade.
        direction: Side requested.
        order_type: Order kind requested.
        is_exit: Whether this closes or reduces an existing position rather than opening
            one. Exits bypass sizing but never bypass the risk engine, since a risk halt
            must still be able to intervene.
        requested_quantity: Explicit size, when the strategy is closing a known position.
            ``None`` means the risk engine sizes it.
        limit_price: Limit price for non-market orders.
        stop_loss: Protective stop level.
        take_profit: Protective target level.
        exit_reason: Reason recorded on the resulting fill, for exits.
        mechanism_tag: Which repeatable market condition this trade claims to exploit.
            Required by Directive 5, and required to be non-empty: a trade whose author
            cannot name its mechanism is an unexplained result, not a validated one.
        rationale: Feature values that drove the decision, carried through to the trade
            log so a trade can be explained months later without re-running anything.
    """

    intent_id: str
    ts_utc: datetime
    instrument_id: InstrumentId
    direction: Direction
    order_type: OrderType = OrderType.MARKET
    is_exit: bool = False
    requested_quantity: Decimal | None = None
    limit_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    exit_reason: ExitReason | None = None
    mechanism_tag: str = ""
    rationale: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Reject an entry intent with no stated mechanism.

        Raises:
            ValueError: If an entry declares no mechanism tag. Exits are exempt: their
                mechanism is the entry's.
        """
        if not self.is_exit and not self.mechanism_tag.strip():
            raise ValueError(
                f"Entry intent {self.intent_id} declares no mechanism_tag; Directive 5 "
                "requires every trade to name the condition it claims to exploit"
            )


@dataclass(frozen=True, slots=True)
class ApprovedOrder:
    """An intent that has passed the risk engine and may be sent to the broker.

    Attributes:
        order_id: Deterministic identifier, sequential within a run.
        intent: The originating intent.
        quantity: Lot-rounded size. Always positive and at or above the minimum lot.
        approved_at_utc: When approval was granted.
        trade_id: Position lifecycle this order belongs to (ARCH-001 C-4).
        resize_reason: Why the risk engine changed the requested size, if it did.
        approval: The risk sentinel. Must be :data:`RISK_APPROVAL`.
    """

    order_id: str
    intent: TradeIntent
    quantity: Decimal
    approved_at_utc: datetime
    trade_id: TradeId
    resize_reason: str | None = None
    approval: _ApprovalSentinel | None = None

    def __post_init__(self) -> None:
        """Verify the approval sentinel and the size.

        Raises:
            ApprovalError: If the sentinel is absent or forged.
            ValueError: If the quantity is not positive.
        """
        if self.approval is not RISK_APPROVAL:
            raise ApprovalError(
                "ApprovedOrder constructed without a valid risk approval; only the risk "
                "engine may authorise an order",
                order_id=self.order_id,
            )
        if self.quantity <= Decimal("0"):
            raise ValueError(f"ApprovedOrder {self.order_id} has non-positive quantity")

    @property
    def direction(self) -> Direction:
        """Return the side of the underlying intent."""
        return self.intent.direction

    @property
    def instrument_id(self) -> InstrumentId:
        """Return the instrument of the underlying intent."""
        return self.intent.instrument_id

    @property
    def is_exit(self) -> bool:
        """Return whether this order reduces or closes a position."""
        return self.intent.is_exit


@dataclass(frozen=True, slots=True)
class RejectedIntent:
    """An intent the risk engine or broker refused, retained as data.

    Rejections are recorded rather than discarded. Section 5 of the constitution treats
    rejected variants as data, and the same reasoning applies within a run: a strategy
    whose orders are mostly rejected for BELOW_MINIMUM_LOT is mis-sized, and the only
    evidence of that is the rejection stream.

    Attributes:
        intent: The refused intent.
        code: Enumerated reason.
        detail: Human-readable context.
        rejected_at_utc: When the refusal occurred.
    """

    intent: TradeIntent
    code: OrderRejectionCode | str
    detail: str
    rejected_at_utc: datetime
