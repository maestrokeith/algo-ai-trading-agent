"""Relaxed ATR caps when market regime score >= configured bullish threshold."""

from __future__ import annotations

import logging

import pytest

from src.position_sizing import PositionSizer
from src.strategy import TrendFollowingStrategy
from src.trade_filters import VolatilityDoNotTrade


def test_volatility_dnt_default_max_atr_pct_is_six() -> None:
    """When YAML omits max_atr_pct, align with config/default.yaml (6.0)."""
    cfg = {"trade_filters": {"volatility_do_not_trade": {"enabled": True}}}
    vd = VolatilityDoNotTrade(cfg)
    assert vd.max_atr_pct == 6.0
    assert vd.max_atr_pct_bullish_regime == 6.0


def test_volatility_dnt_logs_atr_threshold_vol_flag(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="src.trade_filters")
    cfg = {
        "trade_filters": {
            "volatility_do_not_trade": {
                "enabled": True,
                "max_atr_pct": 5.0,
                "max_atr_pct_bullish_regime": 5.0,
                "bullish_regime_min_score": 4,
            }
        }
    }
    vd = VolatilityDoNotTrade(cfg)
    vd.check(atr_pct=6.0, spread_pct=None, symbol="SPY", regime_score=2)
    assert "vol_flag=True" in caplog.text and "ATR=6.0" in caplog.text


def test_volatility_dnt_max_atr_pct_regime_takes_precedence() -> None:
    cfg = {
        "trade_filters": {
            "volatility_do_not_trade": {
                "enabled": True,
                "max_atr_pct": 99.0,
                "max_atr_pct_bullish_regime": 99.0,
                "bullish_regime_min_score": 4,
                "max_atr_pct_regime": {
                    "defensive": 3.0,
                    "neutral": 4.2,
                    "bullish": 5.5,
                },
            }
        }
    }
    vd = VolatilityDoNotTrade(cfg)
    assert vd.check(atr_pct=3.5, spread_pct=None, regime_condition="defensive", regime_score=5).allowed is False
    assert vd.check(atr_pct=4.0, spread_pct=None, regime_condition="neutral", regime_score=5).allowed is True
    assert vd.check(atr_pct=5.6, spread_pct=None, regime_condition="bullish", regime_score=2).allowed is False
    assert vd.check(atr_pct=5.5, spread_pct=None, regime_condition="bullish", regime_score=2).allowed is True


def test_volatility_dnt_per_symbol_spread_from_market_quality() -> None:
    cfg = {
        "market_quality": {"symbol_max_spread_pct": {"SPY": 0.3, "BABA": 1.5}},
        "trade_filters": {"volatility_do_not_trade": {"enabled": True, "max_spread_pct": 1.0}},
    }
    vd = VolatilityDoNotTrade(cfg)
    assert vd.check(atr_pct=1.0, spread_pct=0.5, symbol="SPY").allowed is False
    assert vd.check(atr_pct=1.0, spread_pct=0.25, symbol="SPY").allowed is True
    assert vd.check(atr_pct=1.0, spread_pct=1.4, symbol="BABA").allowed is True
    assert vd.check(atr_pct=1.0, spread_pct=1.55, symbol="BABA").allowed is False


def test_effective_max_atr_higher_when_regime_ge_4() -> None:
    cfg = {
        "strategy": {
            "trend_following": {
                "ma_fast": 10,
                "ma_slow": 50,
                "entry_mode": "momentum",
                "max_atr_pct_for_entry": 4.0,
                "max_atr_pct_for_entry_bullish": 6.0,
                "bullish_regime_min_score_for_atr": 4,
            }
        }
    }
    st = TrendFollowingStrategy(cfg)
    assert st.effective_max_atr_pct_for_entry(3) == 4.0
    assert st.effective_max_atr_pct_for_entry(4) == 6.0
    assert st.effective_max_atr_pct_for_entry(5) == 6.0


def test_effective_max_atr_none_uses_base() -> None:
    cfg = {
        "strategy": {
            "trend_following": {
                "ma_fast": 10,
                "ma_slow": 50,
                "max_atr_pct_for_entry": 4.0,
                "max_atr_pct_for_entry_bullish": 6.0,
            }
        }
    }
    st = TrendFollowingStrategy(cfg)
    assert st.effective_max_atr_pct_for_entry(None) == 4.0


def test_volatility_dnt_relaxed_when_regime_high() -> None:
    cfg = {
        "trade_filters": {
            "volatility_do_not_trade": {
                "enabled": True,
                "max_atr_pct": 6.0,
                "max_atr_pct_bullish_regime": 8.0,
                "bullish_regime_min_score": 4,
            }
        }
    }
    vd = VolatilityDoNotTrade(cfg)
    assert vd.check(atr_pct=7.0, spread_pct=None, symbol="SPY", regime_score=3).allowed is False
    assert vd.check(atr_pct=7.0, spread_pct=None, symbol="SPY", regime_score=4).allowed is True


def test_allow_high_volatility_min_regime_score_at_3() -> None:
    """market_regime.allow_high_volatility_min_regime_score widens ATR gates from regime >= that score."""
    cfg = {
        "market_regime": {"allow_high_volatility_min_regime_score": 3},
        "strategy": {
            "trend_following": {
                "ma_fast": 10,
                "ma_slow": 50,
                "max_atr_pct_for_entry": 4.0,
                "max_atr_pct_for_entry_bullish": 6.0,
                "bullish_regime_min_score_for_atr": 4,
            }
        },
        "trade_filters": {
            "volatility_do_not_trade": {
                "enabled": True,
                "max_atr_pct": 6.0,
                "max_atr_pct_bullish_regime": 8.0,
                "bullish_regime_min_score": 4,
            }
        },
    }
    st = TrendFollowingStrategy(cfg)
    assert st.effective_max_atr_pct_for_entry(2) == 4.0
    assert st.effective_max_atr_pct_for_entry(3) == 6.0
    vd = VolatilityDoNotTrade(cfg)
    assert vd.check(atr_pct=7.0, spread_pct=None, symbol="SPY", regime_score=3).allowed is True


def test_high_vol_size_haircut_skipped_when_regime_allows() -> None:
    cfg = {
        "market_regime": {"allow_high_volatility_min_regime_score": 3},
        "position_sizing": {
            "risk_per_trade_pct": 1.0,
            "volatility_sizing": {"enabled": False},
            "high_vol_reduction": {"enabled": True, "atr_pct_threshold": 2.0, "size_multiplier": 0.5},
        },
    }
    sz = PositionSizer(cfg)
    base = sz.size_position(
        100_000.0, 100.0, 3.0, "QQQ", {}, {}, atr_pct=1.5, regime_score=2
    )
    hi = sz.size_position(
        100_000.0, 100.0, 3.0, "QQQ", {}, {}, atr_pct=5.0, regime_score=2
    )
    ok = sz.size_position(
        100_000.0, 100.0, 3.0, "QQQ", {}, {}, atr_pct=5.0, regime_score=3
    )
    assert hi.shares == max(1, int(base.shares * 0.5))
    assert ok.shares == base.shares
