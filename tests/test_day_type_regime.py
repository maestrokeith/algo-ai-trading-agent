"""Tests for intraday day-type regime classification."""

from __future__ import annotations

import pandas as pd

from src.day_type_regime import compute_day_type


def _minute_df(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """rows: (open, high, low, close) per minute."""
    return pd.DataFrame(
        rows,
        columns=["open", "high", "low", "close"],
    )


def test_day_type_chop() -> None:
    cfg = {"market_regime": {"day_types": {"enabled": True}}}
    # Flat session: tight range, flat return
    o = 500.0
    bars = []
    for i in range(40):
        px = o + (i % 3 - 1) * 0.05
        bars.append((px - 0.02, px + 0.03, px - 0.03, px))
    df = _minute_df(bars)
    r = compute_day_type(df, vix_last=None, vix_prev_close=None, config=cfg)
    assert r.day_type == "chop_day"


def test_day_type_trend_up() -> None:
    cfg = {"market_regime": {"day_types": {"enabled": True}}}
    o = 500.0
    bars = []
    c = o
    for _ in range(45):
        c = c + 0.15
        bars.append((c - 0.05, c + 0.08, c - 0.06, c))
    df = _minute_df(bars)
    r = compute_day_type(df, vix_last=None, vix_prev_close=None, config=cfg)
    assert r.day_type == "trend_day"


def test_day_type_panic() -> None:
    cfg = {"market_regime": {"day_types": {"enabled": True}}}
    o = 500.0
    c = o
    bars = []
    for _ in range(50):
        c = c - 0.35
        bars.append((c + 0.02, c + 0.05, c - 0.15, c))
    df = _minute_df(bars)
    r = compute_day_type(df, vix_last=22.0, vix_prev_close=18.0, config=cfg)
    assert r.day_type == "panic_selloff"


def test_day_type_unknown_empty() -> None:
    assert compute_day_type(None, vix_last=None, vix_prev_close=None, config={}).day_type == "unknown"


def test_profiles_adjust_multiplier() -> None:
    cfg = {
        "market_regime": {
            "day_types": {
                "enabled": True,
                "profiles": {
                    "chop_day": {"position_size_mult": 0.5},
                },
            }
        }
    }
    o = 500.0
    bars = []
    for i in range(40):
        px = o + (i % 3 - 1) * 0.05
        bars.append((px - 0.02, px + 0.03, px - 0.03, px))
    df = _minute_df(bars)
    r = compute_day_type(df, vix_last=None, vix_prev_close=None, config=cfg)
    assert r.day_type == "chop_day"
    assert r.position_size_mult == 0.5
