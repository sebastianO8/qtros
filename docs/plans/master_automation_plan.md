# Master Automation Plan — Claude Code CLI Directives

**Project:** Quantitative Trading Research OS (QTROS)
**Governing documents:** Master Project Constitution v3.2; PI rulings ARCH-001 … ARCH-007; G-1 … G-6; DIR-001; `docs/validation/assumptions.md` (ASS-001 … ASS-007)
**Supervisors:** Principal Investigator (PI) and Gemini (planner / reviewer)
**Executor:** Claude Code (this CLI session and its successors)
**Status:** ACTIVE from 2026-10-03
**Contains behavioral code:** No. Process rules only.

---

## 0. Why this document exists

Until now every file in this repository was generated in a chat window, verified by the PI by hand, and routed into the tree manually. That manual step was the "air gap": nothing reached `src/` without a human reading it first.

Claude Code removes the air gap. It edits files directly. The five directives below replace the human air gap with a mechanical one: no edit survives in the tree unless the verification gate passes, and no invariant-bearing file changes without an explicit PI ruling. The PI's role shifts from router to reviewer.

---

## 1. The five absolute directives

### Directive A1 — MANDATORY VERIFICATION

After generating or modifying **any** `.py` file under `src/` or `tests/`, Claude Code MUST run the verification gate before doing anything else:

```bash
source .venv/bin/activate && python scripts/check_sources.py && python -m pytest -q && lint-imports && ruff check . && ruff format --check . && mypy
```

Both commands must exit 0. The gate runs after every logical edit, not once at the end of a task. When a task touches several files that only make sense together (for example a new module plus its test file), the gate runs as soon as the set is importable, and again at the end.

The gate is **not** satisfied by reading the code and believing it is correct. It is satisfied only by the two exit codes.

### Directive A2 — HALT ON RED

If any test fails or `check_sources.py` reports a problem:

1. Stop feature work immediately. No new files, no new functions, no "while I'm here" edits.
2. Print the exact failing test names and the exact error output to the PI, verbatim.
3. Fix the failure, or revert the edit that caused it, before touching anything else.
4. Re-run the gate. Only a green gate ends the halt.

The repository is never left in a failing state at the end of a turn. If a fix cannot be found, the offending edit is reverted and the PI is told what was attempted and why it failed.

A warning is a failure. `pyproject.toml` sets `filterwarnings = ["error"]` deliberately; Claude Code must not add warning filters, `# noqa`, `# type: ignore`, or `pytest.mark.skip` to make red turn green. Suppression requires a PI ruling.

### Directive A3 — PRESERVATION OF INVARIANTS

The following may NEVER be altered without an explicit PI approval quoted in the turn that makes the change:

| Protected surface | File | What it guards |
|---|---|---|
| Money precision and rounding | `src/qtros/domain/money.py` | `MONEY_EXPONENT`, `PRICE_EXPONENT`, `QUANTITY_EXPONENT`, half-even rounding, `apportion` remainder semantics (ASS-007) |
| The T-1 availability shift | `src/qtros/engine/context.py` | `AVAILABILITY_LAG_BARS = 1`, `feature_row_for`, `_guard_shift`, `_guard_audit`, `last_decision_index = bar_count - 2` (ASS-001, ASS-002, DIR-001) |
| Dual-ledger / cash identity | `src/qtros/engine/portfolio.py` | `_book` is the only cash mover; `cash_delta == realized_pnl - cash_costs`; `closed_equity == cash`; `mtm_equity == cash + unrealized`; reversal fill-splitting; `reconcile()` exact-zero discrepancy (ASS-004, ASS-005, ASS-006) |
| Fill accounting identity | `src/qtros/domain/execution.py` | `ExecutionFill.cash_costs` (commission only), `implicit_costs`, `assert_internally_consistent` (ASS-006) |
| Risk-approval gate | `src/qtros/domain/orders.py` | `RISK_APPROVAL` sentinel; `ApprovedOrder` refuses construction without it; only `qtros.risk` may import the sentinel |
| Friction direction | `src/qtros/execution/friction.py` | slippage never negative; zero friction requires `acknowledge_zero_friction=True` (ASS-003) |
| Path assumption | `src/qtros/execution/broker.py` | `ADVERSE_FIRST` default; gaps fill at the open; `SAME_BAR_CLOSE` + `FAVOURABLE_FIRST` refused |

