# ARCH-002 — Repository Blueprint, Build Configuration & System Configuration Schema

**Project:** Quantitative Trading Research OS
**Governing document:** Master Project Constitution v3.2 + ARCH-001 PI Rulings (C-1 … C-10 approved, Directive 10 adopted)
**Scope:** Phase 1 — The End-to-End MVP (Weeks 1–4, per capacity ruling)
**Author:** Lead Quantitative Software Engineer (Claude)
**Status:** DRAFT — Section 1 flags require PI ruling; Sections 2–6 are actionable now
**Contains behavioral code:** No. Directory structure, build configuration, and declarative schema definitions only.

---

## Section 0 — Rulings Carried Forward

| ID | Ruling | Where it lands in this document |
|---|---|---|
| C-1 | MTM equity gate, `ADVERSE_FIRST` | `ExecutionConfig.path_assumption`, `RiskLimitsConfig.max_drawdown_basis` |
| C-2 | `SignificancePolicy` in strategy hash | `StrategyConfig.significance` (hash-relevant) |
| C-3 | Two named concentration tests | `ReportingConfig.concentration` |
| C-4 | Fills move cash; Trades are lifecycles | `domain/` package; ledger schema in §6 |
| C-5 | Daily arithmetic, 252, rf=0.0 | `SharpeConventionConfig`, stamped in report |
| C-6 | Layer 3.5 Execution | `src/qtros/execution/` package + import contract |
| C-7 | `InstrumentSpec` from YAML | `config/instruments/*.yaml` + §4.2 |
| C-8 | Directive 10 — run provenance | §5, config hashing mechanics |
| C-9 | Functional strategy core | Strategy registry pattern, §4.5 |
| C-10 | Reconciliation inside `run()` | `engine/` package; no separate CLI path |

Capacity ruling noted: Phase 1 is 4 weeks, Layer 5 reporting is **not** thinned. Appendix C re-sequences the build order accordingly.

---

## Section 1 — New Findings Requiring PI Ruling

Six items surfaced while laying out the repository. Two are blocking in the same sense as before — they change artifact structure, which is expensive to migrate later. The rest are recommendations I'll adopt as defaults unless overridden.

### D-1 (BLOCKING) — Config hash granularity: one hash is the wrong number

Directive 10 requires a config hash. A single hash creates a problem the moment you use it.

Consider: you finish a 3-year backtest, then decide the Sharpe convention should exclude a holiday calendar (C-5 territory). Under one hash, that config edit invalidates the run — the experiment ID changes, and to get a comparable result you must **re-run the entire backtest**, even though not a single trade would differ. At 10–15 hrs/week with multi-year runs, that is a real tax paid repeatedly.

The underlying issue is that configuration splits into two populations with different blast radii:

- **Run-affecting** — anything that changes which fills occur or what they cost: data range, features, strategy params, risk limits, friction, execution timing, path assumption, RNG seed.
- **Report-affecting** — anything that changes how a fixed ledger is *scored*: Sharpe convention, concentration thresholds, significance policy, period-attribution rules.

**Proposed resolution — two hashes, one identity:**

```
run_hash    = H(canonical(run-affecting config))
report_hash = H(canonical(report-affecting config))
config_hash = H(run_hash || report_hash)     # Directive 10's required value
```

`RunManifest` records all three. The practical payoff: metrics can be **recomputed from a stored ledger** whenever `run_hash` is unchanged, so a scoring change costs seconds instead of an evening. Two results with identical `run_hash` and differing `report_hash` are honestly comparable at the trade level and explicitly not comparable at the metric level — which is exactly the distinction you want visible.

A third category — **operational** config (log level, output paths, worker count, progress display) — must be excluded from all hashing. If `log_level: DEBUG` mints a new experiment ID, the registry fills with phantom experiments within a week.

**PI ruling required:** approve the three-way split, or mandate a single hash.

### D-2 (BLOCKING) — YAML silently destroys `Decimal` precision

C-7 and the ledger design specify `Decimal` for all cash and price arithmetic. But PyYAML parses `0.1` as a Python `float` **before** Pydantic ever sees it. `Decimal(0.1)` from a float is `0.1000000000000000055511151231257827…`. Precision is lost at parse time; no downstream validation can recover it.

This defeats the exact-equality reconciliation the whole Directive 6 design rests on.

**Proposed resolution:** all `Decimal`-typed fields are **quoted strings in YAML**, and the Pydantic models use a constrained type that *rejects* float input rather than coercing it:

