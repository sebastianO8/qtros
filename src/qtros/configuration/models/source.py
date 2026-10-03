"""Data source configuration (ARCH-002 §4.4, schema amendment A-1)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from pydantic import Field, model_validator

from qtros.configuration.models.base import QtrosConfig, StrictDecimal, TimeZoneName
from qtros.domain.data_contracts import ColumnMapping, SourceSpec
from qtros.domain.enums import DstPolicy, FileFormat, Timeframe, TimestampPolicy
from qtros.domain.identifiers import InstrumentId, SourceId


class ColumnMappingConfig(QtrosConfig):
    """Vendor column names for the canonical bar schema.

    Attributes:
        timestamp: Source column holding the bar timestamp.
        open: Opening price column.
        high: High price column.
        low: Low price column.
        close: Closing price column.
        volume: Volume column, or ``None`` when the vendor supplies none.
    """

    timestamp: str = Field(min_length=1)
    open: str = Field(min_length=1)
    high: str = Field(min_length=1)
    low: str = Field(min_length=1)
    close: str = Field(min_length=1)
    volume: str | None = None

    def to_mapping(self) -> ColumnMapping:
        """Return the domain mapping object."""
        return ColumnMapping(
            timestamp=self.timestamp,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )


class DataSourceConfig(QtrosConfig):
    """Declarative form of :class:`~qtros.domain.data_contracts.SourceSpec`.

    Attributes:
        source_id: Stable key; matches the file under ``config/sources/``.
        instrument_id: Instrument the files describe.
        timeframe: Bar interval.
        root_path: Directory containing the raw files.
        glob_pattern: Pattern matched within ``root_path``.
        file_format: Parser to use.
        columns: Vendor-to-canonical mapping.
        timestamp_format: ``strftime``-style parse format.
        timestamp_policy: How the parsed timestamp is interpreted. Never inferred.
        declared_zone: IANA zone, required iff ``DECLARED_ZONE``.
        declared_offset_minutes: Constant offset, required iff ``DECLARED_FIXED_OFFSET``.
        dst_policy: Resolution rule for ambiguous local times.
        timestamps_are_bar_open: Whether a timestamp labels the open or the close.
        csv_separator: Field separator for CSV sources.
        has_header: Whether CSV files carry a header row.
        recursive_glob: Whether the glob descends into subdirectories.
        max_reject_ratio: Fraction of rows that may be rejected before ingestion aborts.
    """

    source_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    timeframe: Timeframe
    root_path: Path
    glob_pattern: str = Field(min_length=1)
    file_format: FileFormat
    columns: ColumnMappingConfig
    timestamp_format: str = Field(min_length=1)
    timestamp_policy: TimestampPolicy
    declared_zone: TimeZoneName | None = None
    declared_offset_minutes: int | None = None
    dst_policy: DstPolicy = DstPolicy.STRICT_RAISE
    timestamps_are_bar_open: bool = True
    csv_separator: str = ","
    has_header: bool = True
    recursive_glob: bool = False
    max_reject_ratio: StrictDecimal = Decimal("0.001")

    @model_validator(mode="after")
    def _policy_requirements(self) -> DataSourceConfig:
        """Require exactly the timezone field the policy needs.

        Returns:
            The validated model.

        Raises:
            ValueError: If the declared zone or offset is missing or superfluous.
        """
        policy = self.timestamp_policy
        if policy is TimestampPolicy.DECLARED_ZONE and self.declared_zone is None:
            raise ValueError("timestamp_policy DECLARED_ZONE requires declared_zone")
        if (
            policy is TimestampPolicy.DECLARED_FIXED_OFFSET
            and self.declared_offset_minutes is None
        ):
            raise ValueError(
                "timestamp_policy DECLARED_FIXED_OFFSET requires declared_offset_minutes"
            )
        if policy is TimestampPolicy.EMBEDDED_OFFSET and (
            self.declared_zone is not None or self.declared_offset_minutes is not None
        ):
            raise ValueError("EMBEDDED_OFFSET sources must not declare a zone or offset")
        if not 0 <= self.max_reject_ratio <= 1:
            raise ValueError("max_reject_ratio must be between 0 and 1")
        return self

    def to_spec(self) -> SourceSpec:
        """Build the Layer 0 source description.

        Returns:
            A :class:`SourceSpec`. ``max_reject_ratio`` crosses to ``float`` here because
            it is a tolerance, not money; it never participates in a hash as a float.
        """
        return SourceSpec(
            source_id=SourceId(self.source_id),
            instrument_id=InstrumentId(self.instrument_id),
            timeframe=self.timeframe,
            root_path=self.root_path,
            glob_pattern=self.glob_pattern,
            file_format=self.file_format,
            columns=self.columns.to_mapping(),
            timestamp_format=self.timestamp_format,
            timestamp_policy=self.timestamp_policy,
            declared_zone=self.declared_zone,
            declared_offset=(
                timedelta(minutes=self.declared_offset_minutes)
                if self.declared_offset_minutes is not None
                else None
            ),
            dst_policy=self.dst_policy,
            timestamps_are_bar_open=self.timestamps_are_bar_open,
            csv_separator=self.csv_separator,
            has_header=self.has_header,
            recursive_glob=self.recursive_glob,
            max_reject_ratio=float(self.max_reject_ratio),
        )