"Alter" includes: changing a constant, changing a rounding mode, adding a tolerance, adding a code path that moves cash outside `_book`, adding a second place where the T-1 shift is applied, or weakening a guard so a test passes. Adding a *new* test that exercises these files is permitted and encouraged. Refactoring that provably preserves behaviour still requires approval because the proof is the PI's to accept.

Layer boundaries from ARCH-002 §2.2 are also protected: `domain/` imports nothing from `qtros` except `domain/`; only `data/store.py` imports `duckdb`; `configuration/` never imports an executing layer. Claude Code does not relax an import-linter contract to make an import work.

### Directive A4 — RULING ADHERENCE

Claude Code abides by every ratified ruling. Where a ruling and a convenient implementation disagree, the ruling wins and the implementation is reported as blocked.

Rulings currently in the repository: ARCH-002 (`docs/decisions/`), ASS-001 … ASS-009 (`docs/validation/assumptions.md`), G-1 … G-5 (§1A of this document).

Rulings cited by code and docs but **not yet in the repository**: ARCH-001, ARCH-003, ARCH-004 (F-1), ARCH-005, ARCH-006, ARCH-007, DIR-001, Constitution v3.2. (G-6 was named in the first handoff but no text was ever supplied; G-1 … G-5 are the ratified set.) Until the PI places these under `docs/decisions/`, Claude Code treats them as follows:

- Where the code or `assumptions.md` already encodes the ruling (next-bar-open, T-1, ADVERSE_FIRST, exact Decimal, friction-in-price), the code is the ruling.
- Where a ruling is only known by name, Claude Code does not guess its content. It builds to the text the PI pastes, and asks when the text is missing.

Specifically for the next build block, the engine run loop must honour, per bar and in this order unless G-rulings specify otherwise: (1) protective exits resolved against the bar before the strategy is consulted, (2) mark-to-market and drawdown check on the MTM basis, (3) strategy decision from a `BarContext` built by `BarContextFactory`, (4) risk gate producing `ApprovedOrder` or `RejectedIntent`, (5) broker fill against bar T+1's open, (6) portfolio booking, (7) reconciliation inside `run()` at the end (C-10).

### Directive A5 — SUPERVISOR CHECK-INS

After completing any of the following, Claude Code stops and asks the PI to review with Gemini before starting the next block:

1. `src/qtros/configuration/` routed and tested
2. `src/qtros/risk/` (drawdown halt, lot limits, MaxNotional cap, approval minting)
3. `src/qtros/alpha/` (strategy base, registry, first strategy)
4. `src/qtros/engine/backtester.py` (deterministic run loop with in-run reconciliation)
5. Any change to `pyproject.toml` tool configuration or import-linter contracts
6. Any change that touches a protected surface under A3, even with approval

A check-in means: the gate is green, the diff is summarised in plain language, every ruling touched is named, and no further files are edited until the PI replies. "Pause" is literal.

---

## 1A. Core architecture laws — PI rulings G-1 … G-5 (ratified 2026-10-03)

These five rulings were issued by the PI via Gemini in response to the adoption sweep. They are permanent architecture law, ranked with ARCH-001 … ARCH-007. Code that implements one cites it in its module docstring.

| ID | Ruling | Where it lands |
|---|---|---|
| **G-1** | Phase 1 models exposure with a `MaxNotionalExposureRule` and a hard `max_lot` rule. **No full margin model.** Recorded as assumption ASS-008. | `risk/limits.py`; `RiskLimitsConfig.max_lot`, `RiskLimitsConfig.max_notional_pct` |
| **G-2** | The drawdown halt evaluates against the **ADVERSE_FIRST intrabar mark-to-market extreme**, not the bar close. A halt blocks **new entries only**; exits and protective orders continue to execute. Recorded as assumption ASS-009. | `risk/engine.py` → `RiskEngine.on_bar_close`; `ExitReason.RISK_HALT` is not auto-applied |
| **G-3** | The run loop order per bar *t* is strict and fixed: (1) execute orders pending from *t-1* at `open(t)` → (2) resolve resting protective exits against bar *t* → (3) mark to market → (4) `risk.on_bar_close` → (5) `strategy.evaluate` → (6) `risk.approve` → (7) queue approved orders for *t+1*. No step may be reordered, merged, or skipped. | `engine/backtester.py` → `Backtester._step` |
| **G-4** | DIR-001 is enforced at the consumer side by a `SealedFeatureSet` provenance check in `alpha/base.py`: a strategy declares the features it reads, and the engine refuses to run it against a frame whose seal does not cover every one of them. | `alpha/base.py` → `verify_feature_provenance`; `Backtester.__init__` |
| **G-5** | `significance_policy` (tier, `min_trades`, pooling) and the low-frequency flag are **run-affecting** and live in the `run_hash` partition, not the `report_hash`. Editing the significance tier after seeing results therefore mints a new experiment. | `configuration/models/strategy.py`; asserted by `tests/test_configuration.py` |

