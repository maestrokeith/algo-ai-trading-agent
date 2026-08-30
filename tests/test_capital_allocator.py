"""Tests for :class:`src.capital_allocator.CapitalAllocator`."""

from __future__ import annotations

import pytest

from src.capital_allocator import (
    CapitalAllocator,
    apply_allocator_defensive_drift_scores,
    allocator_candidate_book_score,
    allocator_bullish_regime_for_defensive_drift,
    clip_allocator_buy_notionals_to_single_order_caps,
    clip_buy_actions_to_gross_headroom_dollars,
    collect_symbol_cap_tier_hard_fractions,
    consolidate_allocator_actions_net_by_symbol,
    effective_capital_allocator_symbol_cap_frac,
    effective_capital_allocator_symbol_cap_soft_hard,
    effective_capital_allocator_symbol_caps_by_symbol,
    gross_book_near_effective_max_for_net_reduction,
    parse_capital_allocator_cfg,
    reorder_allocator_candidates_diversification,
    trim_allocator_actions_for_max_buy_to_sell_ratio,
    trim_allocator_actions_for_net_sell_gte_buy,
    parse_defensive_drift_cfg,
    symbol_caps_define_tier_buckets,
)
from src.capital_allocator_loop import (
    build_allocator_candidates,
    dedupe_cap_alloc_rows,
    rank_allocator_candidates,
)


def test_allocate_caps_buy_to_total_gross_headroom() -> None:
    """``allocate = min(requested, available_capacity)`` for net new buys: headroom = max_gross - current_gross."""
    a = CapitalAllocator(
        max_positions=10, symbol_cap=0.5, min_trade_size=500.0, min_realloc_leg=200.0
    )
    out = a.allocate(
        portfolio=[],
        candidates=[{"symbol": "NVDA", "score": 0.9}],
        equity=100_000.0,
        cash=2_000.0,
        max_total_gross_dollars=100_000.0,
        current_gross_dollars=99_600.0,
    )
    # Only $400 of gross headroom: min(500, 400) = 400 (≥ min_realloc_leg 200)
    assert out == [{"action": "buy", "symbol": "NVDA", "notional": 400.0}]


def test_clip_buy_actions_to_gross_headroom_dollars_equal_split() -> None:
    act = [
        {"action": "buy", "symbol": "A", "notional": 1000.0},
        {"action": "buy", "symbol": "B", "notional": 1000.0},
    ]
    out = clip_buy_actions_to_gross_headroom_dollars(
        act, gross_headroom_dollars=1500.0, min_realloc_leg=1.0
    )
    assert [x.get("notional") for x in out] == [750.0, 750.0]


def test_allocate_buy_when_cash_and_slot() -> None:
    a = CapitalAllocator(max_positions=10, symbol_cap=0.25, min_trade_size=500.0)
    out = a.allocate(
        portfolio=[],
        candidates=[{"symbol": "NVDA", "score": 0.9}],
        equity=100_000.0,
        cash=2_000.0,
    )
    assert out == [{"action": "buy", "symbol": "NVDA", "notional": 500.0}]


def test_allocate_skips_when_at_symbol_cap() -> None:
    a = CapitalAllocator(max_positions=10, symbol_cap=0.25, min_trade_size=500.0)
    out = a.allocate(
        portfolio=[{"symbol": "QQQ", "value": 26_000.0, "score": 0.5}],
        candidates=[{"symbol": "QQQ", "score": 0.99}],
        equity=100_000.0,
        cash=5_000.0,
    )
    assert out == []


def test_allocate_soft_cap_reduced_notional_when_at_cap() -> None:
    """Soft-cap penalty tranche 250 is below default ``min_realloc_leg`` 300 — no order."""
    a = CapitalAllocator(
        max_positions=10,
        symbol_cap=0.25,
        min_trade_size=500.0,
        soft_cap_mode=True,
        cap_penalty_multiplier=0.5,
    )
    out = a.allocate(
        portfolio=[{"symbol": "QQQ", "value": 26_000.0, "score": 0.5}],
        candidates=[{"symbol": "QQQ", "score": 0.99}],
        equity=100_000.0,
        cash=5_000.0,
    )
    assert out == []


def test_allocate_soft_cap_penalty_allowed_when_leg_ge_min() -> None:
    a = CapitalAllocator(
        max_positions=10,
        symbol_cap=0.25,
        min_trade_size=500.0,
        min_realloc_leg=200.0,
        soft_cap_mode=True,
        cap_penalty_multiplier=0.5,
    )
    out = a.allocate(
        portfolio=[{"symbol": "QQQ", "value": 26_000.0, "score": 0.5}],
        candidates=[{"symbol": "QQQ", "score": 0.99}],
        equity=100_000.0,
        cash=5_000.0,
    )
    assert out == [{"action": "buy", "symbol": "QQQ", "notional": 250.0}]


def test_allocate_soft_cap_clamps_to_headroom_under_full_min() -> None:
    a = CapitalAllocator(
        max_positions=10,
        symbol_cap=0.25,
        min_trade_size=1_000.0,
        soft_cap_mode=True,
        cap_penalty_multiplier=0.5,
    )
    out = a.allocate(
        portfolio=[{"symbol": "QQQ", "value": 24_200.0, "score": 0.5}],
        candidates=[{"symbol": "QQQ", "score": 0.99}],
        equity=100_000.0,
        cash=5_000.0,
    )
    assert out == [{"action": "buy", "symbol": "QQQ", "notional": 500.0}]


def test_allocate_add_on_when_below_cap() -> None:
    a = CapitalAllocator(max_positions=10, symbol_cap=0.25, min_trade_size=500.0)
    out = a.allocate(
        portfolio=[{"symbol": "QQQ", "value": 20_000.0, "score": 0.5}],
        candidates=[{"symbol": "QQQ", "score": 0.99}],
        equity=100_000.0,
        cash=5_000.0,
    )
    assert out == [{"action": "buy", "symbol": "QQQ", "notional": 500.0}]


def test_allocate_clamps_add_to_hard_headroom_without_soft_mode() -> None:
    """
    ``allocation = min(requested, max_allowed - current)``: without soft cap, do not add past hard.
    100k * 0.25 = 25k; 24,900 + 500 would exceed; only 100 fits.
    """
    a = CapitalAllocator(
        max_positions=10,
        symbol_cap=0.25,
        min_trade_size=500.0,
        min_realloc_leg=1.0,
    )
    out = a.allocate(
        portfolio=[{"symbol": "QQQ", "value": 24_900.0, "score": 0.5}],
        candidates=[{"symbol": "QQQ", "score": 0.99}],
        equity=100_000.0,
        cash=5_000.0,
    )
    assert out == [{"action": "buy", "symbol": "QQQ", "notional": 100.0}]


def test_allocate_rotates_when_stronger_and_low_cash() -> None:
    a = CapitalAllocator(
        max_positions=10,
        symbol_cap=0.25,
        min_trade_size=500.0,
        rotate_trim_fraction=0.30,
    )
    out = a.allocate(
        portfolio=[{"symbol": "AAA", "value": 10_000.0, "score": 0.1}],
        candidates=[{"symbol": "BBB", "score": 0.9}],
        equity=100_000.0,
        cash=0.0,
    )
    assert len(out) == 2
    assert out[0] == {"action": "sell", "symbol": "AAA", "notional": 500.0}
    assert out[1] == {"action": "buy", "symbol": "BBB", "notional": 500.0}


def test_allocate_rotates_full_weakest_line_when_trim_below_min_leg() -> None:
    """If base trim is below ``min_realloc_leg``, sell up to a full line / bump so swap still runs."""
    a = CapitalAllocator(
        max_positions=2,
        symbol_cap=0.25,
        min_trade_size=500.0,
        min_realloc_leg=300.0,
        rotate_trim_fraction=0.30,
    )
    out = a.allocate(
        portfolio=[{"symbol": "AAA", "value": 200.0, "score": 0.1}],
        candidates=[{"symbol": "BBB", "score": 0.9}],
        equity=100_000.0,
        cash=0.0,
    )
    assert len(out) == 2
    assert out[0] == {"action": "sell", "symbol": "AAA", "notional": 200.0}
    assert out[1] == {"action": "buy", "symbol": "BBB", "notional": 200.0}


def test_allocate_rotation_uses_strength_eff_vs_book_not_composite() -> None:
    """``rotate_capital`` compares :func:`allocator_candidate_book_score` to weak line (tracker scale)."""
    a = CapitalAllocator(
        max_positions=2,
        symbol_cap=0.25,
        min_trade_size=100.0,
        min_realloc_leg=50.0,
        rotate_trim_fraction=0.30,
    )
    out = a.allocate(
        portfolio=[{"symbol": "OLD", "value": 1_000.0, "score": 0.4}],
        candidates=[{"symbol": "NEW", "score": 9.0, "strength_eff": 0.35}],
        equity=100_000.0,
        cash=0.0,
    )
    # Composite rank is huge but strength 0.35 < 0.4 — no replace
    assert out == []
    out2 = a.allocate(
        portfolio=[{"symbol": "OLD", "value": 1_000.0, "score": 0.4}],
        candidates=[{"symbol": "NEW", "score": 1.0, "strength_eff": 0.5}],
        equity=100_000.0,
        cash=0.0,
    )
    assert len(out2) == 2
    assert out2[0]["action"] == "sell" and out2[1]["action"] == "buy"


