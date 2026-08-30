"""Tests for momentum fade exit and time_based_trim strategy flags."""

from __future__ import annotations

import pandas as pd
import pytest

from src.strategy import ExitReason, TrendFollowingStrategy


def _cfg_mf(**overrides: object) -> dict:
    base = {
        "strategy": {
            "trend_following": {"ma_fast": 10, "ma_slow": 50, "entry_mode": "momentum"},
            "exits": {
                "stop_loss_pct": 15.0,
                "momentum_fade": {
                    "enabled": True,
                    "close_below_fast_ma": True,
                    "min_profit_pct": 0.0,
                    "rsi_confirm": {"enabled": False},
                },
            },
        }
    }
    if overrides:
        deep = dict(base)
        deep["strategy"] = dict(base["strategy"])
        deep["strategy"]["exits"] = {**base["strategy"]["exits"], **overrides.get("exits", {})}
        if "trend_following" in overrides:
            deep["strategy"]["trend_following"] = {
                **base["strategy"]["trend_following"],
                **overrides["trend_following"],
            }
        return deep
    return base


def test_evaluate_momentum_fade_exit_triggers_below_ma() -> None:
    st = TrendFollowingStrategy(_cfg_mf())
    # Ramp then last bar dips below MA10 while still above entry (min_profit ≥ 0).
    close = [100.0 + i * 0.12 for i in range(20)] + [102.4, 102.6, 102.8, 103.0, 101.5]
    df = pd.DataFrame({"close": close, "high": close, "low": close})
    sig = st.evaluate_momentum_fade_exit(
        "TEST",
        df,
        entry_price=100.0,
        current_price=float(close[-1]),
        minutes_held=120.0,
    )
    assert sig is not None
    assert sig.reason == ExitReason.MOMENTUM_FADE


def test_momentum_fade_disabled_returns_none() -> None:
    st = TrendFollowingStrategy(
        {
            "strategy": {
                "trend_following": {"ma_fast": 10, "ma_slow": 50},
                "exits": {"momentum_fade": {"enabled": False}},
            }
        }
    )
    close = list(range(100, 125))
    df = pd.DataFrame({"close": [float(x) for x in close], "high": close, "low": close})
    assert st.evaluate_momentum_fade_exit("X", df, 100.0, 124.0, minutes_held=60.0) is None


def test_check_exit_momentum_fade_with_df() -> None:
    st = TrendFollowingStrategy(_cfg_mf())
    close = [100.0 + i * 0.08 for i in range(28)] + [102.2, 102.4, 101.0]
    df = pd.DataFrame({"close": close, "high": close, "low": close})
    last_px = float(close[-1])
    ex = st.check_exit(
        "NVDA",
        100.0,
        last_px,
        bars_held=5,
        partial_taken=False,
        trail_high=None,
        current_qty=10,
        minutes_held=200.0,
        ohlcv_df=df,
    )
    assert ex is not None
    assert ex.reason == ExitReason.MOMENTUM_FADE


def test_time_based_trim_flags_parse() -> None:
    st = TrendFollowingStrategy(
        {
            "strategy": {
                "trend_following": {"ma_fast": 10},
                "exits": {
                    "time_based_trim": {
                        "enabled": True,
                        "after_minutes": 180,
                        "trim_fraction": 0.25,
                        "min_profit_pct": 0.5,
                    }
                },
            }
        }
    )
    assert st.time_based_trim_enabled is True
    assert st.time_based_trim_after_minutes == pytest.approx(180.0)
    assert st.time_based_trim_fraction == pytest.approx(0.25)
    assert st.time_based_trim_min_profit_pct == pytest.approx(0.5)
