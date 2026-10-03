"""Session calendar configuration (ARCH-002 §4.3, D-3)."""

from __future__ import annotations

from datetime import date, time

from pydantic import Field, model_validator

from qtros.configuration.models.base import QtrosConfig, TimeZoneName
from qtros.domain.enums import DstPolicy
from qtros.domain.timekeeping import SessionCalendar, SessionWindow, WeekBoundary


class WeekBoundaryConfig(QtrosConfig):
    """A weekly open or close rule.

    Attributes:
        weekday: Monday ``0`` through Sunday ``6``.
        time_local: Wall-clock time.
        timezone: IANA zone of the time.
    """

    weekday: int = Field(ge=0, le=6)
    time_local: time
    timezone: TimeZoneName


class SessionWindowConfig(QtrosConfig):
    """A named intraday session.

    Attributes:
        name: Stable label.
        open_local: Session open.
        close_local: Session close.
        timezone: IANA zone.
        weekdays: Weekdays the session opens on.
    """

    name: str = Field(min_length=1)
    open_local: time
    close_local: time
    timezone: TimeZoneName
    weekdays: frozenset[int]

    @model_validator(mode="after")
    def _weekdays_in_range(self) -> SessionWindowConfig:
        """Validate the weekday set."""
        if not self.weekdays or any(not 0 <= d <= 6 for d in self.weekdays):
            raise ValueError("weekdays must be a non-empty subset of 0..6")
        if self.open_local == self.close_local:
            raise ValueError("open_local and close_local cannot coincide")
        return self


class CalendarConfig(QtrosConfig):
    """Declarative session calendar.

    Attributes:
        calendar_id: Key; matches the file under ``config/calendars/``.
        week_open: Weekly open rule.
        week_close: Weekly close rule.
        sessions: Named sessions.
        holidays: Closed trading days.
        dst_policy: DST resolution for every boundary conversion.
    """

    calendar_id: str = Field(min_length=1)
    week_open: WeekBoundaryConfig
    week_close: WeekBoundaryConfig
    sessions: tuple[SessionWindowConfig, ...] = ()
    holidays: tuple[date, ...] = ()
    dst_policy: DstPolicy = DstPolicy.STRICT_RAISE

    def to_calendar(self, *, rollover_time_local: time, rollover_tz: str) -> SessionCalendar:
        """Construct the domain calendar.

        The rollover comes from the instrument (ARCH-002 §4.2) because the trading-day
        boundary is a property of what is traded, not of the venue's week.

        Args:
            rollover_time_local: Instrument's rollover wall-clock time.
            rollover_tz: Its zone.

        Returns:
            A :class:`SessionCalendar`.
        """
        return SessionCalendar(
            calendar_id=self.calendar_id,
            week_open=WeekBoundary(self.week_open.weekday, self.week_open.time_local, self.week_open.timezone),
            week_close=WeekBoundary(self.week_close.weekday, self.week_close.time_local, self.week_close.timezone),
            rollover_time_local=rollover_time_local,
            rollover_tz=rollover_tz,
            sessions=tuple(
                SessionWindow(s.name, s.open_local, s.close_local, s.timezone, frozenset(s.weekdays))
                for s in self.sessions
            ),
            holidays=frozenset(self.holidays),
            dst_policy=self.dst_policy,
        )
