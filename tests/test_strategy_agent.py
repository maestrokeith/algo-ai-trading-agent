from datetime import datetime, timezone

from src.agents.strategy_agent import StrategyAgent
from src.intelligence.schemas import MarketContext, Regime, RegimeAssessment


def test_strategy_agent_proposes_vwap_breakout():
    market = MarketContext(
        symbol="NVDA",
        timestamp=datetime.now(timezone.utc),
        last_price=500,
        ask=500.1,
        vwap=498,
        distance_from_vwap_pct=0.4,
        relative_volume=2.0,
    )
    regime = RegimeAssessment(Regime.TREND_UP, 0.8, ("trend",))

    proposals = StrategyAgent().propose(market, regime, [])

    assert len(proposals) == 1
    assert proposals[0].strategy == "VWAP_BREAKOUT"
    assert proposals[0].confidence > 0.7
