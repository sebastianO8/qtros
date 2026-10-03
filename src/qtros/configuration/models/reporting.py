"""Reporting configuration (ARCH-001 C-3, C-5; ARCH-002 §4.7).

Everything here is **report-affecting** (D-1): it changes how a fixed ledger is scored and
nothing about which fills occur. Constitutional thresholds live in a versioned YAML file
rather than Python constants so that a result carries evidence of which rulebook scored it.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field, model_validator

from qtros.configuration.models.base import ProvenanceClass, QtrosConfig, StrictDecimal
from qtros.domain.enums import PeriodAttribution, ReturnBasis, ReturnType


class SharpeConventionConfig(QtrosConfig):
    """Sharpe ratio convention (C-5: daily, arithmetic, 252, rf = 0).

    Attributes:
        return_basis: Equity series differenced to form returns.
        return_type: Arithmetic or log returns.
        annualization_factor: Periods per year.
        risk_free_rate: Annual risk-free rate subtracted before scaling.
        exclude_non_trading_days: Drop calendar days with no session.
    """

    __provenance__ = ProvenanceClass.REPORT_AFFECTING

    return_basis: ReturnBasis = ReturnBasis.DAILY_CLOSED_EQUITY
    return_type: ReturnType = ReturnType.ARITHMETIC
    annualization_factor: int = Field(gt=0, default=252)
    risk_free_rate: StrictDecimal = Decimal("0.0")
    exclude_non_trading_days: bool = True


class ConcentrationConfig(QtrosConfig):
    """The two named concentration tests (C-3).

    Attributes:
        top_n_trades: How many top trades the test sums.
        quarterly_threshold_pct: Gate: share of a quarter's profit from the top N.
        lifetime_threshold_pct: Kill criterion: share of lifetime profit from the top N.
        boundary_is_inclusive: Whether exactly-at-threshold fails. Tested explicitly.
        trade_period_attribution: Which date assigns a trade to a period.
    """

    __provenance__ = ProvenanceClass.REPORT_AFFECTING

    top_n_trades: int = Field(gt=0, default=5)
    quarterly_threshold_pct: StrictDecimal = Decimal("60.0")
    lifetime_threshold_pct: StrictDecimal = Decimal("60.0")
    boundary_is_inclusive: bool = False
    trade_period_attribution: PeriodAttribution = PeriodAttribution.CLOSE_DATE


class GateThresholdsConfig(QtrosConfig):
    """Constitutional pass/fail thresholds.

    Attributes:
        min_sharpe: Minimum annualised Sharpe.
        min_profit_factor: Minimum gross profit over gross loss.
        max_drawdown_pct: Maximum peak-to-trough drawdown accepted.
        oos_sharpe_decay_max_pct: Maximum out-of-sample Sharpe decay accepted.
        wfo_min_profit_factor: Minimum walk-forward profit factor.
    """

    __provenance__ = ProvenanceClass.REPORT_AFFECTING

    min_sharpe: StrictDecimal = Decimal("1.3")
    min_profit_factor: StrictDecimal = Decimal("1.4")
    max_drawdown_pct: StrictDecimal = Decimal("10.0")
    oos_sharpe_decay_max_pct: StrictDecimal = Decimal("30.0")
    wfo_min_profit_factor: StrictDecimal = Decimal("1.15")

    @model_validator(mode="after")
    def _positive(self) -> GateThresholdsConfig:
        """Require positive thresholds."""
        for name in (
            "min_sharpe",
            "min_profit_factor",
            "max_drawdown_pct",
            "oos_sharpe_decay_max_pct",
            "wfo_min_profit_factor",
        ):
            if getattr(self, name) <= Decimal("0"):
                raise ValueError(f"{name} must be positive")
        return self


class ReportingConfig(QtrosConfig):
    """Top-level report-affecting bundle.

    Attributes:
        reporting_id: Key; matches the file under ``config/reporting/``. Named for the
            constitution version it encodes, e.g. ``constitution_v3_2``.
        sharpe: Sharpe convention.
        concentration: Concentration test parameters.
        gates: Pass/fail thresholds.
    """

    __provenance__ = ProvenanceClass.REPORT_AFFECTING

    reporting_id: str = Field(min_length=1)
    sharpe: SharpeConventionConfig = SharpeConventionConfig()
    concentration: ConcentrationConfig = ConcentrationConfig()
    gates: GateThresholdsConfig = GateThresholdsConfig()
