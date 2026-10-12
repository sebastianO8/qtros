"""Report every stale or missing source file in one pass.

Several modules in this repository were delivered more than once as the architecture
evolved. Holding an older copy of one produces two very different failures:

* **Loud** — an ``ImportError`` at startup, because a symbol added later is missing.
  Annoying, but self-announcing.
* **Silent** — the module imports and runs while computing the wrong answer. A
  pre-ARCH-006 ``portfolio.py`` still has every attribute the new one has, so it imports
  cleanly and then double-charges spread and slippage on every fill: roughly 8 basis
  points per round turn, with no exception anywhere.

The second kind is why this script exists. It parses each file rather than importing it,
so it reports *all* problems at once instead of one per run, and it is insensitive to
whitespace, line endings, and comment drift introduced by copy-paste.

Usage:
    python scripts/check_sources.py

Exit code is 0 when every file is current, 1 otherwise.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"

# Marker symbols added in later revisions. A file missing any of these is stale.
# "Class.member" checks a method or attribute inside that class.
REQUIRED: dict[str, tuple[str, ...]] = {
    "qtros/domain/errors.py": ("FeatureError", "LookaheadDetected", "DetectorVerificationError"),
    "qtros/domain/enums.py": ("AssetClass", "Direction", "ExitReason", "OrderRejectionCode"),
    "qtros/domain/identifiers.py": ("SequentialIdFactory",),
    "qtros/domain/money.py": ("MONEY_EXPONENT", "quantize_money", "quantize_price", "apportion"),
    "qtros/domain/execution.py": ("ExecutionFill.cash_costs", "ExecutionFill.implicit_costs"),
    "qtros/domain/feature_contracts.py": ("BASE_COLUMNS", "SealedFeature", "SealedFeatureSet"),
    "qtros/domain/instrument.py": ("InstrumentSpec",),
    "qtros/domain/market.py": ("BarData",),
    "qtros/domain/orders.py": ("RISK_APPROVAL", "ApprovedOrder", "TradeIntent"),
    "qtros/domain/signals.py": ("BarContext", "FeatureRow", "UnknownFeatureError"),
    "qtros/domain/data_contracts.py": (
        "SourceSpec",
        "SourceSpec.aggregate_to",
        "SourceSpec.stored_timeframe",
        "CoverageReport",
        "DatasetFingerprint",
    ),
    "qtros/data/store.py": ("WarehouseStore",),
    "qtros/data/ingestion.py": ("IngestionService", "OUTPUT_DIR_MARKER"),
    "qtros/data/coverage.py": ("violation_counts", "analyze_coverage"),
    "qtros/data/fingerprint.py": ("compute_fingerprint", "FINGERPRINT_ALGORITHM_VERSION"),
    "qtros/features/pipeline.py": (
        "FeaturePipeline._run_conformance_checks",
        "resolve_warmup",
    ),
    "qtros/features/lag_audit.py": ("LagAuditor", "verify_detector"),
    "qtros/features/transforms/base.py": ("TransformBase", "seal_features"),
    "qtros/features/transforms/atr.py": ("TrueRange._build", "AverageTrueRange._build"),
    "qtros/features/transforms/vwap.py": ("AnchoredVwap._build",),
    "qtros/features/transforms/zscore.py": ("RollingZScore._build",),
    "qtros/features/transforms/_control.py": ("CenteredMeanControl._build",),
    "qtros/engine/context.py": ("BarContextFactory", "AVAILABILITY_LAG_BARS"),
    "qtros/engine/portfolio.py": ("Portfolio", "FillOutcome"),
    "qtros/execution/friction.py": ("StandardFrictionModel", "FrictionCharges"),
    "qtros/execution/broker.py": ("SimulatedBroker._derive_implicit_costs",),
    # ARCH-008 — configuration, risk, alpha, run loop
    "qtros/configuration/models/base.py": (
        "ProvenanceClass",
        "StrictDecimal",
        "QtrosConfig",
        "provenance",
    ),
    "qtros/configuration/models/strategy.py": ("SignificancePolicyConfig", "StrategyConfig"),
    "qtros/configuration/models/risk.py": ("RiskLimitsConfig",),
    "qtros/configuration/hashing.py": (
        "ConfigHashes",
        "canonical_json",
        "partition",
        "compute_hashes",
    ),
    "qtros/configuration/loader.py": ("ConfigLoader", "ResolvedBacktestConfig", "MIGRATIONS"),
    "qtros/configuration/registry.py": ("StrategyRegistry", "strategy_registry"),
    "qtros/risk/limits.py": ("MaxLotRule", "MaxNotionalExposureRule"),
    "qtros/risk/sizing.py": ("FixedFractionalSizer",),
    "qtros/risk/engine.py": (
        "RiskEngine",
        "RiskEngine.on_bar_close",
        "RiskEngine.approve",
        "RiskEvent",
    ),
    "qtros/alpha/base.py": ("StrategyBase", "register_strategy", "verify_feature_provenance"),
    "qtros/engine/backtester.py": ("Backtester", "Backtester._step", "BacktestResult"),
    # ARCH-009 — timekeeping, wiring, Layer 5, persistence
    "qtros/domain/timekeeping/calendar.py": (
        "SessionCalendar",
        "SessionCalendar.trading_day_for",
        "SessionCalendar.classify_gap",
        "localize",
    ),
    "qtros/domain/manifest.py": ("RunManifest",),
    "qtros/configuration/models/calendar.py": ("CalendarConfig", "CalendarConfig.to_calendar"),
    "qtros/configuration/models/features.py": ("FeatureSetConfig", "TransformRefConfig"),
    "qtros/engine/builder.py": (
        "build_run",
        "compute_features",
        "run_from_config",
        "RunComponents",
    ),
    "qtros/analytics/metrics.py": ("trade_statistics", "sharpe_ratio", "drawdown", "daily_equity"),
    "qtros/analytics/concentration.py": ("concentration", "ConcentrationResult"),
    "qtros/analytics/attribution.py": ("attribute", "friction_attribution"),
    "qtros/analytics/gates.py": ("evaluate_gates", "GateReport"),
    "qtros/persistence/writer.py": ("RunWriter", "RunWriter.write", "run_id_for"),
    "qtros/persistence/serialize.py": ("to_jsonable",),
    # Phase 1 finish — CLI, workflow, render, registry
    "qtros/cli.py": ("app", "validate", "run", "ingest", "registry"),
    "qtros/workflow.py": ("execute", "load_bars", "ingest_source", "BASE_INPUT_COLUMNS"),
    "qtros/logging_setup.py": ("configure_logging",),
    "qtros/analytics/render/__init__.py": ("render_report", "equity_curve_svg"),
    "qtros/persistence/registry.py": (
        "scan_results",
        "export_registry",
        "render_registry_markdown",
    ),
    "qtros/data/aggregate.py": ("aggregate_bars", "validate_aggregation"),
}

# Source text that must appear, for changes that add no new top-level symbol.
REQUIRED_TEXT: dict[str, tuple[tuple[str, str], ...]] = {
    "qtros/engine/portfolio.py": (
        ("quantize_money", "ARCH-006 precision quantization (ASS-007)"),
        ("fill.cash_costs", "ARCH-006 friction double-charge fix (ASS-006)"),
    ),
    "qtros/execution/friction.py": (
        ("spread_price", "ARCH-006 price-embedded friction (ASS-006)"),
    ),
    "qtros/features/transforms/vwap.py": (
        ("TransformBase", "ARCH-007 transform scaffold migration"),
    ),
    "qtros/features/pipeline.py": (("seal_features", "ARCH-007 provenance sealing"),),
    "qtros/risk/engine.py": (
        ("RISK_APPROVAL", "ARCH-008 risk engine is the sole minter of approvals"),
        ("adverse_extreme", "G-2 adverse-first intrabar drawdown basis (ASS-009)"),
    ),
    "qtros/configuration/models/strategy.py": (
        ("ProvenanceClass.RUN_AFFECTING", "G-5 significance policy in the run hash"),
    ),
    "qtros/persistence/writer.py": (
        ("ScoredReport", "persistence must not import analytics (sibling layers)"),
    ),
    "qtros/data/coverage.py": (
        ("is_not_null() & (", "null volume is unknown, not a NEGATIVE_VOLUME violation"),
    ),
    "qtros/workflow.py": (
        ("BASE_INPUT_COLUMNS", "bookkeeping columns dropped before the G-4 seal check"),
    ),
    "qtros/engine/backtester.py": (
        ("verify_feature_provenance", "G-4 consumer-side DIR-001 check"),
        ("_resolve_protective_exit", "G-3 step 2 resting exits before the strategy"),
    ),
    "qtros/features/transforms/atr.py": (
        ("TransformBase", "ARCH-007 transform scaffold migration"),
        (
            "min_samples=",
            "polars >= 1.21 rolling keyword; min_periods raises under filterwarnings=error",
        ),
    ),
    "qtros/features/transforms/zscore.py": (
        ("TransformBase", "ARCH-007 transform scaffold migration"),
        ("min_samples=", "polars >= 1.21 rolling keyword"),
    ),
    "qtros/features/transforms/_control.py": (
        ("TransformBase", "ARCH-007 transform scaffold migration"),
        ("min_samples=", "polars >= 1.21 rolling keyword"),
    ),
}

EXPECTED_INIT_FILES: tuple[str, ...] = (
    "qtros/__init__.py",
    "qtros/data/__init__.py",
    "qtros/domain/__init__.py",
    "qtros/engine/__init__.py",
    "qtros/execution/__init__.py",
    "qtros/features/__init__.py",
    "qtros/features/transforms/__init__.py",
    "qtros/configuration/__init__.py",
    "qtros/configuration/models/__init__.py",
    "qtros/risk/__init__.py",
    "qtros/alpha/__init__.py",
    "qtros/alpha/strategies/__init__.py",
    "qtros/domain/timekeeping/__init__.py",
    "qtros/analytics/__init__.py",
    "qtros/persistence/__init__.py",
)


def defined_names(tree: ast.Module) -> set[str]:
    """Return every top-level name and ``Class.member`` pair defined in a module."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            names.add(node.name)
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(f"{node.name}.{member.name}")
                elif isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name):
                    names.add(f"{node.name}.{member.target.id}")
                elif isinstance(member, ast.Assign):
                    for target in member.targets:
                        if isinstance(target, ast.Name):
                            names.add(f"{node.name}.{target.id}")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def main() -> int:
    """Check every tracked file and print a per-file verdict."""
    problems: list[str] = []
    checked = 0

    for relative, required in sorted(REQUIRED.items()):
        path = SRC_ROOT / relative
        if not path.exists():
            problems.append(f"MISSING FILE  {relative}")
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            problems.append(f"SYNTAX ERROR  {relative}: line {exc.lineno}: {exc.msg}")
            continue

        checked += 1
        present = defined_names(tree)
        missing = [name for name in required if name not in present]
        if missing:
            problems.append(f"STALE         {relative}: missing {', '.join(missing)}")

        source = path.read_text(encoding="utf-8")
        for needle, why in REQUIRED_TEXT.get(relative, ()):
            if needle not in source:
                problems.append(f"STALE         {relative}: no '{needle}' — {why}")

    for relative in EXPECTED_INIT_FILES:
        if not (SRC_ROOT / relative).exists():
            problems.append(f"MISSING FILE  {relative}  (package marker; may be empty)")

    if problems:
        print(f"{len(problems)} problem(s) found:\n")
        for line in problems:
            print(f"  {line}")
        print("\nRequest the named files; a STALE file is an older revision, not a corrupt one.")
        return 1

    print(
        f"All {checked} tracked modules and {len(EXPECTED_INIT_FILES)} package markers are current."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
