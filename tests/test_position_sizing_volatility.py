"""ATR volatility sizing, conviction scale, portfolio heat, and open-risk gate."""

from __future__ import annotations

import pytest

from src.position_sizing import PositionSizer


def _vs_cfg(**overrides: object) -> dict:
    base = {
        "position_sizing": {
            "risk_per_trade_pct": 0.6,
            "max_open_risk_pct": 5.0,
            "max_exposure_per_symbol_pct": 50.0,
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
        }
    }
    ps = dict(base["position_sizing"])
    for k, v in overrides.items():
        if k == "volatility_sizing" and isinstance(v, dict):
            merged = dict(ps.get("volatility_sizing") or {})
            merged.update(v)
            ps["volatility_sizing"] = merged
        elif k == "portfolio_heat" and isinstance(v, dict):
            merged = dict(ps.get("portfolio_heat") or {})
            merged.update(v)
            ps["portfolio_heat"] = merged
        elif k == "dynamic_sizing" and isinstance(v, dict):
            merged = dict(ps.get("dynamic_sizing") or {})
            merged.update(v)
            ps["dynamic_sizing"] = merged
        else:
            ps[k] = v
    return {"position_sizing": ps}


def test_volatile_symbol_smaller_than_stable_at_same_budget() -> None:
    """Higher ATR% → smaller share count when not cap-limited."""
    sz = PositionSizer(_vs_cfg())
    equity = 100_000.0
    price = 100.0
    spy = sz.size_position(
        equity,
        price,
        1.5,
        "SPY",
        {},
        {},
        atr_pct=1.0,
        conviction_score=1.0,
    )
    nvda = sz.size_position(
        equity,
        price,
        1.5,
        "NVDA",
        {},
        {},
        atr_pct=4.0,
        conviction_score=1.0,
    )
    assert spy.shares > 0 and nvda.shares > 0
    assert nvda.shares < spy.shares
    assert nvda.shares == pytest.approx(int((equity * 0.006) / (price * 0.04)), rel=0, abs=1)


def test_min_target_position_pct_bumps_shares() -> None:
    """Risk-only size is below 12%% equity target → bump to target (capped by symbol %%)."""
    cfg = _vs_cfg(
        volatility_sizing={"enabled": False},
        risk_per_trade_pct=0.01,
        max_exposure_per_symbol_pct=50.0,
        min_position_pct=0.08,
        target_position_pct=0.12,
    )
    sz = PositionSizer(cfg)
    equity = 100_000.0
    price = 100.0
    r = sz.size_position(
        equity,
        price,
        2.0,
        "TEST",
        {},
        {},
        atr_pct=None,
        conviction_score=1.0,
    )
    assert r.shares == 120
    assert r.trim_reason is not None and "min_target_position_pct" in r.trim_reason


def test_min_position_pct_only_eight_percent_floor() -> None:
    cfg = _vs_cfg(
        volatility_sizing={"enabled": False},
        risk_per_trade_pct=0.01,
        max_exposure_per_symbol_pct=50.0,
        min_position_pct=8,
        target_position_pct=0,
    )
    sz = PositionSizer(cfg)
    r = sz.size_position(
        100_000.0,
        100.0,
        2.0,
        "TEST",
        {},
        {},
        atr_pct=None,
        conviction_score=1.0,
    )
    assert r.shares == 80


def test_symbol_cap_remaining_limits_shares() -> None:
    sz = PositionSizer(_vs_cfg(max_exposure_per_symbol_pct=10.0))
    equity = 100_000.0
    price = 100.0
    cur = {"NVDA": {"notional": 9000.0, "stop_pct": 1.5}}
    r = sz.size_position(
        equity,
        price,
        1.5,
        "NVDA",
        cur,
        {},
        atr_pct=1.0,
    )
    assert r.shares == 10
    assert r.exposure_pct == pytest.approx(1000.0 / equity * 100.0)
    assert r.trimmed is True
    assert r.trim_reason is not None and "symbol_exposure_cap" in r.trim_reason
    assert r.reject_reason is None


