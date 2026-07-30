"""Transform scaffold and the Layer 0.5 half of DIR-001 enforcement.

Scope of this file, stated plainly
----------------------------------
DIR-001 has two halves, and only one of them is enforceable here.

**Enforceable in this file (the producer side).** Every feature reaching a strategy was
computed by a registered transform, with a declared name, version, parameter set, dependency
list, and lookback — and carries a provenance seal proving it. A column that no registered
transform produced cannot masquerade as a feature.

**Not enforceable in this file (the consumer side).** DIR-001 also prohibits a *strategy*
from computing ``ctx.bar.close - ctx.bar.open`` inline. That code lives in Layer 1. Under the
import contract in ``pyproject.toml``, Layer 0.5 sits below Layer 1 and cannot see, wrap, or
constrain it. Nothing written here prevents inline arithmetic in ``alpha/strategies/``.

Claiming otherwise would be worse than leaving the gap open, because a guard believed to
exist stops being watched. What this file does instead is supply the *precondition* for
enforcing the consumer side: a sealed provenance record (:class:`SealedFeatureSet`) that a
future ``alpha/base.py`` can require, so that a value a strategy derived itself is
distinguishable at runtime from one a transform produced. See ``DIR-001 ENFORCEMENT GAP``
below for the proposed mechanism.

Structural guarantees this file does provide
--------------------------------------------
:class:`TransformBase` is a template method. Subclasses implement ``_build``; the base owns
``apply`` and refuses to let a subclass override it. Every transform is therefore checked,
not merely expected to behave:

1. **Declared dependencies are complete.** ``self_check`` runs the transform against a probe
   frame containing *only* its declared dependencies. Reading an undeclared column raises
   there rather than silently working in production — where the DAG resolver would then order
   it wrongly and the cumulative warmup would be short.
2. **Declared outputs are exact.** Producing more, fewer, or differently-named columns than
   declared is refused. A mismatch breaks the resolver for everything downstream.
3. **Existing columns are immutable.** A transform may append; it may not modify or drop.
   Overwriting ``close`` would corrupt every downstream feature and every execution price at
   once, and would present as a market-data problem.
4. **Row count is invariant.** A transform that filters or aggregates would desynchronise the
   frame from the bar index the engine uses, silently shifting the T-1 boundary (ASS-002) for
   every subsequent bar. This is the check most worth having and the least obvious.
5. **Outputs are numeric.** A string or struct column cannot be compared to a price.
6. **No feature may shadow a Layer 0 column**, so a strategy can never be uncertain whether
   it is reading market data or a derived value.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, TypeVar

import polars as pl
import structlog

from qtros.domain.errors import ConfigurationError, SchemaContractError
from qtros.domain.feature_contracts import Causality, FeatureSpec, WarmupRequirement

_LOGGER: Final = structlog.get_logger(__name__)

# --------------------------------------------------------------------------------------
# Layer 0 column contract
# --------------------------------------------------------------------------------------

BASE_COLUMNS: Final[frozenset[str]] = frozenset(
    {"ts_utc", "open", "high", "low", "close", "volume", "is_synthetic"}
)
"""Columns supplied by Layer 0. Readable by any transform, writable by none."""

_BASE_DTYPES: Final[Mapping[str, pl.DataType]] = {
    "ts_utc": pl.Datetime(time_unit="us", time_zone="UTC"),
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "close": pl.Float64(),
    "volume": pl.Float64(),
    "is_synthetic": pl.Boolean(),
}

_NUMERIC_BASE_TYPES: Final[frozenset[object]] = frozenset(
    {pl.Float32, pl.Float64, pl.Int8, pl.Int16, pl.Int32, pl.Int64, pl.UInt8, pl.Boolean}
)

TYPICAL_PRICE_EXPR: Final[pl.Expr] = (pl.col("high") + pl.col("low") + pl.col("close")) / 3.0
"""Typical price, the standard VWAP weighting input."""

PROBE_MARGIN_BARS: Final[int] = 8
"""Bars of probe history beyond a transform's declared lookback.

