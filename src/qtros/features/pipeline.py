"""Feature pipeline: DAG resolution, warmup trimming, and schema validation.

Three properties are established here and depended upon by every later layer.

**Execution order is derived, not declared.** Transforms state what columns they read and
what they write; the pipeline computes a topological order. A researcher adding a feature
that depends on another does not have to remember to insert it in the right place, and
cannot get it wrong.

**Warmup is a path property.** Cumulative warmup along the longest dependency chain, not
the largest single lookback. A 50-bar z-score of a 14-bar ATR over true range needs 65
bars. Trimming 50 would silently hand fifteen bars of wrong data to every walk-forward
fold, and it would look like noise rather than a bug.

**The availability shift happens exactly once.** See :class:`LagPolicy`. The frame records
how much shift it carries so that a second application is refused rather than merely
discouraged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import polars as pl
import structlog

from qtros.domain.errors import ConfigurationError, SchemaContractError
from qtros.domain.feature_contracts import (
    BASE_COLUMNS,
    Causality,
    FeatureFrame,
    FeatureSpec,
    IFeatureTransform,
    LagAuditReport,
    LagPolicy,
    SealedFeatureSet,
    WarmupPlan,
)
from qtros.features.transforms.base import TransformBase, seal_features

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_LOGGER: Final = structlog.get_logger(__name__)

REQUIRED_INPUT_COLUMNS: Final[tuple[str, ...]] = (
    "ts_utc",
    "open",
    "high",
    "low",
    "close",
)

_NUMERIC_DTYPES: Final[frozenset[pl.DataType]] = frozenset(
    {pl.Float32, pl.Float64, pl.Int8, pl.Int16, pl.Int32, pl.Int64, pl.Boolean}  # type: ignore[arg-type]
)

HIGH_NULL_FRACTION_WARNING: Final[float] = 0.10
"""Null fraction in a trimmed feature column above which a warning is logged.

