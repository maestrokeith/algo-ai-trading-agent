"""Event-driven composite bonuses (volatility breakout, volume anomaly)."""

from __future__ import annotations

import pandas as pd
import pytest

from src.signal_ranking import (
    DEFAULT_COMPOSITE_WEIGHTS,
    confidence_score_trend_momentum_volume,
    event_triggers_strength_denom,
    trend_long_composite_rank,
)


def test_event_triggers_strength_denom_defaults() -> None:
    assert event_triggers_strength_denom(None) == pytest.approx(3.0)
    assert event_triggers_strength_denom({}) == pytest.approx(3.0)
    assert event_triggers_strength_denom({"enabled": False}) == pytest.approx(3.0)


def test_event_triggers_strength_denom_one_or_two_slots() -> None:
    et1 = {
        "enabled": True,
        "volatility_breakout": {"enabled": True},
        "volume_anomaly": {"enabled": False},
    }
    assert event_triggers_strength_denom(et1) == pytest.approx(4.0)
    et2 = {
        "enabled": True,
        "volatility_breakout": {"enabled": True},
        "volume_anomaly": {"enabled": True},
    }
    assert event_triggers_strength_denom(et2) == pytest.approx(5.0)


def _quiet_then_spike_ohlcv(*, n: int = 55) -> pd.DataFrame:
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    close = pd.Series([100.0] * n, index=idx)
    high = close + 0.2
    low = close - 0.2
    high.iloc[-1] = 180.0
    low.iloc[-1] = 20.0
    vol = pd.Series([500_000.0] * n, index=idx)
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


def test_volatility_breakout_adds_one_when_atr_spikes_vs_prior() -> None:
    df = _quiet_then_spike_ohlcv()
    et = {
        "enabled": True,
        "volatility_breakout": {"enabled": True, "atr_multiple": 1.5, "baseline_bars": 20},
        "volume_anomaly": {"enabled": False},
    }
    total, bd, den = trend_long_composite_rank(
        df,
        atr_pct=5.0,
        max_atr_pct=10.0,
        event_triggers=et,
        atr_period=14,
    )
    assert den == pytest.approx(4.0)
    assert bd.get("volatility_breakout", 0.0) == pytest.approx(1.0)
    assert "volume_anomaly" not in bd
    w = DEFAULT_COMPOSITE_WEIGHTS
    w01 = (
        w["trend_strength"] * bd["trend_strength"]
        + w["momentum"] * bd["momentum"]
        + w["volatility_expansion"] * bd["volatility_expansion"]
        + w["relative_strength"] * bd["relative_strength"]
    )
    assert total == pytest.approx(3.0 * w01 + bd["volatility_breakout"])


def test_volume_anomaly_adds_one_when_volume_doubles_prior_mean() -> None:
    n = 30
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    close = pd.Series([100.0] * n, index=idx)
    high = close + 0.5
    low = close - 0.5
    vol = pd.Series([1_000_000.0] * n, index=idx)
    vol.iloc[-1] = 3_000_000.0
    df = pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )
    et = {
        "enabled": True,
        "volatility_breakout": {"enabled": False},
        "volume_anomaly": {"enabled": True, "volume_multiple": 2.0, "avg_volume_bars": 20},
    }
    total, bd, den = trend_long_composite_rank(
        df,
        atr_pct=1.0,
        max_atr_pct=10.0,
        event_triggers=et,
        atr_period=14,
    )
    assert den == pytest.approx(4.0)
    assert bd.get("volume_anomaly", 0.0) == pytest.approx(1.0)
    assert "volatility_breakout" not in bd
    w = DEFAULT_COMPOSITE_WEIGHTS
    w01 = (
        w["trend_strength"] * bd["trend_strength"]
        + w["momentum"] * bd["momentum"]
        + w["volatility_expansion"] * bd["volatility_expansion"]
        + w["relative_strength"] * bd["relative_strength"]
    )
    assert total == pytest.approx(3.0 * w01 + bd["volume_anomaly"])


def test_confidence_score_is_sum_of_three_subscores() -> None:
    n = 220
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    close = pd.Series([100.0 + float(i) * 0.05 for i in range(n)], index=idx)
    high = close + 0.2
    low = close - 0.2
    vol = pd.Series([1_000_000.0] * n, index=idx)
    vol.iloc[-1] = 2_500_000.0
    df = pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )
    total, bd = confidence_score_trend_momentum_volume(df, ma_slow=200, momentum_bars=10, volume_bars=20)
    assert set(bd.keys()) == {"trend_strength", "momentum_strength", "volume_signal"}
    assert total == pytest.approx(
        bd["trend_strength"] + bd["momentum_strength"] + bd["volume_signal"], rel=1e-9, abs=1e-9
    )
    assert 0.0 <= total <= 3.0


def test_event_triggers_disabled_no_bonus_keys() -> None:
    df = _quiet_then_spike_ohlcv()
    total, bd, den = trend_long_composite_rank(
        df,
        atr_pct=5.0,
        max_atr_pct=10.0,
        event_triggers={"enabled": False},
        atr_period=14,
    )
    assert den == pytest.approx(3.0)
    assert "volatility_breakout" not in bd
    assert "volume_anomaly" not in bd
    w = DEFAULT_COMPOSITE_WEIGHTS
    w01 = (
        w["trend_strength"] * bd["trend_strength"]
        + w["momentum"] * bd["momentum"]
        + w["volatility_expansion"] * bd["volatility_expansion"]
        + w["relative_strength"] * bd["relative_strength"]
    )
    assert total == pytest.approx(3.0 * w01)
