# CLAUDE.md — AlgoSphere Trading Engine

## Project Overview

Algorithmic trading engine built on Alpaca, using trend-following strategies with a 12-gate risk management pipeline. Currently being extended to support multi-user accounts (SAR-126 epic).

## Tech Stack

- **Language:** Python 3.10+
- **Broker:** Alpaca (paper + live)
- **Config:** YAML (`config/default.yaml`, `config/users.yaml`)
- **Storage:** JSON position tracker (`data/positions_{user_id}.json`), PostgreSQL planned
- **Testing:** pytest
- **Dependencies:** `requirements.txt`

## Project Structure

```
algo/
├── docs/                         # HELP, SCHEDULE, GitHub checklist
├── config/
│   ├── default.yaml              # Master config (all parameters)
│   └── users.yaml.example        # Multi-user config template
├── lean                          # Shim → scripts/lean_cli.py (``python lean …``)
├── src/
│   ├── brokers/alpaca_client.py  # Alpaca SDK wrapper
│   ├── algorithm/                # QCAlgorithm layer
│   ├── backtest/                 # Data loaders + metrics
│   ├── engine/                   # Backtest engine
│   ├── news_sentiment/           # Optional NewsAPI + FinBERT
│   ├── config_loader.py          # YAML loader + deep_merge
│   ├── user_manager.py           # Multi-user loading + config merging
│   ├── strategy.py               # Entry/exit logic (trend-following)
│   ├── trading_engine.py         # 12-gate orchestration
│   ├── position_tracker.py       # JSON position persistence
│   ├── position_sizing.py        # Risk-per-trade sizing
│   ├── portfolio_risk.py         # Daily loss, drawdown, safe mode
│   ├── compliance.py             # PDT rules
│   ├── execution.py              # Limit orders, slippage
│   ├── universe.py               # Market sessions, quality gates
│   └── market_regime.py          # SPY/QQQ/VIX scoring
├── scripts/
│   ├── lean_cli.py               # Lean-style CLI (backtest / live)
│   ├── run_alpaca_loop.py        # Main live trading loop
│   ├── run_alpaca.py             # Single-pass engine
│   └── run_backtest.py           # Backtest runner
├── tests/                        # Unit + integration tests
├── data/                         # Runtime position files (gitignored)
└── requirements.txt
```

## Key Commands

```bash
# Run tests
PYTHONPATH=. pytest tests/ -v

# Run tests with coverage
PYTHONPATH=. pytest tests/ -v --cov=src --cov-report=term-missing

# Run specific test file
PYTHONPATH=. pytest tests/test_user_manager.py -v

# Live trading loop (paper)
python scripts/run_alpaca_loop.py --paper

# Live trading loop (live)
python scripts/run_alpaca_loop.py --live
```

## Development Rules

### Testing Requirements (MANDATORY)

- **Every ticket implementation MUST include tests in `tests/`**
- **Code coverage MUST be >80% for all new/modified modules**
- **No ticket moves to Human Review until coverage gate is satisfied**
- Tests run with `PYTHONPATH=. pytest tests/ -v`
- Test files follow `tests/test_{module_name}.py` naming
- Use `monkeypatch` for env vars, `tmp_path` for temp files
- Cover: happy path, edge cases, validation errors, backward compatibility

### Code Style

- Type hints on all public functions
- Docstrings on modules and classes
- Logging via `logging.getLogger(__name__)` — never print()
- f-strings for formatting
- `from __future__ import annotations` for forward refs

### Config & Secrets

- Never put API keys in config files — always use env var references
- `config/default.yaml` is the single source of truth for parameters
- Per-user overrides via `config/users.yaml` deep-merged onto base
- Credentials: `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` (paper), `ALPACA_LIVE_*` (live)

### Multi-User Architecture

- `UserContext` is the unit of isolation — one per user
- Every stateful component (broker, tracker, risk, compliance) scoped by `user_id`
- Single-user fallback: `user_id="default"` when no `users.yaml` exists
- Errors in one user must never crash others
- All log lines tagged with `[user_id]`

## Ticket Workflow

- All work is tracked in Linear under the **AlgoSphere** project
- Refer to Linear for current epics, sub-issues, dependencies, and status
- Upon completion (with >80% test coverage verified), move tickets to **Human Review**
