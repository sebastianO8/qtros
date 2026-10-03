# Assumptions Registry

**Project:** Quantitative Trading Research OS
**Governing document:** Master Project Constitution v3.2 + PI rulings ARCH-001 … ARCH-007, G-1 … G-5
**Status:** ASS-001 … ASS-005 ratified. ASS-006 and ASS-007 raised in ARCH-006, implemented as stated, awaiting formal ruling. ASS-008 and ASS-009 ratified by G-1 and G-2 (2026-10-03).
**Maintainer:** Principal Investigator

---

## Why this file exists

A backtest is a claim about the past made under assumptions. Most of those assumptions are
invisible in the output: a Sharpe ratio does not say which bar the fills occurred on, or
whether spread was charged once or twice. When a result later turns out to be wrong, the
question is always *which assumption was load-bearing* — and that question is
unanswerable if the assumptions were never written down.

This registry exists so that every material modelling choice has:

* a **statement** precise enough to be implemented two different ways and compared,
* a named **implementation site**, so the code and the claim cannot drift apart,
* a **test** that fails if the assumption stops holding,
* an explicit **failure mode**, describing what a wrong assumption does to results and
  whether it would be visible,
* a **review trigger**, so the assumption is revisited when its context changes rather
  than when someone happens to remember it.

The failure-mode field carries the most weight. An assumption whose violation produces a
crash is cheap. An assumption whose violation produces a *plausible number* is expensive,
and those are marked accordingly.

**Rules.** Assumption IDs are never reused or renumbered. Superseded assumptions are marked
`SUPERSEDED BY ASS-nnn` and kept, because an archived `results/` directory may have been
produced under them. Every assumption must be discoverable from the code: the implementing
module's docstring cites its ID.

---

## ASS-001 — Next-bar-open execution

| Field | Value |
|---|---|
| **Status** | Ratified (ARCH-001 C-1, ARCH-005) |
| **Severity if wrong** | **High** — produces a plausible number |
| **Implementation** | `execution/broker.py` → `SimulatedBroker._reference_price`, `ExecutionTiming` |
| **Verified by** | `tests/test_benchmarks.py::TestBuyAndHoldFillPrices`, `tests/test_context.py::TestDecisionWindow` |

### Statement

A decision taken on the close of bar *T* is filled at the **open of bar T+1**. The
reference price for every market order is the execution bar's open, before friction. A
decision cannot be taken on the final bar of a dataset, because no bar follows it to fill
against; `BarContextFactory.last_decision_index` is therefore `bar_count - 2`.

`ExecutionTiming.SAME_BAR_CLOSE` exists but is not the default. Where used it is stamped
into the run manifest and watermarked in every rendered report.

### Rationale

A strategy that reads bar *T*'s close cannot also trade at that close: by the time the
close is observable, it has already happened. Filling there assumes simultaneous
observation and execution, which flatters every result by approximately one bar of
favourable drift — small per trade, systematic across four hundred.

### Failure mode if wrong

Same-bar-close fills inflate returns by an amount proportional to trade frequency, so a
high-frequency strategy benefits most. The inflation is invisible in every summary metric
and survives out-of-sample testing, because the bias is present in both samples. It would
first appear as a systematic gap between paper trading and backtest — after eight weeks of
paper trading had already been spent.

### Alternatives considered

Same-bar-close fills, rejected as above. Mid-bar fills at the bar's average price, rejected
as unattainable in practice and unverifiable against a broker statement.

### Review trigger

Moving to tick or sub-minute data, where the gap between decision and execution becomes
measurable rather than assumed; or Phase 4 paper trading revealing a systematic entry-price
difference against the model.

---

## ASS-002 — T-1 feature availability boundary

| Field | Value |
|---|---|
| **Status** | Ratified (ARCH-004 F-1, DIR-001) |
| **Severity if wrong** | **Critical** — produces a plausible number and invalidates all downstream results |
| **Implementation** | `engine/context.py` → `AVAILABILITY_LAG_BARS`, `BarContextFactory.feature_row_for` |
| **Verified by** | `tests/test_context.py::TestFeatureLagBoundary`, `::TestStructuralIsolation`, `::TestFactoryGuards` |

### Statement

A strategy deciding on bar *T* sees feature values computed as of bar **T-1**. The shift is
applied at exactly one place — `BarContext` construction — and nowhere else. Feature
transforms emit unshifted, strictly causal values.

