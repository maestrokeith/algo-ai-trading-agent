"""Tests for :func:`src.portfolio_replacement.consider_replacement` (Step 2 min P/L vs strength)."""

from __future__ import annotations

import pytest

from src.portfolio_replacement import (
    REPLACEMENT_STRATEGY_MIN_UNREALIZED_PL_VS_SIGNAL,
    consider_replacement,
    parse_replacement_strategy,
    position_strength_hold,
    replacement_churn_guard_min_new_vs_weakest_ratio,
    replacement_incoming_strong_enough_vs_weakest,
    replacement_is_strong_entry_eval,
    replacement_is_stronger_incoming_vs_weakest,
    replacement_weakest_row_by_unrealized_pl_pct,
    unrealized_pl_frac_for_sort,
)
from src.strategy import EntrySignal
from src.trading_engine import TradeDecision


def _sig(strength: float = 1.0) -> EntrySignal:
    return EntrySignal(
        symbol="ZZZ",
        side="long",
        strength=strength,
        stop_pct=1.5,
        take_profit_pct=3.0,
        time_bars_exit=20,
        metadata={},
    )


def test_unrealized_pl_frac_for_sort_prefers_plpc() -> None:
    p = {"symbol": "SPY", "unrealized_plpc": -0.02, "qty": 1}
    assert unrealized_pl_frac_for_sort(p) == pytest.approx(-0.02)


def test_replacement_weakest_row_by_unrealized_pl_pct() -> None:
    rows = [
        {"symbol": "AAA", "unrealized_plpc": 0.02},
        {"symbol": "BBB", "unrealized_plpc": -0.12},
    ]
    w = replacement_weakest_row_by_unrealized_pl_pct(rows)
    assert w is not None and w["symbol"] == "BBB"


def test_replacement_incoming_strong_enough_vs_weakest() -> None:
    weakest = {"symbol": "BBB", "qty": 1, "unrealized_plpc": 0.0, "side": "long"}
    d = TradeDecision(
        allowed=True,
        reason="ok",
        entry_signal=_sig(0.55),
        position_sizing=None,
    )
    assert not replacement_incoming_strong_enough_vs_weakest(
        d, weakest, strength_jitter_max=0.0, churn_guard_min_new_vs_weakest_ratio=1.2
    )
    assert replacement_incoming_strong_enough_vs_weakest(
        d, weakest, strength_jitter_max=0.0, churn_guard_min_new_vs_weakest_ratio=1.0
    )


def test_replacement_is_strong_entry_eval() -> None:
    assert replacement_is_strong_entry_eval(True, True, True) is True
    assert replacement_is_strong_entry_eval(True, False, True) is False
    assert replacement_is_strong_entry_eval(None, True, True) is None
    assert replacement_is_strong_entry_eval(True, True, None) is None


def test_replacement_is_stronger_incoming_vs_weakest() -> None:
    pos_weak = {"symbol": "BBB", "qty": 1, "unrealized_plpc": 0.05, "side": "long"}
    assert replacement_is_stronger_incoming_vs_weakest(True, True, True, pos_weak) is True
    pos_hot = {"symbol": "BBB", "qty": 1, "unrealized_plpc": 1.05, "side": "long"}
    assert replacement_is_stronger_incoming_vs_weakest(True, True, True, pos_hot) is False
    assert replacement_is_stronger_incoming_vs_weakest(True, None, True, pos_weak) is None


def test_is_stronger_blocks_when_weakest_pl_fraction_ge_one() -> None:
    weakest = {"symbol": "BBB", "qty": 1, "unrealized_plpc": 1.2, "side": "long"}
    d = TradeDecision(allowed=True, reason="ok", entry_signal=_sig(1.0), position_sizing=None)
    assert not replacement_incoming_strong_enough_vs_weakest(
        d,
        weakest,
        strength_jitter_max=0.0,
        churn_guard_min_new_vs_weakest_ratio=1.0,
        entry_eval_trend=True,
        entry_eval_momentum=True,
        entry_eval_pullback=True,
    )


def test_structural_strong_blocks_even_when_numeric_would_pass() -> None:
    weakest = {"symbol": "BBB", "qty": 1, "unrealized_plpc": 0.0, "side": "long"}
    d = TradeDecision(allowed=True, reason="ok", entry_signal=_sig(1.0), position_sizing=None)
    assert not replacement_incoming_strong_enough_vs_weakest(
        d,
        weakest,
        strength_jitter_max=0.0,
        churn_guard_min_new_vs_weakest_ratio=1.0,
        entry_eval_trend=True,
        entry_eval_momentum=True,
        entry_eval_pullback=False,
    )


def test_structural_strong_requires_numeric_too() -> None:
    weakest = {"symbol": "BBB", "qty": 1, "unrealized_plpc": 0.0, "side": "long"}
    weak_in = TradeDecision(allowed=True, reason="ok", entry_signal=_sig(0.01), position_sizing=None)
    assert not replacement_incoming_strong_enough_vs_weakest(
        weak_in,
        weakest,
        strength_jitter_max=0.0,
        churn_guard_min_new_vs_weakest_ratio=1.2,
        entry_eval_trend=True,
        entry_eval_momentum=True,
        entry_eval_pullback=True,
    )


def test_consider_replacement_partial_entry_eval_skips_structural_gate() -> None:
    """Any None among entry_eval_* → structural gate skipped; numeric-only (ratio 1.0)."""
    positions = [{"symbol": "BBB", "qty": 1, "unrealized_plpc": 0.0, "side": "long"}]
    d = TradeDecision(allowed=True, reason="ok", entry_signal=_sig(0.55), position_sizing=None)
    w = consider_replacement(
        d,
        positions=positions,
        eligible_symbols=["BBB"],
        incoming_sym_upper="ZZZ",
        strength_jitter_max=0.0,
        churn_guard_min_new_vs_weakest_ratio=1.0,
        entry_eval_trend=True,
        entry_eval_momentum=None,
        entry_eval_pullback=True,
    )
    assert w == "BBB"


