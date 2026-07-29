"""Deterministic content fingerprinting of bar datasets (Directive 10).

A fingerprint answers one question: *is this exactly the data that produced that
result?* For the answer to be worth anything it must be stable across machines, Python
versions, Polars versions, DuckDB versions, and file layouts. A hash that drifts is
worse than no hash at all, because a spurious mismatch reads as a data change and sends
the researcher hunting for a problem that does not exist.

Stability is achieved by hashing a canonical binary form rather than any library's
serialisation:

    * Timestamps are hashed as int64 microseconds since the Unix epoch, taken from the
      column's physical representation. That representation is UTC-based, so the hash is
      independent of any display timezone.
    * Prices and volume are hashed as little-endian IEEE-754 doubles. Bit patterns are
      exact and portable; text formatting of floats is neither, which is why the
      configuration hashing scheme forbids floats outright while this one embraces them.
      The difference is that config values are authored by a human and can be Decimals,
      whereas market data arrives as doubles and must be hashed as what it is.
    * Negative zero is normalised to positive zero, since the two are numerically equal
      but bitwise distinct.
    * NaN is rejected rather than hashed. NaN payload bits are not standardised, so a
      frame containing NaN cannot be fingerprinted deterministically. Validated data
      never contains NaN, so this raising path is a tripwire on the validation layer.
    * Nulls are hashed as a separate mask byte plus a zeroed value, so a null and a
      genuine zero are distinguishable.

Source filenames deliberately do **not** participate in the hash. The same bars
re-delivered under a different filename are the same bars, and a fingerprint that
disagreed would defeat the purpose.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Final

import numpy as np
import polars as pl
import structlog

from qtros.domain.data_contracts import DatasetFingerprint, sorted_source_files
from qtros.domain.enums import Timeframe
from qtros.domain.errors import FingerprintError, SchemaContractError
from qtros.domain.identifiers import InstrumentId

_LOGGER: Final = structlog.get_logger(__name__)

FINGERPRINT_ALGORITHM_VERSION: Final[int] = 1
"""Increment on any change to canonicalisation.