```yaml
tick_size: "0.001"        # correct
tick_size: 0.001          # ValidationError — float input to a Decimal field
```

This is ugly and it is worth it. A schema test asserts that every `Decimal` field in every model rejects `float` input, so the guard can't rot. Same rule applies to the canonical JSON used for hashing: `Decimal` serializes as a string, never as a float, or the hash becomes platform-dependent.

### D-3 — `SessionCalendar` has no clean home and will create an import cycle

ARCH-001 gave `SessionCalendar` three consumers: `data/` (session anomaly detection), `risk/` (trading-day boundary for MLL and the 50% consistency rule), and `alpha/` (Asian-session logic). If it lives in any one of them, the other two import upward and the layering contract breaks on day one.

It also isn't pure data — it has behavior (`is_open_at`, `trading_day_for`, `session_bounds`), and it owns the DST logic that burned Cycle 1 across three regimes.

**Proposed resolution:** `src/qtros/domain/timekeeping/`. It stays inside `domain/` (so everything may import it) but is the one `domain/` subpackage permitted behavior beyond dataclass methods, on three conditions: no I/O, no filesystem, no config parsing. Calendars are *constructed from* config by a factory in `config/`, and the domain object receives already-parsed rules. `domain/` still depends on nothing but the stdlib and `zoneinfo`.

### D-4 — Strategy loading must not be an import path in YAML

The obvious design is `strategy_class: "qtros.alpha.audjpy.AudJpySweep"` in YAML, resolved by `importlib`. Two problems: a config file becomes arbitrary code execution, and any refactor silently breaks every archived config — including the ones in `results/` that Directive 10 exists to make reproducible.

**Proposed resolution:** a decorator-based registry. Strategies self-register under a stable ID and version; YAML references `strategy_id` + `strategy_version` only. Renaming a class doesn't break a 2026 manifest; **deleting** a registered version does, and the registry can then say so explicitly rather than raising `ModuleNotFoundError` from 2027.

### D-5 — Deep YAML inheritance will make provenance unauditable

YAML anchors, `<<:` merges, and multi-file overlay chains are tempting for sharing risk profiles across strategies. They make it genuinely hard to answer "what values actually ran?" — which is the only question `results/` exists to answer.

**Proposed resolution:** flat composition by reference only. `BacktestConfig` names other configs by ID; the loader resolves them once; the **fully resolved, flattened config** is what gets hashed and what gets written to `results/{run_id}/config_resolved.yaml`. No anchors, no merge keys, one resolution step, one artifact.

### D-6 — Toolchain restraint

A maximal Python toolchain (black + isort + flake8 + pylint + mypy + bandit + pre-commit + tox + nox) is a part-time researcher's second job. I'm proposing a deliberately small set where each tool prevents a failure class this project has already demonstrated:

| Tool | Prevents | Justification |
|---|---|---|
| `ruff` | style drift, unused imports, mutable defaults | Replaces black+isort+flake8 in one fast binary |
| `mypy --strict` | type-contract violations across layers | Mandate §2 requires type hints; unchecked hints are decoration |
| `pytest` + `hypothesis` | logic bugs, OHLC edge cases | Hypothesis generates the malformed bars you wouldn't think to write |
| `import-linter` | layer violations, filesystem leakage | Makes ARCH-001 §1.2's "one door" contract mechanical |
| `pre-commit` | forgetting the above at 11pm | The only one that survives fatigue |

Explicitly declined for Phase 1: `tox`/`nox` (single Python version), `pylint` (overlaps ruff), `bandit` (no untrusted input surface yet — revisit at Phase 4 webhooks), coverage gates (coverage percentage is a metric that invites gaming; the assertion gates in ARCH-001 §5.3 are the real bar).

---

## Section 2 — Repository Blueprint

### 2.1 Directory layout

