"""Unit tests for long-option exit evaluation (options.exits)."""

from __future__ import annotations

import pytest

from src.options_exit import (
    compute_option_pnl_pct,
    evaluate_long_option_exit,
    underlying_breaks_option_signal,
)
from src.strategy import ExitReason


def _cfg(**exit_overrides):
    return {
        "options": {
            "enabled": True,
            "allow_new_entries": False,
            "exits": {
                "automation_enabled": True,
                "profit_take_pct": 40,
                "stop_loss_pct": 30,
                "max_hold_days": 5,
                "exit_if_underlying_breaks_signal": False,
                "underlying_ma_period": 50,
                **exit_overrides,
            },
        }
    }


def test_compute_option_pnl_pct() -> None:
    assert compute_option_pnl_pct(unrealized_pl=50.0, cost_basis=500.0) == pytest.approx(10.0)
    assert compute_option_pnl_pct(unrealized_pl=-150.0, cost_basis=500.0) == pytest.approx(-30.0)
    assert compute_option_pnl_pct(unrealized_pl=10.0, cost_basis=0.0) is None


def test_compute_option_pnl_pct_fallback_market_value_alpaca_negative_basis() -> None:
    """When unrealized_pl is absent, derive from MV + negative cost_basis (Alpaca long options)."""
    assert compute_option_pnl_pct(
        unrealized_pl=None,
        cost_basis=-500.0,
        market_value=350.0,
    ) == pytest.approx(-30.0)


def test_compute_option_pnl_pct_fallback_market_value_positive_basis() -> None:
    assert compute_option_pnl_pct(
        unrealized_pl=None,
        cost_basis=500.0,
        market_value=350.0,
    ) == pytest.approx(-30.0)


def test_stop_loss_with_pnl_from_market_value_only() -> None:
    d = evaluate_long_option_exit(
        _cfg(),
        occ_symbol="QQQ240119P00400000",
        days_held=1,
        unrealized_pl=None,
        cost_basis=-500.0,
        underlying_close=None,
        underlying_ma=None,
        market_value=350.0,
    )
    assert d.should_exit is True
    assert d.reason == ExitReason.OPTION_STOP_LOSS


def test_underlying_break_put_above_ma() -> None:
    assert underlying_breaks_option_signal("put", 101.0, 100.0) is True
    assert underlying_breaks_option_signal("put", 99.0, 100.0) is False


def test_underlying_break_call_below_ma() -> None:
    assert underlying_breaks_option_signal("call", 99.0, 100.0) is True
    assert underlying_breaks_option_signal("call", 101.0, 100.0) is False


def test_stop_loss_triggers() -> None:
    d = evaluate_long_option_exit(
        _cfg(),
        occ_symbol="QQQ240119P00400000",
        days_held=1,
        unrealized_pl=-200.0,
        cost_basis=500.0,
        underlying_close=None,
        underlying_ma=None,
    )
    assert d.should_exit is True
    assert d.reason == ExitReason.OPTION_STOP_LOSS


def test_profit_take_triggers() -> None:
    d = evaluate_long_option_exit(
        _cfg(),
        occ_symbol="QQQ240119P00400000",
        days_held=1,
        unrealized_pl=250.0,
        cost_basis=500.0,
        underlying_close=None,
        underlying_ma=None,
    )
    assert d.should_exit is True
    assert d.reason == ExitReason.OPTION_PROFIT_TAKE


def test_max_hold_triggers() -> None:
    d = evaluate_long_option_exit(
        _cfg(),
        occ_symbol="QQQ240119P00400000",
        days_held=5,
        unrealized_pl=0.0,
        cost_basis=500.0,
        underlying_close=None,
        underlying_ma=None,
    )
    assert d.should_exit is True
    assert d.reason == ExitReason.OPTION_MAX_HOLD


