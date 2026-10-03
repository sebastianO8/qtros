"""The session calendar (ARCH-002 D-3; Cycle 1 DST lesson).

Three consumers depend on this object: ``data/`` for gap attribution, ``risk/`` for the
trading-day boundary, and ``alpha/`` for session logic. It lives in ``domain/`` so all
three may import it without creating a cycle.

Two conventions carry the implementation:

**The trading day follows a wall-clock rollover in a named zone.** For FX that is 17:00
New York, so the UTC boundary moves twice a year with US DST. A bar stamped after the
rollover belongs to the *next* calendar date's trading day: Sunday 18:00 New York is
Monday's trading day.

**Local times that DST makes ambiguous or nonexistent are refused.** Under
``STRICT_RAISE`` — the Phase 1 default — a session boundary that falls into a spring-forward
gap or a fall-back overlap raises rather than resolving by heuristic. Loud beats plausible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from qtros.domain.enums import DstPolicy, GapKind, Timeframe
from qtros.domain.errors import ConfigurationError, TimezoneError
from qtros.domain.identifiers import CalendarId

if TYPE_CHECKING:
    from collections.abc import Iterator

_ONE_DAY: Final = timedelta(days=1)
_ONE_MINUTE: Final = timedelta(minutes=1)
_FINE_STEP_LIMIT: Final[int] = 20_160
"""Minute steps inspected inside a gap before coarsening to hourly (two weeks)."""


def _zone(name: str) -> ZoneInfo:
    """Resolve an IANA zone or raise a configuration error."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigurationError("unknown IANA timezone", timezone=name) from exc


def localize(naive: datetime, zone: str, policy: DstPolicy = DstPolicy.STRICT_RAISE) -> datetime:
    """Attach a zone to a naive local time, resolving DST per policy.

    Args:
        naive: Wall-clock time with no tzinfo.
        zone: IANA zone name.
        policy: What to do when the wall-clock time is ambiguous or does not exist.

    Returns:
        An aware datetime.

    Raises:
        TimezoneError: If the time is nonexistent (spring forward) or ambiguous (fall
            back) and the policy does not resolve it. ``DROP_AMBIGUOUS`` is an ingestion
            policy and cannot apply to a session boundary, so it raises here too.
        ValueError: If ``naive`` already carries a zone.
    """
    if naive.tzinfo is not None:
        raise ValueError("localize expects a naive datetime")
    tz = _zone(zone)
    early = naive.replace(tzinfo=tz, fold=0)
    late = naive.replace(tzinfo=tz, fold=1)

    round_trip = early.astimezone(UTC).astimezone(tz).replace(tzinfo=None)
    if round_trip != naive:
        if policy is DstPolicy.STRICT_RAISE or policy is DstPolicy.DROP_AMBIGUOUS:
            raise TimezoneError(
                "local time does not exist in this zone (DST spring-forward gap)",
                local=naive.isoformat(),
                zone=zone,
            )
        return early.astimezone(UTC).astimezone(tz)

    if early.utcoffset() != late.utcoffset():
        if policy is DstPolicy.EARLIEST:
            return early
        if policy is DstPolicy.LATEST:
            return late
        raise TimezoneError(
            "local time is ambiguous in this zone (DST fall-back overlap)",
            local=naive.isoformat(),
            zone=zone,
        )
    return early


@dataclass(frozen=True, slots=True)
class WeekBoundary:
    """A weekly open or close instant expressed as a local wall-clock rule.

    Attributes:
        weekday: ISO weekday index, Monday ``0`` through Sunday ``6``.
        time_local: Wall-clock time on that weekday.
        timezone: IANA zone the time is expressed in.
    """

    weekday: int
    time_local: time
    timezone: str

    def __post_init__(self) -> None:
        """Validate the weekday and zone."""
        if not 0 <= self.weekday <= 6:
            raise ConfigurationError(
                "weekday must be 0 (Monday) .. 6 (Sunday)", weekday=self.weekday
            )
        _zone(self.timezone)


