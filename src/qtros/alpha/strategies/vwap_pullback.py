"""Reference strategy: VWAP pullback mean reversion (``vwap_pullback`` 1.0.0).

Mechanism hypothesis: price stretched below session VWAP by more than a threshold tends
to revert toward it within the session. Long only in this version so that the reference
run has one mechanism to attribute.

Every value read comes from ``ctx.features`` under a configured column name. The strategy
performs **no arithmetic on bar fields** beyond comparison (DIR-001); the stop and target
distances come from a sealed ATR feature, not from the bar.
"""

from __future__ import annotations

from decimal import Decimal

from qtros.alpha.base import StrategyBase, register_strategy
from qtros.domain.enums import Direction, ExitReason
from qtros.domain.orders import TradeIntent
from qtros.domain.signals import BarContext, StrategyDecision, StrategyState

MECHANISM_TAG = "vwap_pullback_mean_reversion"


@register_strategy("vwap_pullback", "1.0.0")
class VwapPullback(StrategyBase):
    """Long when the z-score of price versus VWAP is stretched; exit on reversion.

    Parameters:
        vwap_column: Sealed VWAP feature column.
        zscore_column: Sealed z-score feature column.
        atr_column: Sealed ATR feature column.
        entry_z: Enter when z-score is at or below this (negative).
        exit_z: Exit when z-score is at or above this.
        stop_atr: Stop distance in ATR multiples.
        target_atr: Target distance in ATR multiples.
    """

    @property
    def required_features(self) -> frozenset[str]:
        """Return the three feature columns this strategy reads."""
        return frozenset(
            {
                self.param_str("vwap_column"),
                self.param_str("zscore_column"),
                self.param_str("atr_column"),
            }
        )

    def evaluate(self, ctx: BarContext, state: StrategyState) -> StrategyDecision:
        """Decide on one bar.

        Args:
            ctx: The bar context.
            state: Engine-owned state.

        Returns:
            The decision. Intents carry the stop and target the risk engine sizes from.
        """
        zscore = ctx.features[self.param_str("zscore_column")]
        atr = ctx.features[self.param_str("atr_column")]
        vwap = ctx.features[self.param_str("vwap_column")]
        diagnostics = {"zscore": zscore or 0.0, "atr": atr or 0.0, "vwap": vwap or 0.0}
        next_state = state.advanced()

        if zscore is None or atr is None or vwap is None or atr <= 0:
            return StrategyDecision(state=next_state, diagnostics=diagnostics)

        entry_z = self.param_decimal("entry_z")
        exit_z = self.param_decimal("exit_z")
        z = Decimal(str(zscore))
        atr_price = Decimal(str(atr))

        if ctx.portfolio.is_flat:
            if z <= entry_z and not ctx.portfolio.is_halted:
                stop = ctx.bar.close - atr_price * self.param_decimal("stop_atr")
                target = ctx.bar.close + atr_price * self.param_decimal("target_atr")
                intent = TradeIntent(
                    intent_id=f"{ctx.bar_index}-entry",
                    ts_utc=ctx.ts_utc,
                    instrument_id=ctx.instrument_id,
                    direction=Direction.LONG,
                    stop_loss=ctx.instrument.round_price(stop, direction=Direction.LONG, adverse=True),
                    take_profit=ctx.instrument.round_price(target, direction=Direction.SHORT, adverse=True),
                    mechanism_tag=MECHANISM_TAG,
                    rationale={"zscore": zscore, "atr": atr},
                )
                return StrategyDecision(
                    state=next_state.advanced(last_signal_ts_utc=ctx.ts_utc),
                    intents=(intent,),
                    diagnostics=diagnostics,
                )
            return StrategyDecision(state=next_state, diagnostics=diagnostics)

        position = ctx.portfolio.open_position
        if position is not None and position.direction is Direction.LONG and z >= exit_z:
            intent = TradeIntent(
                intent_id=f"{ctx.bar_index}-exit",
                ts_utc=ctx.ts_utc,
                instrument_id=ctx.instrument_id,
                direction=Direction.SHORT,
                is_exit=True,
                exit_reason=ExitReason.SIGNAL_EXIT,
                rationale={"zscore": zscore},
            )
            return StrategyDecision(state=next_state, intents=(intent,), diagnostics=diagnostics)
        return StrategyDecision(state=next_state, diagnostics=diagnostics)
