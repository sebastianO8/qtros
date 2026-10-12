"""AUD/JPY Asian-session sweep and reclaim (``audjpy_asian_sweep`` 1.0.0).

A port of the Cycle 1 strategy (a TradingView script translated to one Python file, July
2026) into this system's rules.

Mechanism: AUD/JPY builds a range during the Asian session (19:00-04:00 New York). After
the session, a move beyond the range that fails and closes back inside it (a sweep of the
stops resting beyond the range, then a reclaim) tends to reverse back across the range.

Entry, on a closed five-minute bar inside the 04:00-17:00 New York trade window:

* **Long** when price has traded below the Asian range low since the session ended and this
  bar closes back above the range low. Short is the mirror image at the range high.
* Only within ``freshness_bars`` bars of the session ending; ADX between ``adx_min`` and
  ``adx_max``; at least ``cooldown_bars`` bars after the previous exit; at most one long and
  one short per session.
* Stop at the sweep extreme, clamped so the risk is between ``min_stop_atr`` and
  ``max_stop_atr`` ATRs. Target at ``rr_multiple`` times the risk. Size is the risk
  engine's job (1% of closed equity to the stop), not the strategy's.
* Any open position is closed at the open of the next Asian session.

Deviations from the Cycle 1 script, each forced by this system's rules or engine:

1. **No 50% partial exit at 1R and no move-to-breakeven.** The engine has one resting stop
   and one resting target per position. Adding a second target, or moving a stop after
   entry, changes the run-loop ordering ruling (G-3) and the protected orders module, so
   they are left for a PI ruling rather than approximated here. Results are therefore not
   comparable to the Cycle 1 trade log.
2. **Risk and target are measured from the signal bar's close**, not the next bar's open.
   The script read the next bar's open to place the stop, a small look-ahead.
3. **ATR, ADX, range, and sweep extremes are as of the previous bar** (the T-1 rule). The
   current bar's own low or high still counts toward a sweep, by comparison only.
4. **Sweeps are tracked whether or not a position is open.** The script stopped tracking
   them while in a trade, which made a later setup depend on an earlier, unrelated trade.
5. **Sizing is 1% risk to the stop**, not 95% of equity in notional.
6. **Time zone.** The script read HistData timestamps as fixed EST; they follow New York
   daylight time, so its summer session windows were an hour off.
"""

from __future__ import annotations

from datetime import time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

from qtros.alpha.base import StrategyBase, register_strategy
from qtros.domain.enums import Direction, ExitReason
from qtros.domain.errors import ConfigurationError
from qtros.domain.orders import TradeIntent
from qtros.domain.signals import StrategyDecision

if TYPE_CHECKING:
    from datetime import datetime

    from qtros.domain.signals import BarContext, StrategyState

MECHANISM_TAG: Final[str] = "asian_range_sweep_reclaim"

_FEATURE_PARAMETERS: Final[tuple[str, ...]] = (
    "session_id_column",
    "range_high_column",
    "range_low_column",
    "bars_since_end_column",
    "sweep_low_column",
    "sweep_high_column",
    "atr_column",
    "adx_column",
)


def _clock(value: str, name: str) -> time:
    """Parse ``HH:MM``."""
    try:
        hours, minutes = (int(part) for part in value.split(":"))
        return time(hours, minutes)
    except (ValueError, TypeError) as exc:
        raise ConfigurationError(f"strategy parameter {name} must be HH:MM", value=value) from exc


def in_window(clock: time, start: time, end: time) -> bool:
    """Return whether a wall-clock time is in ``[start, end)``, which may cross midnight."""
    if start <= end:
        return start <= clock < end
    return clock >= start or clock < end


def _decimal(value: float | None) -> Decimal | None:
    """Convert a feature value to an exact decimal of its shortest representation."""
    return None if value is None else Decimal(str(value))