def test_allocate_stops_when_candidate_not_stronger_than_weakest() -> None:
    a = CapitalAllocator(max_positions=10, symbol_cap=0.25, min_trade_size=500.0)
    out = a.allocate(
        portfolio=[{"symbol": "AAA", "value": 10_000.0, "score": 0.8}],
        candidates=[{"symbol": "BBB", "score": 0.1}],
        equity=100_000.0,
        cash=0.0,
    )
    assert out == []


def test_parse_capital_allocator_ignore_soft_caps_after_sell_minutes() -> None:
    c = parse_capital_allocator_cfg(
        {
            "capital_allocator": {
                "ignore_soft_caps_after_sell_minutes": 5.0,
            }
        }
    )
    assert c.get("ignore_soft_caps_after_sell_minutes") == pytest.approx(5.0)


def test_allocate_ignore_soft_caps_uses_single_line_at_hard() -> None:
    """Between soft and hard, penalty tranche; with ``ignore_soft_caps`` use full min until hard."""
    a = CapitalAllocator(
        max_positions=10,
        symbol_cap=0.18,
        symbol_cap_soft=0.10,
        min_trade_size=1000.0,
        min_realloc_leg=200.0,
        cap_penalty_multiplier=0.5,
        ignore_soft_caps=True,
    )
    out = a.allocate(
        portfolio=[{"symbol": "SPY", "value": 11_000.0, "score": 0.5}],
        candidates=[{"symbol": "SPY", "score": 0.99}],
        equity=100_000.0,
        cash=5_000.0,
    )
    # Would be 500 with penalty; ignoring soft — legacy at hard, full min
    assert out == [{"action": "buy", "symbol": "SPY", "notional": 1000.0}]


def test_allocator_candidate_book_score_prefers_strength_eff() -> None:
    assert (
        allocator_candidate_book_score(
            {"symbol": "X", "score": 3.0, "strength_eff": 0.7}, rank_score=3.0
        )
        == pytest.approx(0.7)
    )
    assert allocator_candidate_book_score({"symbol": "X", "score": 3.0}, rank_score=3.0) == pytest.approx(3.0)


def test_rotate_capital_true_iff_new_signal_exceeds_weakest_score() -> None:
    a = CapitalAllocator(max_positions=2, symbol_cap=0.5, min_trade_size=100.0)
    assert a.rotate_capital(
        new_signal_score=0.5, weakest_position={"symbol": "X", "score": 0.2}
    )
    assert not a.rotate_capital(
        new_signal_score=0.2, weakest_position={"symbol": "X", "score": 0.2}
    )
    assert not a.rotate_capital(
        new_signal_score=0.1, weakest_position={"symbol": "X", "score": 0.2}
    )


def test_rotate_capital_ratio_requires_clear_edge_over_weakest() -> None:
    """``replacement_strength_ratio: 1.2`` ⇒ require new > weakest * 1.2 (not merely new > weakest)."""
    a = CapitalAllocator(
        max_positions=2,
        symbol_cap=0.5,
        min_trade_size=100.0,
        replacement_strength_ratio=1.2,
    )
    # weakest 0.5 → floor 0.6; 0.55 is better in absolute terms but not enough edge
    assert not a.rotate_capital(
        new_signal_score=0.55, weakest_position={"symbol": "X", "score": 0.5}
    )
    assert a.rotate_capital(
        new_signal_score=0.61, weakest_position={"symbol": "X", "score": 0.5}
    )


def test_allocate_skips_rotation_when_incoming_below_weakest_times_ratio() -> None:
    a = CapitalAllocator(
        max_positions=2,
        symbol_cap=0.25,
        min_trade_size=500.0,
        replacement_strength_ratio=1.2,
    )
    out = a.allocate(
        portfolio=[{"symbol": "AAA", "value": 10_000.0, "score": 0.5}],
        candidates=[{"symbol": "BBB", "score": 0.55}],
        equity=100_000.0,
        cash=0.0,
    )
    assert out == []


def test_allocate_high_conviction_candidate_uses_relaxed_replacement_ratio(caplog) -> None:
    a = CapitalAllocator(
        max_positions=1,
        symbol_cap=0.25,
        min_trade_size=500.0,
        replacement_strength_ratio=1.2,
    )
    with caplog.at_level("INFO", logger="src.portfolio.allocator_planner"):
        out = a.allocate(
            portfolio=[{"symbol": "OLD", "value": 1_000.0, "score": 0.8}],
            candidates=[
                {
                    "symbol": "BBCP",
                    "score": 9.0,
                    "strength_eff": 0.86,
                    "dynamic_candidate": True,
                    "high_conviction_rotation_relaxed": True,
                    "replacement_strength_ratio_override": 1.05,
                }
            ],
            equity=100_000.0,
            cash=0.0,
        )

    assert out == [
        {"action": "sell", "symbol": "OLD", "notional": 300.0},
        {"action": "buy", "symbol": "BBCP", "notional": 300.0},
    ]
    assert "HIGH_CONVICTION_ROTATION_REJECTED" not in caplog.text


def test_allocate_normal_candidate_still_uses_normal_replacement_ratio(caplog) -> None:
    a = CapitalAllocator(
        max_positions=1,
        symbol_cap=0.25,
        min_trade_size=500.0,
        replacement_strength_ratio=1.2,
    )
    with caplog.at_level("INFO", logger="src.portfolio.allocator_planner"):
        out = a.allocate(
            portfolio=[{"symbol": "OLD", "value": 1_000.0, "score": 0.8}],
            candidates=[
                {
                    "symbol": "BBCP",
                    "score": 9.0,
                    "strength_eff": 0.86,
                    "dynamic_candidate": True,
                    "news_score": 9.0,
                    "event_score": 8.0,
                    "catalyst_score": 0.9,
                }
            ],
            equity=100_000.0,
            cash=0.0,
        )

    assert out == []
    assert "HIGH_CONVICTION_ROTATION_REJECTED" not in caplog.text
    assert "ALLOCATOR_NO_ACTION_DETAIL symbol=BBCP reason=rotation_not_stronger" in caplog.text


def test_allocate_high_conviction_candidate_rejected_below_relaxed_ratio(caplog) -> None:
    a = CapitalAllocator(
        max_positions=1,
        symbol_cap=0.25,
        min_trade_size=500.0,
        replacement_strength_ratio=1.2,
    )
    with caplog.at_level("INFO", logger="src.portfolio.allocator_planner"):
        out = a.allocate(
            portfolio=[{"symbol": "OLD", "value": 1_000.0, "score": 0.8}],
            candidates=[
                {
                    "symbol": "BBCP",
                    "score": 9.0,
                    "strength_eff": 0.83,
                    "dynamic_candidate": True,
                    "high_conviction_rotation_relaxed": True,
                    "replacement_strength_ratio_override": 1.05,
                }
            ],
            equity=100_000.0,
            cash=0.0,
        )

    assert out == []
    assert "HIGH_CONVICTION_ROTATION_REJECTED symbol=BBCP reason=rotation_not_stronger" in caplog.text
    assert "new_ratio=1.050000" in caplog.text


def test_replace_weakest_with_stronger_false_disables_score_rotation() -> None:
    book = [
        {"symbol": f"S{i}", "value": 5_000.0, "score": 0.1 + i * 0.01}
        for i in range(10)
    ]
    a = CapitalAllocator(
        max_positions=10,
        symbol_cap=0.25,
        min_trade_size=500.0,
        replace_weakest_with_stronger=False,
    )
    out = a.allocate(
        portfolio=book,
        candidates=[{"symbol": "NEW", "score": 0.99}],
        equity=200_000.0,
        cash=10_000.0,
    )
    assert out == []


def test_allocate_continues_after_ineligible_new_so_later_ties_still_reinvest() -> None:
    """A new name that cannot outrank the weakest *held* line (ties at the threshold) no longer
    short-circuits the whole pass: later names with the same *signal* order can still take **cash** adds.
    (Stable sort keeps equal-``score`` order from *candidates*; list LAME first, then AAPL add.)
    """
    a = CapitalAllocator(
        max_positions=2,
        symbol_cap=0.5,
        min_trade_size=500.0,
    )
    out = a.allocate(
        portfolio=[
            {"symbol": "AAPL", "value": 2_000.0, "score": 0.4},
            {"symbol": "MSFT", "value": 2_000.0, "score": 0.2},
        ],
        candidates=[
            {"symbol": "LAME", "score": 0.2},
            {"symbol": "AAPL", "score": 0.2},
        ],
        equity=100_000.0,
        cash=1_200.0,
    )
    buys = [x for x in out if x["action"] == "buy"]
    assert len(buys) >= 1
    assert buys[0] == {"action": "buy", "symbol": "AAPL", "notional": 500.0}
    # Before the fix, LAME hit ``break`` and the AAPL add row was never reached (0 buy actions).
    assert len(buys) == 1


