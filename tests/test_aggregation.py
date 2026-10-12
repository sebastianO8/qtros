"""Ingestion-time aggregation and the HistData one-minute source format.

Every expected value is derived by hand from five-row inputs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest
from pydantic import ValidationError

from qtros.configuration import ConfigLoader, DataSourceConfig
from qtros.data.aggregate import aggregate_bars, validate_aggregation
from qtros.data.store import WarehouseStore
from qtros.domain.enums import Timeframe
from qtros.domain.errors import ConfigurationError, SchemaContractError
from qtros.domain.identifiers import InstrumentId
from qtros.workflow import ingest_source

CONFIG_ROOT = Path(__file__).resolve().parent.parent / "config"
T0 = datetime(2024, 1, 3, 10, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


def minute_bars(rows: list[tuple[int, float, float, float, float, float | None]]) -> pl.DataFrame:
    """Build one-minute bars from (minute offset, o, h, l, c, volume) tuples."""
    return pl.DataFrame(
        {
            "ts_utc": [T0 + MINUTE * r[0] for r in rows],
            "open": [r[1] for r in rows],
            "high": [r[2] for r in rows],
            "low": [r[3] for r in rows],
            "close": [r[4] for r in rows],
            "volume": pl.Series([r[5] for r in rows], dtype=pl.Float64),
        }
    ).with_columns(pl.col("ts_utc").dt.cast_time_unit("us"))


class TestAggregateBars:
    def test_ohlc_by_hand(self) -> None:
        bars = minute_bars(
            [
                (0, 95.000, 95.010, 94.990, 95.005, 10.0),
                (1, 95.005, 95.030, 95.000, 95.020, 20.0),
                (2, 95.020, 95.020, 94.950, 94.960, 30.0),
                (3, 94.960, 94.980, 94.940, 94.970, 40.0),
                (4, 94.970, 95.000, 94.960, 94.990, 50.0),
            ]
        )
        out = aggregate_bars(bars, source=Timeframe.M1, target=Timeframe.M5)
        assert out.height == 1
        row = out.row(0, named=True)
        assert row["ts_utc"] == T0
        assert (row["open"], row["high"], row["low"], row["close"]) == (
            95.000,
            95.030,
            94.940,
            94.990,
        )
        assert row["volume"] == 150.0

    def test_windows_are_left_closed_and_epoch_aligned(self) -> None:
        # Minutes 3,4 belong to the 10:00 window; minutes 5,6 to the 10:05 window.
        bars = minute_bars([(m, 95.0, 95.0, 95.0, 95.0, None) for m in (3, 4, 5, 6)])
        out = aggregate_bars(bars, source=Timeframe.M1, target=Timeframe.M5)
        assert out.get_column("ts_utc").to_list() == [T0, T0 + 5 * MINUTE]

    def test_partial_windows_use_only_observed_minutes(self) -> None:
        bars = minute_bars([(0, 95.0, 95.2, 94.9, 95.1, None), (4, 95.1, 95.3, 95.0, 95.2, None)])
        row = aggregate_bars(bars, source=Timeframe.M1, target=Timeframe.M5).row(0, named=True)
        assert (row["open"], row["high"], row["low"], row["close"]) == (95.0, 95.3, 94.9, 95.2)

    def test_empty_windows_produce_no_bar(self) -> None:
        bars = minute_bars([(0, 95.0, 95.0, 95.0, 95.0, None), (12, 96.0, 96.0, 96.0, 96.0, None)])
        out = aggregate_bars(bars, source=Timeframe.M1, target=Timeframe.M5)
        assert out.get_column("ts_utc").to_list() == [T0, T0 + 10 * MINUTE]

    def test_unknown_volume_stays_unknown(self) -> None:
        bars = minute_bars([(0, 95.0, 95.0, 95.0, 95.0, None), (1, 95.0, 95.0, 95.0, 95.0, None)])
        assert aggregate_bars(bars, source=Timeframe.M1, target=Timeframe.M5).get_column(
            "volume"
        ).to_list() == [None]

    def test_partially_known_volume_sums_the_known_part(self) -> None:
        bars = minute_bars([(0, 95.0, 95.0, 95.0, 95.0, 7.0), (1, 95.0, 95.0, 95.0, 95.0, None)])
        assert aggregate_bars(bars, source=Timeframe.M1, target=Timeframe.M5).get_column(
            "volume"
        ).to_list() == [7.0]

    @pytest.mark.parametrize(
        ("source", "target", "match"),
        [
            (Timeframe.M5, Timeframe.M5, "coarser"),
            (Timeframe.M15, Timeframe.M5, "coarser"),
            (Timeframe.M5, Timeframe.H1, None),
            (Timeframe.M1, Timeframe.D1, "daily"),
        ],
    )
    def test_validation(self, source: Timeframe, target: Timeframe, match: str | None) -> None:
        if match is None:
            validate_aggregation(source, target)
        else:
            with pytest.raises(ConfigurationError, match=match):
                validate_aggregation(source, target)

    def test_missing_column_is_refused(self) -> None:
        with pytest.raises(SchemaContractError):
            aggregate_bars(pl.DataFrame({"ts_utc": [T0]}), source=Timeframe.M1, target=Timeframe.M5)


class TestSourceConfig:
    def test_committed_histdata_source_aggregates_to_m5(self) -> None:
        source = ConfigLoader(CONFIG_ROOT).load_one(
            "source", "histdata_audjpy_m1", DataSourceConfig
        )
        assert source.timeframe is Timeframe.M1
        assert source.stored_timeframe is Timeframe.M5
        assert source.columns.volume is None
        assert source.to_spec().stored_timeframe is Timeframe.M5

    def test_existing_sources_are_unchanged(self) -> None:
        source = ConfigLoader(CONFIG_ROOT).load_one("source", "dukascopy_m5", DataSourceConfig)
        assert source.aggregate_to is None and source.stored_timeframe is Timeframe.M5

    def test_inexact_aggregation_is_refused(self) -> None:
        base = ConfigLoader(CONFIG_ROOT).load_one("source", "histdata_audjpy_m1", DataSourceConfig)
        document = {**base.model_dump(), "timeframe": "M15", "aggregate_to": "M5"}
        with pytest.raises(ValidationError, match="coarser exact multiple"):
            DataSourceConfig.model_validate(document)

    def test_histdata_backtest_resolves(self) -> None:
        import qtros.alpha.strategies.vwap_pullback  # noqa: F401

        resolved = ConfigLoader(CONFIG_ROOT).load_backtest("histdata_audjpy_2023_2025")
        assert resolved.source.source_id == "histdata_audjpy_m1"
        assert resolved.source.stored_timeframe in resolved.strategy.supported_timeframes


class TestHistDataIngestion:
    def test_semicolon_headerless_fixed_est_file_lands_as_utc_m5(self, tmp_path: Path) -> None:
        """HistData stamps bars in EST with no DST: 17:00 EST is 22:00 UTC all year."""
        raw = tmp_path / "data_lake" / "raw" / "histdata_audjpy_m1"
        raw.mkdir(parents=True)
        lines = [
            "20240703 170000;160.100000;160.120000;160.090000;160.110000;0",
            "20240703 170100;160.110000;160.150000;160.100000;160.140000;0",
            "20240703 170400;160.140000;160.140000;160.050000;160.060000;0",
            "20240703 170500;160.060000;160.070000;160.060000;160.070000;0",
        ]
        (raw / "DAT_ASCII_AUDJPY_M1_2024.csv").write_text("\n".join(lines) + "\n")
        base = ConfigLoader(CONFIG_ROOT).load_one("source", "histdata_audjpy_m1", DataSourceConfig)
        source = base.model_copy(update={"root_path": raw})
        warehouse = tmp_path / "data_lake" / "warehouse" / "qtros.duckdb"

        report = ingest_source(
            source,
            warehouse_path=warehouse,
            input_root=tmp_path / "data_lake" / "raw",
            results_root=tmp_path / "results",
        )
        assert report.rows_read == 4 and report.rows_rejected == 0 and report.rows_written == 2

        with WarehouseStore(warehouse, read_only=True) as store:
            stored = store.query_bars(
                instrument_id=InstrumentId("AUDJPY"),
                timeframe=Timeframe.M5,
                start_utc=datetime(2024, 7, 3, tzinfo=UTC),
                end_utc=datetime(2024, 7, 4, tzinfo=UTC),
            )
        assert stored.get_column("ts_utc").to_list() == [
            datetime(2024, 7, 3, 22, 0, tzinfo=UTC),
            datetime(2024, 7, 3, 22, 5, tzinfo=UTC),
        ]
        first = stored.row(0, named=True)
        assert (first["open"], first["high"], first["low"], first["close"]) == (
            160.10,
            160.15,
            160.05,
            160.06,
        )
        assert first["volume"] is None


@pytest.mark.regression
class TestNullVolumeIsNotAViolation:
    """Found by the first real-data ingest (HistData publishes no volume).

    ``volume < 0`` is null when volume is null, and the violation mask's null backstop
    turned that into a rejection, so every bar of a volume-less source was dropped.
    """

    def test_valid_bars_with_unknown_volume_pass(self) -> None:
        from qtros.data.coverage import ohlc_violation_mask, violation_counts

        bars = minute_bars([(0, 95.0, 95.1, 94.9, 95.0, None), (1, 95.0, 95.2, 94.8, 95.1, None)])
        assert ohlc_violation_mask(bars).to_list() == [False, False]
        assert dict(violation_counts(bars)) == {} or all(
            v == 0 for v in violation_counts(bars).values()
        )

    def test_negative_volume_is_still_caught(self) -> None:
        from qtros.data.coverage import ohlc_violation_mask

        bars = minute_bars([(0, 95.0, 95.1, 94.9, 95.0, -1.0), (1, 95.0, 95.2, 94.8, 95.1, None)])
        assert ohlc_violation_mask(bars).to_list() == [True, False]

    def test_null_price_is_still_caught(self) -> None:
        from qtros.data.coverage import ohlc_violation_mask

        bars = minute_bars([(0, 95.0, 95.1, 94.9, 95.0, None)]).with_columns(
            pl.lit(None, dtype=pl.Float64).alias("high")
        )
        assert ohlc_violation_mask(bars).to_list() == [True]
