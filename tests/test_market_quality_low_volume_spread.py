"""Low last-bar volume can bypass spread caps (market quality, volatility DNT, execution)."""

from __future__ import annotations

import pandas as pd
import pytest

from src.execution import ExecutionManager
from src.trade_filters import VolatilityDoNotTrade
from src.universe import (
    MarketQualityGate,
    last_bar_volume_from_ohlcv,
    max_spread_pct_for_symbol_resolved,
    spread_cap_tiers_from_mq,
    spread_relief_scale_factor,
    spread_volume_low_bypass,
)


def test_last_bar_volume_from_ohlcv_none_and_column() -> None:
    assert last_bar_volume_from_ohlcv(None) is None
    assert last_bar_volume_from_ohlcv(pd.DataFrame()) is None
    df = pd.DataFrame({"close": [1.0, 2.0]})
    assert last_bar_volume_from_ohlcv(df) is None
    df2 = pd.DataFrame({"close": [1.0, 2.0], "volume": [100, 250]})
    assert last_bar_volume_from_ohlcv(df2) == 250.0


def test_spread_volume_low_bypass() -> None:
    assert spread_volume_low_bypass(None, 50.0) is False
    assert spread_volume_low_bypass(100.0, None) is False
    assert spread_volume_low_bypass(100.0, 99.0) is True
    assert spread_volume_low_bypass(100.0, 100.0) is False


def test_spread_caps_aliases_large_caps_and_volatile() -> None:
    tiers = spread_cap_tiers_from_mq(
        {"spread_caps": {"large_caps": 1.0, "normal": 2.0, "volatile": 2.5}}
    )
    assert tiers is not None
    assert tiers.mega_caps == pytest.approx(1.0)
    assert tiers.normal == pytest.approx(2.0)
    assert tiers.high_vol == pytest.approx(2.5)


def test_spread_caps_tiers_mega_normal_high() -> None:
    mq = {
        "spread_caps": {"mega_caps": 1.5, "normal": 2.5, "high_vol": 4.0},
        "high_vol_symbols": ["COIN"],
    }
    tiers = spread_cap_tiers_from_mq(mq)
    assert tiers is not None
    assert tiers.mega_caps == 1.5 and tiers.normal == 2.5 and tiers.high_vol == 4.0
    hv = frozenset({"COIN"})
    assert (
        max_spread_pct_for_symbol_resolved(
            "AAPL",
            symbol_max_spread_pct={},
            spread_tiers=tiers,
            high_vol_union=hv,
            legacy_max_spread_pct=99.0,
            legacy_high_vol_max_spread_pct=99.0,
        )
        == 1.5
    )
    assert (
        max_spread_pct_for_symbol_resolved(
            "AMD",
            symbol_max_spread_pct={},
            spread_tiers=tiers,
            high_vol_union=hv,
            legacy_max_spread_pct=99.0,
            legacy_high_vol_max_spread_pct=99.0,
        )
        == 2.5
    )
    assert (
        max_spread_pct_for_symbol_resolved(
            "COIN",
            symbol_max_spread_pct={},
            spread_tiers=tiers,
            high_vol_union=hv,
            legacy_max_spread_pct=99.0,
            legacy_high_vol_max_spread_pct=99.0,
        )
        == 4.0
    )


def test_spread_caps_custom_mega_list() -> None:
    mq = {
        "spread_caps": {
            "mega_caps": 1.0,
            "normal": 3.0,
            "high_vol": 5.0,
            "mega_cap_symbols": ["ZZZ"],
        }
    }
    tiers = spread_cap_tiers_from_mq(mq)
    assert tiers is not None
    assert "ZZZ" in tiers.mega_cap_symbols
    assert "AAPL" not in tiers.mega_cap_symbols


