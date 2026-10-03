"""Session calendar tests (ARCH-002 D-3; the Cycle 1 DST lesson)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from qtros.domain.enums import DstPolicy, GapKind, Timeframe
from qtros.domain.errors import ConfigurationError, TimezoneError
from qtros.domain.timekeeping import SessionCalendar, SessionWindow, WeekBoundary, localize

NY = "America/New_York"


@pytest.fixture(scope="module")
def fx() -> SessionCalendar:
    return SessionCalendar(
        calendar_id="fx_sunday_open",
        week_open=WeekBoundary(6, time(17, 0), NY),
        week_close=WeekBoundary(4, time(17, 0), NY),
        rollover_time_local=time(17, 0),
        rollover_tz=NY,
        sessions=(
            SessionWindow("asian", time(9, 0), time(15, 0), "Asia/Tokyo", frozenset(range(5))),
            SessionWindow("late_ny", time(20, 0), time(2, 0), NY, frozenset(range(5))),
        ),
        holidays=frozenset({date(2024, 1, 1)}),
    )


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


class TestTradingDay:
    def test_before_rollover_is_the_same_calendar_date(self, fx: SessionCalendar) -> None:
        # 2024-01-03 16:59 NY (winter, UTC-5) = 21:59 UTC
        assert fx.trading_day_for(utc(2024, 1, 3, 21, 59)) == date(2024, 1, 3)

    def test_at_rollover_is_the_next_trading_day(self, fx: SessionCalendar) -> None:
        assert fx.trading_day_for(utc(2024, 1, 3, 22, 0)) == date(2024, 1, 4)

    def test_sunday_evening_is_monday(self, fx: SessionCalendar) -> None:
        assert fx.trading_day_for(utc(2024, 1, 7, 23, 0)) == date(2024, 1, 8)

    def test_rollover_moves_with_us_dst(self, fx: SessionCalendar) -> None:
        """Summer: 17:00 NY is 21:00 UTC, not 22:00. A UTC constant would be wrong here."""
        assert fx.trading_day_for(utc(2024, 7, 3, 20, 59)) == date(2024, 7, 3)
        assert fx.trading_day_for(utc(2024, 7, 3, 21, 0)) == date(2024, 7, 4)

    def test_trading_day_start_is_the_prior_rollover(self, fx: SessionCalendar) -> None:
        assert fx.trading_day_start(date(2024, 1, 4)) == utc(2024, 1, 3, 22, 0)

    def test_naive_input_is_refused(self, fx: SessionCalendar) -> None:
        with pytest.raises(ValueError, match="aware"):
            fx.trading_day_for(datetime(2024, 1, 3, 12, 0))  # noqa: DTZ001


class TestOpenness:
    def test_open_midweek(self, fx: SessionCalendar) -> None:
        assert fx.is_open_at(utc(2024, 1, 3, 12, 0))

    def test_closed_saturday(self, fx: SessionCalendar) -> None:
        assert not fx.is_open_at(utc(2024, 1, 6, 12, 0))

    def test_week_opens_at_sunday_1700_ny(self, fx: SessionCalendar) -> None:
        assert not fx.is_open_at(utc(2024, 1, 7, 21, 59))
        assert fx.is_open_at(utc(2024, 1, 7, 22, 0))

    def test_week_closes_at_friday_1700_ny(self, fx: SessionCalendar) -> None:
        assert fx.is_open_at(utc(2024, 1, 5, 21, 59))
        assert not fx.is_open_at(utc(2024, 1, 5, 22, 0))

    def test_holiday_closes_the_whole_trading_day(self, fx: SessionCalendar) -> None:
        # Trading day 2024-01-01 runs from Sunday 17:00 NY (Dec 31 22:00 UTC) to Monday 17:00 NY.
        assert not fx.is_open_at(utc(2024, 1, 1, 10, 0))
        assert not fx.is_open_at(utc(2023, 12, 31, 23, 0))
        assert fx.is_open_at(utc(2024, 1, 1, 22, 30))  # trading day Jan 2

    def test_week_bounds(self, fx: SessionCalendar) -> None:
        open_at, close_at = fx.week_bounds(utc(2024, 1, 3, 12, 0))
        assert open_at == utc(2023, 12, 31, 22, 0)
        assert close_at == utc(2024, 1, 5, 22, 0)


class TestSessions:
    def test_asian_session_by_tokyo_clock(self, fx: SessionCalendar) -> None:
        window = fx.session_at(utc(2024, 1, 3, 1, 0))  # 10:00 Tokyo
        assert window is not None and window.name == "asian"
        assert fx.session_at(utc(2024, 1, 3, 7, 0)) is None  # 16:00 Tokyo, 02:00 NY

    def test_session_crossing_midnight(self, fx: SessionCalendar) -> None:
        window = fx.session_at(utc(2024, 1, 4, 6, 0))  # 01:00 NY Thursday, opened Wed 20:00
        assert window is not None and window.name == "late_ny"

    def test_session_bounds_in_utc(self, fx: SessionCalendar) -> None:
        bounds = {b.name: b for b in fx.session_bounds(date(2024, 1, 3))}
        assert bounds["asian"].open_utc == utc(2024, 1, 3, 0, 0)
        assert bounds["asian"].close_utc == utc(2024, 1, 3, 6, 0)
        assert bounds["late_ny"].close_utc == utc(2024, 1, 4, 7, 0)

    def test_no_session_on_weekend(self, fx: SessionCalendar) -> None:
        assert fx.session_bounds(date(2024, 1, 6)) == ()


class TestLayerZeroSurface:
    def test_expected_bar_count_excludes_closed_time(self, fx: SessionCalendar) -> None:
        # Friday 21:00 → Sunday 23:00 UTC: 12 bars Friday, 12 bars Sunday, nothing between.
        assert (
            fx.expected_bar_count(utc(2024, 1, 5, 21, 0), utc(2024, 1, 7, 23, 0), Timeframe.M5)
            == 24
        )

    def test_weekend_gap(self, fx: SessionCalendar) -> None:
        assert fx.classify_gap(utc(2024, 1, 5, 21, 55), utc(2024, 1, 7, 22, 0)) is GapKind.WEEKEND

    def test_holiday_gap(self, fx: SessionCalendar) -> None:
        assert fx.classify_gap(utc(2023, 12, 29, 21, 55), utc(2024, 1, 1, 22, 0)) is GapKind.WEEKEND
        assert fx.classify_gap(utc(2024, 1, 1, 21, 55), utc(2024, 1, 2, 0, 0)) is GapKind.HOLIDAY

    def test_midweek_gap_is_unexpected(self, fx: SessionCalendar) -> None:
        assert fx.classify_gap(utc(2024, 1, 3, 10, 0), utc(2024, 1, 3, 12, 0)) is GapKind.UNEXPECTED

    def test_long_gap_coarsens_without_losing_the_closure(self, fx: SessionCalendar) -> None:
        assert fx.classify_gap(utc(2024, 1, 5, 21, 55), utc(2024, 2, 5, 22, 0)) is GapKind.WEEKEND

    def test_satisfies_the_data_layer_protocol(self, fx: SessionCalendar) -> None:
        from qtros.domain.data_contracts import SessionCalendarLike

        assert isinstance(fx, SessionCalendarLike)


class TestDst:
    def test_nonexistent_local_time_raises(self) -> None:
        with pytest.raises(TimezoneError, match="does not exist"):
            localize(datetime(2024, 3, 10, 2, 30), NY)  # noqa: DTZ001

    def test_ambiguous_local_time_raises_by_default(self) -> None:
        with pytest.raises(TimezoneError, match="ambiguous"):
            localize(datetime(2024, 11, 3, 1, 30), NY)  # noqa: DTZ001

    def test_ambiguous_resolves_under_explicit_policy(self) -> None:
        early = localize(datetime(2024, 11, 3, 1, 30), NY, DstPolicy.EARLIEST)  # noqa: DTZ001
        late = localize(datetime(2024, 11, 3, 1, 30), NY, DstPolicy.LATEST)  # noqa: DTZ001
        assert early.astimezone(UTC) == utc(2024, 11, 3, 5, 30)
        assert late.astimezone(UTC) == utc(2024, 11, 3, 6, 30)

    def test_drop_ambiguous_is_not_a_calendar_policy(self) -> None:
        with pytest.raises(TimezoneError):
            localize(datetime(2024, 11, 3, 1, 30), NY, DstPolicy.DROP_AMBIGUOUS)  # noqa: DTZ001

    def test_aware_input_is_refused(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            localize(utc(2024, 1, 1), NY)

    def test_unknown_zone_is_a_configuration_error(self) -> None:
        with pytest.raises(ConfigurationError):
            WeekBoundary(0, time(9, 0), "Mars/Olympus_Mons")
