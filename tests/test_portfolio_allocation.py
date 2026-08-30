"""Tests for portfolio sleeve caps (stock vs options as % of equity)."""

from __future__ import annotations

import pytest

from src.options_premium_risk import evaluate_options_premium_before_order
from src.portfolio_allocation import (
    MAX_OPTIONS_CAPITAL_FRAC,
    MAX_STOCK_CAPITAL_FRAC,
    MIN_CASH_RESERVE_FRAC,
    add_on_passes_signal_and_scale,
    cash_pct_of_equity,
    dynamic_symbol_allocation_cap_pct,
    effective_buying_power_for_entries,
    effective_min_cash_reserve_frac,
    is_high_cash_deploy,
    effective_options_total_cap_frac,
    effective_stock_capital_frac,
    max_allocation_per_symbol_pct,
    symbol_allocation_cap_is_dynamic,
    min_cash_reserve_frac,
    min_cash_target_frac,
    parse_rebalance_sell_triggers,
    portfolio_rebalance_each_cycle,
    portfolio_rebalance_tolerance_pct,
    rebalance_signal_deterioration_min_gap,
    stock_buy_within_capital_sleeve,
    sum_long_option_market_value_usd,
    sum_long_stock_market_value_usd,
    parse_add_on_gate_cfg,
    parse_pyramid_into_winners_cfg,
    symbol_long_position_market_value_usd,
    symbol_long_unrealized_pl_pct,
    symbol_position_has_headroom_below_cap,
)


def test_cash_pct_of_equity() -> None:
    assert cash_pct_of_equity(cash=35_000.0, equity=100_000.0) == pytest.approx(35.0)
    assert cash_pct_of_equity(cash=None, equity=100_000.0) is None
    assert cash_pct_of_equity(cash=1000.0, equity=0.0) is None
    assert cash_pct_of_equity(cash=-500.0, equity=100_000.0) == pytest.approx(0.0)


def test_is_high_cash_deploy_threshold() -> None:
    cfg = {"portfolio": {"high_cash_deploy_pct": 30}}
    assert is_high_cash_deploy(cfg, cash=29_999.0, equity=100_000.0) is False
    assert is_high_cash_deploy(cfg, cash=30_000.0, equity=100_000.0) is True
    assert is_high_cash_deploy({"portfolio": {"high_cash_deploy_pct": 0}}, cash=99_999.0, equity=100_000.0) is False
    assert is_high_cash_deploy({}, cash=50_000.0, equity=100_000.0) is False


def test_min_cash_reserve_frac_default() -> None:
    assert min_cash_reserve_frac({}) == pytest.approx(MIN_CASH_RESERVE_FRAC)


def test_effective_buying_power_caps_by_equity_after_reserve() -> None:
    cfg = {"portfolio": {"min_cash_reserve_pct": 0.10}}
    # equity 100k, 10% reserve → deployable 90k; broker BP 500k → use 90k
    assert effective_buying_power_for_entries(
        buying_power=500_000.0, equity=100_000.0, config=cfg
    ) == pytest.approx(90_000.0)
    # broker BP tighter
    assert effective_buying_power_for_entries(
        buying_power=50_000.0, equity=100_000.0, config=cfg
    ) == pytest.approx(50_000.0)


def test_min_cash_reserve_frac_percent_points_unchanged() -> None:
    assert min_cash_reserve_frac({"portfolio": {"min_cash_reserve_pct": 20}}) == pytest.approx(0.20)


def test_min_cash_target_frac_unset_is_zero() -> None:
    assert min_cash_target_frac({}) == pytest.approx(0.0)
    assert min_cash_target_frac({"portfolio": {}}) == pytest.approx(0.0)


def test_min_cash_target_frac_fraction_and_percent_points() -> None:
    assert min_cash_target_frac({"portfolio": {"min_cash_target_pct": 0.15}}) == pytest.approx(0.15)
    assert min_cash_target_frac({"portfolio": {"min_cash_target_pct": 15}}) == pytest.approx(0.15)


def test_portfolio_rebalance_each_cycle_flag() -> None:
    assert portfolio_rebalance_each_cycle({}) is False
    assert portfolio_rebalance_each_cycle({"portfolio": {"rebalance_each_cycle": True}}) is True


def test_parse_rebalance_sell_triggers_default_legacy() -> None:
    t = parse_rebalance_sell_triggers({})
    assert t.legacy is True
    assert t.allow_min_cash_target_trim is True
    assert t.require_rfc_deterioration is False


