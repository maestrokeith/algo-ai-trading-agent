"""Tests for strategy.exits.no_trim_before_min_hold and trim_deferred_for_min_hold."""
from __future__ import annotations

import pytest

from src.strategy import TrendFollowingStrategy


def _minimal_strategy_cfg(**exits: object) -> dict:
    return {
        "strategy": {
            "trend_following": {"ma_fast": 10, "ma_slow": 50},
            "exits": {
                "stop_loss_pct": 5.0,
                "min_hold_minutes": 60,
                **exits,
            },
        }
    }


def test_no_trim_before_min_hold_default_off() -> None:
    s = TrendFollowingStrategy(_minimal_strategy_cfg())
    assert s.no_trim_before_min_hold is False


def test_no_trim_before_min_hold_reads_true() -> None:
    s = TrendFollowingStrategy(_minimal_strategy_cfg(no_trim_before_min_hold=True))
    assert s.no_trim_before_min_hold is True


def test_trim_deferred_false_when_flag_off() -> None:
    s = TrendFollowingStrategy(_minimal_strategy_cfg(no_trim_before_min_hold=False))
    assert s.trim_deferred_for_min_hold(minutes_held=1.0, bars_held=0) is False


def test_trim_deferred_false_when_min_hold_zero() -> None:
    s = TrendFollowingStrategy(
        _minimal_strategy_cfg(
            no_trim_before_min_hold=True,
            min_hold_minutes=0,
        )
    )
    assert s.trim_deferred_for_min_hold(minutes_held=0.0, bars_held=0) is False


def test_trim_deferred_false_when_minutes_unknown() -> None:
    s = TrendFollowingStrategy(_minimal_strategy_cfg(no_trim_before_min_hold=True))
    assert s.trim_deferred_for_min_hold(minutes_held=None, bars_held=0) is False


def test_trim_deferred_true_inside_min_hold() -> None:
    s = TrendFollowingStrategy(_minimal_strategy_cfg(no_trim_before_min_hold=True))
    assert s.trim_deferred_for_min_hold(minutes_held=30.0, bars_held=0) is True


def test_trim_deferred_false_after_min_hold() -> None:
    s = TrendFollowingStrategy(_minimal_strategy_cfg(no_trim_before_min_hold=True))
    assert s.trim_deferred_for_min_hold(minutes_held=120.0, bars_held=0) is False
