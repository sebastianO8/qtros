"""Coverage policy: declared gap tolerance and closed-market bars (PI ruling 2026-10-12)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import polars as pl
import pytest
from pydantic import ValidationError

from qtros.configuration import ConfigLoader, DataSourceConfig, ResolvedBacktestConfig
from qtros.data.coverage import analyze_coverage
from qtros.domain.data_contracts import CoverageReport, GapRecord
from qtros.domain.enums import GapKind, Timeframe
from qtros.domain.errors import CoverageError
from qtros.domain.identifiers import InstrumentId
from qtros.domain.timekeeping import SessionCalendar, WeekBoundary
from qtros.workflow import execute
from tests.test_builder import CONFIG_ROOT, synthetic_bars
from tests.test_cli_workflow import populate

if TYPE_CHECKING:
    from pathlib import Path

RUN_AT = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
NY = "America/New_York"


def gap(missing: int, kind: GapKind = GapKind.UNEXPECTED) -> GapRecord:
    start = datetime(2024, 1, 3, tzinfo=UTC)
    return GapRecord(
        start,
        start + timedelta(minutes=5 * (missing + 1)),
        timedelta(minutes=5 * (missing + 1)),
        missing,
        kind,
    )


class TestMissingBarFraction:
    def test_by_hand(self) -> None:
        report = CoverageReport(
            instrument_id=InstrumentId("AUDJPY"),
            timeframe=Timeframe.M5,
            row_count=950,
            first_ts_utc=None,
            last_ts_utc=None,
            gaps=(gap(30), gap(20), gap(500, GapKind.WEEKEND)),
        )
        # Weekend gaps are explained; only 30 + 20 are missing out of 1000 expected.
        assert report.unexpected_missing_bars == 50
        assert report.missing_bar_fraction == Decimal("0.05")

    def test_empty_series_is_zero(self) -> None:
        report = CoverageReport(
            instrument_id=InstrumentId("AUDJPY"),
            timeframe=Timeframe.M5,
            row_count=0,
            first_ts_utc=None,
            last_ts_utc=None,
        )
        assert report.missing_bar_fraction == 0


class TestClosedMarketBars:
    def test_bars_on_a_weekend_are_flagged_not_dropped(self) -> None:
        calendar = SessionCalendar(
            calendar_id="fx",
            week_open=WeekBoundary(6, time(17, 0), NY),
            week_close=WeekBoundary(4, time(17, 0), NY),
            rollover_time_local=time(17, 0),
            rollover_tz=NY,
        )
        friday = datetime(2024, 1, 5, 21, 50, tzinfo=UTC)  # 16:50 New York, open
        stamps = [friday + timedelta(minutes=5 * i) for i in range(4)]  # 16:50 .. 17:05 NY
        bars = pl.DataFrame(
            {
                "ts_utc": stamps,
                "open": [95.0] * 4,
                "high": [95.0] * 4,
                "low": [95.0] * 4,
                "close": [95.0] * 4,
            }
        ).with_columns(pl.col("ts_utc").dt.cast_time_unit("us"))
        report = analyze_coverage(
            bars, instrument_id=InstrumentId("AUDJPY"), timeframe=Timeframe.M5, calendar=calendar
        )
        assert report.row_count == 4
        assert report.closed_market_bars == 2  # 17:00 and 17:05 New York, after the Friday close
        assert report.closed_market_samples[0] == datetime(2024, 1, 5, 22, 0, tzinfo=UTC)

    def test_no_calendar_means_no_count(self) -> None:
        bars = synthetic_bars(10)
        report = analyze_coverage(
            bars, instrument_id=InstrumentId("AUDJPY"), timeframe=Timeframe.M5
        )
        assert report.closed_market_bars == 0


class TestToleranceConfig:
    def test_histdata_declares_the_ruled_tolerance(self) -> None:
        source = ConfigLoader(CONFIG_ROOT).load_one(
            "source", "histdata_audjpy_m1", DataSourceConfig
        )
        assert source.max_missing_bar_fraction == Decimal("0.05")

    def test_sources_without_a_declaration_tolerate_nothing(self) -> None:
        source = ConfigLoader(CONFIG_ROOT).load_one("source", "dukascopy_m5", DataSourceConfig)
        assert source.max_missing_bar_fraction is None

    @pytest.mark.parametrize("value", ["-0.01", "1", "1.5"])
    def test_out_of_range_tolerance_is_refused(self, value: str) -> None:
        base = ConfigLoader(CONFIG_ROOT).load_one("source", "histdata_audjpy_m1", DataSourceConfig)
        with pytest.raises(ValidationError, match="max_missing_bar_fraction"):
            DataSourceConfig.model_validate(
                {**base.model_dump(), "max_missing_bar_fraction": value}
            )

    def test_float_tolerance_is_refused(self) -> None:
        base = ConfigLoader(CONFIG_ROOT).load_one("source", "histdata_audjpy_m1", DataSourceConfig)
        with pytest.raises(ValidationError, match="float"):
            DataSourceConfig.model_validate({**base.model_dump(), "max_missing_bar_fraction": 0.05})


def with_tolerance(
    resolved: ResolvedBacktestConfig, tolerance: str | None
) -> ResolvedBacktestConfig:
    value = Decimal(tolerance) if tolerance is not None else None
    return resolved.model_copy(
        update={"source": resolved.source.model_copy(update={"max_missing_bar_fraction": value})}
    )


class TestWorkflowPolicy:
    @pytest.fixture
    def gappy_warehouse(self, tmp_path: Path) -> Path:
        """900 bars with one 30-bar hole midweek: 30 / 900 = 3.33% missing."""
        bars = synthetic_bars(900)
        holed = pl.concat([bars.head(300), bars.slice(330)])
        path = tmp_path / "w" / "qtros.duckdb"
        populate(path, holed)
        return path

    @pytest.fixture(scope="class")
    def resolved(self) -> ResolvedBacktestConfig:
        import qtros.alpha.strategies  # noqa: F401 - registers every strategy

        return ConfigLoader(CONFIG_ROOT).load_backtest("phase1_mvp")

    def test_gaps_within_tolerance_pass_without_watermark_and_are_recorded(
        self, resolved: ResolvedBacktestConfig, gappy_warehouse: Path, tmp_path: Path
    ) -> None:
        outcome = execute(
            with_tolerance(resolved, "0.05"),
            warehouse_path=gappy_warehouse,
            results_root=tmp_path / "results",
            run_at_utc=RUN_AT,
        )
        assert outcome.loaded.accepted
        assert not any("coverage" in w for w in outcome.report.gates.watermarks)
        assert outcome.written is not None
        recorded = json.loads((outcome.written.directory / "coverage.json").read_text())
        assert recorded["unexpected_gaps"] == 1
        assert recorded["unexpected_missing_bars"] == 30
        assert recorded["missing_bar_fraction"] == "0.033333"
        assert recorded["max_missing_bar_fraction"] == "0.05"
        assert recorded["accepted_without_watermark"] is True
        assert "Data coverage" in (outcome.written.directory / "report.html").read_text()

    def test_gaps_above_tolerance_are_refused(
        self, resolved: ResolvedBacktestConfig, gappy_warehouse: Path, tmp_path: Path
    ) -> None:
        with pytest.raises(CoverageError, match="coverage requirements"):
            execute(
                with_tolerance(resolved, "0.01"),
                warehouse_path=gappy_warehouse,
                results_root=tmp_path / "r",
                write=False,
                render=False,
                run_at_utc=RUN_AT,
            )

    def test_no_declared_tolerance_means_no_gaps_tolerated(
        self, resolved: ResolvedBacktestConfig, gappy_warehouse: Path, tmp_path: Path
    ) -> None:
        with pytest.raises(CoverageError):
            execute(
                with_tolerance(resolved, None),
                warehouse_path=gappy_warehouse,
                results_root=tmp_path / "r",
                write=False,
                render=False,
                run_at_utc=RUN_AT,
            )

    def test_allowed_override_is_still_watermarked(
        self, resolved: ResolvedBacktestConfig, gappy_warehouse: Path, tmp_path: Path
    ) -> None:
        outcome = execute(
            with_tolerance(resolved, "0.01"),
            warehouse_path=gappy_warehouse,
            results_root=tmp_path / "r",
            write=False,
            render=False,
            allow_gaps=True,
            run_at_utc=RUN_AT,
        )
        assert not outcome.loaded.accepted
        assert any("coverage" in w for w in outcome.report.gates.watermarks)