Enough for a rolling window to produce at least one non-null value, so the conformance check
exercises the real computation rather than only its null prologue.
"""


# --------------------------------------------------------------------------------------
# Provenance sealing — the precondition for consumer-side DIR-001 enforcement
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SealedFeature:
    """Provenance record for one feature column.

    Attributes:
        column: Column name as it appears in the frame.
        transform_name: Registry name of the producing transform.
        transform_version: Version of the producing transform's logic.
        spec_hash: Hash of the producing transform's full specification.
        warmup_bars: The transform's own declared lookback.
        causality: Whether the producer is fit for signal generation.
    """

    column: str
    transform_name: str
    transform_version: str
    spec_hash: str
    warmup_bars: int
    causality: Causality


@dataclass(frozen=True, slots=True)
class SealedFeatureSet:
    """The complete set of columns legitimately produced by registered transforms.

    This is the artefact that makes DIR-001 checkable rather than aspirational. A value a
    strategy derived for itself has no entry here; a value a transform produced does. Any
    consumer-side guard needs exactly that distinction, and nothing else can supply it.

    Attributes:
        features: Provenance record per column.
    """

    features: Mapping[str, SealedFeature]

    def __contains__(self, column: object) -> bool:
        """Return whether a column was produced by a registered transform."""
        return isinstance(column, str) and column in self.features

    def require(self, column: str) -> SealedFeature:
        """Return the provenance record for a column.

        Args:
            column: Column name.

        Returns:
            The sealed record.

        Raises:
            SchemaContractError: If the column carries no provenance. Raised rather than
                returning ``None``, because a missing seal means the value's origin is
                unknown, and an unknown origin is exactly what DIR-001 prohibits relying on.
        """
        if column not in self.features:
            raise SchemaContractError(
                "Column has no transform provenance; under DIR-001 a strategy may only "
                "read features produced by a registered Layer 0.5 transform",
                column=column,
                sealed_columns=sorted(self.features),
            )
        return self.features[column]

    def verify_frame_columns(self, columns: Iterable[str]) -> None:
        """Assert every non-base column in a frame carries provenance.

        Closes the hole where a column is added to a feature frame between the pipeline and
        the engine — by a notebook, a debugging session, or a well-meaning helper. Such a
        column would be indistinguishable from a real feature at the context boundary.

        Args:
            columns: Column names present in the frame.

        Raises:
            SchemaContractError: If an unsealed, non-base column is present.
        """
        unsealed: tuple[str, ...] = tuple(
            column
            for column in columns
            if column not in BASE_COLUMNS and column not in self.features
        )
        if unsealed:
            raise SchemaContractError(
                "Feature frame contains columns produced by no registered transform",
                unsealed_columns=list(unsealed),
                sealed_columns=sorted(self.features),
            )

    @property
    def non_causal_columns(self) -> tuple[str, ...]:
        """Return sealed columns produced by non-causal control transforms."""
        return tuple(
            column
            for column, record in sorted(self.features.items())
            if record.causality is not Causality.CAUSAL
        )


def seal_features(transforms: Sequence[TransformBase]) -> SealedFeatureSet:
    """Build the provenance record for a resolved transform set.

    Args:
        transforms: Transforms in execution order.

    Returns:
        The sealed feature set.

    Raises:
        ConfigurationError: If two transforms claim the same output column. The pipeline's
            DAG resolver also detects this; checking here means the seal cannot be built from
            an ambiguous set even if it is constructed outside the pipeline.
    """
    records: dict[str, SealedFeature] = {}
    for transform in transforms:
        spec: FeatureSpec = transform.spec
        for column in spec.output_columns:
            if column in records:
                raise ConfigurationError(
                    "Two transforms claim the same output column; provenance would be "
                    "ambiguous",
                    column=column,
                    first=records[column].transform_name,
                    second=spec.name,
                )
            records[column] = SealedFeature(
                column=column,
                transform_name=spec.name,
                transform_version=spec.version,
                spec_hash=spec.spec_hash(),
                warmup_bars=transform.warmup.bars,
                causality=spec.causality,
            )
    return SealedFeatureSet(features=records)


# --------------------------------------------------------------------------------------
# Transform scaffold
# --------------------------------------------------------------------------------------


class TransformBase(ABC):
    """Base class every feature transform must derive from.

    Subclasses implement :meth:`_build` and declare :attr:`spec`. They must not override
    :meth:`apply`: it is the enforcement point, and a subclass that bypassed it would be
    unchecked while appearing identical to one that is not.

    Subclasses must be immutable after construction, perform no I/O, and read no clock. Those
    three are conventions rather than checks — Python cannot prevent them — but a transform
    that violates any of them will fail :meth:`self_check`'s determinism probe, which runs the
    build twice and compares.
    """

    _RESERVED_METHODS: Final[frozenset[str]] = frozenset({"apply", "self_check"})

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Reject subclasses that override the enforcement path or omit the build hook.

        Raises:
            TypeError: If the subclass overrides ``apply`` or ``self_check``, or defines
                neither ``_build`` nor an inherited one. Raised at class-definition time, so
                the failure arrives at import rather than mid-run.
        """
        super().__init_subclass__(**kwargs)

        for reserved in TransformBase._RESERVED_METHODS:
            if reserved in cls.__dict__:
                raise TypeError(
                    f"{cls.__name__} overrides TransformBase.{reserved}, which is the "
                    f"DIR-001 enforcement path. Implement _build instead."
                )
        if not callable(getattr(cls, "_build", None)):
            raise TypeError(f"{cls.__name__} must implement _build(frame) -> pl.LazyFrame")

    # -- subclass contract -------------------------------------------------------------

    @property
    @abstractmethod
    def spec(self) -> FeatureSpec:
        """Return this transform's identity, dependencies, and declared outputs."""

    @property
    def warmup(self) -> WarmupRequirement:
        """Return the history this transform needs before its output is valid.

        Defaults to none. Any transform with a rolling window, a shift, or a cumulative
        accumulation must override this, or the pipeline will hand a strategy values computed
        from a partial window and nothing will indicate it.
        """
        return WarmupRequirement()

    @abstractmethod
    def _build(self, frame: pl.LazyFrame) -> pl.LazyFrame:
        """Append this transform's declared output columns.

        Args:
            frame: Lazy frame carrying at least this transform's declared dependencies.

        Returns:
            The frame with :attr:`FeatureSpec.output_columns` appended. Must not modify or
            drop an existing column, and must not change the row count.

        Note:
            Must be causal: the value at row *t* may depend only on rows at or before *t*.
            The base class cannot verify causality — that is the prefix-invariance audit's
            job, and it verifies it mechanically rather than by inspection.
        """

    # -- enforcement -------------------------------------------------------------------

    def apply(self, frame: pl.LazyFrame) -> pl.LazyFrame:
        """Run the transform and validate its output contract.

        Args:
            frame: Accumulated lazy frame from upstream transforms.

        Returns:
            The frame with this transform's outputs appended.

        Raises:
            SchemaContractError: If a declared dependency is absent, if the output columns
                do not match the declaration exactly, if an existing column was modified or
                dropped, or if an output is not numeric.
        """
        spec: FeatureSpec = self.spec
        before: pl.Schema = frame.collect_schema()
        before_names: tuple[str, ...] = tuple(before.names())

        missing: tuple[str, ...] = tuple(
            column for column in spec.depends_on if column not in before_names
        )
        if missing:
            raise SchemaContractError(
                "Transform's declared dependencies are not present in the frame",
                transform=spec.name,
                missing=list(missing),
                available=list(before_names),
            )

        try:
            produced: pl.LazyFrame = self._build(frame)
        except Exception as exc:  # noqa: BLE001 - polars raises several unrelated types
            raise SchemaContractError(
                "Transform failed while building its expression",
                transform=spec.name,
                version=spec.version,
                cause=str(exc),
            ) from exc

        self._validate_schema_delta(before, produced.collect_schema(), spec)
        return produced

    def self_check(self, *, probe_rows: int | None = None) -> None:
        """Verify the transform against a synthetic probe before any real data is used.

        Four properties are established here that :meth:`apply` cannot establish on a real
        frame:

        * **Dependency completeness.** The probe contains only declared dependencies, so an
          undeclared read fails here. In production the column would be present and the read
          would succeed — leaving the DAG resolver to order the transform wrongly and the
          cumulative warmup short, with no symptom.
        * **Row-count invariance.** A filtering or aggregating transform desynchronises the
          frame from the engine's bar index, shifting the T-1 boundary for every subsequent
          bar (ASS-002).
        * **Determinism.** The build runs twice and the outputs are compared. A transform
          reading a clock, a random source, or mutable instance state fails.
        * **Output realisability.** The expression is actually collected, so an operation that
          is valid to construct but invalid to evaluate fails at pipeline construction rather
          than twenty minutes into a multi-year run.

        Args:
            probe_rows: Rows in the probe frame. Defaults to the declared lookback plus
                :data:`PROBE_MARGIN_BARS`.

        Raises:
            SchemaContractError: If any property fails.
        """
        spec: FeatureSpec = self.spec
        rows: int = probe_rows if probe_rows is not None else self.warmup.bars + PROBE_MARGIN_BARS
        probe: pl.DataFrame = self._probe_frame(spec.depends_on, rows)

        try:
            first: pl.DataFrame = self.apply(probe.lazy()).collect()
            second: pl.DataFrame = self.apply(probe.lazy()).collect()
        except SchemaContractError:
            raise
        except Exception as exc:  # noqa: BLE001 - polars raises several unrelated types
            raise SchemaContractError(
                "Transform failed its conformance check; it most likely reads a column it "
                "does not declare in depends_on",
                transform=spec.name,
                declared_dependencies=list(spec.depends_on),
                probe_rows=rows,
                cause=str(exc),
            ) from exc

        if first.height != probe.height:
            raise SchemaContractError(
                "Transform changed the row count; a transform may append columns but must "
                "never filter or aggregate, or every downstream bar index shifts",
                transform=spec.name,
                rows_in=probe.height,
                rows_out=first.height,
            )

        for column in spec.output_columns:
            if not first.get_column(column).equals(
                second.get_column(column), null_equal=True
            ):
                raise SchemaContractError(
                    "Transform is not deterministic; two identical builds disagreed. A "
                    "transform must not read a clock, a random source, or mutable state",
                    transform=spec.name,
                    column=column,
                )

        _LOGGER.debug(
            "features.transform_conformant",
            transform=spec.name,
            version=spec.version,
            outputs=list(spec.output_columns),
            declared_dependencies=list(spec.depends_on),
            warmup_bars=self.warmup.bars,
            probe_rows=rows,
        )

    # -- internals ---------------------------------------------------------------------

    @staticmethod
    def _validate_schema_delta(
        before: pl.Schema,
        after: pl.Schema,
        spec: FeatureSpec,
    ) -> None:
        """Assert the transform appended exactly its declared outputs and nothing else.

        Args:
            before: Schema before the build.
            after: Schema after the build.
            spec: The transform's specification.

        Raises:
            SchemaContractError: On a dropped column, a mutated dtype, or an output set that
                does not match the declaration.
        """
        before_names: tuple[str, ...] = tuple(before.names())
        after_names: tuple[str, ...] = tuple(after.names())

        dropped: tuple[str, ...] = tuple(n for n in before_names if n not in after_names)
        if dropped:
            raise SchemaContractError(
                "Transform dropped existing columns; transforms append only",
                transform=spec.name,
                dropped=list(dropped),
            )

        mutated: tuple[str, ...] = tuple(
            n for n in before_names if after[n] != before[n]
        )
        if mutated:
            raise SchemaContractError(
                "Transform changed the dtype of an existing column",
                transform=spec.name,
                mutated=list(mutated),
            )

        added: tuple[str, ...] = tuple(n for n in after_names if n not in before_names)
        if added != tuple(spec.output_columns):
            raise SchemaContractError(
                "Transform's actual outputs do not match its declared output_columns; the "
                "DAG resolver and the warmup plan are both built from the declaration",
                transform=spec.name,
                declared=list(spec.output_columns),
                actual=list(added),
            )

        for column in added:
            if column in BASE_COLUMNS:
                raise SchemaContractError(
                    "Transform output would shadow a Layer 0 market data column",
                    transform=spec.name,
                    column=column,
                )
            if after[column].base_type() not in _NUMERIC_BASE_TYPES:
                raise SchemaContractError(
                    "Feature column is not numeric and cannot be compared to a price",
                    transform=spec.name,
                    column=column,
                    dtype=str(after[column]),
                )

    @staticmethod
    def _probe_frame(dependencies: Sequence[str], rows: int) -> pl.DataFrame:
        """Build a synthetic frame containing only the declared dependencies.

        Values are a gentle monotonic ramp rather than constants, so that a rolling standard
        deviation is non-zero and a divide-by-zero guard does not mask a genuine failure.

        Args:
            dependencies: Declared dependency column names.
            rows: Number of probe rows.

        Returns:
            A frame with exactly the declared dependencies.
        """
        start: datetime = datetime(2024, 1, 2, tzinfo=UTC)
        data: dict[str, object] = {}

        for column in dependencies:
            if column == "ts_utc":
                data[column] = [start + timedelta(minutes=5 * index) for index in range(rows)]
            elif column == "is_synthetic":
                data[column] = [False] * rows
            elif column == "volume":
                data[column] = [1000.0 + index for index in range(rows)]
            elif column in ("open", "high", "low", "close"):
                base = 100.0
                offset = {"open": 0.0, "high": 0.4, "low": -0.4, "close": 0.1}[column]
                data[column] = [base + index * 0.1 + offset for index in range(rows)]
            else:
                # An upstream feature column. Float64 with a ramp, matching what any
                # registered transform emits.
                data[column] = [1.0 + index * 0.25 for index in range(rows)]

        frame: pl.DataFrame = pl.DataFrame(data)
        if "ts_utc" in frame.columns and frame.schema["ts_utc"].time_zone is None:
            frame = frame.with_columns(pl.col("ts_utc").dt.replace_time_zone("UTC"))
        return frame