Bar *T* itself is fully visible, including its close. This is deliberate and follows from
ASS-001: the decision is taken at bar *T*'s close, so that close is a known fact.

`FeatureFrame.availability_shift_applied` records how much shift a frame already carries.
`BarContextFactory` refuses any frame where that value is non-zero.

### Rationale

Two distinct properties are easily conflated and must not be:

* **Causality** — the value at bar *t* uses only bars at or before *t*. A property of the
  transform, unrepairable downstream, verified mechanically by the prefix-invariance audit.
* **Availability lag** — the strategy at *t* sees values from *t-1*. A property of the
  engine.

A causal transform shifted twice yields stale features. A non-causal transform shifted once
still contains look-ahead. Applying the shift in one auditable location beats applying it in
*N* transforms, each of which is a chance to double-shift or to forget.

### Failure mode if wrong

**Double shift** (transforms shift *and* the engine shifts): strategies silently receive
T-2 features. Nothing raises. The backtest runs clean on stale data and understates the
strategy's real edge, which reads as "the idea does not work" and gets the hypothesis
rejected for the wrong reason.

**No shift** (features at T-0 alongside a T-0 close): look-ahead bias. Results are inflated
and survive out-of-sample testing because the bias is present in both samples.

### Residual risk

The boundary guards *feature computation*, not arithmetic. A strategy could compute
`ctx.bar.close - ctx.bar.open` inline, creating an ad-hoc feature that never passes through
the pipeline and is therefore invisible to the prefix-invariance audit. **DIR-001** governs
this: all feature calculation belongs in Layer 0.5 transforms, and strategies may only
compare current bar data against pre-calculated T-1 features. This is enforced by review,
not by code, and is the weakest link in the Directive 1 chain.

### Review trigger

Any change to `AVAILABILITY_LAG_BARS`; any transform that requests a shift of its own; any
strategy whose logic contains arithmetic on `ctx.bar` fields beyond comparison.

---

## ASS-003 — Friction defaults

| Field | Value |
|---|---|
| **Status** | Ratified (Directive 2) — see note on phrasing |
| **Severity if wrong** | **High** — produces a plausible number |
| **Implementation** | `execution/friction.py` → `StandardFrictionModel`; `domain/instrument.py` → `InstrumentSpec` |
| **Verified by** | `tests/test_benchmarks.py::TestFrictionAttribution`, `::TestFrictionlessBaseline` |

### Statement

Friction has three components, itemised separately and never netted:

| Component | Basis | Default |
|---|---|---|
| Spread | Half the instrument's declared spread, in ticks | `InstrumentSpec.typical_spread_ticks / 2` |
| Slippage | Fixed ticks, always adverse | `0.5` ticks |
| Commission | Per lot, round turn, halved per side | `InstrumentSpec.commission_per_lot / 2` |

Slippage cannot be configured negative — the constructor refuses it. A configuration in
which all costs are zero requires `acknowledge_zero_friction=True`; it cannot arise by
omission or by default.

**Note on phrasing.** The ARCH-006 authorisation described this as "fixed per-share and
percentage friction". The implemented model is **per-lot commission and tick-based spread and
slippage**, which is what FX and futures contracts actually quote. A per-share or percentage
basis would require restating `InstrumentSpec` and is not implemented. Flagged for
correction in the ruling rather than silently reinterpreted.

### Rationale

Netting costs into a single figure destroys the most useful diagnostic available in Phase 1.
When a strategy's edge dies under realistic friction the first question is *which* friction
killed it, and one number cannot answer it. A strategy killed by commission is fixable by
trading larger; one killed by spread is fixable by trading a different instrument; one
killed by slippage is probably not fixable at all.

Slippage is unidirectional because a model in which slippage sometimes helps manufactures
edge at high trade counts, and 400 trades is the floor this project gates on.

### Failure mode if wrong

Understated friction is the single most common source of a phantom edge. It scales with
trade frequency, so it destroys exactly the high-frequency strategies that look most
attractive on paper. It is invisible in every summary metric and first appears as a
persistent gap between paper trading and backtest.

Overstated friction is far cheaper: it rejects viable strategies, which costs opportunity
rather than capital.

### Review trigger

First eight weeks of Phase 4 paper trading — compare realised slippage per fill against the
0.5-tick default and update rather than keeping a stale figure. Also on any change of
instrument or broker.

