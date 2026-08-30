"""Tests for :mod:`src.portfolio_score_replacement`."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.portfolio_score_replacement import (
    SWAP_POSITION_SCORE_CLASSIC,
    SWAP_POSITION_SCORE_WEIGHTED_POSITION,
    build_entry_swap_signal_map,
    evaluate_score_based_portfolio_swap,
    evaluate_strength_based_portfolio_swap,
    parse_swap_position_score_mode,
    swap_score_threshold,
)


def test_parse_swap_position_score_mode() -> None:
    assert parse_swap_position_score_mode(None, None) == SWAP_POSITION_SCORE_CLASSIC
    assert (
        parse_swap_position_score_mode({"swap_position_score": "weighted_position"}, {})
        == SWAP_POSITION_SCORE_WEIGHTED_POSITION
    )
    assert parse_swap_position_score_mode({"position_swap_score": "weighted"}, {}) == SWAP_POSITION_SCORE_WEIGHTED_POSITION
    assert (
        parse_swap_position_score_mode({}, {"swap_position_score": "weighted_position"})
        == SWAP_POSITION_SCORE_WEIGHTED_POSITION
    )
    assert (
        parse_swap_position_score_mode({}, {"replacement": {"swap_position_score": "position_score"}})
        == SWAP_POSITION_SCORE_WEIGHTED_POSITION
    )


def test_swap_score_threshold_default() -> None:
    assert swap_score_threshold(None) == 10
    assert swap_score_threshold({}) == 10
    assert swap_score_threshold({"swap_score_threshold": 15}) == 15
    assert swap_score_threshold(None, {"swap_threshold": 7}) == 7
    assert swap_score_threshold({"swap_score_threshold": 99}, {"swap_threshold": 5}) == 5


def test_build_entry_swap_signal_map_empty_df() -> None:
    engine = MagicMock()
    regime = MagicMock(score=4, condition="bullish")
    q = MagicMock(spread_pct=0.1)
    m = build_entry_swap_signal_map(
        engine, "SPY", None, 0.1, 1.0, regime_score=4, regime_result=regime, quote=q
    )
    assert m["regime_ok"] is True and m["spread_ok"] is True
    assert m["trend"] is False


def test_evaluate_swap_replaces_when_new_score_clearly_higher() -> None:
    engine = MagicMock()
    engine.strategy.ma_fast = 5
    engine.strategy.ma_slow = 10
    engine.strategy.entry_eval_components_for_log.return_value = (True, True, True, True)
    n = 25
    base = 200.0
    df = pd.DataFrame(
        {
            "close": [base + i * 0.2 for i in range(n)],
            "high": [base + i * 0.2 + 0.5 for i in range(n)],
            "low": [base + i * 0.2 - 0.5 for i in range(n)],
            "volume": [1e6] * n,
        }
    )
    broker = MagicMock()
    broker.get_bars.return_value = df
    positions = [
        {"symbol": "AAA", "market_value": 1000.0, "unrealized_plpc": -0.05},
        {"symbol": "BBB", "market_value": 1000.0, "unrealized_plpc": -0.05},
    ]
    tracked = {
        "AAA": {"qty": 1, "entry_time": "2024-01-01T10:00:00+00:00"},
        "BBB": {"qty": 1, "entry_time": "2024-01-01T10:00:00+00:00"},
    }
    dt = datetime(2024, 1, 15, 15, 0, 0, tzinfo=timezone.utc)
    wsym, new_sc, weak_sc, th, skip = evaluate_score_based_portfolio_swap(
        incoming_sym_upper="ZZZ",
        engine=engine,
        broker=broker,
        df=df,
        atr_pct=1.0,
        quote=MagicMock(spread_pct=0.1),
        spread_pct=0.1,
        regime_result=MagicMock(score=4, condition="bullish"),
        entry_regime_score=4,
        eligible_active=["AAA", "BBB"],
        tracked=tracked,
        positions=positions,
        dt=dt,
        rep_sub={"min_hold_minutes": 0},
        portfolio_cfg={},
    )
    assert skip is None
    assert wsym in ("AAA", "BBB")
    assert new_sc == 100
    assert th == 10
    assert new_sc > weak_sc + th


def test_evaluate_swap_skips_when_scores_too_close() -> None:
    engine = MagicMock()
    engine.strategy.ma_fast = 5
    engine.strategy.ma_slow = 10
    engine.strategy.entry_eval_components_for_log.return_value = (False, False, False, False)
    n = 25
    df = pd.DataFrame(
        {
            "close": [200.0 + i * 0.5 for i in range(n)],
            "high": [201.0 + i * 0.5 for i in range(n)],
            "low": [199.0 + i * 0.5 for i in range(n)],
            "volume": [1e6] * n,
        }
    )
    broker = MagicMock()
    broker.get_bars = MagicMock(return_value=df)
    positions = [{"symbol": "AAA", "market_value": 1000.0, "unrealized_plpc": 0.05}]
    tracked = {"AAA": {"qty": 1, "entry_time": "2024-01-01T10:00:00+00:00"}}
    dt = datetime(2024, 1, 15, 15, 0, 0, tzinfo=timezone.utc)
    wsym, new_sc, weak_sc, th, skip = evaluate_score_based_portfolio_swap(
        incoming_sym_upper="ZZZ",
        engine=engine,
        broker=broker,
        df=df,
        atr_pct=1.0,
        quote=MagicMock(spread_pct=0.1),
        spread_pct=0.1,
        regime_result=MagicMock(score=4, condition="bullish"),
        entry_regime_score=4,
        eligible_active=["AAA"],
        tracked=tracked,
        positions=positions,
        dt=dt,
        rep_sub={"min_hold_minutes": 0},
        portfolio_cfg={"swap_threshold": 50},
    )
    assert wsym is None
    assert skip is not None
    assert "better positions already held" in (skip or "")
    assert new_sc <= weak_sc + th


def _old_entry_iso() -> str:
    return datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc).isoformat()


def _recent_entry_iso() -> str:
    return datetime(2024, 2, 20, 14, 45, 0, tzinfo=timezone.utc).isoformat()


def test_evaluate_strength_swap_replaces_when_gap_clear() -> None:
    dt = datetime(2024, 2, 20, 15, 0, 0, tzinfo=timezone.utc)
    tracked = {
        "AAA": {"qty": 1, "signal_strength": 0.9, "entry_time": _old_entry_iso()},
        "BBB": {"qty": 1, "signal_strength": 0.5, "entry_time": _old_entry_iso()},
    }
    positions = [
        {"symbol": "AAA", "qty": 1, "market_value": 2000.0},
        {"symbol": "BBB", "qty": 1, "market_value": 900.0},
    ]
    decision = SimpleNamespace(entry_signal=SimpleNamespace(strength=1.0))
    wsym, skip = evaluate_strength_based_portfolio_swap(
        incoming_sym_upper="ZZZ",
        decision=decision,
        tracked=tracked,
        eligible_active=["AAA", "BBB"],
        positions=positions,
        dt=dt,
        rep_sub={
            "min_hold_minutes": 0,
            "min_market_value_to_replace_usd": 750,
            "min_notional_for_incoming_usd": 100,
        },
        strength_jitter_max=0.0,
        replace_if_weakest_older_than_bars=8,
        max_position_age_bars=20,
        allow_equal_replacement=False,
        strength_gap=0.25,
        incoming_notional_usd=800.0,
    )
    assert skip is None
    assert wsym == ["BBB"]


def test_evaluate_strength_swap_young_position_exceptional_override() -> None:
    dt = datetime(2024, 2, 20, 15, 0, 0, tzinfo=timezone.utc)
    tracked = {
        "META": {"qty": 1, "signal_strength": 0.25, "entry_time": _recent_entry_iso()},
    }
    positions = [{"symbol": "META", "qty": 1, "market_value": 1200.0}]
    decision = SimpleNamespace(entry_signal=SimpleNamespace(strength=1.0))
    wsym, skip = evaluate_strength_based_portfolio_swap(
        incoming_sym_upper="OPEN",
        decision=decision,
        tracked=tracked,
        eligible_active=["META"],
        positions=positions,
        dt=dt,
        rep_sub={
            "min_hold_minutes": 0,
            "min_market_value_to_replace_usd": 750,
            "min_notional_for_incoming_usd": 100,
            "young_position_exceptional_override": {
                "enabled": True,
                "min_strength_gap": 0.50,
                "min_incoming_strength": 0.85,
            },
        },
        strength_jitter_max=0.0,
        replace_if_weakest_older_than_bars=8,
        max_position_age_bars=20,
        allow_equal_replacement=False,
        strength_gap=0.25,
        incoming_notional_usd=800.0,
    )
    assert skip is None
    assert wsym == ["META"]


def test_evaluate_strength_swap_health_pick_overrides_tracked_strength() -> None:
    """Tracked strength would favor rotating AAA — health composite should pick BBB as weakest."""
    dt = datetime(2024, 2, 20, 15, 0, 0, tzinfo=timezone.utc)
    tracked = {
        "AAA": {"qty": 1, "signal_strength": 0.15, "entry_time": _old_entry_iso()},
        "BBB": {"qty": 1, "signal_strength": 0.98, "entry_time": _old_entry_iso()},
    }
    positions = [
        {"symbol": "AAA", "qty": 1, "market_value": 2000.0, "unrealized_plpc": 0.07},
        {"symbol": "BBB", "qty": 1, "market_value": 900.0, "unrealized_plpc": -0.15},
    ]
    n = 220
    df_ok = pd.DataFrame(
        {
            "close": [80.0 + i * 0.5 for i in range(n)],
            "high": [81.0 + i * 0.5 for i in range(n)],
            "low": [79.0 + i * 0.5 for i in range(n)],
            "volume": [1e6] * n,
        }
    )
    df_bad = pd.DataFrame(
        {
            "close": [150.0 - min(i, 120) * 0.08 for i in range(n)],
            "high": [151.0 - min(i, 120) * 0.08 for i in range(n)],
            "low": [148.0 - min(i, 120) * 0.08 for i in range(n)],
            "volume": [4e5] * n,
        }
    )

    broker = MagicMock()

    def _bars(sym: str, **_kwargs: object) -> pd.DataFrame:
        return df_bad if str(sym).upper() == "BBB" else df_ok

    broker.get_bars.side_effect = _bars

    decision = SimpleNamespace(entry_signal=SimpleNamespace(strength=1.0))
    wsym, skip = evaluate_strength_based_portfolio_swap(
        incoming_sym_upper="ZZZ",
        decision=decision,
        tracked=tracked,
        eligible_active=["AAA", "BBB"],
        positions=positions,
        dt=dt,
        rep_sub={
            "min_hold_minutes": 0,
            "min_market_value_to_replace_usd": 750,
            "min_notional_for_incoming_usd": 100,
            "weakest_pick": "pnl_momentum_trend",
        },
        strength_jitter_max=0.0,
        replace_if_weakest_older_than_bars=8,
        max_position_age_bars=20,
        allow_equal_replacement=False,
        strength_gap=0.25,
        incoming_notional_usd=800.0,
        broker=broker,
        engine=None,
    )
    assert skip is None
    assert wsym == ["BBB"]


def test_evaluate_strength_swap_skips_low_market_value() -> None:
    dt = datetime(2024, 2, 20, 15, 0, 0, tzinfo=timezone.utc)
    tracked = {
        "BBB": {"qty": 1, "signal_strength": 0.5, "entry_time": _old_entry_iso()},
    }
    positions = [{"symbol": "BBB", "qty": 1, "market_value": 100.0}]
    decision = SimpleNamespace(entry_signal=SimpleNamespace(strength=2.0))
    wsym, skip = evaluate_strength_based_portfolio_swap(
        incoming_sym_upper="ZZZ",
        decision=decision,
        tracked=tracked,
        eligible_active=["BBB"],
        positions=positions,
        dt=dt,
        rep_sub={
            "min_hold_minutes": 0,
            "min_market_value_to_replace_usd": 750,
            "min_notional_for_incoming_usd": 0,
        },
        strength_jitter_max=0.0,
        replace_if_weakest_older_than_bars=8,
        max_position_age_bars=20,
        allow_equal_replacement=False,
        strength_gap=0.25,
        incoming_notional_usd=800.0,
    )
    assert wsym is None
    assert skip is not None
    assert "weakest position too small" in (skip or "")


def test_evaluate_strength_swap_rotate_on_stronger_skips_gap_threshold() -> None:
    """rotate_on_stronger_signal: replace when incoming > weakest only (replacement_threshold gap ignored)."""
    dt = datetime(2024, 2, 20, 15, 0, 0, tzinfo=timezone.utc)
    tracked = {
        "BBB": {"qty": 1, "signal_strength": 0.5, "entry_time": _old_entry_iso()},
    }
    positions = [{"symbol": "BBB", "qty": 1, "market_value": 900.0}]
    decision = SimpleNamespace(entry_signal=SimpleNamespace(strength=0.7))
    wsym, skip = evaluate_strength_based_portfolio_swap(
        incoming_sym_upper="ZZZ",
        decision=decision,
        tracked=tracked,
        eligible_active=["BBB"],
        positions=positions,
        dt=dt,
        rep_sub={
            "min_hold_minutes": 0,
            "rotate_on_stronger_signal": True,
        },
        strength_jitter_max=0.0,
        replace_if_weakest_older_than_bars=9999,
        max_position_age_bars=20,
        allow_equal_replacement=False,
        strength_gap=0.25,
        incoming_notional_usd=800.0,
    )
    assert skip is None
    assert wsym == ["BBB"]


def test_evaluate_strength_swap_composite_needs_incoming_df() -> None:
    """composite_position_score strength path requires daily OHLCV for the incoming symbol."""
    dt = datetime(2024, 2, 20, 15, 0, 0, tzinfo=timezone.utc)
    n = 220
    df_ok = pd.DataFrame(
        {
            "close": [80.0 + i * 0.5 for i in range(n)],
            "high": [81.0 + i * 0.5 for i in range(n)],
            "low": [79.0 + i * 0.5 for i in range(n)],
            "volume": [1e6] * n,
        }
    )
    tracked = {
        "AAA": {"qty": 1, "signal_strength": 0.9, "entry_time": _old_entry_iso()},
        "BBB": {"qty": 1, "signal_strength": 0.9, "entry_time": _old_entry_iso()},
    }
    positions = [
        {"symbol": "AAA", "qty": 1, "market_value": 2000.0, "unrealized_plpc": 0.05},
        {"symbol": "BBB", "qty": 1, "market_value": 900.0, "unrealized_plpc": -0.05},
    ]
    broker = MagicMock()
    broker.get_bars.return_value = df_ok
    decision = SimpleNamespace(entry_signal=SimpleNamespace(strength=1.0))
    wsym, skip = evaluate_strength_based_portfolio_swap(
        incoming_sym_upper="ZZZ",
        decision=decision,
        tracked=tracked,
        eligible_active=["AAA", "BBB"],
        positions=positions,
        dt=dt,
        rep_sub={
            "min_hold_minutes": 0,
            "min_market_value_to_replace_usd": 750,
            "min_notional_for_incoming_usd": 100,
            "weakest_pick": "composite_position_score",
        },
        strength_jitter_max=0.0,
        replace_if_weakest_older_than_bars=8,
        max_position_age_bars=20,
        allow_equal_replacement=False,
        strength_gap=0.05,
        incoming_notional_usd=800.0,
        broker=broker,
        engine=None,
        df=None,
    )
    assert wsym is None
    assert skip is not None
    assert "composite_position_score swap needs daily OHLCV" in (skip or "")


def test_evaluate_strength_swap_composite_pick_and_incoming_gate() -> None:
    """Weakest by raw composite sum; rotate when normalized incoming beats weakest by strength_gap."""
    dt = datetime(2024, 2, 20, 15, 0, 0, tzinfo=timezone.utc)
    n = 220
    df_ok = pd.DataFrame(
        {
            "close": [80.0 + i * 0.5 for i in range(n)],
            "high": [81.0 + i * 0.5 for i in range(n)],
            "low": [79.0 + i * 0.5 for i in range(n)],
            "volume": [1e6] * n,
        }
    )
    df_bad = pd.DataFrame(
        {
            "close": [150.0 - min(i, 120) * 0.08 for i in range(n)],
            "high": [151.0 - min(i, 120) * 0.08 for i in range(n)],
            "low": [148.0 - min(i, 120) * 0.08 for i in range(n)],
            "volume": [4e5] * n,
        }
    )
    df_incoming = pd.DataFrame(
        {
            "close": [50.0 + i * 0.8 for i in range(n)],
            "high": [51.0 + i * 0.8 for i in range(n)],
            "low": [49.0 + i * 0.8 for i in range(n)],
            "volume": [2e6] * n,
        }
    )

    tracked = {
        "AAA": {"qty": 1, "signal_strength": 0.99, "entry_time": _old_entry_iso()},
        "BBB": {"qty": 1, "signal_strength": 0.01, "entry_time": _old_entry_iso()},
    }
    positions = [
        {"symbol": "AAA", "qty": 1, "market_value": 2000.0, "unrealized_plpc": 0.07},
        {"symbol": "BBB", "qty": 1, "market_value": 900.0, "unrealized_plpc": -0.15},
    ]

    broker = MagicMock()

    def _bars(sym: str, **_kwargs: object) -> pd.DataFrame:
        return df_bad if str(sym).upper() == "BBB" else df_ok

    broker.get_bars.side_effect = _bars

    decision = SimpleNamespace(entry_signal=SimpleNamespace(strength=1.0))
    wsym, skip = evaluate_strength_based_portfolio_swap(
        incoming_sym_upper="ZZZ",
        decision=decision,
        tracked=tracked,
        eligible_active=["AAA", "BBB"],
        positions=positions,
        dt=dt,
        rep_sub={
            "min_hold_minutes": 0,
            "min_market_value_to_replace_usd": 750,
            "min_notional_for_incoming_usd": 100,
            "weakest_pick": "composite_position_score",
        },
        strength_jitter_max=0.0,
        replace_if_weakest_older_than_bars=8,
        max_position_age_bars=20,
        allow_equal_replacement=False,
        strength_gap=0.05,
        incoming_notional_usd=800.0,
        broker=broker,
        engine=None,
        df=df_incoming,
    )
    assert skip is None
    assert wsym == ["BBB"]


def test_evaluate_strength_swap_skips_when_incoming_not_strong_enough() -> None:
    dt = datetime(2024, 2, 20, 15, 0, 0, tzinfo=timezone.utc)
    tracked = {
        "BBB": {"qty": 1, "signal_strength": 0.5, "entry_time": _old_entry_iso()},
    }
    positions = [{"symbol": "BBB", "qty": 1, "market_value": 900.0}]
    decision = SimpleNamespace(entry_signal=SimpleNamespace(strength=0.7))
    wsym, skip = evaluate_strength_based_portfolio_swap(
        incoming_sym_upper="ZZZ",
        decision=decision,
        tracked=tracked,
        eligible_active=["BBB"],
        positions=positions,
        dt=dt,
        rep_sub={"min_hold_minutes": 0},
        strength_jitter_max=0.0,
        replace_if_weakest_older_than_bars=9999,
        max_position_age_bars=20,
        allow_equal_replacement=False,
        strength_gap=0.25,
        incoming_notional_usd=800.0,
    )
    assert wsym is None
    assert skip is not None
    assert "insufficient strength improvement" in (skip or "")


def test_evaluate_score_swap_weighted_min_position_score(monkeypatch: pytest.MonkeyPatch) -> None:
    """Weakest = min(eligible, key=weighted composite); rotate when score_signal clears gap on 0-100 scale."""
    import src.portfolio_score_replacement as psr

    monkeypatch.setattr(psr, "build_entry_swap_signal_map", lambda *a, **kw: {})
    monkeypatch.setattr(psr, "score_signal", lambda m: 60)
    monkeypatch.setattr(
        psr,
        "score_eligible_weighted_positions_for_swap",
        lambda *a, **kw: [("AAA", 0.95), ("BBB", 0.31)],
    )
    dt = datetime(2024, 1, 15, 15, 0, 0, tzinfo=timezone.utc)
    wsym, new_sc, weak_sc, th, skip = psr.evaluate_score_based_portfolio_swap(
        incoming_sym_upper="ZZZ",
        engine=MagicMock(),
        broker=MagicMock(),
        df=MagicMock(),
        atr_pct=1.0,
        quote=MagicMock(),
        spread_pct=0.1,
        regime_result=MagicMock(score=4, condition="bullish"),
        entry_regime_score=4,
        eligible_active=["AAA", "BBB"],
        tracked={
            "AAA": {"qty": 1, "entry_time": "2024-01-01T10:00:00+00:00"},
            "BBB": {"qty": 1, "entry_time": "2024-01-01T10:00:00+00:00"},
        },
        positions=[
            {"symbol": "AAA", "market_value": 1000.0, "unrealized_plpc": 0.0},
            {"symbol": "BBB", "market_value": 1000.0, "unrealized_plpc": 0.0},
        ],
        dt=dt,
        rep_sub={"min_hold_minutes": 0, "swap_position_score": "weighted_position"},
        portfolio_cfg={"swap_threshold": 10},
    )
    assert skip is None
    assert wsym == "BBB"
    assert new_sc == 60
    assert weak_sc == 31
    assert th == 10


def test_evaluate_score_swap_weighted_tie_break_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    """Equal weighted scores: lexicographic symbol tie-break (min key (score, sym))."""
    import src.portfolio_score_replacement as psr

    monkeypatch.setattr(psr, "build_entry_swap_signal_map", lambda *a, **kw: {})
    monkeypatch.setattr(psr, "score_signal", lambda m: 100)
    monkeypatch.setattr(
        psr,
        "score_eligible_weighted_positions_for_swap",
        lambda *a, **kw: [("ZZZ", 0.5), ("AAA", 0.5)],
    )
    dt = datetime(2024, 1, 15, 15, 0, 0, tzinfo=timezone.utc)
    wsym, *_rest = psr.evaluate_score_based_portfolio_swap(
        incoming_sym_upper="NEW",
        engine=MagicMock(),
        broker=MagicMock(),
        df=MagicMock(),
        atr_pct=1.0,
        quote=MagicMock(),
        spread_pct=0.1,
        regime_result=MagicMock(score=4, condition="bullish"),
        entry_regime_score=4,
        eligible_active=["ZZZ", "AAA"],
        tracked={
            "ZZZ": {"qty": 1, "entry_time": "2024-01-01T10:00:00+00:00"},
            "AAA": {"qty": 1, "entry_time": "2024-01-01T10:00:00+00:00"},
        },
        positions=[
            {"symbol": "ZZZ", "market_value": 1000.0},
            {"symbol": "AAA", "market_value": 1000.0},
        ],
        dt=dt,
        rep_sub={"min_hold_minutes": 0, "swap_position_score": "weighted", "rotate_on_stronger_signal": True},
        portfolio_cfg={},
    )
    assert wsym == "AAA"


def test_evaluate_score_swap_rotate_on_stronger_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Score path: new > weakest but not by swap gap — still rotates when rotate_on_stronger_signal."""
    import src.portfolio_score_replacement as psr

    monkeypatch.setattr(psr, "build_entry_swap_signal_map", lambda *a, **kw: {})
    monkeypatch.setattr(psr, "score_signal", lambda m: 85)
    monkeypatch.setattr(
        psr,
        "score_eligible_positions_for_swap",
        lambda *a, **kw: [("AAA", 80)],
    )
    dt = datetime(2024, 1, 15, 15, 0, 0, tzinfo=timezone.utc)
    wsym, new_sc, weak_sc, th, skip = psr.evaluate_score_based_portfolio_swap(
        incoming_sym_upper="ZZZ",
        engine=MagicMock(),
        broker=MagicMock(),
        df=MagicMock(),
        atr_pct=1.0,
        quote=MagicMock(),
        spread_pct=0.1,
        regime_result=MagicMock(score=4, condition="bullish"),
        entry_regime_score=4,
        eligible_active=["AAA"],
        tracked={"AAA": {"qty": 1, "entry_time": "2024-01-01T10:00:00+00:00"}},
        positions=[{"symbol": "AAA", "market_value": 1000.0, "unrealized_plpc": 0.0}],
        dt=dt,
        rep_sub={"min_hold_minutes": 0, "rotate_on_stronger_signal": True},
        portfolio_cfg={},
    )
    assert skip is None
    assert wsym == "AAA"
    assert new_sc == 85 and weak_sc == 80 and th == 10