# --------------------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------------------

_REGISTRY: dict[str, type[TransformBase]] = {}

_T = TypeVar("_T", bound=type)


def register_transform(name: str) -> Callable[[_T], _T]:
    """Register a transform class under a stable name.

    Transforms resolve by registry name rather than by import path, for the same reason
    strategies do (ARCH-002 D-4): a config archived in ``results/`` must still resolve after
    a refactor, and a YAML file must never be able to name an arbitrary Python callable.

    Args:
        name: Registry key referenced from ``config/features/*.yaml``.

    Returns:
        A class decorator that registers and returns the class unchanged.

    Raises:
        ConfigurationError: If the name is already taken, or the class does not derive from
            :class:`TransformBase`. Refusing non-derived classes is what makes the DIR-001
            producer-side guarantee total rather than typical — a single unchecked transform
            would leave the seal unable to vouch for the columns it produced.
    """

    def decorator(cls: _T) -> _T:
        if not (isinstance(cls, type) and issubclass(cls, TransformBase)):
            raise ConfigurationError(
                "Feature transforms must derive from TransformBase so their output contract "
                "is enforced; rename the class's apply() to _build() and inherit from it",
                name=name,
                cls=getattr(cls, "__name__", repr(cls)),
            )
        if name in _REGISTRY:
            raise ConfigurationError(
                "Duplicate feature transform registration; silently shadowing one would let "
                "an archived config resolve to a different computation than it recorded",
                name=name,
                existing=_REGISTRY[name].__name__,
                attempted=cls.__name__,
            )
        _REGISTRY[name] = cls
        _LOGGER.debug("features.transform_registered", name=name, cls=cls.__name__)
        return cls

    return decorator