def test_leader_override_raises_symbol_cap_for_named_symbols() -> None:
    cfg = _vs_cfg(max_symbol_exposure_pct=12.5)
    del cfg["position_sizing"]["max_exposure_per_symbol_pct"]
    cfg["leader_overrides"] = {
        "symbols": ["SMH", "NVDA", "GOOGL", "AMZN", "SPY"],
        "max_symbol_exposure_pct": 15,
    }
    sz = PositionSizer(cfg)
    equity = 100_000.0
    price = 100.0

    leader = sz.size_position(
        equity,
        price,
        1.5,
        "SMH",
        {"SMH": {"notional": 14_000.0}},
        {},
        atr_pct=1.0,
    )
    ordinary = sz.size_position(
        equity,
        price,
        1.5,
        "MSFT",
        {"MSFT": {"notional": 12_000.0}},
        {},
        atr_pct=1.0,
    )

    assert leader.shares == 10
    assert ordinary.shares == 5


def test_position_sizing_result_exposure_untrimmed_when_not_limited() -> None:
    sz = PositionSizer(_vs_cfg(max_exposure_per_symbol_pct=100.0))
    equity = 100_000.0
    r = sz.size_position(equity, 100.0, 1.5, "SPY", {}, {}, atr_pct=1.0)
    assert r.shares > 0
    assert r.exposure_pct == pytest.approx(r.notional / equity * 100.0)
    assert r.trimmed is False
    assert r.trim_reason is None
    assert r.reject_reason is None


def test_cap_notional_by_portfolio_budget_trims_on_gross_headroom() -> None:
    sz = PositionSizer(
        _vs_cfg(
            max_gross_exposure_pct=90.0,
            max_net_exposure_pct=100.0,
            max_theme_exposure_pct=100.0,
            max_etf_exposure_pct=100.0,
            max_inverse_etf_exposure_pct=100.0,
        )
    )
    n, trimmed, reason = sz._cap_notional_by_portfolio_budget(
        100_000.0,
        50_000.0,
        80.0,
        0.0,
        0.0,
        False,
        False,
    )
    assert n == pytest.approx(10_000.0)
    assert trimmed is True
    assert reason == "portfolio_budget_trim"


def test_cap_notional_by_portfolio_budget_no_trim_when_within_budget() -> None:
    sz = PositionSizer(
        _vs_cfg(
            max_gross_exposure_pct=100.0,
            max_net_exposure_pct=100.0,
            max_theme_exposure_pct=100.0,
        )
    )
    n, trimmed, reason = sz._cap_notional_by_portfolio_budget(
        100_000.0,
        5_000.0,
        10.0,
        10.0,
        10.0,
        False,
        False,
    )
    assert n == pytest.approx(5_000.0)
    assert trimmed is False
    assert reason is None


def test_cap_notional_by_portfolio_budget_etf_and_inverse_caps() -> None:
    sz = PositionSizer(
        _vs_cfg(
            max_gross_exposure_pct=100.0,
            max_net_exposure_pct=100.0,
            max_theme_exposure_pct=100.0,
            max_etf_exposure_pct=45.0,
            max_inverse_etf_exposure_pct=15.0,
        )
    )
    n_etf, trim_etf, _ = sz._cap_notional_by_portfolio_budget(
        100_000.0, 100_000.0, 0.0, 0.0, 0.0, True, False,
    )
    assert n_etf == pytest.approx(45_000.0)
    assert trim_etf is True

    n_inv, trim_inv, _ = sz._cap_notional_by_portfolio_budget(
        100_000.0, 100_000.0, 0.0, 0.0, 0.0, False, True,
    )
    assert n_inv == pytest.approx(15_000.0)
    assert trim_inv is True


def test_size_position_rejects_portfolio_caps_when_no_room() -> None:
    sz = PositionSizer(
        _vs_cfg(
            max_exposure_per_symbol_pct=100.0,
            max_gross_exposure_pct=100.0,
            max_net_exposure_pct=100.0,
            max_theme_exposure_pct=100.0,
        )
    )
    r = sz.size_position(
        100_000.0,
        100.0,
        1.5,
        "SPY",
        {},
        {},
        atr_pct=1.0,
        current_gross_exposure_pct=100.0,
    )
    assert r.shares == 0
    assert r.reject_reason == "portfolio caps leave no room"


def test_size_position_kwonly_dynamic_and_portfolio_budget() -> None:
    sz = PositionSizer(
        _vs_cfg(
            max_exposure_per_symbol_pct=100.0,
            dynamic_sizing={"enabled": True},
            max_gross_exposure_pct=50.0,
            max_net_exposure_pct=100.0,
            max_theme_exposure_pct=100.0,
            max_etf_exposure_pct=100.0,
            max_inverse_etf_exposure_pct=100.0,
        )
    )
    r = sz.size_position(
        100_000.0,
        100.0,
        1.5,
        "SPY",
        {},
        {},
        atr_pct=1.0,
        current_drawdown_pct=-8.0,
        conviction="strong",
        strategy_winrate=0.35,
        current_gross_exposure_pct=48.0,
    )
    assert r.shares > 0
    assert r.reject_reason is None
    assert "dynamic_sizing" in (r.trim_reason or "")
    assert "portfolio_budget_trim" in (r.trim_reason or "")


