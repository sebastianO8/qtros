"""True Range and Average True Range.

Split into two transforms rather than one because ATR-of-true-range is the smallest honest
example of a dependency chain, and because true range is independently useful for stop
placement in Layer 3. The split is also what exercises cumulative warmup resolution: ATR(14)
over TrueRange needs 15 bars of history, not 14.

Both derive from :class:`TransformBase`, so their declared dependencies, declared outputs,
row-count invariance, and determinism are checked against a probe frame before any real data
reaches them.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

import polars as pl

from qtros.domain.errors import ConfigurationError
from qtros.domain.feature_contracts import Causality, FeatureSpec, WarmupRequirement
from qtros.features.transforms.base import TransformBase, register_transform

TRUE_RANGE_COLUMN: Final[str] = "true_range"


class AtrMethod(StrEnum):
    """Smoothing applied to true range."""

    SMA = "SMA"
    """Simple moving average. Finite kernel: warmup is exactly the period."""

    WILDER = "WILDER"
    """Wilder's smoothing, an EWM with alpha = 1/period.

    The kernel never fully decays, so no finite warmup makes the output exactly correct. A
    declared multiple of the period is used, which is an approximation — an honest one, since
    the alternative is pretending the requirement is ``period``.
    """


@register_transform("true_range")
class TrueRange(TransformBase):
    """True range: the greatest of the current bar's span and its two gap distances.

    Args:
        output_column: Name of the emitted column.

    Note:
        On the first bar the previous close is null, so both gap terms are null and the
        result reduces to high minus low. This is the standard convention, stated here
        because a reader would otherwise reasonably wonder whether the first bar is a bug.
        The declared one-bar warmup means that row is trimmed anyway.
    """

    def __init__(self, output_column: str = TRUE_RANGE_COLUMN) -> None:
        self._column: str = output_column

    @property
    def spec(self) -> FeatureSpec:
        """Return this transform's identity."""
        return FeatureSpec(
            name="true_range",
            version="1.0.0",
            parameters={"output_column": self._column},
            output_columns=(self._column,),
            depends_on=("high", "low", "close"),
            causality=Causality.CAUSAL,
        )

    @property
    def warmup(self) -> WarmupRequirement:
        """Return the one bar of history needed for the previous close."""
        return WarmupRequirement(bars=1)

    def _build(self, frame: pl.LazyFrame) -> pl.LazyFrame:
        """Append the true range column.

        Args:
            frame: Lazy frame carrying high, low, and close.

        Returns:
            The frame with the true range column appended.
        """
        previous_close: pl.Expr = pl.col("close").shift(1)
        return frame.with_columns(
            pl.max_horizontal(
                pl.col("high") - pl.col("low"),
                (pl.col("high") - previous_close).abs(),
                (pl.col("low") - previous_close).abs(),
            ).alias(self._column)
        )


@register_transform("atr")
class AverageTrueRange(TransformBase):
    """Average true range over a declared period.

    Args:
        period: Smoothing period in bars.
        method: Smoothing method.
        source_column: Column holding true range. Declared rather than assumed, so the
            dependency is visible to the DAG resolver and is verified against a probe frame
            containing only what this transform declares.
        output_column: Name of the emitted column. Defaults to ``atr_{period}``.
        wilder_warmup_multiple: Periods of history required under Wilder smoothing.

    Raises:
        ConfigurationError: If the period is below two, or the warmup multiple is below one.
    """

    def __init__(
        self,
        period: int = 14,
        method: AtrMethod = AtrMethod.SMA,
        source_column: str = TRUE_RANGE_COLUMN,
        output_column: str | None = None,
        wilder_warmup_multiple: int = 5,
    ) -> None:
        if period < 2:
            raise ConfigurationError("ATR period must be at least 2", period=period)
        if wilder_warmup_multiple < 1:
            raise ConfigurationError(
                "Wilder warmup multiple must be at least 1",
                wilder_warmup_multiple=wilder_warmup_multiple,
            )
        self._period: int = period
        self._method: AtrMethod = AtrMethod(method)
        self._source: str = source_column
        self._multiple: int = wilder_warmup_multiple
        self._column: str = output_column or f"atr_{period}"

    @property
    def spec(self) -> FeatureSpec:
        """Return this transform's identity."""
        return FeatureSpec(
            name="atr",
            version="1.0.0",
            parameters={
                "period": self._period,
                "method": str(self._method),
                "source_column": self._source,
                "output_column": self._column,
                "wilder_warmup_multiple": self._multiple,
            },
            output_columns=(self._column,),
            depends_on=(self._source,),
            causality=Causality.CAUSAL,
        )

    @property
    def warmup(self) -> WarmupRequirement:
        """Return the history required, which depends on the smoothing kernel.

        Returns:
            The period for SMA; a multiple of the period for Wilder, whose exponential kernel
            has no finite support.
        """
        if self._method is AtrMethod.SMA:
            return WarmupRequirement(bars=self._period)
        return WarmupRequirement(bars=self._period * self._multiple)

    def _build(self, frame: pl.LazyFrame) -> pl.LazyFrame:
        """Append the smoothed average true range column.

        Args:
            frame: Lazy frame carrying the true range source column.

        Returns:
            The frame with the ATR column appended.
        """
        source: pl.Expr = pl.col(self._source)
        smoothed: pl.Expr = (
            source.rolling_mean(window_size=self._period, min_samples=self._period)
            if self._method is AtrMethod.SMA
            else source.ewm_mean(alpha=1.0 / self._period, adjust=False, min_samples=self._period)
        )
        return frame.with_columns(smoothed.alias(self._column))
