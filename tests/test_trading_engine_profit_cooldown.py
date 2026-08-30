"""Profit re-entry cooldown: shorter after partial exits."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from src.config_loader import load_config
from src.trading_engine import TradingEngine


def _df_uptrend(n: int = 55) -> pd.DataFrame:
    base = 400.0
    closes = [base + i * 0.5 for i in range(n)]
    return pd.DataFrame(
        {
            "close": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "volume": [10_000_000.0] * n,
        }
    )


@pytest.fixture
def engine_cooldown() -> TradingEngine:
    """Full default config so SPY passes universe; override cooldowns for assertions."""
    base = load_config()
    ex = dict(base.get("execution") or {}) if isinstance(base.get("execution"), dict) else {}
    ex["symbol_cooldown_minutes"] = 0.0
    ex["symbol_cooldown_etf_minutes"] = 0.0
    ex["symbol_cooldown_etf_symbols"] = []
    cfg = {**base, "execution": {**ex}}
    engine = TradingEngine(config=cfg)
    engine.strategy.cooldown_after_profit_minutes = 15.0
    engine.strategy.cooldown_after_profit_partial_minutes = 2.0
    return engine


def test_strategy_loads_partial_profit_cooldown(engine_cooldown: TradingEngine) -> None:
    assert engine_cooldown.strategy.cooldown_after_profit_minutes == 15.0
    assert engine_cooldown.strategy.cooldown_after_profit_partial_minutes == 2.0


def test_record_profit_exit_marks_partial(engine_cooldown: TradingEngine) -> None:
    dt = datetime(2024, 6, 5, 14, 0, 0)
    engine_cooldown.record_profit_exit("SPY", dt, 100.0, after_partial=True)
    assert engine_cooldown.state.last_profit_exit_was_partial.get("SPY") is True
    engine_cooldown.record_profit_exit("SPY", dt, 101.0, after_partial=False)
    assert engine_cooldown.state.last_profit_exit_was_partial.get("SPY") is False


def test_run_entry_gates_shorter_cooldown_after_partial_exit(engine_cooldown: TradingEngine) -> None:
    dt_exit = datetime(2024, 6, 5, 14, 30, 0)
    # Exit below last bar close (~427) so require_price_above_exit_after_profit does not mask cooldown.
    engine_cooldown.record_profit_exit("SPY", dt_exit, 400.0, after_partial=True)
    df = _df_uptrend()
    dt_block = datetime(2024, 6, 5, 14, 31, 0)
    d = engine_cooldown.run_entry_gates(
        "SPY",
        dt_block,
        account_equity=100_000.0,
        current_positions={},
        sector_exposure_pct={},
        spread_pct=0.05,
        volume_atr_ratio=2.0,
        atr_pct=1.5,
        ohlcv_df=df,
    )
    assert not d.allowed
    assert "3" in (d.reason or "") or "min" in (d.reason or "").lower()

    dt_ok = datetime(2024, 6, 5, 14, 34, 0)
    d2 = engine_cooldown.run_entry_gates(
        "SPY",
        dt_ok,
        account_equity=100_000.0,
        current_positions={},
        sector_exposure_pct={},
        spread_pct=0.05,
        volume_atr_ratio=2.0,
        atr_pct=1.5,
        ohlcv_df=df,
    )
    assert "cooldown after profit" not in (d2.reason or "").lower()


def test_default_partial_profit_cooldown_is_two() -> None:
    e = TradingEngine({"strategy": {"exits": {"cooldown_after_profit_minutes": 20}}})
    assert e.strategy.cooldown_after_profit_partial_minutes == 2.0