Recorded on every fingerprint so that a future scheme change produces an explicit
refusal to compare rather than a silent apparent mismatch.
"""

_MAGIC: Final[bytes] = b"QTROS-DATASET-FINGERPRINT-V1\n"

TS_COLUMN: Final[str] = "ts_utc"

FINGERPRINT_FLOAT_COLUMNS: Final[tuple[str, ...]] = ("open", "high", "low", "close", "volume")
FINGERPRINT_BOOL_COLUMNS: Final[tuple[str, ...]] = ("is_synthetic",)
FINGERPRINT_COLUMNS: Final[tuple[str, ...]] = (
    TS_COLUMN,
    *FINGERPRINT_FLOAT_COLUMNS,
    *FINGERPRINT_BOOL_COLUMNS,
)

_CHUNK_ROWS: Final[int] = 250_000
"""Rows hashed per pass. Bounds peak memory on multi-year minute data without making
the per-chunk overhead significant."""


def compute_fingerprint(
    frame: pl.DataFrame,
    *,
    instrument_id: InstrumentId,
    timeframe: Timeframe,
    source_files: Sequence[Path] | None = None,
) -> DatasetFingerprint:
    """Compute the deterministic content hash of a bar frame.

    The frame is sorted by timestamp before hashing, so two frames holding identical
    bars in different row orders fingerprint identically. Row order is a storage
    accident; bar content is not.

    Args:
        frame: Bars to fingerprint. Must carry every column in
            :data:`FINGERPRINT_COLUMNS`.
        instrument_id: Instrument the bars describe. Bound into the hash, so identical
            price series for two instruments do not collide.
        timeframe: Interval the bars describe. Also bound into the hash.
        source_files: Contributing raw files, recorded for traceability only. These do
            not affect the hash.

    Returns:
        A fully populated fingerprint.

    Raises:
        SchemaContractError: If a required column is missing.
        FingerprintError: If the frame contains NaN, or a column has an unexpected
            dtype that would make the binary form ambiguous.
    """
    missing: tuple[str, ...] = tuple(
        name for name in FINGERPRINT_COLUMNS if name not in frame.columns
    )
    if missing:
        raise SchemaContractError(
            "Frame is missing columns required for fingerprinting",
            missing=list(missing),
            present=frame.columns,
        )

    digest = hashlib.sha256()
    digest.update(_MAGIC)
    digest.update(
        (
            f"algorithm_version={FINGERPRINT_ALGORITHM_VERSION}\n"
            f"instrument_id={instrument_id}\n"
            f"timeframe={timeframe}\n"
            f"columns={','.join(FINGERPRINT_COLUMNS)}\n"
            f"row_count={frame.height}\n"
        ).encode("utf-8")
    )

    if frame.height == 0:
        return DatasetFingerprint(
            content_hash=digest.hexdigest(),
            algorithm_version=FINGERPRINT_ALGORITHM_VERSION,
            row_count=0,
            first_ts_utc=None,
            last_ts_utc=None,
            instrument_id=instrument_id,
            timeframe=timeframe,
            column_order=FINGERPRINT_COLUMNS,
            source_files=sorted_source_files(source_files or ()),
        )

    ordered: pl.DataFrame = frame.select(FINGERPRINT_COLUMNS).sort(TS_COLUMN)

    for offset in range(0, ordered.height, _CHUNK_ROWS):
        chunk: pl.DataFrame = ordered.slice(offset, _CHUNK_ROWS)
        digest.update(_encode_timestamps(chunk.get_column(TS_COLUMN)))
        for name in FINGERPRINT_FLOAT_COLUMNS:
            mask_bytes, value_bytes = _encode_float_column(chunk.get_column(name), name)
            digest.update(mask_bytes)
            digest.update(value_bytes)
        for name in FINGERPRINT_BOOL_COLUMNS:
            digest.update(_encode_bool_column(chunk.get_column(name)))

    timestamps: pl.Series = ordered.get_column(TS_COLUMN)
    first_ts: datetime = timestamps[0]
    last_ts: datetime = timestamps[-1]
    content_hash: str = digest.hexdigest()

    _LOGGER.info(
        "fingerprint.computed",
        instrument_id=str(instrument_id),
        timeframe=str(timeframe),
        rows=ordered.height,
        content_hash=content_hash[:12],
        algorithm_version=FINGERPRINT_ALGORITHM_VERSION,
    )

    return DatasetFingerprint(
        content_hash=content_hash,
        algorithm_version=FINGERPRINT_ALGORITHM_VERSION,
        row_count=ordered.height,
        first_ts_utc=first_ts,
        last_ts_utc=last_ts,
        instrument_id=instrument_id,
        timeframe=timeframe,
        column_order=FINGERPRINT_COLUMNS,
        source_files=sorted_source_files(source_files or ()),
    )


# --------------------------------------------------------------------------------------
# Canonical binary encoding
# --------------------------------------------------------------------------------------


def _encode_timestamps(series: pl.Series) -> bytes:
    """Encode a timestamp column as little-endian int64 epoch microseconds.

    Args:
        series: Datetime column, expected to be timezone-aware UTC.

    Returns:
        Contiguous little-endian int64 bytes.

    Raises:
        FingerprintError: If the column is not a Datetime, or contains nulls. A null
            timestamp is not a data quality issue to be recorded — it is a bar with no
            position in time, which cannot be hashed meaningfully.
    """
    if series.dtype.base_type() is not pl.Datetime:
        raise FingerprintError(
            "Timestamp column has an unexpected dtype",
            column=series.name,
            dtype=str(series.dtype),
        )
    if series.null_count() > 0:
        raise FingerprintError(
            "Timestamp column contains nulls and cannot be fingerprinted",
            column=series.name,
            null_count=series.null_count(),
        )

    micros: pl.Series = series.dt.cast_time_unit("us").to_physical()
    array: np.ndarray = np.ascontiguousarray(micros.to_numpy(), dtype="<i8")
    return array.tobytes()


def _encode_float_column(series: pl.Series, name: str) -> tuple[bytes, bytes]:
    """Encode a float column as a null mask plus little-endian float64 values.

    Args:
        series: Numeric column to encode.
        name: Column name, used in error context.

    Returns:
        A pair of (null mask bytes, value bytes). The mask carries one byte per row so
        that a null and a genuine zero produce different digests.

    Raises:
        FingerprintError: If the column contains NaN, whose bit pattern is not portable.
    """
    # copy=True is load-bearing: Polars may hand back a read-only zero-copy view, and
    # the negative-zero normalisation below writes into this array.
    mask: np.ndarray = np.array(series.is_null().to_numpy(), dtype="u1", copy=True)
    values: np.ndarray = np.array(
        series.fill_null(0.0).cast(pl.Float64).to_numpy(), dtype="<f8", copy=True
    )

    if bool(np.isnan(values).any()):
        raise FingerprintError(
            "Column contains NaN and cannot be fingerprinted deterministically",
            column=name,
            nan_count=int(np.isnan(values).sum()),
        )

    # -0.0 and 0.0 are numerically equal but bitwise distinct; normalise to +0.0.
    values[values == 0.0] = 0.0
    return mask.tobytes(), values.tobytes()


def _encode_bool_column(series: pl.Series) -> bytes:
    """Encode a boolean column as one byte per row.

    Args:
        series: Boolean column to encode. Nulls are treated as ``False``, matching the
            warehouse schema where the column is ``NOT NULL DEFAULT FALSE``.

    Returns:
        Contiguous unsigned byte data.
    """
    array: np.ndarray = np.ascontiguousarray(
        series.fill_null(value=False).cast(pl.UInt8).to_numpy(), dtype="u1"
    )
    return array.tobytes()
