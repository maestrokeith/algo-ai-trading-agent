import pandas as pd

from engine.signal_engine import decide_signal, prepare_features
from engine.trading_config import StrategyConfig


def test_decide_signal_accepts_full_long_confluence():
    cfg = StrategyConfig()
    spec = cfg.instrument("EURUSD")
    row = {"close": 1.1010, "ema_fast": 1.1005, "ema_slow": 1.1000, "rsi": 54.0, "rsi_prev": 52.0, "atr": 0.0005, "atr_pct": 0.00045, "atr_median": 0.0005, "volume": 150.0, "volume_ma": 100.0, "spread": 0.00010, "m5_trend": 1, "m15_trend": 1}
    decision = decide_signal(row, spec, cfg)
    assert decision.side == 1
    assert decision.reason == "long_confluence"


def test_spread_filter_blocks_signal():
    cfg = StrategyConfig()
    spec = cfg.instrument("EURUSD")
    row = {"close": 1.1010, "ema_fast": 1.1005, "ema_slow": 1.1000, "rsi": 54.0, "rsi_prev": 52.0, "atr": 0.0005, "atr_pct": 0.00045, "atr_median": 0.0005, "volume": 150.0, "volume_ma": 100.0, "spread": 0.001, "m5_trend": 1, "m15_trend": 1}
    assert decide_signal(row, spec, cfg).reason == "spread"


def test_htf_alignment_is_shifted_to_avoid_lookahead():
    idx = pd.date_range("2026-01-01", periods=4000, freq="min")
    price = pd.Series(range(len(idx)), index=idx, dtype=float) * 0.00001 + 1.1
    frame = pd.DataFrame({"open": price, "high": price + 0.0001, "low": price - 0.0001, "close": price + 0.00002, "volume": 100.0}, index=idx)
    features = prepare_features(frame, "EURUSD", StrategyConfig())
    assert features["m15_ema_slow"].iloc[:3000].isna().any()
