"""Tests for ENTRY_EVAL logging in :meth:`TradingEngine.run_entry_gates`."""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd
import pytest

from src.strategy import EntrySignal
from src.trading_engine import TradingEngine, _entry_eval_route


def _engine_with_exposure_gates() -> TradingEngine:
    return TradingEngine(
        config={
            "strategy": {
                "trend_following": {
                    "ma_fast": 10,
                    "ma_slow": 50,
                    "entry_mode": "momentum",
                    "volatility_filter_atr_period": 14,
                    "max_atr_pct_for_entry": 99.0,
                },
                "exits": {"stop_loss_pct": 2.0, "time_bars_exit": 20},
            },
            "universe": {"symbols": ["SPY"]},
            "portfolio": {
                "exposure_gates": {
                    "enabled": True,
                    "max_total_exposure_frac": 0.9,
                    "max_tech_sector_exposure_frac": 0.4,
                    "tech_over_cap_size_multiplier": 0.5,
                }
            },
            "position_sizing": {
                "risk_per_trade_pct": 0.5,
                "max_open_risk_pct": 5.0,
                "max_exposure_per_symbol_pct": 100.0,
                "volatility_sizing": {"enabled": False},
                "portfolio_heat": {"enabled": False},
                "high_vol_reduction": {"enabled": False},
                "confidence_sizing": {"enabled": False},
            },
            "entries": {},
            "trade_filters": {"volatility_do_not_trade": {"enabled": False}},
            "market_regime": {},
            "holidays": [],
        }
    )


def test_entry_eval_route_trend_when_no_override() -> None:
    assert _entry_eval_route(None) == "trend"


def test_entry_eval_route_news_override() -> None:
    sig = EntrySignal(
        symbol="SPY",
        side="long",
        strength=1.0,
        stop_pct=1.5,
        take_profit_pct=3.0,
        time_bars_exit=20,
        metadata={"source": "news_sentiment"},
    )
    assert _entry_eval_route(sig) == "news_override"


def test_entry_eval_route_alternate_breakout() -> None:
    sig = EntrySignal(
        symbol="SPY",
        side="long",
        strength=1.0,
        stop_pct=1.5,
        take_profit_pct=3.0,
        time_bars_exit=20,
        metadata={"alternate_entry": True, "source": "breakout"},
    )
    assert _entry_eval_route(sig) == "breakout"


def test_entry_eval_route_generic_override() -> None:
    sig = EntrySignal(
        symbol="SPY",
        side="long",
        strength=1.0,
        stop_pct=1.5,
        take_profit_pct=3.0,
        time_bars_exit=20,
        metadata={},
    )
    assert _entry_eval_route(sig) == "entry_override"


def test_news_catalyst_entry_uses_small_notional_order() -> None:
    eng = TradingEngine(
        config={
            "strategy": {
                "trend_following": {
                    "entry_mode": "momentum",
                    "max_atr_pct_for_entry": 99.0,
                },
                "exits": {"stop_loss_pct": 2.0, "time_bars_exit": 20},
            },
            "execution": {
                "allow_fractional": True,
                "prefer_limit_orders": False,
                "max_spread_pct": 2.0,
                "min_trade_dollars": 0.0,
            },
            "position_sizing": {
                "risk_per_trade_pct": 1.0,
                "max_open_risk_pct": 100.0,
                "max_exposure_per_symbol_pct": 100.0,
                "volatility_sizing": {"enabled": False},
                "portfolio_heat": {"enabled": False},
                "high_vol_reduction": {"enabled": False},
                "confidence_sizing": {"enabled": False},
            },
            "universe": {"symbols": ["NEWS"]},
            "portfolio": {"exposure_gates": {"enabled": False}},
            "trade_filters": {"volatility_do_not_trade": {"enabled": False}},
        }
    )
    sig = EntrySignal(
        symbol="NEWS",
        side="long",
        strength=0.62,
        stop_pct=2.0,
        take_profit_pct=4.0,
        time_bars_exit=20,
        metadata={
            "source": "news_catalyst",
            "max_buy_notional_usd": 750.0,
        },
    )
    decision = eng.run_entry_gates(
        "NEWS",
        datetime(2026, 5, 29, 10, 0, 0),
        account_equity=100_000.0,
        current_positions={},
        sector_exposure_pct={},
        spread_pct=0.2,
        volume_atr_ratio=2.0,
        atr_pct=1.0,
        ohlcv_df=_sample_ohlcv_uptrend(),
        entry_override=sig,
    )

    assert decision.allowed is True
    assert decision.position_sizing is not None
    assert decision.position_sizing.notional <= 750.0
    assert decision.order_request.notional == pytest.approx(decision.position_sizing.notional)


