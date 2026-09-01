"""Strategy proposal agent."""

from __future__ import annotations

from src.intelligence.schemas import MarketContext, Regime, RegimeAssessment, StrategyStats, TradeProposal, TradeSide, new_id, utc_now


class StrategyAgent:
    """Create trade proposals from deterministic features and strategy memory."""

    def propose(
        self,
        market: MarketContext,
        regime: RegimeAssessment,
        memory: list[StrategyStats] | None = None,
    ) -> tuple[TradeProposal, ...]:
        if market.last_price <= 0:
            return ()
        if regime.regime not in {Regime.TREND_UP, Regime.LOW_VOLATILITY}:
            return ()
        if market.vwap is None or market.distance_from_vwap_pct is None:
            return ()
        if market.distance_from_vwap_pct < 0:
            return ()

        strategy = "VWAP_BREAKOUT"
        confidence = 0.56 + max(0.0, regime.confidence) * 0.25
        evidence = [f"regime {regime.regime.value}", f"price above VWAP by {market.distance_from_vwap_pct:.2f}%"]
        if market.relative_volume is not None:
            evidence.append(f"relative volume {market.relative_volume:.2f}x")
            if market.relative_volume >= 1.5:
                confidence += 0.08
        confidence += _memory_adjustment(strategy, regime.regime.value, memory or [])
        confidence = max(0.0, min(0.95, confidence))
        entry = market.ask or market.last_price
        return (
            TradeProposal(
                proposal_id=new_id("tp"),
                symbol=market.symbol,
                side=TradeSide.BUY,
                strategy=strategy,
                entry_type="limit_or_market_by_policy",
                suggested_entry=float(entry),
                stop_price=round(float(entry) * 0.985, 2),
                target_price=round(float(entry) * 1.025, 2),
                max_holding_minutes=60,
                confidence=confidence,
                reasoning_summary="VWAP breakout candidate in supportive regime",
                supporting_evidence=tuple(evidence),
                invalidating_conditions=("price loses VWAP", "relative volume fades", "spread widens"),
                timestamp=utc_now(),
            ),
        )


def _memory_adjustment(strategy: str, regime: str, memory: list[StrategyStats]) -> float:
    for row in memory:
        if row.strategy == strategy and row.regime == regime and row.trades >= 5:
            if row.avg_return is not None and row.avg_return < 0:
                return -0.12
            if row.win_rate is not None and row.win_rate >= 0.6:
                return 0.06
    return 0.0