def test_rebalance_skips_trim_when_incoming_weaker_than_any_line_sell_only() -> None:
    """With ``sell_only_if_needed``, do not raise cash from a hold unless incoming beats that line (rotate_capital)."""
    a = CapitalAllocator(
        max_positions=5,
        symbol_cap=0.5,
        min_trade_size=500.0,
        min_realloc_leg=200.0,
        rebalance_fund_from_weakest=True,
        rebalance_weakest_trim_fraction=0.30,
        sell_only_if_needed=True,
    )
    out = a.allocate(
        portfolio=[
            {"symbol": "WMT", "value": 10_000.0, "score": 0.40},
            {"symbol": "AAPL", "value": 2_000.0, "score": 0.80},
        ],
        candidates=[{"symbol": "BBB", "score": 0.05}],
        equity=100_000.0,
        cash=0.0,
    )
    assert out == []


def test_rebalance_legacy_funds_weakest_when_sell_only_disabled() -> None:
    """``sell_only_if_needed: false`` restores weakest-first funding even when incoming is weaker."""
    a = CapitalAllocator(
        max_positions=5,
        symbol_cap=0.5,
        min_trade_size=500.0,
        min_realloc_leg=200.0,
        rebalance_fund_from_weakest=True,
        rebalance_weakest_trim_fraction=0.30,
        sell_only_if_needed=False,
    )
    out = a.allocate(
        portfolio=[
            {"symbol": "WMT", "value": 10_000.0, "score": 0.40},
            {"symbol": "AAPL", "value": 2_000.0, "score": 0.80},
        ],
        candidates=[{"symbol": "BBB", "score": 0.05}],
        equity=100_000.0,
        cash=0.0,
    )
    assert out[0] == {"action": "sell", "symbol": "WMT", "notional": 3_000.0}
    assert out[-1]["action"] == "buy"


def test_rebalance_sells_30p_weakest_when_cash_tight_funds_add() -> None:
    """``rebalance_fund_from_weakest``: no cash — trim 30% of lowest-score name, then add to the best signal."""
    a = CapitalAllocator(
        max_positions=5,
        symbol_cap=0.5,
        min_trade_size=500.0,
        min_realloc_leg=200.0,
        rebalance_fund_from_weakest=True,
        rebalance_weakest_trim_fraction=0.30,
    )
    out = a.allocate(
        portfolio=[
            {"symbol": "WMT", "value": 10_000.0, "score": 0.10},
            {"symbol": "AAPL", "value": 2_000.0, "score": 0.8},
        ],
        candidates=[{"symbol": "AAPL", "score": 0.8}],
        equity=100_000.0,
        cash=0.0,
    )
    sells = [x for x in out if x["action"] == "sell"]
    assert sells == [{"action": "sell", "symbol": "WMT", "notional": 3_000.0}]
    assert out == [
        {"action": "sell", "symbol": "WMT", "notional": 3_000.0},
        {"action": "buy", "symbol": "AAPL", "notional": 500.0},
    ]


def test_allocate_one_min_trade_per_candidate_reinvests_cash_across_names() -> None:
    """After a sell, deployable cash can fund one tranche per candidate line in one pass (4 names → 4 buys)."""
    a = CapitalAllocator(
        max_positions=10,
        symbol_cap=0.25,
        min_trade_size=500.0,
    )
    book = [
        {"symbol": s, "value": 1_000.0, "score": 0.1 * (i + 1)}
        for i, s in enumerate(["WMT", "AAPL", "MSFT", "GOOGL"])
    ]
    cands = [{"symbol": s, "score": 0.8 - i * 0.01} for i, s in enumerate(["WMT", "AAPL", "MSFT", "GOOGL"])]
    out = a.allocate(
        portfolio=book,
        candidates=cands,
        equity=100_000.0,
        cash=3_000.0,
    )
    buys = [x for x in out if x["action"] == "buy"]
    assert len(buys) == 4
    assert sum(b["notional"] for b in buys) == 2_000.0


def test_allocate_new_name_at_max_positions_does_not_buy_without_rotation() -> None:
    """With 10 slots full and plenty of cash, a new ticker must rotate — not open an 11th line."""
    book = [
        {"symbol": f"S{i}", "value": 5_000.0, "score": 0.1 + i * 0.01}
        for i in range(10)
    ]
    a = CapitalAllocator(max_positions=10, symbol_cap=0.25, min_trade_size=500.0)
    out = a.allocate(
        portfolio=book,
        candidates=[{"symbol": "NEW", "score": 0.99}],
        equity=200_000.0,
        cash=10_000.0,
    )
    assert any(x["action"] == "sell" for x in out)
    assert out[-1] == {"action": "buy", "symbol": "NEW", "notional": 500.0}


def test_allocate_respects_candidate_notional_cap() -> None:
    a = CapitalAllocator(
        max_positions=10,
        symbol_cap=0.25,
        min_trade_size=1_200.0,
        min_realloc_leg=300.0,
    )
    out = a.allocate(
        portfolio=[],
        candidates=[
            {
                "symbol": "AAPL",
                "score": 0.25,
                "candidate_notional_cap": 558.22,
                "route": "core_rebuild",
            }
        ],
        equity=27_911.0,
        cash=10_000.0,
    )
    assert out == [{"action": "buy", "symbol": "AAPL", "notional": 558.22}]


def test_dynamic_catalyst_candidate_uses_requested_notional_base(caplog: pytest.LogCaptureFixture) -> None:
    a = CapitalAllocator(
        max_positions=10,
        symbol_cap=0.15,
        min_trade_size=200.0,
        min_realloc_leg=1_200.0,
        minimum_cash_to_deploy_frac=0.03,
    )

    with caplog.at_level("INFO", logger="src.portfolio.allocator_planner"):
        out = a.allocate(
            portfolio=[],
            candidates=[
                {
                    "symbol": "GOOGL",
                    "score": 2.5,
                    "allocation_bucket": "dynamic",
                    "candidate_notional_requested": 1_312.50,
                    "catalyst_score": 0.40,
                    "event_score": 4.0,
                    "source": "sec_filing",
                }
            ],
            equity=28_000.0,
            cash=6_000.0,
        )

    assert out == [{"action": "buy", "symbol": "GOOGL", "notional": 1_312.50}]
    assert "ALLOCATOR_NO_ACTION_DETAIL symbol=GOOGL" not in caplog.text


def test_allocator_size_trace_logs_before_min_deploy_skip(caplog: pytest.LogCaptureFixture) -> None:
    a = CapitalAllocator(
        max_positions=10,
        symbol_cap=0.25,
        min_trade_size=500.0,
        min_realloc_leg=300.0,
        minimum_cash_to_deploy_frac=0.0347,
    )

    with caplog.at_level("INFO", logger="src.portfolio.allocator_planner"):
        out = a.allocate(
            portfolio=[],
            candidates=[
                {
                    "symbol": "INTC",
                    "score": 9.5,
                    "strength_eff": 9.5,
                    "route": "dynamic_momentum_override",
                    "source": "dynamic_universe",
                    "dynamic_candidate": True,
                    "candidate_notional_requested": 1_312.50,
                    "catalyst_score": 0.90,
                    "event_score": 7.0,
                    "news_score": 8.0,
                }
            ],
            equity=100_000.0,
            cash=69_000.0,
            diagnostics={"dynamic_sleeve_cap": 12_000.0, "core_sleeve_cap": 88_000.0},
        )

    assert out == []
    assert "ALLOCATOR_SIZE_TRACE symbol=INTC" in caplog.text
    assert "route=dynamic_momentum_override" in caplog.text
    assert "source=dynamic_universe" in caplog.text
    assert "dynamic_candidate=true" in caplog.text
    assert "candidate_rank=0" in caplog.text
    assert "raw_target_notional=1312.50" in caplog.text
    assert "after_gross_headroom=1312.50" in caplog.text
    assert "final_trade_size=1312.50" in caplog.text
    assert "minimum_cash_to_deploy=3470.00" in caplog.text
    assert "skipped_by_min_deploy=true" in caplog.text
    assert "skip_reason=minimum_cash_to_deploy" in caplog.text


def _intc_like_dynamic_candidate() -> dict[str, object]:
    return {
        "symbol": "INTC",
        "score": 9.5,
        "strength_eff": 9.5,
        "route": "dynamic_momentum_override",
        "source": "dynamic_universe",
        "dynamic_candidate": True,
        "candidate_notional_requested": 1_312.50,
        "catalyst_score": 0.90,
        "event_score": 7.0,
        "news_score": 8.0,
    }


def _allocator_for_min_deploy_experiment(
    *,
    broker_mode: str = "paper",
    experiment_enabled: bool = False,
) -> CapitalAllocator:
    return CapitalAllocator(
        max_positions=10,
        symbol_cap=0.25,
        min_trade_size=1_312.50,
        min_realloc_leg=1_200.0,
        minimum_cash_to_deploy_frac=0.03468,
        broker_mode=broker_mode,
        paper_dynamic_min_deploy_experiment_enabled=experiment_enabled,
        paper_dynamic_min_deploy_experiment_use_min_realloc_leg=True,
    )


def test_dynamic_min_deploy_experiment_live_still_uses_default_floor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    a = _allocator_for_min_deploy_experiment(
        broker_mode="live",
        experiment_enabled=True,
    )

    with caplog.at_level("INFO", logger="src.portfolio.allocator_planner"):
        out = a.allocate(
            portfolio=[],
            candidates=[_intc_like_dynamic_candidate()],
            equity=100_000.0,
            cash=69_000.0,
        )

    assert out == []
    assert "minimum_cash_to_deploy=3468.00" in caplog.text
    assert "DYNAMIC_MIN_DEPLOY_EXPERIMENT" not in caplog.text


