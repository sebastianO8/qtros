"""Session calendars and trading-day arithmetic (ARCH-002 D-3).

The one ``domain/`` subpackage permitted behaviour beyond dataclass methods, on three
conditions: no I/O, no filesystem, no config parsing. Calendars are constructed from
configuration by ``configuration/``; this package receives already-parsed rules.
"""

from __future__ import annotations

from qtros.domain.timekeeping.calendar import (
    SessionBounds,
    SessionCalendar,
    SessionWindow,
    WeekBoundary,
    localize,
)

__all__ = ["SessionBounds", "SessionCalendar", "SessionWindow", "WeekBoundary", "localize"]
