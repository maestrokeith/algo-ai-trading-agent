"""Tests for EXIT_EVAL logging and :meth:`TrendFollowingStrategy.exit_eval_flags_for_log`."""

from __future__ import annotations

import logging

import pytest

from src.strategy import TrendFollowingStrategy
from src.trading_engine import TradingEngine


def test_exit_eval_flags_for_log_sl_deferred_matches_check_exit() -> None:
    cfg = {
        "strategy": {
            "exits": {
                "stop_loss_pct": 2.8,
                "avoid_stop_loss_first_minutes_after_open": 5,
                "time_bars_exit": 100,
                "partial_take_profit_pct": 3.0,
                "min_hold_minutes": 0,
            }
        }
    }
    s = TrendFollowingStrategy(cfg)
    tp_hit, sl_hit, time_exit = s.exit_eval_flags_for_log(
        "QQQ",
        100.0,
        97.0,
        1,
        minutes_since_session_open_et=3.0,
        current_qty=10,
        partial_taken=False,
    )
    assert sl_hit is False
    assert tp_hit is False
    assert time_exit is False

    tp_hit2, sl_hit2, _ = s.exit_eval_flags_for_log(
        "QQQ",
        100.0,
        97.0,
        1,
        minutes_since_session_open_et=6.0,
        current_qty=10,
        partial_taken=False,
    )
    assert sl_hit2 is True
    assert tp_hit2 is False


def test_trigger_profit_pct_overrides_partial_take_profit_pct() -> None:
    cfg = {
        "strategy": {
            "exits": {
                "partial_take_profit_pct": 2.0,
                "trigger_profit_pct": 5.0,
                "time_bars_exit": 100,
                "min_hold_minutes": 0,
                "stop_loss_pct": 50.0,
            }
        }
    }
    s = TrendFollowingStrategy(cfg)
    assert s.partial_take_profit_pct == 5.0


def test_trim_winners_overrides_partial_take_and_fraction() -> None:
    cfg = {
        "strategy": {
            "exits": {
                "trigger_profit_pct": 9.0,
                "partial_exit_ratio": 0.5,
                "trim_winners_enabled": True,
                "trim_threshold_pct": 2.5,
                "trim_fraction": 0.25,
                "time_bars_exit": 100,
                "min_hold_minutes": 0,
                "stop_loss_pct": 50.0,
            }
        }
    }
    s = TrendFollowingStrategy(cfg)
    assert s.partial_take_profit_pct == pytest.approx(2.5)
    assert s.partial_exit_ratio == pytest.approx(0.25)


def test_exit_eval_flags_for_log_tp_hit() -> None:
    cfg = {
        "strategy": {
            "exits": {
                "stop_loss_pct": 10.0,
                "partial_take_profit_pct": 2.0,
                "time_bars_exit": 100,
                "min_hold_minutes": 0,
            }
        }
    }
    s = TrendFollowingStrategy(cfg)
    tp_hit, sl_hit, time_exit = s.exit_eval_flags_for_log(
        "SPY",
        100.0,
        103.0,
        1,
        minutes_since_session_open_et=None,
        current_qty=5,
        partial_taken=False,
        minutes_held=60.0,
    )
    assert tp_hit is True
    assert sl_hit is False
    assert time_exit is False


def test_exit_eval_flags_for_log_time_exit() -> None:
    cfg = {
        "strategy": {
            "exits": {
                "stop_loss_pct": 50.0,
                "partial_take_profit_pct": 99.0,
                "time_bars_exit": 5,
                "min_hold_minutes": 0,
            }
        }
    }
    s = TrendFollowingStrategy(cfg)
    tp_hit, sl_hit, time_exit = s.exit_eval_flags_for_log(
        "SPY",
        100.0,
        100.0,
        5,
        current_qty=1,
        partial_taken=False,
        minutes_held=120.0,
    )
    assert time_exit is True
    assert tp_hit is False
    assert sl_hit is False


def test_exit_eval_flags_for_log_short() -> None:
    cfg = {"strategy": {"exits": {"min_hold_minutes": 0}}}
    s = TrendFollowingStrategy(cfg)
    tp_hit, sl_hit, time_exit = s.exit_eval_flags_for_log_short(
        "SPY",
        100.0,
        97.0,
        3,
        1.5,
        2.0,
        10,
        minutes_held=60.0,
    )
    assert tp_hit is True
    assert sl_hit is False
    assert time_exit is False


def test_trading_engine_check_exit_logs_exit_eval_when_flag(caplog: pytest.LogCaptureFixture) -> None:
    engine = TradingEngine()
    with caplog.at_level(logging.INFO, logger="src.trading_engine"):
        engine.check_exit(
            "qqq",
            100.0,
            100.0,
            0,
            log_exit_context=True,
        )
    assert "QQQ EXIT_EVAL tp_hit=" in caplog.text
    assert "sl_hit=" in caplog.text and "time_exit=" in caplog.text
