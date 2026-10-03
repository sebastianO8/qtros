"""The deterministic run loop (ARCH-001 C-10; ASS-001; PI rulings G-2, G-3, G-4).

Per bar *t*, in this order and no other (G-3):

1. **Execute** orders queued at *t-1* against ``open(t)``.
2. **Resolve** resting protective exits against bar *t*'s range.
3. **Mark** the book to bar *t*'s close.
4. **Risk** evaluates the drawdown limit on the adverse-first intrabar extreme.
5. **Strategy** evaluates bar *t* through a :class:`BarContext` (features as of *t-1*).
6. **Risk** approves or rejects each intent.
7. **Queue** approved orders for *t+1*.

Step order is the correctness of the backtest. Marking before executing would price the
book on a position it does not yet hold; evaluating the strategy before resolving stops
would let a strategy act on a position the market had already closed; approving before
marking would size from stale equity. None of the seven steps is optional.

Reconciliation runs inside :meth:`Backtester.run` (C-10). A run that does not reconcile
raises unless ``allow_unreconciled`` was set, in which case the result is stamped
untrusted. There is no path by which an unreconciled number reaches a report unmarked.

Protective levels are tracked here, not in the portfolio. The portfolio books fills and
knows nothing about intents; the stop and target travel on the intent, so the engine
remembers them per lifecycle and arms them on the position copy it hands to the broker.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import structlog

from qtros.alpha.base import IStrategy, verify_feature_provenance
from qtros.domain.enums import OrderRejectionCode
from qtros.domain.errors import QtrosError
from qtros.domain.orders import ApprovedOrder, RejectedIntent, TradeIntent
from qtros.engine.context import BarContextFactory
from qtros.engine.portfolio import Portfolio, equity_curve_bounds

if TYPE_CHECKING:
    from collections.abc import Sequence

    from qtros.domain.execution import EquitySnapshot, ExecutionFill, ReconciliationReport, Trade
    from qtros.domain.feature_contracts import FeatureFrame, SealedFeatureSet
    from qtros.domain.identifiers import SequentialIdFactory, TradeId
    from qtros.domain.instrument import InstrumentSpec
    from qtros.domain.market import BarData
    from qtros.domain.signals import PortfolioView, StrategyState
    from qtros.execution.broker import BrokerResult, SimulatedBroker
    from qtros.risk.engine import RiskEngine, RiskEvent

_LOGGER: Final = structlog.get_logger(__name__)


class BacktestError(QtrosError):
    """The run could not be completed."""


class ReconciliationFailedError(BacktestError):
    """Directive 6 did not hold at the end of the run."""


@dataclass(frozen=True, slots=True)
class ProtectiveLevels:
    """Resting stop and target for one position lifecycle.

    Attributes:
        stop: Protective stop price, if any.
        target: Protective target price, if any.
    """

    stop: Decimal | None
    target: Decimal | None

    @property
    def armed(self) -> bool:
        """Return whether either level is set."""
        return self.stop is not None or self.target is not None


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Everything a run produced. Immutable; the persistence layer writes it verbatim.

    Attributes:
        strategy_id: Registry key of the strategy that ran.
        strategy_version: Its version.
        trades: Every assembled trade, including one flagged open at end if any.
        fills: The complete fill ledger.
        equity_curve: One snapshot per bar.
        rejected_intents: Every rejection, from risk or broker.
        risk_events: Halts and releases.
        reconciliation: The Directive 6 report.
        trusted: ``False`` if the run was allowed to finish unreconciled.
        halted_at_end: Whether the risk halt was active when the run ended.
        bars_processed: Bars stepped.
        decisions_taken: Bars on which the strategy was consulted.
        sealed_columns: Feature columns whose provenance was verified (G-4).
        feature_set_hash: Hash of the feature set that ran.
        peak_mtm_equity: Highest mark-to-market equity on the curve.
        max_drawdown: Largest peak-to-trough on the mark-to-market curve.
    """

    strategy_id: str
    strategy_version: str
    trades: tuple[Trade, ...]
    fills: tuple[ExecutionFill, ...]
    equity_curve: tuple[EquitySnapshot, ...]
    rejected_intents: tuple[RejectedIntent, ...]
    risk_events: tuple[RiskEvent, ...]
    reconciliation: ReconciliationReport
    trusted: bool
    halted_at_end: bool
    bars_processed: int
    decisions_taken: int
    sealed_columns: tuple[str, ...]
    feature_set_hash: str
    peak_mtm_equity: Decimal
    max_drawdown: Decimal

    @property
    def closed_trades(self) -> tuple[Trade, ...]:
        """Return trades that completed within the run."""
        return tuple(t for t in self.trades if not t.is_open_at_backtest_end)

    @property
    def open_trade_at_end(self) -> Trade | None:
        """Return the position still open when the run ended, if any."""
        return next((t for t in self.trades if t.is_open_at_backtest_end), None)

    @property
    def final_cash(self) -> Decimal:
        """Return cash at the end of the run."""
        return self.equity_curve[-1].cash if self.equity_curve else Decimal("0")


