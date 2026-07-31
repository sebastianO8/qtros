"""Distinct identifier types.

These are :func:`typing.NewType` aliases rather than bare :class:`str`. They cost
nothing at runtime and let mypy catch the class of bug where a ``source_id`` is passed
where an ``instrument_id`` was expected — both are strings, both look plausible in a
log line, and the resulting backtest runs cleanly against the wrong data.
"""

from __future__ import annotations

from typing import NewType

InstrumentId = NewType("InstrumentId", str)
"""Canonical instrument key, e.g. ``AUDJPY``. Matches a file in ``config/instruments/``."""

SourceId = NewType("SourceId", str)
"""Raw data source key, e.g. ``dukascopy_m5``. Matches a file in ``config/sources/``."""

CalendarId = NewType("CalendarId", str)
"""Session calendar key, e.g. ``fx_sunday_open``. Matches a file in ``config/calendars/``."""

RunId = NewType("RunId", str)
"""Backtest run key: ``{utc_date}-{experiment_id}-{run_hash[:8]}``."""

TradeId = NewType("TradeId", str)
"""Identifies one flat-to-flat position lifecycle (ARCH-001 C-4)."""

FillId = NewType("FillId", str)
"""Identifies one atomic execution event. Fills are the only movers of cash."""


class SequentialIdFactory:
    """Deterministic identifier source for a single backtest run.

    Identifiers must be reproducible, not merely unique. ARCH-002 §6.1 claims that
    re-running from an archived manifest yields identical trade identifiers; a random
    source such as ``uuid4`` would make that claim untestable, because every run would
    differ in exactly the field used to compare runs. A counter seeded per run is unique
    within the run, stable across runs, and readable in a log.

    Args:
        run_prefix: Short prefix distinguishing this run's identifiers, normally the
            first characters of the run hash.

    Example:
        >>> factory = SequentialIdFactory("a1b2c3d4")
        >>> factory.next_fill_id()
        'a1b2c3d4-f-000001'
    """

    __slots__ = ("_counters", "_prefix")

    def __init__(self, run_prefix: str) -> None:
        self._prefix: str = run_prefix
        self._counters: dict[str, int] = {}

    def _next(self, kind: str) -> str:
        """Return the next identifier of a given kind.

        Args:
            kind: Single-character kind marker.

        Returns:
            A zero-padded identifier, sortable in the order it was issued.
        """
        nxt: int = self._counters.get(kind, 0) + 1
        self._counters[kind] = nxt
        return f"{self._prefix}-{kind}-{nxt:06d}"

    def next_intent_id(self) -> str:
        """Return the next trade intent identifier."""
        return self._next("i")

    def next_order_id(self) -> str:
        """Return the next order identifier."""
        return self._next("o")

    def next_fill_id(self) -> FillId:
        """Return the next fill identifier."""
        return FillId(self._next("f"))

    def next_trade_id(self) -> TradeId:
        """Return the next trade identifier."""
        return TradeId(self._next("t"))

    def issued(self) -> dict[str, int]:
        """Return how many identifiers of each kind have been issued.

        Returns:
            A copy of the internal counters, useful as a run-summary sanity check.
        """
        return dict(self._counters)