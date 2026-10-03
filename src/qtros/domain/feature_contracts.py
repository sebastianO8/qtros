"""Domain contracts for the feature vector pipeline (Layer 0.5).

These live in ``domain/`` because ``IFeatureTransform`` is a protocol other layers type
against, and ``domain/`` may not import ``features/``.

The central distinction encoded here is between two properties that are easy to conflate
and must not be:

**Causality** — the value at bar *t* depends only on bars at or before *t*. This is a
property of the transform itself. It cannot be repaired downstream, and it is what the
prefix-invariance audit verifies mechanically.

**Availability lag** — the strategy evaluating bar *t* sees feature values as of bar
*t-1*. This is a property of the *engine*, applied once at the ``BarContext`` boundary.

A causal transform that is shifted twice produces stale features; a non-causal transform
that is shifted once still contains look-ahead. Keeping the two ideas separate in the
type system is what makes each one enforceable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

from qtros.domain.errors import SchemaContractError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import datetime

    import polars as pl

BASE_COLUMNS: Final[frozenset[str]] = frozenset(
    {"ts_utc", "open", "high", "low", "close", "volume", "is_synthetic"}
)
"""Columns supplied by Layer 0. Readable by any transform, writable by none.

Declared in ``domain/`` rather than in the transform package because the Layer 0 column
contract is a fact about the system's data model, not about the layer that happens to
consume it — and because :class:`SealedFeatureSet` below needs it, while ``domain/`` may
not import from ``features/``.
"""


class Causality(StrEnum):
    """Whether a transform may legitimately be used to generate signals."""

    CAUSAL = "CAUSAL"
    """Value at bar *t* uses only bars at or before *t*. The only kind permitted."""

    NON_CAUSAL_CONTROL = "NON_CAUSAL_CONTROL"
    """Deliberately look-ahead. Exists solely to prove the audit detects look-ahead.

    A detector that has never caught anything is not known to work. The pipeline refuses
    to run these unless explicitly permitted, so the control cannot reach a strategy.
    """


class LagPolicy(StrEnum):
    """Where the availability shift is applied.

    Getting this wrong in either direction is a silent, plausible-looking bug, which is
    why it is an explicit enum rather than a default buried in a transform.
    """

    DEFER_TO_ENGINE = "DEFER_TO_ENGINE"
    """Emit unshifted causal features. The engine applies the single T-1 shift when it
    builds each ``BarContext``. The Phase 1 default."""

    SHIFT_IN_PIPELINE = "SHIFT_IN_PIPELINE"
    """Apply the T-1 shift here. For feature-only research where no engine is involved.
    Frames produced this way are stamped, and the engine refuses to shift them again."""


@dataclass(frozen=True, slots=True)
class WarmupRequirement:
    """History a transform needs before its output is trustworthy.

    Attributes:
        bars: Bars of history required before the first valid output. Declared by the
            transform, never inferred from the data.
        session_column: Name of a session-index column this transform emits, when its
            output is only valid for a complete session. Anchored features such as VWAP
            are meaningless for a session the dataset joined halfway through.
    """

    bars: int = 0
    session_column: str | None = None

    def __post_init__(self) -> None:
        """Reject a negative lookback.

        Raises:
            ValueError: If ``bars`` is negative.
        """
        if self.bars < 0:
            raise ValueError(f"Warmup cannot be negative: {self.bars}")


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    """Identity of a configured feature transform.

    Two transforms with equal specs produce identical columns from identical input, which
    is what makes :meth:`spec_hash` usable as a cache key and as a component of the
    strategy configuration hash.

    Attributes:
        name: Registry name, e.g. ``atr``.
        version: Semantic version of the transform's logic. Bumped whenever the
            computation changes, so archived results remain attributable to the code
            that produced them.
        parameters: Configured parameters.
        output_columns: Columns this transform appends.
        depends_on: Columns this transform reads.
        causality: Whether the transform is fit for signal generation.
    """

    name: str
    version: str
    parameters: Mapping[str, int | str | bool]
    output_columns: tuple[str, ...]
    depends_on: tuple[str, ...]
    causality: Causality = Causality.CAUSAL

    def __post_init__(self) -> None:
        """Reject float parameters and empty output declarations.

        Raises:
            ValueError: If a parameter is a float, or no output column is declared.

        Note:
            Floats are refused for the same reason the configuration hashing scheme
            refuses them: float text representation is not portable, so a hash built
            from one is not stable across machines. Express fractional parameters as
            integers in basis points or as strings.
        """
        for key, value in self.parameters.items():
            if isinstance(value, float):
                raise ValueError(
                    f"Feature parameter {self.name}.{key} is a float; use an int or a "
                    "string so the spec hash stays portable"
                )
        if not self.output_columns:
            raise ValueError(f"Transform {self.name} declares no output columns")

    def spec_hash(self) -> str:
        """Return a stable hash of this specification.

        Returns:
            Lowercase hex SHA-256 of the canonical JSON form.
        """
        canonical: str = json.dumps(
            {
                "name": self.name,
                "version": self.version,
                "parameters": dict(sorted(self.parameters.items())),
                "output_columns": list(self.output_columns),
                "causality": str(self.causality),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def short_hash(self) -> str:
        """Return the first twelve characters of the spec hash."""
        return self.spec_hash()[:12]


@runtime_checkable
class IFeatureTransform(Protocol):
    """A single named, causal, side-effect-free column transformation.

    Implementations must be immutable after construction and must not perform I/O, read
    the wall clock, or depend on any state outside the frame they are given.
    """

    @property
    def spec(self) -> FeatureSpec:
        """Return this transform's identity."""
        ...

    @property
    def warmup(self) -> WarmupRequirement:
        """Return the history this transform needs before its output is valid."""
        ...

    def apply(self, frame: pl.LazyFrame) -> pl.LazyFrame:
        """Append this transform's output columns.

        Args:
            frame: Lazy frame carrying at least this transform's declared dependencies.

        Returns:
            The frame with :attr:`FeatureSpec.output_columns` appended. Existing columns
            are never modified or dropped.
        """
        ...


