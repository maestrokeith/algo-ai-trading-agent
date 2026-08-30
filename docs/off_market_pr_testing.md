# Off-Market PR Testing

Use the offline replay tooling to validate trading PRs when the market is closed.
Replay uses saved artifacts and a mock broker only; it must not call a real broker
submit path.

## Replay Live Cycle

Run:

```bash
PYTHONPATH=. python scripts/replay_live_cycle.py --date latest --user live_bot --broker-mock
```

Replay reads:

- `data/dynamic_scan_history/`
- `data/premarket/latest_event_feed.json`
- `data/premarket/latest_rankings.json`
- `data/premarket/latest_catalysts.json`
- `data/reports/` when present
- `data/positions_<user>.json` when present

The replay flow converts saved accepted dynamic candidates into allocator signals,
runs allocator candidate building and action generation, and then runs the execution
order builder with replay quotes from the saved scan history.

The broker is always a recording mock. Intended orders are written as simulated
orders in the summary instead of being submitted.

Replay writes:

```text
data/replay/YYYY-MM-DD_<user>.json
```

The summary includes selected candidates, rejected candidates, allocator action
lines, order build rejects, allocator blocks, and simulated submitted orders.

Replay is allocator-only. It forces `options.enabled=false` and logs
`options_disabled_by_replay_live_cycle`; do not use it to validate paper-options
entry or contract selection behavior.

## Paper Options Diagnostics

Use the safe mock-only paper options diagnostic runner when you need to exercise
the real entry gate and paper-only options selection path:

```bash
./bin/algo paper-options-diagnostics --user paper_bot --symbol QQQ
```

The runner loads `paper_bot` config, applies a diagnostic overlay with
`options.enabled=true` and `options.mode=paper_only`, runs
`TradingEngine.run_entry_gates`, then routes to the paper-only options helper
with a deterministic mock option chain. It does not use `scripts/replay_live_cycle.py`
and does not submit live broker orders.

Expected diagnostic lines include:

```text
OPTIONS_CONFIG enabled=true mode=paper_only
ENTRY_PIPELINE_STAGE
ENTRY_EVAL
OPTION_PIPELINE_STAGE
OPTION_CHAIN_LOADED
OPTION_FILTER_SUMMARY
OPTION_BEST_REJECTED
```

`OPTION_SELECTED` may appear instead of `OPTION_BEST_REJECTED` when the mock
chain and budget allow a contract.

## Safety Checks

Run the PR safety script before asking for review:

```bash
scripts/pr_safety_check.sh
```

It runs:

```bash
PYTHONPATH=. pytest tests/test_dynamic_universe.py -v
PYTHONPATH=. pytest tests/test_live_cycle.py -v
PYTHONPATH=. pytest tests/test_capital_allocator_loop.py -v
PYTHONPATH=. pytest tests/test_capital_allocator.py -v
PYTHONPATH=. pytest tests/test_execution.py -v
PYTHONPATH=. pytest tests/test_allocation_profile.py -v
```

If `data/dynamic_scan_history/*.json` exists, it also runs replay for `live_bot`
with `--broker-mock`.

The script blocks unsafe live broker validation when `BROKER_MODE=LIVE` unless
mock mode is enabled.