@register_strategy("audjpy_asian_sweep", "1.0.0")
class AudJpyAsianSweep(StrategyBase):
    """Fade a failed break of the Asian-session range. See the module docstring."""

    @property
    def required_features(self) -> frozenset[str]:
        """Return every feature column the strategy reads."""
        return frozenset(self.param_str(name) for name in _FEATURE_PARAMETERS)

    # -- helpers -----------------------------------------------------------------------

    def _local(self, instant: datetime) -> time:
        return instant.astimezone(ZoneInfo(self.param_str("timezone"))).time()

    def _in_range_session(self, clock: time) -> bool:
        return in_window(
            clock,
            _clock(self.param_str("range_start"), "range_start"),
            _clock(self.param_str("range_end"), "range_end"),
        )

    def _in_trade_window(self, clock: time) -> bool:
        return in_window(
            clock,
            _clock(self.param_str("trade_window_start"), "trade_window_start"),
            _clock(self.param_str("trade_window_end"), "trade_window_end"),
        )

    # -- decision ----------------------------------------------------------------------

    def evaluate(self, ctx: BarContext, state: StrategyState) -> StrategyDecision:
        """Decide on one closed bar.

        Args:
            ctx: The bar context. Features are as of the previous bar.
            state: Engine-owned state: last exit bar, trades seen, and which sides have
                fired in the current session.

        Returns:
            At most one intent: a session-close exit, or an entry.
        """
        features = ctx.features
        values: dict[str, object] = dict(state.values)

        trades_seen = ctx.portfolio.trade_count_to_date
        if trades_seen != values.get("trades_seen", 0):
            values["trades_seen"] = trades_seen
            values["last_exit_bar"] = ctx.bar_index

        session = features[self.param_str("session_id_column")]
        if session is not None and values.get("session") != session:
            values.update(session=session, bull_fired=False, bear_fired=False)

        now = self._local(ctx.ts_utc)
        bar_length = timedelta(minutes=self.param_int("bar_minutes"))
        next_open = self._local(ctx.ts_utc + bar_length)
        in_range_now = self._in_range_session(now)

        diagnostics: dict[str, float] = {
            "in_range_session": float(in_range_now),
            "adx": features[self.param_str("adx_column")] or 0.0,
        }

        def decide(*intents: TradeIntent) -> StrategyDecision:
            return StrategyDecision(
                state=state.advanced(**values), intents=intents, diagnostics=diagnostics
            )

        position = ctx.portfolio.open_position
        if position is not None:
            if in_range_now or self._in_range_session(next_open):
                return decide(
                    TradeIntent(
                        intent_id=f"{ctx.bar_index}-exit",
                        ts_utc=ctx.ts_utc,
                        instrument_id=ctx.instrument_id,
                        direction=position.direction.opposite,
                        is_exit=True,
                        exit_reason=ExitReason.SESSION_CLOSE,
                    )
                )
            return decide()

        if in_range_now or not self._in_trade_window(now) or ctx.portfolio.is_halted:
            return decide()
        last_exit = values.get("last_exit_bar")
        if isinstance(last_exit, int) and ctx.bar_index - last_exit < self.param_int(
            "cooldown_bars"
        ):
            return decide()

        range_high = _decimal(features[self.param_str("range_high_column")])
        range_low = _decimal(features[self.param_str("range_low_column")])
        atr = _decimal(features[self.param_str("atr_column")])
        adx = features[self.param_str("adx_column")]
        if range_high is None or range_low is None or atr is None or atr <= 0 or adx is None:
            return decide()

        # bars_since_end is as of the previous bar; null there means the previous bar was
        # still inside the session, so this bar is the first one after it.
        since_previous = features[self.param_str("bars_since_end_column")]
        bars_since_end = 0 if since_previous is None else int(since_previous) + 1
        fresh = bars_since_end <= self.param_int("freshness_bars")
        adx_ok = self.param_decimal("adx_min") <= Decimal(str(adx)) <= self.param_decimal("adx_max")
        if not (fresh and adx_ok):
            return decide()

        bar = ctx.bar
        sweep_low = _decimal(features[self.param_str("sweep_low_column")])
        if bar.low < range_low and (sweep_low is None or bar.low < sweep_low):
            sweep_low = bar.low
        sweep_high = _decimal(features[self.param_str("sweep_high_column")])
        if bar.high > range_high and (sweep_high is None or bar.high > sweep_high):
            sweep_high = bar.high

        if sweep_low is not None and not values.get("bull_fired") and bar.close > range_low:
            values["bull_fired"] = True
            return decide(self._entry(ctx, Direction.LONG, sweep_low, atr, range_low, range_high))
        if sweep_high is not None and not values.get("bear_fired") and bar.close < range_high:
            values["bear_fired"] = True
            return decide(self._entry(ctx, Direction.SHORT, sweep_high, atr, range_low, range_high))
        return decide()

    def _entry(
        self,
        ctx: BarContext,
        direction: Direction,
        extreme: Decimal,
        atr: Decimal,
        range_low: Decimal,
        range_high: Decimal,
    ) -> TradeIntent:
        """Build an entry with the ATR-clamped stop and the R-multiple target.

        Args:
            ctx: The bar context.
            direction: Side to enter.
            extreme: The sweep extreme, the unclamped stop.
            atr: ATR as of the previous bar.
            range_low: Asian range low.
            range_high: Asian range high.

        Returns:
            The entry intent. The stop and target are rounded to the tick grid the same way
            as the reference strategy, toward the entry.
        """
        close = ctx.bar.close
        sign = Decimal(direction.sign)
        raw_risk = (close - extreme) * sign
        min_risk = atr * self.param_decimal("min_stop_atr")
        max_risk = atr * self.param_decimal("max_stop_atr")
        risk = min(max(raw_risk, min_risk), max_risk)
        stop = close - risk * sign
        target = close + risk * self.param_decimal("rr_multiple") * sign
        instrument = ctx.instrument
        return TradeIntent(
            intent_id=f"{ctx.bar_index}-entry",
            ts_utc=ctx.ts_utc,
            instrument_id=ctx.instrument_id,
            direction=direction,
            stop_loss=instrument.round_price(stop, direction=direction, adverse=True),
            take_profit=instrument.round_price(target, direction=direction.opposite, adverse=True),
            mechanism_tag=MECHANISM_TAG,
            rationale={
                "range_low": float(range_low),
                "range_high": float(range_high),
                "sweep_extreme": float(extreme),
                "atr": float(atr),
                "risk": float(risk),
            },
        )