# --------------------------------------------------------------------------------------
# Pipeline outputs
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WarmupPlan:
    """Resolved warmup across a whole transform DAG.

    Attributes:
        total_bars: Bars trimmed from the head of the frame. This is the longest
            cumulative path through the DAG, not the largest single lookback — a
            50-period z-score of a 14-period ATR needs 64 bars, not 50.
        per_transform: Cumulative warmup for each transform, keyed by name.
        session_columns: Session-index columns whose first (incomplete) session is
            dropped.
    """

    total_bars: int
    per_transform: Mapping[str, int]
    session_columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LagAuditFinding:
    """One transform output that failed the prefix-invariance check.

    Attributes:
        transform_name: Transform that produced the column.
        column: Column whose value changed.
        probe_index: Row index at which the divergence was observed.
        full_frame_value: Value computed with the whole frame present.
        prefix_value: Value computed with everything after the probe removed.
        detail: Human-readable note.
    """

    transform_name: str
    column: str
    probe_index: int
    full_frame_value: str
    prefix_value: str
    detail: str


@dataclass(frozen=True, slots=True)
class LagAuditReport:
    """Outcome of a prefix-invariance audit — the structural enforcement of Directive 1.

    Attributes:
        passed: Whether every probed value was prefix-invariant.
        transforms_audited: Names of the transforms probed.
        probe_indices: Row indices probed.
        findings: Every divergence detected.
        columns_checked: Feature columns compared.
        tolerance: Absolute tolerance used. Zero means exact equality was required.
        rows_in_frame: Rows in the audited frame.
        audited_at_utc: When the audit ran.
    """

    passed: bool
    transforms_audited: tuple[str, ...]
    probe_indices: tuple[int, ...]
    findings: tuple[LagAuditFinding, ...]
    columns_checked: tuple[str, ...]
    tolerance: float
    rows_in_frame: int
    audited_at_utc: datetime

    @property
    def failing_columns(self) -> tuple[str, ...]:
        """Return the distinct columns that diverged, in first-seen order."""
        seen: list[str] = []
        for finding in self.findings:
            if finding.column not in seen:
                seen.append(finding.column)
        return tuple(seen)

    def summary(self) -> str:
        """Return a one-line verdict suitable for a log message or CLI output."""
        if self.passed:
            return (
                f"LAG AUDIT PASS — {len(self.columns_checked)} columns, "
                f"{len(self.probe_indices)} probes, {self.rows_in_frame} rows"
            )
        return (
            f"LAG AUDIT FAIL — look-ahead bias in {len(self.failing_columns)} column(s): "
            f"{', '.join(self.failing_columns)}"
        )


