"""Constitutional gate evaluation (ARCH-002 §4.7; Directives 7 and 8).

Thresholds come from ``config/reporting/*.yaml``, never from constants here, so a result
carries evidence of which rulebook scored it. Each gate yields a verdict rather than a
boolean: a metric computed on forty trades is not a pass or a fail, it is an
insufficient sample, and collapsing that to a boolean is how a promising early number
becomes a false belief.

Two gates — out-of-sample Sharpe decay and walk-forward profit factor — cannot be
evaluated from a single in-sample run and are reported ``NOT_EVALUATED``. A run is
*publishable* only when every in-sample gate passes, nothing is unevaluated by reason of
sample size, and no watermark is present.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from qtros.domain.enums import GateVerdict

if TYPE_CHECKING:
    from collections.abc import Sequence

    from qtros.analytics.concentration import ConcentrationResult
    from qtros.analytics.metrics import DrawdownResult, SharpeResult, TradeStatistics
    from qtros.configuration.models.reporting import ReportingConfig
    from qtros.configuration.models.strategy import SignificancePolicyConfig

WATERMARK_UNTRUSTED: Final[str] = "UNTRUSTED: run did not reconcile"
WATERMARK_SAME_BAR_CLOSE: Final[str] = "SAME_BAR_CLOSE execution timing"
WATERMARK_FAVOURABLE_FIRST: Final[str] = "FAVOURABLE_FIRST path assumption"
WATERMARK_HALTED: Final[str] = "risk halt active at end of run"


@dataclass(frozen=True, slots=True)
class GateCheck:
    """One gate's outcome.

    Attributes:
        name: Gate identifier.
        verdict: The outcome.
        observed: The measured value, when there is one.
        threshold: The threshold applied, when there is one.
        detail: Human-readable explanation.
    """

    name: str
    verdict: GateVerdict
    observed: Decimal | None
    threshold: Decimal | None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class GateReport:
    """Every gate, the kill criterion, watermarks, and the overall verdict.

    Attributes:
        checks: Per-gate outcomes.
        kill_criterion: The lifetime concentration test.
        overall: ``FAIL`` if any gate failed; else ``INSUFFICIENT_SAMPLE`` if any gate
            was starved; else ``PASS`` over the gates that could be evaluated.
        not_evaluated: Gates a single run cannot evaluate.
        watermarks: Conditions that disqualify the result from publication.
        sample_size: Closed trades.
        required_sample: Significance floor.
        reporting_id: Which rulebook scored the run.
    """

    checks: tuple[GateCheck, ...]
    kill_criterion: GateCheck
    overall: GateVerdict
    not_evaluated: tuple[str, ...]
    watermarks: tuple[str, ...]
    sample_size: int
    required_sample: int
    reporting_id: str

    @property
    def publishable(self) -> bool:
        """Return whether the result may be recorded as a validated pass."""
        return (
            self.overall is GateVerdict.PASS
            and not self.watermarks
            and self.kill_criterion.verdict is not GateVerdict.FAIL
        )


def _metric_gate(
    name: str,
    observed: Decimal | None,
    threshold: Decimal,
    *,
    sufficient: bool,
    passes: bool,
    undefined_reason: str = "",
) -> GateCheck:
    """Build a metric gate check with sample-size awareness."""
    if observed is None:
        return GateCheck(
            name,
            GateVerdict.INSUFFICIENT_SAMPLE if not sufficient else GateVerdict.FAIL,
            None,
            threshold,
            undefined_reason or "metric undefined",
        )
    if not sufficient:
        return GateCheck(
            name,
            GateVerdict.INSUFFICIENT_SAMPLE,
            observed,
            threshold,
            "computed, but below the significance floor",
        )
    return GateCheck(name, GateVerdict.PASS if passes else GateVerdict.FAIL, observed, threshold)


def evaluate_gates(
    *,
    stats: TradeStatistics,
    sharpe: SharpeResult,
    drawdown: DrawdownResult,
    concentration: ConcentrationResult,
    reporting: ReportingConfig,
    significance: SignificancePolicyConfig,
    watermarks: Sequence[str] = (),
) -> GateReport:
    """Score a run against its rulebook.

    Args:
        stats: Trade statistics.
        sharpe: Sharpe result.
        drawdown: Drawdown result.
        concentration: Both concentration tests.
        reporting: Thresholds and conventions.
        significance: Trade-count floor (G-5: part of the run identity).
        watermarks: Conditions already known to disqualify the run.

    Returns:
        The gate report.
    """
    gates = reporting.gates
    sufficient = stats.trade_count >= significance.min_trades

    checks: list[GateCheck] = [
        _metric_gate(
            "min_sharpe",
            sharpe.sharpe,
            gates.min_sharpe,
            sufficient=sufficient,
            passes=sharpe.sharpe is not None and sharpe.sharpe >= gates.min_sharpe,
            undefined_reason=sharpe.reason,
        ),
        _metric_gate(
            "min_profit_factor",
            stats.profit_factor,
            gates.min_profit_factor,
            sufficient=sufficient,
            passes=stats.profit_factor is not None
            and stats.profit_factor >= gates.min_profit_factor,
            undefined_reason="no losing trades" if stats.gross_loss == 0 else "",
        ),
        # Drawdown is evaluated regardless of sample size: a breach is a breach.
        GateCheck(
            "max_drawdown_pct",
            GateVerdict.PASS
            if drawdown.max_drawdown_pct <= gates.max_drawdown_pct
            else GateVerdict.FAIL,
            drawdown.max_drawdown_pct,
            gates.max_drawdown_pct,
        ),
    ]

    breaches = concentration.quarterly_breaches
    worst = max(
        (q.top_n_share_pct for q in concentration.quarterly if q.top_n_share_pct is not None),
        default=None,
    )
    if not sufficient:
        verdict = GateVerdict.INSUFFICIENT_SAMPLE
    elif worst is None:
        verdict = GateVerdict.FAIL
    else:
        verdict = GateVerdict.FAIL if breaches else GateVerdict.PASS
    checks.append(
        GateCheck(
            "quarterly_concentration",
            verdict,
            worst,
            reporting.concentration.quarterly_threshold_pct,
            f"breached quarters: {', '.join(breaches)}"
            if breaches
            else ("no evaluable quarter" if worst is None else ""),
        )
    )

    checks.append(
        GateCheck(
            "significance_sample",
            GateVerdict.PASS if sufficient else GateVerdict.INSUFFICIENT_SAMPLE,
            Decimal(stats.trade_count),
            Decimal(significance.min_trades),
            f"tier {significance.tier}",
        )
    )

    not_evaluated = ("oos_sharpe_decay_max_pct", "wfo_min_profit_factor")
    checks.extend(
        GateCheck(
            name,
            GateVerdict.NOT_EVALUATED,
            None,
            getattr(gates, name),
            "requires out-of-sample or walk-forward runs",
        )
        for name in not_evaluated
    )

    lifetime = concentration.lifetime
    if lifetime.breached is None:
        kill = GateCheck(
            "lifetime_concentration",
            GateVerdict.NOT_EVALUATED,
            None,
            reporting.concentration.lifetime_threshold_pct,
            "no gross profit to concentrate",
        )
    else:
        kill = GateCheck(
            "lifetime_concentration",
            GateVerdict.FAIL if lifetime.breached else GateVerdict.PASS,
            lifetime.top_n_share_pct,
            reporting.concentration.lifetime_threshold_pct,
            "kill criterion: requires a mechanism_note before the strategy may pass"
            if lifetime.breached
            else "",
        )

    verdicts = {c.verdict for c in checks}
    if GateVerdict.FAIL in verdicts:
        overall = GateVerdict.FAIL
    elif GateVerdict.INSUFFICIENT_SAMPLE in verdicts:
        overall = GateVerdict.INSUFFICIENT_SAMPLE
    else:
        overall = GateVerdict.PASS

    return GateReport(
        checks=tuple(checks),
        kill_criterion=kill,
        overall=overall,
        not_evaluated=not_evaluated,
        watermarks=tuple(watermarks),
        sample_size=stats.trade_count,
        required_sample=significance.min_trades,
        reporting_id=reporting.reporting_id,
    )
