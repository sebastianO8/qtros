"""Execution simulation configuration (ARCH-001 C-1, C-6; ARCH-002 §4.6; ASS-001)."""

from __future__ import annotations

from pydantic import model_validator

from qtros.configuration.models.base import QtrosConfig
from qtros.domain.enums import ExecutionTiming, PathAssumption, SubMinLotPolicy, ZeroVolumePolicy


class ExecutionConfig(QtrosConfig):
    """How decisions become fills.

    Attributes:
        timing: Next-bar-open by default (ASS-001).
        path_assumption: Adverse-first by default (C-1).
        zero_volume_policy: Whether a zero-volume bar may fill.
        sub_min_lot_policy: Whether a sub-minimum size is rejected or raised.
        allow_same_bar_close_fill: Must be ``True`` to select ``SAME_BAR_CLOSE``; the
            report is watermarked whenever it is.
    """

    timing: ExecutionTiming = ExecutionTiming.NEXT_BAR_OPEN
    path_assumption: PathAssumption = PathAssumption.ADVERSE_FIRST
    zero_volume_policy: ZeroVolumePolicy = ZeroVolumePolicy.REJECT_FILL
    sub_min_lot_policy: SubMinLotPolicy = SubMinLotPolicy.REJECT_ORDER
    allow_same_bar_close_fill: bool = False

    @model_validator(mode="after")
    def _optimistic_pairs(self) -> ExecutionConfig:
        """Refuse undeclared or stacked optimism.

        Returns:
            The validated model.

        Raises:
            ValueError: If same-bar-close is selected without the flag, or combined with
                favourable-first.
        """
        if self.timing is ExecutionTiming.SAME_BAR_CLOSE and not self.allow_same_bar_close_fill:
            raise ValueError("SAME_BAR_CLOSE timing requires allow_same_bar_close_fill: true")
        if (
            self.timing is ExecutionTiming.SAME_BAR_CLOSE
            and self.path_assumption is PathAssumption.FAVOURABLE_FIRST
        ):
            raise ValueError(
                "SAME_BAR_CLOSE with FAVOURABLE_FIRST stacks two optimistic assumptions"
            )
        return self

    @property
    def is_watermarked(self) -> bool:
        """Return whether any optimistic setting is active and must be watermarked."""
        return (
            self.timing is ExecutionTiming.SAME_BAR_CLOSE
            or self.path_assumption is PathAssumption.FAVOURABLE_FIRST
        )