@dataclass(frozen=True, slots=True)
class SessionWindow:
    """A named intraday session, for labelling and session-scoped logic.

    Sessions do not define whether the market is open — the weekly boundaries and the
    holiday list do. A window that crosses midnight (``open_local > close_local``) is
    permitted.

    Attributes:
        name: Stable label, e.g. ``asian``.
        open_local: Session open, wall-clock.
        close_local: Session close, wall-clock.
        timezone: IANA zone of both times.
        weekdays: Weekdays on which the session opens.
    """

    name: str
    open_local: time
    close_local: time
    timezone: str
    weekdays: frozenset[int]

    def __post_init__(self) -> None:
        """Validate the window."""
        if not self.name:
            raise ConfigurationError("session window needs a name")
        if any(not 0 <= d <= 6 for d in self.weekdays) or not self.weekdays:
            raise ConfigurationError(
                "session weekdays must be a non-empty subset of 0..6", name=self.name
            )
        if self.open_local == self.close_local:
            raise ConfigurationError("session open and close cannot coincide", name=self.name)
        _zone(self.timezone)

    @property
    def crosses_midnight(self) -> bool:
        """Return whether the window wraps past local midnight."""
        return self.open_local > self.close_local


@dataclass(frozen=True, slots=True)
class SessionBounds:
    """A session's resolved UTC bounds on one trading day.

    Attributes:
        name: Session label.
        open_utc: Opening instant.
        close_utc: Closing instant, exclusive.
    """

    name: str
    open_utc: datetime
    close_utc: datetime