def test_plan_replace_losers_with_winners_stack_funds_incoming_from_two_laggards() -> None:
    """``replace_losers_with_winners``: sell XLF+WMT until cumulative MV covers incoming notional."""
    from src.portfolio_score_replacement import plan_replace_losers_with_winners_stack

    dt = datetime(2024, 2, 20, 15, 0, 0, tzinfo=timezone.utc)
    et = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc).isoformat()
    tracked = {
        "XLF": {"qty": 10, "signal_strength": 0.2, "entry_time": et},
        "WMT": {"qty": 5, "signal_strength": 0.25, "entry_time": et},
    }
    positions = [
        {"symbol": "XLF", "qty": 10, "market_value": 400.0},
        {"symbol": "WMT", "qty": 5, "market_value": 500.0},
    ]
    rep = {
        "min_hold_minutes": 0,
        "rotate_on_stronger_signal": True,
        "replace_losers_with_winners": {"enabled": True, "max_sells": 2, "prefer_sell_count": 0},
        "min_market_value_to_replace_usd": 100,
        "min_notional_for_incoming_usd": 800,
    }
    out = plan_replace_losers_with_winners_stack(
        su="NVDA",
        candidates_asc=[("XLF", 0.2), ("WMT", 0.25)],
        inc_cmp=1.0,
        is_composite=False,
        tracked=tracked,
        positions=positions,
        dt=dt,
        rep=rep,
        strength_jitter_max=0.0,
        replace_if_weakest_older_than_bars=8,
        max_position_age_bars=1,
        allow_equal_replacement=False,
        strength_gap=0.25,
        incoming_notional_usd=800.0,
    )
    assert out == ["XLF", "WMT"]