```
qtros/
├── pyproject.toml                  # build, deps, tool config, import contracts
├── uv.lock                         # committed — Directive 10 needs pinned versions
├── .pre-commit-config.yaml
├── .env.example                    # never .env itself
├── .gitignore
├── README.md
├── CONSTITUTION.md                 # v3.2 + ruling addenda, versioned in-repo
│
├── config/                         # declarative only; no logic, ever
│   ├── instruments/
│   │   ├── audjpy.yaml
│   │   └── cadjpy.yaml             # cross-asset validation (§7 lesson)
│   ├── calendars/
│   │   ├── fx_sunday_open.yaml
│   │   └── cme_equity_index.yaml
│   ├── sources/
│   │   └── dukascopy_m5.yaml       # declared tz offset, schema map
│   ├── features/
│   │   └── baseline_v1.yaml
│   ├── strategies/
│   │   ├── vwap_pullback_v0_2.yaml
│   │   └── audjpy_sweep_v1_0.yaml
│   ├── risk/
│   │   ├── phase1_baseline.yaml
│   │   └── propfirm_eval.yaml      # Phase 2; schema exists now
│   ├── friction/
│   │   └── retail_fx_typical.yaml
│   ├── reporting/
│   │   └── constitution_v3_2.yaml  # gate thresholds live here, not in code
│   └── backtest/
│       └── phase1_mvp.yaml         # top-level composition
│
├── src/qtros/
│   ├── __init__.py
│   ├── domain/                     # LAYER-FREE CORE — depends on stdlib only
│   │   ├── enums.py                # Direction, ExitReason, Timeframe, …
│   │   ├── identifiers.py          # InstrumentId, RunId, TradeId (NewType)
│   │   ├── market.py               # BarData, MarketFrame, FeatureFrame
│   │   ├── instrument.py           # InstrumentSpec
│   │   ├── signals.py              # TradeIntent, BarContext, StrategyDecision
│   │   ├── orders.py               # ApprovedOrder + construction token (C-9/1.5)
│   │   ├── execution.py            # ExecutionFill, PositionState, Trade
│   │   ├── accounting.py           # EquitySnapshot, ReconciliationReport
│   │   ├── reporting.py            # PerformanceReport, ConcentrationResult
│   │   ├── manifest.py             # RunManifest, DatasetFingerprint
│   │   ├── errors.py               # exception hierarchy, single root
│   │   ├── protocols/              # all Protocol definitions from ARCH-001 §2
│   │   └── timekeeping/            # D-3: SessionCalendar, DST logic
│   │
│   ├── configuration/              # Pydantic schema + loader + hashing
│   │   ├── models/                 # one module per config family
│   │   ├── loader.py               # resolve → flatten → validate → freeze
│   │   ├── hashing.py              # canonicalization, run/report/config hash
│   │   └── registry.py             # strategy ID registry (D-4)
│   │
│   ├── data/                       # LAYER 0 — sole filesystem ingress
│   │   ├── sources/                # per-vendor parsers
│   │   ├── ingestion.py
│   │   ├── store.py                # DuckDB access; ONLY module importing duckdb
│   │   ├── coverage.py
│   │   └── fingerprint.py
│   │
│   ├── features/                   # LAYER 0.5
│   │   ├── transforms/             # one module per feature family
│   │   ├── pipeline.py
│   │   └── lag_audit.py            # prefix-invariance detector
│   │
│   ├── alpha/                      # LAYER 1
│   │   ├── base.py                 # registration decorator
│   │   └── strategies/             # one module per strategy, mutually independent
│   │
│   ├── regime/                     # LAYER 2 — Phase 3 stub, empty in Phase 1
│   ├── risk/                       # LAYER 3
│   │   ├── engine.py
│   │   ├── sizing.py
│   │   └── limits.py
│   │
│   ├── execution/                  # LAYER 3.5 (C-6)
│   │   ├── simulator.py
│   │   ├── friction.py
│   │   └── fills.py
│   │
│   ├── portfolio/                  # LAYER 4 — Phase 2 stub
│   ├── engine/                     # orchestration
│   │   ├── backtester.py
│   │   ├── ledger.py               # only writer of cash movements
│   │   └── reconciliation.py       # C-10: called inside run()
│   │
│   ├── analytics/                  # LAYER 5
│   │   ├── metrics/
│   │   ├── concentration.py        # C-3: both tests
│   │   ├── attribution.py
│   │   └── render/                 # Jinja2 templates → HTML
│   │
│   ├── persistence/                # results/ writer, registry index builder
│   ├── logging_setup.py            # structlog config; no print() anywhere
│   └── cli.py                      # thin argparse/typer shell over engine
│
├── tests/
│   ├── unit/                       # mirrors src/qtros/ package for package
│   ├── integration/
│   ├── regression/
│   │   └── cycle1/                 # one test per §7 lesson, named for it
│   ├── property/                   # hypothesis strategies for OHLC, fills
│   ├── fixtures/
│   │   ├── bars/                   # small, committed, hand-verified
│   │   └── expected/               # golden ledgers
│   └── conftest.py
│
├── pine/                           # TradingView sources, versioned with Python
│   └── audjpy_sweep_v1_0.pine
├── results/                        # gitignored; immutable run artifacts
├── data_lake/                      # gitignored; raw + DuckDB
│   ├── raw/                        # inputs — read-only to the app
│   └── warehouse/                  # DuckDB file
└── docs/
    ├── registry.md                 # generated Strategy Registry export (committed)
    └── decisions/                  # ADRs; ARCH-001, ARCH-002 live here
```

