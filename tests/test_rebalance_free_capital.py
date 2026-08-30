"""Tests for rebalance_free_capital planning helpers."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import random

from src.rebalance_free_capital import (
    broker_long_shares_for_symbol,
    broker_position_market_value_usd,
    broker_position_unrealized_pnl_pct,
    effective_allow_add_after_capital_trim,
    emergency_bulk_trim_notional_usd,
    get_top_n_positions,
    gross_liquidation_trim_shares,
    just_trimmed_position,
    long_stock_symbols_by_market_value_desc,
    parse_rebalance_free_capital_cfg,
    plan_bulk_notional_trims_for_free_capital,
    plan_emergency_deleverage_portfolio_pct_trims,
    plan_full_exit_weakest_for_gross_delever,
    plan_full_exit_weakest_when_stronger,
    plan_proportional_gross_delever_notional_trims,
    plan_weakest_gross_unwind_phase1,
    plan_weakest_trim_for_free_capital,
    position_has_signal_deterioration,
    rebalance_trim_fraction_for_attempt,
    rfc_uses_largest_exposure_notional_trim,
    symbols_ordered_for_bulk_trim_priority,
    trim_candidate_symbols_largest_exposure_notional,
    trim_fraction_by_gross_leverage,
    trim_qty_for_fraction,
)


def test_just_trimmed_position() -> None:
    s = {"QQQ"}
    assert just_trimmed_position("QQQ", s) is True
    assert just_trimmed_position("qqq", s) is True
    assert just_trimmed_position("SPY", s) is False


def test_effective_allow_add_after_capital_trim() -> None:
    trimmed = {"NVDA"}
    assert (
        effective_allow_add_after_capital_trim(
            "NVDA", portfolio_allow_add=False, symbols_trimmed_this_scan=trimmed
        )
        is True
    )
    assert (
        effective_allow_add_after_capital_trim(
            "SPY", portfolio_allow_add=False, symbols_trimmed_this_scan=trimmed
        )
        is False
    )
    assert (
        effective_allow_add_after_capital_trim(
            "SPY", portfolio_allow_add=True, symbols_trimmed_this_scan=trimmed
        )
        is True
    )


def test_parse_rebalance_free_capital_cfg_defaults() -> None:
    assert parse_rebalance_free_capital_cfg(None)["enabled"] is False
    b = parse_rebalance_free_capital_cfg({})
    assert b["enabled"] is False
    assert b.get("trim_target", "weakest") == "weakest"
    assert b.get("largest_exposure_max_try", 3) == 3
    assert b["gross_liquidation"]["enabled"] is False
    assert b["gross_liquidation"]["target_gross_pct"] == pytest.approx(95.0)
    assert b["gross_liquidation"]["passes"] == 2
    assert b["bulk_trim"]["enabled"] is False
    assert b["bulk_trim"]["notional_per_symbol_usd"] == pytest.approx(1500.0)
    assert b["bulk_trim"]["max_symbols_per_pass"] == 3
    assert b["bulk_trim"]["buy_cooldown_minutes"] == pytest.approx(30.0)
    tpp0 = b["top_position_protection"]
    assert tpp0["enabled"] is True
    assert tpp0["n"] == 3
    assert tpp0["intensity_mult"] == pytest.approx(0.5)
    assert b["first_pass_winner_pnl_skip_pct"] == pytest.approx(3.0)
    _tpu0 = b["two_phase_gross_cap_unwind"]
    assert _tpu0["enabled"] is False
    assert _tpu0["phase1_weakest_trim_fraction"] == pytest.approx(0.5)
    assert _tpu0["proportional_max_submits"] == 25
    cfg = parse_rebalance_free_capital_cfg(
        {"rebalance_free_capital": {"enabled": True, "trim_fraction": 0.25, "max_trims_per_entry_scan": 2}}
    )
    assert cfg["enabled"] is True
    assert cfg["trim_fraction"] == pytest.approx(0.25)
    assert cfg["max_trims_per_entry_scan"] == 2
    assert cfg["exclude_incoming_symbol"] is True
    assert cfg["rotate_full_weakest_when_stronger"] is False
    assert cfg.get("trim_target", "weakest") == "weakest"
    assert cfg.get("largest_exposure_max_try", 3) == 3
    assert cfg["gross_liquidation"]["enabled"] is False


def test_parse_rotate_full_weakest_flag() -> None:
    cfg = parse_rebalance_free_capital_cfg(
        {"rebalance_free_capital": {"rotate_full_weakest_when_stronger": True}}
    )
    assert cfg["rotate_full_weakest_when_stronger"] is True


def test_parse_rebalance_free_capital_cfg_clamps_fraction() -> None:
    cfg = parse_rebalance_free_capital_cfg(
        {"rebalance_free_capital": {"trim_fraction": "not-a-float"}}
    )
    assert cfg["trim_fraction"] == pytest.approx(0.15)


def test_parse_trim_pct_range_midpoint() -> None:
    cfg = parse_rebalance_free_capital_cfg(
        {"rebalance_free_capital": {"trim_pct_min": 10, "trim_pct_max": 20}}
    )
    assert cfg["trim_fraction"] == pytest.approx(0.15)
    assert cfg["trim_pct_lo"] == pytest.approx(10.0)
    assert cfg["trim_pct_hi"] == pytest.approx(20.0)
    assert cfg["trim_band_uniform"] is False


def test_parse_trim_pct_20_30_midpoint() -> None:
    cfg = parse_rebalance_free_capital_cfg(
        {"rebalance_free_capital": {"trim_pct_min": 20, "trim_pct_max": 30, "trim_band_uniform": True}}
    )
    assert cfg["trim_fraction"] == pytest.approx(0.25)
    assert cfg["trim_pct_lo"] == pytest.approx(20.0)
    assert cfg["trim_pct_hi"] == pytest.approx(30.0)
    assert cfg["trim_band_uniform"] is True


def test_rebalance_trim_fraction_uniform_uses_rng() -> None:
    rfc = {
        "trim_fraction": 0.25,
        "trim_band_uniform": True,
        "trim_pct_lo": 20.0,
        "trim_pct_hi": 30.0,
    }
    rng = random.Random(42)
    f = rebalance_trim_fraction_for_attempt(rfc, rng=rng)
    assert 0.20 <= f <= 0.30
    f2 = rebalance_trim_fraction_for_attempt(rfc, rng=rng)
    assert 0.20 <= f2 <= 0.30


def test_rebalance_trim_fraction_midpoint_when_uniform_off() -> None:
    rfc = parse_rebalance_free_capital_cfg(
        {"rebalance_free_capital": {"trim_pct_min": 20, "trim_pct_max": 30}}
    )
    assert rebalance_trim_fraction_for_attempt(rfc) == pytest.approx(0.25)


def test_parse_gross_liquidation_block() -> None:
    cfg = parse_rebalance_free_capital_cfg(
        {
            "rebalance_free_capital": {
                "gross_liquidation": {
                    "enabled": True,
                    "target_gross_pct": 92.0,
                    "passes": 1,
                }
            }
        }
    )
    assert cfg["gross_liquidation"]["enabled"] is True
    assert cfg["gross_liquidation"]["target_gross_pct"] == pytest.approx(92.0)
    assert cfg["gross_liquidation"]["passes"] == 1


def test_gross_liquidation_trim_shares_107_to_95_two_passes() -> None:
    # 12% of 100k = 12k gap; /2 passes = 6k; / $200 = 30 sh
    sh = gross_liquidation_trim_shares(
        account_equity=100_000.0,
        current_gross_pct=107.0,
        target_gross_pct=95.0,
        passes=2,
        bqty=200,
        mid_price=200.0,
    )
    assert sh == 30
    sh_full = gross_liquidation_trim_shares(
        account_equity=100_000.0,
        current_gross_pct=107.0,
        target_gross_pct=95.0,
        passes=2,
        bqty=5,
        mid_price=200.0,
    )
    assert sh_full == 5


def test_gross_liquidation_trim_shares_invalid_mid() -> None:
    assert (
        gross_liquidation_trim_shares(
            account_equity=100_000.0,
            current_gross_pct=110.0,
            target_gross_pct=90.0,
            passes=2,
            bqty=100,
            mid_price=0.0,
        )
        is None
    )


def test_plan_gross_liquidation_overrides_fraction() -> None:
    now = datetime(2025, 1, 2, 16, 0, tzinfo=timezone.utc)
    old = "2025-01-01T10:00:00+00:00"
    tracked = {
        "ZZZ": {"signal_strength": 0.9, "entry_time": old},
    }
    pos = [{"symbol": "ZZZ", "qty": 200, "market_value": 40_000.0}]
    plan = plan_weakest_trim_for_free_capital(
        tracked=tracked,
        eligible_symbols=["ZZZ"],
        positions=pos,
        rep_sub={"min_hold_minutes": 0.0},
        now_dt=now,
        incoming_sym_upper="",
        trim_fraction=0.1,
        exclude_incoming_symbol=False,
        trim_target="largest_exposure_notional",
        gross_liquidation={
            "enabled": True,
            "account_equity": 100_000.0,
            "current_gross_pct": 107.0,
            "target_gross_pct": 95.0,
            "passes": 2,
            "get_mid": lambda s: 200.0,
        },
    )
    assert plan is not None
    sym, qty = plan
    assert sym == "ZZZ"
    assert qty == 30


def test_trim_fraction_by_gross_leverage_tiers() -> None:
    assert trim_fraction_by_gross_leverage(50.0) == pytest.approx(0.25)
    assert trim_fraction_by_gross_leverage(100.0) == pytest.approx(0.25)
    assert trim_fraction_by_gross_leverage(100.01) == pytest.approx(0.4)
    assert trim_fraction_by_gross_leverage(105.0) == pytest.approx(0.4)
    assert trim_fraction_by_gross_leverage(105.01) == pytest.approx(0.6)


def test_emergency_bulk_trim_notional_usd_tiers() -> None:
    eq = 100_000.0
    assert emergency_bulk_trim_notional_usd(eq, 1.0) == pytest.approx(5000.0)  # 5%
    assert emergency_bulk_trim_notional_usd(eq, 1.2 + 0.0001) == pytest.approx(10_000.0)  # 10%
    assert emergency_bulk_trim_notional_usd(eq, 1.5 + 0.0001) == pytest.approx(20_000.0)  # 20%
    assert emergency_bulk_trim_notional_usd(eq, 1.2) == pytest.approx(5000.0)  # at boundary, else 5%
    # Strictly > 1.5 for 20%; at 1.5 use 10% band (1.5 > 1.2, not > 1.5)
    assert emergency_bulk_trim_notional_usd(eq, 1.5) == pytest.approx(10_000.0)


def test_emergency_bulk_trim_notional_usd_floors_at_one_dollar() -> None:
    assert emergency_bulk_trim_notional_usd(100.0, 1.0) == pytest.approx(5.0)
    assert emergency_bulk_trim_notional_usd(0.0, 2.0) == pytest.approx(0.0)


def test_parse_trim_pct_range_swaps_when_reversed() -> None:
    cfg = parse_rebalance_free_capital_cfg(
        {"rebalance_free_capital": {"trim_pct_min": 20, "trim_pct_max": 10}}
    )
    assert cfg["trim_fraction"] == pytest.approx(0.15)


def test_trim_qty_for_fraction() -> None:
    assert trim_qty_for_fraction(1, 0.2) is None
    assert trim_qty_for_fraction(2, 0.2) == 1
    assert trim_qty_for_fraction(5, 0.2) == 1
    assert trim_qty_for_fraction(10, 0.2) == 2
    assert trim_qty_for_fraction(100, 0.2) == 20
    assert trim_qty_for_fraction(10, 0.0) is None
    assert trim_qty_for_fraction(10, 0.95) == 9


def test_broker_long_shares_for_symbol_skips_options() -> None:
    pos = [
        {"symbol": "AAPL", "qty": 5},
        {"symbol": "SPY250119C00450000", "qty": 1},
    ]
    assert broker_long_shares_for_symbol(pos, "AAPL") == 5
    assert broker_long_shares_for_symbol(pos, "SPY250119C00450000") == 0


def test_plan_weakest_trim_respects_min_hold() -> None:
    now = datetime(2026, 4, 14, 16, 0, tzinfo=timezone.utc)
    recent = "2026-04-14T15:59:00+00:00"
    tracked = {
        "AAA": {"qty": 10, "signal_strength": 0.9, "entry_time": recent},
        "BBB": {"qty": 10, "signal_strength": 0.5, "entry_time": recent},
    }
    elig = ["AAA", "BBB"]
    pos = [{"symbol": "AAA", "qty": 10}, {"symbol": "BBB", "qty": 10}]
    rep = {"min_hold_minutes": 15.0}
    plan = plan_weakest_trim_for_free_capital(
        tracked=tracked,
        eligible_symbols=elig,
        positions=pos,
        rep_sub=rep,
        now_dt=now,
        incoming_sym_upper="ZZZ",
        trim_fraction=0.2,
        exclude_incoming_symbol=True,
    )
    assert plan is None


def test_plan_weakest_trim_require_deterioration_blocks_without_verifiable_engine() -> None:
    now = datetime(2025, 1, 2, 16, 0, tzinfo=timezone.utc)
    old = "2025-01-01T10:00:00+00:00"
    tracked = {
        "AAA": {"qty": 10, "signal_strength": 0.9, "entry_time": old},
        "BBB": {"qty": 10, "signal_strength": 0.3, "entry_time": old},
    }
    elig = ["AAA", "BBB"]
    pos = [{"symbol": "AAA", "qty": 10}, {"symbol": "BBB", "qty": 10}]
    plan = plan_weakest_trim_for_free_capital(
        tracked=tracked,
        eligible_symbols=elig,
        positions=pos,
        rep_sub={"min_hold_minutes": 0.0},
        now_dt=now,
        incoming_sym_upper="",
        trim_fraction=0.2,
        exclude_incoming_symbol=False,
        broker=type("B", (), {"get_bars": lambda s, **k: None})(),
        engine=None,
        require_signal_deterioration=True,
    )
    assert plan is None


def test_position_has_signal_deterioration_false_without_bars() -> None:
    assert position_has_signal_deterioration("X", {}, [], None, None) is False


def test_parse_bulk_trim() -> None:
    cfg = parse_rebalance_free_capital_cfg(
        {
            "rebalance_free_capital": {
                "bulk_trim": {
                    "enabled": True,
                    "notional_per_symbol_usd": 2_200,
                    "max_symbols_per_pass": 5,
                    "buy_cooldown_minutes": 45,
                }
            }
        }
    )
    assert cfg["bulk_trim"]["enabled"] is True
    assert cfg["bulk_trim"]["notional_per_symbol_usd"] == pytest.approx(2200.0)
    assert cfg["bulk_trim"]["max_symbols_per_pass"] == 5
    assert cfg["bulk_trim"]["buy_cooldown_minutes"] == pytest.approx(45.0)


def test_long_stock_symbols_by_market_value_desc() -> None:
    elig = ["A", "B", "C"]
    pos = [
        {"symbol": "A", "qty": 1, "market_value": 9_000.0},
        {"symbol": "B", "qty": 1, "market_value": 18_000.0},
        {"symbol": "C", "qty": 2, "market_value": 12_000.0},
    ]
    assert long_stock_symbols_by_market_value_desc(elig, pos) == ["B", "C", "A"]


def test_get_top_n_positions() -> None:
    elig = ["A", "B", "C"]
    pos = [
        {"symbol": "A", "qty": 1, "market_value": 9_000.0},
        {"symbol": "B", "qty": 1, "market_value": 18_000.0},
        {"symbol": "C", "qty": 2, "market_value": 12_000.0},
    ]
    assert get_top_n_positions(elig, pos, n=2) == frozenset({"B", "C"})
    assert get_top_n_positions(elig, pos, n=0) == frozenset()


def test_plan_bulk_notional_respects_top_position_protection() -> None:
    """Largest notional (B) uses ``intensity_mult`` on the requested tranche; others do not."""
    now = datetime(2025, 1, 2, 16, 0, tzinfo=timezone.utc)
    old = "2025-01-01T10:00:00+00:00"
    tracked = {s: {"signal_strength": 0.5, "entry_time": old} for s in ("B", "C", "A")}
    pos = [
        {"symbol": "A", "qty": 10, "market_value": 1_200.0},
        {"symbol": "B", "qty": 10, "market_value": 3_000.0},
        {"symbol": "C", "qty": 10, "market_value": 2_000.0},
    ]
    out = plan_bulk_notional_trims_for_free_capital(
        eligible_symbols=["A", "B", "C"],
        positions=pos,
        tracked=tracked,
        rep_sub={"min_hold_minutes": 0.0},
        now_dt=now,
        incoming_sym_upper="",
        notional_per_symbol_usd=1500.0,
        max_symbols=2,
        exclude_incoming_symbol=False,
        require_signal_deterioration=False,
        top_positions=frozenset({"B"}),
        top_position_sell_intensity=0.5,
    )
    assert out == [("B", 750.0), ("C", 1500.0)]


def test_plan_bulk_notional_trims_largest_and_caps() -> None:
    now = datetime(2025, 1, 2, 16, 0, tzinfo=timezone.utc)
    old = "2025-01-01T10:00:00+00:00"
    tracked = {s: {"signal_strength": 0.5, "entry_time": old} for s in ("B", "C", "A")}
    pos = [
        {"symbol": "A", "qty": 10, "market_value": 1_200.0},
        {"symbol": "B", "qty": 10, "market_value": 3_000.0},
        {"symbol": "C", "qty": 10, "market_value": 2_000.0},
    ]
    plans = plan_bulk_notional_trims_for_free_capital(
        eligible_symbols=["A", "B", "C"],
        positions=pos,
        tracked=tracked,
        rep_sub={"min_hold_minutes": 0.0},
        now_dt=now,
        incoming_sym_upper="",
        notional_per_symbol_usd=1500.0,
        max_symbols=2,
        exclude_incoming_symbol=False,
        require_signal_deterioration=False,
    )
    assert plans == [("B", 1500.0), ("C", 1500.0)]
    cap = plan_bulk_notional_trims_for_free_capital(
        eligible_symbols=["A", "B", "C"],
        positions=pos,
        tracked=tracked,
        rep_sub={"min_hold_minutes": 0.0},
        now_dt=now,
        incoming_sym_upper="",
        notional_per_symbol_usd=1_500.0,
        max_symbols=3,
        exclude_incoming_symbol=False,
    )
    assert cap[0] == ("B", 1500.0)
    assert cap[1] == ("C", 1500.0)
    assert cap[2] == ("A", 1200.0)


def test_parse_rebalance_largest_exposure_keys() -> None:
    cfg = parse_rebalance_free_capital_cfg(
        {"rebalance_free_capital": {"trim_target": "largest", "largest_exposure_max_try": 5}}
    )
    assert cfg["trim_target"] == "largest_exposure_notional"
    assert cfg["largest_exposure_max_try"] == 5


def test_broker_position_market_value_usd_uses_abs() -> None:
    pos = [{"symbol": "X", "qty": 1, "market_value": -123.4}]
    assert broker_position_market_value_usd(pos, "X") == pytest.approx(123.4)


def test_broker_position_unrealized_pnl_pct() -> None:
    pos = [
        {
            "symbol": "A",
            "qty": 10,
            "market_value": 10_000.0,
            "cost_basis": 9_000.0,
            "unrealized_pl": 1_000.0,
        }
    ]
    assert broker_position_unrealized_pnl_pct(pos, "A") == pytest.approx(100.0 * 1_000.0 / 9_000.0)


def test_plan_bulk_skips_first_pass_winners() -> None:
    now = datetime(2025, 1, 2, 16, 0, tzinfo=timezone.utc)
    old = "2025-01-01T10:00:00+00:00"
    tracked = {s: {"signal_strength": 0.5, "entry_time": old} for s in ("B", "A")}
    # B is largest; B is a winner (unrealized_pl / cost > 3%%)
    pos = [
        {
            "symbol": "A",
            "qty": 10,
            "market_value": 1_200.0,
            "cost_basis": 1_200.0,
            "unrealized_pl": 0.0,
        },
        {
            "symbol": "B",
            "qty": 10,
            "market_value": 3_000.0,
            "cost_basis": 2_000.0,
            "unrealized_pl": 1_000.0,
        },
    ]
    out = plan_bulk_notional_trims_for_free_capital(
        eligible_symbols=["A", "B"],
        positions=pos,
        tracked=tracked,
        rep_sub={"min_hold_minutes": 0.0},
        now_dt=now,
        incoming_sym_upper="",
        notional_per_symbol_usd=500.0,
        max_symbols=2,
        exclude_incoming_symbol=False,
        is_first_rfc_pass=True,
        skip_if_unrealized_pnl_pct_above=3.0,
    )
    # B: 1000/2000*100 = 50%% > 3%% — skip; only A
    assert out == [("A", 500.0)]
    out2 = plan_bulk_notional_trims_for_free_capital(
        eligible_symbols=["A", "B"],
        positions=pos,
        tracked=tracked,
        rep_sub={"min_hold_minutes": 0.0},
        now_dt=now,
        incoming_sym_upper="",
        notional_per_symbol_usd=500.0,
        max_symbols=2,
        exclude_incoming_symbol=False,
        is_first_rfc_pass=False,
        skip_if_unrealized_pnl_pct_above=3.0,
    )
    assert out2[0] == ("B", 500.0)


def test_rfc_uses_largest_exposure_notional() -> None:
    assert rfc_uses_largest_exposure_notional_trim("Largest")
    assert rfc_uses_largest_exposure_notional_trim("largest_exposure_notional")
    assert not rfc_uses_largest_exposure_notional_trim("other_mode")
    assert not rfc_uses_largest_exposure_notional_trim("weakest")


def test_trim_candidate_symbols_largest_exposure_notional() -> None:
    elig = ["A", "B", "C"]
    pos = [
        {"symbol": "A", "qty": 10, "market_value": 10.0},
        {"symbol": "B", "qty": 10, "market_value": 500.0},
        {"symbol": "C", "qty": 10, "market_value": 200.0},
    ]
    assert trim_candidate_symbols_largest_exposure_notional(elig, pos, max_candidates=2) == [
        "B",
        "C",
    ]


def test_plan_largest_exposure_notional_trims_largest() -> None:
    now = datetime(2025, 1, 2, 16, 0, tzinfo=timezone.utc)
    old = "2025-01-01T10:00:00+00:00"
    tracked = {
        "AAA": {"signal_strength": 0.1, "entry_time": old},
        "BBB": {"signal_strength": 0.95, "entry_time": old},
        "CCC": {"signal_strength": 0.5, "entry_time": old},
    }
    pos = [
        {"symbol": "AAA", "qty": 10, "market_value": 100.0},
        {"symbol": "BBB", "qty": 10, "market_value": 5000.0},
        {"symbol": "CCC", "qty": 10, "market_value": 200.0},
    ]
    plan = plan_weakest_trim_for_free_capital(
        tracked=tracked,
        eligible_symbols=["AAA", "BBB", "CCC"],
        positions=pos,
        rep_sub={"min_hold_minutes": 0.0},
        now_dt=now,
        incoming_sym_upper="ZZZ",
        trim_fraction=0.2,
        exclude_incoming_symbol=True,
        trim_target="largest_exposure_notional",
    )
    assert plan is not None
    assert plan[0] == "BBB"
    assert plan[1] == 2


def test_plan_largest_exposure_tries_fallthrough_after_min_hold_block() -> None:
    now = datetime(2026, 4, 14, 16, 0, tzinfo=timezone.utc)
    recent = "2026-04-14T15:59:00+00:00"
    old = "2025-01-01T10:00:00+00:00"
    tracked = {
        "AAA": {"signal_strength": 0.9, "entry_time": old},
        "BBB": {"signal_strength": 0.9, "entry_time": recent},
    }
    pos = [
        {"symbol": "AAA", "qty": 10, "market_value": 200.0},
        {"symbol": "BBB", "qty": 10, "market_value": 500.0},
    ]
    plan = plan_weakest_trim_for_free_capital(
        tracked=tracked,
        eligible_symbols=["AAA", "BBB"],
        positions=pos,
        rep_sub={"min_hold_minutes": 15.0},
        now_dt=now,
        incoming_sym_upper="ZZZ",
        trim_fraction=0.2,
        exclude_incoming_symbol=True,
        trim_target="largest_exposure_notional",
        largest_exposure_max_try=3,
    )
    assert plan is not None
    assert plan[0] == "AAA"
    assert plan[1] == 2


def test_plan_weakest_trim_excludes_incoming_and_picks_weakest() -> None:
    now = datetime(2025, 1, 2, 16, 0, tzinfo=timezone.utc)
    old = "2025-01-01T10:00:00+00:00"
    tracked = {
        "AAA": {"qty": 10, "signal_strength": 0.9, "entry_time": old},
        "BBB": {"qty": 10, "signal_strength": 0.3, "entry_time": old},
        "CCC": {"qty": 10, "signal_strength": 0.1, "entry_time": old},
    }
    elig = ["AAA", "BBB", "CCC"]
    pos = [{"symbol": "AAA", "qty": 10}, {"symbol": "BBB", "qty": 10}, {"symbol": "CCC", "qty": 10}]
    rep = {"min_hold_minutes": 0.0}
    plan = plan_weakest_trim_for_free_capital(
        tracked=tracked,
        eligible_symbols=elig,
        positions=pos,
        rep_sub=rep,
        now_dt=now,
        incoming_sym_upper="BBB",
        trim_fraction=0.2,
        exclude_incoming_symbol=True,
    )
    assert plan is not None
    sym, qty = plan
    assert sym == "CCC"
    assert qty == 2


def test_plan_full_exit_weakest_when_stronger() -> None:
    now = datetime(2025, 1, 2, 16, 0, tzinfo=timezone.utc)
    old = "2025-01-01T10:00:00+00:00"
    tracked = {
        "AAA": {"qty": 10, "signal_strength": 0.9, "entry_time": old},
        "BBB": {"qty": 7, "signal_strength": 0.2, "entry_time": old},
    }
    pos = [{"symbol": "AAA", "qty": 10}, {"symbol": "BBB", "qty": 7}]
    rep = {"min_hold_minutes": 0.0}
    plan = plan_full_exit_weakest_when_stronger(
        tracked=tracked,
        eligible_symbols=["AAA", "BBB"],
        positions=pos,
        rep_sub=rep,
        now_dt=now,
        incoming_sym_upper="ZZZ",
        incoming_signal_strength=0.5,
        exclude_incoming_symbol=True,
    )
    assert plan == ("BBB", 7)
    assert (
        plan_full_exit_weakest_when_stronger(
            tracked=tracked,
            eligible_symbols=["AAA", "BBB"],
            positions=pos,
            rep_sub=rep,
            now_dt=now,
            incoming_sym_upper="ZZZ",
            incoming_signal_strength=0.15,
            exclude_incoming_symbol=True,
        )
        is None
    )


def test_plan_weakest_trim_none_when_only_incoming_eligible() -> None:
    now = datetime(2025, 1, 2, 16, 0, tzinfo=timezone.utc)
    old = "2025-01-01T10:00:00+00:00"
    tracked = {"ONLY": {"qty": 5, "signal_strength": 0.2, "entry_time": old}}
    pos = [{"symbol": "ONLY", "qty": 5}]
    plan = plan_weakest_trim_for_free_capital(
        tracked=tracked,
        eligible_symbols=["ONLY"],
        positions=pos,
        rep_sub={"min_hold_minutes": 0.0},
        now_dt=now,
        incoming_sym_upper="ONLY",
        trim_fraction=0.2,
        exclude_incoming_symbol=True,
    )
    assert plan is None


def test_parse_two_phase_gross_cap_unwind() -> None:
    c = parse_rebalance_free_capital_cfg(
        {
            "rebalance_free_capital": {
                "two_phase_gross_cap_unwind": {
                    "enabled": True,
                    "phase1_weakest_trim_fraction": 0.4,
                    "proportional_max_submits": 8,
                }
            }
        }
    )
    t = c["two_phase_gross_cap_unwind"]
    assert t["enabled"] is True
    assert t["phase1_weakest_trim_fraction"] == pytest.approx(0.4)
    assert t["proportional_max_submits"] == 8


def test_plan_proportional_gross_delever_weakest_first_waterfill() -> None:
    """Gap is covered from lowest ``replacement_hold_strength`` first (default: entry strength)."""
    now = datetime(2025, 1, 2, 16, 0, tzinfo=timezone.utc)
    old = "2025-01-01T10:00:00+00:00"
    tracked = {
        "A": {"signal_strength": 0.9, "entry_time": old},
        "B": {"signal_strength": 0.5, "entry_time": old},
        "C": {"signal_strength": 0.1, "entry_time": old},
    }
    pos = [
        {"symbol": "A", "qty": 30, "market_value": 3000.0},
        {"symbol": "B", "qty": 20, "market_value": 2000.0},
        {"symbol": "C", "qty": 10, "market_value": 1000.0},
    ]
    pls = plan_proportional_gross_delever_notional_trims(
        eligible_symbols=["A", "B", "C"],
        positions=pos,
        tracked=tracked,
        rep_sub={"min_hold_minutes": 0.0},
        now_dt=now,
        incoming_sym_upper="",
        exclude_incoming_symbol=False,
        current_gross_pct=110.0,
        target_gross_pct=95.0,
        account_equity=10_000.0,
        max_submits=10,
        get_bars=None,
        engine=None,
    )
    # Weakest C first: 1000, then B: 500 of 1500 gap
    assert pls == [("C", 1000.0), ("B", 500.0)]


def test_plan_full_exit_weakest_for_gross_delever() -> None:
    now = datetime(2025, 1, 2, 16, 0, tzinfo=timezone.utc)
    old = "2025-01-01T10:00:00+00:00"
    tracked = {
        "AAA": {"qty": 10, "signal_strength": 0.9, "entry_time": old},
        "BBB": {"qty": 7, "signal_strength": 0.1, "entry_time": old},
    }
    pos = [{"symbol": "AAA", "qty": 10}, {"symbol": "BBB", "qty": 7}]
    p = plan_full_exit_weakest_for_gross_delever(
        tracked=tracked,
        eligible_symbols=["AAA", "BBB"],
        positions=pos,
        rep_sub={"min_hold_minutes": 0.0},
        now_dt=now,
        incoming_sym_upper="",
        exclude_incoming_symbol=True,
    )
    assert p == ("BBB", 7)


def test_plan_weakest_gross_unwind_phase1_matches_weakest_50() -> None:
    now = datetime(2025, 1, 2, 16, 0, tzinfo=timezone.utc)
    old = "2025-01-01T10:00:00+00:00"
    tracked = {
        "AAA": {"qty": 10, "signal_strength": 0.9, "entry_time": old},
        "BBB": {"qty": 10, "signal_strength": 0.2, "entry_time": old},
    }
    pos = [
        {"symbol": "AAA", "qty": 10, "market_value": 100.0},
        {"symbol": "BBB", "qty": 10, "market_value": 100.0},
    ]
    p = plan_weakest_gross_unwind_phase1(
        tracked=tracked,
        eligible_symbols=["AAA", "BBB"],
        positions=pos,
        rep_sub={"min_hold_minutes": 0.0},
        now_dt=now,
        incoming_sym_upper="",
        phase1_weakest_trim_fraction=0.5,
        exclude_incoming_symbol=True,
    )
    assert p is not None
    assert p[0] == "BBB"
    assert p[1] == 5


def test_symbols_ordered_bulk_trim_priority_tiebreak_pnl() -> None:
    pos = [
        {
            "symbol": "BIG",
            "qty": 10,
            "market_value": 5000.0,
            "cost_basis": 5000.0,
            "unrealized_pl": 0.0,
        },
        {
            "symbol": "BIG2",
            "qty": 10,
            "market_value": 5000.0,
            "cost_basis": 6000.0,
            "unrealized_pl": -1000.0,
        },
        {"symbol": "SMALL", "qty": 10, "market_value": 1000.0},
    ]
    o = symbols_ordered_for_bulk_trim_priority(
        ["SMALL", "BIG", "BIG2"],
        pos,
        ["highest_weight", "weakest_pnl"],
    )
    # Same notional bucket: tie-break by weakest PnL % (BIG2 loses vs BIG).
    assert o == ["BIG2", "BIG", "SMALL"]


def test_plan_emergency_deleverage_portfolio_pct_trims() -> None:
    now = datetime(2025, 1, 2, 16, 0, tzinfo=timezone.utc)
    old = "2025-01-01T10:00:00+00:00"
    tracked = {
        "A": {"signal_strength": 0.5, "entry_time": old},
        "B": {"signal_strength": 0.5, "entry_time": old},
    }
    pos = [
        {"symbol": "A", "qty": 10, "market_value": 7000.0},
        {"symbol": "B", "qty": 10, "market_value": 3000.0},
    ]
    out = plan_emergency_deleverage_portfolio_pct_trims(
        eligible_symbols=["A", "B"],
        positions=pos,
        tracked=tracked,
        rep_sub={"min_hold_minutes": 0.0},
        now_dt=now,
        incoming_sym_upper="",
        portfolio_trim_pct=0.30,
        max_symbols=5,
        exclude_incoming_symbol=False,
        bulk_trim_priority=["largest_exposure"],
        require_signal_deterioration=False,
    )
    total_mv = 10_000.0
    target = total_mv * 0.30
    got = sum(x[1] for x in out)
    assert got == pytest.approx(target)
    # Greedy on A first fills entire 30% budget in one leg.
    assert len(out) == 1
    assert out[0][0] == "A"
    assert out[0][1] == pytest.approx(target)
