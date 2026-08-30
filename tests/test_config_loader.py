"""Tests for config_loader — deep_merge and load_config."""

from pathlib import Path

import pytest
import yaml

from src.config_loader import deep_merge, load_config
from src.user_manager import UserManager


class TestDeepMerge:
    """deep_merge should recursively merge overrides into a base dict."""

    def test_flat_override(self):
        base = {"a": 1, "b": 2}
        overrides = {"b": 99}
        result = deep_merge(base, overrides)
        assert result == {"a": 1, "b": 99}

    def test_nested_override(self):
        base = {"position_sizing": {"risk_per_trade_pct": 0.25, "max_position_dollar_cap": 2000}}
        overrides = {"position_sizing": {"max_position_dollar_cap": 1000}}
        result = deep_merge(base, overrides)
        assert result["position_sizing"]["risk_per_trade_pct"] == 0.25
        assert result["position_sizing"]["max_position_dollar_cap"] == 1000

    def test_deeply_nested(self):
        base = {"a": {"b": {"c": 1, "d": 2}, "e": 3}}
        overrides = {"a": {"b": {"c": 99}}}
        result = deep_merge(base, overrides)
        assert result == {"a": {"b": {"c": 99, "d": 2}, "e": 3}}

    def test_new_keys_added(self):
        base = {"a": 1}
        overrides = {"b": 2}
        result = deep_merge(base, overrides)
        assert result == {"a": 1, "b": 2}

    def test_list_replaced_not_merged(self):
        base = {"symbols": ["SPY", "QQQ"]}
        overrides = {"symbols": ["AAPL"]}
        result = deep_merge(base, overrides)
        assert result["symbols"] == ["AAPL"]

    def test_base_not_mutated(self):
        base = {"a": {"b": 1}}
        overrides = {"a": {"b": 99}}
        deep_merge(base, overrides)
        assert base["a"]["b"] == 1

    def test_empty_overrides(self):
        base = {"a": 1, "b": {"c": 2}}
        result = deep_merge(base, {})
        assert result == base

    def test_override_scalar_with_dict(self):
        base = {"a": 1}
        overrides = {"a": {"nested": True}}
        result = deep_merge(base, overrides)
        assert result == {"a": {"nested": True}}

    def test_override_dict_with_scalar(self):
        base = {"a": {"nested": True}}
        overrides = {"a": "flat"}
        result = deep_merge(base, overrides)
        assert result == {"a": "flat"}

    def test_none_override_value(self):
        base = {"a": 1}
        overrides = {"a": None}
        result = deep_merge(base, overrides)
        assert result == {"a": None}