### 2.2 Two structural rules that carry the design

**Rule 1 — `domain/` imports nothing from `qtros`.** It depends on the stdlib and `zoneinfo` only. Not Polars, not DuckDB, not Pydantic. This is what makes every other package independently testable with plain constructors instead of fixtures, and it's what mandate §2's dependency-injection requirement actually needs to be true in practice.

The cost is real and worth naming: `MarketFrame` wraps a Polars frame, so `domain/market.py` needs `polars` as a type. **Exception granted to Polars only**, declared in the import contract, because a columnar frame *is* the domain's data representation. DuckDB is not — it's a storage choice — and stays confined to `data/store.py`.

**Rule 2 — one door to the filesystem.** Only `data/` may import `duckdb`, `csv`, `glob`, or open files for market data. `persistence/` is the only other package permitted file writes, and only under `results/`. Enforced by import-linter (Appendix A), not by discipline. This is Cycle 1's self-ingestion bug converted from a convention into a build failure.

### 2.3 Dependency direction

```
        analytics ─┐
                   ├──► engine ──► execution ──► risk ──► alpha ──► features ──► data
        persistence┘                                                              │
                                                                                  ▼
        ┌─────────────────────────  configuration  ────────────────────────┐    domain
        └──────────────────────────────► domain ◄──────────────────────────┘
```

Every arrow points one way. `configuration/` may be imported by anything but imports only `domain/`. Nothing imports `cli.py`.

---

## Section 3 — `pyproject.toml`

Delivered as a working file alongside this document. Annotated highlights:

- **`requires-python = ">=3.11"`** per mandate §2. Not 3.12+, because `polars`/`duckdb`/`xgboost` wheel availability on a school laptop is a real constraint and 3.11 gives `Self`, `LiteralString`, and the exception-group machinery the error hierarchy wants.
- **XGBoost and scikit-learn are in an optional `[ml]` extra.** Phase 3 is explicitly optional per the constitution; a Phase 1 install shouldn't pull ~200 MB of ML wheels. If Phase 3 is skipped entirely — an outcome the constitution names as successful — the dependency is never installed.
- **`uv.lock` is committed.** Directive 10 records library versions in the manifest; without a lockfile that record is descriptive rather than reproducible.
- **Upper bounds on Polars and DuckDB.** Both move fast and both have made breaking changes to APIs this project depends on. A five-year-maintained repo that floats its data layer will not re-run its own 2026 results.
- **`hypothesis` in dev.** Called out because it's the tool that will find the OHLC edge cases in ARCH-001 §5.2 that nobody writes by hand.
- **`[tool.importlinter]` contracts live in `pyproject.toml`** so the layering rules sit next to the dependency list rather than in a file nobody opens.

See Appendix A for the full contract set and Appendix B for `mypy`/`ruff` settings rationale.

---

## Section 4 — System Configuration Schema

All models inherit a frozen, `extra="forbid"` base. `extra="forbid"` matters more than it looks: a typo'd key that's silently ignored produces a backtest that runs cleanly with the wrong parameters, which is the most expensive possible failure — a plausible number you trust.

### 4.1 Base types and provenance marking

```python
# configuration/models/base.py

class ProvenanceClass(StrEnum):
    RUN_AFFECTING    = "run"       # changes which fills occur, or their cost
    REPORT_AFFECTING = "report"    # changes how a fixed ledger is scored
    OPERATIONAL      = "operational"  # excluded from all hashing (D-1)


def provenance(kind: ProvenanceClass) -> dict[str, str]:
    """Field marker consumed by configuration.hashing to partition config."""


# Rejects float input outright (D-2). Accepts str, int, Decimal.
StrictDecimal = Annotated[Decimal, BeforeValidator(reject_float_input)]


class QtrosConfig(BaseModel):
    """Root of every config model.

    model_config: frozen=True, extra="forbid", validate_default=True,
                  use_enum_values=False, arbitrary_types_allowed=False
    """
    schema_version: PositiveInt   # D-? five-year migration hook; see §4.8
```

### 4.2 Instrument (C-7)

