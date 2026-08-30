# What to push to a public GitHub repo

## Files that GO to GitHub (safe, no secrets)

```
config/default.yaml
requirements.txt
README.md
docs/HELP.md
docs/SCHEDULE.md
docs/GITHUB_PUSH.md
.gitignore

scripts/run_example.py
scripts/run_alpaca.py
scripts/run_alpaca_loop.py
scripts/run_scheduled_alpaca.py
scripts/check_equity.py
scripts/check_prices.py
scripts/check_positions.py
scripts/show_daily_summary.py
scripts/show_sell_strategy.py
scripts/reset_paper.py

start_trading_at_market_open.sh

src/__init__.py
src/config_loader.py
src/universe.py
src/strategy.py
src/position_sizing.py
src/portfolio_risk.py
src/execution.py
src/compliance.py
src/trade_filters.py
src/trading_engine.py
src/candlestick.py
src/position_tracker.py
src/brokers/__init__.py
src/brokers/alpaca_client.py
```

## Files that do NOT go (in .gitignore)

- **`.env`** – API keys (Alpaca). Never commit.
- **`data/`** – positions_tracked.json (local trading state).
- **`logs/`** – if you use it for cron/scheduled output.
- **`__pycache__/`, `.venv/`, `venv/`** – Python cache and virtual env.

## Commands to push

```bash
cd /Users/psuriset/cursor/algo
git add .
git status   # confirm no .env or data/ is staged
git commit -m "Algo trading app: strategy, risk, Alpaca, exits"
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

If the repo already exists and you use `git add .`, `.gitignore` will prevent `.env` and `data/` from being added. Always run `git status` before committing and ensure no secrets are listed.
