"""Tests for ``strategy.exits.kill_switch`` partial trim mode."""

from __future__ import annotations

import pytest

from src.strategy import ExitReason, TrendFollowingStrategy


def _exits_for_kill_tests(**kill_kw: object) -> dict:
    return {
        "kill_switch": {
            "max_spread_pct": 1.0,
            "max_atr_pct": 100.0,
            **kill_kw,
        },
        "use_trailing_stop": False,
        "smart_trailing": {"enabled": False},
        "time_bars_exit": 99999,
        "partial_take_profit_pct": 999.0,
        "min_hold_minutes": 0,
        "stop_loss_pct": 99.0,
    }


def test_kill_switch_partial_spread_trims_fraction() -> None:
    s = TrendFollowingStrategy(
        {"strategy": {"exits": _exits_for_kill_tests(mode="partial", sell_fraction=0.5)}}
    )
    sig = s.check_exit(
        "SPY",
        100.0,
        100.0,
        1,
        spread_pct=2.0,
        atr_pct=None,
        partial_taken=True,
        trail_high=100.0,
        current_qty=100,
    )
    assert sig is not None
    assert sig.reason == ExitReason.KILL_SWITCH_PARTIAL
    assert sig.metadata.get("qty_to_sell") == 50
    assert sig.metadata.get("spread_pct") == pytest.approx(2.0)


def test_kill_switch_partial_atr_trims_fraction() -> None:
    s = TrendFollowingStrategy(
        {
            "strategy": {
                "exits": _exits_for_kill_tests(
                    mode="partial",
                    sell_fraction=0.25,
                    max_spread_pct=50.0,
                    max_atr_pct=3.0,
                )
            }
        }
    )
    sig = s.check_exit(
        "SPY",
        100.0,
        100.0,
        1,
        spread_pct=0.1,
        atr_pct=5.0,
        partial_taken=True,
        trail_high=100.0,
        current_qty=80,
    )
    assert sig is not None
    assert sig.reason == ExitReason.KILL_SWITCH_PARTIAL
    assert sig.metadata.get("qty_to_sell") == 20
    assert "atr_pct" in sig.metadata


def test_kill_switch_default_is_full_exit() -> None:
    s = TrendFollowingStrategy({"strategy": {"exits": _exits_for_kill_tests()}})
    sig = s.check_exit(
        "SPY",
        100.0,
        100.0,
        1,
        spread_pct=2.0,
        atr_pct=None,
        partial_taken=True,
        trail_high=100.0,
        current_qty=100,
    )
    assert sig is not None
    assert sig.reason == ExitReason.KILL_SWITCH


def test_kill_switch_partial_sell_pct_alias() -> None:
    s = TrendFollowingStrategy(
        {
            "strategy": {
                "exits": _exits_for_kill_tests(
                    mode="partial",
                    partial_sell_pct=0.2,
                    max_spread_pct=50.0,
                    max_atr_pct=100.0,
                )
            }
        }
    )
    assert s.kill_switch_sell_fraction == pytest.approx(0.2)


def test_kill_switch_drawdown_threshold_alias_for_atr() -> None:
    ex = {
        "kill_switch": {
            "drawdown_threshold_pct": 4.5,
            "max_spread_pct": 50.0,
        },
        "use_trailing_stop": False,
        "smart_trailing": {"enabled": False},
        "time_bars_exit": 99999,
        "partial_take_profit_pct": 999.0,
        "min_hold_minutes": 0,
        "stop_loss_pct": 99.0,
    }
    s = TrendFollowingStrategy({"strategy": {"exits": ex}})
    assert s.kill_switch_max_atr_pct == pytest.approx(4.5)


def test_kill_switch_partial_single_share_sells_all() -> None:
    s = TrendFollowingStrategy(
        {"strategy": {"exits": _exits_for_kill_tests(mode="partial", sell_fraction=0.5)}}
    )
    sig = s.check_exit(
        "SPY",
        50.0,
        50.0,
        1,
        spread_pct=2.0,
        atr_pct=None,
        partial_taken=False,
        trail_high=None,
        current_qty=1,
    )
    assert sig is not None
    assert sig.reason == ExitReason.KILL_SWITCH_PARTIAL
    assert sig.metadata.get("qty_to_sell") == 1