Not an error: some features are legitimately null in flat or thin windows. But a column
that is a quarter null after warmup trimming usually means a misconfigured window or a
divide-by-zero guard firing constantly, and that is worth noticing before a backtest is
built on it.
"""


class FeaturePipeline:
    """Composes feature transforms into a validated, warmup-trimmed frame.

    The pipeline is stateless between calls and holds no configuration. Everything that
    varies between runs arrives as an argument, so two calls with the same inputs produce
    the same output — which is the precondition for the feature cache and for
    reproducibility under Directive 10.
    """

    # -- public API --------------------------------------------------------------------

    def compute(
        self,
        frame: pl.DataFrame,
        transforms: Sequence[IFeatureTransform],
        *,
        lag_policy: LagPolicy = LagPolicy.DEFER_TO_ENGINE,
        allow_non_causal: bool = False,
        audit: bool = True,
        audit_probe_count: int = 25,
        audit_seed: int = 0,
        conformance_check: bool = True,
    ) -> FeatureFrame:
        """Compute a feature frame, trimmed and validated.

        Args:
            frame: Bars from Layer 0, sorted ascending by timestamp.
            transforms: Transforms to apply. Order is irrelevant; it is resolved.
            lag_policy: Where the availability shift is applied.
            allow_non_causal: Permit non-causal control transforms. Never true outside
                audit self-verification.
            audit: Run the prefix-invariance audit before returning. Defaults to true,
                because a feature frame that has not been audited is a feature frame
                whose causality is a matter of belief.
            audit_probe_count: Probe indices to sample.
            audit_seed: Seed for deterministic probe selection.
            conformance_check: Run each transform's contract check against a probe frame
                before touching real data. True by default: it costs milliseconds and it
                catches an undeclared dependency, a row-count change, or a non-deterministic
                build before a multi-year run rather than during one.

        Returns:
            A validated, provenance-sealed feature frame.

        Raises:
            SchemaContractError: If the input frame is missing a required column, or a
                transform failed to emit a declared output.
            ConfigurationError: If the transform DAG is unresolvable, or a non-causal
                transform is present without permission.
            LookaheadDetected: If the audit finds a non-prefix-invariant column. Raised
                by the auditor, not caught here — a frame containing look-ahead bias must
                never be returned, however convenient that would be mid-debugging.
        """
        self._validate_input_frame(frame)
        order: tuple[IFeatureTransform, ...] = resolve_execution_order(transforms)
        self._guard_causality(order, allow_non_causal=allow_non_causal)
        self._run_conformance_checks(order, enabled=conformance_check)
        plan: WarmupPlan = resolve_warmup(order)

        computed: pl.DataFrame = self._apply_all(frame, order)
        self._validate_outputs(computed, order)

        feature_columns: tuple[str, ...] = tuple(
            column for transform in order for column in transform.spec.output_columns
        )

        report: LagAuditReport | None = None
        if audit:
            from qtros.features.lag_audit import LagAuditor

            report = LagAuditor().audit(
                frame,
                order,
                probe_count=audit_probe_count,
                seed=audit_seed,
                allow_non_causal=allow_non_causal,
            )

        shift_applied: int = 1 if lag_policy is LagPolicy.SHIFT_IN_PIPELINE else 0
        if shift_applied:
            computed = computed.with_columns(
                [pl.col(column).shift(shift_applied) for column in feature_columns]
            )

        head_rows: int = self._resolve_head_trim(computed, plan) + shift_applied
        trimmed: pl.DataFrame = computed.slice(head_rows)

        # DIR-001, producer side: record which transform produced every feature column,
        # then assert the frame contains nothing else. A column added between the pipeline
        # and the engine — by a notebook, a debugging session, a well-meaning helper —
        # would otherwise be indistinguishable from a real feature at the context boundary.
        sealed: SealedFeatureSet = seal_features(order)
        sealed.verify_frame_columns(trimmed.columns)

        self._report_null_fractions(trimmed, feature_columns)

        result = FeatureFrame(
            frame=trimmed,
            specs=tuple(transform.spec for transform in order),
            warmup=plan,
            rows_trimmed=head_rows,
            lag_policy=lag_policy,
            availability_shift_applied=shift_applied,
            lag_audit=report,
            feature_columns=feature_columns,
            sealed_features=sealed,
        )

        _LOGGER.info(
            "features.computed",
            transforms=[transform.spec.name for transform in order],
            rows_in=frame.height,
            rows_out=result.height,
            rows_trimmed=head_rows,
            warmup_bars=plan.total_bars,
            lag_policy=str(lag_policy),
            audited=audit,
            conformance_checked=conformance_check,
            sealed_columns=len(sealed.features),
            non_causal_columns=list(sealed.non_causal_columns),
            feature_set_hash=result.feature_set_hash()[:12],
        )
        return result

    def compute_raw(
        self,
        frame: pl.DataFrame,
        transforms: Sequence[IFeatureTransform],
        *,
        allow_non_causal: bool = False,
    ) -> pl.DataFrame:
        """Apply transforms with no trimming, no shift, and no audit.

        This is what the lag auditor calls. It must remain free of trimming and shifting,
        because both would mask exactly the divergence the audit is looking for.

        Conformance checks are deliberately *not* run here. The auditor calls this once per
        probe index, and re-verifying an unchanged transform on every call would multiply the
        audit's cost for no additional information — the check is a property of the transform,
        not of the frame.

        Args:
            frame: Bars to transform.
            transforms: Transforms to apply, in any order.
            allow_non_causal: Permit non-causal control transforms.

        Returns:
            The transformed frame, untrimmed.

        Raises:
            SchemaContractError: If the input frame is missing a required column.
            ConfigurationError: If the DAG is unresolvable or contains a disallowed
                non-causal transform.
        """
        self._validate_input_frame(frame)
        order: tuple[IFeatureTransform, ...] = resolve_execution_order(transforms)
        self._guard_causality(order, allow_non_causal=allow_non_causal)
        return self._apply_all(frame, order)

    # -- internals ---------------------------------------------------------------------

    @staticmethod
    def _apply_all(
        frame: pl.DataFrame,
        order: Sequence[IFeatureTransform],
    ) -> pl.DataFrame:
        """Apply transforms in resolved order through a single lazy plan.

        Args:
            frame: Input bars.
            order: Transforms in topological order.

        Returns:
            The collected result.

        Raises:
            SchemaContractError: If a transform raises while building its expression.
        """
        lazy: pl.LazyFrame = frame.lazy()
        for transform in order:
            try:
                lazy = transform.apply(lazy)
            except Exception as exc:
                raise SchemaContractError(
                    "Feature transform failed while building its expression",
                    transform=transform.spec.name,
                    version=transform.spec.version,
                    cause=str(exc),
                ) from exc
        try:
            return lazy.collect()
        except Exception as exc:
            raise SchemaContractError(
                "Feature pipeline failed during evaluation",
                transforms=[transform.spec.name for transform in order],
                cause=str(exc),
            ) from exc

    @staticmethod
    def _validate_input_frame(frame: pl.DataFrame) -> None:
        """Assert the input frame carries the Layer 0 columns transforms rely on.

        Args:
            frame: Frame to check.

        Raises:
            SchemaContractError: If a required column is missing, or the frame is not
                sorted ascending by timestamp. Sort order is checked rather than imposed:
                every rolling and cumulative operation downstream assumes it, and quietly
                sorting here would hide a Layer 0 fault.
        """
        missing: tuple[str, ...] = tuple(
            column for column in REQUIRED_INPUT_COLUMNS if column not in frame.columns
        )
        if missing:
            raise SchemaContractError(
                "Input frame is missing columns required by the feature pipeline",
                missing=list(missing),
                present=frame.columns,
            )
        if frame.height > 1:
            descending: int = int(
                frame.select(
                    (pl.col("ts_utc").diff() < pl.duration(microseconds=0))
                    .fill_null(value=False)
                    .alias("_back")
                )
                .get_column("_back")
                .sum()
            )
            if descending:
                raise SchemaContractError(
                    "Input frame is not sorted ascending by timestamp; every rolling "
                    "and cumulative feature below assumes it is",
                    descending_steps=descending,
                )

    @staticmethod
    def _run_conformance_checks(
        order: Sequence[IFeatureTransform],
        *,
        enabled: bool,
    ) -> None:
        """Verify every transform honours its declared contract before real data is used.

        Args:
            order: Transforms in execution order.
            enabled: Whether to run the checks.

        Raises:
            ConfigurationError: If a transform does not derive from :class:`TransformBase`.
                Registration already refuses such classes; this covers transforms constructed
                directly, and it is what makes the producer-side guarantee total rather than
                typical — one unchecked transform and the seal cannot vouch for the columns it
                produced.
            SchemaContractError: If a transform fails its contract check.
        """
        if not enabled:
            return
        for transform in order:
            if not isinstance(transform, TransformBase):
                raise ConfigurationError(
                    "Feature transforms must derive from TransformBase so their declared "
                    "dependencies, outputs, and row-count invariance are enforced",
                    transform=transform.spec.name,
                    cls=type(transform).__name__,
                )
            transform.self_check()

    @staticmethod
    def _guard_causality(
        order: Sequence[IFeatureTransform],
        *,
        allow_non_causal: bool,
    ) -> None:
        """Refuse non-causal transforms unless explicitly permitted.

        Args:
            order: Transforms about to run.
            allow_non_causal: Whether control transforms are permitted.

        Raises:
            ConfigurationError: If a non-causal transform is present without permission.
        """
        if allow_non_causal:
            return
        offenders: tuple[str, ...] = tuple(
            transform.spec.name
            for transform in order
            if transform.spec.causality is not Causality.CAUSAL
        )
        if offenders:
            raise ConfigurationError(
                "Non-causal control transforms cannot be used to generate features; "
                "they exist only to verify that the lag audit detects look-ahead",
                transforms=list(offenders),
            )

    @staticmethod
    def _validate_outputs(
        frame: pl.DataFrame,
        order: Sequence[IFeatureTransform],
    ) -> None:
        """Assert every transform produced the columns it declared, with usable dtypes.

        A transform whose declared outputs do not match its actual outputs breaks the DAG
        resolver for every transform downstream of it, so the mismatch is caught here
        rather than surfacing later as a missing-column error with no obvious cause.

        Args:
            frame: Computed frame.
            order: Transforms that were applied.

        Raises:
            SchemaContractError: On a missing output column or a non-numeric dtype.
        """
        for transform in order:
            spec: FeatureSpec = transform.spec
            for column in spec.output_columns:
                if column not in frame.columns:
                    raise SchemaContractError(
                        "Transform did not emit a declared output column",
                        transform=spec.name,
                        version=spec.version,
                        column=column,
                    )
                dtype: pl.DataType = frame.schema[column]
                if dtype.base_type() not in _NUMERIC_DTYPES:
                    raise SchemaContractError(
                        "Feature column has a non-numeric dtype",
                        transform=spec.name,
                        column=column,
                        dtype=str(dtype),
                    )

    @staticmethod
    def _resolve_head_trim(frame: pl.DataFrame, plan: WarmupPlan) -> int:
        """Return how many leading rows to drop.

        Takes the larger of the cumulative bar warmup and the length of the first
        session, when any transform declares a session-scoped requirement. The first
        session is always dropped rather than inspected for completeness: whether the
        dataset happens to begin at a session boundary is an accident of the download,
        and an anchored feature computed from mid-session is a different statistic rather
        than a noisier one.

        Args:
            frame: Computed, untrimmed frame.
            plan: Resolved warmup plan.

        Returns:
            Number of leading rows to remove.
        """
        head: int = plan.total_bars
        for column in plan.session_columns:
            if column not in frame.columns or frame.height == 0:
                continue
            first_session = frame.get_column(column)[0]
            first_session_rows: int = int(
                frame.select((pl.col(column) == first_session).sum()).item()
            )
            head = max(head, first_session_rows)
        return min(head, frame.height)

    @staticmethod
    def _report_null_fractions(
        frame: pl.DataFrame,
        feature_columns: Sequence[str],
    ) -> None:
        """Log a warning for any feature column that is substantially null after trimming.

        Args:
            frame: Trimmed frame.
            feature_columns: Columns to inspect.
        """
        if frame.height == 0:
            return
        for column in feature_columns:
            fraction: float = frame.get_column(column).null_count() / frame.height
            if fraction > HIGH_NULL_FRACTION_WARNING:
                _LOGGER.warning(
                    "features.high_null_fraction",
                    column=column,
                    null_fraction=round(fraction, 4),
                    rows=frame.height,
                )


# --------------------------------------------------------------------------------------
# DAG resolution
# --------------------------------------------------------------------------------------


def resolve_execution_order(
    transforms: Sequence[IFeatureTransform],
) -> tuple[IFeatureTransform, ...]:
    """Order transforms so every dependency is produced before it is consumed.

    Args:
        transforms: Transforms in arbitrary order.

    Returns:
        Transforms in a deterministic topological order. Ties are broken by the caller's
        original ordering, so the same input always yields the same order — which matters
        because execution order participates in the feature set hash.

    Raises:
        ConfigurationError: If two transforms claim the same output column, if a
            dependency is produced by nothing, or if the graph contains a cycle.
    """
    if not transforms:
        return ()

    producers: dict[str, int] = {}
    for index, transform in enumerate(transforms):
        for column in transform.spec.output_columns:
            if column in producers:
                raise ConfigurationError(
                    "Two transforms declare the same output column",
                    column=column,
                    first=transforms[producers[column]].spec.name,
                    second=transform.spec.name,
                )
            producers[column] = index

    dependencies: dict[int, set[int]] = {index: set() for index in range(len(transforms))}
    for index, transform in enumerate(transforms):
        for column in transform.spec.depends_on:
            if column in BASE_COLUMNS:
                continue
            if column not in producers:
                raise ConfigurationError(
                    "Transform depends on a column no transform produces",
                    transform=transform.spec.name,
                    missing_column=column,
                    available=sorted(set(producers) | BASE_COLUMNS),
                )
            producer: int = producers[column]
            if producer == index:
                raise ConfigurationError(
                    "Transform depends on its own output",
                    transform=transform.spec.name,
                    column=column,
                )
            dependencies[index].add(producer)

    ordered: list[IFeatureTransform] = []
    resolved: set[int] = set()
    remaining: list[int] = list(range(len(transforms)))

    while remaining:
        ready: list[int] = [index for index in remaining if dependencies[index] <= resolved]
        if not ready:
            cycle: list[str] = [transforms[index].spec.name for index in remaining]
            raise ConfigurationError(
                "Feature transform dependency graph contains a cycle",
                involved=cycle,
            )
        for index in ready:
            ordered.append(transforms[index])
            resolved.add(index)
        remaining = [index for index in remaining if index not in resolved]

    return tuple(ordered)


def resolve_warmup(order: Sequence[IFeatureTransform]) -> WarmupPlan:
    """Compute cumulative warmup along the longest path through the DAG.

    Args:
        order: Transforms in topological order.

    Returns:
        The resolved plan, including per-transform cumulative warmup and any
        session-scoped requirements.
    """
    producer_warmup: dict[str, int] = {}
    per_transform: dict[str, int] = {}
    session_columns: list[str] = []
    total: int = 0

    for transform in order:
        spec: FeatureSpec = transform.spec
        upstream: int = max(
            (producer_warmup.get(column, 0) for column in spec.depends_on),
            default=0,
        )
        cumulative: int = upstream + transform.warmup.bars
        per_transform[spec.name] = cumulative
        total = max(total, cumulative)

        for column in spec.output_columns:
            producer_warmup[column] = cumulative

        session_column: str | None = transform.warmup.session_column
        if session_column is not None:
            session_columns.append(session_column)

    return WarmupPlan(
        total_bars=total,
        per_transform=per_transform,
        session_columns=tuple(session_columns),
    )


def describe_plan(order: Sequence[IFeatureTransform], plan: WarmupPlan) -> Mapping[str, object]:
    """Flatten a resolved pipeline into a log-friendly mapping.

    Args:
        order: Transforms in execution order.
        plan: Resolved warmup plan.

    Returns:
        A mapping suitable for structured logging or CLI output.
    """
    return {
        "execution_order": [transform.spec.name for transform in order],
        "output_columns": [
            column for transform in order for column in transform.spec.output_columns
        ],
        "warmup_total_bars": plan.total_bars,
        "warmup_per_transform": dict(plan.per_transform),
        "session_scoped": list(plan.session_columns),
        "spec_hashes": {transform.spec.name: transform.spec.short_hash for transform in order},
        "sealed_columns": sorted(seal_features(order).features),
    }