---

## ASS-004 — Position reversal fill-splitting

| Field | Value |
|---|---|
| **Status** | Ratified (ARCH-005) |
| **Severity if wrong** | **Medium** — corrupts trade statistics, visible on inspection |
| **Implementation** | `engine/portfolio.py` → `Portfolio._reverse_position`; `domain/money.py` → `apportion` |
| **Verified by** | `tests/test_engine.py::TestReversal` |

### Statement

A fill that opposes the open position and exceeds its size spans two position lifecycles.
It is split into two booked fills:

1. A closing fill for the open quantity, joined to the existing `trade_id`, exit reason
   `REVERSAL`, completing that trade.
2. An opening fill for the residual, carrying a **new** `trade_id`, starting a new lifecycle.

Fill identifiers are suffixed `c` and `o` so both remain traceable to the broker fill they
came from. Costs are apportioned by quantity share, with the opening side taking the exact
arithmetic remainder so the two halves sum to the original exactly.

### Rationale

Under ARCH-001 C-4 a trade is one flat-to-flat lifecycle. A reversal contains two entries
and one exit, so it is two lifecycles by definition. Recording it as one trade would merge
two distinct entry decisions into a single record, distorting trade count, holding-period
statistics, and per-trade PnL — all three of which feed Directives 7 and 8.

### Failure mode if wrong

**Merged into one trade:** understates trade count, so a strategy appears less active than it
is and reaches the 400-trade gate later. Holding period is overstated, because a reversal's
two positions are reported as one long hold.

**Costs duplicated instead of apportioned:** each half charged the full friction, inflating
costs on every reversal. Plausible enough to survive review, and material for any strategy
that flips rather than flattening.

**Costs quantized independently:** a sub-cent residual belongs to neither half and
reconciliation fails. This is why `apportion` returns a remainder rather than two
independently rounded figures.

### Review trigger

Any strategy that reverses rather than flattening, since these paths are otherwise unused;
Phase 2 multi-instrument work, where a reversal may span instruments.

---

## ASS-005 — Margin debit model (friction-only on entry)

| Field | Value |
|---|---|
| **Status** | Ratified (ARCH-005) — Phase 1 scope only |
| **Severity if wrong** | **High** for equities, **not applicable** to Phase 1 instruments |
| **Implementation** | `engine/portfolio.py` → `Portfolio._open_position`, `Portfolio.closed_equity` |
| **Verified by** | `tests/test_engine.py::TestUnclosedPosition::test_no_cash_moves` |

### Statement

Opening a position debits **friction only**. No notional leaves the account. Consequently:

* `cash == initial_cash + Σ realized_pnl − Σ commission`
* `closed_equity == cash` by construction, not by coincidence
* `mtm_equity == cash + unrealized_pnl`

Margin requirement is not modelled in Phase 1. A position open when the run ends is
recorded with `is_open_at_backtest_end=True`, excluded from the reported trade count, and
**not** closed — closing it would invent a fill at a price nobody traded.

### Rationale

Correct for margin-traded instruments: FX spot, CFDs, and futures, which is the entirety of
Phase 1's scope. Margin is collateral, not a purchase, so the notional never leaves the
account.

### Failure mode if wrong

Applying this model to **cash-settled equities**, where the full notional leaves the account
on purchase, would overstate available capital for the entire holding period. Every
position-sizing decision downstream would be computed against a balance the account does not
have, and the error compounds with position size. This is a code change, not a configuration
value.

Not modelling **margin requirement** means Phase 1 cannot detect a margin call. Acceptable
while position sizes are small relative to the account; not acceptable once prop-firm
leverage is applied, which is why it is scoped to Phase 1.

### Review trigger

**Mandatory before any equity instrument is added.** Also before Phase 4 prop-firm
deployment, where leverage makes margin requirement binding rather than theoretical.

---

## ASS-006 — Friction is embedded in the fill price, not debited from cash

| Field | Value |
|---|---|
| **Status** | **RAISED IN ARCH-006 — awaiting PI ruling** |
| **Severity if wrong** | **Critical** — double-charges friction on every fill |
| **Implementation** | `domain/execution.py` → `ExecutionFill.cash_costs` / `implicit_costs`; `execution/broker.py` → `_derive_implicit_costs` |
| **Verified by** | `tests/test_benchmarks.py::TestBuyAndHoldPnl`, `::TestFrictionAttribution` |