def _sample_ohlcv_uptrend(n: int = 55) -> pd.DataFrame:
    base = 400.0
    closes = [base + i * 0.5 for i in range(n)]
    return pd.DataFrame(
        {
            "close": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "volume": [10_000_000.0] * n,
        }
    )


def _sample_ohlcv_below_ema(n: int = 55) -> pd.DataFrame:
    closes = [450.0 - i * 0.75 for i in range(n)]
    closes[-1] = closes[-2] - 5.0
    return pd.DataFrame(
        {
            "close": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "volume": [10_000_000.0] * n,
        }
    )


def test_news_trend_override_skips_only_internal_ema_trend_gate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = {
        "strategy": {
            "trend_following": {
                "entry_mode": "momentum",
                "max_atr_pct_for_entry": 99.0,
            },
            "exits": {"stop_loss_pct": 2.0, "time_bars_exit": 20},
        },
        "filters": {
            "require_adx": False,
            "require_price_above_20ema": True,
            "require_20ema_slope_positive": False,
        },
        "execution": {
            "allow_fractional": True,
            "prefer_limit_orders": False,
            "max_spread_pct": 2.0,
            "min_trade_dollars": 0.0,
            "symbol_cooldown_minutes": 0.0,
        },
        "position_sizing": {
            "risk_per_trade_pct": 1.0,
            "max_open_risk_pct": 100.0,
            "max_exposure_per_symbol_pct": 100.0,
            "volatility_sizing": {"enabled": False},
            "portfolio_heat": {"enabled": False},
            "high_vol_reduction": {"enabled": False},
            "confidence_sizing": {"enabled": False},
        },
        "universe": {"symbols": ["AVGO"]},
        "portfolio": {"exposure_gates": {"enabled": False}},
        "trade_filters": {"volatility_do_not_trade": {"enabled": False}},
        "market_regime": {},
        "holidays": [],
    }
    eng = TradingEngine(config=cfg)
    sig = EntrySignal(
        symbol="AVGO",
        side="long",
        strength=0.8,
        stop_pct=2.0,
        take_profit_pct=4.0,
        time_bars_exit=20,
        metadata={"source": "high_conviction_catalyst", "event_score": 8.0},
    )
    common = {
        "symbol": "AVGO",
        "dt": datetime(2026, 6, 4, 10, 0, 0),
        "account_equity": 100_000.0,
        "current_positions": {},
        "sector_exposure_pct": {},
        "spread_pct": 0.2,
        "volume_atr_ratio": 2.0,
        "atr_pct": 1.0,
        "ohlcv_df": _sample_ohlcv_below_ema(),
        "entry_override": sig,
    }

    blocked = eng.run_entry_gates(**common, entry_route="trend_long")
    assert blocked.allowed is False
    assert "trend filter: close" in (blocked.reason or "")

    with caplog.at_level(logging.INFO, logger="src.trading_engine"):
        overridden = eng.run_entry_gates(**common, entry_route="news_trend_override")

    assert "trend filter: close" not in (overridden.reason or "")
    assert "NEWS_TREND_OVERRIDE_GATE symbol=AVGO skipped=trend_regime_filters" in caplog.text


@pytest.fixture
def engine() -> TradingEngine:
    return TradingEngine()


