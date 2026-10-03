"""Lossless conversion of run artifacts into JSON-compatible structures.

``Decimal`` is emitted as its exact string, never as a float, so that a figure read back
from ``performance.json`` equals the figure that was computed. Floats are permitted only
because some diagnostics are legitimately float-valued — feature values in a trade's
``rationale``, the lag audit's tolerance — and are emitted as JSON numbers via Python's
shortest round-trip repr. No monetary or price figure is ever a float; those are
``Decimal`` by construction in ``domain/`` and arrive here as strings.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import PurePath
from typing import Any

from pydantic import BaseModel


def to_jsonable(value: object) -> Any:
    """Convert a value into something :func:`json.dumps` accepts without loss.

    Args:
        value: Dataclass, model, mapping, sequence, or scalar.

    Returns:
        A JSON-compatible structure. ``None`` is preserved (unlike the hash canon),
        because an artifact reader needs to see that a metric was undefined.
    """
    if value is None or isinstance(value, (bool, int, str, float)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return {"seconds": int(value.total_seconds())}
    if isinstance(value, PurePath):
        return value.as_posix()
    if isinstance(value, (frozenset, set)):
        return sorted(to_jsonable(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, BaseModel):
        return {name: to_jsonable(getattr(value, name)) for name in type(value).model_fields}
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in fields(value)}
    raise TypeError(f"no artifact serialisation for {type(value).__name__}")