def test_apply_dynamic_multipliers_returns_shares_unchanged_when_disabled() -> None:
    sz = PositionSizer(_vs_cfg())
    assert sz._apply_dynamic_multipliers(100, current_drawdown_pct=-10.0, conviction="weak") == 100


def test_apply_dynamic_multipliers_drawdown_conviction_and_health() -> None:
    sz = PositionSizer(
        _vs_cfg(
            dynamic_sizing={
                "enabled": True,
                "drawdown_scaling": {"dd_3_pct": 0.85, "dd_5_pct": 0.70, "dd_8_pct": 0.50},
                "conviction_scaling": {"strong": 1.15, "weak": 0.70},
                "strategy_health_scaling": {
                    "cold_threshold_winrate": 0.40,
                    "hot_threshold_winrate": 0.60,
                    "cold_multiplier": 0.75,
                    "hot_multiplier": 1.10,
                },
            }
        )
    )
    # -8% drawdown → ×0.50; strong → ×1.15; win 0.35 cold → ×0.75  →  100 * 0.5 * 1.15 * 0.75 = 43.125 → 43
    out = sz._apply_dynamic_multipliers(
        100,
        current_drawdown_pct=-8.0,
        conviction="strong",
        strategy_winrate=0.35,
    )
    assert out == 43

    # weak only: 100 * 0.7 = 70
    assert sz._apply_dynamic_multipliers(100, conviction="weak") == 70

    # hot win rate only: 100 * 1.10 = 110
    assert sz._apply_dynamic_multipliers(100, strategy_winrate=0.65) == 110


def test_high_vol_reduction_sets_trim_reason_on_stop_path() -> None:
    sz = PositionSizer(
        _vs_cfg(
            volatility_sizing={"enabled": False},
            high_vol_reduction={"enabled": True, "atr_pct_threshold": 2.0, "size_multiplier": 0.5},
        )
    )
    r = sz.size_position(
        100_000.0,
        100.0,
        1.5,
        "QQQ",
        {},
        {},
        atr_pct=3.0,
        regime_score=0,
    )
    assert r.shares > 0
    assert r.trimmed is True
    assert r.trim_reason is not None and "high_vol_reduction" in r.trim_reason


def test_fallback_stop_path_when_volatility_sizing_disabled() -> None:
    sz = PositionSizer(_vs_cfg(volatility_sizing={"enabled": False}))
    r = sz.size_position(
        100_000.0,
        100.0,
        3.0,
        "QQQ",
        {},
        {},
        atr_pct=5.0,
    )
    risk_budget = 100_000.0 * 0.006
    risk_per_share = 3.0
    assert r.shares == int(risk_budget / risk_per_share)


def test_conviction_scales_risk_budget() -> None:
    sz = PositionSizer(
        _vs_cfg(
            volatility_sizing={
                "enabled": True,
                "conviction_min_scale": 0.5,
                "conviction_max_scale": 1.0,
            }
        )
    )
    low = sz.size_position(100_000.0, 100.0, 1.5, "X", {}, {}, atr_pct=2.0, conviction_score=0.0)
    high = sz.size_position(100_000.0, 100.0, 1.5, "X", {}, {}, atr_pct=2.0, conviction_score=1.0)
    assert high.shares > low.shares


def test_proportional_signal_strength_scales_with_conviction_score() -> None:
    """Risk budget multiplier ≈ strength (allocation ∝ signal_strength)."""
    sz = PositionSizer(
        _vs_cfg(
            volatility_sizing={
                "enabled": True,
                "atr_risk_multiple": 1.0,
                "signal_strength_mapping": "proportional",
                "signal_strength_floor": 0.05,
                "conviction_min_scale": 0.5,
                "conviction_max_scale": 1.25,
            },
            confidence_sizing={
                "enabled": True,
                "suppress_when_signal_strength_proportional": True,
                "momentum_bars": 10,
                "volume_bars": 20,
            },
        )
    )
    weak = sz.size_position(
        100_000.0,
        100.0,
        1.5,
        "SPY",
        {},
        {},
        atr_pct=1.5,
        conviction_score=0.25,
    )
    strong = sz.size_position(
        100_000.0,
        100.0,
        1.5,
        "SPY",
        {},
        {},
        atr_pct=1.5,
        conviction_score=1.0,
    )
    assert weak.shares > 0 and strong.shares > weak.shares


