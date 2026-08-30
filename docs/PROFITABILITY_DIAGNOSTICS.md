# Profitability Diagnostics

This framework is diagnostic and protective. It does not add indicators, news
sources, AI models, entry routes, or fallback strategies.

## Trading Modes

- `live`: real orders allowed when all safety checks pass.
- `paper`: paper account/execution.
- `shadow`: production decision pipeline may run, but broker order submission is blocked.
- `entries-disabled`: existing positions may be safely exited; new buy entries are blocked.

Disable new entries:

```bash
./bin/algo live --mode entries-disabled
```

Run shadow:

```bash
./bin/algo live --mode shadow
```

Restore live entries only after reconciliation:

```bash
./bin/algo trading-audit --date YYYY-MM-DD --user live_bot
./bin/algo profitability-report --from YYYY-MM-DD --to YYYY-MM-DD --user live_bot
./bin/algo strategy-readiness
./bin/algo live --mode live
```

## Reports

```bash
./bin/algo day-review --date YYYY-MM-DD --user live_bot
./bin/algo duplicate-forensics --date YYYY-MM-DD --user live_bot
./bin/algo trading-audit --date YYYY-MM-DD --user live_bot
./bin/algo profitability-report --from YYYY-MM-DD --to YYYY-MM-DD --user live_bot
./bin/algo news-edge-report --from YYYY-MM-DD --to YYYY-MM-DD
./bin/algo strategy-readiness
```

`duplicate-forensics` explains duplicate order and fill rows without mutating
history. Use it before accepting profitability results when raw order snapshots
and unique lifecycle counts diverge.

`day-review` is the operator-facing canonical review. It writes
`reports/day_review/YYYY-MM-DD.json` and `.md` and answers what happened, data
completeness, why entries were blocked, whether signals can be measured, news
linkage, execution/exit availability, and the single highest-priority fix.

`trading-audit` reconciles the full persisted funnel and flags unmatched
submissions, fills without decisions, duplicate records, mixed live/paper data,
option/equity mixups, missing attribution, missing exits, and P&L traceability
gaps.

`profitability-report` reports win rate, expectancy, profit factor, P&L,
spread/slippage cost, drawdown, holding time, MFE/MAE, MFE capture, latency,
and breakdowns by strategy, symbol, exit reason, quality bands, option bands,
news fields, and mode.

`news-edge-report` evaluates whether news adds incremental expectancy. Ingested
news is not treated as useful unless linked reconciled outcomes show positive
incremental expectancy after costs.

## Loss Attribution

Losing trades receive one primary deterministic classification:

`BAD_SIGNAL`, `LATE_ENTRY`, `CHASED_ENTRY`, `BAD_OPTION_CONTRACT`,
`WIDE_SPREAD`, `LOW_LIQUIDITY`, `IV_COLLAPSE`, `SLIPPAGE`, `EXIT_GIVEBACK`,
`STOP_TOO_WIDE`, `STOP_TOO_TIGHT`, `MARKET_REGIME_FAILURE`, `DATA_STALE`,
`MISSING_FEATURE`, `RUNTIME_FAILURE`, `BROKER_EXECUTION_FAILURE`,
`UNRECONCILED`, or `INSUFFICIENT_DATA`.

Unreconciled trades are never counted as normal strategy losses.

## Strategy State

Routes use explicit states:

- `LIVE`: eligible for live entries when readiness gates pass.
- `SHADOW`: evaluated without live promotion.
- `DISABLED`: not eligible.

New or insufficiently validated strategies default to `SHADOW`. Promotion is
manual only and requires reconciled sample size, positive net expectancy,
acceptable drawdown/spread cost, no unresolved integrity errors, and successful
replay or shadow validation.

## Experiments

Experiments may change one variable only and run in `replay` or `shadow` mode:

```bash
./bin/algo experiment list
./bin/algo experiment report <experiment-id>
```

Production promotion remains manual.

## Canonical Lifecycle

The canonical lifecycle source is `src/trading_lifecycle.py`. Reports should
use this layer or explicitly state a narrower scope.

Authoritative sources:

- scanner events and entry decisions: persisted trade attribution candidates
- allocator actions: trade attribution allocator rows
- orders: canonical local order identity linked to broker order ID, then client order ID, then logical order ID
- fills: broker activity/fill ID; otherwise deterministic fill deltas per canonical order
- positions: derived from unique fills and exits
- P&L: reconciled entry and exit fills only
- forward outcomes: local historical bars after decision timestamp
- news: persisted news events and explicit signal links

Raw counts are loop rows or snapshots. Unique counts are canonical lifecycle
entities. Repeated loop evaluations can be valid, but they are not separate
orders, fills, or positions unless their identities differ.

Replay, mock, shadow, paper, and test rows are not live broker evidence.
Suspicious live identifiers such as `replay-*`, `mock-*`, `shadow-*`, `paper-*`,
and `test-*` are blocked from new live attribution with
`LIVE_DATA_CONTAMINATION_BLOCKED`. Historical contamination is quarantined
non-destructively by writing a manifest under:

```text
data/quarantine/replay_contamination/YYYY-MM-DD/
```

The original artifact is not deleted or overwritten.

Forward outcomes use the decision timestamp as time zero. Missing local bars
produce `OUTCOME_UNAVAILABLE_MISSING_BARS`; outside-session timestamps produce
`OUTCOME_UNAVAILABLE_OUTSIDE_SESSION`; invalid symbols produce
`OUTCOME_UNAVAILABLE_INVALID_SYMBOL`. Reports must not fabricate option
outcomes when historical option quotes are unavailable.

Entry alignment states are `PASS`, `FAIL`, `UNAVAILABLE`, `STALE`, and `ERROR`.
Unavailable features must not be counted as ordinary alignment failures.

Adaptive relaxation is research-only by default in production:

```yaml
trading_control:
  adaptive_relaxation:
    production_auto_apply: false
```

Low trade frequency alone is informational and must not loosen production
filters. Zero-trade days are allowed.
