"""Tests for options YAML resolution (canonical keys + legacy aliases)."""

from __future__ import annotations

import pytest

from types import SimpleNamespace

from src.options_config import (
    allow_new_entries,
    conviction_band_from_entry_strength,
    dynamic_options_entry_eligible,
    fallback_to_stock,
    max_bid_ask_spread_pct_cap,
    max_open_option_positions_cap,
    max_option_delta,
    max_premium_frac_of_equity,
    max_premium_per_trade_usd,
    min_option_delta,
    never_bypass_stock_risk_caps,
    options_live_pilot_enabled,
    options_ordering_allowed,
    options_conviction_entry_allowed,
    options_conviction_required_min_rank,
    options_entry_environment_blocks,
    paper_dynamic_options_spread_cap,
    portfolio_full_strong_signal_small_call_cap_usd,
    target_dte_bounds,
    trend_long_options_top_signals_only_passes,
)


def test_trend_long_options_top_signals_only_passes() -> None:
    assert trend_long_options_top_signals_only_passes({}, {})
    assert trend_long_options_top_signals_only_passes({"options": {}}, {})
    cfg_on = {"options": {"top_signals_only": True}}
    assert not trend_long_options_top_signals_only_passes(cfg_on, {})
    assert not trend_long_options_top_signals_only_passes(cfg_on, {"in_top_signals": False})
    assert trend_long_options_top_signals_only_passes(cfg_on, {"in_top_signals": True})


def test_trend_long_options_require_top_signal_alias() -> None:
    cfg = {"options": {"require_top_signal": True}}
    assert not trend_long_options_top_signals_only_passes(cfg, {})
    assert trend_long_options_top_signals_only_passes(cfg, {"in_top_signals": True})


def test_allow_new_entries_alias() -> None:
    assert allow_new_entries({"options": {"new_entries_enabled": False}}) is False
    assert allow_new_entries({"options": {"allow_new_entries": False}}) is False
    assert allow_new_entries({"options": {"new_entries_enabled": True, "allow_new_entries": False}}) is True


def test_live_options_pilot_flag_required_for_live_ordering() -> None:
    disabled = {"options": {"enabled": True, "mode": "live", "live_pilot_enabled": False}}
    enabled = {"options": {"enabled": True, "mode": "live", "live_pilot_enabled": True}}
    nested_enabled = {"options": {"enabled": True, "mode": "live_long_premium", "live_pilot": {"enabled": True}}}

    assert options_live_pilot_enabled(disabled) is False
    assert options_ordering_allowed(disabled, broker_is_paper=False) == (
        False,
        "live options not explicitly enabled",
    )
    assert options_live_pilot_enabled(enabled) is True
    assert options_ordering_allowed(enabled, broker_is_paper=False) == (True, None)
    assert options_live_pilot_enabled(nested_enabled) is True
    assert options_ordering_allowed(nested_enabled, broker_is_paper=False) == (True, None)


def test_fallback_alias() -> None:
    assert fallback_to_stock({"options": {"allow_fallback_to_shares": False}}) is False
    assert fallback_to_stock({"options": {"fallback_to_stock": False}}) is False


def test_max_premium_frac_fraction_and_percent() -> None:
    assert max_premium_frac_of_equity({"options": {"max_premium_pct_of_equity": 0.02}}) == pytest.approx(0.02)
    assert max_premium_frac_of_equity({"options": {"max_premium_pct_of_equity": 2}}) == pytest.approx(0.02)
    assert max_premium_frac_of_equity({"options": {"risk_per_trade_pct": 5}}) == pytest.approx(0.05)


def test_max_premium_frac_per_trade_alias() -> None:
    assert max_premium_frac_of_equity({"options": {"per_trade": 0.04}}) == pytest.approx(0.04)
    assert max_premium_frac_of_equity({"options": {"per_trade": 4}}) == pytest.approx(0.04)
    assert max_premium_frac_of_equity(
        {"options": {"per_trade": 0.04, "max_premium_pct_of_equity": 0.5}}
    ) == pytest.approx(0.04)


def test_max_premium_per_trade_usd() -> None:
    assert max_premium_per_trade_usd({"options": {}}) is None
    assert max_premium_per_trade_usd({"options": {"max_premium_per_trade": 300}}) == pytest.approx(300.0)


def test_target_dte_bounds_top_level() -> None:
    assert target_dte_bounds({"options": {"target_dte_min": 7, "target_dte_max": 21}}) == (7, 21)


def test_target_dte_min_dte_aliases() -> None:
    assert target_dte_bounds({"options": {"min_dte": 14, "max_dte": 35}}) == (14, 35)