def test_parse_rebalance_sell_triggers_user_string() -> None:
    t = parse_rebalance_sell_triggers(
        {
            "rebalance": {
                "trigger": "signal_deterioration OR stop_loss OR take_profit",
            }
        }
    )
    assert t.legacy is False
    assert t.allow_min_cash_target_trim is False
    assert t.allow_rfc_partial_trim is True
    assert t.require_rfc_deterioration is True
    assert t.allow_rfc_full_stronger_incoming is False


def test_parse_rebalance_sell_triggers_low_cash_adds() -> None:
    t = parse_rebalance_sell_triggers(
        {"rebalance": {"trigger": "signal_deterioration OR min_cash_target"}}
    )
    assert t.allow_min_cash_target_trim is True


def test_rebalance_signal_deterioration_min_gap() -> None:
    assert rebalance_signal_deterioration_min_gap({}) == pytest.approx(0.05)
    assert rebalance_signal_deterioration_min_gap({"rebalance": {"signal_deterioration_min_gap": 0.1}}) == pytest.approx(0.1)


def test_portfolio_rebalance_tolerance_pct() -> None:
    assert portfolio_rebalance_tolerance_pct({}) == pytest.approx(0.0)
    assert portfolio_rebalance_tolerance_pct({"portfolio": {"rebalance_tolerance_pct": 3}}) == pytest.approx(3.0)
    assert portfolio_rebalance_tolerance_pct({"portfolio": {"rebalance_tolerance_pct": -2}}) == pytest.approx(0.0)


def test_effective_buying_power_zero_equity_falls_back_to_broker_bp() -> None:
    assert effective_buying_power_for_entries(
        buying_power=12_345.0, equity=0.0, config={}
    ) == pytest.approx(12_345.0)


def test_effective_min_cash_reserve_full_invest() -> None:
    cfg: dict = {
        "portfolio": {"min_cash_reserve_pct": 10},
        "cash_management": {"reserve_by_regime": {"neutral": 8}},
    }
    assert effective_min_cash_reserve_frac(cfg, regime_score=3) == pytest.approx(0.08)
    assert effective_min_cash_reserve_frac(
        cfg, regime_score=3, full_invest=True
    ) == pytest.approx(0.0)
    assert effective_buying_power_for_entries(
        buying_power=100_000.0,
        equity=100_000.0,
        config=cfg,
        regime_score=3,
        full_invest=True,
    ) == pytest.approx(100_000.0)


def test_effective_min_cash_reserve_uses_regime_table() -> None:
    cfg: dict = {
        "portfolio": {"min_cash_reserve_pct": 10},
        "cash_management": {
            "reserve_by_regime": {
                "bullish": 5,
                "neutral": 8,
                "bearish": 20,
            }
        },
    }
    assert effective_min_cash_reserve_frac(cfg, regime_score=3) == pytest.approx(0.08)
    assert effective_min_cash_reserve_frac(cfg, regime_score=4) == pytest.approx(0.05)
    assert effective_min_cash_reserve_frac(cfg, regime_score=0) == pytest.approx(0.20)
    assert effective_min_cash_reserve_frac(
        cfg, regime_condition="defensive", regime_score=None
    ) == pytest.approx(0.20)


def test_effective_min_cash_reserve_falls_back_without_regime() -> None:
    cfg: dict = {
        "portfolio": {"min_cash_reserve_pct": 10},
        "cash_management": {"reserve_by_regime": {"neutral": 8}},
    }
    assert effective_min_cash_reserve_frac(cfg) == pytest.approx(0.10)


def test_effective_buying_power_uses_cash_management_with_regime() -> None:
    cfg: dict = {
        "portfolio": {"min_cash_reserve_pct": 10},
        "cash_management": {
            "reserve_by_regime": {
                "bullish": 5,
                "neutral": 8,
                "bearish": 20,
            }
        },
    }
    assert effective_buying_power_for_entries(
        buying_power=500_000.0,
        equity=100_000.0,
        config=cfg,
        regime_score=3,
    ) == pytest.approx(92_000.0)


def test_effective_stock_capital_frac_default() -> None:
    assert effective_stock_capital_frac({}) == pytest.approx(MAX_STOCK_CAPITAL_FRAC)


def test_effective_stock_capital_frac_from_yaml() -> None:
    cfg = {"portfolio": {"max_stock_capital_pct": 55}}
    assert effective_stock_capital_frac(cfg) == pytest.approx(0.55)


