"""Strategy configuration (ARCH-001 C-2, C-9; ARCH-002 §4.5, D-4; PI ruling G-5).

G-5 places the significance policy in the **run-affecting** partition. Editing the tier
after seeing results therefore changes the run hash and mints a new experiment, which is
the anti-retroactivity property C-2 asks for: two experiments with an ordering that speaks
for itself, rather than one experiment whose bar was quietly moved.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime

from pydantic import Field, field_validator, model_validator

from qtros.configuration.models.base import (
    ProvenanceClass,
    QtrosConfig,
    StrictDecimal,
    provenance,
)
from qtros.domain.enums import SignificanceTier, Timeframe

STANDARD_MIN_TRADES: int = 400
"""Directive 7 floor for a single-instrument strategy."""

LOW_FREQUENCY_MIN_TRADES: int = 60
"""Floor for a low-frequency strategy pooled across instruments."""

LOW_FREQUENCY_MIN_INSTRUMENTS: int = 4

_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_MIN_HYPOTHESIS_LENGTH: int = 40


class SignificancePolicyConfig(QtrosConfig):
    """Trade-count bar the strategy will be judged against (C-2).

    Attributes:
        tier: Standard single-instrument or pooled low-frequency.
        min_trades: Closed trades required before the gate can be evaluated.
        pooling_instruments: Instruments pooled under the low-frequency tier.
    """

    __provenance__ = ProvenanceClass.RUN_AFFECTING  # G-5

    tier: SignificanceTier
    min_trades: int = Field(gt=0)
    pooling_instruments: frozenset[str] = frozenset()

    @property
    def is_low_frequency(self) -> bool:
        """Return the low-frequency flag (G-5: run-affecting)."""
        return self.tier is SignificanceTier.LOW_FREQUENCY

    @model_validator(mode="after")
    def _tier_rules(self) -> SignificancePolicyConfig:
        """Enforce the tier constraints from ARCH-002 §4.5.

        Returns:
            The validated model.

        Raises:
            ValueError: If the tier and the trade or pooling counts disagree.
        """
        if self.tier is SignificanceTier.STANDARD:
            if self.min_trades != STANDARD_MIN_TRADES:
                raise ValueError(f"STANDARD tier requires min_trades == {STANDARD_MIN_TRADES}")
            if self.pooling_instruments:
                raise ValueError("STANDARD tier must not pool instruments")
        else:
            if self.min_trades < LOW_FREQUENCY_MIN_TRADES:
                raise ValueError(
                    f"LOW_FREQUENCY tier requires min_trades >= {LOW_FREQUENCY_MIN_TRADES}"
                )
            if len(self.pooling_instruments) < LOW_FREQUENCY_MIN_INSTRUMENTS:
                raise ValueError(
                    "LOW_FREQUENCY tier requires at least "
                    f"{LOW_FREQUENCY_MIN_INSTRUMENTS} pooled instruments"
                )
        return self


class StrategyConfig(QtrosConfig):
    """One registered strategy at one version, with its parameters.

    Attributes:
        strategy_id: Registry key (D-4). Never an import path.
        strategy_version: Semantic version of the registered logic.
        experiment_id: Registry key for the results index.
        mechanism_hypothesis: Directive 5 — why this is expected to work. Required and
            non-trivial in length, so a backtest cannot start without it written down.
        supported_timeframes: Bar intervals the logic is defined for.
        parameters: Strategy parameters. Decimals quoted; floats rejected.
        significance: The significance policy (run-affecting under G-5).
        declared_at_utc: When the hypothesis and policy were declared. Hash-relevant, so
            a post-hoc edit is visible as a later declaration.
    """

    strategy_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    strategy_version: str
    experiment_id: str = Field(min_length=1)
    mechanism_hypothesis: str = Field(min_length=_MIN_HYPOTHESIS_LENGTH)
    supported_timeframes: frozenset[Timeframe]
    parameters: Mapping[str, StrictDecimal | int | bool | str] = Field(default_factory=dict)
    significance: SignificancePolicyConfig = Field(**provenance(ProvenanceClass.RUN_AFFECTING))
    declared_at_utc: datetime

    @field_validator("strategy_version")
    @classmethod
    def _semver(cls, value: str) -> str:
        """Require ``MAJOR.MINOR.PATCH``."""
        if not _SEMVER.match(value):
            raise ValueError("strategy_version must be MAJOR.MINOR.PATCH")
        return value

    @field_validator("declared_at_utc")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        """Require a timezone-aware declaration timestamp."""
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("declared_at_utc must be timezone-aware")
        return value

    @field_validator("supported_timeframes")
    @classmethod
    def _non_empty(cls, value: frozenset[Timeframe]) -> frozenset[Timeframe]:
        """Require at least one timeframe."""
        if not value:
            raise ValueError("supported_timeframes must not be empty")
        return value
