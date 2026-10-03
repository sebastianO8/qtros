"""Simulated matching engine.

Converts approved orders into fills under a declared intrabar path assumption. This is
the only component that constructs an :class:`ExecutionFill`, and it accepts only an
:class:`ApprovedOrder` — so an unapproved intent has no route to a fill.

Three simulation decisions that materially change results and are therefore explicit
rather than implicit:

**Next-bar-open execution.** A decision taken on bar T's close fills at bar T+1's open.
Filling at bar T's close would assume the close was simultaneously observable and
tradeable, which flatters every result by roughly one bar of favourable drift.

**Gaps fill at the open, not at the level.** If price gaps through a stop, the fill is at
the bar's open — worse than the stop price. Filling at the stop level assumes liquidity
that was not there, and it is precisely the assumption that makes a backtested maximum
drawdown smaller than a real one.

**Ambiguity resolves against the trader.** When a bar's range contains both the stop and
the target, OHLC data cannot say which came first. Under ``ADVERSE_FIRST`` the stop wins.
The alternative, resolving in the trader's favour, turns every ambiguous bar into a
winner and is how a strategy that loses money backtests profitably.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import structlog

from qtros.domain.enums import (
    Direction,
    ExecutionTiming,
    ExitReason,
    OrderRejectionCode,
    PathAssumption,
    ZeroVolumePolicy,
)
from qtros.domain.execution import ExecutionFill, PositionState
from qtros.domain.money import quantize_money

if TYPE_CHECKING:
    from qtros.domain.identifiers import SequentialIdFactory
    from qtros.domain.instrument import InstrumentSpec
    from qtros.domain.market import BarData
    from qtros.domain.orders import ApprovedOrder
    from qtros.execution.friction import FrictionCharges, IFrictionModel

_LOGGER: Final = structlog.get_logger(__name__)

ZERO: Final[Decimal] = Decimal("0")


@dataclass(frozen=True, slots=True)
class ProtectiveTrigger:
    """A resting stop or target that a bar would have hit.

    Attributes:
        reason: Which protective level fired.
        trigger_price: The level itself.
        fill_price: Price the fill would actually occur at, before friction. Equals the
            trigger unless price gapped through it, in which case it is the bar's open.
        gapped: Whether price gapped through the level.
    """

    reason: ExitReason
    trigger_price: Decimal
    fill_price: Decimal
    gapped: bool


@dataclass(frozen=True, slots=True)
class BrokerResult:
    """Outcome of submitting one order.

    Attributes:
        fill: The resulting unbooked fill, or ``None`` if the order was rejected.
        rejection_code: Why the order was rejected, if it was.
        detail: Human-readable context for a rejection.
    """

    fill: ExecutionFill | None
    rejection_code: OrderRejectionCode | None = None
    detail: str = ""

    @property
    def filled(self) -> bool:
        """Return whether the order produced a fill."""
        return self.fill is not None


class SimulatedBroker:
    """Matches approved orders against bars.

    Args:
        friction: Cost model.
        ids: Deterministic identifier factory.
        timing: When a decision is filled.
        path_assumption: Assumed intrabar event order.
        zero_volume_policy: Whether a bar reporting zero volume may fill an order.

    Raises:
        ValueError: If same-bar-close timing is combined with a favourable path
            assumption. That pair compounds two optimistic assumptions and produces
            results that cannot be defended to a prop firm or to a future reader.
    """

    __slots__ = ("_friction", "_ids", "_path", "_timing", "_zero_volume")

    def __init__(
        self,
        friction: IFrictionModel,
        ids: SequentialIdFactory,
        *,
        timing: ExecutionTiming = ExecutionTiming.NEXT_BAR_OPEN,
        path_assumption: PathAssumption = PathAssumption.ADVERSE_FIRST,
        zero_volume_policy: ZeroVolumePolicy = ZeroVolumePolicy.REJECT_FILL,
    ) -> None:
        if (
            timing is ExecutionTiming.SAME_BAR_CLOSE
            and path_assumption is PathAssumption.FAVOURABLE_FIRST
        ):
            raise ValueError(
                "SAME_BAR_CLOSE with FAVOURABLE_FIRST stacks two optimistic assumptions; "
                "results under that pair are not defensible"
            )
        self._friction: IFrictionModel = friction
        self._ids: SequentialIdFactory = ids
        self._timing: ExecutionTiming = timing
        self._path: PathAssumption = path_assumption
        self._zero_volume: ZeroVolumePolicy = zero_volume_policy

    @property
    def timing(self) -> ExecutionTiming:
        """Return the configured execution timing."""
        return self._timing

    @property
    def path_assumption(self) -> PathAssumption:
        """Return the configured intrabar path assumption."""
        return self._path

    # -- order submission ---------------------------------------------------------------

    def submit(
        self,
        order: ApprovedOrder,
        execution_bar: BarData | None,
        spec: InstrumentSpec,
        *,
        forced_price: Decimal | None = None,
        exit_reason: ExitReason | None = None,
    ) -> BrokerResult:
        """Attempt to fill an approved order against a bar.

        Args:
            order: Risk-approved order. The type is the gate: an unapproved intent cannot
                be passed here.
            execution_bar: Bar to fill against — normally bar T+1 under next-bar-open
                timing. ``None`` when no bar follows, which happens at the end of the run.
            spec: Contract specification.
            forced_price: Reference price overriding the bar's open, used when a
                protective level determines the fill price.
            exit_reason: Reason recorded on the fill, for exits.

        Returns:
            A fill, or an enumerated rejection.
        """
        if execution_bar is None:
            return self._reject(
                OrderRejectionCode.NO_EXECUTION_BAR,
                "No bar follows the decision bar; the order cannot be filled",
                order,
            )
        if order.instrument_id != spec.instrument_id:
            return self._reject(
                OrderRejectionCode.INSTRUMENT_MISMATCH,
                "Order instrument does not match the supplied contract specification",
                order,
            )
        if order.quantity <= ZERO:
            return self._reject(
                OrderRejectionCode.NON_POSITIVE_QUANTITY,
                "Order quantity is not positive",
                order,
            )
        if not spec.is_tradeable_quantity(order.quantity):
            return self._reject(
                OrderRejectionCode.BELOW_MINIMUM_LOT,
                f"Quantity {order.quantity} is below the minimum lot {spec.min_lot}",
                order,
            )
        if (
            self._zero_volume is ZeroVolumePolicy.REJECT_FILL
            and execution_bar.volume is not None
            and execution_bar.volume <= ZERO
        ):
            return self._reject(
                OrderRejectionCode.ZERO_VOLUME_BAR,
                "Execution bar reports zero volume; nothing traded there to fill against",
                order,
            )

        reference: Decimal = (
            forced_price if forced_price is not None else self._reference_price(execution_bar)
        )
        charges: FrictionCharges = self._friction.charges(
            direction=order.direction,
            quantity=order.quantity,
            reference_price=reference,
            bar=execution_bar,
            spec=spec,
        )
        fill_price: Decimal = self._apply_adverse_adjustment(
            reference, order.direction, charges.price_adjustment, spec
        )
        spread_cost, slippage_cost = self._derive_implicit_costs(
            reference=reference,
            fill_price=fill_price,
            direction=order.direction,
            quantity=order.quantity,
            charges=charges,
            spec=spec,
        )

        fill = ExecutionFill(
            fill_id=self._ids.next_fill_id(),
            order_id=order.order_id,
            trade_id=order.trade_id,
            instrument_id=spec.instrument_id,
            ts_utc=execution_bar.ts_utc,
            direction=order.direction,
            quantity=order.quantity,
            reference_price=reference,
            fill_price=fill_price,
            commission=quantize_money(charges.commission),
            spread_cost=spread_cost,
            slippage_cost=slippage_cost,
            is_entry=not order.is_exit,
            exit_reason=exit_reason or order.intent.exit_reason,
        )

        _LOGGER.debug(
            "broker.filled",
            order_id=order.order_id,
            fill_id=str(fill.fill_id),
            direction=str(order.direction),
            quantity=str(order.quantity),
            reference_price=str(reference),
            fill_price=str(fill_price),
            implicit_costs=str(fill.implicit_costs),
            commission=str(fill.commission),
        )
        return BrokerResult(fill=fill)

    # -- protective orders ---------------------------------------------------------------

    def resolve_protective_exit(
        self,
        position: PositionState,
        bar: BarData,
    ) -> ProtectiveTrigger | None:
        """Determine whether a bar would have triggered a resting stop or target.

        Called before the strategy is consulted on each bar, so protective levels take
        precedence over new signals — which matches reality, where a resting stop does not
        wait for a strategy to have an opinion.

        Args:
            position: The open position and its protective levels.
            bar: Bar to evaluate.

        Returns:
            The trigger that fired, or ``None`` if neither level was reached. When both
            were reached within the same bar, the configured path assumption decides;
            under ``ADVERSE_FIRST`` that is the stop.
        """
        stop: ProtectiveTrigger | None = self._stop_trigger(position, bar)
        target: ProtectiveTrigger | None = self._target_trigger(position, bar)

        if stop is not None and target is not None:
            chosen: ProtectiveTrigger = (
                stop if self._path is PathAssumption.ADVERSE_FIRST else target
            )
            _LOGGER.debug(
                "broker.ambiguous_bar",
                trade_id=str(position.trade_id),
                ts=bar.ts_utc.isoformat(),
                resolved_to=str(chosen.reason),
                path_assumption=str(self._path),
            )
            return chosen
        return stop or target

    def _stop_trigger(
        self,
        position: PositionState,
        bar: BarData,
    ) -> ProtectiveTrigger | None:
        """Return the stop trigger for a bar, if the stop was reached.

        Args:
            position: Open position carrying the stop level.
            bar: Bar to evaluate.

        Returns:
            The trigger, with ``gapped`` set when the open was already through the level.
        """
        stop: Decimal | None = position.current_stop
        if stop is None:
            return None

        long_position: bool = position.direction is Direction.LONG
        gapped: bool = bar.open <= stop if long_position else bar.open >= stop
        reached: bool = bar.low <= stop if long_position else bar.high >= stop

        if gapped:
            return ProtectiveTrigger(ExitReason.STOP_LOSS, stop, bar.open, gapped=True)
        if reached:
            return ProtectiveTrigger(ExitReason.STOP_LOSS, stop, stop, gapped=False)
        return None

    def _target_trigger(
        self,
        position: PositionState,
        bar: BarData,
    ) -> ProtectiveTrigger | None:
        """Return the target trigger for a bar, if the target was reached.

        Args:
            position: Open position carrying the target level.
            bar: Bar to evaluate.

        Returns:
            The trigger. A favourable gap fills at the open, which is *better* than the
            target — the same rule as for stops, applied without exception so the model
            stays symmetric.
        """
        target: Decimal | None = position.current_target
        if target is None:
            return None

        long_position: bool = position.direction is Direction.LONG
        gapped: bool = bar.open >= target if long_position else bar.open <= target
        reached: bool = bar.high >= target if long_position else bar.low <= target

        if gapped:
            return ProtectiveTrigger(ExitReason.TAKE_PROFIT, target, bar.open, gapped=True)
        if reached:
            return ProtectiveTrigger(ExitReason.TAKE_PROFIT, target, target, gapped=False)
        return None

    # -- helpers -------------------------------------------------------------------------

    def _reference_price(self, bar: BarData) -> Decimal:
        """Return the pre-friction reference price for a fill.

        Args:
            bar: Execution bar.

        Returns:
            The bar's open under next-bar-open timing, or its close under same-bar-close.
        """
        return bar.close if self._timing is ExecutionTiming.SAME_BAR_CLOSE else bar.open

    @staticmethod
    def _derive_implicit_costs(
        *,
        reference: Decimal,
        fill_price: Decimal,
        direction: Direction,
        quantity: Decimal,
        charges: FrictionCharges,
        spec: InstrumentSpec,
    ) -> tuple[Decimal, Decimal]:
        """Convert the price degradation that actually occurred into cash costs.

        Costs are derived from ``fill_price - reference`` rather than from the friction
        model's intended adjustment. The two differ because the fill price is snapped
        adversely to the tick grid, and that rounding is a real cost paid on every single
        fill. Reporting the intended figure would leave it unattributed, and over four
        hundred trades an unattributed fraction of a tick per fill is not a rounding
        footnote.

        The degradation is split spread-first: the spread is a floor the market charges
        before anything else happens, so any excess — intended slippage plus tick
        rounding — is attributed to slippage.

        Args:
            reference: Pre-friction price.
            fill_price: Executable price after adjustment and rounding.
            direction: Side being filled.
            quantity: Lots transacted.
            charges: The friction model's price-unit components.
            spec: Contract specification.

        Returns:
            A pair of (spread cost, slippage cost) in cash. Both are non-negative, and
            their sum equals the cash value of the actual price degradation exactly —
            which is what lets a trade's gross PnL reconstruct the reference-price PnL.
        """
        degradation: Decimal = (fill_price - reference) * Decimal(direction.sign)
        if degradation < ZERO:
            # Rounding can only move a price adversely or leave it unchanged; a negative
            # degradation would mean the grid handed the trader free edge.
            degradation = ZERO

        contract_units: Decimal = quantity * spec.contract_size
        # Quantize the total first, then derive the spread half and take slippage as the
        # remainder. Quantizing the two halves independently would leave a sub-cent
        # residual that belongs to neither, and the implicit cost would no longer equal
        # the cash value of the price degradation.
        total_implicit: Decimal = quantize_money(degradation * contract_units)
        spread_price: Decimal = min(charges.spread_price, degradation)
        spread_cost: Decimal = quantize_money(spread_price * contract_units)
        return spread_cost, total_implicit - spread_cost

    @staticmethod
    def _apply_adverse_adjustment(
        reference: Decimal,
        direction: Direction,
        adjustment: Decimal,
        spec: InstrumentSpec,
    ) -> Decimal:
        """Move a reference price against the trader and snap it to the tick grid.

        Args:
            reference: Pre-friction price.
            direction: Side being filled.
            adjustment: Non-negative adverse move in price units.
            spec: Contract specification.

        Returns:
            The executable price: higher for buys, lower for sells, rounded adversely so
            that sub-tick precision cannot manufacture a fraction of a tick of edge on
            every fill.
        """
        signed: Decimal = reference + adjustment * Decimal(direction.sign)
        return spec.round_price(signed, direction=direction, adverse=True)

    @staticmethod
    def _reject(
        code: OrderRejectionCode,
        detail: str,
        order: ApprovedOrder,
    ) -> BrokerResult:
        """Record and return an enumerated rejection.

        Args:
            code: Enumerated reason.
            detail: Human-readable context.
            order: The rejected order.

        Returns:
            A rejection result carrying no fill.
        """
        _LOGGER.warning(
            "broker.order_rejected",
            order_id=order.order_id,
            code=str(code),
            detail=detail,
            quantity=str(order.quantity),
        )
        return BrokerResult(fill=None, rejection_code=code, detail=detail)