```python
class InstrumentConfig(QtrosConfig):
    instrument_id:        InstrumentId
    display_name:         str
    asset_class:          AssetClass
    base_currency:        CurrencyCode
    quote_currency:       CurrencyCode
    tick_size:            StrictDecimal
    contract_size:        StrictDecimal
    min_lot:              StrictDecimal
    lot_step:             StrictDecimal
    typical_spread_ticks: StrictDecimal
    calendar_id:          str            # → config/calendars/
    rollover_time_local:  time
    rollover_tz:          TimeZoneName    # validated against zoneinfo
```

```yaml
# config/instruments/audjpy.yaml
schema_version: 1
instrument_id: AUDJPY
display_name: "Australian Dollar / Japanese Yen"
asset_class: FX_SPOT
base_currency: AUD
quote_currency: JPY
tick_size: "0.001"            # quoted — D-2
contract_size: "100000"
min_lot: "0.01"
lot_step: "0.01"
typical_spread_ticks: "1.8"
calendar_id: fx_sunday_open
rollover_time_local: "17:00:00"
rollover_tz: America/New_York   # NOT UTC — shifts with US DST
```

That last field is the Cycle 1 DST lesson made declarative. The FX trading day boundary follows New York wall-clock, so the UTC boundary moves twice a year. Writing it as a UTC constant would be a magic constant *and* wrong for ~34 weeks of the year.

### 4.3 Session calendar (D-3)

```python
class SessionWindowConfig(QtrosConfig):
    name:        str            # "asian", "london", "ny_overlap"
    open_local:  time
    close_local: time
    timezone:    TimeZoneName
    weekdays:    frozenset[Weekday]


class CalendarConfig(QtrosConfig):
    calendar_id:      str
    week_open:        WeekBoundaryConfig
    week_close:       WeekBoundaryConfig
    sessions:         tuple[SessionWindowConfig, ...]
    holidays:         tuple[date, ...]
    dst_policy:       DstPolicy   # STRICT_RAISE | ...
```

`DstPolicy.STRICT_RAISE` is the Phase 1 default: a nonexistent local time (spring forward) or ambiguous local time (fall back) raises rather than resolving by heuristic. Loud beats plausible.

### 4.4 Data source

```python
class ColumnMappingConfig(QtrosConfig):
    timestamp: str
    open: str
    high: str
    low: str
    close: str
    volume: str | None


class DataSourceConfig(QtrosConfig):
    source_id:         str
    instrument_id:     InstrumentId
    timeframe:         Timeframe
    root_path:         Path              # validated: inside declared input root
    glob_pattern:      str
    file_format:       FileFormat
    columns:           ColumnMappingConfig
    timestamp_format:  str
    declared_tz:       TimeZoneName | FixedOffset   # required; never inferred
    timestamps_are_bar_open: bool        # vs. bar close — silently flips signals
    gap_policy:        GapPolicy
    max_tolerable_gap: timedelta
```

Two fields worth defending. `declared_tz` has no default and no inference path — Cycle 1 lost an evening to exactly this. And `timestamps_are_bar_open` is the ambiguity that makes two vendors' "same" data disagree by one bar; leaving it implicit means a look-ahead bug that the prefix-invariance audit cannot catch, because the frame itself is shifted before features are computed.

### 4.5 Strategy (C-2, C-9, D-4)

```python
class SignificancePolicyConfig(QtrosConfig):
    tier:                 SignificanceTier      # STANDARD | LOW_FREQUENCY
    min_trades:           PositiveInt
    pooling_instruments:  frozenset[InstrumentId]  # required iff LOW_FREQUENCY

    # validator: STANDARD ⇒ min_trades == 400, pooling empty
    #            LOW_FREQUENCY ⇒ min_trades >= 60, len(pooling) >= 4


class StrategyConfig(QtrosConfig):
    strategy_id:           str            # registry key (D-4), not an import path
    strategy_version:      SemanticVersion
    experiment_id:         str            # §5 registry key, e.g. AUDJPY_SWEEP
    mechanism_hypothesis:  str            # Directive 5 — required, min_length
    supported_timeframes:  frozenset[Timeframe]
    parameters:            Mapping[str, StrictDecimal | int | bool | str]
    significance:          SignificancePolicyConfig
    declared_at_utc:       datetime       # C-2 anti-retroactivity evidence
```

`mechanism_hypothesis` being a required non-empty field is Directive 5 enforced at config-parse time. You cannot start a backtest without having written down why you expect it to work. That's a thirty-second cost that reliably prevents the "the metrics looked good so I kept it" failure.