@dataclass(frozen=True, slots=True)
class FeatureFrame:
    """Bar data plus computed features, warmup-trimmed and audited.

    Attributes:
        frame: The computed frame. Warmup rows have already been removed, so a consumer
            never sees a partially-populated feature column.
        specs: Specifications of every transform applied, in execution order.
        warmup: The resolved warmup plan.
        rows_trimmed: Rows removed from the head.
        lag_policy: Where the availability shift was applied.
        availability_shift_applied: Bars of shift already applied. The engine asserts
            this is zero before applying its own, which makes an accidental double shift
            impossible rather than merely unlikely.
        lag_audit: The audit report, when one was run.
        sealed_features: Provenance for every feature column. Present whenever the frame
            came from the pipeline; the boundary that hands features to a strategy can
            require it, which is the precondition for consumer-side DIR-001 enforcement.
    """

    frame: pl.DataFrame
    specs: tuple[FeatureSpec, ...]
    warmup: WarmupPlan
    rows_trimmed: int
    lag_policy: LagPolicy
    availability_shift_applied: int
    lag_audit: LagAuditReport | None = None
    feature_columns: tuple[str, ...] = field(default_factory=tuple)
    sealed_features: SealedFeatureSet | None = None

    @property
    def height(self) -> int:
        """Return the number of usable rows."""
        return self.frame.height

    def feature_set_hash(self) -> str:
        """Return a stable hash of the whole feature set.

        Returns:
            Lowercase hex SHA-256 over the ordered transform spec hashes. Combined with
            a :class:`DatasetFingerprint` this forms the feature cache key.
        """
        digest = hashlib.sha256()
        digest.update(b"QTROS-FEATURE-SET-V1\n")
        for spec in self.specs:
            digest.update(spec.spec_hash().encode("ascii"))
        digest.update(f"|lag={self.lag_policy}|shift={self.availability_shift_applied}".encode())
        return digest.hexdigest()


# --------------------------------------------------------------------------------------
# Feature provenance (DIR-001, producer side)
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
    """Every column legitimately produced by a registered transform.

    This is what makes DIR-001 checkable rather than aspirational. A value a strategy
    derived for itself has no entry here; a value a transform produced does. Any
    consumer-side guard needs exactly that distinction, and nothing else can supply it.

    The type lives in ``domain/`` so that :class:`FeatureFrame` can carry it. Construction
    is Layer 0.5's job — see ``features.transforms.base.seal_features`` — because only that
    layer knows which transforms ran.

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
                unknown, and an unknown origin is what DIR-001 prohibits relying on.
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


@runtime_checkable
class FeatureComputer(Protocol):
    """Minimal surface the lag auditor needs from a pipeline.

    Declared structurally so ``lag_audit`` does not import ``pipeline``, which would
    make the two mutually dependent.
    """

    def compute_raw(
        self,
        frame: pl.DataFrame,
        transforms: Sequence[IFeatureTransform],
        *,
        allow_non_causal: bool = False,
    ) -> pl.DataFrame:
        """Apply transforms with no warmup trimming and no availability shift."""
        ...