def test_target_dte_fallback_contract_selection() -> None:
    cfg = {
        "options": {
            "contract_selection": {"expiry_min_days": 10, "expiry_max_days": 40},
        }
    }
    assert target_dte_bounds(cfg) == (10, 40)


def test_spread_cap_fraction_vs_percent() -> None:
    assert max_bid_ask_spread_pct_cap({"options": {"max_bid_ask_spread_pct": 0.015}}) == pytest.approx(1.5)
    assert max_bid_ask_spread_pct_cap({"options": {"max_bid_ask_spread_pct": 1.0}}) == pytest.approx(1.0)
    assert max_bid_ask_spread_pct_cap({"options": {"max_bid_ask_spread_pct": 5.0}}) == pytest.approx(5.0)


def test_max_open_option_positions_cap_prefers_max_positions() -> None:
    assert max_open_option_positions_cap({"options": {"max_positions": 2}}) == 2
    assert max_open_option_positions_cap({"options": {"max_option_positions": 7}}) == 7
    assert max_open_option_positions_cap({"options": {"max_open_option_positions": 5, "max_positions": 2}}) == 2
    assert max_open_option_positions_cap({"options": {"max_open_option_positions": 3}}) == 3


def test_min_option_delta() -> None:
    assert min_option_delta({"options": {}}) is None
    assert min_option_delta({"options": {"min_delta": 0.4}}) == pytest.approx(0.4)
    assert min_option_delta({"options": {"min_delta": 0}}) is None
    assert min_option_delta({"options": {"target_delta_min": 0.35}}) == pytest.approx(0.35)


def test_max_option_delta_alias() -> None:
    assert max_option_delta({"options": {}}) is None
    assert max_option_delta({"options": {"target_delta_max": 0.55}}) == pytest.approx(0.55)


def test_options_entry_environment_blocks_enable_only_if_gross_below_alias() -> None:
    cfg = {"options": {"enable_only_if_gross_below": 0.75}}
    assert options_entry_environment_blocks(cfg, gross_exposure_pct=74.0, reduce_only=False) == (
        False,
        None,
    )
    blocked, reason = options_entry_environment_blocks(cfg, gross_exposure_pct=76.0, reduce_only=False)
    assert blocked is True
    assert "gross" in (reason or "")


def test_options_entry_environment_blocks_gross_disable_wins_over_enable_only() -> None:
    cfg = {
        "options": {
            "disable_if_gross_exposure_above": 0.90,
            "enable_only_if_gross_below": 0.75,
        }
    }
    assert options_entry_environment_blocks(cfg, gross_exposure_pct=80.0, reduce_only=False) == (
        False,
        None,
    )


def test_options_entry_environment_blocks_gross() -> None:
    cfg = {"options": {"disable_if_gross_exposure_above": 0.80}}
    assert options_entry_environment_blocks(cfg, gross_exposure_pct=79.0, reduce_only=False) == (
        False,
        None,
    )
    blocked, reason = options_entry_environment_blocks(
        cfg, gross_exposure_pct=81.0, reduce_only=False
    )
    assert blocked is True
    assert reason is not None


def test_options_entry_environment_blocks_reduce_only() -> None:
    cfg = {"options": {"disable_if_reduce_only": True}}
    assert options_entry_environment_blocks(cfg, gross_exposure_pct=None, reduce_only=True)[0] is True


def test_never_bypass_stock_risk_caps_default_true() -> None:
    assert never_bypass_stock_risk_caps({}) is True
    assert never_bypass_stock_risk_caps({"options": {"never_bypass_stock_risk_caps": False}}) is False


def test_conviction_band_from_entry_strength_edges() -> None:
    assert conviction_band_from_entry_strength(None) is None
    assert conviction_band_from_entry_strength(80.0) == "strong"
    assert conviction_band_from_entry_strength(0.66) == "medium"
    assert conviction_band_from_entry_strength(0.1) == "weak"


def test_options_conviction_required_min_rank() -> None:
    assert options_conviction_required_min_rank({"options": {}}) is None
    assert options_conviction_required_min_rank({"options": {"conviction_required": "high"}}) == 2
    assert options_conviction_required_min_rank({"options": {"conviction_required": "medium"}}) == 1


def test_options_conviction_entry_allowed_uses_score() -> None:
    cfg = {"options": {"conviction_required": "high"}}
    ok_hi, _ = options_conviction_entry_allowed(
        cfg, SimpleNamespace(conviction_band=None, conviction_score=0.9)
    )
    assert ok_hi
    ok_lo, reason = options_conviction_entry_allowed(
        cfg, SimpleNamespace(conviction_band=None, conviction_score=0.5)
    )
    assert not ok_lo
    assert reason and "not met" in reason