def test_dynamic_min_deploy_experiment_paper_default_uses_default_floor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    a = _allocator_for_min_deploy_experiment(
        broker_mode="paper",
        experiment_enabled=False,
    )

    with caplog.at_level("INFO", logger="src.portfolio.allocator_planner"):
        out = a.allocate(
            portfolio=[],
            candidates=[_intc_like_dynamic_candidate()],
            equity=100_000.0,
            cash=69_000.0,
        )

    assert out == []
    assert "minimum_cash_to_deploy=3468.00" in caplog.text
    assert "DYNAMIC_MIN_DEPLOY_EXPERIMENT" not in caplog.text


def test_dynamic_min_deploy_experiment_paper_dynamic_uses_min_realloc_leg(
    caplog: pytest.LogCaptureFixture,
) -> None:
    a = _allocator_for_min_deploy_experiment(
        broker_mode="paper",
        experiment_enabled=True,
    )

    with caplog.at_level("INFO", logger="src.portfolio.allocator_planner"):
        out = a.allocate(
            portfolio=[],
            candidates=[_intc_like_dynamic_candidate()],
            equity=100_000.0,
            cash=69_000.0,
        )

    assert out == [{"action": "buy", "symbol": "INTC", "notional": 1_312.50}]
    assert "DYNAMIC_MIN_DEPLOY_EXPERIMENT symbol=INTC mode=paper" in caplog.text
    assert "original_floor=3468.00" in caplog.text
    assert "experiment_floor=1200.00" in caplog.text
    assert "trade_size=1312.50" in caplog.text
    assert "would_have_skipped_default=true" in caplog.text
    assert "minimum_cash_to_deploy=1200.00" in caplog.text
    assert "skipped_by_min_deploy=false" in caplog.text


def test_dynamic_min_deploy_experiment_paper_core_still_uses_default_floor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    a = _allocator_for_min_deploy_experiment(
        broker_mode="paper",
        experiment_enabled=True,
    )
    core_candidate = {
        "symbol": "IWM",
        "score": 9.5,
        "route": "trend_long",
        "source": "core_universe",
    }

    with caplog.at_level("INFO", logger="src.portfolio.allocator_planner"):
        out = a.allocate(
            portfolio=[],
            candidates=[core_candidate],
            equity=100_000.0,
            cash=69_000.0,
        )

    assert out == []
    assert "minimum_cash_to_deploy=3468.00" in caplog.text
    assert "DYNAMIC_MIN_DEPLOY_EXPERIMENT" not in caplog.text


def test_candidate_without_requested_notional_keeps_tranche_behavior() -> None:
    a = CapitalAllocator(
        max_positions=10,
        symbol_cap=0.15,
        min_trade_size=200.0,
        min_realloc_leg=200.0,
    )

    out = a.allocate(
        portfolio=[],
        candidates=[
            {
                "symbol": "GOOGL",
                "score": 2.5,
                "allocation_bucket": "dynamic",
                "catalyst_score": 0.40,
                "event_score": 4.0,
                "source": "sec_filing",
            }
        ],
        equity=28_000.0,
        cash=6_000.0,
    )

    assert out == [{"action": "buy", "symbol": "GOOGL", "notional": 200.0}]


def test_dynamic_requested_notional_below_min_order_is_rejected(caplog: pytest.LogCaptureFixture) -> None:
    a = CapitalAllocator(
        max_positions=10,
        symbol_cap=0.15,
        min_trade_size=200.0,
        min_realloc_leg=1_200.0,
    )

    with caplog.at_level("INFO", logger="src.portfolio.allocator_planner"):
        out = a.allocate(
            portfolio=[],
            candidates=[
                {
                    "symbol": "GOOGL",
                    "score": 2.5,
                    "allocation_bucket": "dynamic",
                    "candidate_notional_requested": 1_000.0,
                    "catalyst_score": 0.40,
                    "event_score": 4.0,
                    "source": "sec_filing",
                }
            ],
            equity=28_000.0,
            cash=6_000.0,
        )

    assert out == []
    assert "ALLOCATOR_NO_ACTION_DETAIL symbol=GOOGL reason=size = 0" in caplog.text
    assert "candidate requested notional $1000.00 < min_realloc_leg 1200" in caplog.text
    assert "tranche_min=200.00" in caplog.text
    assert "candidate_requested_notional=1000.00" in caplog.text
    assert "base_requested_notional=1000.00" in caplog.text


def test_core_rebuild_candidate_requested_notional_does_not_change_base() -> None:
    a = CapitalAllocator(
        max_positions=10,
        symbol_cap=0.15,
        min_trade_size=200.0,
        min_realloc_leg=1_200.0,
    )

    out = a.allocate(
        portfolio=[],
        candidates=[
            {
                "symbol": "GOOGL",
                "score": 2.5,
                "route": "core_rebuild",
                "requested_notional": 1_312.50,
                "catalyst_score": 0.40,
                "event_score": 4.0,
            }
        ],
        equity=28_000.0,
        cash=6_000.0,
    )

    assert out == [{"action": "buy", "symbol": "GOOGL", "notional": 1_200.0}]


def test_allocator_size_floor_does_not_print_debug(capsys: pytest.CaptureFixture[str]) -> None:
    a = CapitalAllocator(
        max_positions=10,
        symbol_cap=0.15,
        min_trade_size=200.0,
        min_realloc_leg=1_200.0,
    )

    out = a.allocate(
        portfolio=[],
        candidates=[{"symbol": "GOOGL", "score": 2.5}],
        equity=28_000.0,
        cash=6_000.0,
    )

    assert out == [{"action": "buy", "symbol": "GOOGL", "notional": 1_200.0}]
    assert "ALLOCATOR SIZE FLOOR" not in capsys.readouterr().out


def test_ranked_dynamic_candidate_consumes_gross_headroom_before_core() -> None:
    a = CapitalAllocator(
        max_positions=10,
        symbol_cap=0.25,
        min_trade_size=1_200.0,
        min_realloc_leg=1_200.0,
    )

    out = a.allocate(
        portfolio=[],
        candidates=[
            {
                "symbol": "GOOGL",
                "score": 10.0,
                "dynamic_candidate": True,
                "allocation_bucket": "dynamic",
                "candidate_notional_requested": 1_312.50,
                "catalyst_score": 0.4,
                "event_score": 4.0,
            },
            {"symbol": "AMD", "score": 5.0, "route": "core_rebuild"},
            {"symbol": "ORCL", "score": 1.0, "route": "core_rebuild"},
        ],
        equity=28_000.0,
        cash=6_000.0,
        max_total_gross_dollars=1_312.50,
        current_gross_dollars=0.0,
    )

    assert out == [{"action": "buy", "symbol": "GOOGL", "notional": 1_312.50}]


def test_core_can_be_attempted_after_ranked_dynamic_rejected_for_risk(
    caplog: pytest.LogCaptureFixture,
) -> None:
    a = CapitalAllocator(
        max_positions=10,
        symbol_cap=0.25,
        min_trade_size=1_200.0,
        min_realloc_leg=1_200.0,
    )

    with caplog.at_level("INFO", logger="src.portfolio.allocator_planner"):
        out = a.allocate(
            portfolio=[],
            candidates=[
                {
                    "symbol": "GOOGL",
                    "score": 10.0,
                    "dynamic_candidate": True,
                    "allocation_bucket": "dynamic",
                    "candidate_notional_cap": 1_000.0,
                    "catalyst_score": 0.4,
                    "event_score": 4.0,
                },
                {"symbol": "ORCL", "score": 1.0, "route": "core_rebuild"},
            ],
            equity=28_000.0,
            cash=6_000.0,
            max_total_gross_dollars=1_200.0,
            current_gross_dollars=0.0,
        )

    assert out == [{"action": "buy", "symbol": "ORCL", "notional": 1_200.0}]
    assert "ALLOCATOR_NO_ACTION_DETAIL symbol=GOOGL reason=size = 0" in caplog.text
    assert "candidate requested notional $1000.00 < min_realloc_leg 1200" in caplog.text


def test_allocate_empty_candidates() -> None:
    a = CapitalAllocator()
    assert (
        a.allocate(portfolio=[], candidates=[], equity=100_000.0, cash=5_000.0) == []
    )