`declared_at_utc` is hash-relevant. Editing the significance tier after seeing results changes both the hash and this timestamp — the registry then shows two experiments with an ordering that speaks for itself.

### 4.6 Risk, friction, execution

```python
class RiskLimitsConfig(QtrosConfig):
    max_drawdown_pct:       StrictDecimal
    max_drawdown_basis:     DrawdownBasis = DrawdownBasis.MARK_TO_MARKET   # C-1
    risk_per_trade_pct:     StrictDecimal
    atr_stop_multiple:      StrictDecimal
    atr_lookback_bars:      PositiveInt
    max_concurrent_positions: PositiveInt
    max_daily_loss_pct:     StrictDecimal | None      # Phase 2
    daily_consistency_pct:  StrictDecimal | None      # Phase 2, prop 50% rule
    on_limit_breach:        BreachAction              # HALT_SESSION | HALT_RUN


class FrictionConfig(QtrosConfig):
    commission_model:     CommissionModel      # PER_LOT | PER_NOTIONAL | NONE
    commission_value:     StrictDecimal
    spread_model:         SpreadModel          # FIXED_TICKS | INSTRUMENT_TYPICAL
    spread_value_ticks:   StrictDecimal | None
    slippage_model:       SlippageModel        # FIXED_TICKS | ATR_FRACTION
    slippage_value:       StrictDecimal
    # validator: NONE commission requires explicit acknowledge_zero_friction=True


class ExecutionConfig(QtrosConfig):
    timing:              ExecutionTiming = ExecutionTiming.NEXT_BAR_OPEN
    path_assumption:     PathAssumption = PathAssumption.ADVERSE_FIRST   # C-1
    zero_volume_policy:  ZeroVolumePolicy = ZeroVolumePolicy.REJECT_FILL
    sub_min_lot_policy:  SubMinLotPolicy = SubMinLotPolicy.REJECT_ORDER
    allow_same_bar_close_fill: bool = False   # watermarks report if True
```

`FrictionConfig` has no zero default and requires an explicit acknowledgment flag to disable costs. Frictionless backtests are the single most common source of a phantom edge, and Directive 2 makes them a violation — so the schema makes them awkward to express by accident and impossible to express silently.

### 4.7 Reporting (C-3, C-5)

```python
class SharpeConventionConfig(QtrosConfig):
    return_basis:         ReturnBasis = ReturnBasis.DAILY_CLOSED_EQUITY   # C-5
    return_type:          ReturnType = ReturnType.ARITHMETIC
    annualization_factor: PositiveInt = 252
    risk_free_rate:       StrictDecimal = Decimal("0.0")
    exclude_non_trading_days: bool = True


class ConcentrationConfig(QtrosConfig):
    top_n_trades:              PositiveInt = 5
    quarterly_threshold_pct:   StrictDecimal = Decimal("60.0")   # gate
    lifetime_threshold_pct:    StrictDecimal = Decimal("60.0")   # kill criterion
    boundary_is_inclusive:     bool = False    # tested explicitly, ARCH-001 §5.2
    trade_period_attribution:  PeriodAttribution = PeriodAttribution.CLOSE_DATE


class GateThresholdsConfig(QtrosConfig):
    min_sharpe:           StrictDecimal = Decimal("1.3")
    min_profit_factor:    StrictDecimal = Decimal("1.4")
    max_drawdown_pct:     StrictDecimal = Decimal("10.0")
    oos_sharpe_decay_max_pct: StrictDecimal = Decimal("30.0")
    wfo_min_profit_factor:    StrictDecimal = Decimal("1.15")
```

Constitutional thresholds live in `config/reporting/constitution_v3_2.yaml`, not in Python constants. Two reasons: mandate §3 forbids magic constants, and when the constitution reaches v3.3 the diff is a config file with a version in its name — so a 2026 result and a 2027 result carry evidence of *which* rulebook scored them.

### 4.8 Top-level composition (D-5)

```python
class BacktestConfig(QtrosConfig):
    run_label:        str = Field(..., **provenance(OPERATIONAL))
    instrument_id:    InstrumentId
    source_id:        str
    date_range:       DateRangeConfig
    feature_set_id:   str
    strategy_ref:     StrategyRef            # id + version
    risk_ref:         str
    friction_ref:     str
    execution:        ExecutionConfig
    reporting_ref:    str = Field(..., **provenance(REPORT_AFFECTING))
    rng_seed:         int = 0
    paths:            PathsConfig = Field(..., **provenance(OPERATIONAL))
    runtime:          RuntimeConfig = Field(..., **provenance(OPERATIONAL))
```