### Statement

Spread and slippage are paid by **receiving a worse price**, not by a cash debit. They are
already inside `fill_price` and therefore inside `realized_pnl`. Commission is a genuine
separate debit. Therefore:

```
cash_delta   = realized_pnl − commission          (the cash identity)
gross_pnl    = realized_pnl + implicit_costs      (reconstructs reference-price PnL)
net_pnl      = gross_pnl − total_costs            (equals sum of cash deltas, by identity)
```

Implicit costs are derived from the **actual** degradation `fill_price − reference_price`,
not from the friction model's intended adjustment. The two differ because the fill price is
snapped adversely to the tick grid, and that rounding is a real cost.

### Why this is being raised

The first implementation applied spread and slippage **twice**: once by worsening the fill
price, and again as a separate cash debit. Every component test passed, because each checked
its own behaviour rather than the total. The error was found by the known-truth buy-and-hold
benchmark authorised in ARCH-006 — the first figure computed outside the system rather than
by it.

For the benchmark instrument the double charge was 400 on a 500,000 gross, or **8 basis
points per round turn**. At 400 trades that is roughly 32% of gross erased by an accounting
error. It would have rejected viable strategies while looking entirely plausible.

### Failure mode if wrong

**Double charging** (the bug): friction overstated by the spread-plus-slippage component on
every fill. Rejects viable strategies. Because it is an overstatement, it would never have
been caught by comparison against live trading — paper results would have looked
*better* than backtest, which reads as good news.

**No charging**: friction understated, phantom edge, as in ASS-003.

### Requested ruling

Ratify the price-embedded convention, or specify the reference-price convention instead
(fill at mid, debit spread and slippage as cash). Both are internally consistent; the
current implementation and all reported figures assume the former.

---

## ASS-007 — Bounded monetary and price precision

| Field | Value |
|---|---|
| **Status** | **RAISED IN ARCH-006 — awaiting PI ruling** |
| **Severity if wrong** | **High** — breaks exact reconciliation for arithmetic rather than logical reasons |
| **Implementation** | `domain/money.py` → `MONEY_EXPONENT`, `PRICE_EXPONENT`, `quantize_money`, `apportion` |
| **Verified by** | `tests/test_engine.py::TestLedgerInvariant` (property-based, 200 examples per invariant) |

### Statement

Every value entering the ledger is quantized before storage:

| Quantity | Precision | Rounding |
|---|---|---|
| Cash (realised PnL, costs, cash deltas) | 0.01 — quote currency minor unit | Half-even |
| Derived prices (weighted-average entry) | 1e-10 | Half-even |
| Apportioned quantities | 1e-8 | Half-even |

Split amounts use `apportion`, which quantizes the first part and takes the second as the
exact arithmetic remainder.

### Why this is being raised

Directive 6 requires reconciliation to hold **exactly**, with no tolerance. That is only
achievable if precision is bounded — and Python's `Decimal` is not bounded by default.
Division produces up to 28 significant digits, and both the weighted-average entry price and
reversal cost apportionment involve division.

Once a running balance carries more significant digits than the Decimal context allows,
`cash += delta` rounds. Summing the same deltas in a different order rounds differently. The
running balance and the ledger total then disagree by an amount too small to notice and too
real to ignore.

Found by the property-based ledger invariant test, not by any scenario test: it requires a
weighted-average entry price with a non-terminating expansion followed by enough subsequent
fills for the residual to matter. No hand-written scenario would have constructed that.

Half-even rounding is used rather than half-up because half-up biases every marginal cent in
one direction, and over four hundred trades a systematic bias is a phantom edge rather than a
rounding artefact.

### Failure mode if wrong

Reconciliation fails intermittently, on sequences that look no different from those that
pass. The natural response to an intermittent exact-equality failure is to introduce a
tolerance — which is precisely the "discrepancy is within tolerance" escape hatch that
Directive 6 exists to close, and which would then mask the real Cycle-1-class bugs it was
written to catch.

### Requested ruling

Ratify the precision table, or specify different exponents. Note that a quote currency
without a minor unit still reconciles exactly under the current table; it simply never
populates the final two places.

---

## ASS-008 — Exposure is capped by notional and lot limits, not by a margin model