def build_transform(
    name: str,
    parameters: Mapping[str, object] | None = None,
    *,
    check: bool = True,
) -> TransformBase:
    """Construct a registered transform and verify its contract.

    Args:
        name: Registry key.
        parameters: Keyword arguments for the transform's constructor.
        check: Run :meth:`TransformBase.self_check` before returning. True by default: the
            check costs milliseconds against a probe frame and its absence costs a
            multi-year run.

    Returns:
        The constructed, conformance-checked transform.

    Raises:
        ConfigurationError: If the name is unknown or the parameters do not fit the
            constructor.
        SchemaContractError: If the transform fails its conformance check.
    """
    if name not in _REGISTRY:
        raise ConfigurationError(
            "Unknown feature transform",
            name=name,
            available=sorted(_REGISTRY),
        )
    try:
        transform: TransformBase = _REGISTRY[name](**dict(parameters or {}))
    except TypeError as exc:
        raise ConfigurationError(
            "Feature transform parameters do not match its constructor",
            name=name,
            parameters=dict(parameters or {}),
            cause=str(exc),
        ) from exc

    if check:
        transform.self_check()
    return transform


def registered_transforms() -> tuple[str, ...]:
    """Return every registered transform name, sorted."""
    return tuple(sorted(_REGISTRY))


