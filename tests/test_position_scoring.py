"""Tests for :mod:`src.position_scoring`."""

from __future__ import annotations

import pandas as pd
import pytest

from types import SimpleNamespace

from src.position_scoring import (
    COOLDOWN_BYPASS_MIN_SIGNAL_SCORE,
    PositionScoreComponents,
    composite_position_score,
    pnl_score_01,
    strength_score,
    position_dict_for_signal_score,
    position_score,
    position_score_weighted,
    score_position,
    weighted_composite_position_score,
    weakest_position_score_momentum_pnl_trend,
)


def test_score_position_base_no_market() -> None:
    assert score_position({"symbol": "SPY", "unrealized_plpc": 0.0}, {}) == 50


@pytest.mark.parametrize(
    "plpc,expected_min,expected_max",
    [
        (0.04, 70, 70),  # >3% -> +20
        (0.015, 60, 60),  # >1% -> +10
        (-0.025, 30, 30),  # <-2% -> -20
        (-0.015, 40, 40),  # <-1% -> -10
    ],
)
def test_score_position_pnl_buckets(plpc: float, expected_min: int, expected_max: int) -> None:
    s = score_position({"symbol": "SPY", "unrealized_plpc": plpc}, {})
    assert s == expected_min == expected_max


def test_score_position_strong_trend_and_momentum_decay() -> None:
    md = {"SPY": {"close": 105.0, "ma_fast": 100.0, "ma_slow": 95.0}}
    s = score_position({"symbol": "SPY", "unrealized_plpc": 0.0, "bars_held": 0}, md)
    assert s == 50 + 15

    md2 = {"SPY": {"close": 90.0, "ma_fast": 100.0, "ma_slow": 95.0}}
    s2 = score_position({"symbol": "SPY", "unrealized_plpc": 0.0, "bars_held": 0}, md2)
    assert s2 == 50 - 15 - 10


def test_score_position_holding_time_penalties() -> None:
    assert score_position({"symbol": "SPY", "unrealized_plpc": 0.0, "bars_held": 21}, {}) == 40
    assert score_position({"symbol": "SPY", "unrealized_plpc": 0.0, "bars_held": 41}, {}) == 20


def test_score_position_clamped_high_and_low() -> None:
    md_hi = {"SPY": {"close": 200.0, "ma_fast": 100.0, "ma_slow": 90.0}}
    s = score_position({"symbol": "SPY", "unrealized_plpc": 0.05, "bars_held": 0}, md_hi)
    assert s == 85  # 50 + 20 (pnl>3%) + 15 (stacked MAs)

    md_lo = {"SPY": {"close": 70.0, "ma_fast": 100.0, "ma_slow": 95.0}}
    s2 = score_position({"symbol": "SPY", "unrealized_plpc": -0.03, "bars_held": 50}, md_lo)
    assert s2 == 0  # heavy penalties clamp to floor


def test_score_position_symbol_case_insensitive() -> None:
    md = {"spy": {"close": 105.0, "ma_fast": 100.0, "ma_slow": 95.0}}
    assert score_position({"symbol": "SPY", "unrealized_plpc": 0.0}, md) == 65


def test_score_position_missing_trend_skips_ma_rules() -> None:
    md = {"SPY": {"close": 105.0}}
    assert score_position({"symbol": "SPY", "unrealized_plpc": 0.0}, md) == 50


def test_score_position_fallback_intraday_plpc() -> None:
    assert score_position({"symbol": "QQQ", "unrealized_intraday_plpc": 0.02}, {}) == 60


def test_score_position_invalid_plpc_ignored() -> None:
    assert score_position({"symbol": "SPY", "unrealized_plpc": "n/a"}, {}) == 50


def test_score_position_price_above_fast_but_not_slow_stack() -> None:
    md = {"SPY": {"close": 100.0, "ma_fast": 99.0, "ma_slow": 100.0}}
    assert score_position({"symbol": "SPY", "unrealized_plpc": 0.0}, md) == 50


def test_position_dict_for_signal_score_plpc_and_cost_basis() -> None:
    rows = [
        {
            "symbol": "QQQ",
            "unrealized_plpc": 0.02,
            "cost_basis": 1000.0,
        },
        {"symbol": "SPY", "unrealized_pl": 40.0, "cost_basis": 1000.0},
    ]
    assert position_dict_for_signal_score("SPY", rows) == {
        "symbol": "SPY",
        "unrealized_plpc": 0.04,
    }
    assert position_dict_for_signal_score("QQQ", rows) == {"symbol": "QQQ", "unrealized_plpc": 0.02}


def test_position_dict_for_signal_score_missing_returns_symbol_only() -> None:
    assert position_dict_for_signal_score("IWM", []) == {"symbol": "IWM"}


def test_cooldown_bypass_threshold_is_85() -> None:
    assert COOLDOWN_BYPASS_MIN_SIGNAL_SCORE == 85


def test_pnl_score_01_flat_is_mid() -> None:
    assert pnl_score_01({"symbol": "SPY", "unrealized_plpc": 0.0}) == pytest.approx(0.5)


def test_strength_score_is_momentum_pnl_leg_trend() -> None:
    """strength = momentum + pnl_leg + trend (spec *pnl_pct* → pnl_leg in [0,1])."""
    assert strength_score(0.2, 0.3, 0.1) == pytest.approx(0.6)
    assert strength_score(0.5, 0.5, 0.5) == pytest.approx(1.5)
    assert strength_score(1.0, 1.0, 1.0) == pytest.approx(3.0)
    # clamp each term to [0, 1]
    assert strength_score(2.0, -0.1, 0.5) == pytest.approx(1.0 + 0.0 + 0.5)


