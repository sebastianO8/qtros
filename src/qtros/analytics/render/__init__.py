"""HTML report rendering (Layer 5; ARCH-002 §6.1 ``report.html``).

The report is a self-contained HTML file: inline CSS, an inline SVG equity curve, and no
script or remote asset, so it opens identically in five years from a cold archive. Every
figure on it comes from :class:`~qtros.analytics.PerformanceReport` and the run result;
the renderer computes nothing new.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal
from importlib import resources
from typing import Final

from jinja2 import Environment, StrictUndefined, select_autoescape

from qtros.analytics import PerformanceReport
from qtros.domain.execution import EquitySnapshot
from qtros.engine.backtester import BacktestResult

_WIDTH: Final[int] = 1040
_HEIGHT: Final[int] = 300
_PAD: Final[int] = 40


def _environment() -> Environment:
    """Return a strict, autoescaping Jinja2 environment."""
    return Environment(
        autoescape=select_autoescape(default=True),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _scale(values: Sequence[Decimal], lo: Decimal, hi: Decimal, size: int, pad: int, *, invert: bool) -> list[Decimal]:
    """Map values onto pixel coordinates."""
    span = hi - lo if hi != lo else Decimal("1")
    out: list[Decimal] = []
    for v in values:
        frac = (v - lo) / span
        pixel = pad + frac * (size - 2 * pad)
        out.append((size - pixel) if invert else pixel)
    return out


def equity_curve_svg(curve: Sequence[EquitySnapshot]) -> str:
    """Draw the mark-to-market and closed equity curves as an inline SVG.

    Args:
        curve: Snapshots in chronological order.

    Returns:
        SVG markup. An empty curve yields an empty chart with axes only.
    """
    if not curve:
        return f'<svg width="{_WIDTH}" height="{_HEIGHT}" viewBox="0 0 {_WIDTH} {_HEIGHT}"></svg>'

    mtm = [s.mtm_equity for s in curve]
    closed = [s.closed_equity for s in curve]
    lo = min(min(mtm), min(closed))
    hi = max(max(mtm), max(closed))
    xs = _scale([Decimal(i) for i in range(len(curve))], Decimal(0), Decimal(max(len(curve) - 1, 1)), _WIDTH, _PAD, invert=False)
    y_mtm = _scale(mtm, lo, hi, _HEIGHT, _PAD, invert=True)
    y_closed = _scale(closed, lo, hi, _HEIGHT, _PAD, invert=True)

    def path(ys: Sequence[Decimal]) -> str:
        return " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys, strict=True))

    first, last = curve[0].ts_utc, curve[-1].ts_utc
    return (
        f'<svg width="{_WIDTH}" height="{_HEIGHT}" viewBox="0 0 {_WIDTH} {_HEIGHT}" role="img" aria-label="equity curve">'
        f'<line x1="{_PAD}" y1="{_HEIGHT - _PAD}" x2="{_WIDTH - _PAD}" y2="{_HEIGHT - _PAD}" stroke="#d9dee3"/>'
        f'<line x1="{_PAD}" y1="{_PAD}" x2="{_PAD}" y2="{_HEIGHT - _PAD}" stroke="#d9dee3"/>'
        f'<polyline fill="none" stroke="#8a94a6" stroke-width="1.5" points="{path(y_closed)}"/>'
        f'<polyline fill="none" stroke="#2b6cb0" stroke-width="1.5" points="{path(y_mtm)}"/>'
        f'<text x="{_PAD}" y="{_PAD - 8}" font-size="11" fill="#5b6670">{hi}</text>'
        f'<text x="{_PAD}" y="{_HEIGHT - _PAD + 14}" font-size="11" fill="#5b6670">{lo}</text>'
        f'<text x="{_PAD}" y="{_HEIGHT - 8}" font-size="11" fill="#5b6670">{first:%Y-%m-%d %H:%M}</text>'
        f'<text x="{_WIDTH - _PAD}" y="{_HEIGHT - 8}" font-size="11" fill="#5b6670" text-anchor="end">{last:%Y-%m-%d %H:%M}</text>'
        "</svg>"
    )


def render_report(
    report: PerformanceReport,
    result: BacktestResult,
    *,
    run_id: str,
    hashes: Mapping[str, str],
    title: str | None = None,
) -> str:
    """Render the HTML report.

    Args:
        report: Layer 5 scoring.
        result: The run.
        run_id: Run identifier for the header.
        hashes: ``run_hash``, ``report_hash``, ``config_hash`` for the footer.
        title: Page title. Defaults to the strategy and run id.

    Returns:
        A complete HTML document.
    """
    template_text = resources.files("qtros.analytics.render").joinpath("report.html.j2").read_text(encoding="utf-8")
    template = _environment().from_string(template_text)
    rejections = sorted(Counter(str(r.code) for r in result.rejected_intents).items())
    return template.render(
        title=title or f"{report.strategy_id} {report.strategy_version} — {run_id}",
        strategy_id=report.strategy_id,
        strategy_version=report.strategy_version,
        reporting_id=report.reporting_id,
        run_id=run_id,
        trusted=report.trusted,
        watermarks=report.gates.watermarks,
        gates=report.gates,
        trades=report.trades,
        sharpe=report.sharpe,
        drawdown=report.drawdown,
        concentration=report.concentration,
        attribution=report.attribution,
        equity_svg=equity_curve_svg(result.equity_curve),
        bars=result.bars_processed,
        risk_events=result.risk_events,
        rejections=rejections,
        reconciliation=result.reconciliation,
        reconciliation_summary=result.reconciliation.summary(),
        open_trade=result.open_trade_at_end,
        run_hash=hashes.get("run_hash", ""),
        report_hash=hashes.get("report_hash", ""),
        config_hash=hashes.get("config_hash", ""),
        feature_set_hash=result.feature_set_hash,
        scored_at=report.scored_at_utc.isoformat(),
    )


__all__ = ["equity_curve_svg", "render_report"]