```yaml
# config/backtest/phase1_mvp.yaml
schema_version: 1
run_label: "phase1-mvp-audjpy-baseline"
instrument_id: AUDJPY
source_id: dukascopy_m5
date_range:
  start: "2022-01-01"
  end: "2024-12-31"
feature_set_id: baseline_v1
strategy_ref:
  strategy_id: audjpy_asian_sweep
  strategy_version: "1.0.0"
risk_ref: phase1_baseline
friction_ref: retail_fx_typical
execution:
  timing: NEXT_BAR_OPEN
  path_assumption: ADVERSE_FIRST
  zero_volume_policy: REJECT_FILL
  sub_min_lot_policy: REJECT_ORDER
reporting_ref: constitution_v3_2
rng_seed: 0
paths:
  results_root: "./results"
  warehouse: "./data_lake/warehouse/qtros.duckdb"
runtime:
  log_level: INFO
  log_format: json
```

Loading is one pass: read → resolve refs → flatten → validate → freeze → hash. The flattened result is written to `results/{run_id}/config_resolved.yaml`, and that file — not the composed originals — is the reproduction input.

### 4.9 Schema versioning

Every config carries `schema_version`. `configuration/loader.py` holds a migration table from older versions to current. This costs nothing now and is the difference between "re-run the 2026 experiment" and "the 2026 config no longer parses" in year three. Given the explicit five-year maintenance assumption, it's cheap insurance.

Secrets never enter YAML. Phase 4 webhook tokens arrive via `.env` + `pydantic-settings`, and the manifest records **presence and key name**, never value.

---

## Section 5 — Config Hashing & Provenance Mechanics (Directive 10)

Specification, not implementation.

**Canonicalization.** Serialize the partition to JSON with: keys sorted lexicographically; no insignificant whitespace; UTF-8, no ASCII escaping; `Decimal` as its exact string form; `datetime` as RFC 3339 in UTC with explicit offset; `frozenset` as a sorted array; `None` omitted rather than serialized as `null`; **floats forbidden** in any hash-relevant field. Digest: SHA-256, lowercase hex, first 16 chars used as the display ID and the full digest stored.

Floats are forbidden rather than rounded because float repr has varied across platforms and versions; a hash that differs between your laptop and a future machine makes Directive 10 worse than useless — it makes reproduction *look* broken when it isn't.

**Partitioning.** Fields carry a provenance marker; unmarked fields default to `RUN_AFFECTING`. Defaulting to the strictest class means a forgotten marker causes an over-sensitive hash (harmless churn) rather than a hash collision between materially different runs (a silently wrong comparison). A schema test asserts every field in every model resolves to exactly one class.

**Manifest.** `RunManifest` records `run_hash`, `report_hash`, `config_hash`, `DatasetFingerprint`, git revision plus dirty-flag, resolved library versions from the lockfile, `rng_seed`, and UTC run timestamp. A dirty working tree does not block a run but stamps the result `UNTRUSTED` — same treatment as `allow_unreconciled`, for the same reason: it must be impossible to later mistake an exploratory number for a validated one.

---

## Section 6 — Results Artifacts & the Strategy Registry

### 6.1 Run artifact layout

```
results/{run_id}/
├── manifest.json              # Directive 10
├── config_resolved.yaml       # the flattened config that actually ran
├── fills.parquet              # every cash movement (C-4)
├── trades.parquet             # flat-to-flat lifecycles (C-4)
├── equity.parquet             # closed_equity + mtm_equity (C-1)
├── risk_events.parquet
├── rejected_intents.parquet   # rejections are data, per §5 of the constitution
├── reconciliation.json        # C-10
├── performance.json
├── lag_audit.json
├── report.html                # Layer 5 render
└── run.log.jsonl              # structlog output
```

`run_id` format: `{utc_date}-{experiment_id}-{run_hash[:8]}` — sortable, greppable, and self-identifying. Directories are written once and never modified; a re-scored run creates a new directory that shares its `run_hash` with the original, which is exactly how D-1's benefit becomes visible on disk.

### 6.2 The registry is a derived index, not a source of truth

Constitution §5 mandates a Strategy Registry. Maintained by hand, it drifts from reality the first busy week. Maintained as a database written during runs, it can disagree with the artifacts.