def test_effective_stock_capital_frac_dynamic_risk_budget() -> None:
    """DRB: stock sleeve = 1 − options sleeve (``max_stock_capital_pct`` ignored)."""
    cfg = {
        "portfolio": {
            "allocator": {"mode": "dynamic_risk_budget", "buckets": {"hedge": 10}},
            "max_options_capital_pct": 40,
        },
        "options": {"max_total_options_exposure_pct": 100},
    }
    assert effective_stock_capital_frac(cfg) == pytest.approx(0.60)


def test_effective_min_cash_reserve_dynamic_risk_budget() -> None:
    cfg = {
        "portfolio": {
            "allocator": {
                "mode": "dynamic_risk_budget",
                "buckets": {"hedge": 1},
                "cash_buffer_pct": 12,
            }
        }
    }
    assert effective_min_cash_reserve_frac(cfg) == pytest.approx(0.12)


def test_effective_options_total_cap_frac_min_of_yaml_and_sleeve() -> None:
    # YAML higher than sleeve → sleeve wins
    hi_yaml = {
        "portfolio": {"max_options_capital_pct": 40},
        "options": {"max_total_options_exposure_pct": 80},
    }
    assert effective_options_total_cap_frac(hi_yaml) == pytest.approx(0.40)
    # YAML lower than sleeve → YAML wins
    lo_yaml = {
        "portfolio": {"max_options_capital_pct": 40},
        "options": {"max_total_options_exposure_pct": 10},
    }
    assert effective_options_total_cap_frac(lo_yaml) == pytest.approx(0.10)


def test_effective_options_total_cap_frac_default_sleeve_when_no_portfolio() -> None:
    cfg = {"options": {"max_total_options_exposure_pct": 50}}
    assert effective_options_total_cap_frac(cfg) == pytest.approx(
        min(0.50, MAX_OPTIONS_CAPITAL_FRAC)
    )


def test_effective_options_total_cap_total_exposure_limit_alias() -> None:
    assert effective_options_total_cap_frac({"options": {"total_exposure_limit": 0.20}}) == pytest.approx(0.20)
    assert effective_options_total_cap_frac({"options": {"total_exposure_limit": 20}}) == pytest.approx(0.20)
    assert effective_options_total_cap_frac({"options": {"max_options_notional_pct": 0.05}}) == pytest.approx(0.05)
    # Prefer total_exposure_limit when both set
    both = {
        "portfolio": {"max_options_capital_pct": 40},
        "options": {"total_exposure_limit": 0.20, "max_total_options_exposure_pct": 80},
    }
    assert effective_options_total_cap_frac(both) == pytest.approx(0.20)


def test_max_allocation_per_symbol_pct_parsing() -> None:
    assert max_allocation_per_symbol_pct({}) == 0.0
    assert max_allocation_per_symbol_pct({"portfolio": {}}) == 0.0
    assert max_allocation_per_symbol_pct({"portfolio": {"max_allocation_per_symbol": "10%"}}) == pytest.approx(10.0)
    assert max_allocation_per_symbol_pct({"portfolio": {"max_allocation_per_symbol": 12.5}}) == pytest.approx(12.5)
    assert max_allocation_per_symbol_pct({"portfolio": {"max_allocation_per_symbol": "bad"}}) == 0.0


def test_symbol_allocation_cap_parsing() -> None:
    assert max_allocation_per_symbol_pct({"portfolio": {"symbol_allocation_cap": "22%"}}) == pytest.approx(22.0)
    assert max_allocation_per_symbol_pct({"portfolio": {"symbol_allocation_cap": 30}}) == pytest.approx(30.0)


def test_symbol_allocation_cap_is_dynamic() -> None:
    assert symbol_allocation_cap_is_dynamic("dynamic") is True
    assert symbol_allocation_cap_is_dynamic(" Dynamic ") is True
    assert symbol_allocation_cap_is_dynamic({"mode": "dynamic"}) is True
    assert symbol_allocation_cap_is_dynamic(25) is False


def test_dynamic_symbol_allocation_cap_pct() -> None:
    dyn = {"max_pct": 30, "min_trade_size_usd": 500, "floor_pct": 12}
    assert dynamic_symbol_allocation_cap_pct(account_equity=200_000.0, dyn=dyn) == pytest.approx(12.0)
    assert dynamic_symbol_allocation_cap_pct(account_equity=2000.0, dyn=dyn) == pytest.approx(25.0)
    assert dynamic_symbol_allocation_cap_pct(account_equity=1400.0, dyn=dyn) == pytest.approx(30.0)


