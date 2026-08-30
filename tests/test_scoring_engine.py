"""Tests for ScoringEngine and ScoreBreakdown."""

from __future__ import annotations

import pytest

from src.scoring_engine import ScoreBreakdown, ScoringEngine


def _base_data(**overrides: float) -> dict[str, float]:
    d = {
        "price": 100.0,
        "ma20": 100.0,
        "ma50": 95.0,
        "ma200": 90.0,
        "volume": 1_000_000.0,
        "avg_volume": 1_000_000.0,
    }
    d.update(overrides)
    return d


def test_score_breakdown_total() -> None:
    b = ScoreBreakdown(trend=1.0, pullback=2.0, volume=3.0, regime=4.0)
    assert b.total == 10.0


def test_default_weights_full_bullish_regime() -> None:
    eng = ScoringEngine({})
    d = _base_data(volume=1_000_001.0)
    out = eng.score_symbol(d, regime_score=4)
    # trend: 2+2+1=5, pullback: 5 (on ma20), volume: 1 (ratio > 1.0), regime: 5
    assert out.trend == 5.0
    assert out.pullback == 5.0
    assert out.volume == 1.0
    assert out.regime == 5.0
    assert out.total == 16.0


@pytest.mark.parametrize(
    "regime, expected_regime_component",
    [(5, 5.0), (4, 5.0), (3, 3.0), (2, 1.0), (1, 0.0), (0, 0.0), (-1, 0.0)],
)
def test_regime_score_tiers(regime: int, expected_regime_component: float) -> None:
    eng = ScoringEngine({})
    out = eng.score_symbol(_base_data(), regime_score=regime)
    assert out.regime == expected_regime_component


def test_trend_only_partial_alignment() -> None:
    eng = ScoringEngine({})
    d = _base_data(price=92.0, ma50=95.0, ma200=90.0)
    out = eng.score_symbol(d, regime_score=0)
    # price > ma200 (+2), price not > ma50, ma50 > ma200 (+1) -> 3
    assert out.trend == 3.0


def test_pullback_distance_tiers() -> None:
    eng = ScoringEngine({})
    ma20 = 100.0
    # dist 0.005 -> 5
    assert eng._pullback_score(_base_data(price=100.5, ma20=ma20)) == 5
    # dist 0.015 -> 3
    assert eng._pullback_score(_base_data(price=101.5, ma20=ma20)) == 3
    # dist 0.025 -> 1
    assert eng._pullback_score(_base_data(price=102.5, ma20=ma20)) == 1
    assert eng._pullback_score(_base_data(price=110.0, ma20=ma20)) == 0


def test_volume_ratio_tiers() -> None:
    eng = ScoringEngine({})
    assert eng._volume_score(_base_data(volume=1_600_000, avg_volume=1_000_000)) == 3
    assert eng._volume_score(_base_data(volume=1_300_000, avg_volume=1_000_000)) == 2
    assert eng._volume_score(_base_data(volume=1_000_001, avg_volume=1_000_000)) == 1
    assert eng._volume_score(_base_data(volume=1_000_000, avg_volume=1_000_000)) == 0
    assert eng._volume_score(_base_data(volume=500_000, avg_volume=1_000_000)) == 0


def test_custom_weights() -> None:
    eng = ScoringEngine({"scoring": {"weights": {"trend": 0.5, "pullback": 0.0, "volume": 2.0, "regime": 1.0}}})
    d = _base_data(volume=1_000_001.0)
    out = eng.score_symbol(d, regime_score=4)
    assert out.trend == 2.5
    assert out.pullback == 0.0
    assert out.volume == 2.0
    assert out.regime == 5.0


def test_ma20_near_zero_avoids_div_zero() -> None:
    eng = ScoringEngine({})
    out = eng._pullback_score(_base_data(price=0.0, ma20=0.0))
    assert out == 5  # dist 0 / 1e-9 → tight to MA20 band