def test_options_conviction_entry_allowed_missing_score_skips_gate() -> None:
    cfg = {"options": {"conviction_required": "high"}}
    ok, _ = options_conviction_entry_allowed(cfg, SimpleNamespace(conviction_band=None, conviction_score=None))
    assert ok


def test_dynamic_options_entry_eligibility_thresholds() -> None:
    cfg = {"options": {"dynamic_entry": {"min_scanner_score": 50, "min_news_score": 8, "min_catalyst_score": 0.70}}}
    assert dynamic_options_entry_eligible(cfg, scanner_score=49, news_score=7.9, catalyst_score=0.69)[0] is False
    assert dynamic_options_entry_eligible(cfg, scanner_score=50)[0] is True
    assert dynamic_options_entry_eligible(cfg, news_score=8)[0] is True
    assert dynamic_options_entry_eligible(cfg, catalyst_score=0.70)[0] is True


def test_paper_dynamic_options_spread_cap_is_paper_only() -> None:
    cfg = {
        "options": {
            "mode": "paper_only",
            "max_bid_ask_spread_pct": 8.0,
            "dynamic_entry": {
                "paper_spread_relaxation": {
                    "enabled": True,
                    "max_bid_ask_spread_pct": 12.0,
                }
            },
        }
    }
    assert paper_dynamic_options_spread_cap(cfg, broker_is_paper=True) == pytest.approx(12.0)
    assert paper_dynamic_options_spread_cap(cfg, broker_is_paper=False) is None


def test_portfolio_full_small_call_cap_when_at_max_names_and_strong() -> None:
    cfg = {
        "options": {
            "portfolio_full_strong_signal_options": {
                "enabled": True,
                "min_signal_strength": 0.8,
                "max_premium_usd": 400.0,
            }
        }
    }
    dec = SimpleNamespace(entry_signal=SimpleNamespace(strength=0.9))
    row = {"strength_eff": 0.85}
    ok, cap = portfolio_full_strong_signal_small_call_cap_usd(
        cfg,
        max_port_positions=5,
        n_eligible_long_stocks=5,
        symbol_upper="NVDA",
        current_position_keys={"SPY": {}},
        row_tl=row,
        decision=dec,
        strength_jitter_max=0.0,
        account_equity=100_000.0,
    )
    assert ok is True
    assert cap == pytest.approx(400.0)


def test_bypass_when_full_allow_when_full_and_max_option_allocation_pct() -> None:
    cfg = {
        "options": {
            "bypass_when_full": {
                "allow_when_full": True,
                "max_option_allocation_per_trade": 5,
                "min_signal_strength": 0.5,
            }
        }
    }
    dec = SimpleNamespace(entry_signal=SimpleNamespace(strength=0.9))
    ok, cap = portfolio_full_strong_signal_small_call_cap_usd(
        cfg,
        max_port_positions=5,
        n_eligible_long_stocks=5,
        symbol_upper="NVDA",
        current_position_keys={"SPY": {}},
        row_tl={},
        decision=dec,
        strength_jitter_max=0.0,
        account_equity=100_000.0,
    )
    assert ok is True
    assert cap == pytest.approx(5000.0)


def test_bypass_when_full_overrides_legacy_enabled_off() -> None:
    cfg = {
        "options": {
            "portfolio_full_strong_signal_options": {"enabled": True, "max_premium_usd": 9999},
            "bypass_when_full": {"allow_when_full": False},
        }
    }
    dec = SimpleNamespace(entry_signal=SimpleNamespace(strength=0.99))
    ok, cap = portfolio_full_strong_signal_small_call_cap_usd(
        cfg,
        max_port_positions=5,
        n_eligible_long_stocks=5,
        symbol_upper="NVDA",
        current_position_keys={"SPY": {}},
        row_tl={"strength_eff": 0.99},
        decision=dec,
        strength_jitter_max=0.0,
        account_equity=50_000.0,
    )
    assert ok is False
    assert cap is None


def test_portfolio_full_small_call_cap_skips_when_not_full() -> None:
    cfg = {
        "options": {
            "portfolio_full_strong_signal_options": {"enabled": True, "max_premium_usd": 300}
        }
    }
    dec = SimpleNamespace(entry_signal=SimpleNamespace(strength=0.99))
    ok, cap = portfolio_full_strong_signal_small_call_cap_usd(
        cfg,
        max_port_positions=10,
        n_eligible_long_stocks=3,
        symbol_upper="NVDA",
        current_position_keys={},
        row_tl={},
        decision=dec,
        strength_jitter_max=0.0,
        account_equity=50_000.0,
    )
    assert ok is False
    assert cap is None
