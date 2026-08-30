"""Composite entry signal strength on :class:`~src.strategy.EntrySignal` (not binary 1.0)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.strategy import TrendFollowingStrategy


def _uptrend_df(*, n: int = 120) -> pd.DataFrame:
    t = np.arange(n, dtype=float)
    close = 100.0 + t * 0.08 + np.sin(t / 12.0) * 0.4
    high = close + 0.25
    low = close - 0.25
    vol = np.full(n, 2e7)
    vol[-1] = 3e7
    return pd.DataFrame({"high": high, "low": low, "close": close, "volume": vol})


def _strategy_cfg(**tf_overrides: object) -> dict:
    tf = {
        "entry_mode": "momentum",
        "ma_fast": 10,
        "ma_slow": 50,
        "max_atr_pct_for_entry": 10.0,
        "entry_signal_strength": {"enabled": True},
    }
    tf.update(tf_overrides)
    return {
        "strategy": {
            "trend_following": tf,
            "exits": {
                "stop_loss_pct": 2.0,
                "kill_switch": {"max_spread_pct": 1.0, "max_atr_pct": 12.0},
            },
        },
        "portfolio": {"signal_ranking": {"event_triggers": {"enabled": False}}},
        "position_sizing": {"confidence_sizing": {"momentum_bars": 10, "volume_bars": 20}},
    }


def test_generate_entry_strength_is_normalized_composite() -> None:
    st = TrendFollowingStrategy(_strategy_cfg())
    df = _uptrend_df()
    sig = st.generate_entry("TEST", df, spread_pct=0.05, atr_pct_now=1.0, regime_score=4)
    assert sig is not None
    assert 0.0 < sig.strength <= 1.0
    assert sig.metadata.get("entry_signal_strength_source") == "composite_rank"
    assert "composite_breakdown" in sig.metadata


def test_entry_signal_strength_disabled_is_full_scale() -> None:
    st = TrendFollowingStrategy(_strategy_cfg(entry_signal_strength={"enabled": False}))
    df = _uptrend_df()
    sig = st.generate_entry("TEST", df, spread_pct=0.05, atr_pct_now=1.0, regime_score=4)
    assert sig is not None
    assert sig.strength == pytest.approx(1.0)
