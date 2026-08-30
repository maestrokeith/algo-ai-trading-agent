"""Unit tests for src/portfolio_selection (top-N ranking and rebalance plan)."""
from __future__ import annotations

from src.portfolio_selection import (
    DEFAULT_PRIORITY_SYMBOLS,
    keep_top_n_names,
    merge_fresh_signals_with_held,
    plan_top_n_rebalance,
    sort_by_strength_desc,
    sort_by_strength_desc_priority,
)


def test_sort_by_strength_desc_order_and_tie_break() -> None:
    pairs = [("B", 1.0), ("A", 2.0), ("C", 2.0)]
    out = sort_by_strength_desc(pairs)
    assert out == [("A", 2.0), ("C", 2.0), ("B", 1.0)]


def test_sort_by_strength_desc_priority_tie() -> None:
    pairs = [("MSFT", 2.0), ("SPY", 2.0), ("QQQ", 2.0)]
    out = sort_by_strength_desc_priority(pairs, list(DEFAULT_PRIORITY_SYMBOLS))
    assert [x[0] for x in out] == ["SPY", "QQQ", "MSFT"]


def test_plan_top_n_rebalance_tie_breaks_with_priority_symbols() -> None:
    target, _, _ = plan_top_n_rebalance(
        fresh_signal_strength=[("MSFT", 2.0), ("SPY", 2.0), ("QQQ", 2.0)],
        held_eligible_longs=[],
        tracked={},
        max_positions=2,
        strength_jitter_max=0.0,
        priority_symbols=("SPY", "QQQ", "NVDA", "MSFT"),
    )
    assert target == ["SPY", "QQQ"]


def test_plan_top_n_rebalance_empty_priority_lexi_only() -> None:
    target, _, _ = plan_top_n_rebalance(
        fresh_signal_strength=[("B", 2.0), ("A", 2.0)],
        held_eligible_longs=[],
        tracked={},
        max_positions=2,
        strength_jitter_max=0.0,
        priority_symbols=(),
    )
    assert target == ["A", "B"]


def test_keep_top_n_names() -> None:
    ranked = [("A", 3.0), ("B", 2.0), ("C", 1.0)]
    assert keep_top_n_names(ranked, 2) == ["A", "B"]
    assert keep_top_n_names(ranked, 0) == []
    assert keep_top_n_names(ranked, 10) == ["A", "B", "C"]


def test_merge_fresh_signals_with_held_adds_tracked_strength() -> None:
    tracked = {
        "MSFT": {"signal_strength": 0.5},
        "ZZZ": {"signal_strength": 1.2},
    }
    merged = merge_fresh_signals_with_held(
        [("AAPL", 2.0)],
        ["MSFT", "ZZZ"],
        tracked,
        strength_jitter_max=0.0,
    )
    d = dict(merged)
    assert d["AAPL"] == 2.0
    assert d["MSFT"] == 0.5
    assert d["ZZZ"] == 1.2


def test_plan_top_n_rebalance_demote_and_promote() -> None:
    tracked = {
        "WEAK": {"signal_strength": 0.1},
        "KEEP": {"signal_strength": 5.0},
    }
    target, to_sell, to_buy = plan_top_n_rebalance(
        fresh_signal_strength=[("NEW", 9.0)],
        held_eligible_longs=["WEAK", "KEEP"],
        tracked=tracked,
        max_positions=2,
        strength_jitter_max=0.0,
    )
    assert set(target) == {"NEW", "KEEP"}
    assert to_sell == {"WEAK"}
    assert to_buy == ["NEW"]


def test_plan_top_n_rebalance_held_only_drops_weakest() -> None:
    tracked = {
        "A": {"signal_strength": 3.0},
        "B": {"signal_strength": 1.0},
        "C": {"signal_strength": 2.0},
    }
    target, to_sell, to_buy = plan_top_n_rebalance(
        fresh_signal_strength=[],
        held_eligible_longs=["A", "B", "C"],
        tracked=tracked,
        max_positions=2,
        strength_jitter_max=0.0,
    )
    assert target == ["A", "C"]
    assert to_sell == {"B"}
    assert to_buy == []
