"""Prefix-invariance auditing: the mechanical enforcement of Directive 1.

The test is simple enough to state in one sentence and is the reason it works: **for a
causal feature, the value at bar t must not change when every bar after t is removed.**

Compute the features on the full frame. Compute them again on the frame truncated at
bar *t*. Compare the values at *t*. Any difference is look-ahead bias, regardless of how
the transform is written, whether it contains a ``.shift()``, or whether a reviewer
found it convincing.

Why this matters more than review: look-ahead bias is the failure mode most resistant to
code inspection, because the offending code usually looks correct. A reviewer reading
``rolling_mean(...).shift(1)`` will approve it; the bug is typically two layers down in
how the frame was sorted, joined, or grouped. This test requires no understanding of what
a feature computes, runs against every transform ever added, and cannot be argued with.

Only the *tail* is truncated, never the head. That preserves the frame's starting point,
which matters for expanding and exponentially-weighted features: those legitimately
depend on where the data begins, so truncating the head would report a false positive on
a perfectly causal transform.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import structlog

from qtros.domain.errors import DetectorVerificationError, LookaheadDetected
from qtros.domain.feature_contracts import (
    FeatureComputer,
    IFeatureTransform,
    LagAuditFinding,
    LagAuditReport,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    import polars as pl

_LOGGER: Final = structlog.get_logger(__name__)

DEFAULT_PROBE_COUNT: Final[int] = 25
"""Probes sampled by default.

Each probe recomputes the whole prefix, so the audit costs roughly ``probes x n`` row
operations. On three years of five-minute bars that is seconds, not minutes - but it is
not free, which is why CI runs the full audit against a fixture and a production run
samples. The probe indices are recorded in the report so a run's coverage is auditable
rather than assumed.
"""

DEFAULT_TOLERANCE: Final[float] = 0.0
"""Exact equality by default.