class Backtester:
    """Runs one strategy over one feature frame, deterministically.

    Args:
        feature_frame: Audited, warmup-trimmed, sealed output of Layer 0.5.
        strategy: The strategy to run.
        instrument: Contract specification.
        portfolio: A fresh, funded portfolio.
        broker: The simulated matching engine.
        risk: The risk gate.
        ids: Identifier factory shared with the broker, portfolio, and risk engine.
        session_column: Session index column, if the frame has one.
        allow_unreconciled: Finish and stamp untrusted rather than raise if Directive 6
            fails. For diagnosis only.

    Raises:
        StrategyProvenanceError: If the strategy's features are not sealed (G-4).
        ConfigurationError: From the context factory, if the frame is shifted or
            unaudited.
    """

    __slots__ = (
        "_allow_unreconciled",
        "_broker",
        "_decisions",
        "_equity",
        "_factory",
        "_feature_frame",
        "_ids",
        "_instrument",
        "_levels",
        "_pending",
        "_pending_levels",
        "_portfolio",
        "_rejected",
        "_risk",
        "_sealed",
        "_strategy",
    )

    def __init__(
        self,
        *,
        feature_frame: FeatureFrame,
        strategy: IStrategy,
        instrument: InstrumentSpec,
        portfolio: Portfolio,
        broker: SimulatedBroker,
        risk: RiskEngine,
        ids: SequentialIdFactory,
        session_column: str | None = None,
        allow_unreconciled: bool = False,
    ) -> None:
        self._sealed: SealedFeatureSet = verify_feature_provenance(strategy, feature_frame)  # G-4
        self._factory: BarContextFactory = BarContextFactory(
            feature_frame, instrument, session_column=session_column
        )
        self._feature_frame: FeatureFrame = feature_frame
        self._strategy: IStrategy = strategy
        self._instrument: InstrumentSpec = instrument
        self._portfolio: Portfolio = portfolio
        self._broker: SimulatedBroker = broker
        self._risk: RiskEngine = risk
        self._ids: SequentialIdFactory = ids
        self._allow_unreconciled: bool = allow_unreconciled

        self._pending: list[ApprovedOrder] = []
        self._pending_levels: dict[str, ProtectiveLevels] = {}
        self._levels: dict[TradeId, ProtectiveLevels] = {}
        self._equity: list[EquitySnapshot] = []
        self._rejected: list[RejectedIntent] = []
        self._decisions: int = 0

    # -- public ------------------------------------------------------------------------

    def run(self) -> BacktestResult:
        """Execute the full run and reconcile.

        Returns:
            The immutable result.

        Raises:
            ReconciliationFailedError: If Directive 6 fails and ``allow_unreconciled`` is off.
        """
        bar_count: int = self._factory.bar_count
        if bar_count < 2:
            raise BacktestError("a run needs at least two bars", bars=bar_count)

        _LOGGER.info(
            "engine.run_started",
            strategy=self._strategy.strategy_id,
            version=self._strategy.strategy_version,
            bars=bar_count,
            first_decision=self._factory.first_decision_index,
            last_decision=self._factory.last_decision_index,
        )

        state: StrategyState = self._strategy.initial_state()
        for index in range(bar_count):
            state = self._step(index, state)

        final_bar: BarData = self._factory.bar_at(bar_count - 1)
        self._portfolio.mark_open_position_at_end(final_bar, bar_index=bar_count - 1)
        report: ReconciliationReport = self._portfolio.reconcile()
        if not report.passed and not self._allow_unreconciled:
            raise ReconciliationFailedError(report.summary(), discrepancy=str(report.discrepancy))

        peak, max_drawdown = equity_curve_bounds(self._equity)
        result = BacktestResult(
            strategy_id=self._strategy.strategy_id,
            strategy_version=self._strategy.strategy_version,
            trades=self._portfolio.closed_trades,
            fills=self._portfolio.ledger,
            equity_curve=tuple(self._equity),
            rejected_intents=tuple(self._rejected),
            risk_events=self._risk.events,
            reconciliation=report,
            trusted=report.passed,
            halted_at_end=self._risk.is_halted,
            bars_processed=bar_count,
            decisions_taken=self._decisions,
            sealed_columns=tuple(sorted(self._sealed.features)),
            feature_set_hash=self._feature_frame.feature_set_hash(),
            peak_mtm_equity=peak,
            max_drawdown=max_drawdown,
        )
        _LOGGER.info(
            "engine.run_finished",
            trades=len(result.closed_trades),
            fills=len(result.fills),
            rejections=len(result.rejected_intents),
            reconciled=report.passed,
            trusted=result.trusted,
            final_cash=str(result.final_cash),
            max_drawdown=str(max_drawdown),
        )
        return result

    # -- the seven steps (G-3) ---------------------------------------------------------

    def _step(self, index: int, state: StrategyState) -> StrategyState:
        """Process one bar in the G-3 order.

        Args:
            index: Bar index.
            state: Strategy state entering the bar.

        Returns:
            Strategy state leaving the bar.
        """
        bar: BarData = self._factory.bar_at(index)
        session = self._factory.session_at(index)

        # 1. Execute orders queued at t-1 against open(t).
        queued: Sequence[ApprovedOrder] = tuple(self._pending)
        self._pending.clear()
        for order in queued:
            self._execute(order, bar, index)

        # 2. Resolve resting protective exits against bar t.
        self._resolve_protective_exit(bar, index)

        # 3. Mark to market at close(t).
        snapshot: EquitySnapshot = self._portfolio.mark_to_market(bar)
        self._equity.append(snapshot)

        # 4. Risk evaluates the drawdown limit (adverse-first intrabar extreme, G-2).
        self._risk.on_bar_close(
            snapshot=snapshot,
            bar=bar,
            position=self._portfolio.position,
            bar_index=index,
            session_index=session.session_index,
        )

        # 5-7 only inside the decision window (ASS-001, ASS-002).
        if not self._factory.first_decision_index <= index <= self._factory.last_decision_index:
            return state

        view: PortfolioView = self._portfolio.view(
            mark_price=bar.close, is_halted=self._risk.is_halted
        )
        ctx = self._factory.build(index, view)

        # 5. Strategy evaluates.
        decision = self._strategy.evaluate(ctx, state)
        self._decisions += 1

        # 6. Risk approves or rejects each intent, in the order the strategy emitted them.
        exits_pending_close: bool = False
        for intent in decision.intents:
            trade_id: TradeId = self._lifecycle_for(intent, exits_pending_close)
            approval = self._risk.approve(intent, view=view, bar=bar, trade_id=trade_id)
            if approval.order is None:
                assert approval.rejection is not None
                self._rejected.append(approval.rejection)
                continue
            # 7. Queue for t+1, remembering the protective levels the intent declared.
            approved: ApprovedOrder = approval.order
            self._pending.append(approved)
            if not approved.is_exit:
                self._pending_levels[approved.order_id] = ProtectiveLevels(
                    stop=intent.stop_loss, target=intent.take_profit
                )
            elif (
                view.open_position is not None and approved.quantity >= view.open_position.quantity
            ):
                exits_pending_close = True

        return decision.state

    def _lifecycle_for(self, intent: TradeIntent, exit_pending_close: bool) -> TradeId:
        """Return the trade lifecycle an intent's order should carry.

        A fresh identifier is issued when the book is flat, or when an exit queued earlier
        in the same bar will have closed the position before this entry fills. Otherwise
        the open lifecycle is joined; a reversal joins it too, and the portfolio splits the
        opening leg into a new lifecycle at booking time.

        Args:
            intent: The intent being approved.
            exit_pending_close: Whether an earlier intent this bar closes the position.

        Returns:
            The lifecycle identifier.
        """
        if not intent.is_exit and exit_pending_close:
            return self._ids.next_trade_id()
        return self._portfolio.allocate_trade_id()

    # -- execution ---------------------------------------------------------------------

    def _execute(self, order: ApprovedOrder, bar: BarData, index: int) -> None:
        """Submit a queued order against the execution bar and book any fill.

        Args:
            order: Approved order queued at the prior bar.
            bar: Execution bar.
            index: Its index.
        """
        result: BrokerResult = self._broker.submit(order, bar, self._instrument)
        if not result.filled or result.fill is None:
            self._pending_levels.pop(order.order_id, None)
            self._rejected.append(
                RejectedIntent(
                    intent=order.intent,
                    code=result.rejection_code or OrderRejectionCode.NO_EXECUTION_BAR,
                    detail=result.detail,
                    rejected_at_utc=bar.ts_utc,
                )
            )
            return

        outcome = self._portfolio.apply_fill(result.fill, bar_index=index)
        levels: ProtectiveLevels | None = self._pending_levels.pop(order.order_id, None)
        position = self._portfolio.position

        if position is None:
            self._levels.clear()
            return
        if levels is not None and levels.armed:
            # An entry (or the opening leg of a reversal) arms the lifecycle it opened.
            self._levels[position.trade_id] = levels
        elif outcome.closed_trade is not None:
            # A reversal closed one lifecycle and opened another with no new levels.
            self._levels.pop(outcome.closed_trade.trade_id, None)

    def _resolve_protective_exit(self, bar: BarData, index: int) -> None:
        """Fire a resting stop or target if bar ``index`` reached it (G-3 step 2).

        Args:
            bar: The bar to test.
            index: Its index.
        """
        position = self._portfolio.position
        if position is None:
            return
        levels: ProtectiveLevels | None = self._levels.get(position.trade_id)
        if levels is None or not levels.armed:
            return

        armed = replace(position, current_stop=levels.stop, current_target=levels.target)
        trigger = self._broker.resolve_protective_exit(armed, bar)
        if trigger is None:
            return

        intent = TradeIntent(
            intent_id=self._ids.next_intent_id(),
            ts_utc=bar.ts_utc,
            instrument_id=self._instrument.instrument_id,
            direction=position.direction.opposite,
            is_exit=True,
            requested_quantity=position.quantity,
            exit_reason=trigger.reason,
        )
        view: PortfolioView = self._portfolio.view(
            mark_price=trigger.fill_price, is_halted=self._risk.is_halted
        )
        approval = self._risk.approve(intent, view=view, bar=bar, trade_id=position.trade_id)
        if approval.order is None:
            assert approval.rejection is not None
            self._rejected.append(approval.rejection)
            return

        result: BrokerResult = self._broker.submit(
            approval.order,
            bar,
            self._instrument,
            forced_price=trigger.fill_price,
            exit_reason=trigger.reason,
        )
        if not result.filled or result.fill is None:
            self._rejected.append(
                RejectedIntent(
                    intent=intent,
                    code=result.rejection_code or OrderRejectionCode.NO_EXECUTION_BAR,
                    detail=result.detail,
                    rejected_at_utc=bar.ts_utc,
                )
            )
            return

        self._portfolio.apply_fill(result.fill, bar_index=index)
        self._levels.pop(position.trade_id, None)
        _LOGGER.debug(
            "engine.protective_exit",
            bar_index=index,
            reason=str(trigger.reason),
            trigger=str(trigger.trigger_price),
            fill=str(trigger.fill_price),
            gapped=trigger.gapped,
        )
