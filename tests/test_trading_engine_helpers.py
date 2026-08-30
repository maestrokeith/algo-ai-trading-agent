"""Tests for :mod:`src.trading_engine` exposure / conviction helpers."""

from __future__ import annotations

import pytest

from src.trading_engine import (
    _conviction_band_from_strength,
    _positions_rows_for_exposure,
    score_conviction,
)


def test_positions_rows_for_exposure_notional_and_market_value() -> None:
    rows = _positions_rows_for_exposure(
        {
            "SPY": {"notional": 1000.0, "stop_pct": 1.5},
            "QQQ": {"market_value": 500.0, "stop_pct": 1.5},
        }
    )
    syms = {r["symbol"] for r in rows}
    assert syms == {"SPY", "QQQ"}
    assert sum(r["market_value"] for r in rows) == pytest.approx(1500.0)


def test_conviction_band_from_strength() -> None:
    assert _conviction_band_from_strength(None) is None
    assert _conviction_band_from_strength(0.9) == "strong"
    assert _conviction_band_from_strength(0.1) == "weak"
    assert _conviction_band_from_strength(0.5) == "medium"
    assert _conviction_band_from_strength(75.0) == "strong"


def test_score_conviction_strong_all_buckets() -> None:
    es = type("E", (), {"allowed": True})()
    assert score_conviction(es, 2.0, 0.2, "bullish") == "strong"


def test_score_conviction_strong_risk_on_label() -> None:
    es = type("E", (), {"allowed": True})()
    assert score_conviction(es, 1.0, 0.1, "strong_risk_on") == "strong"


def test_score_conviction_not_allowed_caps_at_weak_without_other_points() -> None:
    es = type("E", (), {"allowed": False})()
    assert score_conviction(es, None, 1.0, "bearish") == "weak"


def test_score_conviction_medium_sixty() -> None:
    es = type("E", (), {"allowed": True})()
    # 40 + ATR + spread only → 60
    assert score_conviction(es, 2.0, 0.4, "neutral") == "medium"