def test_run_entry_gates_logs_entry_eval_when_log_strategy_context(engine: TradingEngine, caplog: pytest.LogCaptureFixture) -> None:
    dt = datetime(2024, 6, 5, 14, 30, 0)
    df = _sample_ohlcv_uptrend()
    with caplog.at_level(logging.INFO, logger="src.trading_engine"):
        engine.run_entry_gates(
            "SPY",
            dt,
            account_equity=100_000.0,
            current_positions={},
            sector_exposure_pct={},
            spread_pct=0.05,
            volume_atr_ratio=2.0,
            atr_pct=1.5,
            ohlcv_df=df,
            log_strategy_context=True,
        )
    joined = caplog.text
    assert "SPY ENTRY_EVAL route=trend" in joined
    assert "momentum=" in joined and "volatility=" in joined and "spread=True" in joined


def test_run_entry_gates_blocks_when_total_exposure_over_adaptive_bearish_cap() -> None:
    eng = TradingEngine(
        config={
            "strategy": {
                "trend_following": {
                    "ma_fast": 10,
                    "ma_slow": 50,
                    "entry_mode": "momentum",
                    "volatility_filter_atr_period": 14,
                    "max_atr_pct_for_entry": 99.0,
                },
                "exits": {"stop_loss_pct": 2.0, "time_bars_exit": 20},
            },
            "universe": {"symbols": ["SPY"]},
            "portfolio": {
                "exposure_gates": {
                    "enabled": True,
                    "max_total_exposure_frac": 0.95,
                    "max_tech_sector_exposure_frac": 0.4,
                    "tech_over_cap_size_multiplier": 0.5,
                }
            },
            "adaptive": {
                "max_exposure_by_regime": {"bearish": 0.5},
            },
            "position_sizing": {
                "risk_per_trade_pct": 0.5,
                "max_open_risk_pct": 5.0,
                "max_exposure_per_symbol_pct": 100.0,
                "volatility_sizing": {"enabled": False},
                "portfolio_heat": {"enabled": False},
                "high_vol_reduction": {"enabled": False},
                "confidence_sizing": {"enabled": False},
            },
            "entries": {},
            "trade_filters": {"volatility_do_not_trade": {"enabled": False}},
            "market_regime": {},
            "holidays": [],
        }
    )
    dt = datetime(2024, 6, 5, 14, 30, 0)
    df = _sample_ohlcv_uptrend()
    d = eng.run_entry_gates(
        "SPY",
        dt,
        account_equity=100_000.0,
        current_positions={},
        sector_exposure_pct={},
        spread_pct=0.05,
        volume_atr_ratio=2.0,
        atr_pct=1.5,
        ohlcv_df=df,
        gross_exposure_pct=55.0,
        regime_score=0,
    )
    assert d.allowed is False
    assert d.reason is not None and "exposure_gate" in d.reason


def test_run_entry_gates_blocks_reduce_only_when_gross_over_100_pct_equity() -> None:
    eng = TradingEngine(
        config={
            "strategy": {
                "trend_following": {
                    "ma_fast": 10,
                    "ma_slow": 50,
                    "entry_mode": "momentum",
                    "volatility_filter_atr_period": 14,
                    "max_atr_pct_for_entry": 99.0,
                },
                "exits": {"stop_loss_pct": 2.0, "time_bars_exit": 20},
            },
            "universe": {"symbols": ["SPY"]},
            "portfolio": {
                "exposure_gates": {
                    "overexposed_reduce_only": True,
                    "overexposed_reduce_only_gross_frac": 1.0,
                }
            },
            "position_sizing": {
                "risk_per_trade_pct": 0.5,
                "max_open_risk_pct": 5.0,
                "max_exposure_per_symbol_pct": 100.0,
                "volatility_sizing": {"enabled": False},
                "portfolio_heat": {"enabled": False},
                "high_vol_reduction": {"enabled": False},
                "confidence_sizing": {"enabled": False},
            },
            "entries": {},
            "trade_filters": {"volatility_do_not_trade": {"enabled": False}},
            "market_regime": {},
            "holidays": [],
        }
    )
    dt = datetime(2024, 6, 5, 14, 30, 0)
    df = _sample_ohlcv_uptrend()
    d2 = eng.run_entry_gates(
        "SPY",
        dt,
        account_equity=100_000.0,
        current_positions={},
        sector_exposure_pct={},
        spread_pct=0.05,
        volume_atr_ratio=2.0,
        atr_pct=1.5,
        ohlcv_df=df,
        gross_exposure_pct=100.01,
    )
    assert d2.allowed is False
    assert d2.reason is not None
    assert "reduce_only" in d2.reason
    assert "over_exposed" in d2.reason