Supplementary PI rulings from the same message:

- **Linters.** Ruff, mypy, and import-linter findings are deferred to a dedicated "Linting & Typing" sprint after the run loop is complete. They remain advisory and are not part of the A1 gate until then.
- **Repo cleanup.** `.DS_Store` removed from tracking and ignored; stale `domain/protocols.py` and `features/protocols.py` deleted.
- **Configuration.** The chat-generated `configuration/` was never routed; Claude Code regenerates it from ARCH-002 §4–§5 plus G-5. No ARCH-008 document exists in the repo; where "the ARCH-008 spec" is cited, ARCH-002 §4–§5 and the G-rulings above are the operative text.
- **Execution greenlight.** For the ARCH-008 build pass the PI waived the per-module pause in A5; the pause happens once, after `engine/backtester.py` and the gate. A5 resumes in full afterwards.

---

## 2. Verified baseline

### 2.000 Merged to `main` with the full toolchain green (2026-10-03)

| Check | Result |
|---|---|
| `python scripts/check_sources.py` | 53 tracked modules + 15 package markers current |
| `python -m pytest -q` | 290 passed |
| `lint-imports` | 7 contracts kept, 0 broken |
| `ruff check .` | All checks passed |
| `ruff format --check .` | All files formatted |
| `mypy` | Success: no issues found in 90 source files |

**The A1 gate is now the full toolchain.** From this point every `.py` edit must leave all six checks green. Suppression rules from A2 still apply: no `# noqa`, `# type: ignore`, or per-file ignore may be added to reach green without a reason in the comment and a line in the check-in.

Linting sprint notes (PI ruling 2026-10-03):
- `ruff --fix` + `ruff format` applied repository-wide (the first-ever formatting pass; large mechanical diff).
- Imports used only in annotations moved under `TYPE_CHECKING`; `cli.py` is exempt because typer evaluates `Annotated[...]` at runtime, and pydantic config models are declared `runtime-evaluated-base-classes` so their field types stay live.
- `domain/` is now free of explicit `Any` (`QtrosError.context` and `StrategyState.values` are `object`-typed).
- Pre-existing mypy debt in `data/` cleared: DuckDB scalars coerced through typed helpers, numpy arrays typed, polars DST arguments typed as literals, `Datetime` dtype narrowed before reading `time_zone`.
- `ReconciliationFailed` renamed `ReconciliationFailedError`. `LookaheadDetected` keeps its name with a justified `noqa`: it names a finding, and archived docs cite it.
- Ambiguous Unicode (`×`, `–`, `→`) replaced with ASCII in code, comments, and docstrings.
- No behaviour change: the test suite and `check_sources.py` needles were unchanged throughout.

### 2.00 Phase 1 complete (2026-10-03, branch `feat/arch-008`)

| Check | Result |
|---|---|
| `python scripts/check_sources.py` | 53 tracked modules + 15 package markers current (exit 0) |
| `python -m pytest -q` | 290 passed (exit 0) |
| `lint-imports` | **7 contracts kept, 0 broken** — re-enabled per PI ruling; `qtros.workflow` added as a layer between `cli` and `analytics | persistence`; the strategies-independence contract now lists only the strategy modules that exist |
| End-to-end | `qtros run phase1_mvp` against a DuckDB warehouse → coverage gate → features → run → analytics → `results/{run_id}/` incl. `report.html` → `qtros registry` (`tests/test_cli_workflow.py`) |

