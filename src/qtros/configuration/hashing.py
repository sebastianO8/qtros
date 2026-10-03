"""Canonicalisation, partitioning, and hashing of resolved configuration (ARCH-002 §5).

Canonical form: keys sorted, no insignificant whitespace, UTF-8 without ASCII escaping,
``Decimal`` as its exact string, ``datetime`` as RFC 3339 in UTC with explicit offset,
sets as sorted arrays, ``None`` omitted, enums as their values, **floats forbidden**.
Digest: SHA-256, lowercase hex; the first 16 characters serve as a display identifier.

Floats are forbidden rather than rounded because float repr has varied across platforms
and interpreter versions. A hash that differs between the PI's laptop and a future machine
makes Directive 10 worse than useless: reproduction would *look* broken when it is not.

Partitioning follows D-1. A leaf's partition is the nearest explicit marker walking up from
the field: a :func:`provenance` marker on the field, else the enclosing model's
``__provenance__``, else the partition inherited from the parent field, else
``RUN_AFFECTING``. Defaulting to the strictest class means a forgotten marker causes
harmless churn rather than a silent collision between materially different runs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import PurePath
from typing import Any

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from qtros.configuration.models.base import PROVENANCE_KEY, ProvenanceClass, QtrosConfig
from qtros.domain.errors import ConfigurationError

HASH_PREFIX_LENGTH: int = 16
"""Characters of the full digest used as a display identifier."""

_CANON_VERSION: bytes = b"QTROS-CONFIG-CANON-V1\n"


class FloatInHashError(ConfigurationError):
    """A float reached the canonical serialiser."""


# --------------------------------------------------------------------------------------
# Canonical JSON
# --------------------------------------------------------------------------------------


def _canonical_value(value: object, *, path: str) -> object:
    """Convert one value into its canonical JSON-compatible form.

    Args:
        value: Any value found in a resolved configuration.
        path: Dotted location, for error messages.

    Returns:
        A JSON-serialisable value.

    Raises:
        FloatInHashError: If a float is encountered anywhere.
        ConfigurationError: If a type has no canonical form.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        raise FloatInHashError(
            "float values are forbidden in hash-relevant configuration", path=path
        )
    if value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return _canonical_value(value.value, path=path)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ConfigurationError("naive datetime cannot be canonicalised", path=path)
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return {"seconds": int(value.total_seconds())}
    if isinstance(value, PurePath):
        return value.as_posix()
    if isinstance(value, (frozenset, set)):
        items = [_canonical_value(item, path=path) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, Mapping):
        out: dict[str, object] = {}
        for key, item in value.items():
            if item is None:
                continue
            out[str(key)] = _canonical_value(item, path=f"{path}.{key}")
        return out
    if isinstance(value, BaseModel):
        return _canonical_value(_model_to_mapping(value), path=path)
    raise ConfigurationError(
        f"no canonical form for {type(value).__name__}", path=path
    )


def _model_to_mapping(model: BaseModel) -> dict[str, object]:
    """Return a model's fields as a plain mapping without triggering serialisation."""
    return {name: getattr(model, name) for name in type(model).model_fields}