| Field | Value |
|---|---|
| **Status** | Ratified (PI ruling G-1, 2026-10-03) |
| **Severity if wrong** | **High** for leveraged deployment — permits positions a real account could not hold |
| **Implementation** | `risk/limits.py` → `MaxNotionalExposureRule`, `MaxLotRule`; `configuration/models/risk.py` → `RiskLimitsConfig.max_lot`, `max_notional_pct` |
| **Verified by** | `tests/test_risk.py::TestExposureRules` |

### Statement

Phase 1 does not model margin requirement, initial or maintenance. Instead, every entry is
bounded by two independent caps evaluated by the risk engine before approval:

* **Notional cap** — `quantity × contract_size × reference_price` may not exceed
  `max_notional_pct` of closed equity (plus any already-open notional).
* **Hard lot cap** — `quantity` may not exceed `max_lot`, regardless of equity.

An entry breaching either cap is **rejected**, not resized down. A resize would make the
strategy's realised risk differ silently from its declared risk.

### Rationale

A margin model needs the broker's leverage schedule, which varies by firm, instrument, and
account tier and is not knowable in Phase 1. Two caps that are explicit, conservative, and
visible in the rejection stream are more honest than a margin model built on a guessed
leverage ratio. ASS-005 already establishes that notional never leaves the account, so the
notional cap is what stands in for the capital the position would otherwise tie up.

### Failure mode if wrong

Caps set too loose permit a backtested position size that a prop-firm account could not
hold, so the backtest's drawdown and return are both computed on exposure that would have
been margin-called in practice. Caps set too tight reject viable trades, which costs
opportunity rather than capital and is visible in `rejected_intents`.

### Review trigger

Before Phase 4 prop-firm deployment, where the firm's leverage schedule becomes knowable
and a real margin model (ASS-005 review trigger) replaces these caps.

---

## ASS-009 — Drawdown halt evaluates the adverse-first intrabar extreme and blocks entries only

| Field | Value |
|---|---|
| **Status** | Ratified (PI ruling G-2, 2026-10-03) |
| **Severity if wrong** | **High** — a close-only drawdown understates the loss a prop firm would have measured |
| **Implementation** | `risk/engine.py` → `RiskEngine.on_bar_close`, `RiskEngine.approve`; `engine/backtester.py` → `Backtester._adverse_mtm` |
| **Verified by** | `tests/test_risk.py::TestDrawdownHalt`, `tests/test_backtester.py::TestRiskHalt` |

### Statement

At each bar close the risk engine computes the **worst** mark-to-market equity the open
position reached within that bar, under the ADVERSE_FIRST path assumption: cash plus
unrealised PnL marked at the bar's low for a long, or its high for a short. Drawdown is
measured from the running peak of close-of-bar MTM equity to that intrabar extreme. If it
reaches `max_drawdown_pct`, the engine enters the halted state.

A halt **blocks new entries only**. Exit intents, protective stops, and targets continue to
be approved and executed, so an open position is managed to its conclusion rather than
frozen or force-flattened at an invented price.

### Rationale

Prop firms measure loss limits on intraday mark-to-market, not on closes. A bar whose close
recovers above the limit but whose low breached it would have ended a funded account; a
close-only check would miss it and report the strategy as compliant. ADVERSE_FIRST is the
same assumption the broker already uses to resolve ambiguous bars (ARCH-001 C-1), so the
halt and the fills agree about what the bar did.

Blocking entries only, rather than flattening, avoids inventing a fill: the halt is a
decision about future risk, and the open position's exit is still governed by the strategy
and its protective levels under the same rules as before the halt.

### Failure mode if wrong

**Close-only evaluation:** understates maximum drawdown for any strategy with wide stops or
intrabar volatility, passes gates a funded account would have failed, and first appears as a
breached loss limit in live trading.

**Halt force-flattens:** introduces a fill at a price the strategy never chose, with an exit
reason the strategy never produced, and makes trade statistics depend on the risk limit
rather than on the mechanism under test.

### Review trigger

Any change to `PathAssumption` defaults; any prop-firm rule set that measures drawdown on a
different basis (for example end-of-day balance); Phase 2 daily-loss and consistency rules.

---

## Change log

| Date | Change |
|---|---|
| ARCH-006 | Registry created. ASS-001 … ASS-005 recorded as ratified. ASS-006 and ASS-007 raised from findings during test authoring. |
| 2026-10-03 | ASS-008 (G-1 exposure caps) and ASS-009 (G-2 adverse-first drawdown halt) recorded as ratified. |