Delivered: `cli.py` (`validate`, `run`, `ingest`, `registry`), `workflow.py` (warehouse → engine → analytics → persistence → render; coverage is a precondition, `--allow-gaps` watermarks), `logging_setup.py`, `analytics/render/` (self-contained HTML with inline SVG equity curve, no scripts or remote assets), `persistence/registry.py` (derived `docs/registry.md` + `.json` from `results/*`, joined with `config/registry/decisions.yaml`). `persistence` no longer imports `analytics` (sibling layers): the writer takes a structural `ScoredReport`.

Dependency added: **`pyarrow`** — `data/store.py` already moved frames through Arrow for every DuckDB insert and query, but the package was neither declared nor installed, so the warehouse write path had never run in this environment. Declared in `pyproject.toml` and installed; flagged for the PI because §3 of this plan reserves new dependencies for a ruling.

Not produced (deliberate): `registry.duckdb` — only `data/store` may import DuckDB; the JSON index serves the purpose. `uv.lock` is still absent. Ruff/mypy remain deferred by ruling.

### 2.0 After the Day 6 Phase 1 completion pass (2026-10-03, branch `feat/arch-008`)

| Check | Result |
|---|---|
| `python scripts/check_sources.py` | 48 tracked modules + 15 package markers current (exit 0) |
| `python -m pytest -q` | 268 passed (exit 0) |
| End-to-end | `config/backtest/phase1_mvp.yaml` → loader → features → run → analytics → `results/{run_id}/` exercised by `tests/test_builder.py`, `tests/test_analytics.py`, `tests/test_persistence.py` |

Delivered: `domain/timekeeping/` (SessionCalendar, DST-strict), `domain/manifest.py`, config families `calendars/` and `features/`, the committed `config/` tree, `engine/builder.py` (config → run graph), `analytics/` (trade statistics, C-5 Sharpe, C-1 drawdown, C-3 concentration, attribution, gate verdicts), `persistence/` (immutable `results/{run_id}/` writer with manifest, resolved config, Parquet ledgers). ASS-010 recorded. PI rulings applied: sizing basis closed equity (ratified); `PER_NOTIONAL` / `ATR_FRACTION` stay schema-only (they did not block resolution).

Fix outside the new code: `features/transforms/{atr,zscore,_control}.py` passed polars' deprecated `min_periods`; under `filterwarnings = error` that raised on the first real pipeline run. Renamed to `min_samples` (polars ≥ 1.21). No logic change.

Not built in Phase 1: `cli.py` (so import-linter still cannot run), the HTML report render (`analytics/render/`), the derived registry export (`docs/registry.md`), Layer 0 → builder wiring for a real DuckDB-backed run (the builder takes a bar frame; `data/` produces one, but no CLI joins them yet).

### 2.1 After the ARCH-008 build pass (2026-10-03, branch `feat/arch-008`)

| Check | Result |
|---|---|
| `python scripts/check_sources.py` | 37 tracked modules + 12 package markers current (exit 0) |
| `python -m pytest -q` | 193 passed (exit 0) — 78 inherited + 47 configuration + 27 risk + 11 alpha + 18 backtester + 12 enum/param cases |
| Layering | Every new import points downward: alpha → configuration → domain; risk → configuration → domain; engine → alpha, risk, execution |
| `RISK_APPROVAL` importers in `src/` | `domain/orders.py` (definition) and `risk/engine.py` only |

Delivered: `configuration/` (models, hashing, loader, registry), `risk/` (limits, sizing, engine), `alpha/` (base, registry decorator, provenance check, `vwap_pullback` reference), `engine/backtester.py`, ASS-008/ASS-009, repo cleanup. **Awaiting PI + Gemini review before any further block (A5).**

Open for PI ruling from this pass: sizing basis is closed equity (`risk/sizing.py` docstring); `max_concurrent_positions` is schema-only because the Phase 1 book holds one position; `PER_NOTIONAL` commission and `ATR_FRACTION` slippage are schema-only and refused at build time; `config/` YAML files and `SessionCalendar` (D-3) are still not built.

### 2.2 At plan adoption (2026-10-03, before the build pass)

