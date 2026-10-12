"""Average Directional Index, Wilder's definition.

Matches the construction used by TradingView's ``ta.dmi`` and by the Cycle 1 script: the
directional movements and the true range are each smoothed with Wilder's moving average
(an EWM with alpha = 1/period), the directional indices are formed from those, and the ADX
is a second Wilder smoothing of the directional index spread. Simple or exponential
averages give a visibly different series, which is why the smoothing is not configurable.
"""

from __future__ import annotations

from typing import Final

import polars as pl

from qtros.domain.errors import ConfigurationError
from qtros.domain.feature_contracts import Causality, FeatureSpec, WarmupRequirement
from qtros.features.transforms.base import TransformBase, register_transform, safe_divide

_HUNDRED: Final[float] = 100.0


def _wilder(expr: pl.Expr, length: int) -> pl.Expr:
    """Return Wilder's moving average of an expression."""
    return expr.ewm_mean(alpha=1.0 / length, adjust=False, min_samples=length)


@register_transform("adx")
class AverageDirectionalIndex(TransformBase):
    """Wilder's ADX: trend strength on a 0-100 scale, direction-agnostic.

    Args:
        period: Smoothing length for directional movement and true range.
        smoothing: Smoothing length applied to the directional index to form the ADX.
        output_column: Name of the emitted column. Defaults to ``adx_{period}``.
        wilder_warmup_multiple: Wilder's average never fully forgets its seed, so the
            declared warmup is this multiple of ``period + smoothing`` bars. An honest
            approximation, as for ATR.

    Raises:
        ConfigurationError: If a length is below two or the multiple below one.
    """

    def __init__(
        self,
        period: int = 14,
        smoothing: int = 14,
        output_column: str | None = None,
        wilder_warmup_multiple: int = 5,
    ) -> None:
        if period < 2 or smoothing < 2:
            raise ConfigurationError(
                "ADX lengths must be at least 2", period=period, smoothing=smoothing
            )
        if wilder_warmup_multiple < 1:
            raise ConfigurationError(
                "Wilder warmup multiple must be at least 1", multiple=wilder_warmup_multiple
            )
        self._period: int = period
        self._smoothing: int = smoothing
        self._multiple: int = wilder_warmup_multiple
        self._column: str = output_column or f"adx_{period}"

    @property
    def spec(self) -> FeatureSpec:
        """Return this transform's identity."""
        return FeatureSpec(
            name="adx",
            version="1.0.0",
            parameters={
                "period": self._period,
                "smoothing": self._smoothing,
                "output_column": self._column,
                "wilder_warmup_multiple": self._multiple,
            },
            output_columns=(self._column,),
            depends_on=("high", "low", "close"),
            causality=Causality.CAUSAL,
        )

    @property
    def warmup(self) -> WarmupRequirement:
        """Return the declared Wilder warmup."""
        return WarmupRequirement(bars=(self._period + self._smoothing) * self._multiple)

    def _build(self, frame: pl.LazyFrame) -> pl.LazyFrame:
        """Append the ADX column.

        Args:
            frame: Lazy frame carrying ``high``, ``low``, and ``close``.

        Returns:
            The frame with the ADX appended. Null until both smoothings have their minimum
            samples, and null wherever both directional indices are zero.
        """
        high, low, close = pl.col("high"), pl.col("low"), pl.col("close")
        up_move = high.diff()
        down_move = -low.diff()
        plus_dm = pl.when((up_move > down_move) & (up_move > 0.0)).then(up_move).otherwise(0.0)
        minus_dm = pl.when((down_move > up_move) & (down_move > 0.0)).then(down_move).otherwise(0.0)
        previous_close = close.shift(1)
        true_range = pl.max_horizontal(
            high - low, (high - previous_close).abs(), (low - previous_close).abs()
        )
        smoothed_tr = _wilder(true_range, self._period)
        plus_di = _HUNDRED * safe_divide(_wilder(plus_dm, self._period), smoothed_tr)
        minus_di = _HUNDRED * safe_divide(_wilder(minus_dm, self._period), smoothed_tr)
        dx = _HUNDRED * safe_divide((plus_di - minus_di).abs(), plus_di + minus_di)
        return frame.with_columns(_wilder(dx, self._smoothing).alias(self._column))
