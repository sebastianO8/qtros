"""The look-ahead detector must catch look-ahead (Directive 1; ASS-002).

A detector that has never caught anything is not known to work. These tests run the
prefix-invariance audit against a deliberately cheating indicator (a centred moving
average, which reads future bars) and require it to fail, then against the real feature
set and require it to pass.
"""

from __future__ import annotations

import pytest

from qtros.domain.errors import ConfigurationError, LookaheadDetected
from qtros.features.lag_audit import LagAuditor, verify_detector
from qtros.features.pipeline import FeaturePipeline
from qtros.features.transforms import CenteredMeanControl, build_transform
from tests.test_builder import synthetic_bars


def test_detector_catches_the_cheating_control() -> None:
    report = verify_detector(synthetic_bars(400), probe_count=10)
    assert not report.passed
    assert "control_centered_mean" in report.failing_columns


def test_audit_raises_on_look_ahead_by_default() -> None:
    control = CenteredMeanControl(source_column="close", window=5)
    with pytest.raises(LookaheadDetected):
        LagAuditor().audit(synthetic_bars(400), [control], probe_count=10, allow_non_causal=True)


def test_real_feature_set_passes_the_audit() -> None:
    transforms = [
        build_transform("true_range"),
        build_transform("atr", {"period": 14, "output_column": "atr_14"}),
        build_transform("rolling_zscore", {"source_column": "close", "window": 50}),
    ]
    report = LagAuditor().audit(synthetic_bars(400), transforms, probe_count=10)
    assert report.passed and not report.findings


def test_pipeline_refuses_a_non_causal_transform_without_explicit_permission() -> None:
    control = CenteredMeanControl(source_column="close", window=5)
    with pytest.raises(ConfigurationError):
        FeaturePipeline().compute(synthetic_bars(400), [control])
