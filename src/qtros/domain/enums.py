"""Enumerations shared across the system.

This module currently holds the Layer 0 subset. Later layers append to it; nothing is
renamed once written, because enum member names appear in archived configs and in
``results/`` manifests that Directive 10 requires to remain readable years later.

All enums are :class:`enum.StrEnum` so they serialise to their own names in YAML, JSON,
and DuckDB without a conversion table.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Final


class Timeframe(StrEnum):
    """Bar interval.

    The interval is load-bearing beyond labelling: Cycle 1 established that a strategy
    profitable on 5-minute bars was decisively unprofitable on 15-minute bars, so the
    timeframe is part of a strategy's identity, not a display detail.
    """

    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"

    @property
    def duration(self) -> timedelta:
        """Return the nominal wall-clock duration of one bar.

        Returns:
            The bar interval as a :class:`datetime.timedelta`.

        Note:
            ``D1`` returns 24 hours. This is the *nominal* duration and is deliberately
            not DST-aware: a daily bar spanning a DST transition is 23 or 25 hours long
            in local time. Gap detection must therefore consult a session calendar for
            daily data rather than relying on this value alone.
        """
        return _TIMEFRAME_DURATIONS[self]


_TIMEFRAME_DURATIONS: Final[dict[Timeframe, timedelta]] = {
    Timeframe.M1: timedelta(minutes=1),
    Timeframe.M5: timedelta(minutes=5),
    Timeframe.M15: timedelta(minutes=15),
    Timeframe.M30: timedelta(minutes=30),
    Timeframe.H1: timedelta(hours=1),
    Timeframe.H4: timedelta(hours=4),
    Timeframe.D1: timedelta(days=1),
}


class FileFormat(StrEnum):
    """Supported raw source file formats."""

    CSV = "CSV"
    PARQUET = "PARQUET"


class TimestampPolicy(StrEnum):
    """How a source's raw timestamp column is to be interpreted.

    ARCH-002 schema amendment A-1. ``DataSourceConfig`` originally declared only
    ``declared_tz``, which cannot express the common case of a vendor whose timestamps
    already carry an explicit UTC offset. Inferring the difference at parse time would
    reintroduce exactly the ambiguity that cost Cycle 1 an evening, so the source
    declares which case applies and parsing never guesses.
    """

    EMBEDDED_OFFSET = "EMBEDDED_OFFSET"
    """Timestamp string carries its own offset (``%z``). Converted directly to UTC."""

    DECLARED_ZONE = "DECLARED_ZONE"
    """Naive local timestamps in a named IANA zone. Subject to DST resolution."""

    DECLARED_FIXED_OFFSET = "DECLARED_FIXED_OFFSET"
    """Naive timestamps at a constant offset. No DST, therefore no ambiguity."""


class DstPolicy(StrEnum):
    """How to resolve local timestamps that DST makes ambiguous or nonexistent.

    ``STRICT_RAISE`` is the Phase 1 default. A loud failure at ingestion is recoverable;
    a plausible off-by-one-hour bar series produces a backtest that looks correct and
    is not.
    """

    STRICT_RAISE = "STRICT_RAISE"
    EARLIEST = "EARLIEST"
    LATEST = "LATEST"
    DROP_AMBIGUOUS = "DROP_AMBIGUOUS"


class IngestMode(StrEnum):
    """Write semantics for an ingestion run."""

    APPEND_ONLY = "APPEND_ONLY"
    """Insert new bars; silently skip any (instrument, timeframe, ts) already present."""

    REPLACE_RANGE = "REPLACE_RANGE"
    """Delete the incoming timestamp range for this instrument/timeframe, then insert.

    Used when a vendor reissues corrected history. The deleted row count is reported so
    a silent restatement is impossible.
    """

    DRY_RUN = "DRY_RUN"
    """Parse, validate, and report. Write nothing."""


class GapKind(StrEnum):
    """Classification of a discontinuity in a bar series.

    Only ``UNEXPECTED`` is a data quality problem. The others are the market being
    closed, and treating them as missing data produces phantom gap-fill bars that can
    generate signals on prices that never existed.
    """

    WEEKEND = "WEEKEND"
    HOLIDAY = "HOLIDAY"
    SESSION_BREAK = "SESSION_BREAK"
    UNEXPECTED = "UNEXPECTED"
    UNCLASSIFIED = "UNCLASSIFIED"
    """No session calendar was supplied, so the gap could not be attributed."""


class GapPolicy(StrEnum):
    """What ``load_bars`` does when it encounters an unexpected gap."""

    RAISE = "RAISE"
    """Abort the load. The default: a strategy should not silently run on partial data."""

    FILL_FORWARD = "FILL_FORWARD"
    """Synthesise bars flagged ``is_synthetic=True`` so downstream code can exclude them."""

    LEAVE = "LEAVE"
    """Return the frame as-is, gaps included, recorded in the coverage report."""


class OhlcViolation(StrEnum):
    """Ways a bar can be internally impossible.

    These are corruption, not gaps. The system rejects such rows rather than repairing
    them, because a repaired bar is an invented price.
    """

    HIGH_BELOW_OPEN = "HIGH_BELOW_OPEN"
    HIGH_BELOW_CLOSE = "HIGH_BELOW_CLOSE"
    LOW_ABOVE_OPEN = "LOW_ABOVE_OPEN"
    LOW_ABOVE_CLOSE = "LOW_ABOVE_CLOSE"
    HIGH_BELOW_LOW = "HIGH_BELOW_LOW"
    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    NEGATIVE_VOLUME = "NEGATIVE_VOLUME"
    NULL_PRICE = "NULL_PRICE"


# --------------------------------------------------------------------------------------
# Layer 1 / 3.5 — trading, orders, and execution
# --------------------------------------------------------------------------------------


class AssetClass(StrEnum):
    """Broad instrument category. Drives contract maths, not strategy logic."""

    FX_SPOT = "FX_SPOT"
    FUTURES = "FUTURES"
    EQUITY = "EQUITY"
    CFD = "CFD"


class Direction(StrEnum):
    """Side of a position or order."""

    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> int:
        """Return +1 for long and -1 for short.

        Returns:
            The directional multiplier used in every PnL calculation. Centralising it
            here means the sign convention exists once rather than in each formula.
        """
        return 1 if self is Direction.LONG else -1

    @property
    def opposite(self) -> Direction:
        """Return the opposing direction."""
        return Direction.SHORT if self is Direction.LONG else Direction.LONG


class OrderType(StrEnum):
    """Order kind. Phase 1 simulates MARKET plus resting protective orders."""

    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class ExitReason(StrEnum):
    """Why a position was reduced or closed.

    Recorded on every exit fill. Exit-reason mix is one of the fastest reads on whether
    a strategy behaves as its mechanism claims: a breakout strategy exiting mostly on
    SESSION_CLOSE is not doing what its author believes.
    """

    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    SIGNAL_EXIT = "SIGNAL_EXIT"
    REVERSAL = "REVERSAL"
    SESSION_CLOSE = "SESSION_CLOSE"
    RISK_HALT = "RISK_HALT"
    BACKTEST_END = "BACKTEST_END"


class ExecutionTiming(StrEnum):
    """When a decision taken on bar T is filled."""

    NEXT_BAR_OPEN = "NEXT_BAR_OPEN"
    """Fill at bar T+1's open. The Phase 1 default and the only defensible one for a
    decision that used bar T's close."""

    SAME_BAR_CLOSE = "SAME_BAR_CLOSE"
    """Fill at bar T's close. Optimistic: it assumes the close was both observable and
    tradeable at the same instant. Permitted, stamped in the manifest, and watermarked
    in every report that uses it."""


