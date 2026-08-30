"""Composite symbol scoring from trend, pullback, volume, and market regime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ScoreBreakdown:
    """Weighted component scores; ``total`` is their sum."""

    trend: float
    pullback: float
    volume: float
    regime: float

    @property
    def total(self) -> float:
        return self.trend + self.pullback + self.volume + self.regime


class ScoringEngine:
    """Scores a symbol from OHLC-style aggregates and a discrete regime score."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.weights = (config.get("scoring") or {}).get("weights") or {
            "trend": 1.0,
            "pullback": 1.0,
            "volume": 1.0,
            "regime": 1.0,
        }

    def score_symbol(self, data: dict[str, Any], regime_score: int) -> ScoreBreakdown:
        trend = self._trend_score(data) * self.weights["trend"]
        pullback = self._pullback_score(data) * self.weights["pullback"]
        volume = self._volume_score(data) * self.weights["volume"]
        regime = self._regime_score(regime_score) * self.weights["regime"]
        return ScoreBreakdown(trend=trend, pullback=pullback, volume=volume, regime=regime)

    def _trend_score(self, d: dict[str, Any]) -> float:
        score = 0.0
        if d["price"] > d["ma200"]:
            score += 2
        if d["price"] > d["ma50"]:
            score += 2
        if d["ma50"] > d["ma200"]:
            score += 1
        return score

    def _pullback_score(self, d: dict[str, Any]) -> float:
        dist = abs(d["price"] - d["ma20"]) / max(d["ma20"], 1e-9)
        if dist < 0.01:
            return 5
        if dist < 0.02:
            return 3
        if dist < 0.03:
            return 1
        return 0

    def _volume_score(self, d: dict[str, Any]) -> float:
        ratio = d["volume"] / max(d["avg_volume"], 1e-9)
        if ratio > 1.5:
            return 3
        if ratio > 1.2:
            return 2
        if ratio > 1.0:
            return 1
        return 0

    def _regime_score(self, regime_score: int) -> float:
        if regime_score >= 4:
            return 5
        if regime_score == 3:
            return 3
        if regime_score == 2:
            return 1
        return 0