Tail truncation leaves every windowed and cumulative computation bit-identical, so exact
comparison is achievable - and a tolerance is a place where a small real leak can hide
beneath the threshold. The parameter exists for transforms that legitimately involve
iterative solvers; using it otherwise weakens the guarantee.
"""


class LagAuditor:
    """Verifies that feature transforms are prefix-invariant.

    Args:
        computer: Component used to recompute features. Injected so the auditor can be
            tested against a stub, and so it does not depend on the pipeline module at
            import time. Defaults to a fresh :class:`FeaturePipeline`.
    """

    def __init__(self, computer: FeatureComputer | None = None) -> None:
        if computer is None:
            from qtros.features.pipeline import FeaturePipeline

            computer = FeaturePipeline()
        self._computer: FeatureComputer = computer

    def audit(
        self,
        frame: pl.DataFrame,
        transforms: Sequence[IFeatureTransform],
        *,
        probe_count: int = DEFAULT_PROBE_COUNT,
        seed: int = 0,
        tolerance: float = DEFAULT_TOLERANCE,
        allow_non_causal: bool = False,
        raise_on_failure: bool = True,
    ) -> LagAuditReport:
        """Run the prefix-invariance audit.

        Args:
            frame: Bars to audit against. Should be representative - a frame with no DST
                transition and no session gap will not exercise the features that break
                on them.
            transforms: Transforms to audit.
            probe_count: Number of row indices to sample.
            seed: Seed for deterministic probe selection. The same seed always probes the
                same rows, so a failure is reproducible.
            tolerance: Absolute difference treated as equal.
            allow_non_causal: Permit control transforms, used when verifying the detector.
            raise_on_failure: Raise rather than returning a failing report. True in normal
                use, because a caller that ignores a failing report would proceed with a
                biased frame.

        Returns:
            The audit report.

        Raises:
            LookaheadDetected: If any probed value diverged and ``raise_on_failure``.
        """
        started: datetime = datetime.now(UTC)

        if not transforms or frame.height < 3:
            return LagAuditReport(
                passed=True,
                transforms_audited=tuple(t.spec.name for t in transforms),
                probe_indices=(),
                findings=(),
                columns_checked=(),
                tolerance=tolerance,
                rows_in_frame=frame.height,
                audited_at_utc=started,
            )

        from qtros.features.pipeline import resolve_execution_order, resolve_warmup

        order: tuple[IFeatureTransform, ...] = resolve_execution_order(transforms)
        warmup_bars: int = resolve_warmup(order).total_bars

        full: pl.DataFrame = self._computer.compute_raw(
            frame, order, allow_non_causal=allow_non_causal
        )
        columns: tuple[str, ...] = tuple(
            column for transform in order for column in transform.spec.output_columns
        )
        owner: dict[str, str] = {
            column: transform.spec.name
            for transform in order
            for column in transform.spec.output_columns
        }

        probes: tuple[int, ...] = self._select_probes(
            row_count=frame.height,
            warmup_bars=warmup_bars,
            probe_count=probe_count,
            seed=seed,
        )

        findings: list[LagAuditFinding] = []
        for index in probes:
            prefix: pl.DataFrame = self._computer.compute_raw(
                frame.head(index + 1), order, allow_non_causal=allow_non_causal
            )
            full_row: dict[str, Any] = full.slice(index, 1).row(0, named=True)
            prefix_row: dict[str, Any] = prefix.slice(prefix.height - 1, 1).row(0, named=True)

            for column in columns:
                if not _values_differ(full_row[column], prefix_row[column], tolerance):
                    continue
                findings.append(
                    LagAuditFinding(
                        transform_name=owner[column],
                        column=column,
                        probe_index=index,
                        full_frame_value=repr(full_row[column]),
                        prefix_value=repr(prefix_row[column]),
                        detail=(
                            "Value at this bar changed when later bars were removed, "
                            "so the feature reads data from the future"
                        ),
                    )
                )

        report = LagAuditReport(
            passed=not findings,
            transforms_audited=tuple(transform.spec.name for transform in order),
            probe_indices=probes,
            findings=tuple(findings),
            columns_checked=columns,
            tolerance=tolerance,
            rows_in_frame=frame.height,
            audited_at_utc=started,
        )

        if report.passed:
            _LOGGER.info(
                "features.lag_audit_passed",
                transforms=list(report.transforms_audited),
                columns=len(columns),
                probes=len(probes),
                rows=frame.height,
            )
        else:
            _LOGGER.error(
                "features.lag_audit_failed",
                failing_columns=list(report.failing_columns),
                findings=len(findings),
                probes=len(probes),
            )
            if raise_on_failure:
                first: LagAuditFinding = findings[0]
                raise LookaheadDetected(
                    "Prefix-invariance audit failed: a feature reads data from the future",
                    failing_columns=list(report.failing_columns),
                    finding_count=len(findings),
                    first_transform=first.transform_name,
                    first_column=first.column,
                    first_probe_index=first.probe_index,
                    full_frame_value=first.full_frame_value,
                    prefix_value=first.prefix_value,
                )

        return report

    @staticmethod
    def _select_probes(
        *,
        row_count: int,
        warmup_bars: int,
        probe_count: int,
        seed: int,
    ) -> tuple[int, ...]:
        """Choose which row indices to probe.

        Probes start at the warmup boundary. Probing inside the warmup window would
        compare null against null and pass trivially, consuming a probe while testing
        nothing - the first row where a feature actually has a value is the first row
        where the question is meaningful.

        The first valid row and the last two rows are always probed. The final rows are
        where a look-ahead transform has the least future data available to it, so they
        are where a subtle leak is most likely to produce a small, easily-missed
        divergence rather than an obvious one.

        Args:
            row_count: Rows in the frame.
            warmup_bars: Cumulative warmup across the DAG.
            probe_count: Target number of probes.
            seed: Seed for deterministic selection.

        Returns:
            Sorted, distinct row indices.
        """
        lowest: int = min(max(warmup_bars, 1), row_count - 1)
        highest: int = row_count - 1
        if lowest >= highest:
            return (highest,)

        mandatory: set[int] = {lowest, highest, highest - 1}
        mandatory = {index for index in mandatory if lowest <= index <= highest}

        candidates: list[int] = [
            index for index in range(lowest, highest + 1) if index not in mandatory
        ]
        wanted: int = max(probe_count - len(mandatory), 0)
        rng = random.Random(seed)
        sampled: list[int] = (
            rng.sample(candidates, wanted) if wanted < len(candidates) else candidates
        )
        return tuple(sorted(mandatory | set(sampled)))


def verify_detector(
    frame: pl.DataFrame,
    *,
    probe_count: int = DEFAULT_PROBE_COUNT,
    seed: int = 0,
) -> LagAuditReport:
    """Confirm the audit detects a known non-causal transform.

    A detector that has never caught anything is not known to work. This runs the audit
    against a deliberately look-ahead control and asserts that it *fails*. It belongs in
    CI alongside the real audit, and a green audit run whose detector verification did
    not also run should not be trusted.

    Args:
        frame: Bars to run the control against.
        probe_count: Probes to sample.
        seed: Seed for deterministic probe selection.

    Returns:
        The failing report produced by the control, for inspection.

    Raises:
        DetectorVerificationError: If the control *passes*, which means the detector is
            broken and every other pass result from the same run is meaningless.
    """
    from qtros.features.transforms._control import CenteredMeanControl

    control = CenteredMeanControl(source_column="close", window=5)
    report: LagAuditReport = LagAuditor().audit(
        frame,
        [control],
        probe_count=probe_count,
        seed=seed,
        allow_non_causal=True,
        raise_on_failure=False,
    )

    if report.passed:
        raise DetectorVerificationError(
            "The prefix-invariance audit passed a deliberately non-causal transform; "
            "the detector is broken and no other pass result from this run is valid",
            probes=len(report.probe_indices),
            rows=report.rows_in_frame,
        )

    _LOGGER.info(
        "features.detector_verified",
        findings=len(report.findings),
        probes=len(report.probe_indices),
    )
    return report


def _values_differ(left: Any, right: Any, tolerance: float) -> bool:
    """Compare two feature values, treating nulls as significant.

    Args:
        left: Value from the full-frame computation.
        right: Value from the prefix computation.
        tolerance: Absolute difference treated as equal.

    Returns:
        ``True`` if the values differ meaningfully.

    Note:
        A null on one side and a value on the other counts as a difference. Above the
        warmup boundary the prefix has enough history for every transform, so a null
        appearing only in the truncated computation means the feature needed future data
        to produce its value - which is the very thing being tested for.
    """
    if left is None and right is None:
        return False
    if left is None or right is None:
        return True
    if isinstance(left, float) and isinstance(right, float):
        if left != left and right != right:  # both NaN
            return False
        return abs(left - right) > tolerance
    return bool(left != right)