def test_max_allocation_per_symbol_pct_dynamic() -> None:
    cfg = {
        "portfolio": {
            "symbol_allocation_cap": "dynamic",
            "symbol_allocation_cap_dynamic": {
                "max_pct": 30,
                "min_trade_size_usd": 500,
                "floor_pct": 12,
            },
        }
    }
    assert max_allocation_per_symbol_pct(cfg, account_equity=200_000.0) == pytest.approx(12.0)
    assert max_allocation_per_symbol_pct(cfg) == pytest.approx(30.0)


def test_max_single_position_pct_when_cap_keys_absent() -> None:
    cfg = {"portfolio": {"max_single_position_pct": 8}}
    assert max_allocation_per_symbol_pct(cfg) == pytest.approx(8.0)


def test_symbol_allocation_cap_takes_precedence_over_max_single_position_pct() -> None:
    cfg = {"portfolio": {"symbol_allocation_cap": "25%", "max_single_position_pct": 8}}
    assert max_allocation_per_symbol_pct(cfg) == pytest.approx(25.0)


def test_symbol_allocation_cap_takes_precedence_over_legacy_key() -> None:
    cfg = {
        "portfolio": {
            "symbol_allocation_cap": 25,
            "max_allocation_per_symbol": "10%",
        }
    }
    assert max_allocation_per_symbol_pct(cfg) == pytest.approx(25.0)


def test_symbol_long_position_market_value_usd() -> None:
    positions = [
        {"symbol": "AAPL", "qty": 10, "market_value": 1500.0},
        {"symbol": "QQQ260417P00567000", "qty": 1, "market_value": 500.0},
    ]
    assert symbol_long_position_market_value_usd(positions, "AAPL") == pytest.approx(1500.0)
    assert symbol_long_position_market_value_usd(positions, "qqq") == 0.0
    assert symbol_long_position_market_value_usd(None, "AAPL") == 0.0


def test_sum_long_stock_excludes_occ() -> None:
    positions = [
        {"symbol": "AAPL", "qty": 10, "market_value": 2000.0},
        {"symbol": "QQQ260417P00567000", "qty": 1, "market_value": 500.0},
    ]
    assert sum_long_stock_market_value_usd(positions) == pytest.approx(2000.0)


def test_sum_long_option_only_occ() -> None:
    positions = [
        {"symbol": "AAPL", "qty": 10, "market_value": 2000.0},
        {"symbol": "QQQ260417P00567000", "qty": 1, "market_value": 500.0},
    ]
    assert sum_long_option_market_value_usd(positions) == pytest.approx(500.0)


def test_stock_buy_within_capital_sleeve_allows_under_cap() -> None:
    cfg = {"portfolio": {"max_stock_capital_pct": 60}}
    ok, msg = stock_buy_within_capital_sleeve(
        equity=100_000.0,
        positions=[{"symbol": "AAPL", "qty": 1, "market_value": 50_000.0}],
        additional_stock_notional=5_000.0,
        config=cfg,
    )
    assert ok is True
    assert msg is None


def test_stock_buy_within_capital_sleeve_blocks_over_cap() -> None:
    cfg = {"portfolio": {"max_stock_capital_pct": 60}}
    ok, msg = stock_buy_within_capital_sleeve(
        equity=100_000.0,
        positions=[{"symbol": "AAPL", "qty": 1, "market_value": 58_000.0}],
        additional_stock_notional=5_000.0,
        config=cfg,
    )
    assert ok is False
    assert msg is not None
    assert "stock capital cap" in msg


def test_evaluate_options_premium_respects_min_yaml_and_sleeve() -> None:
    equity = 100_000.0
    # Total cap = min(80%, 25%) * equity = 25k premium budget
    cfg = {
        "portfolio": {"max_options_capital_pct": 25},
        "options": {
            "max_total_options_exposure_pct": 80,
            "risk_per_trade_pct": 25,
            "max_option_position_pct": 100,
            "v1_max_contracts_per_trade": 300,
        },
    }
    # Mid $1 → $100/contract → floor(25000/100)=250 contracts (25% risk × 100k vs 25k sleeve cap)
    r = evaluate_options_premium_before_order(
        cfg, equity=equity, positions=[], option_mid_price=1.0
    )
    assert r.ok is True
    assert r.contracts == 250