def test_run_entry_gates_blocks_when_total_exposure_over_cap() -> None:
    eng = _engine_with_exposure_gates()
    dt = datetime(2024, 6, 5, 14, 30, 0)
    df = _sample_ohlcv_uptrend()
    d = eng.run_entry_gates(
        "SPY",
        dt,
        account_equity=100_000.0,
        current_positions={},
        sector_exposure_pct={},
        spread_pct=0.05,
        volume_atr_ratio=2.0,
        atr_pct=1.5,
        ohlcv_df=df,
        gross_exposure_pct=91.0,
    )
    assert d.allowed is False
    assert d.reason is not None and "exposure_gate" in d.reason


def test_run_entry_gates_blocks_when_projected_gross_would_exceed_cap() -> None:
    """Current book can be under cap while a sized order would push gross over max (see skip_buy_if_projected_gross_over_max)."""
    eng = _engine_with_exposure_gates()
    dt = datetime(2024, 6, 5, 14, 30, 0)
    df = _sample_ohlcv_uptrend()
    d = eng.run_entry_gates(
        "SPY",
        dt,
        account_equity=100_000.0,
        current_positions={},
        sector_exposure_pct={},
        spread_pct=0.05,
        volume_atr_ratio=2.0,
        atr_pct=1.5,
        ohlcv_df=df,
        gross_exposure_pct=88.0,
    )
    assert d.allowed is False
    assert d.reason is not None
    r = (d.reason or "").lower()
    assert "projected" in r or "hard_exposure" in r
    assert d.position_sizing is not None


def test_run_entry_gates_soft_cap_trims_buy_when_projected_would_exceed_cap() -> None:
    """With ``portfolio.exposure_gates.soft_cap``, clamp notional to headroom instead of hard-rejecting."""
    eng = TradingEngine(
        config={
            "strategy": {
                "trend_following": {
                    "ma_fast": 10,
                    "ma_slow": 50,
                    "entry_mode": "momentum",
                    "volatility_filter_atr_period": 14,
                    "max_atr_pct_for_entry": 99.0,
                },
                "exits": {"stop_loss_pct": 2.0, "time_bars_exit": 20},
            },
            "universe": {"symbols": ["SPY"]},
            "portfolio": {
                "exposure_gates": {
                    "enabled": True,
                    "max_total_exposure_frac": 0.9,
                    "max_tech_sector_exposure_frac": 0.4,
                    "tech_over_cap_size_multiplier": 0.5,
                    "soft_cap": {"enabled": True},
                }
            },
            "position_sizing": {
                "risk_per_trade_pct": 0.5,
                "max_open_risk_pct": 5.0,
                "max_exposure_per_symbol_pct": 100.0,
                "volatility_sizing": {"enabled": False},
                "portfolio_heat": {"enabled": False},
                "high_vol_reduction": {"enabled": False},
                "confidence_sizing": {"enabled": False},
            },
            "entries": {},
            "trade_filters": {"volatility_do_not_trade": {"enabled": False}},
            "market_regime": {},
            "holidays": [],
        }
    )
    dt = datetime(2024, 6, 5, 14, 30, 0)
    df = _sample_ohlcv_uptrend()
    d = eng.run_entry_gates(
        "SPY",
        dt,
        account_equity=100_000.0,
        current_positions={},
        sector_exposure_pct={},
        spread_pct=0.05,
        volume_atr_ratio=2.0,
        atr_pct=1.5,
        ohlcv_df=df,
        gross_exposure_pct=88.0,
    )
    assert d.allowed is True
    assert d.position_sizing is not None
    ps = d.position_sizing
    assert ps is not None
    assert ps.shares >= 1
    # At 88%% book vs 90%% cap, at most ~$2000 new gross on $100k equity.
    assert float(ps.notional) <= 2100.0
    tr = (ps.trim_reason or "").lower()
    assert ps.trimmed is True
    assert "portfolio_soft_cap" in tr