class PathAssumption(StrEnum):
    """Assumed order of intrabar price events (ARCH-001 C-1).

    OHLC data does not record whether the high or the low came first, so a bar in which
    both a stop and a target lie inside the range is genuinely ambiguous. The assumption
    is declared rather than resolved by heuristic.
    """

    ADVERSE_FIRST = "ADVERSE_FIRST"
    """Assume the adverse extreme is reached first. Pessimistic, deterministic, and the
    approved Phase 1 default."""

    FAVOURABLE_FIRST = "FAVOURABLE_FIRST"
    """Assume the favourable extreme is reached first. Flatters results; available only
    for sensitivity analysis and watermarked wherever used."""


class ZeroVolumePolicy(StrEnum):
    """How to treat an order that would fill on a bar reporting zero volume."""

    REJECT_FILL = "REJECT_FILL"
    ALLOW_FILL = "ALLOW_FILL"


class SubMinLotPolicy(StrEnum):
    """How to treat a size that rounds below the instrument's minimum lot."""

    REJECT_ORDER = "REJECT_ORDER"
    """Reject explicitly. A zero-quantity fill is a trade that did not happen being
    recorded as though it did."""

    ROUND_UP = "ROUND_UP"
    """Raise to the minimum lot. Silently increases risk beyond the sizing decision, so
    it is never the default."""


