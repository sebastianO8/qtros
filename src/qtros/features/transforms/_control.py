"""Deliberately non-causal transforms.

**These must never appear in a strategy's feature set.** They exist for exactly one purpose:
to prove that the prefix-invariance audit detects look-ahead bias. A detector that has never
caught anything is not known to work, and "it passed on every feature we wrote" is not
evidence when every feature we wrote was also intended to be correct.

Four defences keep these out of production. They live in a module whose leading underscore
marks it private; they are registered under names beginning with ``control_``; they declare
:attr:`Causality.NON_CAUSAL_CONTROL`, which :class:`SealedFeatureSet` surfaces through
``non_causal_columns``; and the pipeline refuses to run any non-causal transform unless the
caller explicitly passes ``allow_non_causal=True``.

Note that a control still passes :meth:`TransformBase.self_check`, and should. Conformance
and causality are different properties: conformance asks whether the transform honours its
declared contract, causality asks whether it reads the future. This module honours its
contract while deliberately reading the future, which is exactly what makes it a useful
control.
"""

from __future__ import annotations

import polars as pl

from qtros.domain.errors import ConfigurationError
from qtros.domain.feature_contracts import Causality, FeatureSpec, WarmupRequirement
from qtros.features.transforms.base import TransformBase, register_transform


@register_transform("control_centered_mean")
class CenteredMeanControl(TransformBase):
    """A centred moving average, which reads bars from the future by construction.

    Its value at bar *t* changes when later bars are removed, so the prefix-invariance audit
    must flag it. If an audit run over this transform reports a pass, the audit is broken and
    no other pass result from that run means anything.

    Args:
        source_column: Column to average.
        window: Total window length. Must be odd so the window is genuinely centred.
        output_column: Name of the emitted column.

    Raises:
        ConfigurationError: If the window is even or below three.
    """

    def __init__(
        self,
        source_column: str = "close",
        window: int = 5,
        output_column: str = "control_centered_mean",
    ) -> None:
        if window < 3 or window % 2 == 0:
            raise ConfigurationError("Centred window must be odd and at least 3", window=window)
        self._source: str = source_column
        self._window: int = window
        self._column: str = output_column

    @property
    def spec(self) -> FeatureSpec:
        """Return this transform's identity, marked as a non-causal control."""
        return FeatureSpec(
            name="control_centered_mean",
            version="1.0.0",
            parameters={
                "source_column": self._source,
                "window": self._window,
                "output_column": self._column,
            },
            output_columns=(self._column,),
            depends_on=(self._source,),
            causality=Causality.NON_CAUSAL_CONTROL,
        )

    @property
    def warmup(self) -> WarmupRequirement:
        """Return the leading half-window as nominal warmup."""
        return WarmupRequirement(bars=self._window // 2)

    def _build(self, frame: pl.LazyFrame) -> pl.LazyFrame:
        """Append the centred mean by shifting a trailing mean backwards in time.

        Args:
            frame: Lazy frame carrying the source column.

        Returns:
            The frame with the non-causal column appended.
        """
        half: int = self._window // 2
        trailing: pl.Expr = pl.col(self._source).rolling_mean(
            window_size=self._window, min_samples=self._window
        )
        return frame.with_columns(trailing.shift(-half).alias(self._column))