# --------------------------------------------------------------------------------------
# Shared expression helpers
# --------------------------------------------------------------------------------------


def guard_column_name(column: str, transform_name: str) -> str:
    """Reject an output column that would shadow a Layer 0 column.

    Retained for use in transform constructors, so a bad name fails at construction with the
    transform's own context rather than later inside a schema diff.

    Args:
        column: Proposed output column name.
        transform_name: Transform proposing it.

    Returns:
        The column name, unchanged.

    Raises:
        ConfigurationError: If the name collides with a base column.
    """
    if column in BASE_COLUMNS:
        raise ConfigurationError(
            "Feature output column would shadow a base market data column",
            column=column,
            transform=transform_name,
        )
    return column


def safe_divide(numerator: pl.Expr, denominator: pl.Expr) -> pl.Expr:
    """Divide, yielding null rather than infinity when the denominator is zero.

    A null propagates visibly and is trimmed or handled explicitly downstream. An infinity
    silently poisons every rolling statistic it touches and surfaces much later as an
    inexplicable signal.

    Args:
        numerator: Dividend expression.
        denominator: Divisor expression.

    Returns:
        An expression yielding the quotient, or null where the divisor is zero or null.
    """
    return (
        pl.when((denominator.is_not_null()) & (denominator != 0.0))
        .then(numerator / denominator)
        .otherwise(None)
    )


def base_column_dtype(column: str) -> pl.DataType:
    """Return the declared dtype of a Layer 0 column.

    Args:
        column: Base column name.

    Returns:
        The expected dtype.

    Raises:
        ConfigurationError: If the column is not a Layer 0 column.
    """
    if column not in _BASE_DTYPES:
        raise ConfigurationError(
            "Not a Layer 0 base column",
            column=column,
            base_columns=sorted(BASE_COLUMNS),
        )
    return _BASE_DTYPES[column]
