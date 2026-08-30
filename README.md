# AlgoSphere

## How To Run

### Setup

```bash
cd algo
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Run tests

```bash
PYTHONPATH=. pytest tests/ -v
```

### Run a single test file

```bash
PYTHONPATH=. pytest tests/test_user_manager.py -v
```

### Run with coverage

```bash
PYTHONPATH=. pytest tests/ -v --cov=src --cov-report=term-missing
```

### Run the live trading loop

Paper:

```bash
python scripts/run_alpaca_loop.py --paper
```

Live:

```bash
python scripts/run_alpaca_loop.py --live
```

### Operations quick start

Install read-only daily operations timers on a node:

```bash
cd /opt/algosphere/algo-ai-trading-agent
PYTHONPATH=. pytest tests/ -v
./bin/algo install-node --dry-run --user live_bot
./bin/algo install-node --user live_bot
```

The node installer performs dependency checks, repo path validation,
`config/users.yaml` and `.env`/environment validation, Python import checks,
a pytest smoke test, systemd unit installation, timer enablement, and
post-install verification with `systemctl list-timers 'algosphere*'`.
It installs every unit in `deploy/systemd/`; if `algo.service` is present there,
it is installed with the rest of the node units.

Install only the ops timers when node prerequisites have already been checked:

```bash
./bin/algo install-ops-timers --dry-run
./bin/algo install-ops-timers
```

Enable optional replay analysis:

```bash
./bin/algo install-node --enable-replay --user live_bot
```

All operations times are US/Eastern:

| Time ET | Mode | Task | Service | Timer |
| --- | --- | --- | --- | --- |
| 05:15-09:25 every 12 min | Automatic | Premarket Collection | `algosphere-premarket.service` | `algosphere-premarket.timer` |
| 09:20 | Automatic | Premarket Readiness Check | `algosphere-ops-premarket-ready.service` | `algosphere-ops-premarket-ready.timer` |
| 09:30 | Automatic | Live algo startup | `algo.service` | host deployment dependent |
| 09:35 | Automatic | Startup Validation | `algosphere-ops-startup-validation.service` | `algosphere-ops-startup-validation.timer` |
| 16:15 | Automatic | Daily Summary | `algosphere-ops-daily-summary.service` | `algosphere-ops-daily-summary.timer` |
| 16:25 | Automatic | Profitability Attribution and Catalyst Reports | `algosphere-ops-postmarket-analytics.service` | `algosphere-ops-postmarket-analytics.timer` |
| 16:35 | Optional automatic | Replay Analysis | `algosphere-ops-replay-summary.service` | `algosphere-ops-replay-summary.timer` |
| 16:45 | Automatic | Research Feedback | `algosphere-ops-research-feedback.service` | `algosphere-ops-research-feedback.timer` |
| Sat 09:00 | Automatic | Weekly Research Feedback | `algosphere-ops-weekly-research-feedback.service` | `algosphere-ops-weekly-research-feedback.timer` |

Operations artifacts:

- Premarket feed: `data/premarket/latest_event_feed.json`
- Premarket rankings: `data/premarket/latest_rankings.json`
- Premarket catalysts: `data/premarket/latest_catalysts.json`
- Daily reports: `reports/daily/YYYY-MM-DD/`
- Research reports and dashboards: `reports/research_feedback/`
- Historical catalyst outcome database: `data/research/catalyst_outcomes/YYYY-MM-DD_live_bot.json`
- Ops logs: `data/logs/ops_*_YYYY-MM-DD.log`

Automatic actions are run by the systemd timers in the schedule above. Manual
actions are the validation and recovery commands below.

Manual validation commands:

```bash
./bin/algo premarket-ready
./bin/algo ops startup-validation --date 2026-06-07 --user live_bot --journal-unit algo.service
./bin/algo ops daily-summary --date 2026-06-07 --user live_bot
./bin/algo ops postmarket-analytics --date 2026-06-07 --user live_bot
./bin/algo ops replay-summary --date 2026-06-07 --user live_bot
./bin/algo ops research-feedback --date 2026-06-07 --user live_bot
./bin/algo ops weekly-research-feedback --date 2026-06-07 --user live_bot
./bin/algo research-feedback 2026-06-07 --user live_bot
./bin/algo catalyst-outcomes --date 2026-06-07 --user live_bot
./bin/algo paper-options-diagnostics --user paper_bot --symbol QQQ
systemctl list-timers 'algosphere-*'
journalctl -u algo.service --since '2026-06-07 09:30:00' --no-pager | grep PREMARKET_STARTUP_ARTIFACTS
journalctl -u algo.service --since today --no-pager | grep -E 'OPTION_SCAN_START|OPTION_SCAN_SUMMARY|OPTION_SIGNAL|OPTION_SELECTED|OPTION_ENTRY_BLOCKED|OPTION_ORDER_INTENT|OPTION_ORDER_SUBMITTED|OPTION_POSITION_OPENED'
```

Troubleshooting commands:

```bash
systemctl status algosphere-premarket.service
systemctl status algosphere-ops-startup-validation.service
journalctl -u algosphere-premarket.service --since today --no-pager
journalctl -u algosphere-ops-startup-validation.service --since today --no-pager
ls -l data/premarket/latest_event_feed.json data/premarket/latest_rankings.json data/premarket/latest_catalysts.json
ls -l reports/daily/2026-06-07/
ls -l data/logs/ops_*_2026-06-07.log
```

Daily operator checklist:

- Before 09:20 ET, confirm `algosphere-premarket.timer` ran recently.
- At 09:20 ET, confirm `./bin/algo premarket-ready` exits 0.
- At 09:30 ET, confirm `algo.service` is running.
- At 09:35 ET, confirm startup validation found `PREMARKET_STARTUP_ARTIFACTS status=fresh`.
- After 16:15 ET, confirm `reports/daily/YYYY-MM-DD/daily_summary.txt` exists.
- After 16:25 ET, confirm catalyst and profitability reports exist.
- After 16:35 ET, if replay is enabled, confirm `replay_summary.txt` exists.

Direct commands that require `PYTHONPATH=.`:

```bash
PYTHONPATH=. pytest tests/ -v
PYTHONPATH=. python scripts/run_premarket_collection.py --force
```

### Trigger Codex from iPhone

The manual GitHub Actions workflow `Codex From Issue` can ask Codex to fix one
GitHub issue, push a `codex/issue-<issue_number>` branch, and open a pull
request linked to the issue. It does not merge automatically.

Repository setup:

- Add an `OPENAI_API_KEY` repository secret for the Codex CLI.

From iPhone:

1. Open the GitHub app or browser.
2. Open this repo.
3. Create an issue with the task.
4. Go to Actions.
5. Open `Codex From Issue`.
6. Tap `Run workflow`.
7. Enter the issue number.
8. Watch for the PR.

Dynamic momentum catalyst runner notes:

- Dynamic scanner max price defaults to `dynamic_universe.max_price: 150`.
- Normal dynamic gain filter remains `dynamic_universe.max_day_gain_pct: 80.0`.
- Catalyst-backed candidates can use `dynamic_universe.catalyst_boost.max_gain_pct_catalyst` (default `250`) after price, quote, spread, and liquidity checks pass.
- This is scanner-only; it does not change live order placement, sizing, allocation, or exits.
- Check logs for `DYNAMIC_GAIN_FILTER_LIMITS` and `DYNAMIC_CATALYST_RELAXED_GATE`.

Full deployment, troubleshooting, and the daily operator checklist are in
`docs/OPERATIONS.md`. Premarket-specific notes are in
`docs/premarket_collection.md`.

### Analytics commands

Show a combined daily analytics summary for an explicit trading date:

```bash
./bin/algo summary 2026-06-07 --user live_bot
```

Run analytics scripts directly without setting `PYTHONPATH`:

```bash
python scripts/show_catalyst_stats.py
python scripts/generate_profitability_attribution_report.py --date 2026-06-07 --user live_bot
python scripts/replay_market_session.py --date 2026-06-07 --user live_bot --broker-mock
python scripts/generate_research_feedback.py 2026-06-07 --user live_bot
python scripts/generate_research_feedback.py --date 2026-06-07 --user live_bot
python scripts/generate_catalyst_outcomes.py --date 2026-06-07 --user live_bot
./bin/algo dynamic-rejection-report --date 2026-06-07 --user live_bot
./bin/algo dynamic-gate-research --date 2026-06-07 --user live_bot
```

Dynamic rejection reports are research-only. They read
`data/dynamic_scan_history/*.json` and write
`reports/research_feedback/dynamic_rejections_YYYY-MM-DD_live_bot.md`, showing
rejected dynamic symbols that later moved +5%, +10%, or +20% when same-day bar
outcomes are available.

Dynamic gate research reports are also read-only. They parse local dynamic scan
history and logs to summarize downstream gates such as `no_catalyst`, short
history, spread cap, entry alignment, VWAP extension, relative volume, unstable
quote, below-min price, and excessive gain. Reports are written under
`data/research/dynamic_gate_research/`.

### Control the loop from Telegram

Start the Telegram command listener:

```bash
python scripts/run_telegram_control.py
```

Required env:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_CHAT_IDS` or `TELEGRAM_CHAT_ID`

Telegram commands:

- `/status`
- `/paper [user_id]`
- `/live [user_id]`
- `/stop`

### Run the single-pass engine

Paper:

```bash
python scripts/run_alpaca.py --paper
```

Live:

```bash
python scripts/run_alpaca.py --live
```

### Run a backtest

```bash
python scripts/run_backtest.py
```

### Notes

- Main config: `config/default.yaml`
- Multi-user overrides: `config/users.yaml`
- Paper credentials: `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`
- Live credentials: `ALPACA_LIVE_API_KEY_ID`, `ALPACA_LIVE_API_SECRET_KEY`