def test_parse_capital_allocator_cfg_defaults() -> None:
    c = parse_capital_allocator_cfg({})
    assert c["enabled"] is False
    assert c["max_positions"] == 10
    assert c["min_realloc_leg"] == pytest.approx(300.0)
    assert c["soft_cap_mode"] is False
    assert c["cap_penalty_multiplier"] == pytest.approx(0.5)
    assert c.get("allow_cross_bucket_rebalance") is False
    assert c.get("require_net_sell_gte_buy") is True
    assert c.get("risk_control_gross_frac") == pytest.approx(0.95)
    assert c.get("risk_control_block_buys") is True
    assert c.get("prioritize_diversification") is False
    assert c.get("diversification_reentry_scale") == pytest.approx(0.55)
    assert c.get("net_reduction_max_buy_to_sell_ratio") == pytest.approx(0.5)
    assert c.get("net_reduction_near_cap_relative_to_max") == pytest.approx(0.9)
    assert c.get("min_gross_deployment_pct") == pytest.approx(0.85)
    assert c.get("deploy_top_n_signals") == 4
    assert c.get("fallback_on_empty_alloc") is True
    assert c.get("empty_alloc_top_n") == 5
    assert c.get("symbol_caps") == {}
    assert c.get("bullish_force_minimum_deploy") is True
    assert c.get("rebalance_fund_from_weakest") is False
    assert c.get("rebalance_weakest_trim_fraction") == pytest.approx(0.30)
    assert c.get("replace_weakest_with_stronger") is True
    assert c.get("sell_only_if_needed") is True
    assert c.get("replacement_strength_ratio") == pytest.approx(1.0)
    assert c.get("ignore_soft_caps_after_sell_minutes", 0.0) == pytest.approx(0.0)
    assert c.get("concentration_bias_enabled") is False
    assert int(c.get("concentration_top_n", 0) or 0) == 0
    assert c.get("minimum_cash_to_deploy_pct") == pytest.approx(0.0)
    assert c.get("single_pass_per_cycle") is True
    assert c.get("max_single_order_notional_pct") is None
    assert c.get("max_single_order_notional") is None
    assert c.get("refuse_to_allocate_if_gross_above") is None
    assert c.get("max_gross_increase_per_cycle") is None


def test_parse_capital_allocator_cfg_minimum_cash_to_deploy_pct() -> None:
    c = parse_capital_allocator_cfg(
        {
            "capital_allocator": {
                "enabled": True,
                "minimum_cash_to_deploy_pct": 0.05,
            }
        }
    )
    assert c["minimum_cash_to_deploy_pct"] == pytest.approx(0.05)


def test_allocate_skips_buy_below_minimum_cash_to_deploy_after_headroom_clip() -> None:
    a = CapitalAllocator(
        max_positions=10,
        symbol_cap=0.5,
        min_trade_size=500.0,
        min_realloc_leg=200.0,
        minimum_cash_to_deploy_frac=0.05,
    )
    out = a.allocate(
        portfolio=[],
        candidates=[{"symbol": "NEW", "score": 1.0}],
        equity=100_000.0,
        cash=25_000.0,
        max_total_gross_dollars=80_000.0,
        current_gross_dollars=79_700.0,
    )
    assert out == []


def test_minimum_cash_to_deploy_uses_five_dollar_buffer_allows_edge() -> None:
    """Notional is below min-deploy floor but within ``+ 5`` buffer — buy is allowed."""
    a = CapitalAllocator(
        max_positions=10,
        symbol_cap=0.5,
        min_trade_size=996.0,
        min_realloc_leg=200.0,
        minimum_cash_to_deploy_frac=0.05,
    )
    out = a.allocate(
        portfolio=[],
        candidates=[{"symbol": "NEW", "score": 1.0}],
        equity=20_000.0,
        cash=10_000.0,
    )
    assert len(out) == 1
    assert out[0]["action"] == "buy"
    assert out[0]["symbol"] == "NEW"
    assert out[0]["notional"] == pytest.approx(996.0)


def test_minimum_cash_to_deploy_buffer_still_skips_when_sum_below_floor() -> None:
    """``trade_size + 5`` still under min-deploy — skip (same as strict check for small notionals)."""
    a = CapitalAllocator(
        max_positions=10,
        symbol_cap=0.5,
        min_trade_size=994.0,
        min_realloc_leg=200.0,
        minimum_cash_to_deploy_frac=0.05,
    )
    out = a.allocate(
        portfolio=[],
        candidates=[{"symbol": "NEW", "score": 1.0}],
        equity=20_000.0,
        cash=10_000.0,
    )
    assert out == []


def test_parse_capital_allocator_cfg_max_gross_increase_per_cycle() -> None:
    c = parse_capital_allocator_cfg(
        {"allocator": {"max_gross_increase_per_cycle": "5"}}
    )
    assert c["max_gross_increase_per_cycle"] == pytest.approx(0.05)
    c2 = parse_capital_allocator_cfg(
        {
            "capital_allocator": {"max_gross_increase_per_cycle": 0.03},
            "allocator": {"max_gross_increase_per_cycle": 0.09},
        }
    )
    assert c2["max_gross_increase_per_cycle"] == pytest.approx(0.03)


def test_parse_capital_allocator_max_single_order_caps() -> None:
    c = parse_capital_allocator_cfg(
        {
            "capital_allocator": {
                "max_single_order_notional_pct": 0.08,
                "max_single_order_notional": 2000,
            }
        }
    )
    assert c["max_single_order_notional_pct"] == pytest.approx(0.08)
    assert c["max_single_order_notional"] == pytest.approx(2000.0)


def test_clip_allocator_buy_notionals_pct_and_abs() -> None:
    act = [
        {"action": "buy", "symbol": "SPY", "notional": 50_000.0},
        {"action": "sell", "symbol": "XLP", "notional": 1000.0},
    ]
    out = clip_allocator_buy_notionals_to_single_order_caps(
        act,
        account_equity=100_000.0,
        max_single_order_notional_pct=0.08,
        max_single_order_notional=2000.0,
    )
    assert out[0]["notional"] == pytest.approx(2000.0)
    assert out[1]["notional"] == pytest.approx(1000.0)


def test_clip_allocator_buy_notionals_pct_only() -> None:
    act = [{"action": "buy", "symbol": "SPY", "notional": 10_000.0}]
    out = clip_allocator_buy_notionals_to_single_order_caps(
        act,
        account_equity=100_000.0,
        max_single_order_notional_pct=0.08,
        max_single_order_notional=None,
    )
    assert out[0]["notional"] == pytest.approx(8000.0)


def test_parse_capital_allocator_replacement_strength_ratio() -> None:
    c = parse_capital_allocator_cfg(
        {"capital_allocator": {"replacement_strength_ratio": 1.2}}
    )
    assert c["replacement_strength_ratio"] == pytest.approx(1.2)


def test_parse_capital_allocator_single_pass_per_cycle_false() -> None:
    c = parse_capital_allocator_cfg(
        {"capital_allocator": {"single_pass_per_cycle": False}}
    )
    assert c.get("single_pass_per_cycle") is False


def test_parse_capital_allocator_cfg_symbol_caps_sets_base_symbol_cap() -> None:
    c = parse_capital_allocator_cfg(
        {
            "capital_allocator": {
                "symbol_caps": {"base": 0.10, "bullish_regime": 0.15}
            }
        }
    )
    assert c["symbol_cap"] == pytest.approx(0.10)
    assert c.get("symbol_caps", {}).get("base") in (0.1, 0.10)
    assert c.get("symbol_caps", {}).get("bullish_regime") == 0.15


def test_effective_capital_allocator_symbol_cap_bullish_score() -> None:
    ca = {
        "symbol_cap": 0.10,
        "symbol_caps": {"base": 0.10, "bullish_regime": 0.15},
    }
    assert effective_capital_allocator_symbol_cap_frac(
        {}, ca, regime_score=4, regime_condition=None, account_equity=100_000.0
    ) == pytest.approx(0.15)


def test_effective_capital_allocator_symbol_cap_neutral_uses_base() -> None:
    ca = {
        "symbol_cap": 0.10,
        "symbol_caps": {"base": 0.10, "bullish_regime": 0.15},
    }
    assert effective_capital_allocator_symbol_cap_frac(
        {}, ca, regime_score=3, regime_condition=None, account_equity=100_000.0
    ) == pytest.approx(0.10)


def test_effective_capital_allocator_symbol_cap_merges_risk_stricter() -> None:
    cfg = {"risk": {"max_symbol_allocation_pct": 0.12}, "portfolio": {}}
    ca = {
        "symbol_cap": 0.10,
        "symbol_caps": {"base": 0.10, "bullish_regime": 0.15},
    }
    assert effective_capital_allocator_symbol_cap_frac(
        cfg, ca, regime_score=4, regime_condition=None, account_equity=100_000.0
    ) == pytest.approx(0.12)


def test_effective_capital_allocator_symbol_cap_soft_hard_basic() -> None:
    ca = {
        "symbol_cap": 0.18,
        "symbol_caps": {"soft": 0.10, "hard": 0.18},
    }
    s, h = effective_capital_allocator_symbol_cap_soft_hard(
        {}, ca, regime_score=3, regime_condition=None, account_equity=100_000.0
    )
    assert s == pytest.approx(0.10)
    assert h == pytest.approx(0.18)
    assert effective_capital_allocator_symbol_cap_frac(
        {}, ca, regime_score=3, regime_condition=None, account_equity=100_000.0
    ) == pytest.approx(0.18)


def test_effective_capital_allocator_bullish_soft_hard_override() -> None:
    ca = {
        "symbol_cap": 0.18,
        "symbol_caps": {
            "soft": 0.10,
            "hard": 0.18,
            "bullish_soft": 0.12,
            "bullish_hard": 0.20,
        },
    }
    s, h = effective_capital_allocator_symbol_cap_soft_hard(
        {}, ca, regime_score=4, regime_condition=None, account_equity=100_000.0
    )
    assert s == pytest.approx(0.12)
    assert h == pytest.approx(0.20)