def test_liquid_spread_relief_allows_mega_cap_with_scale() -> None:
    cfg = {
        "market_quality": {
            "spread_caps": {"large_caps": 1.0, "normal": 2.5, "high_vol": 4.0},
            "high_vol_symbols": ["COIN"],
            "liquid_spread_relief": {
                "enabled": True,
                "include_mega_cap_tiers": True,
            },
        }
    }
    g = MarketQualityGate(cfg)
    r = g.check(
        symbol="AAPL",
        spread_pct=1.6,
        volume_atr_ratio=2.0,
        last_bar_volume=1_000_000.0,
    )
    assert r.ok
    assert r.spread_position_scale < 1.0
    assert r.spread_position_scale == pytest.approx(spread_relief_scale_factor(1.6, 1.0, min_scale=0.15))


def test_market_quality_gate_spread_caps_integration() -> None:
    cfg = {
        "market_quality": {
            "spread_caps": {"mega_caps": 1.5, "normal": 2.5, "high_vol": 4.0},
            "high_vol_symbols": ["COIN"],
        }
    }
    g = MarketQualityGate(cfg)
    assert not g.check(symbol="AAPL", spread_pct=1.6, volume_atr_ratio=2.0).ok
    assert g.check(symbol="AAPL", spread_pct=1.4, volume_atr_ratio=2.0).ok
    assert g.check(symbol="AMD", spread_pct=2.4, volume_atr_ratio=2.0).ok
    assert not g.check(symbol="AMD", spread_pct=2.6, volume_atr_ratio=2.0).ok
    assert g.check(symbol="COIN", spread_pct=3.5, volume_atr_ratio=2.0).ok


def test_market_quality_gate_symbol_max_spread_pct() -> None:
    cfg = {
        "market_quality": {
            "max_spread_pct": 1.0,
            "symbol_max_spread_pct": {"QQQ": 0.3, "NVDA": 0.7},
        }
    }
    g = MarketQualityGate(cfg)
    assert not g.check(symbol="QQQ", spread_pct=0.5, volume_atr_ratio=2.0).ok
    assert g.check(symbol="QQQ", spread_pct=0.25, volume_atr_ratio=2.0).ok
    assert not g.check(symbol="NVDA", spread_pct=0.8, volume_atr_ratio=2.0).ok
    assert g.check(symbol="IWM", spread_pct=0.9, volume_atr_ratio=2.0).ok


def test_market_quality_gate_spread_bypass_low_volume() -> None:
    cfg = {
        "market_quality": {
            "max_spread_pct": 0.5,
            "ignore_spread_when_last_bar_volume_below": 1_000_000.0,
        }
    }
    g = MarketQualityGate(cfg)
    bad = g.check(symbol="QQQ", spread_pct=2.0, last_bar_volume=500_000.0)
    assert bad.ok
    bad2 = g.check(symbol="QQQ", spread_pct=2.0, last_bar_volume=2_000_000.0)
    assert not bad2.ok


def test_market_quality_volatility_spike_cap_dynamic_vs_core() -> None:
    cfg = {
        "market_quality": {
            "block_on_news_spike": True,
            "news_volatility_spike_atr_pct": 8.0,
            "news_volatility_spike_atr_pct_dynamic": 14.0,
            "max_spread_pct": 99.0,
            "min_volume_atr_ratio": 0.0,
        }
    }
    g = MarketQualityGate(cfg)
    assert not g.check(
        symbol="SPY",
        spread_pct=0.1,
        volume_atr_ratio=2.0,
        current_atr_pct=10.0,
        is_dynamic_universe_symbol=False,
    ).ok
    assert g.check(
        symbol="RUNNER",
        spread_pct=0.1,
        volume_atr_ratio=2.0,
        current_atr_pct=10.0,
        is_dynamic_universe_symbol=True,
    ).ok
    assert not g.check(
        symbol="RUNNER",
        spread_pct=0.1,
        volume_atr_ratio=2.0,
        current_atr_pct=15.0,
        is_dynamic_universe_symbol=True,
    ).ok


