"""Confidence-based share scaling (trend + momentum + volume)."""

from __future__ import annotations

import pandas as pd
import pytest

from src.position_sizing import PositionSizer


def _base_cfg() -> dict:
    return {
        "strategy": {"trend_following": {"ma_slow": 200}},
        "position_sizing": {
            "risk_per_trade_pct": 1.0,
            "max_open_risk_pct": 10.0,
            "max_exposure_per_symbol_pct": 100.0,
            "max_position_dollar_cap": 0,
            "max_exposure_per_sector_pct": 100.0,
            "volatility_sizing": {
                "enabled": True,
                "atr_risk_multiple": 1.0,
                "conviction_min_scale": 1.0,
                "conviction_max_scale": 1.0,
            },
            "portfolio_heat": {"enabled": False},
            "high_vol_reduction": {"enabled": False},
            "confidence_sizing": {
                "enabled": True,
                "momentum_bars": 10,
                "volume_bars": 20,
            },
        },
    }


def _daily_df(*, n: int = 220) -> pd.DataFrame:
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    close = pd.Series([100.0 + float(i) * 0.05 for i in range(n)], index=idx)
    high = close + 0.3
    low = close - 0.3
    vol = pd.Series([1_000_000.0] * n, index=idx)
    vol.iloc[-1] = 2_500_000.0
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


def test_confidence_sizing_rejects_without_ohlcv() -> None:
    sz = PositionSizer(_base_cfg())
    r = sz.size_position(
        100_000.0,
        100.0,
        1.5,
        "SPY",
        {},
        {},
        atr_pct=1.0,
        conviction_score=1.0,
    )
    assert r.shares == 0
    assert r.reject_reason == "confidence_sizing enabled but ohlcv_df missing or empty"


def test_confidence_sizing_multiplies_shares(monkeypatch: pytest.MonkeyPatch) -> None:
    sz = PositionSizer(_base_cfg())
    df = _daily_df()

    def _fake_conf(*_a: object, **_k: object) -> tuple[float, dict[str, float]]:
        return 0.5, {"trend_strength": 0.2, "momentum_strength": 0.2, "volume_signal": 0.1}

    monkeypatch.setattr("src.position_sizing.confidence_score_trend_momentum_volume", _fake_conf)
    base = sz.size_position(
        100_000.0,
        100.0,
        1.5,
        "SPY",
        {},
        {},
        atr_pct=1.0,
        conviction_score=1.0,
        ohlcv_df=df,
    )
    assert base.confidence_score == pytest.approx(0.5)
    assert base.shares == max(0, int(round(1000 * 0.5)))
