# Production Deployment Checklist

Use this checklist before any production live-trading restart or release. It is intentionally read-only until the restart step and must not expose or edit broker credentials, account IDs, `.env` files, or live keys.

## Scope

- Confirm the target is the local `main` branch and review the exact commits intended for production.
- Confirm the deployment window is before market open or after market close unless an incident requires otherwise.
- Confirm no unreviewed changes touch broker credentials, account IDs, live keys, `.env` files, or systemd service definitions.
- Record the current known-good commit before changing the running version:

```bash
git rev-parse HEAD
```

## Tests

Run the full Python test suite from the repository root:

```bash
PYTHONPATH=. pytest tests/ -v
```

Required result:

- All tests pass.
- New or modified modules remain above the 80% coverage expectation for their touched surface.
- Any warning that indicates broken broker connectivity, missing credentials, or unsafe live mode is investigated before deployment.

## Preflight

Run the live safety preflight before market open. This performs broker reads, validates premarket artifacts, runs dry-run scan/entry checks, and installs submit guards so no orders can be placed by the preflight.

```bash
python scripts/preflight_live_safety.py --project-root .
```

Required result:

- Account snapshot is readable.
- Premarket artifacts are present and fresh.
- Dynamic scan dry-run completes.
- Entry dry-run produces only `would_buy`/`would_skip` decisions and submits no orders.
- Open orders and exposure are understood before restart.

Generate the daily pre-market health report:

```bash
python scripts/generate_premarket_health_report.py --live --user-label default
```

Required result:

- Account status is ready.
- News status is ready or intentionally disabled.
- Dynamic scan status is ready or intentionally disabled.
- Open orders are expected.
- Exposure is within configured caps.

## Restart Procedure

1. Stop the currently running live loop using the approved operator mechanism for the host.
2. Confirm no duplicate live loop process remains.
3. Start the live loop in live mode only after tests and preflight have passed:

```bash
python scripts/run_alpaca_loop.py --live
```

4. Watch startup logs for the active user id, broker mode, premarket artifact load, health checks, and first loop heartbeat.
5. Confirm the loop is not in an unexpected reduce-only or startup-warmup state.

Do not cancel orders, liquidate positions, or reset paper/live state as part of a normal deployment.

## Validation Procedure

After restart, validate:

- One live loop process is running.
- Logs show the expected `user_id` tags.
- Broker mode is live only when the deployment was explicitly approved for live reads/trading.
- Account equity, cash/buying power, positions, open orders, and exposure match expectations.
- Premarket health report has been generated and reviewed before the open.
- Telegram/SMTP notifications are delivered when configured.
- No unexpected order submissions occur during the validation window.

After market close, confirm the automated daily trading report was generated:

```bash
ls -l reports/daily_*.html
```

## Abort Criteria

Abort or roll back before trading if any of the following are true:

- Full pytest fails.
- Live preflight fails.
- Premarket artifacts are missing/stale while premarket intelligence is required.
- Broker account status is blocked, unreadable, or has unexpected equity/cash values.
- Open orders or exposure do not match operator expectations.
- Logs show missing user isolation, wrong broker mode, or duplicate loop processes.
