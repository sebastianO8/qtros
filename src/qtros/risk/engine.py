"""The risk gate (ARCH-001 §1.5, C-1; PI rulings G-1, G-2; ASS-008, ASS-009).

Two responsibilities, kept separate on purpose:

**Approval.** Every :class:`~qtros.domain.orders.TradeIntent` passes through
:meth:`RiskEngine.approve`. Exits are approved without sizing or caps — a risk engine that
can refuse an exit is a risk engine that can trap a losing position — but they still pass
through here so a halt can be observed and logged. Entries are sized, snapped to the lot
grid, checked against every rule, and only then minted into an
:class:`~qtros.domain.orders.ApprovedOrder`. This module is the only importer of the
approval sentinel.

**The drawdown halt (G-2).** At each bar close the engine computes the *worst*
mark-to-market equity the open position reached within the bar under the ADVERSE_FIRST
path assumption and measures drawdown from the running peak to that extreme. Prop firms
measure intraday; a close-only check would pass a bar whose low ended a funded account.
A halt blocks **new entries only**. Exits, stops, and targets continue, so the open
position is managed to its conclusion rather than force-flattened at an invented price.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import structlog

from qtros.domain.enums import (
    BreachAction,
    Direction,
    DrawdownBasis,
    OrderRejectionCode,
    RiskEventKind,
    SubMinLotPolicy,
)
from qtros.domain.errors import ConfigurationError
from qtros.domain.money import quantize_money
from qtros.domain.orders import RISK_APPROVAL, ApprovedOrder, RejectedIntent, TradeIntent
from qtros.risk.limits import IRiskRule, MaxLotRule, MaxNotionalExposureRule
from qtros.risk.sizing import FixedFractionalSizer, ISizer

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from qtros.configuration.models.risk import RiskLimitsConfig
    from qtros.domain.execution import EquitySnapshot, PositionState
    from qtros.domain.identifiers import SequentialIdFactory, TradeId
    from qtros.domain.instrument import InstrumentSpec
    from qtros.domain.market import BarData
    from qtros.domain.signals import PortfolioView

_LOGGER: Final = structlog.get_logger(__name__)

ZERO: Final[Decimal] = Decimal("0")


@dataclass(frozen=True, slots=True)
class RiskEvent:
    """A state change in the risk engine, recorded to ``risk_events``.

    Attributes:
        kind: What happened.
        ts_utc: Bar timestamp at which it happened.
        bar_index: Bar index.
        peak_equity: Running peak at evaluation time.
        extreme_equity: Adverse-first intrabar MTM extreme evaluated.
        drawdown: ``peak_equity - extreme_equity``.
        drawdown_fraction: Drawdown as a fraction of the peak.
        detail: Human-readable context.
    """

    kind: RiskEventKind
    ts_utc: datetime
    bar_index: int
    peak_equity: Decimal
    extreme_equity: Decimal
    drawdown: Decimal
    drawdown_fraction: Decimal
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    """Outcome of :meth:`RiskEngine.approve`. Exactly one side is populated.

    Attributes:
        order: The approved order, if approved.
        rejection: The retained rejection, if refused.
    """

    order: ApprovedOrder | None
    rejection: RejectedIntent | None

    @property
    def approved(self) -> bool:
        """Return whether an order was minted."""
        return self.order is not None


class RiskEngine:
    """Sizes, caps, and halts. The sole minter of approved orders.

    Args:
        limits: Risk configuration.
        spec: Contract specification.
        ids: Deterministic identifier factory.
        initial_equity: Starting balance; seeds the drawdown peak so a loss on the very
            first trade is measured from the funded amount, not from zero.
        sizer: Sizing model. Defaults to fixed-fractional from the configured risk
            fraction.
        rules: Entry rules. Defaults to the G-1 pair built from ``limits``.
        sub_min_lot_policy: Whether a sized quantity below the minimum lot is rejected
            or raised.

    Raises:
        ConfigurationError: If the drawdown basis is not mark-to-market (G-2 permits no
            other gate basis) or the initial equity is not positive.
    """

    __slots__ = (
        "_events",
        "_halt_session",
        "_halted",
        "_ids",
        "_limits",
        "_peak",
        "_rules",
        "_sizer",
        "_spec",
        "_sub_min_lot",
    )

    def __init__(
        self,
        limits: RiskLimitsConfig,
        spec: InstrumentSpec,
        ids: SequentialIdFactory,
        *,
        initial_equity: Decimal,
        sizer: ISizer | None = None,
        rules: Sequence[IRiskRule] | None = None,
        sub_min_lot_policy: SubMinLotPolicy = SubMinLotPolicy.REJECT_ORDER,
    ) -> None:
        if limits.max_drawdown_basis is not DrawdownBasis.MARK_TO_MARKET:
            raise ConfigurationError(
                "G-2: the drawdown halt evaluates mark-to-market equity only; a "
                "closed-equity basis would pass a bar whose low ended a funded account",
                basis=str(limits.max_drawdown_basis),
            )
        if initial_equity <= ZERO:
            raise ConfigurationError("initial_equity must be positive")

        self._limits: RiskLimitsConfig = limits
        self._spec: InstrumentSpec = spec
        self._ids: SequentialIdFactory = ids
        self._sizer: ISizer = sizer or FixedFractionalSizer(limits.risk_per_trade_fraction)
        self._rules: tuple[IRiskRule, ...] = (
            tuple(rules)
            if rules is not None
            else (
                MaxLotRule(limits.max_lot),
                MaxNotionalExposureRule(limits.max_notional_fraction),
            )
        )
        self._sub_min_lot: SubMinLotPolicy = sub_min_lot_policy
        self._peak: Decimal = initial_equity
        self._halted: bool = False
        self._halt_session: int | None = None
        self._events: list[RiskEvent] = []

    # -- state -------------------------------------------------------------------------

    @property
    def is_halted(self) -> bool:
        """Return whether new entries are currently blocked."""
        return self._halted

    @property
    def peak_equity(self) -> Decimal:
        """Return the running peak of close-of-bar mark-to-market equity."""
        return self._peak

    @property
    def events(self) -> tuple[RiskEvent, ...]:
        """Return every recorded risk event, in order."""
        return tuple(self._events)

    @property
    def rules(self) -> tuple[IRiskRule, ...]:
        """Return the active entry rules."""
        return self._rules

    # -- G-2: drawdown halt ------------------------------------------------------------

    @staticmethod
    def adverse_extreme(
        snapshot: EquitySnapshot,
        bar: BarData,
        position: PositionState | None,
        spec: InstrumentSpec,
    ) -> Decimal:
        """Return the worst MTM equity reached within a bar under ADVERSE_FIRST.

        Args:
            snapshot: Close-of-bar equity.
            bar: The bar just closed.
            position: Open position, if any.
            spec: Contract specification.

        Returns:
            Cash plus unrealised PnL marked at the bar's low for a long or high for a
            short. Equal to cash when flat.
        """
        if position is None:
            return snapshot.cash
        adverse_price: Decimal = bar.low if position.direction is Direction.LONG else bar.high
        return snapshot.cash + position.unrealized_pnl(adverse_price, spec.contract_size)

    def on_bar_close(
        self,
        *,
        snapshot: EquitySnapshot,
        bar: BarData,
        position: PositionState | None,
        bar_index: int,
        session_index: int = 0,
    ) -> RiskEvent | None:
        """Evaluate the drawdown limit at a bar close (G-3 step 4).

        Drawdown is measured from the peak **before** this bar against this bar's adverse
        extreme, and only then is the peak advanced by this bar's close. Advancing first
        would let a bar that both breached the limit and closed at a new high escape.

        Args:
            snapshot: Close-of-bar equity from the portfolio.
            bar: The bar just closed.
            position: Open position, if any.
            bar_index: Bar index.
            session_index: Current session, for session-scoped halts.

        Returns:
            The event raised at this bar, or ``None``.
        """
        event: RiskEvent | None = None
        extreme: Decimal = self.adverse_extreme(snapshot, bar, position, self._spec)
        drawdown: Decimal = quantize_money(self._peak - extreme)
        fraction: Decimal = drawdown / self._peak if self._peak > ZERO else ZERO

        if (
            self._halted
            and self._limits.on_limit_breach is BreachAction.HALT_SESSION
            and self._halt_session is not None
            and session_index != self._halt_session
        ):
            self._halted = False
            self._halt_session = None
            event = RiskEvent(
                RiskEventKind.HALT_RELEASED,
                bar.ts_utc,
                bar_index,
                self._peak,
                extreme,
                drawdown,
                fraction,
                "session-scoped halt lapsed at session boundary",
            )
            self._events.append(event)
            _LOGGER.info("risk.halt_released", bar_index=bar_index, session=session_index)

        if not self._halted and fraction >= self._limits.max_drawdown_fraction:
            self._halted = True
            self._halt_session = session_index
            event = RiskEvent(
                RiskEventKind.DRAWDOWN_HALT,
                bar.ts_utc,
                bar_index,
                self._peak,
                extreme,
                drawdown,
                fraction,
                f"adverse-first intrabar drawdown {drawdown} ({fraction:.4%}) reached the "
                f"{self._limits.max_drawdown_pct}% limit; new entries blocked "
                f"({self._limits.on_limit_breach})",
            )
            self._events.append(event)
            _LOGGER.warning(
                "risk.drawdown_halt",
                bar_index=bar_index,
                ts=bar.ts_utc.isoformat(),
                peak=str(self._peak),
                extreme=str(extreme),
                drawdown=str(drawdown),
                fraction=str(fraction),
            )

        self._peak = max(self._peak, snapshot.mtm_equity)
        return event

    # -- approval ----------------------------------------------------------------------

    def approve(
        self,
        intent: TradeIntent,
        *,
        view: PortfolioView,
        bar: BarData,
        trade_id: TradeId,
    ) -> ApprovalResult:
        """Approve or reject one intent (G-3 step 6).

        Args:
            intent: The strategy's request.
            view: Account state at the decision bar.
            bar: The decision bar; its close is the sizing reference.
            trade_id: Lifecycle the resulting order belongs to, allocated by the
                portfolio so adds and exits join the open lifecycle.

        Returns:
            An approved order or a retained rejection.
        """
        if intent.is_exit:
            return self._approve_exit(intent, view=view, bar=bar, trade_id=trade_id)
        return self._approve_entry(intent, view=view, bar=bar, trade_id=trade_id)

    def _approve_exit(
        self,
        intent: TradeIntent,
        *,
        view: PortfolioView,
        bar: BarData,
        trade_id: TradeId,
    ) -> ApprovalResult:
        """Approve an exit. Never blocked by a halt; never sized or capped.

        An exit larger than the open position is clipped to it, with the resize recorded,
        because an exit that reverses is an entry wearing an exit's exemption.
        """
        position: PositionState | None = view.open_position
        if position is None:
            return self._reject(intent, OrderRejectionCode.NO_OPEN_POSITION, "exit while flat", bar)

        requested: Decimal = (
            intent.requested_quantity
            if intent.requested_quantity is not None
            else position.quantity
        )
        quantity: Decimal = min(self._spec.round_quantity(requested), position.quantity)
        if quantity <= ZERO:
            return self._reject(
                intent,
                OrderRejectionCode.NON_POSITIVE_QUANTITY,
                "exit quantity rounds to zero",
                bar,
            )
        resize: str | None = (
            f"exit clipped from {requested} to open quantity {quantity}"
            if quantity != requested
            else None
        )
        return self._mint(intent, quantity, bar, trade_id, resize)

    def _approve_entry(
        self,
        intent: TradeIntent,
        *,
        view: PortfolioView,
        bar: BarData,
        trade_id: TradeId,
    ) -> ApprovalResult:
        """Size, snap, rule-check, and mint an entry."""
        if self._halted:
            return self._reject(
                intent,
                OrderRejectionCode.RISK_HALTED,
                "drawdown halt is active; new entries are blocked (G-2)",
                bar,
            )

        sizing = self._sizer.size(
            intent=intent, reference_price=bar.close, view=view, spec=self._spec
        )
        if not sizing.sized:
            assert sizing.code is not None
            return self._reject(intent, sizing.code, sizing.detail, bar)

        quantity: Decimal = sizing.quantity
        resize: str | None = None
        if not self._spec.is_tradeable_quantity(quantity):
            if self._sub_min_lot is SubMinLotPolicy.ROUND_UP:
                resize = f"raised from {quantity} to minimum lot {self._spec.min_lot}"
                quantity = self._spec.min_lot
            else:
                return self._reject(
                    intent,
                    OrderRejectionCode.SIZED_TO_ZERO,
                    f"sized quantity {quantity} is below minimum lot {self._spec.min_lot} "
                    f"(risk {sizing.risk_amount} over stop distance {sizing.stop_distance})",
                    bar,
                )

        for rule in self._rules:
            verdict = rule.evaluate(
                intent=intent,
                quantity=quantity,
                reference_price=bar.close,
                view=view,
                spec=self._spec,
            )
            if not verdict.allowed:
                assert verdict.code is not None
                return self._reject(intent, verdict.code, f"{rule.name}: {verdict.detail}", bar)

        return self._mint(intent, quantity, bar, trade_id, resize)

    def _mint(
        self,
        intent: TradeIntent,
        quantity: Decimal,
        bar: BarData,
        trade_id: TradeId,
        resize_reason: str | None,
    ) -> ApprovalResult:
        """Construct the approved order. The only call site of the approval sentinel."""
        order = ApprovedOrder(
            order_id=self._ids.next_order_id(),
            intent=intent,
            quantity=quantity,
            approved_at_utc=bar.ts_utc,
            trade_id=trade_id,
            resize_reason=resize_reason,
            approval=RISK_APPROVAL,
        )
        _LOGGER.debug(
            "risk.approved",
            order_id=order.order_id,
            intent_id=intent.intent_id,
            direction=str(intent.direction),
            quantity=str(quantity),
            is_exit=intent.is_exit,
            resize=resize_reason,
        )
        return ApprovalResult(order=order, rejection=None)

    @staticmethod
    def _reject(
        intent: TradeIntent,
        code: OrderRejectionCode,
        detail: str,
        bar: BarData,
    ) -> ApprovalResult:
        """Record and return a rejection."""
        _LOGGER.info(
            "risk.rejected",
            intent_id=intent.intent_id,
            code=str(code),
            detail=detail,
            is_exit=intent.is_exit,
        )
        return ApprovalResult(
            order=None,
            rejection=RejectedIntent(
                intent=intent, code=code, detail=detail, rejected_at_utc=bar.ts_utc
            ),
        )