class OrderRejectionCode(StrEnum):
    """Enumerated reason an order was not filled.

    Enumerated rather than free text so rejections are aggregatable: a rising count of
    ZERO_VOLUME_BAR rejections is a data problem, while a rising count of
    BELOW_MINIMUM_LOT is a sizing problem, and a free-text field would hide both.
    """

    ZERO_VOLUME_BAR = "ZERO_VOLUME_BAR"
    BELOW_MINIMUM_LOT = "BELOW_MINIMUM_LOT"
    NON_POSITIVE_QUANTITY = "NON_POSITIVE_QUANTITY"
    NO_EXECUTION_BAR = "NO_EXECUTION_BAR"
    INSTRUMENT_MISMATCH = "INSTRUMENT_MISMATCH"
    MARKET_CLOSED = (
        "MARKET_CLOSED"  # ARCH-008 additions — Layer 3 (risk) rejection reasons. Append-only.
    )
    RISK_HALTED = "RISK_HALTED"
    """Entry refused because the drawdown halt is active (G-2). Exits are never refused
    for this reason."""

    EXCEEDS_MAX_LOT = "EXCEEDS_MAX_LOT"
    """Sized quantity exceeds the hard lot cap (G-1, ASS-008)."""

    EXCEEDS_NOTIONAL_CAP = "EXCEEDS_NOTIONAL_CAP"
    """Resulting notional exposure exceeds the configured share of equity (G-1, ASS-008)."""

    MAX_POSITIONS_OPEN = "MAX_POSITIONS_OPEN"
    """Entry would exceed ``max_concurrent_positions``."""

    NO_STOP_FOR_SIZING = "NO_STOP_FOR_SIZING"
    """Entry carries no stop level, so fixed-fractional sizing has no risk distance."""

    SIZED_TO_ZERO = "SIZED_TO_ZERO"
    """Sizing produced a quantity below the minimum lot under ``REJECT_ORDER``."""

    NO_OPEN_POSITION = "NO_OPEN_POSITION"
    """Exit requested while flat."""


# --------------------------------------------------------------------------------------
# ARCH-008 — configuration vocabulary (ARCH-002 §4) and risk (G-1, G-2)
# --------------------------------------------------------------------------------------


class DrawdownBasis(StrEnum):
    """Equity basis a drawdown limit is measured on (ARCH-001 C-1)."""

    MARK_TO_MARKET = "MARK_TO_MARKET"
    """Cash plus open-position mark. The only basis a prop firm uses, and the default."""

    CLOSED_EQUITY = "CLOSED_EQUITY"
    """Cash only. Available for comparison; never the gate basis."""


class BreachAction(StrEnum):
    """What the risk engine does when a limit is breached."""

    HALT_SESSION = "HALT_SESSION"
    """Block new entries for the remainder of the current session."""

    HALT_RUN = "HALT_RUN"
    """Block new entries for the remainder of the run."""


class CommissionModel(StrEnum):
    """How commission is charged."""

    PER_LOT = "PER_LOT"
    PER_NOTIONAL = "PER_NOTIONAL"
    NONE = "NONE"


class SpreadModel(StrEnum):
    """Where the spread figure comes from."""

    FIXED_TICKS = "FIXED_TICKS"
    INSTRUMENT_TYPICAL = "INSTRUMENT_TYPICAL"


class SlippageModel(StrEnum):
    """How slippage is sized."""

    FIXED_TICKS = "FIXED_TICKS"
    ATR_FRACTION = "ATR_FRACTION"


class SignificanceTier(StrEnum):
    """Trade-count tier a strategy is judged under (ARCH-001 C-2)."""

    STANDARD = "STANDARD"
    """400 closed trades, single instrument."""

    LOW_FREQUENCY = "LOW_FREQUENCY"
    """At least 60 trades pooled across at least four instruments."""


class ReturnBasis(StrEnum):
    """Equity series the Sharpe ratio is computed from (ARCH-001 C-5)."""

    DAILY_CLOSED_EQUITY = "DAILY_CLOSED_EQUITY"
    DAILY_MTM_EQUITY = "DAILY_MTM_EQUITY"


class ReturnType(StrEnum):
    """Return arithmetic used for the Sharpe ratio (ARCH-001 C-5)."""

    ARITHMETIC = "ARITHMETIC"
    LOG = "LOG"


class PeriodAttribution(StrEnum):
    """Which date a trade is attributed to for period statistics (ARCH-001 C-3)."""

    CLOSE_DATE = "CLOSE_DATE"
    OPEN_DATE = "OPEN_DATE"


class RiskEventKind(StrEnum):
    """Risk engine events recorded to ``risk_events`` (G-2)."""

    DRAWDOWN_HALT = "DRAWDOWN_HALT"
    """Drawdown limit reached on the adverse-first intrabar basis; entries blocked."""

    HALT_RELEASED = "HALT_RELEASED"
    """A session-scoped halt lapsed at a session boundary."""

    PEAK_UPDATED = "PEAK_UPDATED"
    """Running peak equity advanced. Debug-level; not written in Phase 1."""


class GateVerdict(StrEnum):
    """Outcome of one constitutional gate check (Layer 5)."""

    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
    """The metric was computed but the trade count is below the significance floor."""
    NOT_EVALUATED = "NOT_EVALUATED"
    """The gate needs inputs a single in-sample run cannot provide (OOS decay, WFO)."""
