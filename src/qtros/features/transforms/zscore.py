"""Rolling z-score of an arbitrary column.

Exists partly for its own sake and partly because it is the clearest demonstration that
warmup is a path property rather than a maximum. A 50-period z-score of a 14-period ATR over
true range requires 1 + 14 + 50 = 65 bars before its first trustworthy value. Taking the
maximum single lookback would trim 50 and hand the strategy fifteen bars of quietly-wrong
data at the start of every walk-forward fold.
"""

from __future__ import annotations

import polars as pl

from qtros.domain.errors import ConfigurationError
from qtros.domain.feature_contracts import Causality, FeatureSpec, WarmupRequirement
from qtros.features.transforms.base import TransformBase, register_transform, safe_divide


@register_transform("rolling_zscore")
class RollingZScore(TransformBase):
    """Standardise a column against its own trailing window.

    Args:
        source_column: Column to standardise.
        window: Trailing window length in bars.
        output_column: Name of the emitted column. Defaults to ``z_{source_column}_{window}``.
        min_periods: Observations required before emitting a value. Defaults to the full
            window, because a z-score computed from four observations is a number with no
            information in it.

    Raises:
        ConfigurationError: If the window is below two, or ``min_periods`` exceeds it.
    """

    def __init__(
        self,
        source_column: str,
        window: int = 50,
        output_column: str | None = None,
        min_periods: int | None = None,
    ) -> None:
        if window < 2:
            raise ConfigurationError("Z-score window must be at least 2", window=window)
        resolved_min: int = window if min_periods is None else min_periods
        if resolved_min > window:
            raise ConfigurationError(
                "min_periods cannot exceed the window",
                window=window,
                min_periods=resolved_min,
            )

        self._source: str = source_column
        self._window: int = window
        self._min_periods: int = resolved_min
        self._column: str = output_column or f"z_{source_column}_{window}"

    @property
    def spec(self) -> FeatureSpec:
        """Return this transform's identity."""
        return FeatureSpec(
            name="rolling_zscore",
            version="1.0.0",
            parameters={
                "source_column": self._source,
                "window": self._window,
                "min_periods": self._min_periods,
                "output_column": self._column,
            },
            output_columns=(self._column,),
            depends_on=(self._source,),
            causality=Causality.CAUSAL,
        )

    @property
    def warmup(self) -> WarmupRequirement:
        """Return the window length as this transform's own lookback."""
        return WarmupRequirement(bars=self._window)

    def _build(self, frame: pl.LazyFrame) -> pl.LazyFrame:
        """Append the rolling z-score column.

        Args:
            frame: Lazy frame carrying the source column.

        Returns:
            The frame with the z-score column appended.

        Note:
            A zero trailing standard deviation yields null rather than infinity. Flat windows
            are common in thin sessions and on illiquid instruments, and an infinity there
            would propagate into every downstream statistic and surface much later as an
            inexplicable signal.
        """
        source: pl.Expr = pl.col(self._source)
        mean: pl.Expr = source.rolling_mean(
            window_size=self._window, min_samples=self._min_periods
        )
        deviation: pl.Expr = source.rolling_std(
            window_size=self._window, min_samples=self._min_periods, ddof=1
        )
        return frame.with_columns(safe_divide(source - mean, deviation).alias(self._column))
