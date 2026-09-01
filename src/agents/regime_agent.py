"""Explainable deterministic market regime classifier."""

from __future__ import annotations

from src.intelligence.schemas import MarketContext, Regime, RegimeAssessment


class RegimeAgent:
    def assess(self, market: MarketContext) -> RegimeAssessment:
        evidence: list[str] = []
        returns = list(market.recent_returns)
        vol = market.volatility or 0.0
        persistence = 0.0
        if returns:
            positive = sum(1 for r in returns if r > 0)
            persistence = positive / len(returns)
        distance = market.distance_from_vwap_pct
        rel_volume = market.relative_volume

        if vol >= 0.025:
            evidence.append(f"realized volatility elevated at {vol:.2%}")
            return RegimeAssessment(Regime.HIGH_VOLATILITY, min(0.9, 0.55 + vol * 8), tuple(evidence))
        if vol > 0 and vol <= 0.003 and len(returns) >= 5:
            evidence.append(f"realized volatility muted at {vol:.2%}")
            return RegimeAssessment(Regime.LOW_VOLATILITY, 0.62, tuple(evidence))

        if distance is not None and distance > 0.15 and persistence >= 0.6:
            evidence.append(f"price {distance:.2f}% above VWAP")
            evidence.append(f"positive return persistence {persistence:.0%}")
            if rel_volume is not None and rel_volume >= 1.2:
                evidence.append(f"relative volume {rel_volume:.2f}x")
            return RegimeAssessment(Regime.TREND_UP, min(0.85, 0.45 + persistence * 0.35), tuple(evidence))
        if distance is not None and distance < -0.15 and persistence <= 0.4:
            evidence.append(f"price {abs(distance):.2f}% below VWAP")
            evidence.append(f"positive return persistence {persistence:.0%}")
            return RegimeAssessment(Regime.TREND_DOWN, min(0.85, 0.45 + (1 - persistence) * 0.35), tuple(evidence))
        if returns:
            evidence.append("mixed short-term returns")
            if distance is not None:
                evidence.append(f"price {distance:.2f}% from VWAP")
            return RegimeAssessment(Regime.CHOP, 0.55, tuple(evidence))
        return RegimeAssessment(Regime.UNKNOWN, 0.0, ("insufficient market context",))
