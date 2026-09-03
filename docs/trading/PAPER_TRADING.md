# Paper Trading Research Stack

This repository contains an isolated **paper/simulation-only** FX and metals research stack. It does not contain broker credentials, real-money order routing, or an autonomous live-execution path.

Supported research instruments are `EURUSD`, `GBPUSD`, `USDJPY`, `AUDUSD`, `USDCAD`, `XAUUSD`, and `XAGUSD`.

The stack models spread, configurable slippage, fixed-fractional sizing, one-percent hard risk caps, portfolio open-risk limits, next-bar paper entries, stop/target handling, break-even moves, ATR trailing, trade journals, grouped session/instrument statistics, drawdown, expectancy, consecutive losses, walk-forward splits, and Monte-Carlo resampling.

## Modules

- `engine/market_data.py` — validation, spread defaults, and right-closed resampling.
- `engine/signal_engine.py` — 5m/15m EMA trend alignment plus 1m EMA/RSI/ATR/volume filters.
- `engine/risk_engine.py` — swing/ATR stops, R targets, and fixed-fractional sizing.
- `engine/portfolio.py` — deterministic position-count and total-open-risk limits.
- `engine/paper_broker.py` — paper fills, spread/slippage, break-even, trailing, and journal records.
- `engine/analytics.py` — equity, win rate, profit factor, drawdown, expectancy, streaks, grouped stats, walk-forward splits, and Monte Carlo.
- `engine/paper_scalper.py` — end-to-end backtest runner.
- `engine/trading_research.py` — safe public research facade.

## Safety boundary

`PAPER_ONLY = True` and `LIVE_EXECUTION = False` are explicit invariants. The GitHub Actions workflow verifies that boundary on every relevant push or pull request.
