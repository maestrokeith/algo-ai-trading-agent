# Scripts index

| Script | Purpose |
|--------|---------|
| `lean_cli.py` | Lean-style CLI: `backtest` (CSV or Alpaca), `live` → `run_alpaca_loop.py`. From repo root you can also run `python lean …` (thin `lean` shim at project root). |
| `run_alpaca_loop.py` | Main live/paper loop (multi-user, locks, allocator path). |
| `run_alpaca.py` | Single-pass trading engine. |
| `run_backtest.py` | Strategy/backtest runner. |
| `run_scheduled_alpaca.py` | Scheduled launcher wrapper. |
| `run_api.py` | API server entry (see `src/api/`). |
| `run_telegram_control.py` | Poll Telegram for `/paper`, `/live`, `/status`, `/stop` and launch the trading loop. |
| `bootstrap_local_db.py` / `.sh` | Enable local DB: creates `data/algosphere.db` + Alembic migrations (`ALGOSPHERE_LOCAL_SQLITE=1`). |
| `run_example.py` | Synthetic SPY example (no broker). |
| `algo_loop.py` | Re-exports `run_alpaca_loop.main()` for convenience. |
| `check_equity.py` / `check_prices.py` / `check_positions.py` | Account / quote / position diagnostics. |
| `show_daily_summary.py` / `show_position_charts.py` / `show_sell_strategy.py` | Reporting / charts. |
| `reset_paper.py` | Paper reset + tracked JSON. |
| `download_backtest_data.py` | Fetch historical data for backtests. |
| `seed_users.py` | DB user seeding (when using API/DB). |

Makefile and `./bin/algo` wrap the most common commands from repo root.