def test_effective_capital_symbol_soft_hard_merges_risk() -> None:
    cfg = {"risk": {"max_symbol_allocation_pct": 0.14}, "portfolio": {}}
    ca = {"symbol_caps": {"soft": 0.10, "hard": 0.18}, "symbol_cap": 0.18}
    s, h = effective_capital_allocator_symbol_cap_soft_hard(
        cfg, ca, regime_score=2, account_equity=10_000.0
    )
    assert s == pytest.approx(0.10)
    assert h == pytest.approx(0.14)


def test_effective_capital_symbol_soft_hard_regime_4_bumps_line_vs_risk_95() -> None:
    """``regime_4`` in symbol_caps hard line + min with portfolio cap (replaces 9.5% risk in merge)."""
    cfg = {
        "risk": {"max_symbol_allocation_pct": 0.095},
        "portfolio": {
            "max_single_position_pct": 18.0,
            "capital_allocator": {
                "symbol_caps": {
                    "soft": 0.10,
                    "hard": 0.18,
                    "regime_4": 0.15,
                }
            },
        },
    }
    ca = {
        "symbol_cap": 0.18,
        "symbol_caps": {"soft": 0.10, "hard": 0.18, "regime_4": 0.15},
    }
    s, h = effective_capital_allocator_symbol_cap_soft_hard(
        cfg, ca, regime_score=4, account_equity=10_000.0
    )
    assert s == pytest.approx(0.10)
    assert h == pytest.approx(0.15)
    assert effective_capital_allocator_symbol_cap_frac(
        cfg, ca, regime_score=4, account_equity=10_000.0
    ) == pytest.approx(0.15)


def test_parse_capital_allocator_cfg_symbol_caps_soft_hard_sets_symbol_cap() -> None:
    c = parse_capital_allocator_cfg(
        {
            "capital_allocator": {
                "symbol_caps": {"soft": 0.10, "hard": 0.18},
            }
        }
    )
    assert c["symbol_cap"] == pytest.approx(0.18)
    assert c["symbol_caps"].get("soft") == 0.10
    assert c["symbol_caps"].get("hard") == 0.18


def test_collect_symbol_cap_tier_hard_fractions_leaders_before_core() -> None:
    sc = {
        "leaders": {"NVDA": "12%", "SMH": 0.10},
        "core": {"NVDA": "6%", "IWM": "6%"},
        "defensive": {"KO": "3%"},
    }
    m = collect_symbol_cap_tier_hard_fractions(sc)
    assert m["NVDA"] == pytest.approx(0.12)
    assert m["SMH"] == pytest.approx(0.10)
    assert m["IWM"] == pytest.approx(0.06)
    assert m["KO"] == pytest.approx(0.03)


def test_collect_symbol_cap_tier_hard_fractions_nested_tiers_key() -> None:
    sc = {"soft": 0.1, "hard": 0.18, "tiers": {"leaders": {"SPY": "10%"}}}
    assert collect_symbol_cap_tier_hard_fractions(sc)["SPY"] == pytest.approx(0.10)
    assert symbol_caps_define_tier_buckets(sc) is True


def test_parse_capital_allocator_cfg_tiers_bumps_symbol_cap_when_no_soft_hard_base() -> None:
    c = parse_capital_allocator_cfg(
        {
            "capital_allocator": {
                "symbol_caps": {
                    "leaders": {"NVDA": 0.12},
                    "defensive": {"KO": "3%"},
                },
            }
        }
    )
    assert c["symbol_cap"] == pytest.approx(0.12)


def test_effective_capital_allocator_symbol_caps_by_symbol_merges_dual_band() -> None:
    cfg = {
        "portfolio": {"max_single_position_pct": 18.0},
        # 15%% risk line keeps allocator soft/hard band (10%%/18%%) after merge → 10%%/15%%.
        "risk": {"max_symbol_allocation_pct": 0.15},
    }
    ca = {
        "symbol_cap": 0.18,
        "symbol_caps": {
            "soft": 0.10,
            "hard": 0.18,
            "leaders": {"NVDA": 0.12},
            "defensive": {"KO": "3%"},
        },
    }
    bys = effective_capital_allocator_symbol_caps_by_symbol(
        cfg, ca, regime_score=2, account_equity=100_000.0
    )
    assert bys is not None
    s_nvda, h_nvda = bys["NVDA"]
    assert h_nvda == pytest.approx(0.12)
    ratio = 0.10 / 0.15
    assert s_nvda == pytest.approx(h_nvda * ratio)
    s_ko, h_ko = bys["KO"]
    assert h_ko == pytest.approx(0.03)
    assert s_ko == pytest.approx(0.03 * ratio)


def test_effective_capital_allocator_symbol_caps_by_symbol_uses_etf_risk_lane() -> None:
    cfg = {
        "portfolio": {},
        "risk": {"max_symbol_allocation_pct": {"default": 0.15, "etf": 0.22}},
    }
    ca = {
        "symbol_cap": 0.22,
        "symbol_caps": {
            "soft": 0.10,
            "hard": 0.15,
            "leaders": {"SPY": "22%", "QQQ": "22%", "AAPL": "22%"},
            "core": {"IWM": "22%"},
        },
    }
    bys = effective_capital_allocator_symbol_caps_by_symbol(
        cfg,
        ca,
        account_equity=100_000.0,
    )
    assert bys is not None
    assert bys["SPY"][1] == pytest.approx(0.22)
    assert bys["QQQ"][1] == pytest.approx(0.22)
    assert bys["IWM"][1] == pytest.approx(0.22)
    assert bys["AAPL"][1] == pytest.approx(0.15)


def test_allocate_respects_symbol_cap_fractions_per_ticker() -> None:
    """KO hard 3%% vs NVDA 12%% on same equity — tier map caps adds separately."""
    fr = {"KO": (0.03, 0.03), "NVDA": (0.12, 0.12)}
    cap_alloc = CapitalAllocator(
        max_positions=10,
        symbol_cap=0.18,
        symbol_cap_soft=0.18,
        min_trade_size=1000.0,
        min_realloc_leg=50.0,
        soft_cap_mode=True,
        cap_penalty_multiplier=0.5,
        symbol_cap_fractions=fr,
    )
    out_ko = cap_alloc.allocate(
        portfolio=[{"symbol": "KO", "value": 2900.0, "score": 0.5}],
        candidates=[{"symbol": "KO", "score": 0.99}],
        equity=100_000.0,
        cash=50_000.0,
    )
    assert out_ko == [{"action": "buy", "symbol": "KO", "notional": 100.0}]
    out_nvda = cap_alloc.allocate(
        portfolio=[{"symbol": "NVDA", "value": 11_000.0, "score": 0.5}],
        candidates=[{"symbol": "NVDA", "score": 0.99}],
        equity=100_000.0,
        cash=50_000.0,
    )
    assert out_nvda == [{"action": "buy", "symbol": "NVDA", "notional": 1000.0}]


def test_allocate_dual_cap_penalty_in_soft_hard_band() -> None:
    """v0 in [10%%, 18%%) of equity: reduced tranche capped to hard line."""
    a = CapitalAllocator(
        max_positions=10,
        symbol_cap=0.18,
        symbol_cap_soft=0.10,
        min_trade_size=1000.0,
        min_realloc_leg=200.0,
        cap_penalty_multiplier=0.5,
    )
    out = a.allocate(
        portfolio=[{"symbol": "SPY", "value": 11_000.0, "score": 0.5}],
        candidates=[{"symbol": "SPY", "score": 0.99}],
        equity=100_000.0,
        cash=5_000.0,
    )
    assert out == [{"action": "buy", "symbol": "SPY", "notional": 500.0}]


def test_allocate_dual_cap_no_add_at_or_above_hard() -> None:
    a = CapitalAllocator(
        max_positions=10,
        symbol_cap=0.18,
        symbol_cap_soft=0.10,
        min_trade_size=1000.0,
    )
    out = a.allocate(
        portfolio=[{"symbol": "SPY", "value": 18_000.0, "score": 0.5}],
        candidates=[{"symbol": "SPY", "score": 0.99}],
        equity=100_000.0,
        cash=5_000.0,
    )
    assert out == []


def test_allocate_dual_cap_crossing_soft_clips_full_tranche() -> None:
    """v0+min would exceed soft*eq: add at most headroom to soft (full tranche, not penalty)."""
    a = CapitalAllocator(
        max_positions=10,
        symbol_cap=0.18,
        symbol_cap_soft=0.10,
        min_trade_size=1000.0,
        min_realloc_leg=200.0,
    )
    out = a.allocate(
        portfolio=[{"symbol": "SPY", "value": 9_500.0, "score": 0.5}],
        candidates=[{"symbol": "SPY", "score": 0.99}],
        equity=100_000.0,
        cash=5_000.0,
    )
    assert out == [{"action": "buy", "symbol": "SPY", "notional": 500.0}]


def test_parse_capital_allocator_cfg_max_positions_from_portfolio_allocator() -> None:
    c = parse_capital_allocator_cfg({"capital_allocator": {}, "allocator": {"max_positions": 8}})
    assert c["max_positions"] == 8


def test_parse_capital_allocator_cfg_capital_allocator_wins_over_portfolio_allocator() -> None:
    c = parse_capital_allocator_cfg(
        {"capital_allocator": {"max_positions": 3}, "allocator": {"max_positions": 9}}
    )
    assert c["max_positions"] == 3
    assert c["symbol_cap"] == pytest.approx(0.25)
    assert c["min_trade_size"] == pytest.approx(1000.0)


