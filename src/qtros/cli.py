"""Thin command-line shell over :mod:`qtros.workflow` (ARCH-002 §2.1).

Nothing here computes anything. Each command resolves configuration, calls one workflow
function, and prints a summary. Errors raised deliberately by the system
(:class:`~qtros.domain.errors.QtrosError`) are reported in one line and exit non-zero;
anything else is a bug and is allowed to traceback.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Final

import typer

from qtros.configuration.loader import ConfigLoader, ResolvedBacktestConfig
from qtros.configuration.models.source import DataSourceConfig
from qtros.domain.enums import IngestMode
from qtros.domain.errors import QtrosError
from qtros.logging_setup import configure_logging

app: Final = typer.Typer(
    name="qtros",
    help="Quantitative Trading Research OS — Phase 1 backtesting.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)

DEFAULT_CONFIG_ROOT: Final[Path] = Path("config")


def _resolve_target(target: str, config_root: Path | None) -> tuple[Path, str]:
    """Turn a backtest identifier or YAML path into ``(config_root, backtest_id)``.

    Accepts ``phase1_mvp``, ``config/backtest/phase1_mvp.yaml``, or any ``*.yaml`` path
    whose parent is a ``backtest/`` directory under a config root.
    """
    candidate = Path(target)
    if candidate.suffix in {".yaml", ".yml"} or candidate.is_file():
        root = candidate.parent.parent if candidate.parent.name == "backtest" else (config_root or candidate.parent)
        return root, candidate.stem
    return config_root or DEFAULT_CONFIG_ROOT, target


def _load(target: str, config_root: Path | None) -> ResolvedBacktestConfig:
    """Resolve a backtest, importing the strategy library so registrations exist."""
    import qtros.alpha.strategies.vwap_pullback  # noqa: F401, PLC0415 — registration side effect

    root, backtest_id = _resolve_target(target, config_root)
    return ConfigLoader(root).load_backtest(backtest_id)


def _fail(exc: QtrosError) -> None:
    """Report a deliberate system error and exit non-zero."""
    typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
    raise typer.Exit(code=1)


ConfigRoot = Annotated[Path | None, typer.Option("--config-root", help="Config tree root (default: ./config).")]
LogLevel = Annotated[str, typer.Option("--log-level", help="DEBUG, INFO, WARNING, ERROR.")]
LogFormat = Annotated[str, typer.Option("--log-format", help="console or json.")]


@app.command()
def validate(
    target: Annotated[str, typer.Argument(help="Backtest id or path to its YAML.")],
    config_root: ConfigRoot = None,
    log_level: LogLevel = "WARNING",
    log_format: LogFormat = "console",
) -> None:
    """Resolve a backtest composition and print its Directive 10 hashes."""
    configure_logging(log_level, log_format)
    try:
        resolved = _load(target, config_root)
    except QtrosError as exc:
        _fail(exc)
        return
    hashes = resolved.hashes()
    typer.echo(f"backtest      {resolved.backtest.run_label}")
    typer.echo(f"instrument    {resolved.instrument.instrument_id} ({resolved.source.timeframe})")
    typer.echo(f"strategy      {resolved.strategy.strategy_id} {resolved.strategy.strategy_version}")
    typer.echo(f"features      {resolved.features.feature_set_id}: {', '.join(t.transform for t in resolved.features.transforms)}")
    typer.echo(f"risk          {resolved.risk.risk_limits_id}   friction {resolved.friction.friction_id}   reporting {resolved.reporting.reporting_id}")
    typer.echo(f"run_hash      {hashes.run_hash}")
    typer.echo(f"report_hash   {hashes.report_hash}")
    typer.echo(f"config_hash   {hashes.config_hash}")


@app.command()
def run(
    target: Annotated[str, typer.Argument(help="Backtest id or path to its YAML.")],
    config_root: ConfigRoot = None,
    warehouse: Annotated[Path | None, typer.Option("--warehouse", help="DuckDB file (default: backtest paths.warehouse).")] = None,
    results_root: Annotated[Path | None, typer.Option("--results-root", help="Results tree (default: backtest paths.results_root).")] = None,
    no_write: Annotated[bool, typer.Option("--no-write", help="Run and score without writing artifacts.")] = False,
    no_render: Annotated[bool, typer.Option("--no-render", help="Skip report.html.")] = False,
    allow_gaps: Annotated[bool, typer.Option("--allow-gaps", help="Proceed on unclean coverage, under a watermark.")] = False,
    repo_root: Annotated[Path | None, typer.Option("--repo-root", help="Git repository to stamp (default: cwd).")] = None,
    log_level: LogLevel = "INFO",
    log_format: LogFormat = "console",
) -> None:
    """Run a backtest end to end against ingested data and write results/{run_id}/."""
    from qtros.workflow import execute  # noqa: PLC0415 — keep validate/registry fast to import

    try:
        resolved = _load(target, config_root)
        configure_logging(
            log_level or resolved.backtest.runtime.log_level,
            log_format or resolved.backtest.runtime.log_format,
        )
        outcome = execute(
            resolved,
            warehouse_path=warehouse or resolved.backtest.paths.warehouse,
            results_root=results_root or resolved.backtest.paths.results_root,
            repo_root=repo_root or Path.cwd(),
            write=not no_write,
            render=not no_render,
            allow_gaps=allow_gaps,
        )
    except QtrosError as exc:
        _fail(exc)
        return

    report = outcome.report
    typer.echo(f"run_id        {outcome.run_id}")
    typer.echo(f"bars          {outcome.result.bars_processed}   fills {len(outcome.result.fills)}   trades {report.trades.trade_count}")
    typer.echo(f"net_pnl       {report.trades.net_pnl}   max_dd {report.drawdown.max_drawdown_pct}%   sharpe {report.sharpe.sharpe if report.sharpe.sharpe is not None else 'n/a'}")
    typer.echo(f"reconciled    {outcome.result.reconciliation.passed}   trusted {outcome.written.manifest.trusted if outcome.written else report.trusted}")
    typer.echo(f"gates         {report.gates.overall}   publishable {report.gates.publishable}")
    for mark in report.gates.watermarks:
        typer.secho(f"watermark     {mark}", fg=typer.colors.YELLOW)
    if outcome.written is not None:
        typer.echo(f"written       {outcome.written.directory}")


@app.command()
def ingest(
    source_id: Annotated[str, typer.Argument(help="Source id under config/sources/.")],
    config_root: ConfigRoot = None,
    warehouse: Annotated[Path, typer.Option("--warehouse")] = Path("./data_lake/warehouse/qtros.duckdb"),
    input_root: Annotated[Path, typer.Option("--input-root")] = Path("./data_lake/raw"),
    results_root: Annotated[Path, typer.Option("--results-root")] = Path("./results"),
    mode: Annotated[IngestMode, typer.Option("--mode", help="APPEND_ONLY, REPLACE_RANGE, or DRY_RUN.")] = IngestMode.APPEND_ONLY,
    log_level: LogLevel = "INFO",
    log_format: LogFormat = "console",
) -> None:
    """Register and ingest a configured raw source into the warehouse."""
    from qtros.workflow import ingest_source  # noqa: PLC0415

    configure_logging(log_level, log_format)
    try:
        source = ConfigLoader(config_root or DEFAULT_CONFIG_ROOT).load_one("source", source_id, DataSourceConfig)
        report = ingest_source(
            source, warehouse_path=warehouse, input_root=input_root, results_root=results_root, mode=mode
        )
    except QtrosError as exc:
        _fail(exc)
        return
    typer.echo(f"source        {report.source_id}   mode {report.mode}")
    typer.echo(f"files         {len(report.files_processed)}   rows read {report.rows_read}   written {report.rows_written}")
    typer.echo(f"skipped       {report.rows_skipped_duplicate}   rejected {report.rows_rejected}   deleted {report.rows_deleted}")
    typer.echo(f"range         {report.first_ts_utc} → {report.last_ts_utc}")


@app.command()
def registry(
    results_root: Annotated[Path, typer.Option("--results-root")] = Path("./results"),
    output: Annotated[Path, typer.Option("--output", help="Markdown destination.")] = Path("docs/registry.md"),
    decisions: Annotated[Path, typer.Option("--decisions", help="Hand-authored decisions YAML.")] = Path("config/registry/decisions.yaml"),
    log_level: LogLevel = "WARNING",
    log_format: LogFormat = "console",
) -> None:
    """Rebuild the derived strategy registry from results/."""
    from qtros.persistence.registry import export_registry  # noqa: PLC0415

    configure_logging(log_level, log_format)
    try:
        exported = export_registry(results_root, output, decisions_path=decisions)
    except QtrosError as exc:
        _fail(exc)
        return
    typer.echo(f"runs          {len(exported.entries)}")
    typer.echo(f"markdown      {exported.markdown_path}")
    typer.echo(f"json          {exported.json_path}")


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