def test_load_config_merges_top_level_exits_into_strategy_exits(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(
        yaml.dump(
            {
                "strategy": {"exits": {"stop_loss_pct": 2.0, "trim_winners_enabled": False}},
                "exits": {"trim_winners_enabled": True, "trim_threshold_pct": 1.5, "trim_fraction": 0.3},
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(p)
    ex = cfg["strategy"]["exits"]
    assert ex["stop_loss_pct"] == 2.0
    assert ex["trim_winners_enabled"] is True
    assert ex["trim_threshold_pct"] == pytest.approx(1.5)
    assert ex["trim_fraction"] == pytest.approx(0.3)


def test_load_config_merges_reentry_into_strategy_exits_and_entries(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(
        yaml.dump(
            {
                "strategy": {
                    "exits": {
                        "stop_loss_pct": 2.0,
                        "require_new_breakout_after_stop": True,
                    }
                },
                "entries": {"min_trade_size": 500, "allow_reentry_on_pullback": False},
                "reentry": {
                    "require_new_high": False,
                    "allow_pullback_reentry": True,
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg["strategy"]["exits"]["stop_loss_pct"] == 2.0
    assert cfg["strategy"]["exits"]["require_new_breakout_after_stop"] is False
    assert cfg["entries"]["min_trade_size"] == 500
    assert cfg["entries"]["allow_reentry_on_pullback"] is True


def test_load_config_merges_cooldown_into_strategy_exits(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(
        yaml.dump(
            {
                "strategy": {
                    "exits": {
                        "stop_loss_pct": 2.0,
                        "cooldown_after_stop_minutes": 12,
                        "strong_trend_reconfirm_bypass_cooldown": False,
                    }
                },
                "cooldown": {
                    "after_stop_loss_minutes": 5,
                    "allow_reentry_if_strong_trend": True,
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(p)
    ex = cfg["strategy"]["exits"]
    assert ex["stop_loss_pct"] == 2.0
    assert ex["cooldown_after_stop_minutes"] == 5
    assert ex["strong_trend_reconfirm_bypass_cooldown"] is True


def test_load_config_merges_top_level_exit_into_strategy_exits(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(
        yaml.dump(
            {
                "strategy": {"exits": {"stop_loss_pct": 2.0, "take_profit_pct": None}},
                "exit": {"take_profit_pct": 3.0, "partial_trim_trigger_pct": 1.5},
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(p)
    ex = cfg["strategy"]["exits"]
    assert ex["stop_loss_pct"] == 2.0
    assert ex["take_profit_pct"] == pytest.approx(3.0)
    assert ex["partial_trim_trigger_pct"] == pytest.approx(1.5)


def test_load_config_maps_root_disable_partial_trim_below_gross_pct(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(
        yaml.dump(
            {
                "strategy": {"exits": {"stop_loss_pct": 2.0}},
                "disable_partial_trim_below_gross_pct": 0.85,
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg["strategy"]["exits"]["stop_loss_pct"] == 2.0
    assert cfg["strategy"]["exits"]["disable_partial_trim_below_gross_pct"] == pytest.approx(0.85)


def test_load_config_maps_allocator_operator_aliases(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(
        yaml.dump(
            {
                "allocator": {
                    "allow_no_trade_cycles": True,
                    "selected_must_execute": False,
                    "minimum_cash_to_deploy_pct": 0.05,
                    "if_no_actions_cycles": 2,
                    "idle_fallback": {
                        "enabled": True,
                        "max_gross_pct": 85,
                        "prefer_dynamic_symbols": True,
                    },
                    "fallback": {
                        "pick_top_n": 2,
                        "size_pct": 0.1,
                        "enforce_diversity": True,
                    },
                    "max_new_positions_neutral": 3,
                    "min_trade_notional": 100,
                    "reentry_cooldown_after_exit_minutes": 60,
                }
                ,
                "correlation": {
                    "max_per_group": 2,
                    "groups": {"indices": ["SPY", "IWM", "DIA"]},
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg["regime"]["score_3"]["max_new_positions"] == 3
    assert cfg["portfolio"]["capital_allocator"]["min_trade_notional"] == pytest.approx(100.0)
    assert cfg["portfolio"]["capital_allocator"]["if_no_actions_cycles"] == 2
    assert cfg["portfolio"]["capital_allocator"]["fallback_pick_top_n"] == 2
    assert cfg["portfolio"]["capital_allocator"]["fallback_size_pct"] == pytest.approx(0.1)
    assert cfg["portfolio"]["capital_allocator"]["idle_fallback"]["enabled"] is True
    assert cfg["portfolio"]["capital_allocator"]["idle_fallback"]["max_gross_pct"] == 85
    assert cfg["portfolio"]["capital_allocator"]["idle_fallback"]["prefer_dynamic_symbols"] is True
    assert cfg["portfolio"]["capital_allocator"]["allow_no_trade_cycles"] is True
    assert cfg["portfolio"]["capital_allocator"]["selected_must_execute"] is False
    assert cfg["portfolio"]["capital_allocator"]["minimum_cash_to_deploy_pct"] == pytest.approx(0.004)
    assert cfg["portfolio"]["capital_allocator"]["fallback_enforce_diversity"] is True
    assert cfg["portfolio"]["capital_allocator"]["correlation_max_per_group"] == 2
    assert cfg["portfolio"]["capital_allocator"]["correlation_groups"]["indices"] == ["SPY", "IWM", "DIA"]
    assert cfg["execution"]["min_recent_exit_reentry_minutes"] == 60


def test_default_config_uses_concentrated_stock_book() -> None:
    cfg = load_config()

    assert cfg["allocation"]["allocate_top_n"] == 3
    assert cfg["allocator"]["max_new_positions_per_cycle"] == 3
    assert cfg["allocator"]["pick_top_n_signals"] == 3
    assert cfg["allocator"]["allow_no_trade_cycles"] is True
    assert cfg["allocator"]["selected_must_execute"] is False
    assert cfg["allocator"]["minimum_cash_to_deploy_pct"] == pytest.approx(0.05)

    portfolio = cfg["portfolio"]
    assert portfolio["max_positions"] == 10
    assert portfolio["rank_based_holding"]["enabled"] is True
    assert portfolio["rank_based_holding"]["top_n"] == 8
    assert portfolio["rank_based_holding"]["max_sells_per_pass"] == 1

    cap_alloc = portfolio["capital_allocator"]
    assert cap_alloc["max_positions"] == 10
    assert cap_alloc["allow_no_trade_cycles"] is True
    assert cap_alloc["selected_must_execute"] is False
    assert cap_alloc["minimum_cash_to_deploy_pct"] == pytest.approx(0.03)
    assert cap_alloc["deploy_top_n_signals"] == 3
    assert cap_alloc["empty_alloc_top_n"] == 3
    assert cap_alloc["min_trade_size"] == pytest.approx(750)
    assert cap_alloc["min_trade_notional"] == pytest.approx(750)
    assert cap_alloc["max_single_order_notional_pct"] == pytest.approx(0.12)
    assert cap_alloc["max_single_order_notional"] == pytest.approx(5000)
    assert cap_alloc["concentration_bias"]["enabled"] is True
    assert cap_alloc["concentration_bias"]["top_n"] == 2
    assert cap_alloc["concentration_bias"]["top_tranche_scale"] == pytest.approx(1.75)
    assert cap_alloc["concentration_bias"]["rest_tranche_scale"] == pytest.approx(0.50)
    assert cfg["risk"]["max_symbol_allocation_pct"]["default"] == pytest.approx(15)
    assert cfg["risk"]["max_symbol_allocation_pct"]["etf"] == pytest.approx(22)
    assert cap_alloc["symbol_caps"]["leaders"]["SPY"] == "22%"
    assert cap_alloc["symbol_caps"]["leaders"]["QQQ"] == "22%"
    assert cap_alloc["symbol_caps"]["core"]["IWM"] == "22%"
    assert cfg["market_quality"]["liquid_spread_relief"]["enabled"] is True
    assert cfg["entries"]["symbol_cooldown_minutes"] == 45
    assert cfg["entries"]["leader_cooldown_overrides"]["GOOGL"] == 15
    assert cfg["risk"]["max_trades_per_symbol_per_day"] == 4
    assert cfg["dynamic_universe"]["min_relative_volume"] == pytest.approx(
        cfg["dynamic_universe"]["min_rel_volume"]
    )
    assert cfg["dynamic_universe"]["min_rel_volume"] == pytest.approx(0.3)
    assert cfg["dynamic_universe"]["min_intraday_range_pct"] == pytest.approx(1.0)
    assert cfg["dynamic_universe"]["min_atr_expansion_ratio"] == pytest.approx(0.25)
    assert "require_above_vwap" not in cfg["dynamic_universe"]
    assert cfg["dynamic_universe"]["require_5m_trend_alignment"] is False
    assert cfg["dynamic_universe"]["min_price"] == pytest.approx(2)
    assert cfg["dynamic_universe"]["max_price"] == pytest.approx(150)
    assert cfg["dynamic_universe"]["min_day_gain_pct"] == pytest.approx(2.0)
    assert cfg["dynamic_universe"]["max_day_gain_pct"] == pytest.approx(80.0)
    assert cfg["dynamic_universe"]["min_avg_volume"] == pytest.approx(5_000)
    assert cfg["dynamic_universe"]["max_spread_pct"] == pytest.approx(2.5)
    assert cfg["dynamic_universe"]["execution_max_spread_pct"] == pytest.approx(8.0)
    assert cfg["dynamic_universe"]["max_symbols"] == 30
    assert cfg["dynamic_universe"]["max_total_exposure_pct"] == pytest.approx(30)
    assert cfg["dynamic_universe"]["max_symbol_exposure_pct"] == pytest.approx(12)
    assert cfg["dynamic_universe"]["catalyst_boost"]["min_relative_volume_with_catalyst"] == pytest.approx(1.0)
    assert cfg["dynamic_momentum_override"]["enabled"] is True
    assert cfg["dynamic_momentum_override"]["min_day_gain_pct"] == pytest.approx(20.0)
    assert cfg["dynamic_momentum_override"]["min_relative_volume"] == pytest.approx(1.8)
    assert cfg["dynamic_momentum_override"]["require_above_vwap"] is False
    assert cfg["dynamic_entry_guard"]["max_vwap_distance_pct"] == pytest.approx(15.0)
    assert cfg["dynamic_entry_guard"]["require_ema_5_above_20"] is False
    assert cfg["dynamic_momentum_entry"]["enabled"] is True
    assert cfg["dynamic_momentum_entry"]["min_day_gain_pct"] == pytest.approx(2.0)
    assert cfg["dynamic_momentum_entry"]["min_relative_volume"] == pytest.approx(0.3)
    assert cfg["dynamic_momentum_entry"]["vwap_score_alignment"]["enabled"] is True
    assert cfg["dynamic_momentum_entry"]["vwap_score_alignment"]["min_score"] == pytest.approx(80)
    assert cfg["dynamic_momentum_entry"]["max_entry_spread_pct"] == pytest.approx(3.0)
    assert cfg["dynamic_momentum_entry"]["dynamic_atr_cap"] == pytest.approx(15.0)
    assert cfg["dynamic_momentum_entry"]["momentum_top_n"] == 3
    assert cfg["dynamic_momentum_entry"]["momentum_score"]["enabled"] is True
    assert cfg["dynamic_momentum_entry"]["momentum_score"]["weights"]["rel_volume"] == pytest.approx(
        0.3
    )
    assert cfg["dynamic_momentum_entry"]["news_dynamic_entry"]["early_min_relative_volume"] == pytest.approx(0.5)
    assert cfg["market"]["open_protection"]["dynamic_scan_delay_minutes"] == pytest.approx(5)
    assert cfg["portfolio"]["target_dynamic_pct"] == pytest.approx(45)

    sizing = cfg["position_sizing"]
    assert sizing["base_position_pct"] == pytest.approx(0.08)
    assert sizing["max_symbol_exposure_pct"] == pytest.approx(12.5)
    assert cfg["leader_overrides"]["symbols"] == ["SMH", "NVDA", "GOOGL", "AMZN", "SPY"]
    assert cfg["leader_overrides"]["max_symbol_exposure_pct"] == pytest.approx(15)
    assert sizing["risk_per_trade_pct"] == pytest.approx(0.70)
    assert sizing["max_position_dollar_cap"] == pytest.approx(6000)
    assert sizing["min_position_dollar"] == pytest.approx(2000)
    assert sizing["winner_allocation"]["top_n"] == 2
    assert sizing["winner_allocation"]["size_multiplier"] == pytest.approx(1.75)
    assert sizing["volatility_sizing"]["conviction_max_scale"] == pytest.approx(1.50)
    assert cfg["strategy"]["exits"]["disable_partial_trim_below_gross_pct"] == pytest.approx(0.85)
    assert cfg["portfolio"]["max_gross_exposure"] == pytest.approx(1.0)
    assert cfg["portfolio"]["target_gross_exposure_pct"] == pytest.approx(1.0)
    assert cfg["portfolio"]["exposure_gates"]["max_total_exposure_frac"] == pytest.approx(0.95)


def test_live_and_paper_stock_dynamic_rvol_loosen_preserves_options_and_hard_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    users_path = tmp_path / "users.yaml"
    users_path.write_text(
        yaml.safe_dump(
            {
                "users": [
                    {
                        "id": "live_bot",
                        "alpaca_key_env": "LIVE_KEY",
                        "alpaca_secret_env": "LIVE_SECRET",
                        "paper": False,
                    },
                    {
                        "id": "paper_bot",
                        "alpaca_key_env": "PAPER_KEY",
                        "alpaca_secret_env": "PAPER_SECRET",
                        "paper": True,
                        "overrides": {"options": {"enabled": True}},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    for key in ("LIVE_KEY", "LIVE_SECRET", "PAPER_KEY", "PAPER_SECRET"):
        monkeypatch.setenv(key, "x")

    manager = UserManager(load_config(), users_path=users_path)
    live_cfg = manager.get_user("live_bot").config
    paper_cfg = manager.get_user("paper_bot").config

    for cfg in (live_cfg, paper_cfg):
        assert cfg["dynamic_universe"]["min_relative_volume"] == pytest.approx(0.3)
        assert cfg["dynamic_universe"]["min_rel_volume"] == pytest.approx(0.3)
        assert cfg["dynamic_momentum_entry"]["min_relative_volume"] == pytest.approx(0.3)
        assert cfg["dynamic_momentum_entry"]["news_dynamic_entry"]["early_min_relative_volume"] == pytest.approx(0.5)

        assert cfg["dynamic_universe"]["min_price"] == pytest.approx(2)
        assert cfg["dynamic_universe"]["max_price"] == pytest.approx(150)
        assert cfg["dynamic_universe"]["max_spread_pct"] == pytest.approx(2.5)
        assert cfg["dynamic_universe"]["execution_max_spread_pct"] == pytest.approx(8.0)
        assert cfg["dynamic_universe"]["min_avg_volume"] == pytest.approx(5_000)
        assert cfg["dynamic_universe"]["max_symbols"] == 30
        assert cfg["dynamic_universe"]["max_entry_vwap_extension_pct"] == pytest.approx(8.0)
        assert cfg["dynamic_universe"]["catalyst_boost"]["min_relative_volume_with_catalyst"] == pytest.approx(1.0)
        assert cfg["portfolio"]["max_positions"] == 10
        assert cfg["portfolio"]["target_dynamic_pct"] == pytest.approx(45)
        assert cfg["options"]["mode"] == "paper_only"

    assert live_cfg["options"]["enabled"] is False
    assert live_cfg["options"]["live_pilot_enabled"] is False
    assert paper_cfg["options"]["enabled"] is True


def test_load_config_risk_max_total_exposure_pct_merges_portfolio(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(
        yaml.dump(
            {
                "portfolio": {
                    "exposure_gates": {"max_total_exposure_frac": 0.5},
                    "max_gross_exposure": 0.5,
                },
                "risk": {"max_total_exposure_pct": 80},
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg["portfolio"]["exposure_gates"]["max_total_exposure_frac"] == pytest.approx(0.8)
    assert cfg["portfolio"]["max_gross_exposure"] == pytest.approx(0.8)


def test_load_config_cooldowns_merges_scan_cooldowns(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(
        yaml.dump(
            {
                "cooldowns": {
                    "default_minutes": 45,
                    "leader_overrides": {"SMH": 10, "NVDA": 10},
                },
                "entries": {},
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg["entries"]["symbol_cooldown_minutes"] == 45
    assert cfg["entries"]["leader_cooldown_overrides"]["SMH"] == 10
    assert cfg["entries"]["leader_cooldown_overrides"]["NVDA"] == 10


def test_load_config_execution_max_trades_per_symbol_per_day(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(
        yaml.dump({"execution": {"max_trades_per_symbol_per_day": 4}}),
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg["risk"]["max_trades_per_symbol_per_day"] == 4
    assert cfg["portfolio_risk"]["max_trades_per_symbol_per_day"] == 4


def test_load_config_execution_add_and_min_trade_aliases(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(
        yaml.dump(
            {
                "entries": {"min_trade_size": 999},
                "execution": {
                    "allow_add_to_position": True,
                    "add_position_cooldown_minutes": 20,
                    "min_trade_notional": 500,
                },
                "position_sizing": {"incremental_add_pct": 0.015},
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg["entries"]["allow_add_to_existing_positions"] is True
    assert cfg["entries"]["symbol_cooldown_minutes"] == 20
    assert cfg["entries"]["min_trade_size"] == pytest.approx(500.0)
    assert cfg["portfolio"]["allow_add"] is True
    assert cfg["portfolio"]["add_on"]["incremental_add_pct"] == pytest.approx(0.015)
