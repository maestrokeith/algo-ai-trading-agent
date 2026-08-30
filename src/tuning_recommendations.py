"""Self-tuning recommendations from historical trade outcomes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import median
from typing import Any


@dataclass(frozen=True)
class TuningRecommendation:
    """One recommended parameter adjustment."""

    parameter: str
    recommended_value: float | dict[str, float]
    rationale: str
    sample_size: int

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-serializable recommendation."""
        return self.__dict__.copy()


def _as_float(raw: Any, default: float | None = None) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value != value:
        return default
    return value


def _pct(trade: Mapping[str, Any]) -> float | None:
    for key in ("return_pct", "pnl_pct", "realized_return_pct"):
        value = _as_float(trade.get(key))
        if value is not None:
            return value
    return None


def _hold_minutes(trade: Mapping[str, Any]) -> float | None:
    for key in ("hold_minutes", "holding_minutes", "duration_minutes"):
        value = _as_float(trade.get(key))
        if value is not None and value >= 0:
            return value
    return None


def recommend_stop_loss(trades: Sequence[Mapping[str, Any]]) -> TuningRecommendation:
    """Recommend stop loss from losing-trade return distribution."""
    losses = [abs(value) for trade in trades if (value := _pct(trade)) is not None and value < 0]
    if not losses:
        return TuningRecommendation("stop_loss_pct", 2.0, "No losing return history; keep default.", 0)
    rec = min(10.0, max(0.5, median(losses) * 1.15))
    return TuningRecommendation(
        "stop_loss_pct",
        round(rec, 2),
        "Set near the median historical loss with a small buffer.",
        len(losses),
    )


def recommend_take_profit(trades: Sequence[Mapping[str, Any]]) -> TuningRecommendation:
    """Recommend take profit from winning-trade return distribution."""
    wins = [value for trade in trades if (value := _pct(trade)) is not None and value > 0]
    if not wins:
        return TuningRecommendation("take_profit_pct", 4.0, "No winning return history; keep default.", 0)
    rec = min(25.0, max(1.0, median(wins) * 0.9))
    return TuningRecommendation(
        "take_profit_pct",
        round(rec, 2),
        "Set below the median historical winner to improve fill probability.",
        len(wins),
    )


def recommend_hold_time(trades: Sequence[Mapping[str, Any]]) -> TuningRecommendation:
    """Recommend hold time from profitable holding periods."""
    winning_holds = [
        hold
        for trade in trades
        if (_pct(trade) or 0.0) > 0 and (hold := _hold_minutes(trade)) is not None
    ]
    if not winning_holds:
        return TuningRecommendation("hold_time_minutes", 60.0, "No profitable hold-time history; keep default.", 0)
    rec = min(390.0, max(5.0, median(winning_holds)))
    return TuningRecommendation(
        "hold_time_minutes",
        round(rec, 2),
        "Use median hold time of profitable trades.",
        len(winning_holds),
    )


def recommend_ranking_weights(trades: Sequence[Mapping[str, Any]]) -> TuningRecommendation:
    """Recommend ranking weights from average P/L contribution of available features."""
    feature_keys = ("trend_score", "news_score", "volume_score", "momentum_score")
    contributions: dict[str, float] = {}
    counts: dict[str, int] = {}
    for trade in trades:
        pnl = _as_float(trade.get("pnl"), 0.0) or 0.0
        for key in feature_keys:
            score = _as_float(trade.get(key))
            if score is None:
                continue
            contributions[key] = contributions.get(key, 0.0) + max(0.0, pnl) * max(0.0, score)
            counts[key] = counts.get(key, 0) + 1
    if not contributions:
        return TuningRecommendation(
            "ranking_weights",
            {"trend_score": 0.4, "momentum_score": 0.3, "news_score": 0.2, "volume_score": 0.1},
            "No scored trade history; keep balanced defaults.",
            0,
        )
    total = sum(contributions.values())
    if total <= 0:
        weights = {key: round(1.0 / len(contributions), 4) for key in sorted(contributions)}
    else:
        weights = {key: round(value / total, 4) for key, value in sorted(contributions.items())}
    return TuningRecommendation(
        "ranking_weights",
        weights,
        "Weight features by positive P/L contribution in historical trades.",
        max(counts.values()) if counts else 0,
    )


def generate_tuning_recommendations(
    trades: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Generate all self-tuning recommendations."""
    recommendations = [
        recommend_stop_loss(trades),
        recommend_take_profit(trades),
        recommend_hold_time(trades),
        recommend_ranking_weights(trades),
    ]
    return {
        "sample_size": len(trades),
        "recommendations": [item.as_dict() for item in recommendations],
    }


__all__ = [
    "TuningRecommendation",
    "generate_tuning_recommendations",
    "recommend_hold_time",
    "recommend_ranking_weights",
    "recommend_stop_loss",
    "recommend_take_profit",
]