def test_underlying_break_put() -> None:
    d = evaluate_long_option_exit(
        _cfg(exit_if_underlying_breaks_signal=True),
        occ_symbol="QQQ240119P00400000",
        days_held=1,
        unrealized_pl=-5.0,
        cost_basis=500.0,
        underlying_close=402.0,
        underlying_ma=400.0,
    )
    assert d.should_exit is True
    assert d.reason == ExitReason.OPTION_UNDERLYING_BREAK


def test_stop_before_profit_when_both_apply() -> None:
    """Large loss should hit stop even if other rules exist."""
    d = evaluate_long_option_exit(
        _cfg(max_hold_days=99),
        occ_symbol="QQQ240119P00400000",
        days_held=20,
        unrealized_pl=-200.0,
        cost_basis=500.0,
        underlying_close=None,
        underlying_ma=None,
    )
    assert d.reason == ExitReason.OPTION_STOP_LOSS


def test_exit_automation_disabled_no_exit() -> None:
    d = evaluate_long_option_exit(
        {
            "options": {
                "enabled": True,
                "exits": {"automation_enabled": False, "stop_loss_pct": 1},
            }
        },
        occ_symbol="QQQ240119P00400000",
        days_held=99,
        unrealized_pl=-999.0,
        cost_basis=100.0,
        underlying_close=None,
        underlying_ma=None,
    )
    assert d.should_exit is False


def test_entries_off_but_exits_still_run() -> None:
    """allow_new_entries false must not affect exit evaluation."""
    d = evaluate_long_option_exit(
        {
            "options": {
                "enabled": True,
                "allow_new_entries": False,
                "exits": {
                    "automation_enabled": True,
                    "stop_loss_pct": 30,
                    "profit_take_pct": 40,
                    "max_hold_days": 5,
                },
            }
        },
        occ_symbol="QQQ240119P00400000",
        days_held=1,
        unrealized_pl=-200.0,
        cost_basis=500.0,
        underlying_close=None,
        underlying_ma=None,
    )
    assert d.should_exit is True
    assert d.reason is not None


def _cfg_tiered(**exit_overrides):
    return _cfg(
        profit_tier_partial_pct=30,
        profit_tier_partial_fraction=0.5,
        profit_tier_final_pct=50,
        profit_take_pct=99,
        **exit_overrides,
    )


def test_tiered_profit_partial_at_30pct() -> None:
    d = evaluate_long_option_exit(
        _cfg_tiered(),
        occ_symbol="QQQ240119P00400000",
        days_held=1,
        unrealized_pl=200.0,
        cost_basis=500.0,
        underlying_close=None,
        underlying_ma=None,
        open_contracts=4,
        tracker_position={},
    )
    assert d.should_exit is True
    assert d.reason == ExitReason.OPTION_PROFIT_TAKE_PARTIAL
    assert d.contracts_to_sell == 2
    assert d.remove_tracker_after is False
    assert d.mark_option_profit_tier1_done is True


def test_tiered_profit_final_at_50pct() -> None:
    d = evaluate_long_option_exit(
        _cfg_tiered(),
        occ_symbol="QQQ240119P00400000",
        days_held=1,
        unrealized_pl=300.0,
        cost_basis=500.0,
        underlying_close=None,
        underlying_ma=None,
        open_contracts=2,
        tracker_position={"option_profit_tier1_done": True},
    )
    assert d.should_exit is True
    assert d.reason == ExitReason.OPTION_PROFIT_TAKE
    assert d.contracts_to_sell is None
    assert d.remove_tracker_after is True


def test_tiered_no_repeat_partial_after_tier1_done() -> None:
    """Between 30% and 50% P/L, do not sell partial again once tier1 fired."""
    d = evaluate_long_option_exit(
        _cfg_tiered(),
        occ_symbol="QQQ240119P00400000",
        days_held=1,
        unrealized_pl=200.0,
        cost_basis=500.0,
        underlying_close=None,
        underlying_ma=None,
        open_contracts=4,
        tracker_position={"option_profit_tier1_done": True},
    )
    assert d.should_exit is False


