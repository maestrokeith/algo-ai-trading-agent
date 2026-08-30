#!/usr/bin/env python3
"""
Example: run the trading engine with sample data (no live broker).
Demonstrates the full gate sequence for one symbol.
"""
import sys
from pathlib import Path

import pandas as pd
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.trading_engine import TradingEngine, TradingEngineState
from src.strategy import _atr


def make_sample_ohlcv(bars: int = 220, trend_up: bool = True) -> pd.DataFrame:
    """Generate minimal OHLCV for trend-following (price above 200 MA, pullback to 20 MA)."""
    np.random.seed(42)
    base = 400.0
    ret = 0.0003 if trend_up else -0.0002
    close = base * np.cumprod(1 + np.random.randn(bars) * 0.01 + ret)
    high = close * (1 + np.abs(np.random.randn(bars)) * 0.005)
    low = close * (1 - np.abs(np.random.randn(bars)) * 0.005)
    vol = np.random.randint(1_000_000, 10_000_000, bars)
    return pd.DataFrame({
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": vol,
    })


def main() -> None:
    config_path = PROJECT_ROOT / "config" / "default.yaml"
    engine = TradingEngine(config_path=config_path)

    account_equity = 100_000.0
    engine.update_equity(account_equity)

    symbol = "SPY"
    df = make_sample_ohlcv(220, trend_up=True)
    # Ensure last bar is pullback-ish: price near 20 MA
    ma20 = df["close"].rolling(20).mean()
    df.loc[df.index[-1], "close"] = ma20.iloc[-1] * 1.002

    atr = _atr(df["high"], df["low"], df["close"], 14)
    spread_pct = 0.05
    volume_atr_ratio = 1.5

    decision = engine.run_entry_gates(
        symbol=symbol,
        dt=pd.Timestamp("2025-02-19 10:30:00", tz="America/New_York"),
        account_equity=account_equity,
        current_positions={},
        sector_exposure_pct={"Technology": 0.0},
        spread_pct=spread_pct,
        volume_atr_ratio=volume_atr_ratio,
        atr_pct=1.0,
        ohlcv_df=df,
        symbol_sector={symbol: "Technology"},
    )

    print("Entry decision:", decision.allowed, "—", decision.reason)
    if decision.order_request:
        print("  Order:", decision.order_request)
    if decision.position_sizing:
        print("  Size:", decision.position_sizing.shares, "shares, risk %:", decision.position_sizing.risk_pct)


if __name__ == "__main__":
    main()
