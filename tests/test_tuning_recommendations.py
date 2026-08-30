from __future__ import annotations

from src.tuning_recommendations import (
    generate_tuning_recommendations,
    recommend_hold_time,
    recommend_ranking_weights,
    recommend_stop_loss,
    recommend_take_profit,
)


TRADES = [
    {
        "pnl": 100,
        "return_pct": 5.0,
        "hold_minutes": 45,
        "trend_score": 8,
        "momentum_score": 7,
        "news_score": 1,
    },
    {
        "pnl": 50,
        "return_pct": 3.0,
        "hold_minutes": 90,
        "trend_score": 6,
        "volume_score": 5,
    },
    {"pnl": -30, "return_pct": -2.0, "hold_minutes": 20, "trend_score": 3},
    {"pnl": -80, "return_pct": -4.0, "hold_minutes": 30, "news_score": 8},
]


def test_recommend_stop_loss_from_losing_distribution() -> None:
    rec = recommend_stop_loss(TRADES)

    assert rec.parameter == "stop_loss_pct"
    assert rec.recommended_value == 3.45
    assert rec.sample_size == 2


def test_recommend_take_profit_from_winning_distribution() -> None:
    rec = recommend_take_profit(TRADES)

    assert rec.parameter == "take_profit_pct"
    assert rec.recommended_value == 3.6
    assert rec.sample_size == 2


def test_recommend_hold_time_from_profitable_trades() -> None:
    rec = recommend_hold_time(TRADES)

    assert rec.recommended_value == 67.5
    assert rec.sample_size == 2


def test_recommend_ranking_weights_from_positive_pnl_contribution() -> None:
    rec = recommend_ranking_weights(TRADES)

    weights = rec.recommended_value
    assert isinstance(weights, dict)
    assert weights["trend_score"] > weights["news_score"]
    assert round(sum(weights.values()), 3) == 1.0


def test_generate_tuning_recommendations_empty_defaults() -> None:
    payload = generate_tuning_recommendations([])

    assert payload["sample_size"] == 0
    assert len(payload["recommendations"]) == 4
    assert payload["recommendations"][0]["recommended_value"] == 2.0