def test_weakest_position_score_matches_composite_total() -> None:
    n = 220
    df = pd.DataFrame(
        {
            "close": [100.0 + i * 0.3 for i in range(n)],
            "high": [101.0 + i * 0.3 for i in range(n)],
            "low": [99.0 + i * 0.3 for i in range(n)],
            "volume": [1e6] * n,
        }
    )
    pos = [{"symbol": "SPY", "unrealized_plpc": 0.04}]
    total, _ = composite_position_score("SPY", pos, df, ma_slow=200, momentum_bars=10, volume_bars=20)
    w = weakest_position_score_momentum_pnl_trend(
        "SPY", pos, df, ma_slow=200, momentum_bars=10, volume_bars=20
    )
    assert w == pytest.approx(total)


def test_composite_position_score_sum_of_three_terms() -> None:
    n = 220
    df = pd.DataFrame(
        {
            "close": [100.0 + i * 0.3 for i in range(n)],
            "high": [101.0 + i * 0.3 for i in range(n)],
            "low": [99.0 + i * 0.3 for i in range(n)],
            "volume": [1e6] * n,
        }
    )
    pos = [{"symbol": "SPY", "unrealized_plpc": 0.04}]
    total, bd = composite_position_score("SPY", pos, df, ma_slow=200, momentum_bars=10, volume_bars=20)
    assert bd["pnl_score"] == pytest.approx(pnl_score_01(position_dict_for_signal_score("SPY", pos)))
    assert (
        bd["score"]
        == pytest.approx(bd["pnl_score"] + bd["momentum_score"] + bd["trend_strength"])
    )
    assert total == pytest.approx(bd["score"])
    assert 0.0 <= total <= 3.0


def test_composite_position_score_no_bars_neutral_trend_momentum() -> None:
    pos = [{"symbol": "QQQ", "unrealized_plpc": 0.0}]
    total, bd = composite_position_score("QQQ", pos, None)
    assert bd["momentum_score"] == pytest.approx(0.5)
    assert bd["trend_strength"] == pytest.approx(0.5)
    assert total == pytest.approx(bd["pnl_score"] + 1.0)


def test_position_score_pos_object_and_mapping() -> None:
    ns = SimpleNamespace(unrealized_pl_pct=1.0, momentum_score=0.0, trend_score=0.5)
    assert position_score(ns) == pytest.approx(position_score_weighted(1.0, 0.0, 0.5))
    d = {"unrealized_pl_pct": 0.0, "momentum_score": 1.0, "trend_score": 1.0}
    assert position_score(d) == pytest.approx(position_score_weighted(0.0, 1.0, 1.0))


def test_position_score_weighted_all_mid() -> None:
    # 0.5 everywhere → 0.4*0.5 + 0.4*0.5 + 0.2*0.5 = 0.5
    assert position_score_weighted(0.5, 0.5, 0.5) == pytest.approx(0.5)


def test_position_score_weighted_corners() -> None:
    assert position_score_weighted(1.0, 1.0, 1.0) == pytest.approx(1.0)
    assert position_score_weighted(0.0, 0.0, 0.0) == pytest.approx(0.0)
    assert position_score_weighted(1.0, 0.0, 0.0) == pytest.approx(0.4)


def test_position_score_weighted_clamps_inputs() -> None:
    assert position_score_weighted(2.0, -1.0, 0.5) == pytest.approx(0.4 * 1.0 + 0.4 * 0.0 + 0.2 * 0.5)


def test_position_score_components_combined() -> None:
    pos = PositionScoreComponents(unrealized_pl_pct=1.0, momentum_score=0.0, trend_score=0.5)
    assert pos.combined() == pytest.approx(0.4 + 0.1)


def test_weighted_composite_matches_components_and_df() -> None:
    n = 220
    df = pd.DataFrame(
        {
            "close": [100.0 + i * 0.3 for i in range(n)],
            "high": [101.0 + i * 0.3 for i in range(n)],
            "low": [99.0 + i * 0.3 for i in range(n)],
            "volume": [1e6] * n,
        }
    )
    pos = [{"symbol": "SPY", "unrealized_plpc": 0.04}]
    pnl = pnl_score_01(position_dict_for_signal_score("SPY", pos))
    wtot, wbd = weighted_composite_position_score("SPY", pos, df, ma_slow=200, momentum_bars=10, volume_bars=20)
    assert wbd["pnl_score"] == pytest.approx(pnl)
    manual = position_score_weighted(pnl, wbd["momentum_score"], wbd["trend_strength"])
    assert wtot == pytest.approx(manual)
    assert wbd["weighted_score"] == pytest.approx(wtot)
    assert 0.0 <= wtot <= 1.0


def test_weighted_composite_no_bars() -> None:
    pos = [{"symbol": "QQQ", "unrealized_plpc": 0.0}]
    wtot, wbd = weighted_composite_position_score("QQQ", pos, None)
    assert wbd["momentum_score"] == pytest.approx(0.5)
    assert wbd["trend_strength"] == pytest.approx(0.5)
    assert wtot == pytest.approx(position_score_weighted(wbd["pnl_score"], 0.5, 0.5))
