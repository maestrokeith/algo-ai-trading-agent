"""``strategy.exits`` overweight trim helpers and :class:`TrendFollowingStrategy` fields."""

from __future__ import annotations

import pytest

from src.strategy import (
    TrendFollowingStrategy,
    compute_overweight_trim_shares,
    target_position_pct_to_fraction,
)


def test_target_position_pct_to_fraction_decimal() -> None:
    assert target_position_pct_to_fraction(0.08) == pytest.approx(0.08)
    assert target_position_pct_to_fraction("0.08") == pytest.approx(0.08)


def test_target_position_pct_to_fraction_percent_points() -> None:
    assert target_position_pct_to_fraction(8) == pytest.approx(0.08)
    assert target_position_pct_to_fraction(12.5) == pytest.approx(0.125)


def test_compute_overweight_trim_shares_zero_when_at_or_below_target() -> None:
    assert (
        compute_overweight_trim_shares(
            equity=100_000.0,
            position_market_value_usd=8000.0,
            qty=80,
            mid_price=100.0,
            target_fraction=0.08,
            aggressiveness=0.5,
        )
        == 0
    )


def test_compute_overweight_trim_shares_half_excess() -> None:
    # 12% of 100k = 12k; target 8k → excess 4k; aggr 0.5 → trim 2k → 20 sh @ 100
    n = compute_overweight_trim_shares(
        equity=100_000.0,
        position_market_value_usd=12_000.0,
        qty=200,
        mid_price=100.0,
        target_fraction=0.08,
        aggressiveness=0.5,
    )
    assert n == 20


def test_compute_overweight_trim_shares_caps_qty() -> None:
    n = compute_overweight_trim_shares(
        equity=10_000.0,
        position_market_value_usd=5000.0,
        qty=3,
        mid_price=100.0,
        target_fraction=0.08,
        aggressiveness=1.0,
    )
    assert n == 3


def test_trim_winners_maps_threshold_and_fraction() -> None:
    cfg = {
        "strategy": {
            "exits": {
                "trim_winners_enabled": True,
                "trim_threshold_pct": 1.5,
                "trim_fraction": 0.3,
                "trigger_profit_pct": 9.0,
                "partial_exit_ratio": 0.99,
                "min_hold_minutes": 0,
            }
        }
    }
    s = TrendFollowingStrategy(cfg)
    assert s.partial_take_profit_pct == pytest.approx(1.5)
    assert s.partial_exit_ratio == pytest.approx(0.3)


def test_trend_following_strategy_reads_overweight_exits() -> None:
    cfg = {
        "strategy": {
            "exits": {
                "trim_on_overweight": True,
                "target_position_pct": 0.08,
                "trim_aggressiveness": 0.5,
            }
        }
    }
    s = TrendFollowingStrategy(cfg)
    assert s.trim_on_overweight is True
    assert s.overweight_target_fraction == pytest.approx(0.08)
    assert s.trim_aggressiveness == pytest.approx(0.5)


def test_partial_trim_trigger_pct_overrides_trim_winners_threshold() -> None:
    cfg = {
        "strategy": {
            "exits": {
                "trim_winners_enabled": True,
                "trim_threshold_pct": 2.0,
                "trim_fraction": 0.4,
                "partial_trim_trigger_pct": 1.5,
                "min_hold_minutes": 0,
            }
        }
    }
    s = TrendFollowingStrategy(cfg)
    assert s.partial_trim_trigger_pct == pytest.approx(1.5)
    assert s.partial_take_profit_pct == pytest.approx(1.5)
    assert s.partial_exit_ratio == pytest.approx(0.4)


def test_trim_aggressiveness_clamped() -> None:
    cfg = {
        "strategy": {
            "exits": {
                "trim_on_overweight": True,
                "target_position_pct": 8,
                "trim_aggressiveness": 1.5,
            }
        }
    }
    s = TrendFollowingStrategy(cfg)
    assert s.trim_aggressiveness == pytest.approx(1.0)