def test_proportional_mapping_skips_confidence_when_suppressed_without_ohlcv() -> None:
    sz = PositionSizer(
        _vs_cfg(
            volatility_sizing={
                "enabled": True,
                "signal_strength_mapping": "proportional",
                "signal_strength_floor": 0.1,
            },
            confidence_sizing={
                "enabled": True,
                "suppress_when_signal_strength_proportional": True,
                "momentum_bars": 10,
                "volume_bars": 20,
            },
        )
    )
    r = sz.size_position(
        100_000.0,
        100.0,
        1.5,
        "SPY",
        {},
        {},
        atr_pct=1.2,
        conviction_score=0.6,
        ohlcv_df=None,
    )
    assert r.shares > 0 and r.reject_reason is None


def test_portfolio_heat_reduces_shares() -> None:
    sz = PositionSizer(
        _vs_cfg(
            portfolio_heat={
                "enabled": True,
                "max_exposure_frac_for_heat": 0.50,
                "min_risk_scale_at_full_heat": 0.5,
            }
        )
    )
    cold = sz.size_position(
        100_000.0,
        100.0,
        1.5,
        "SPY",
        {},
        {},
        atr_pct=1.0,
    )
    hot_book = {"OTHER": {"notional": 30_000.0, "stop_pct": 1.5}}
    hot = sz.size_position(
        100_000.0,
        100.0,
        1.5,
        "SPY",
        hot_book,
        {},
        atr_pct=1.0,
    )
    assert hot.shares < cold.shares


def test_would_exceed_max_open_risk_two_args() -> None:
    sz = PositionSizer({"position_sizing": {"max_open_risk_pct": 5.0}})
    assert sz.would_exceed_max_open_risk(3.0, 3.0) is True
    assert sz.would_exceed_max_open_risk(3.0, 1.99) is False


def test_option_keys_ignored_for_cap_remaining() -> None:
    sz = PositionSizer(_vs_cfg(max_exposure_per_symbol_pct=10.0))
    occ = "NVDA260117C00180000"
    cur = {occ: {"notional": 5000.0, "stop_pct": 0}, "NVDA": {"notional": 5000.0, "stop_pct": 1.5}}
    r = sz.size_position(100_000.0, 100.0, 1.5, "NVDA", cur, {}, atr_pct=1.0)
    assert r.shares == 50


def test_cap_notional_sector_caps_broad_index_vs_max_theme() -> None:
    sz = PositionSizer(
        _vs_cfg(
            max_gross_exposure_pct=100.0,
            max_net_exposure_pct=100.0,
            max_theme_exposure_pct=100.0,
            sector_caps={"broad_index": 12.0, "others": 90.0},
        )
    )
    n, trimmed, _ = sz._cap_notional_by_portfolio_budget(
        100_000.0, 50_000.0, 0.0, 0.0, 10.0, False, False, theme_key="broad_index"
    )
    assert n == pytest.approx(2000.0)
    assert trimmed is True


def test_cap_notional_sector_caps_others_for_unlisted_theme() -> None:
    sz = PositionSizer(
        _vs_cfg(
            max_gross_exposure_pct=100.0,
            max_net_exposure_pct=100.0,
            max_theme_exposure_pct=100.0,
            sector_caps={"broad_index": 60.0, "others": 25.0},
        )
    )
    n, trimmed, _ = sz._cap_notional_by_portfolio_budget(
        100_000.0, 20_000.0, 0.0, 0.0, 20.0, False, False, theme_key="financials"
    )
    assert n == pytest.approx(5000.0)
    assert trimmed is True


def test_sector_caps_tech_alias_covers_ai_growth() -> None:
    sz = PositionSizer(
        _vs_cfg(
            max_gross_exposure_pct=100.0,
            max_net_exposure_pct=100.0,
            max_theme_exposure_pct=100.0,
            sector_caps={"tech": 30.0},
        )
    )
    n, _, _ = sz._cap_notional_by_portfolio_budget(
        100_000.0, 50_000.0, 0.0, 0.0, 20.0, False, False, theme_key="ai_growth"
    )
    assert n == pytest.approx(10_000.0)