def test_tiered_single_contract_waits_for_final_tier() -> None:
    """One contract: no 50% partial leg; exit at final tier only."""
    d = evaluate_long_option_exit(
        _cfg_tiered(),
        occ_symbol="QQQ240119P00400000",
        days_held=1,
        unrealized_pl=175.0,
        cost_basis=500.0,
        underlying_close=None,
        underlying_ma=None,
        open_contracts=1,
        tracker_position={},
    )
    assert d.should_exit is False

    d2 = evaluate_long_option_exit(
        _cfg_tiered(),
        occ_symbol="QQQ240119P00400000",
        days_held=1,
        unrealized_pl=275.0,
        cost_basis=500.0,
        underlying_close=None,
        underlying_ma=None,
        open_contracts=1,
        tracker_position={},
    )
    assert d2.should_exit is True
    assert d2.reason == ExitReason.OPTION_PROFIT_TAKE
    assert d2.remove_tracker_after is True


def _cfg_trail(**exit_overrides):
    return _cfg(
        profit_trail_arm_peak_pct=40,
        profit_trail_exit_below_pct=25,
        profit_take_pct=99,
        **exit_overrides,
    )


def test_trail_exit_when_peak_armed_and_pnl_below_floor() -> None:
    d = evaluate_long_option_exit(
        _cfg_trail(),
        occ_symbol="QQQ240119P00400000",
        days_held=1,
        unrealized_pl=120.0,
        cost_basis=500.0,
        underlying_close=None,
        underlying_ma=None,
        tracker_position={"option_pnl_peak_pct": 45.0},
    )
    assert d.should_exit is True
    assert d.reason == ExitReason.OPTION_PNL_TRAIL
    assert d.persist_option_pnl_peak_pct is None


def test_trail_no_exit_when_peak_not_armed() -> None:
    d = evaluate_long_option_exit(
        _cfg_trail(),
        occ_symbol="QQQ240119P00400000",
        days_held=1,
        unrealized_pl=100.0,
        cost_basis=500.0,
        underlying_close=None,
        underlying_ma=None,
        tracker_position={"option_pnl_peak_pct": 35.0},
    )
    assert d.should_exit is False
    assert d.persist_option_pnl_peak_pct == pytest.approx(35.0)


def test_trail_no_exit_when_pnl_equals_floor() -> None:
    """Exit requires current P/L strictly below profit_trail_exit_below_pct."""
    d = evaluate_long_option_exit(
        _cfg_trail(),
        occ_symbol="QQQ240119P00400000",
        days_held=1,
        unrealized_pl=125.0,
        cost_basis=500.0,
        underlying_close=None,
        underlying_ma=None,
        tracker_position={"option_pnl_peak_pct": 45.0},
    )
    assert d.should_exit is False


def test_trail_persist_peak_on_hold() -> None:
    d = evaluate_long_option_exit(
        _cfg_trail(),
        occ_symbol="QQQ240119P00400000",
        days_held=1,
        unrealized_pl=150.0,
        cost_basis=500.0,
        underlying_close=None,
        underlying_ma=None,
        tracker_position={},
    )
    assert d.should_exit is False
    assert d.persist_option_pnl_peak_pct == pytest.approx(30.0)


def test_trail_disabled_when_keys_omitted() -> None:
    d = evaluate_long_option_exit(
        _cfg(),
        occ_symbol="QQQ240119P00400000",
        days_held=1,
        unrealized_pl=150.0,
        cost_basis=500.0,
        underlying_close=None,
        underlying_ma=None,
        tracker_position={"option_pnl_peak_pct": 50.0},
    )
    assert d.should_exit is False
    assert d.persist_option_pnl_peak_pct is None


def test_stop_loss_before_trail() -> None:
    d = evaluate_long_option_exit(
        _cfg_trail(),
        occ_symbol="QQQ240119P00400000",
        days_held=1,
        unrealized_pl=-200.0,
        cost_basis=500.0,
        underlying_close=None,
        underlying_ma=None,
        tracker_position={"option_pnl_peak_pct": 50.0},
    )
    assert d.reason == ExitReason.OPTION_STOP_LOSS
