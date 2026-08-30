"""execution.symbol_cooldown_* and run_entry_gates post-exit re-entry wait."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from src.config_loader import load_config
from src.execution import (
    minutes_since_last_broker_structured_exit,
    symbol_post_exit_cooldown_minutes,
)
from src.trading_engine import TradingEngine


def test_symbol_post_exit_minutes_etf_list_default_60() -> None:
    cfg = {"execution": {"symbol_cooldown_minutes": 30}}
    assert symbol_post_exit_cooldown_minutes("SPY", cfg) == 60.0
    assert symbol_post_exit_cooldown_minutes("XLP", cfg) == 60.0
    assert symbol_post_exit_cooldown_minutes("AAPL", cfg) == 30.0


def test_symbol_post_exit_top_momentum_relaxed() -> None:
    """Listed names use symbol_cooldown_top_momentum_minutes instead of the 60m ETF default."""
    cfg = {"execution": {"symbol_cooldown_minutes": 30}}
    assert symbol_post_exit_cooldown_minutes("SPY", cfg) == 60.0
    cfg_tm = {
        "execution": {
            "symbol_cooldown_minutes": 30,
            "symbol_cooldown_top_momentum_symbols": ["SPY", "NVDA"],
            "symbol_cooldown_top_momentum_minutes": 22,
        }
    }
    assert symbol_post_exit_cooldown_minutes("SPY", cfg_tm) == 22.0
    assert symbol_post_exit_cooldown_minutes("NVDA", cfg_tm) == 22.0
    assert symbol_post_exit_cooldown_minutes("AAPL", cfg_tm) == 30.0


def test_symbol_post_exit_top_momentum_alias_and_default_minutes() -> None:
    cfg = {
        "execution": {
            "symbol_cooldown_minutes": 100,
            "top_momentum_symbols": ["QQQ"],
        }
    }
    assert symbol_post_exit_cooldown_minutes("QQQ", cfg) == 25.0


def test_symbol_post_exit_etf_explicit() -> None:
    cfg = {
        "execution": {
            "symbol_cooldown_minutes": 30,
            "symbol_cooldown_etf_minutes": 45,
        }
    }
    assert symbol_post_exit_cooldown_minutes("SPY", cfg) == 45.0


def test_minutes_since_last_broker_structured_exit() -> None:
    e = load_config()
    ex = dict(e.get("execution") or {})
    ex["symbol_cooldown_minutes"] = 0.0
    ex["symbol_cooldown_etf_minutes"] = 0.0
    ex["symbol_cooldown_etf_symbols"] = []
    eng = TradingEngine({**e, "execution": {**ex}})
    t1 = datetime(2024, 6, 5, 10, 0, 0)
    t2 = datetime(2024, 6, 5, 12, 0, 0)
    eng.state.last_stop_loss_at["X"] = t1
    eng.state.last_profit_exit_at["X"] = t2
    now = datetime(2024, 6, 5, 12, 30, 0)
    m = minutes_since_last_broker_structured_exit(eng.state, "X", now)
    assert m is not None and 29.0 < m < 31.0


def _df() -> pd.DataFrame:
    closes = [100.0 + i * 0.1 for i in range(50)]
    return pd.DataFrame(
        {
            "close": closes,
            "high": [c + 0.1 for c in closes],
            "low": [c - 0.1 for c in closes],
            "volume": [1_000_000.0] * 50,
        }
    )


def test_run_entry_gates_blocks_post_exit_shorter_than_requirement() -> None:
    base = load_config()
    ex = dict(base.get("execution") or {})
    ex["symbol_cooldown_minutes"] = 100.0
    ex["symbol_cooldown_etf_minutes"] = 0.0
    ex["symbol_cooldown_etf_symbols"] = []
    # Ensure entries sell cd does not dominate; execution max wins when larger.
    entries = dict((base.get("entries") or {}) if isinstance(base.get("entries"), dict) else {})
    entries["per_symbol_sell_cooldown_min"] = 5.0
    eng = TradingEngine(
        {**base, "execution": {**ex}, "entries": {**entries}},
    )
    eng.strategy.cooldown_after_profit_minutes = 0.0
    eng.strategy.cooldown_after_profit_partial_minutes = 0.0
    eng.strategy.cooldown_after_stop_minutes = 0.0
    sym = "SPY"
    dt_ex = datetime(2024, 6, 5, 10, 0, 0)
    eng.record_profit_exit(sym, dt_ex, 1.0, after_partial=False)
    dt_in = dt_ex + timedelta(minutes=10)
    d = eng.run_entry_gates(
        sym,
        dt_in,
        account_equity=100_000.0,
        current_positions={},
        sector_exposure_pct={},
        spread_pct=0.05,
        volume_atr_ratio=2.0,
        atr_pct=1.5,
        ohlcv_df=_df(),
    )
    assert not d.allowed
    assert "post-exit" in (d.reason or "").lower()
