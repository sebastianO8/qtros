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