**Proposal:** the registry is **rebuilt by scanning `results/*/manifest.json` + `performance.json`** into `registry.duckdb`, and exported to a committed `docs/registry.md`. It is always regenerable and therefore can never be stale or wrong. The only hand-authored fields are the ones that genuinely require human judgment — `mechanism_note` (required by C-3 before a lifetime-concentrated strategy can pass) and the accept/reject decision — which live in a small `config/registry/decisions.yaml` keyed by `run_id` and are joined in at export time.

This gives you the constitution's registry table, with `Result` and `Outlier Check` columns that are computed rather than remembered.

---

## Appendix A — Import-Linter Contracts

```
1. Layered contract (top → bottom, no upward imports):
     qtros.cli
     qtros.analytics | qtros.persistence
     qtros.engine
     qtros.execution
     qtros.risk
     qtros.alpha
     qtros.features
     qtros.data
     qtros.configuration
     qtros.domain

2. Forbidden: qtros.domain → any qtros.* except qtros.domain
   (independence of the core; makes every layer unit-testable)

3. Forbidden: duckdb imported by anything except qtros.data.store
   (Rule 2 — one door)

4. Forbidden: csv, glob, shutil imported by anything except qtros.data, qtros.persistence
   (Cycle 1 self-ingestion, made structural)

5. Forbidden: qtros.* → builtins.print
   (mandate §2, zero print statements — via ruff T20 rather than import-linter)

6. Independence: qtros.alpha.strategies.* are mutually independent
   (no strategy may import another; prevents accidental coupling that would
    invalidate the Phase 2 correlation check)

7. Forbidden: qtros.configuration → qtros.{data,features,alpha,risk,execution,engine}
   (config describes, never executes)
```

Contract 6 is worth a note: the Phase 2 correlation check measures whether two strategies are one position wearing two names. If strategies can import each other, they can share state and become correlated through a mechanism the PnL correlation test would attribute to the market.

## Appendix B — Tool Configuration Rationale

**mypy** runs `--strict` with `disallow_any_explicit` on `domain/` only (strictest where contracts matter most, pragmatic at the Polars boundary where `Any` is sometimes unavoidable). `warn_unreachable = true` catches dead branches in risk-limit logic, which is where dead branches are most dangerous.

**ruff** selects at minimum: `E,F,W` (core), `I` (imports), `N` (naming), `UP` (modern syntax), `ANN` (annotations — mandate §2), `D` (Google-style docstrings — mandate §2), `T20` (no print), `DTZ` (**naive-datetime detection — the Cycle 1 lesson as a lint rule**), `RUF` , `B` (bugbear), `SIM`, `PTH`. `DTZ` alone justifies the tool: it flags `datetime.now()`, `datetime.utcnow()`, and naive constructors at edit time rather than at backtest time.

**pytest** markers: `slow`, `integration`, `regression`, `property`. Default run excludes `slow` so the fast suite stays usable during an evening session; CI runs everything.

## Appendix C — Revised Phase 1 Build Order (4 weeks, ~40–60h)

| Week | Work | Est. |
|---|---|---|
| 1 | Repo scaffold, pyproject, import contracts, `domain/` models, `configuration/` schema + hashing | 12–15h |
| 2 | Data engine, coverage, fingerprinting, session calendar, Cycle 1 ingestion guard | 12–15h |
| 3 | Feature pipeline + prefix-invariance audit, execution simulator + friction, risk engine | 12–15h |
| 4 | Engine loop, ledger, reconciliation, analytics + report render, regression suite, TradingView parity fixture | 12–15h |

Week 1 is front-loaded with structure that produces no backtest results, which will feel slow. It's the week that makes weeks 2–4 possible at this pace, and it's the week that would be cut first under deadline pressure — which is precisely what Directive 9 exists to prevent.

## Appendix D — Ruling Summary Requested

| ID | Item | Recommendation |
|---|---|---|
| D-1 | Three-way hash partition (run / report / operational) | **Approve** — saves re-runs on every scoring change |
| D-2 | `Decimal` as quoted strings; reject float input | **Approve** — required for exact reconciliation |
| D-3 | `SessionCalendar` in `domain/timekeeping/` | Approve as default |
| D-4 | Registry-based strategy loading, no import paths in YAML | Approve as default |
| D-5 | Flat composition; no YAML anchors or merges | Approve as default |
| D-6 | Minimal toolchain (ruff, mypy, pytest+hypothesis, import-linter, pre-commit) | Approve as default |

---

*End of ARCH-002. Behavioral implementation deferred to ARCH-003. `pyproject.toml` delivered as a companion file.*