def test_position_strength_hold_mid_for_flat() -> None:
    p = {"symbol": "SPY", "unrealized_plpc": 0.0, "qty": 1}
    assert 0.0 <= position_strength_hold(p) <= 1.0


def test_consider_replacement_picks_min_pl_and_strong_incoming() -> None:
    """Default 1.2 churn guard: weak BBB vs strong incoming still clears weakest * ratio."""
    positions = [
        {"symbol": "AAA", "qty": 10, "unrealized_plpc": 0.02, "side": "long"},
        {"symbol": "BBB", "qty": 5, "unrealized_plpc": -0.12, "side": "long"},
        {"symbol": "CCC", "qty": 1, "unrealized_plpc": 0.0, "side": "long"},
    ]
    d = TradeDecision(
        allowed=True,
        reason="ok",
        entry_signal=_sig(1.0),
        position_sizing=None,
    )
    w = consider_replacement(
        d,
        positions=positions,
        eligible_symbols=["AAA", "BBB", "CCC"],
        incoming_sym_upper="ZZZ",
        strength_jitter_max=0.0,
        churn_guard_min_new_vs_weakest_ratio=1.2,
    )
    assert w == "BBB"


def test_consider_replacement_none_when_incoming_not_stronger() -> None:
    positions = [
        {"symbol": "BBB", "qty": 5, "unrealized_plpc": -0.01, "side": "long"},
    ]
    d = TradeDecision(
        allowed=True,
        reason="ok",
        entry_signal=_sig(0.01),
        position_sizing=None,
    )
    w = consider_replacement(
        d,
        positions=positions,
        eligible_symbols=["BBB"],
        incoming_sym_upper="ZZZ",
        strength_jitter_max=0.0,
        churn_guard_min_new_vs_weakest_ratio=1.2,
    )
    assert w is None


def test_consider_replacement_excludes_incoming_and_shorts() -> None:
    positions = [
        {"symbol": "ZZZ", "qty": 1, "unrealized_plpc": -0.5, "side": "long"},
        {"symbol": "YYY", "qty": 1, "unrealized_plpc": -0.1, "side": "short"},
    ]
    d = TradeDecision(allowed=True, reason="ok", entry_signal=_sig(1.0), position_sizing=None)
    w = consider_replacement(
        d,
        positions=positions,
        eligible_symbols=["ZZZ", "YYY"],
        incoming_sym_upper="ZZZ",
        strength_jitter_max=0.0,
        churn_guard_min_new_vs_weakest_ratio=1.2,
    )
    assert w is None


def test_churn_guard_blocks_marginal_upgrade() -> None:
    """Step 3: require new > weakest * 1.2 — marginal beat does not rotate."""
    positions = [{"symbol": "BBB", "qty": 1, "unrealized_plpc": 0.0, "side": "long"}]
    marginal = TradeDecision(
        allowed=True,
        reason="ok",
        entry_signal=_sig(0.55),
        position_sizing=None,
    )
    w = consider_replacement(
        marginal,
        positions=positions,
        eligible_symbols=["BBB"],
        incoming_sym_upper="ZZZ",
        strength_jitter_max=0.0,
        churn_guard_min_new_vs_weakest_ratio=1.2,
    )
    assert w is None
    clears = TradeDecision(
        allowed=True,
        reason="ok",
        entry_signal=_sig(0.61),
        position_sizing=None,
    )
    w2 = consider_replacement(
        clears,
        positions=positions,
        eligible_symbols=["BBB"],
        incoming_sym_upper="ZZZ",
        strength_jitter_max=0.0,
        churn_guard_min_new_vs_weakest_ratio=1.2,
    )
    assert w2 == "BBB"


def test_churn_guard_ratio_one_disables_margin() -> None:
    positions = [{"symbol": "BBB", "qty": 1, "unrealized_plpc": 0.0, "side": "long"}]
    d = TradeDecision(allowed=True, reason="ok", entry_signal=_sig(0.55), position_sizing=None)
    w = consider_replacement(
        d,
        positions=positions,
        eligible_symbols=["BBB"],
        incoming_sym_upper="ZZZ",
        strength_jitter_max=0.0,
        churn_guard_min_new_vs_weakest_ratio=1.0,
    )
    assert w == "BBB"


def test_replacement_churn_guard_ratio_parser() -> None:
    assert replacement_churn_guard_min_new_vs_weakest_ratio({}) == pytest.approx(1.2)
    assert replacement_churn_guard_min_new_vs_weakest_ratio({"churn_guard_min_new_vs_weakest_ratio": 1.0}) == 1.0
    assert replacement_churn_guard_min_new_vs_weakest_ratio({"churn_guard_min_new_vs_weakest_ratio": 1.5}) == pytest.approx(1.5)


def test_parse_replacement_strategy_aliases() -> None:
    assert parse_replacement_strategy(None) != REPLACEMENT_STRATEGY_MIN_UNREALIZED_PL_VS_SIGNAL
    assert parse_replacement_strategy({"strategy": "step2"}) == REPLACEMENT_STRATEGY_MIN_UNREALIZED_PL_VS_SIGNAL
    assert parse_replacement_strategy({"replacement_strategy": "min_pl"}) == REPLACEMENT_STRATEGY_MIN_UNREALIZED_PL_VS_SIGNAL
