"""Tests for ``config["filters"]`` min range% / ATR% on listed symbols."""

from __future__ import annotations

import pandas as pd

from src.config_loader import load_config
from src.entry_filters import (
    adx_wilder_last,
    bar_range_pct_last_row,
    check_listed_defensive_tape_gates,
    check_trend_regime_filters,
    listed_defensive_filter_symbols,
)
from src.trading_engine import TradingEngine


def test_listed_defensive_filter_symbols() -> None:
    cfg = {
        "filters": {
            "symbols": ["ko", "XLP"],
        }
    }
    assert listed_defensive_filter_symbols(cfg) == frozenset({"KO", "XLP"})


def test_bar_range_pct_last_row() -> None:
    df = pd.DataFrame(
        {
            "high": [10.0, 11.0, 10.2],
            "low": [9.5, 10.0, 9.0],
            "close": [9.8, 10.5, 10.0],
        }
    )
    r = bar_range_pct_last_row(df)
    # last: (10.2 - 9.0) / 10.0 * 100 = 12.0
    assert r is not None and 11.99 < r < 12.01


def test_tape_gates_allows_unlisted() -> None:
    cfg = load_config()
    df = _df_flat()
    r = check_listed_defensive_tape_gates(cfg, "SPY", df, 0.1)
    assert r.allowed


def test_tape_gates_blocks_low_range() -> None:
    cfg = {
        "filters": {
            "min_intraday_range_pct": 1.0,
            "min_atr_pct": 0.0,
            "symbols": ["ZZZ"],
        }
    }
    # range ~0.1%
    df = _df_small_range()
    r = check_listed_defensive_tape_gates(cfg, "ZZZ", df, 2.0)
    assert not r.allowed
    assert "range%" in (r.reason or "")


def test_tape_gates_blocks_low_atr() -> None:
    cfg = {
        "filters": {
            "min_intraday_range_pct": 0.0,
            "min_atr_pct": 1.2,
            "symbols": ["ZZZ"],
        }
    }
    df = _df_wide()
    r = check_listed_defensive_tape_gates(cfg, "ZZZ", df, 0.4)
    assert not r.allowed
    assert "ATR%" in (r.reason or "")


def test_tape_gates_allows_pass() -> None:
    cfg = {
        "filters": {
            "min_intraday_range_pct": 0.5,
            "min_atr_pct": 0.5,
            "symbols": ["ZZZ"],
        }
    }
    df = _df_wide()
    r = check_listed_defensive_tape_gates(cfg, "ZZZ", df, 1.5)
    assert r.allowed


def test_trading_engine_run_entry_gates_applies_tape_filter() -> None:
    base = load_config()
    f = {
        "min_intraday_range_pct": 0.0,
        "min_atr_pct": 2.0,
        "symbols": ["SPY"],
    }
    eng = TradingEngine({**base, "filters": f})
    eng.strategy.cooldown_after_stop_minutes = 0.0
    eng.strategy.cooldown_after_profit_minutes = 0.0
    eng.strategy.cooldown_after_profit_partial_minutes = 0.0
    ex = dict(base.get("execution") or {})
    ex["symbol_cooldown_minutes"] = 0.0
    ex["symbol_cooldown_etf_minutes"] = 0.0
    ex["symbol_cooldown_etf_symbols"] = []
    eng.config = {**eng.config, "execution": {**ex}}
    d = eng.run_entry_gates(
        "SPY",
        _dt(),
        account_equity=200_000.0,
        current_positions={},
        sector_exposure_pct={},
        spread_pct=0.1,
        volume_atr_ratio=1.0,
        atr_pct=0.1,
        ohlcv_df=_df_wide(60),
    )
    assert not d.allowed
    assert "defensive tape filter" in (d.reason or "").lower()


def _df_flat() -> pd.DataFrame:
    return _df_small_range()


def _df_small_range() -> pd.DataFrame:
    c = 100.0
    n = 40
    return pd.DataFrame(
        {
            "close": [c] * n,
            "high": [c * 1.0005] * n,
            "low": [c * 0.9995] * n,
            "volume": [1e6] * n,
        }
    )


def _df_wide(n: int = 30) -> pd.DataFrame:
    c = 100.0
    r = 3.0
    return pd.DataFrame(
        {
            "close": [c + i * 0.02 for i in range(n)],
            "high": [c + r + i * 0.02 for i in range(n)],
            "low": [c - r * 0.1 + i * 0.02 for i in range(n)],
            "volume": [1e6] * n,
        }
    )


def _dt():
    from datetime import datetime

    return datetime(2024, 6, 5, 10, 30, 0)


def _df_downtrend(n: int = 50) -> pd.DataFrame:
    c = 200.0
    closes = [c - i * 0.3 for i in range(n)]
    return pd.DataFrame(
        {
            "close": closes,
            "high": [x + 0.2 for x in closes],
            "low": [x - 0.2 for x in closes],
            "volume": [1e6] * n,
        }
    )


def test_trend_regime_all_off_allows() -> None:
    cfg = {"filters": {"require_adx": False, "require_price_above_20ema": False, "require_20ema_slope_positive": False}}
    r = check_trend_regime_filters(cfg, "SPY", _df_downtrend(50))
    assert r.allowed


def test_trend_regime_ema_fails_downtrend() -> None:
    cfg = {
        "filters": {
            "require_price_above_20ema": True,
            "require_20ema_slope_positive": True,
        }
    }
    r = check_trend_regime_filters(cfg, "SPY", _df_downtrend(50))
    assert not r.allowed
    assert "trend filter" in (r.reason or "").lower()


def test_trend_regime_ema_slope_tolerance_allows_strong_dynamic_context() -> None:
    closes = [100.0 + i * 0.01 for i in range(24)] + [100.20, 100.19, 100.18]
    df = pd.DataFrame(
        {
            "close": closes,
            "high": [x + 0.2 for x in closes],
            "low": [x - 0.2 for x in closes],
            "volume": [1_000_000] * len(closes),
        }
    )
    cfg = {
        "filters": {
            "require_price_above_20ema": False,
            "require_20ema_slope_positive": True,
            "ema_slope_tolerance": {
                "enabled": True,
                "max_negative_slope_pct": 0.05,
                "strong_momentum_score_min": 80,
                "allow_breakout_continuation": True,
            },
        }
    }
    r = check_trend_regime_filters(
        cfg,
        "AVGO",
        df,
        momentum_score=90.0,
        breakout_continuation=True,
    )
    assert r.allowed


def test_trend_regime_ema_ok_uptrend() -> None:
    cfg = {
        "filters": {
            "require_price_above_20ema": True,
            "require_20ema_slope_positive": True,
        }
    }
    r = check_trend_regime_filters(cfg, "SPY", _df_wide(60))
    assert r.allowed


def test_adx_wilder_trending_series() -> None:
    df = _df_wide(80)
    a = adx_wilder_last(df, period=14)
    assert a is not None
    assert a >= 0.0


def test_trend_regime_adx_blocks_below_floor() -> None:
    df = _df_wide(80)
    a = adx_wilder_last(df, period=14)
    assert a is not None
    floor = a + 5.0
    cfg = {
        "filters": {
            "require_adx": True,
            "adx_min": floor,
            "adx_period": 14,
            "require_price_above_20ema": False,
            "require_20ema_slope_positive": False,
        }
    }
    r = check_trend_regime_filters(cfg, "SPY", df)
    assert not r.allowed
    assert "adx" in (r.reason or "").lower()
    assert "<" in (r.reason or "")