def test_parse_capital_allocator_cfg_soft_from_portfolio_allocator() -> None:
    c = parse_capital_allocator_cfg(
        {
            "capital_allocator": {"symbol_cap": 0.25},
            "allocator": {
                "soft_cap_mode": True,
                "cap_penalty_multiplier": 0.4,
            },
        }
    )
    assert c["soft_cap_mode"] is True
    assert c["cap_penalty_multiplier"] == pytest.approx(0.4)


def test_parse_capital_allocator_wins_for_soft_on_conflict() -> None:
    c = parse_capital_allocator_cfg(
        {
            "capital_allocator": {"soft_cap_mode": False},
            "allocator": {"soft_cap_mode": True},
        }
    )
    assert c["soft_cap_mode"] is False


def test_parse_capital_allocator_cfg_cross_from_portfolio_allocator() -> None:
    c = parse_capital_allocator_cfg(
        {
            "capital_allocator": {"enabled": False},
            "allocator": {"allow_cross_bucket_rebalance": True},
        }
    )
    assert c["allow_cross_bucket_rebalance"] is True


def test_dedupe_cap_alloc_rows_keeps_stronger() -> None:
    rows = [
        {"sym_u": "SPY", "strength_eff": 0.5, "df": None},
        {"sym_u": "SPY", "strength_eff": 0.9, "df": None},
    ]
    out = dedupe_cap_alloc_rows(rows)
    assert len(out) == 1
    assert out[0]["strength_eff"] == pytest.approx(0.9)


def test_dedupe_cap_alloc_rows_prefers_higher_composite_score() -> None:
    rows = [
        {"sym_u": "SPY", "composite_score": 2.0, "strength_eff": 0.9, "df": None},
        {"sym_u": "SPY", "composite_score": 3.5, "strength_eff": 0.1, "df": None},
    ]
    out = dedupe_cap_alloc_rows(rows)
    assert len(out) == 1
    assert out[0]["composite_score"] == pytest.approx(3.5)


def test_dedupe_cap_alloc_rows_prefers_higher_signal_priority_score() -> None:
    from src.signal_ranking import SIGNAL_RANKING_MODE_SIGNAL_PRIORITY

    rows = [
        {
            "sym_u": "SPY",
            "composite_score": 3.5,
            "priority_score": 1.0,
            "rank_breakdown": {
                "trend_strength": 0.25,
                "momentum": 0.25,
                "volatility_expansion": 0.25,
                "relative_strength": 0.25,
            },
        },
        {
            "sym_u": "SPY",
            "composite_score": 2.0,
            "priority_score": 3.2,
            "rank_breakdown": {
                "trend_strength": 0.8,
                "momentum": 0.8,
                "volatility_expansion": 0.8,
                "relative_strength": 0.8,
            },
        },
    ]
    out = dedupe_cap_alloc_rows(rows, ranking_mode=SIGNAL_RANKING_MODE_SIGNAL_PRIORITY)
    assert len(out) == 1
    assert out[0]["priority_score"] == pytest.approx(3.2)


def test_build_allocator_candidates_uses_priority_score_before_composite() -> None:
    cands = build_allocator_candidates(
        [{"sym_u": "X", "priority_score": 3.9, "composite_score": 1.0, "strength_eff": 0.5}]
    )
    assert len(cands) == 1
    assert cands[0]["score"] == pytest.approx(3.9)


def test_parse_capital_allocator_cfg_risk_control_gross_as_percent() -> None:
    c = parse_capital_allocator_cfg(
        {"capital_allocator": {"risk_control_gross_frac": 90}}
    )
    assert c["risk_control_gross_frac"] == pytest.approx(0.90)


def test_parse_capital_allocator_cfg_min_gross_deployment_as_percent() -> None:
    c = parse_capital_allocator_cfg(
        {"capital_allocator": {"min_gross_deployment_pct": 80}}
    )
    assert c["min_gross_deployment_pct"] == pytest.approx(0.80)


def test_parse_capital_allocator_cfg_deploy_top_n_clamped() -> None:
    c = parse_capital_allocator_cfg(
        {"capital_allocator": {"deploy_top_n_signals": 10}}
    )
    assert c["deploy_top_n_signals"] == 5
    c2 = parse_capital_allocator_cfg(
        {"capital_allocator": {"deploy_top_n_signals": 2}}
    )
    assert c2["deploy_top_n_signals"] == 3


def test_parse_capital_allocator_cfg_empty_alloc_top_n_clamped() -> None:
    c = parse_capital_allocator_cfg(
        {"capital_allocator": {"empty_alloc_top_n": 7}}
    )
    assert c["empty_alloc_top_n"] == 7
    c2 = parse_capital_allocator_cfg(
        {"capital_allocator": {"empty_alloc_top_n": 99}}
    )
    assert c2["empty_alloc_top_n"] == 20


def test_parse_capital_allocator_cfg_risk_control_block_buys_false() -> None:
    c = parse_capital_allocator_cfg(
        {"capital_allocator": {"risk_control_block_buys": "false"}}
    )
    assert c["risk_control_block_buys"] is False


def test_parse_capital_allocator_cfg_require_net_sell_gte_buy_false() -> None:
    c = parse_capital_allocator_cfg(
        {"capital_allocator": {"require_net_sell_gte_buy": False}}
    )
    assert c["require_net_sell_gte_buy"] is False
    c2 = parse_capital_allocator_cfg(
        {"capital_allocator": {"require_net_sell_gte_buy": "false"}}
    )
    assert c2["require_net_sell_gte_buy"] is False


def test_parse_capital_allocator_cfg_execution_policy_flags() -> None:
    c = parse_capital_allocator_cfg(
        {
            "capital_allocator": {
                "allow_no_trade_cycles": "false",
                "selected_must_execute": "true",
                "force_deploy_when_candidates_exist": True,
                "force_minimum_trade_single_candidate": "false",
            }
        }
    )

    assert c["allow_no_trade_cycles"] is False
    assert c["selected_must_execute"] is True
    assert c["force_deploy_when_candidates_exist"] is True
    assert c["force_minimum_trade_single_candidate"] is False


def test_parse_capital_allocator_cfg_idle_fallback_max_gross_pct() -> None:
    c = parse_capital_allocator_cfg(
        {
            "capital_allocator": {
                "if_no_actions_cycles": 2,
                "idle_fallback": {
                    "enabled": True,
                    "max_gross_pct": 85,
                    "prefer_dynamic_symbols": True,
                },
            }
        }
    )
    assert c["if_no_actions_cycles"] == 2
    assert c["idle_fallback_enabled"] is True
    assert c["idle_fallback_max_gross_pct"] == pytest.approx(0.85)
    assert c["idle_fallback_prefer_dynamic_symbols"] is True


def test_trim_net_sell_gte_buy_unchanged_when_sells_gte_buys() -> None:
    act = [
        {"action": "sell", "symbol": "A", "notional": 500.0},
        {"action": "buy", "symbol": "B", "notional": 300.0},
    ]
    out = trim_allocator_actions_for_net_sell_gte_buy(
        act, min_realloc_leg=300.0
    )
    assert out == act


def test_trim_net_sell_gte_buy_trims_last_buy() -> None:
    act = [
        {"action": "sell", "symbol": "A", "notional": 200.0},
        {"action": "buy", "symbol": "B", "notional": 500.0},
    ]
    out = trim_allocator_actions_for_net_sell_gte_buy(
        act, min_realloc_leg=200.0
    )
    assert out[0] == act[0]
    assert out[1]["action"] == "buy"
    assert out[1]["notional"] == pytest.approx(200.0)


def test_trim_net_sell_gte_buy_removes_buys_only_plans() -> None:
    act = [
        {"action": "buy", "symbol": "A", "notional": 100.0},
        {"action": "buy", "symbol": "B", "notional": 200.0},
    ]
    out = trim_allocator_actions_for_net_sell_gte_buy(
        act, min_realloc_leg=300.0
    )
    assert out == []


def test_trim_net_sell_gte_buy_drops_line_when_partial_below_min_leg() -> None:
    """If trimming would leave a sub-``min_realloc_leg`` remainder, the whole buy line is dropped."""
    act = [
        {"action": "sell", "symbol": "A", "notional": 100.0},
        {"action": "buy", "symbol": "B", "notional": 400.0},
    ]
    out = trim_allocator_actions_for_net_sell_gte_buy(
        act, min_realloc_leg=300.0
    )
    assert out == [act[0]]


def test_trim_max_buy_to_sell_ratio_half() -> None:
    """0.5 ⇒ buy notional at most half of sell notional; unchanged when already satisfied."""
    act = [
        {"action": "sell", "symbol": "A", "notional": 1000.0},
        {"action": "buy", "symbol": "B", "notional": 500.0},
    ]
    out = trim_allocator_actions_for_max_buy_to_sell_ratio(
        act, min_realloc_leg=100.0, max_buy_to_sell_ratio=0.5
    )
    assert out[0] == act[0]
    assert out[1]["notional"] == pytest.approx(500.0)
    more_buy = [
        {"action": "sell", "symbol": "A", "notional": 1000.0},
        {"action": "buy", "symbol": "B", "notional": 800.0},
    ]
    out2 = trim_allocator_actions_for_max_buy_to_sell_ratio(
        more_buy, min_realloc_leg=100.0, max_buy_to_sell_ratio=0.5
    )
    assert out2[1]["notional"] == pytest.approx(500.0)