class SessionCalendar:
    """Weekly open/close, holidays, trading-day rollover, and named sessions.

    Args:
        calendar_id: Stable key.
        week_open: When the trading week opens.
        week_close: When it closes.
        rollover_time_local: Wall-clock time at which the trading day rolls.
        rollover_tz: Zone of the rollover time.
        sessions: Named intraday windows.
        holidays: Trading days on which the market is closed.
        dst_policy: DST resolution for every local-to-UTC conversion.

    Raises:
        ConfigurationError: On an unknown zone or an empty identifier.
    """

    __slots__ = (
        "_calendar_id",
        "_dst_policy",
        "_holidays",
        "_rollover_time",
        "_rollover_tz",
        "_sessions",
        "_week_close",
        "_week_open",
    )

    def __init__(
        self,
        *,
        calendar_id: str,
        week_open: WeekBoundary,
        week_close: WeekBoundary,
        rollover_time_local: time,
        rollover_tz: str,
        sessions: tuple[SessionWindow, ...] = (),
        holidays: frozenset[date] = frozenset(),
        dst_policy: DstPolicy = DstPolicy.STRICT_RAISE,
    ) -> None:
        if not calendar_id:
            raise ConfigurationError("calendar_id must be non-empty")
        _zone(rollover_tz)
        self._calendar_id: CalendarId = CalendarId(calendar_id)
        self._week_open: WeekBoundary = week_open
        self._week_close: WeekBoundary = week_close
        self._rollover_time: time = rollover_time_local
        self._rollover_tz: str = rollover_tz
        self._sessions: tuple[SessionWindow, ...] = tuple(sessions)
        self._holidays: frozenset[date] = frozenset(holidays)
        self._dst_policy: DstPolicy = dst_policy

    # -- identity ----------------------------------------------------------------------

    @property
    def calendar_id(self) -> CalendarId:
        """Return the calendar's stable key."""
        return self._calendar_id

    @property
    def sessions(self) -> tuple[SessionWindow, ...]:
        """Return the named sessions."""
        return self._sessions

    @property
    def holidays(self) -> frozenset[date]:
        """Return the closed trading days."""
        return self._holidays

    # -- trading day -------------------------------------------------------------------

    def trading_day_for(self, instant_utc: datetime) -> date:
        """Return the trading day an instant belongs to.

        Args:
            instant_utc: Aware instant.

        Returns:
            The local calendar date, advanced by one when the local time is at or past
            the rollover. Sunday 18:00 New York is Monday's trading day.

        Raises:
            ValueError: If the instant is naive.
        """
        self._require_aware(instant_utc)
        local = instant_utc.astimezone(_zone(self._rollover_tz))
        day = local.date()
        if local.time() >= self._rollover_time:
            day += _ONE_DAY
        return day

    def trading_day_start(self, trading_day: date) -> datetime:
        """Return the UTC instant at which a trading day begins.

        Args:
            trading_day: The trading day.

        Returns:
            The rollover instant on the previous calendar date, in UTC.
        """
        local = datetime.combine(trading_day - _ONE_DAY, self._rollover_time)
        return localize(local, self._rollover_tz, self._dst_policy).astimezone(UTC)

    # -- openness ----------------------------------------------------------------------

    def is_open_at(self, instant_utc: datetime) -> bool:
        """Return whether the market is open at an instant.

        Open means inside the weekly window and not on a holiday. Named sessions do not
        gate openness; a continuous market like FX has no closed minutes between them.

        Args:
            instant_utc: Aware instant.

        Returns:
            Whether trading can occur.
        """
        self._require_aware(instant_utc)
        return self._in_week_window(instant_utc) and not self._is_holiday(instant_utc)

    def _is_holiday(self, instant_utc: datetime) -> bool:
        """Return whether the instant's trading day is a holiday."""
        return bool(self._holidays) and self.trading_day_for(instant_utc) in self._holidays

    def _in_week_window(self, instant_utc: datetime) -> bool:
        """Return whether the instant lies between the week's open and close."""
        open_at = self._latest_boundary_at_or_before(self._week_open, instant_utc)
        close_at = self._next_boundary_after(self._week_close, open_at)
        return open_at <= instant_utc < close_at

    def _latest_boundary_at_or_before(
        self, boundary: WeekBoundary, instant_utc: datetime
    ) -> datetime:
        """Return the most recent occurrence of a weekly boundary at or before an instant."""
        local = instant_utc.astimezone(_zone(boundary.timezone))
        for back in range(8):
            day = local.date() - timedelta(days=back)
            if day.weekday() != boundary.weekday:
                continue
            candidate = localize(
                datetime.combine(day, boundary.time_local), boundary.timezone, self._dst_policy
            ).astimezone(UTC)
            if candidate <= instant_utc:
                return candidate
        raise ConfigurationError("weekly boundary could not be located", weekday=boundary.weekday)

    def _next_boundary_after(self, boundary: WeekBoundary, instant_utc: datetime) -> datetime:
        """Return the first occurrence of a weekly boundary strictly after an instant."""
        local = instant_utc.astimezone(_zone(boundary.timezone))
        for ahead in range(8):
            day = local.date() + timedelta(days=ahead)
            if day.weekday() != boundary.weekday:
                continue
            candidate = localize(
                datetime.combine(day, boundary.time_local), boundary.timezone, self._dst_policy
            ).astimezone(UTC)
            if candidate > instant_utc:
                return candidate
        raise ConfigurationError("weekly boundary could not be located", weekday=boundary.weekday)

    def week_bounds(self, instant_utc: datetime) -> tuple[datetime, datetime]:
        """Return the (open, close) UTC instants of the trading week containing an instant.

        Args:
            instant_utc: Aware instant. If it falls in a weekend, the *previous* week's
                bounds are returned, which is what gap classification needs.

        Returns:
            Open and close instants.
        """
        self._require_aware(instant_utc)
        open_at = self._latest_boundary_at_or_before(self._week_open, instant_utc)
        return open_at, self._next_boundary_after(self._week_close, open_at)

    # -- sessions ----------------------------------------------------------------------

    def session_at(self, instant_utc: datetime) -> SessionWindow | None:
        """Return the named session containing an instant, if any.

        Args:
            instant_utc: Aware instant.

        Returns:
            The first matching window in declaration order, or ``None``.
        """
        self._require_aware(instant_utc)
        for window in self._sessions:
            local = instant_utc.astimezone(_zone(window.timezone))
            weekday, clock = local.weekday(), local.time()
            if not window.crosses_midnight:
                if weekday in window.weekdays and window.open_local <= clock < window.close_local:
                    return window
            elif (weekday in window.weekdays and clock >= window.open_local) or (
                (weekday - 1) % 7 in window.weekdays and clock < window.close_local
            ):
                return window
        return None

    def session_bounds(self, trading_day: date) -> tuple[SessionBounds, ...]:
        """Return every named session's UTC bounds on a trading day.

        Args:
            trading_day: The trading day. Sessions are anchored to the calendar date
                whose weekday matches their declaration.

        Returns:
            Bounds for each session that opens on that weekday, in declaration order.
        """
        bounds: list[SessionBounds] = []
        for window in self._sessions:
            if trading_day.weekday() not in window.weekdays:
                continue
            open_dt = localize(
                datetime.combine(trading_day, window.open_local), window.timezone, self._dst_policy
            )
            close_day = trading_day + _ONE_DAY if window.crosses_midnight else trading_day
            close_dt = localize(
                datetime.combine(close_day, window.close_local), window.timezone, self._dst_policy
            )
            bounds.append(
                SessionBounds(window.name, open_dt.astimezone(UTC), close_dt.astimezone(UTC))
            )
        return tuple(bounds)

    # -- Layer 0 surface (SessionCalendarLike) -----------------------------------------

    def expected_bar_count(
        self, start_utc: datetime, end_utc: datetime, timeframe: Timeframe
    ) -> int:
        """Return how many bars should open in ``[start, end)`` while the market is open.

        Args:
            start_utc: Inclusive window start.
            end_utc: Exclusive window end.
            timeframe: Bar interval.

        Returns:
            The count of open bar-open instants.
        """
        self._require_aware(start_utc)
        self._require_aware(end_utc)
        return sum(
            1
            for instant in self._bar_opens(start_utc, end_utc, timeframe.duration)
            if self.is_open_at(instant)
        )

    def classify_gap(self, start_utc: datetime, end_utc: datetime) -> GapKind:
        """Attribute a discontinuity between two consecutive bars.

        Args:
            start_utc: Timestamp of the bar before the gap.
            end_utc: Timestamp of the bar after the gap.

        The calendar does not know the bar interval, so it cannot tell which interior
        minutes *should* have carried a bar. It therefore asks a weaker question: did the
        market close at any point inside the gap? A gap containing no closure is missing
        data. A gap containing a closure is attributed to that closure, even if it also
        swallowed a few bars either side of it; ``expected_bar_count`` exists to catch that
        remainder.

        Returns:
            ``UNEXPECTED`` if the market was open at every interior minute; otherwise
            ``WEEKEND`` if the interior crosses the weekly close, ``HOLIDAY`` if it lies
            on a holiday, else ``SESSION_BREAK``.
        """
        self._require_aware(start_utc)
        self._require_aware(end_utc)
        if end_utc <= start_utc:
            raise ValueError("gap end must follow gap start")

        span_minutes = int((end_utc - start_utc) / _ONE_MINUTE)
        step = _ONE_MINUTE if span_minutes <= _FINE_STEP_LIMIT else timedelta(hours=1)
        interior = list(self._bar_opens(start_utc + step, end_utc, step))
        if not interior:
            return GapKind.SESSION_BREAK
        closed = [instant for instant in interior if not self.is_open_at(instant)]
        if not closed:
            return GapKind.UNEXPECTED
        if any(not self._in_week_window(instant) for instant in closed):
            return GapKind.WEEKEND
        if any(self._is_holiday(instant) for instant in closed):
            return GapKind.HOLIDAY
        return GapKind.SESSION_BREAK

    # -- helpers -----------------------------------------------------------------------

    @staticmethod
    def _bar_opens(start: datetime, end: datetime, step: timedelta) -> Iterator[datetime]:
        """Yield instants from ``start`` stepping by ``step`` while before ``end``."""
        current = start
        while current < end:
            yield current
            current += step

    @staticmethod
    def _require_aware(instant: datetime) -> None:
        """Refuse a naive datetime; the Cycle 1 lesson as a precondition."""
        if instant.tzinfo is None or instant.tzinfo.utcoffset(instant) is None:
            raise ValueError("calendar operations require an aware datetime")