def test_controlled_live_caps_adaptive_bullish_exposure_before_soft_cap() -> None:
    """Controlled-live keeps the advertised 85% cap even when adaptive bullish caps exceed 100%."""
    eng = TradingEngine(
        config={
            "trading_control": {
                "mode": "live",
                "runtime_profile": "controlled_live_equity",
                "controlled_live_equity": {
                    "enabled": True,
                    "max_managed_positions": 10,
                    "max_single_order_notional_pct": 0.12,
                    "max_single_order_notional": 5000,
                    "max_symbol_exposure_pct": 15,
                    "strategy_allocation_cap_pct": 60,
                    "portfolio_exposure_cap_pct": 85,
                    "stock_capital_pct": 60,
                    "min_cash_reserve_pct": 12,
                    "daily_loss_limit_pct": 3,
                },
            },
            "strategy": {
                "trend_following": {
                    "ma_fast": 10,
                    "ma_slow": 50,
                    "entry_mode": "momentum",
                    "volatility_filter_atr_period": 14,
                    "max_atr_pct_for_entry": 99.0,
                },
                "exits": {"stop_loss_pct": 2.0, "time_bars_exit": 20},
            },
            "universe": {"symbols": ["SPY"]},
            "portfolio": {
                "max_gross_exposure": 1.0,
                "exposure_gates": {
                    "enabled": True,
                    "max_total_exposure_frac": 0.95,
                    "max_tech_sector_exposure_frac": 0.4,
                    "tech_over_cap_size_multiplier": 0.5,
                    "soft_cap": {"enabled": True},
                },
            },
            "adaptive": {
                "max_exposure_by_regime": {"bullish": 0.98, "neutral": 0.95, "bearish": 0.85},
                "bullish_score_4_plus_max_exposure_frac": 1.20,
                "max_exposure_frac_ceiling": 1.35,
            },
            "position_sizing": {
                "risk_per_trade_pct": 0.5,
                "max_open_risk_pct": 5.0,
                "max_exposure_per_symbol_pct": 100.0,
                "volatility_sizing": {"enabled": False},
                "portfolio_heat": {"enabled": False},
                "high_vol_reduction": {"enabled": False},
                "confidence_sizing": {"enabled": False},
            },
            "entries": {},
            "trade_filters": {"volatility_do_not_trade": {"enabled": False}},
            "market_regime": {},
            "holidays": [],
        }
    )
    dt = datetime(2024, 6, 5, 14, 30, 0)
    df = _sample_ohlcv_uptrend()

    allowed = eng.run_entry_gates(
        "SPY",
        dt,
        account_equity=100_000.0,
        current_positions={},
        sector_exposure_pct={},
        spread_pct=0.05,
        volume_atr_ratio=2.0,
        atr_pct=1.5,
        ohlcv_df=df,
        gross_exposure_pct=1.5,
        regime_score=5,
        regime_condition="bullish",
    )
    blocked = eng.run_entry_gates(
        "SPY",
        dt,
        account_equity=100_000.0,
        current_positions={},
        sector_exposure_pct={},
        spread_pct=0.05,
        volume_atr_ratio=2.0,
        atr_pct=1.5,
        ohlcv_df=df,
        gross_exposure_pct=86.0,
        regime_score=5,
        regime_condition="bullish",
    )

    assert allowed.allowed is True
    assert allowed.position_sizing is not None
    assert allowed.position_sizing.reject_reason is None
    assert blocked.allowed is False
    assert blocked.reason is not None and "85%" in blocked.reason


def test_run_entry_gates_skips_entry_eval_without_log_strategy_context(
    engine: TradingEngine, caplog: pytest.LogCaptureFixture
) -> None:
    dt = datetime(2024, 6, 5, 14, 30, 0)
    df = _sample_ohlcv_uptrend()
    with caplog.at_level(logging.INFO, logger="src.trading_engine"):
        engine.run_entry_gates(
            "SPY",
            dt,
            account_equity=100_000.0,
            current_positions={},
            sector_exposure_pct={},
            spread_pct=0.05,
            volume_atr_ratio=2.0,
            atr_pct=1.5,
            ohlcv_df=df,
            log_strategy_context=False,
        )
    assert "ENTRY_EVAL" not in caplog.text
