"""Tests for portfolio cap / rotation helpers."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from src.exposure_gates import parse_strong_signal_cap_relief
from src.portfolio_replacement import (
    MAX_NEW_TRADES_PER_LOOP,
    MAX_POSITIONS,
    MAX_REPLACEMENTS_PER_LOOP,
    MIN_HOLD_TIME_MINUTES,
    max_portfolio_positions_from_config,
    max_replacements_per_entry_cycle,
    replacement_max_per_cycle,
    replacement_min_market_value_to_replace_usd,
    replacement_min_notional_for_incoming_usd,
    replacement_size_ok,
    replacement_strength_gap_ok,
    allowed_symbols_for_stock_orders_set,
    new_symbol_blocked_at_position_cap_only_replacement,
    effective_signal_strength,
    eligible_long_stock_symbols,
    replacement_rotate_target_ok,
    replacement_strength_ok,
    replacement_hold_strength,
    replacement_weakest_min_hold_ok,
    should_short_circuit_trend_long_symbol_scan,
    stronger_deferred_replacement_row,
    tracked_signal_strength,
    trend_long_blocked_by_portfolio_cap,
    WEAKEST_PICK_COMPOSITE_POSITION_SCORE,
    WEAKEST_PICK_ENTRY_SIGNAL_STRENGTH,
    WEAKEST_PICK_PNL_MOMENTUM_TREND,
    weakest_replacement_hold,
)


def test_eligible_long_excludes_bear_and_options() -> None:
    pos = [
        {"symbol": "NVDA", "qty": 10},
        {"symbol": "SQQQ", "qty": 5},
        {"symbol": "AAPL240119C00190000", "qty": 1},
    ]
    u = {"NVDA", "SQQQ", "AAPL"}
    bear = {"SQQQ", "SPXS"}
    assert eligible_long_stock_symbols(pos, universe_symbols=u, bear_etf_symbols=bear) == ["NVDA"]


def test_weakest_by_signal_strength() -> None:
    tr = {
        "AAA": {"qty": 1, "signal_strength": 0.9},
        "BBB": {"qty": 1, "signal_strength": 0.5},
    }
    sym, st = weakest_replacement_hold(tr, ["AAA", "BBB"])
    assert sym == "BBB" and st == 0.5


def test_weakest_by_signal_strength_notional_rows() -> None:
    tr = {
        "AAA": {"notional": 1000.0, "signal_strength": 0.9},
        "BBB": {"notional": 1000.0, "signal_strength": 0.5},
    }
    sym, st = weakest_replacement_hold(tr, ["AAA", "BBB"])
    assert sym == "BBB" and st == 0.5


def test_replacement_hold_strength_entry_signal() -> None:
    tr = {"Z": {"qty": 1, "signal_strength": 0.22}}
    assert replacement_hold_strength("Z", tr, [], get_bars=None, engine=None, rep_sub={}) == pytest.approx(0.22)


def test_weakest_health_mode_prefers_low_pnl_momentum_trend() -> None:
    """Weakest = minimum mean(pnl, trend_strength, momentum) when weakest_pick is health."""
    n = 220
    df_strong = pd.DataFrame(
        {
            "close": [100.0 + i * 0.4 for i in range(n)],
            "high": [101.0 + i * 0.4 for i in range(n)],
            "low": [99.0 + i * 0.4 for i in range(n)],
            "volume": [1e6] * n,
        }
    )
    df_weak = pd.DataFrame(
        {
            "close": [50.0 - min(i, 30) * 0.01 for i in range(n)],
            "high": [51.0 - min(i, 30) * 0.01 for i in range(n)],
            "low": [49.0 - min(i, 30) * 0.01 for i in range(n)],
            "volume": [5e5] * n,
        }
    )

    def _bars(sym: str) -> pd.DataFrame:
        return df_weak if sym == "BBB" else df_strong

    tr = {"AAA": {"qty": 1}, "BBB": {"qty": 1}}
    positions = [
        {"symbol": "AAA", "unrealized_plpc": 0.08},
        {"symbol": "BBB", "unrealized_plpc": -0.12},
    ]
    sym, _st = weakest_replacement_hold(
        tr,
        ["AAA", "BBB"],
        positions=positions,
        get_bars=_bars,
        engine=None,
        weakest_pick=WEAKEST_PICK_PNL_MOMENTUM_TREND,
    )
    assert sym == "BBB"


def test_parse_weakest_pick_aliases() -> None:
    from src.portfolio_replacement import parse_weakest_pick

    assert parse_weakest_pick({}) == WEAKEST_PICK_ENTRY_SIGNAL_STRENGTH
    assert parse_weakest_pick({"weakest_pick": "pnl_momentum_trend"}) == WEAKEST_PICK_PNL_MOMENTUM_TREND
    assert parse_weakest_pick({"weakest_definition": "health"}) == WEAKEST_PICK_PNL_MOMENTUM_TREND
    assert (
        parse_weakest_pick({"weakest_pick": "weakest_simple"})
        == WEAKEST_PICK_COMPOSITE_POSITION_SCORE
    )
    assert (
        parse_weakest_pick({"weakest_pick": "momentum_pnl_trend_strength_sum"})
        == WEAKEST_PICK_COMPOSITE_POSITION_SCORE
    )


def test_stronger_deferred_replacement_row_first_wins() -> None:
    a = {"sym_u": "A", "strength_eff": 1.1}
    assert stronger_deferred_replacement_row(None, a) is a


def test_stronger_deferred_replacement_row_higher_strength() -> None:
    low = {"sym_u": "AAA", "strength_eff": 1.0}
    high = {"sym_u": "ZZZ", "strength_eff": 1.2}
    assert stronger_deferred_replacement_row(low, high) == high
    assert stronger_deferred_replacement_row(high, low) == high


def test_stronger_deferred_replacement_row_tiebreak_sym_u() -> None:
    z_first = {"sym_u": "ZZZ", "strength_eff": 1.0}
    a_first = {"sym_u": "AAA", "strength_eff": 1.0}
    assert stronger_deferred_replacement_row(z_first, a_first) == a_first
    assert stronger_deferred_replacement_row(a_first, z_first) == a_first


def test_stronger_deferred_replacement_row_tie_priority_symbols() -> None:
    spy = {"sym_u": "SPY", "strength_eff": 1.0}
    msft = {"sym_u": "MSFT", "strength_eff": 1.0}
    pr = ("SPY", "QQQ", "NVDA", "MSFT")
    assert stronger_deferred_replacement_row(spy, msft, priority_symbols=pr) == spy
    assert stronger_deferred_replacement_row(msft, spy, priority_symbols=pr) == spy


def test_max_replacements_per_loop_is_two() -> None:
    assert MAX_REPLACEMENTS_PER_LOOP == 2


def test_replacement_min_market_value_parses() -> None:
    assert replacement_min_market_value_to_replace_usd({}) == 0.0
    assert replacement_min_market_value_to_replace_usd({"min_market_value_to_replace_usd": 750}) == 750.0
    assert replacement_min_market_value_to_replace_usd({"min_market_value_to_replace_usd": -5}) == 0.0
    assert replacement_min_market_value_to_replace_usd({"min_market_value_to_replace_usd": "x"}) == 0.0


def test_replacement_min_notional_parses() -> None:
    assert replacement_min_notional_for_incoming_usd({}) == 0.0
    assert replacement_min_notional_for_incoming_usd({"min_notional_for_incoming_usd": 500}) == 500.0
    assert replacement_min_notional_for_incoming_usd({"min_notional_for_incoming_usd": "bad"}) == 0.0


def test_replacement_max_per_cycle() -> None:
    assert replacement_max_per_cycle({}) == 1
    assert replacement_max_per_cycle({"max_replacements_per_entry_cycle": 3}) == 3
    assert replacement_max_per_cycle({"max_replacements_per_entry_cycle": 0}) == 0
    assert replacement_max_per_cycle({"max_replacements_per_entry_cycle": "nope"}) == 1


def test_replacement_strength_gap_ok() -> None:
    ok, _ = replacement_strength_gap_ok(
        1.0, 0.5, threshold=0.25, allow_equal_replacement=False, strength_jitter_max=0.05
    )
    assert ok
    ok2, r2 = replacement_strength_gap_ok(
        0.7, 0.5, threshold=0.25, allow_equal_replacement=False, strength_jitter_max=0.05
    )
    assert not ok2 and r2 and "insufficient strength" in r2
    ok3, r3 = replacement_strength_gap_ok(
        0.52, 0.5, threshold=0.25, allow_equal_replacement=False, strength_jitter_max=0.05
    )
    assert not ok3 and r3 and "too close" in r3


def test_replacement_size_ok() -> None:
    rep = {"min_market_value_to_replace_usd": 100, "min_notional_for_incoming_usd": 200}
    ok, _ = replacement_size_ok(
        weakest_market_value_usd=150, incoming_notional_usd=250, rep_cfg=rep
    )
    assert ok
    ok2, r2 = replacement_size_ok(
        weakest_market_value_usd=50, incoming_notional_usd=250, rep_cfg=rep
    )
    assert not ok2 and r2 and "too small" in r2
    ok3, r3 = replacement_size_ok(
        weakest_market_value_usd=150, incoming_notional_usd=100, rep_cfg=rep
    )
    assert not ok3 and r3 and "incoming order too small" in r3


def test_max_portfolio_positions_from_config_default_top_n() -> None:
    assert max_portfolio_positions_from_config(None) == MAX_POSITIONS
    assert max_portfolio_positions_from_config({}) == MAX_POSITIONS
    assert max_portfolio_positions_from_config({"max_positions": None}) == MAX_POSITIONS
    assert max_portfolio_positions_from_config({"max_positions": ""}) == MAX_POSITIONS
    assert max_portfolio_positions_from_config({"max_positions": 0}) == MAX_POSITIONS
    assert max_portfolio_positions_from_config({"max_positions": "x"}) == MAX_POSITIONS


def test_max_portfolio_positions_from_config_parses_and_unbounded() -> None:
    assert max_portfolio_positions_from_config({"max_positions": 3}) == 3
    assert max_portfolio_positions_from_config({"max_positions": "7"}) == 7
    assert max_portfolio_positions_from_config({"max_positions": 1000000000}) == 1000000000


def test_max_portfolio_positions_from_config_allocator_fallback() -> None:
    assert max_portfolio_positions_from_config({"allocator": {"max_positions": 8}}) == 8
    assert max_portfolio_positions_from_config({"max_positions": 4, "allocator": {"max_positions": 99}}) == 4


def test_max_replacements_per_entry_cycle_reads_portfolio() -> None:
    assert max_replacements_per_entry_cycle(None) == MAX_REPLACEMENTS_PER_LOOP
    assert max_replacements_per_entry_cycle({}) == MAX_REPLACEMENTS_PER_LOOP
    assert (
        max_replacements_per_entry_cycle(
            {"replacement": {"max_replacements_per_entry_cycle": 1}}
        )
        == 1
    )
    assert (
        max_replacements_per_entry_cycle(
            {"replacement": {"max_replacements_per_entry_cycle": 0}}
        )
        == MAX_REPLACEMENTS_PER_LOOP
    )
    assert (
        max_replacements_per_entry_cycle(
            {"replacement": {"max_replacements_per_entry_cycle": "bad"}}
        )
        == MAX_REPLACEMENTS_PER_LOOP
    )


def test_max_new_trades_per_loop_is_three() -> None:
    assert MAX_NEW_TRADES_PER_LOOP == 3


def test_replacement_rotate_target_ok_sells_weakest() -> None:
    old = datetime(2020, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    now = datetime(2020, 1, 1, 16, 0, 0, tzinfo=timezone.utc)
    tr = {
        "AAA": {"qty": 1, "signal_strength": 0.9, "entry_time": old.isoformat()},
        "BBB": {"qty": 1, "signal_strength": 0.5, "entry_time": old.isoformat()},
    }
    el = ["AAA", "BBB"]
    decision = SimpleNamespace(entry_signal=SimpleNamespace(strength=1.2))
    ok, wsym, reason = replacement_rotate_target_ok(
        incoming_sym_upper="AAA",
        decision=decision,
        tracked=tr,
        eligible_active=el,
        max_port_positions=2,
        rep_sub={"min_hold_minutes": 0},
        now=now,
        replace_if_weakest_older_than_bars=None,
        strength_jitter_max=0.0,
    )
    assert ok and wsym == "BBB" and reason is None


def test_new_symbol_at_cap_allows_scan_for_rotation_without_replacement_flag() -> None:
    cp = {f"S{i}": {} for i in range(6)}
    assert not new_symbol_blocked_at_position_cap_only_replacement(
        max_positions=6,
        enable_replacement=False,
        current_positions=cp,
        symbol_upper="NEW",
    )


def test_new_symbol_at_cap_allows_with_replacement_flag() -> None:
    cp = {f"S{i}": {} for i in range(6)}
    assert not new_symbol_blocked_at_position_cap_only_replacement(
        max_positions=6,
        enable_replacement=True,
        current_positions=cp,
        symbol_upper="NEW",
    )


def test_new_symbol_defer_to_ranked_batch_allows_past_name_cap() -> None:
    cp = {f"S{i}": {} for i in range(6)}
    assert not new_symbol_blocked_at_position_cap_only_replacement(
        max_positions=6,
        enable_replacement=False,
        current_positions=cp,
        symbol_upper="NEW",
        defer_to_ranked_batch=True,
    )


def test_new_symbol_not_blocked_when_already_held() -> None:
    cp = {"SPY": {}, "QQQ": {}}
    assert not new_symbol_blocked_at_position_cap_only_replacement(
        max_positions=6,
        enable_replacement=False,
        current_positions=cp,
        symbol_upper="SPY",
    )


def test_replacement_rotate_target_ok_below_cap() -> None:
    tr = {"AAA": {"qty": 1, "signal_strength": 0.5}}
    decision = SimpleNamespace(entry_signal=SimpleNamespace(strength=2.0))
    ok, wsym, reason = replacement_rotate_target_ok(
        incoming_sym_upper="ZZZ",
        decision=decision,
        tracked=tr,
        eligible_active=["AAA"],
        max_port_positions=2,
        rep_sub={},
        now=datetime(2020, 1, 1, tzinfo=timezone.utc),
        replace_if_weakest_older_than_bars=None,
        strength_jitter_max=0.0,
    )
    assert not ok and wsym is None and reason is not None


def test_replacement_rotate_target_ok_incoming_is_weakest() -> None:
    tr = {
        "AAA": {"qty": 1, "signal_strength": 0.9, "entry_time": "2020-01-01T10:00:00+00:00"},
        "BBB": {"qty": 1, "signal_strength": 0.5, "entry_time": "2020-01-01T10:00:00+00:00"},
    }
    el = ["AAA", "BBB"]
    decision = SimpleNamespace(entry_signal=SimpleNamespace(strength=2.0))
    ok, wsym, reason = replacement_rotate_target_ok(
        incoming_sym_upper="BBB",
        decision=decision,
        tracked=tr,
        eligible_active=el,
        max_port_positions=2,
        rep_sub={"min_hold_minutes": 0},
        now=datetime(2020, 1, 2, tzinfo=timezone.utc),
        replace_if_weakest_older_than_bars=None,
        strength_jitter_max=0.0,
    )
    assert not ok and wsym is None and "weakest" in (reason or "").lower()


def test_effective_signal_strength_zero_jitter() -> None:
    assert effective_signal_strength(1.25, 0.0) == 1.25


def test_effective_signal_strength_seeded() -> None:
    rng = random.Random(12345)
    a = effective_signal_strength(1.0, 0.01, rng=rng)
    rng2 = random.Random(12345)
    b = effective_signal_strength(1.0, 0.01, rng=rng2)
    assert a == b
    assert 1.0 <= a <= 1.01


def test_replacement_strength_ok_strictly_greater_than_weakest() -> None:
    assert replacement_strength_ok(1.2, 1.0)
    assert replacement_strength_ok(1.000000002, 1.0)
    assert not replacement_strength_ok(1.0, 1.0)
    assert not replacement_strength_ok(0.99, 1.0)
    assert not replacement_strength_ok(0.9, 1.0)


def test_replacement_stale_weakest_bypasses_strength() -> None:
    assert replacement_strength_ok(
        0.5, 1.0, weakest_age_bars=25, replace_if_weakest_older_than_bars=20
    )
    assert not replacement_strength_ok(
        0.5, 1.0, weakest_age_bars=20, replace_if_weakest_older_than_bars=20
    )
    assert not replacement_strength_ok(
        0.5, 1.0, weakest_age_bars=25, replace_if_weakest_older_than_bars=None
    )
    assert not replacement_strength_ok(
        0.5, 1.0, weakest_age_bars=None, replace_if_weakest_older_than_bars=20
    )


def test_tracked_signal_strength_default() -> None:
    assert tracked_signal_strength({}) == 1.0
    assert tracked_signal_strength({"signal_strength": 0.7}) == 0.7


def test_allowed_symbols_for_stock_orders_omit() -> None:
    assert allowed_symbols_for_stock_orders_set(None) is None
    assert allowed_symbols_for_stock_orders_set({}) is None
    assert allowed_symbols_for_stock_orders_set({"max_positions": 3}) is None


def test_allowed_symbols_for_stock_orders_list() -> None:
    s = allowed_symbols_for_stock_orders_set(
        {"allowed_symbols_for_stock_orders": ["qqq", "SPY"]}
    )
    assert s == frozenset({"QQQ", "SPY"})


def test_allowed_symbols_for_stock_orders_empty() -> None:
    assert allowed_symbols_for_stock_orders_set({"allowed_symbols_for_stock_orders": []}) == frozenset()


def test_should_not_short_circuit_broker_row_cap() -> None:
    """At broker row count cap we still per-symbol scan so rotation at portfolio cap can run."""
    assert not should_short_circuit_trend_long_symbol_scan(
        top_n_enabled=False,
        enable_replacement=False,
        allow_add=False,
        broker_position_count=MAX_POSITIONS,
    )
    assert not should_short_circuit_trend_long_symbol_scan(
        top_n_enabled=False,
        enable_replacement=False,
        allow_add=False,
        broker_position_count=MAX_POSITIONS - 1,
    )


def test_should_not_short_circuit_eligible_at_cap_if_broker_rows_below_max() -> None:
    """Portfolio max_positions can be hit on eligible longs; short-circuit uses broker row count only."""
    assert not should_short_circuit_trend_long_symbol_scan(
        top_n_enabled=False,
        enable_replacement=False,
        allow_add=False,
        broker_position_count=3,
    )


def test_replacement_weakest_min_hold_blocks_when_too_young() -> None:
    now = datetime(2026, 4, 10, 14, 0, 0, tzinfo=timezone.utc)
    entry = (now - timedelta(minutes=5)).isoformat()
    ok, msg = replacement_weakest_min_hold_ok(
        weakest_entry_time_iso=entry,
        now=now,
        min_hold_minutes=None,
    )
    assert ok is False
    assert msg is not None
    assert "min hold" in msg.lower()
    assert str(int(MIN_HOLD_TIME_MINUTES)) in msg


def test_replacement_weakest_min_hold_allows_when_old_enough() -> None:
    now = datetime(2026, 4, 10, 14, 0, 0, tzinfo=timezone.utc)
    entry = (now - timedelta(minutes=100)).isoformat()
    ok, msg = replacement_weakest_min_hold_ok(
        weakest_entry_time_iso=entry,
        now=now,
        min_hold_minutes=None,
    )
    assert ok is True
    assert msg is None


def test_replacement_weakest_min_hold_disabled_when_zero() -> None:
    now = datetime(2026, 4, 10, 14, 0, 0, tzinfo=timezone.utc)
    entry = (now - timedelta(minutes=1)).isoformat()
    ok, msg = replacement_weakest_min_hold_ok(
        weakest_entry_time_iso=entry,
        now=now,
        min_hold_minutes=0,
    )
    assert ok is True
    assert msg is None


def test_should_not_short_circuit_when_add_replace_or_topn() -> None:
    assert not should_short_circuit_trend_long_symbol_scan(
        top_n_enabled=True,
        enable_replacement=False,
        allow_add=False,
        broker_position_count=MAX_POSITIONS + 5,
    )
    assert not should_short_circuit_trend_long_symbol_scan(
        top_n_enabled=False,
        enable_replacement=True,
        allow_add=False,
        broker_position_count=MAX_POSITIONS + 5,
    )
    assert not should_short_circuit_trend_long_symbol_scan(
        top_n_enabled=False,
        enable_replacement=False,
        allow_add=True,
        broker_position_count=MAX_POSITIONS + 5,
    )


def test_cap_not_blocked_below_max() -> None:
    assert not trend_long_blocked_by_portfolio_cap(
        max_positions=12,
        enable_replacement=False,
        allow_add=False,
        num_eligible_long_stocks=11,
        symbol_upper="NVDA",
        eligible_long_symbols_upper={"AAPL"},
    )


def test_cap_allows_new_name_at_max_for_rotation_scan() -> None:
    assert not trend_long_blocked_by_portfolio_cap(
        max_positions=12,
        enable_replacement=False,
        allow_add=False,
        num_eligible_long_stocks=12,
        symbol_upper="NVDA",
        eligible_long_symbols_upper={"AAPL"},
    )


def test_cap_not_blocked_top_n_batch_mode() -> None:
    assert not trend_long_blocked_by_portfolio_cap(
        max_positions=12,
        enable_replacement=False,
        allow_add=False,
        num_eligible_long_stocks=12,
        symbol_upper="NVDA",
        eligible_long_symbols_upper={"AAPL"},
        top_n_batch_mode=True,
    )


def test_cap_allows_add_at_max_for_held_symbol() -> None:
    assert not trend_long_blocked_by_portfolio_cap(
        max_positions=12,
        enable_replacement=False,
        allow_add=True,
        num_eligible_long_stocks=12,
        symbol_upper="NVDA",
        eligible_long_symbols_upper={"NVDA", "AAPL"},
    )


def test_cap_allows_new_ticker_at_max_with_allow_add_for_dispatch_rotation() -> None:
    assert not trend_long_blocked_by_portfolio_cap(
        max_positions=12,
        enable_replacement=False,
        allow_add=True,
        num_eligible_long_stocks=12,
        symbol_upper="MSFT",
        eligible_long_symbols_upper={"NVDA", "AAPL"},
    )


def test_cap_not_blocked_when_replacement_enabled() -> None:
    assert not trend_long_blocked_by_portfolio_cap(
        max_positions=12,
        enable_replacement=True,
        allow_add=False,
        num_eligible_long_stocks=12,
        symbol_upper="MSFT",
        eligible_long_symbols_upper={"NVDA"},
    )


def test_cap_strong_signal_relief_bypasses_position_cap_gate() -> None:
    r = parse_strong_signal_cap_relief(
        {"portfolio": {"strong_signal_cap_relief": {"enabled": True, "min_strength": 0.82}}}
    )
    assert not trend_long_blocked_by_portfolio_cap(
        max_positions=12,
        enable_replacement=False,
        allow_add=True,
        num_eligible_long_stocks=12,
        symbol_upper="MSFT",
        eligible_long_symbols_upper={"NVDA", "AAPL"},
        incoming_signal_strength=0.9,
        cap_relief=r,
    )


def test_cap_relief_symbols_only_bypass_for_listed_ticker() -> None:
    r = parse_strong_signal_cap_relief(
        {
            "portfolio": {
                "strong_signal_cap_relief": {
                    "enabled": True,
                    "min_strength": 0.82,
                    "relief_symbols": ["SPY"],
                }
            }
        }
    )
    assert not trend_long_blocked_by_portfolio_cap(
        max_positions=12,
        enable_replacement=False,
        allow_add=True,
        num_eligible_long_stocks=12,
        symbol_upper="QQQ",
        eligible_long_symbols_upper={"NVDA", "AAPL"},
        incoming_signal_strength=0.99,
        cap_relief=r,
    )
    assert not trend_long_blocked_by_portfolio_cap(
        max_positions=12,
        enable_replacement=False,
        allow_add=True,
        num_eligible_long_stocks=12,
        symbol_upper="SPY",
        eligible_long_symbols_upper={"NVDA", "AAPL"},
        incoming_signal_strength=0.99,
        cap_relief=r,
    )


def test_new_symbol_cap_relief_bypass_when_strong() -> None:
    cp = {f"S{i}": {} for i in range(6)}
    r = parse_strong_signal_cap_relief(
        {"portfolio": {"strong_signal_cap_relief": {"enabled": True, "min_strength": 0.82}}}
    )
    assert not new_symbol_blocked_at_position_cap_only_replacement(
        max_positions=6,
        enable_replacement=False,
        current_positions=cp,
        symbol_upper="NEW",
        incoming_signal_strength=0.91,
        cap_relief=r,
    )
