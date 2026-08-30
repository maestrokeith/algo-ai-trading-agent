"""Tests for :mod:`src.smart_exit`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.strategy import TrendFollowingStrategy

from src.smart_exit import (
    EXIT_TRAILING_STOP,
    SmartExitPositionState,
    bump_high_price,
    check_scale_out,
    compute_trailing_stop,
    compute_unrealized_pct,
    effective_smart_trail_pct,
    load_smart_exit_state_from_row,
    new_smart_exit_position_state,
    process_smart_exit,
    should_activate_trailing,
    smart_exit_state_from_json,
    smart_exit_state_to_json,
    smart_trailing_cfg_for_process,
)


def test_compute_unrealized_pct() -> None:
    assert compute_unrealized_pct(100.0, 103.0) == pytest.approx(3.0)
    assert compute_unrealized_pct(200.0, 190.0) == pytest.approx(-5.0)


def test_should_activate_trailing() -> None:
    cfg = {"activate_profit_pct": 3.0}
    assert should_activate_trailing(2.9, cfg) is False
    assert should_activate_trailing(3.0, cfg) is True


def test_compute_trailing_stop() -> None:
    cfg = {"trail_pct": 2.0}
    assert compute_trailing_stop(100.0, cfg) == pytest.approx(98.0)
    assert compute_trailing_stop(110.0, cfg) == pytest.approx(107.8)


def test_effective_smart_trail_pct_dynamic() -> None:
    assert (
        effective_smart_trail_pct(
            fixed_trail_pct=2.0,
            dynamic_enabled=True,
            floor_pct=1.5,
            atr_mult=1.2,
            atr_pct=2.0,
        )
        == pytest.approx(2.4)
    )
    assert (
        effective_smart_trail_pct(
            fixed_trail_pct=2.0,
            dynamic_enabled=True,
            floor_pct=1.5,
            atr_mult=1.2,
            atr_pct=1.0,
        )
        == pytest.approx(1.5)
    )


def test_effective_smart_trail_pct_fallback_fixed() -> None:
    assert (
        effective_smart_trail_pct(
            fixed_trail_pct=2.0,
            dynamic_enabled=True,
            floor_pct=1.5,
            atr_mult=1.2,
            atr_pct=None,
        )
        == 2.0
    )
    assert (
        effective_smart_trail_pct(
            fixed_trail_pct=2.0,
            dynamic_enabled=False,
            floor_pct=1.5,
            atr_mult=1.2,
            atr_pct=3.0,
        )
        == 2.0
    )


def test_check_scale_out_empty_already() -> None:
    scale = [
        {"profit_pct": 5.0, "sell_pct": 0.25},
        {"profit_pct": 10.0, "sell_pct": 0.25},
    ]
    done: set[float] = set()
    assert check_scale_out(4.0, scale, done) == []
    out = check_scale_out(5.0, scale, done)
    assert len(out) == 1
    assert out[0]["profit_pct"] == 5.0


def test_check_scale_out_skips_done_tier() -> None:
    scale = [
        {"profit_pct": 5.0, "sell_pct": 0.25},
        {"profit_pct": 10.0, "sell_pct": 0.25},
    ]
    done = {5.0}
    out = check_scale_out(11.0, scale, done)
    assert len(out) == 1
    assert out[0]["profit_pct"] == 10.0


def test_check_scale_out_multiple_in_one_pass() -> None:
    scale = [
        {"profit_pct": 5.0, "sell_pct": 0.25},
        {"profit_pct": 10.0, "sell_pct": 0.25},
    ]
    done: set[float] = set()
    out = check_scale_out(12.0, scale, done)
    assert [float(x["profit_pct"]) for x in out] == [5.0, 10.0]


def test_new_smart_exit_position_state() -> None:
    st = new_smart_exit_position_state(900.0)
    assert st.entry_price == 900.0
    assert st.high_price == 900.0
    assert st.scaled_levels == set()
    assert st.trailing_active is False


def test_bump_high_price() -> None:
    st = new_smart_exit_position_state(100.0)
    bump_high_price(st, 105.0)
    assert st.high_price == 105.0
    bump_high_price(st, 103.0)
    assert st.high_price == 105.0


def test_smart_exit_state_json_roundtrip() -> None:
    st = SmartExitPositionState(
        entry_price=900.0,
        high_price=920.0,
        scaled_levels={5.0, 10.0},
        trailing_active=True,
    )
    raw = smart_exit_state_to_json(st)
    st2 = smart_exit_state_from_json(raw)
    assert st2 is not None
    assert st2.entry_price == 900.0
    assert st2.high_price == 920.0
    assert st2.scaled_levels == {5.0, 10.0}
    assert st2.trailing_active is True


def test_load_smart_exit_state_from_row_embedded() -> None:
    row = {
        "qty": 1,
        "entry_price": 900.0,
        "smart_exit_state": {
            "entry_price": 900.0,
            "high_price": 910.0,
            "scaled_levels": [5.0],
            "trailing_active": True,
        },
    }
    st = load_smart_exit_state_from_row(row)
    assert st is not None
    assert st.high_price == 910.0
    assert st.scaled_levels == {5.0}
    assert st.trailing_active is True


def test_load_smart_exit_state_from_row_legacy_trail_high() -> None:
    row = {"qty": 1, "entry_price": 100.0, "trail_high": 108.0}
    st = load_smart_exit_state_from_row(row)
    assert st is not None
    assert st.high_price == 108.0
    assert st.scaled_levels == set()


def test_process_smart_exit_bumps_high_and_arms_trailing() -> None:
    sells: list[tuple[str, int]] = []
    alls: list[str] = []

    def sell(sym: str, q: int) -> None:
        sells.append((sym, q))

    def sell_all(sym: str) -> None:
        alls.append(sym)

    pos = SimpleNamespace(symbol="QQQ", qty=100)
    st = new_smart_exit_position_state(100.0)
    cfg = {"activate_profit_pct": 3.0, "trail_pct": 2.0, "scale_out": []}
    assert process_smart_exit(pos, 104.0, cfg, st, sell=sell, sell_all=sell_all) is None
    assert st.high_price == 104.0
    assert st.trailing_active is True
    assert sells == []


def test_process_smart_exit_trailing_stop_calls_sell_all() -> None:
    sells: list[tuple[str, int]] = []
    alls: list[str] = []

    def sell(sym: str, q: int) -> None:
        sells.append((sym, q))

    def sell_all(sym: str) -> None:
        alls.append(sym)

    pos = SimpleNamespace(symbol="QQQ", qty=100)
    st = SmartExitPositionState(entry_price=100.0, high_price=110.0, scaled_levels=set(), trailing_active=True)
    cfg = {"activate_profit_pct": 3.0, "trail_pct": 2.0, "scale_out": []}
    # threshold 110 * 0.98 = 107.8
    out = process_smart_exit(pos, 107.0, cfg, st, sell=sell, sell_all=sell_all)
    assert out == EXIT_TRAILING_STOP
    assert alls == ["QQQ"]


def test_process_smart_exit_scale_out_sequential_qty() -> None:
    sells: list[tuple[str, int]] = []

    def sell(sym: str, q: int) -> None:
        sells.append((sym, q))

    def sell_all(sym: str) -> None:
        raise AssertionError("unexpected sell_all")

    pos = SimpleNamespace(symbol="X", qty=100)
    st = new_smart_exit_position_state(100.0)
    cfg = {
        "activate_profit_pct": 3.0,
        "trail_pct": 2.0,
        "scale_out": [
            {"profit_pct": 5.0, "sell_pct": 0.25},
            {"profit_pct": 10.0, "sell_pct": 0.25},
        ],
    }
    assert process_smart_exit(pos, 112.0, cfg, st, sell=sell, sell_all=sell_all) is None
    assert sells[0] == ("X", 25)
    assert sells[1] == ("X", 18)
    assert st.scaled_levels == {5.0, 10.0}


def test_process_smart_exit_scale_out_stops_when_sell_returns_false() -> None:
    calls = 0

    def sell(sym: str, q: int) -> bool | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return True
        return False

    def sell_all(sym: str) -> None:
        raise AssertionError("unexpected sell_all")

    pos = SimpleNamespace(symbol="X", qty=100)
    st = new_smart_exit_position_state(100.0)
    cfg = {
        "activate_profit_pct": 3.0,
        "trail_pct": 2.0,
        "scale_out": [
            {"profit_pct": 5.0, "sell_pct": 0.25},
            {"profit_pct": 10.0, "sell_pct": 0.25},
        ],
    }
    assert process_smart_exit(pos, 112.0, cfg, st, sell=sell, sell_all=sell_all) is None
    assert calls == 2
    assert st.scaled_levels == {5.0}


def test_smart_trailing_cfg_for_process() -> None:
    cfg = {
        "strategy": {
            "exits": {
                "stop_loss_pct": 10.0,
                "time_bars_exit": 100,
                "smart_trailing": {
                    "enabled": True,
                    "activate_profit_pct": 3.0,
                    "trail_pct": 2.0,
                    "scale_out": [
                        {"profit_pct": 10.0, "sell_pct": 0.2},
                        {"profit_pct": 5.0, "sell_pct": 0.25},
                    ],
                },
            }
        }
    }
    s = TrendFollowingStrategy(cfg)
    out = smart_trailing_cfg_for_process(s)
    assert out["activate_profit_pct"] == 3.0
    assert out["trail_pct"] == 2.0
    assert out["scale_out"] == [
        {"profit_pct": 5.0, "sell_pct": 0.25},
        {"profit_pct": 10.0, "sell_pct": 0.2},
    ]


def test_smart_trailing_cfg_for_process_dynamic_atr() -> None:
    cfg = {
        "strategy": {
            "exits": {
                "stop_loss_pct": 10.0,
                "time_bars_exit": 100,
                "smart_trailing": {
                    "enabled": True,
                    "activate_profit_pct": 3.0,
                    "trail_pct": 2.0,
                    "dynamic_trail_pct": {"enabled": True, "floor_pct": 1.5, "atr_mult": 1.2},
                    "scale_out": [],
                },
            }
        }
    }
    s = TrendFollowingStrategy(cfg)
    out = smart_trailing_cfg_for_process(s, atr_pct=2.0)
    assert out["trail_pct"] == pytest.approx(2.4)


def test_process_smart_exit_accepts_quantity_attr() -> None:
    sells: list[tuple[str, int]] = []

    def sell(sym: str, q: int) -> None:
        sells.append((sym, q))

    def sell_all(sym: str) -> None:
        pass

    pos = SimpleNamespace(symbol="Y", quantity=40)
    st = new_smart_exit_position_state(100.0)
    cfg = {
        "activate_profit_pct": 3.0,
        "trail_pct": 2.0,
        "scale_out": [{"profit_pct": 5.0, "sell_pct": 0.25}],
    }
    process_smart_exit(pos, 106.0, cfg, st, sell=sell, sell_all=sell_all)
    assert sells == [("Y", 10)]
