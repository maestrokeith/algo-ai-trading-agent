"""Self-criticism agent for trade proposals."""

from __future__ import annotations

from src.intelligence.schemas import CriticAssessment, MarketContext, Regime, RegimeAssessment, TradeProposal


class CriticAgent:
    def critique(
        self,
        proposal: TradeProposal,
        market: MarketContext,
        regime: RegimeAssessment,
    ) -> CriticAssessment:
        risks: list[str] = []
        warnings: list[str] = []
        rejections: list[str] = []
        adjustments: list[str] = []

        if regime.regime in {Regime.CHOP, Regime.TREND_DOWN, Regime.UNKNOWN}:
            rejections.append(f"regime {regime.regime.value} inconsistent with {proposal.strategy}")
        if market.spread_pct is not None and market.spread_pct > 1.0:
            rejections.append(f"spread too wide at {market.spread_pct:.2f}%")
        if market.distance_from_vwap_pct is not None and market.distance_from_vwap_pct > 2.0:
            rejections.append(f"price extended {market.distance_from_vwap_pct:.2f}% above VWAP")
        if market.relative_volume is not None and market.relative_volume < 1.2:
            risks.append(f"weak relative volume {market.relative_volume:.2f}x")
        if "crossed_quote" in market.warnings:
            rejections.append("crossed quote")
        if proposal.confidence < 0.55:
            rejections.append(f"proposal confidence too low at {proposal.confidence:.0%}")
        if market.distance_from_vwap_pct is not None and market.distance_from_vwap_pct > 0.5:
            warnings.append("entry may be late versus VWAP")
            adjustments.append("wait for pullback toward VWAP")

        score = max(0.0, 1.0 - 0.25 * len(risks) - 0.45 * len(rejections) - 0.1 * len(warnings))
        return CriticAssessment(
            proposal_id=proposal.proposal_id,
            approved=not rejections,
            critic_score=score,
            risks=tuple(risks),
            warnings=tuple(warnings),
            rejection_reasons=tuple(rejections),
            recommended_adjustments=tuple(adjustments),
        )