def test_trim_max_buy_to_sell_ratio_no_sells() -> None:
    act = [
        {"action": "buy", "symbol": "A", "notional": 200.0},
    ]
    out = trim_allocator_actions_for_max_buy_to_sell_ratio(
        act, min_realloc_leg=50.0, max_buy_to_sell_ratio=0.5
    )
    assert out == []


def test_consolidate_net_by_symbol_merges_same_side() -> None:
    act = [
        {"action": "buy", "symbol": "SPY", "notional": 200.0},
        {"action": "buy", "symbol": "SPY", "notional": 300.0},
    ]
    out = consolidate_allocator_actions_net_by_symbol(
        act, min_abs_net_notional=0.0
    )
    assert out == [{"action": "buy", "symbol": "SPY", "notional": 500.0}]


def test_consolidate_net_by_symbol_washes_to_zero() -> None:
    act = [
        {"action": "buy", "symbol": "SPY", "notional": 500.0},
        {"action": "sell", "symbol": "SPY", "notional": 500.0},
    ]
    out = consolidate_allocator_actions_net_by_symbol(
        act, min_abs_net_notional=500.0
    )
    assert out == []


def test_consolidate_net_by_symbol_residual_below_floor_dropped() -> None:
    act = [
        {"action": "buy", "symbol": "SPY", "notional": 400.0},
        {"action": "sell", "symbol": "SPY", "notional": 100.0},
    ]
    out = consolidate_allocator_actions_net_by_symbol(
        act, min_abs_net_notional=500.0
    )
    assert out == []


def test_consolidate_net_by_symbol_keeps_distinct_symbols() -> None:
    act = [
        {"action": "buy", "symbol": "SPY", "notional": 500.0},
        {"action": "sell", "symbol": "XLP", "notional": 500.0},
    ]
    out = consolidate_allocator_actions_net_by_symbol(
        act, min_abs_net_notional=300.0
    )
    assert len(out) == 2
    assert {x["symbol"] for x in out} == {"SPY", "XLP"}


def test_gross_book_near_effective_max_for_net_reduction() -> None:
    base = 0.92
    _cfg: dict = {
        "portfolio": {
            "exposure_gates": {
                "enabled": True,
                "max_total_exposure_frac": base,
            }
        }
    }
    rel = 0.9
    thr = base * rel
    g_ok = 100.0 * (thr + 0.01)
    g_lo = 100.0 * (thr - 0.01)
    assert gross_book_near_effective_max_for_net_reduction(
        g_ok, _cfg, relative_to_max_frac=rel
    )
    assert not gross_book_near_effective_max_for_net_reduction(
        g_lo, _cfg, relative_to_max_frac=rel
    )


def test_reorder_diversification_favors_underweight_sector() -> None:
    """With heavy **broad** book, a **financials** name outranks a same-score broad add."""
    ssec = {
        "SPY": "broad",
        "XLF": "financials",
    }
    tmap = {
        "SPY": "broad_index",
        "XLF": "financials",
    }
    port = [{"symbol": "SPY", "value": 50_000.0, "score": 0.4}]
    cands = [
        {"symbol": "SPY", "score": 0.8},
        {"symbol": "XLF", "score": 0.8},
    ]
    ca = parse_capital_allocator_cfg(
        {
            "capital_allocator": {
                "prioritize_diversification": True,
                "diversification_reentry_scale": 0.55,
                "diversification_reference_exposure_pct": 30.0,
            }
        }
    )
    out = reorder_allocator_candidates_diversification(
        cands, port, 100_000.0, ca, ssec, tmap, "other"
    )
    assert [x["symbol"] for x in out] == ["XLF", "SPY"]


def test_reorder_diversification_deprioritizes_reentry() -> None:
    """**Re-entry** (add to held) falls behind a **new** name in an empty sleeve with similar score."""
    ssec = {"A": "aa", "B": "bb"}
    tmap = {"A": "t_a", "B": "t_b"}
    port = [{"symbol": "A", "value": 10_000.0, "score": 0.1}]
    cands = [
        {"symbol": "A", "score": 0.8},
        {"symbol": "B", "score": 0.75},
    ]
    ca = parse_capital_allocator_cfg(
        {
            "capital_allocator": {
                "prioritize_diversification": True,
                "diversification_reentry_scale": 0.2,
            }
        }
    )
    out = reorder_allocator_candidates_diversification(
        cands, port, 100_000.0, ca, ssec, tmap, "other"
    )
    assert [x["symbol"] for x in out] == ["B", "A"]


def test_parse_concentration_bias() -> None:
    c = parse_capital_allocator_cfg(
        {
            "capital_allocator": {
                "concentration_bias": {
                    "enabled": True,
                    "top_n": 3,
                    "top_tranche_scale": 1.35,
                    "rest_tranche_scale": 0.7,
                }
            }
        }
    )
    assert c.get("concentration_bias_enabled") is True
    assert c.get("concentration_top_n") == 3
    assert c.get("concentration_top_tranche_scale") == pytest.approx(1.35)
    assert c.get("concentration_rest_tranche_scale") == pytest.approx(0.7)


def test_rank_allocator_candidates_is_score_desc() -> None:
    rows = [
        {"symbol": "LO", "score": 0.1},
        {"symbol": "HI", "score": 0.9},
        {"symbol": "MID", "score": 0.5},
    ]
    r = rank_allocator_candidates(rows)
    assert [x["symbol"] for x in r] == ["HI", "MID", "LO"]
    assert [x["symbol"] for x in r[:3]] == ["HI", "MID", "LO"]


def test_concentration_bias_scales_tranches_by_rank() -> None:
    """Top 3: 2× min_trade; 4th+: 0.5× (allocate_more / allocate_less)."""
    a = CapitalAllocator(
        max_positions=5,
        symbol_cap=0.5,
        min_trade_size=100.0,
        min_realloc_leg=1.0,
        concentration_top_n=3,
        concentration_top_tranche_scale=2.0,
        concentration_rest_tranche_scale=0.5,
    )
    out = a.allocate(
        portfolio=[],
        candidates=[
            {"symbol": "A", "score": 3.0},
            {"symbol": "B", "score": 2.0},
            {"symbol": "C", "score": 1.0},
            {"symbol": "D", "score": 0.5},
        ],
        equity=1_000_000.0,
        cash=50_000.0,
    )
    buys = [x for x in out if x.get("action") == "buy"]
    assert len(buys) >= 4
    # Processing order: A, B, C, D — first three legs 200, fourth 50
    assert buys[0]["notional"] == pytest.approx(200.0)
    assert buys[1]["notional"] == pytest.approx(200.0)
    assert buys[2]["notional"] == pytest.approx(200.0)
    assert buys[3]["notional"] == pytest.approx(50.0)


def test_allocator_bullish_regime_for_defensive_drift() -> None:
    assert allocator_bullish_regime_for_defensive_drift(4, "bullish", regime_min_score=4)
    assert allocator_bullish_regime_for_defensive_drift(4, None, regime_min_score=4)
    assert not allocator_bullish_regime_for_defensive_drift(3, None, regime_min_score=4)
    assert not allocator_bullish_regime_for_defensive_drift(4, "defensive", regime_min_score=4)


def test_apply_defensive_drift_scales_ko_in_bullish_regime() -> None:
    ca = {
        "defensive_drift": {
            "enabled": True,
            "priority_scale": 0.12,
            "symbols": ["KO"],
            "match_sector_substrings": [],
            "match_theme_substrings": [],
        }
    }
    sector = {"KO": "consumer_staples", "NVDA": "technology"}
    theme = {"KO": "mega_cap", "NVDA": "semis"}
    rows = [
        {"symbol": "KO", "score": 2.0},
        {"symbol": "NVDA", "score": 2.0},
    ]
    out = apply_allocator_defensive_drift_scores(
        rows,
        regime_score=4,
        regime_condition="bullish",
        symbol_sector=sector,
        theme_map=theme,
        default_sector="other",
        ca_cfg=ca,
    )
    assert out[0]["score"] == pytest.approx(0.24)
    assert out[0].get("defensive_drift_scaled") is True
    assert out[1]["score"] == pytest.approx(2.0)


def test_apply_defensive_drift_skips_when_regime_defensive() -> None:
    ca = {"defensive_drift": {"enabled": True, "symbols": ["KO"]}}
    rows = [{"symbol": "KO", "score": 2.0}]
    out = apply_allocator_defensive_drift_scores(
        rows,
        regime_score=4,
        regime_condition="defensive",
        symbol_sector={"KO": "other"},
        theme_map={"KO": "other"},
        default_sector="other",
        ca_cfg=ca,
    )
    assert out[0]["score"] == pytest.approx(2.0)


def test_parse_defensive_drift_cfg_defaults_off() -> None:
    d = parse_defensive_drift_cfg({})
    assert d["enabled"] is False


def test_parse_defensive_drift_custom_lists() -> None:
    d = parse_defensive_drift_cfg(
        {
            "defensive_drift": {
                "enabled": True,
                "symbols": ["xlp"],
                "match_sector_substrings": ["foo"],
            }
        }
    )
    assert "XLP" in d["symbols"]
    assert "foo" in d["sector_substrings"]