| Check | Result |
|---|---|
| `python scripts/check_sources.py` | 26 tracked modules + 7 package markers current (exit 0) |
| `python -m pytest -q` | 78 passed (exit 0) |
| `ruff check src tests` | 133 findings — mostly TC001/TC003 (type-checking import blocks), RUF100, W292, I001 |
| `mypy` | 25 errors in 11 files, 13 of them in `data/store.py` |
| `lint-imports` | Cannot run: layer `qtros.cli` does not exist yet |

The hard gate under A1 is the full toolchain since 2026-10-03: `check_sources.py`, `pytest`, `lint-imports`, `ruff check`, `ruff format --check`, and `mypy`. Claude Code must not silently widen `ignore` lists to reach green.

### Open items surfaced during adoption (for PI ruling, not for autonomous action)

- **`src/qtros/configuration/` is absent.** Gemini reports it as "delivered, pending route". The files exist only in chat output and must be placed in the tree before `risk/` can take a `RiskLimitsConfig`.
- **Two stale duplicate modules** that nothing imports: `src/qtros/domain/protocols.py` (pre-ARCH-007 copy of `data_contracts.py`) and `src/qtros/features/protocols.py` (pre-ARCH-007 copy of `feature_contracts.py`, minus `SealedFeature*`). Candidates for deletion; `check_sources.py` does not track them.
- **No `config/` YAML directory, no `pine/`, no `CONSTITUTION.md`, no `uv.lock`** despite ARCH-002 §2.1 listing them.
- **`.DS_Store` is tracked.** Should be added to `.gitignore` and removed from the index.
- **Ruling texts missing from the repo** (see A4). G-1 … G-5 resolved 2026-10-03 (§1A); the ARCH-00x texts remain outstanding.

---

## 3. Working conventions

- **Branching.** Feature work for each A5 block happens on a branch named for the block (`feat/risk-gate`, `feat/alpha-base`, `feat/engine-backtester`). Nothing is committed or pushed without the PI asking for it.
- **Tests travel with code.** A new module ships with its test module in the same turn; the gate counts the new tests.
- **No new dependencies** without a PI ruling; `pyproject.toml` upper bounds are deliberate (ARCH-002 §3).
- **Docstrings and types** per mandate §2: Google-style docstrings, full annotations, `Decimal` for anything that touches cash or price, no `print`, no naive datetimes.
- **Ruling citations in code.** A module that implements a ruling cites it in its docstring (`assumptions.md` rule), and new assumptions are added to `assumptions.md` rather than left implicit.
- **Scratch work** goes in the session scratchpad, never in the repo.

---

## 4. Build order after adoption

| Step | Block | Gate at exit | Check-in |
|---|---|---|---|
| 0 | ✅ `configuration/` built from ARCH-002 §4–§5 + G-5 with StrictDecimal and hash-partition tests | A1 | A5 #1 |
| 1 | ✅ `risk/`: MaxLot + MaxNotional rules (G-1), fixed-fractional sizer, adverse-first halt (G-2), sole approval minter | A1 | A5 #2 |
| 2 | ✅ `alpha/`: `StrategyBase`, `register_strategy` (D-4), `verify_feature_provenance` (G-4), `vwap_pullback` 1.0.0 | A1 | A5 #3 |
| 3 | ✅ `engine/backtester.py`: seven-step G-3 loop, protective levels, reconciliation inside `run()` | A1 | A5 #4 |
| 4 | ✅ Timekeeping, config tree, builder, analytics, persistence (Day 6) | A1 | A5 #4 |
| 5 | ✅ `cli.py`, `workflow.py`, DuckDB-backed `qtros run`, HTML render, registry export; import-linter green | A1 + import-linter | A5 #5 |
| 6 | ✅ Merged to `main`; ruff/mypy sprint complete; full toolchain is the gate | A1 (full) | A5 |
| 7 | Phase 2: `uv.lock`; real Dukascopy ingest + first real-data run; tune G-1 limits on real volatility; `audjpy_asian_sweep`; prop-firm daily rules | A1 (full) | A5 |

Each step begins by re-running the gate on the untouched tree, so a red inherited from outside the session is reported rather than blamed on the step.

---

*Adopted at PI direction, relayed via Gemini, 2026-10-03. Amendments to this plan require the same PI approval as the directives it contains.*