def test_symbol_position_has_headroom_below_pct_cap() -> None:
    pos = [{"symbol": "QQQ", "qty": 10, "market_value": 24_000.0}]
    assert symbol_position_has_headroom_below_cap(
        "QQQ",
        positions=pos,
        account_equity=100_000.0,
        max_alloc_sym_pct=25.0,
        max_pos_mval_usd=0.0,
    )
    pos_at = [{"symbol": "QQQ", "qty": 10, "market_value": 25_000.0}]
    assert not symbol_position_has_headroom_below_cap(
        "QQQ",
        positions=pos_at,
        account_equity=100_000.0,
        max_alloc_sym_pct=25.0,
        max_pos_mval_usd=0.0,
    )


def test_symbol_position_has_headroom_respects_dollar_cap() -> None:
    pos = [{"symbol": "QQQ", "qty": 1, "market_value": 9_000.0}]
    assert symbol_position_has_headroom_below_cap(
        "QQQ",
        positions=pos,
        account_equity=100_000.0,
        max_alloc_sym_pct=30.0,
        max_pos_mval_usd=8_000.0,
    ) is False


def test_symbol_long_unrealized_pl_pct_from_basis() -> None:
    pos = [
        {
            "symbol": "SPY",
            "qty": 10,
            "market_value": 10_500.0,
            "cost_basis": 10_000.0,
            "unrealized_pl": 500.0,
        }
    ]
    assert symbol_long_unrealized_pl_pct("SPY", positions=pos) == pytest.approx(5.0)


def test_symbol_long_unrealized_pl_pct_from_avg() -> None:
    pos = [
        {
            "symbol": "IWM",
            "qty": 100,
            "market_value": 22_000.0,
            "avg_entry_price": 200.0,
        }
    ]
    assert symbol_long_unrealized_pl_pct("IWM", positions=pos) == pytest.approx(10.0)


def test_parse_pyramid_into_winners_cfg_defaults() -> None:
    assert parse_pyramid_into_winners_cfg(None)["enabled"] is False
    g = parse_pyramid_into_winners_cfg(
        {
            "pyramid_into_winners": {
                "enabled": True,
                "min_unrealized_profit_pct": 7.5,
                "cap_relax_multiplier": 1.25,
            }
        }
    )
    assert g["enabled"] is True
    assert g["min_unrealized_profit_pct"] == pytest.approx(7.5)
    assert g["cap_relax_multiplier"] == pytest.approx(1.25)


def test_parse_add_on_gate_cfg_aliases() -> None:
    cfg = {
        "portfolio": {
            "add_on": {
                "enabled": True,
                "min_signal_strength": 0.71,
                "max_scaled_size_usd": 42_000,
                "incremental_add_pct": 1.5,
            }
        }
    }
    g = parse_add_on_gate_cfg(cfg["portfolio"])
    assert g["enabled"] is True
    assert g["min_signal_strength"] == pytest.approx(0.71)
    assert g["max_scaled_position_usd"] == pytest.approx(42_000)
    assert g["incremental_add_pct"] == pytest.approx(0.015)


def test_add_on_passes_when_disabled() -> None:
    assert add_on_passes_signal_and_scale(
        gate_cfg={"enabled": False, "min_signal_strength": 0.99},
        entry_signal_strength=0.1,
        position_market_value_usd=9e9,
    ) == (True, 1.0, None)


def test_add_on_scales_when_strength_below_threshold() -> None:
    g = {"enabled": True, "min_signal_strength": 0.75, "max_scaled_position_usd": 0}
    ok, scale, reason = add_on_passes_signal_and_scale(
        gate_cfg=g,
        entry_signal_strength=0.375,
        position_market_value_usd=1_000.0,
    )
    assert ok is True
    assert scale == pytest.approx(0.5)
    assert "scaled" in str(reason)
    ok2, scale2, _ = add_on_passes_signal_and_scale(
        gate_cfg=g,
        entry_signal_strength=0.751,
        position_market_value_usd=1_000.0,
    )
    assert ok2 is True
    assert scale2 == pytest.approx(1.0)


def test_add_on_requires_position_below_max_scaled_usd() -> None:
    g = {"enabled": True, "min_signal_strength": None, "max_scaled_position_usd": 50_000}
    ok, _, _ = add_on_passes_signal_and_scale(
        gate_cfg=g,
        entry_signal_strength=1.0,
        position_market_value_usd=50_000.0,
    )
    assert ok is False
    ok2, _, _ = add_on_passes_signal_and_scale(
        gate_cfg=g,
        entry_signal_strength=1.0,
        position_market_value_usd=49_999.0,
    )
    assert ok2 is True