def test_market_quality_volatility_spike_cap_override_for_momentum_breakout() -> None:
    cfg = {
        "market_quality": {
            "block_on_news_spike": True,
            "news_volatility_spike_atr_pct": 8.0,
            "news_volatility_spike_atr_pct_dynamic": 14.0,
            "max_spread_pct": 99.0,
            "min_volume_atr_ratio": 0.0,
        }
    }
    g = MarketQualityGate(cfg)
    assert g.check(
        symbol="RUNNER",
        spread_pct=0.1,
        volume_atr_ratio=2.0,
        current_atr_pct=14.99,
        is_dynamic_universe_symbol=True,
        volatility_spike_atr_cap_override=15.0,
    ).ok


def test_volatility_dnt_respects_mq_spread_caps() -> None:
    cfg = {
        "market_quality": {
            "spread_caps": {"mega_caps": 1.5, "normal": 2.5, "high_vol": 4.0},
            "high_vol_symbols": ["COIN"],
        },
        "trade_filters": {
            "volatility_do_not_trade": {
                "enabled": True,
                "max_spread_pct": 0.5,
                "high_vol_max_spread_pct": 0.5,
            }
        },
    }
    dnt = VolatilityDoNotTrade(cfg)
    assert dnt.check(spread_pct=3.0, atr_pct=1.0, symbol="COIN").allowed
    assert not dnt.check(spread_pct=4.5, atr_pct=1.0, symbol="COIN").allowed


def test_volatility_dnt_uses_runtime_dynamic_symbols_for_dynamic_atr_cap() -> None:
    cfg = {
        "dynamic_universe": {
            "max_atr_pct": 12.0,
            "leader_pools": {"ai": ["SMCI"]},
        },
        "trade_filters": {
            "volatility_do_not_trade": {
                "enabled": True,
                "max_atr_pct": 5.0,
            }
        },
    }
    dnt = VolatilityDoNotTrade(cfg)
    assert not dnt.check(atr_pct=8.0, symbol="SMCI").allowed
    assert dnt.check(atr_pct=8.0, symbol="SMCI", dynamic_symbols=["SMCI"]).allowed


def test_volatility_dnt_momentum_breakout_uses_dynamic_atr_cap_from_config() -> None:
    cfg = {
        "dynamic_universe": {"max_atr_pct": 12.0},
        "dynamic_momentum_entry": {"dynamic_atr_cap": 15.0},
        "trade_filters": {
            "volatility_do_not_trade": {
                "enabled": True,
                "max_atr_pct": 5.0,
            }
        },
    }
    dnt = VolatilityDoNotTrade(cfg)
    assert dnt.check(
        atr_pct=14.0,
        symbol="RUNNER",
        dynamic_symbols=["RUNNER"],
        entry_route="momentum_breakout",
    ).allowed
    assert not dnt.check(
        atr_pct=16.0,
        symbol="RUNNER",
        dynamic_symbols=["RUNNER"],
        entry_route="momentum_breakout",
    ).allowed
    assert not dnt.check(
        atr_pct=14.0,
        symbol="RUNNER",
        dynamic_symbols=["RUNNER"],
        entry_route="trend_long",
    ).allowed


def test_volatility_dnt_inherits_mq_volume_threshold() -> None:
    cfg = {
        "market_quality": {"ignore_spread_when_last_bar_volume_below": 1_000.0},
        "trade_filters": {"volatility_do_not_trade": {"enabled": True, "max_spread_pct": 0.5}},
    }
    dnt = VolatilityDoNotTrade(cfg)
    assert dnt.check(spread_pct=2.0, atr_pct=1.0, last_bar_volume=500.0).allowed
    assert not dnt.check(spread_pct=2.0, atr_pct=1.0, last_bar_volume=2000.0).allowed


def test_execution_build_order_ignore_spread_gate() -> None:
    mgr = ExecutionManager({"execution": {"max_spread_pct": 0.7}})
    assert mgr.build_order("QQQ", "buy", 1, 100.0, 5.0) is None
    assert mgr.build_order("QQQ", "buy", 1, 100.0, 5.0, ignore_spread_gate=True) is not None