def canonical_json(value: object) -> str:
    """Serialise a value into the canonical hash form.

    Args:
        value: A config model, mapping, or partition.

    Returns:
        Compact, key-sorted, UTF-8 JSON text.
    """
    return json.dumps(
        _canonical_value(value, path="$"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def digest(text: str) -> str:
    """Return the full lowercase hex SHA-256 of canonical text.

    Args:
        text: Canonical JSON.

    Returns:
        64-character hex digest.
    """
    hasher = hashlib.sha256()
    hasher.update(_CANON_VERSION)
    hasher.update(text.encode("utf-8"))
    return hasher.hexdigest()


# --------------------------------------------------------------------------------------
# Partitioning
# --------------------------------------------------------------------------------------


def _field_marker(info: FieldInfo) -> ProvenanceClass | None:
    """Return a field's explicit provenance marker, if any."""
    extra = info.json_schema_extra
    if isinstance(extra, dict) and PROVENANCE_KEY in extra:
        return ProvenanceClass(str(extra[PROVENANCE_KEY]))
    return None


def _model_default(model: BaseModel) -> ProvenanceClass | None:
    """Return a model's class-level provenance default, if any."""
    if isinstance(model, QtrosConfig):
        return type(model).__provenance__
    return None


Partition = dict[ProvenanceClass, dict[str, Any]]


def partition(model: BaseModel) -> Partition:
    """Split a resolved configuration into its three provenance partitions.

    Args:
        model: Root configuration model.

    Returns:
        A mapping from partition to a nested dict mirroring the model's structure,
        containing only the leaves assigned to that partition. Empty partitions are
        present as empty dicts.
    """
    out: Partition = {kind: {} for kind in ProvenanceClass}
    _walk(model, inherited=ProvenanceClass.RUN_AFFECTING, out=out, prefix=())
    return out


def _assign(out: Partition, kind: ProvenanceClass, prefix: tuple[str, ...], value: object) -> None:
    """Place a leaf value at its nested position within one partition."""
    target: dict[str, Any] = out[kind]
    for key in prefix[:-1]:
        target = target.setdefault(key, {})
    target[prefix[-1]] = value


def _walk(
    model: BaseModel,
    *,
    inherited: ProvenanceClass,
    out: Partition,
    prefix: tuple[str, ...],
) -> None:
    """Recursively assign every leaf of a model to a partition."""
    model_kind = _model_default(model) or inherited
    for name, info in type(model).model_fields.items():
        value = getattr(model, name)
        kind = _field_marker(info) or model_kind
        if isinstance(value, BaseModel):
            _walk(value, inherited=kind, out=out, prefix=(*prefix, name))
        elif value is None:
            continue
        else:
            _assign(out, kind, (*prefix, name), value)


def field_provenance(model: BaseModel) -> dict[str, ProvenanceClass]:
    """Return the resolved partition of every leaf field, keyed by dotted path.

    Exists so a schema test can assert that every field in every model resolves to
    exactly one class (ARCH-002 §5).

    Args:
        model: Root configuration model.

    Returns:
        Dotted path to partition.
    """
    result: dict[str, ProvenanceClass] = {}

    def visit(node: BaseModel, inherited: ProvenanceClass, prefix: str) -> None:
        node_kind = _model_default(node) or inherited
        for name, info in type(node).model_fields.items():
            kind = _field_marker(info) or node_kind
            value = getattr(node, name)
            dotted = f"{prefix}.{name}" if prefix else name
            if isinstance(value, BaseModel):
                visit(value, kind, dotted)
            else:
                result[dotted] = kind

    visit(model, ProvenanceClass.RUN_AFFECTING, "")
    return result


# --------------------------------------------------------------------------------------
# Hashes
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConfigHashes:
    """The three Directive 10 identities (D-1).

    Attributes:
        run_hash: Digest of the run-affecting partition.
        report_hash: Digest of the report-affecting partition.
        config_hash: Digest of ``run_hash || report_hash``. Directive 10's required value.
    """

    run_hash: str
    report_hash: str
    config_hash: str

    @property
    def run_id_fragment(self) -> str:
        """Return the short run hash used in ``run_id`` (ARCH-002 §6.1)."""
        return self.run_hash[:8]

    @property
    def display(self) -> str:
        """Return the short config hash for logs."""
        return self.config_hash[:HASH_PREFIX_LENGTH]


def compute_hashes(model: BaseModel) -> ConfigHashes:
    """Partition, canonicalise, and hash a resolved configuration.

    Args:
        model: Root configuration model.

    Returns:
        The three hashes. The operational partition is never hashed.
    """
    parts = partition(model)
    run_hash = digest(canonical_json(parts[ProvenanceClass.RUN_AFFECTING]))
    report_hash = digest(canonical_json(parts[ProvenanceClass.REPORT_AFFECTING]))
    config_hash = hashlib.sha256(f"{run_hash}||{report_hash}".encode("ascii")).hexdigest()
    return ConfigHashes(run_hash=run_hash, report_hash=report_hash, config_hash=config_hash)
