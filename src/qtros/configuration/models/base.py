"""Root config model, provenance markers, and the float-rejecting Decimal (ARCH-002 §4.1).

Two decisions here carry the whole configuration layer:

**``extra="forbid"``.** A typo'd key that is silently ignored produces a backtest that runs
cleanly with the wrong parameters — the most expensive possible failure, because the
number it produces is plausible.

**``StrictDecimal`` rejects floats (D-2).** PyYAML parses ``0.1`` as a binary float before
Pydantic sees it, and ``Decimal(0.1)`` is ``0.1000000000000000055511151231257827…``.
Precision lost at parse time cannot be recovered downstream, and it would defeat the exact
reconciliation that Directive 6 rests on. Decimal fields therefore accept ``str``, ``int``,
and ``Decimal`` only; a YAML author writes ``tick_size: "0.001"``.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, PositiveInt

PROVENANCE_KEY: str = "provenance"
"""Key under ``Field(json_schema_extra=...)`` that carries a field's provenance class."""


class ProvenanceClass(StrEnum):
    """Which hash partition a configuration field belongs to (ARCH-002 D-1)."""

    RUN_AFFECTING = "run"
    """Changes which fills occur or what they cost. Default for unmarked fields, because a
    forgotten marker should cause harmless hash churn rather than a silent collision."""

    REPORT_AFFECTING = "report"
    """Changes how a fixed ledger is scored. Metrics can be recomputed without a re-run."""

    OPERATIONAL = "operational"
    """Excluded from all hashing. Log level, paths, worker count."""


def provenance(kind: ProvenanceClass) -> dict[str, Any]:
    """Return ``Field`` keyword arguments that mark a field's provenance class.

    Args:
        kind: The partition the field belongs to.

    Returns:
        Keyword arguments to splat into :func:`pydantic.Field`.

    Example:
        >>> from pydantic import Field
        >>> class Example(QtrosConfig):
        ...     label: str = Field(default="x", **provenance(ProvenanceClass.OPERATIONAL))
    """
    return {"json_schema_extra": {PROVENANCE_KEY: kind.value}}


def reject_float_input(value: object) -> object:
    """Refuse a float where a Decimal is expected (D-2).

    Args:
        value: Raw input before Decimal validation.

    Returns:
        The value unchanged when it is not a float.

    Raises:
        ValueError: If the value is a float. Booleans are also refused: ``True`` is an
            ``int`` subclass and would otherwise validate as ``Decimal("1")``.
    """
    if isinstance(value, float):
        raise ValueError(
            "float input is not accepted for a Decimal field; quote the value in YAML "
            '(e.g. "0.001") so its exact decimal representation is preserved'
        )
    if isinstance(value, bool):
        raise ValueError("boolean input is not accepted for a Decimal field")
    return value


StrictDecimal = Annotated[Decimal, BeforeValidator(reject_float_input)]
"""A Decimal field that accepts ``str``, ``int``, or ``Decimal`` and refuses ``float``."""


def _validate_zone(name: str) -> str:
    """Confirm a timezone name resolves in the system zoneinfo database.

    Args:
        name: IANA zone name.

    Returns:
        The name unchanged.

    Raises:
        ValueError: If the zone is unknown.
    """
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown IANA timezone {name!r}") from exc
    return name


TimeZoneName = Annotated[str, AfterValidator(_validate_zone)]
"""An IANA zone name validated against ``zoneinfo`` at parse time."""


class QtrosConfig(BaseModel):
    """Root of every configuration model.

    Frozen, closed to unknown keys, and validating defaults. Subclasses may set
    ``__provenance__`` to assign every unmarked field in the model to a partition; a field
    marked with :func:`provenance` overrides the model default.

    Attributes:
        schema_version: Migration hook (ARCH-002 §4.9). Every config carries it so a
            config written this year still parses in year three.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_default=True,
        use_enum_values=False,
        arbitrary_types_allowed=False,
        strict=False,
    )

    __provenance__: ClassVar[ProvenanceClass | None] = None

    schema_version: PositiveInt = 1
