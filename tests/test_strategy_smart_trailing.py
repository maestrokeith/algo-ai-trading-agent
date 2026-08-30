"""Tests for ``strategy.exits.smart_trailing`` in :class:`~src.strategy.TrendFollowingStrategy`."""

from __future__ import annotations

import pytest

from src.strategy import ExitReason, TrendFollowingStrategy


def _base_exits(**overrides: object) -> dict:
    base: dict = {
        "stop_loss_pct": 10.0,
        "time_bars_exit": 100,
        "min_hold_minutes": 0,
        "partial_take_profit_pct": 2.0,
        "use_trailing_stop": True,
        "trailing_stop_pct": 1.0,
        "smart_trailing": {
            "enabled": True,
            "activate_profit_pct": 3.0,
            "trail_pct": 2.0,
            "min_hold_minutes": 10.0,
            "scale_out": [
                {"profit_pct": 5.0, "sell_pct": 0.25},
                {"profit_pct": 10.0, "sell_pct": 0.25},
            ],
        },
    }
    base.update(overrides)
    return base


def test_smart_trailing_scale_out_sorted_by_profit_pct() -> None:
    cfg = {
        "strategy": {
            "exits": _base_exits(
                smart_trailing={
                    "enabled": True,
                    "activate_profit_pct": 3.0,
                    "trail_pct": 2.0,
                    "min_hold_minutes": 0,
                    "scale_out": [
                        {"profit_pct": 10.0, "sell_pct": 0.25},
                        {"profit_pct": 5.0, "sell_pct": 0.25},
                    ],
                }
            )
        }
    }
    s = TrendFollowingStrategy(cfg)
    assert s.smart_trailing_scale_out == [(5.0, 0.25), (10.0, 0.25)]


def test_smart_scale_out_first_tier() -> None:
    s = TrendFollowingStrategy({"strategy": {"exits": _base_exits()}})
    # +6% → first scale tier (5%)
    sig = s.check_exit(
        "AAPL",
        100.0,
        106.0,
        bars_held=5,
        current_qty=10,
        minutes_held=15.0,
        smart_scale_out_index=0,
    )
    assert sig is not None
    assert sig.reason == ExitReason.PARTIAL_TAKE_PROFIT
    assert sig.metadata.get("smart_scale_level") == 0
    assert sig.metadata.get("qty_to_sell") == 2  # 25% of 10


def test_smart_scale_out_second_tier() -> None:
    s = TrendFollowingStrategy({"strategy": {"exits": _base_exits()}})
    sig = s.check_exit(
        "AAPL",
        100.0,
        111.0,
        bars_held=5,
        current_qty=8,
        minutes_held=15.0,
        smart_scale_out_index=1,
    )
    assert sig is not None
    assert sig.reason == ExitReason.PARTIAL_TAKE_PROFIT
    assert sig.metadata.get("smart_scale_level") == 1


def test_smart_min_hold_defers_scale_and_trail() -> None:
    s = TrendFollowingStrategy({"strategy": {"exits": _base_exits()}})
    # High profit but only 5 minutes held (< smart min_hold 10)
    sig = s.check_exit(
        "AAPL",
        100.0,
        120.0,
        bars_held=5,
        current_qty=10,
        minutes_held=5.0,
        trail_high=120.0,
        smart_scale_out_index=0,
    )
    assert sig is None


def test_smart_trailing_dynamic_trail_wider_with_atr() -> None:
    """ATR%% × mult can exceed fixed trail_pct → stop farther below peak."""
    exits = _base_exits()
    exits["smart_trailing"] = {
        "enabled": True,
        "activate_profit_pct": 3.0,
        "trail_pct": 2.0,
        "min_hold_minutes": 0.0,
        "dynamic_trail_pct": {"enabled": True, "floor_pct": 1.5, "atr_mult": 1.2},
        "scale_out": [],
    }
    s = TrendFollowingStrategy({"strategy": {"exits": exits}})
    # Fixed 2%: 110 * 0.98 = 107.8 → 107.7 triggers. ATR 2% → dynamic trail 2.4% → 110 * 0.976 = 107.36; 107.7 does not.
    sig_dyn = s.check_exit(
        "AAPL",
        100.0,
        107.7,
        bars_held=5,
        current_qty=10,
        minutes_held=15.0,
        trail_high=110.0,
        smart_scale_out_index=0,
        atr_pct=2.0,
    )
    assert sig_dyn is None
    sig_floor = s.check_exit(
        "AAPL",
        100.0,
        107.3,
        bars_held=5,
        current_qty=10,
        minutes_held=15.0,
        trail_high=110.0,
        smart_scale_out_index=0,
        atr_pct=2.0,
    )
    assert sig_floor is not None
    assert sig_floor.reason == ExitReason.TRAILING_STOP
    assert sig_floor.metadata.get("trail_pct") == pytest.approx(2.4)


def test_smart_trailing_stop_uses_trail_high() -> None:
    s = TrendFollowingStrategy({"strategy": {"exits": _base_exits()}})
    # Peak 110 → threshold 110 * (1 - 2%) = 107.8; price 107.7 triggers trail
    sig = s.check_exit(
        "AAPL",
        100.0,
        107.7,
        bars_held=5,
        current_qty=10,
        minutes_held=15.0,
        trail_high=110.0,
        smart_scale_out_index=2,  # no more scale tiers
    )
    assert sig is not None
    assert sig.reason == ExitReason.TRAILING_STOP
    assert sig.metadata.get("smart_trailing") is True
    assert sig.metadata.get("trail_high") == 110.0


def test_smart_trailing_requires_activate_profit_pct() -> None:
    s = TrendFollowingStrategy({"strategy": {"exits": _base_exits()}})
    # Below 3% activate: no trail even if price drops from entry
    sig = s.check_exit(
        "AAPL",
        100.0,
        101.0,
        bars_held=5,
        current_qty=10,
        minutes_held=15.0,
        trail_high=102.0,
        smart_scale_out_index=2,
    )
    assert sig is None


def test_smart_enabled_skips_legacy_partial_trail() -> None:
    exits = _base_exits()
    exits["partial_take_profit_pct"] = 1.0  # would fire legacy at +2%
    s = TrendFollowingStrategy({"strategy": {"exits": exits}})
    # +2% with no smart scale (index past tiers) and trail not active enough
    sig = s.check_exit(
        "AAPL",
        100.0,
        102.0,
        bars_held=5,
        partial_taken=False,
        current_qty=10,
        minutes_held=15.0,
        smart_scale_out_index=2,
    )
    assert sig is None


def test_builtin_smart_trailing_false_skips_builtin_smart_signals() -> None:
    exits = _base_exits()
    s = TrendFollowingStrategy({"strategy": {"exits": exits}})
    assert s.check_exit(
        "AAPL",
        100.0,
        112.0,
        bars_held=5,
        current_qty=100,
        minutes_held=15.0,
        smart_scale_out_index=0,
        builtin_smart_trailing=False,
    ) is None


def test_smart_disabled_uses_legacy_partial() -> None:
    exits = _base_exits()
    exits["smart_trailing"] = {"enabled": False}
    exits["partial_take_profit_pct"] = 2.0
    s = TrendFollowingStrategy({"strategy": {"exits": exits}})
    sig = s.check_exit("AAPL", 100.0, 103.0, bars_held=5, partial_taken=False, current_qty=10, minutes_held=15.0)
    assert sig is not None
    assert sig.reason == ExitReason.PARTIAL_TAKE_PROFIT
    assert "smart_scale_level" not in sig.metadata


def test_take_profit_pct_full_exit_when_smart_off() -> None:
    exits = _base_exits()
    exits["smart_trailing"] = {"enabled": False}
    exits["partial_take_profit_pct"] = 1.5
    exits["take_profit_pct"] = 3.0
    s = TrendFollowingStrategy({"strategy": {"exits": exits}})
    sig_full = s.check_exit(
        "AAPL", 100.0, 104.0, bars_held=5, partial_taken=False, current_qty=10, minutes_held=15.0
    )
    assert sig_full is not None
    assert sig_full.reason == ExitReason.TAKE_PROFIT

    sig_part = s.check_exit(
        "AAPL", 100.0, 102.0, bars_held=5, partial_taken=False, current_qty=10, minutes_held=15.0
    )
    assert sig_part is not None
    assert sig_part.reason == ExitReason.PARTIAL_TAKE_PROFIT
