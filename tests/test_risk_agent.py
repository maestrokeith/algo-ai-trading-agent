from datetime import datetime, timezone

from src.agents.critic_agent import CriticAgent
from src.agents.risk_agent import RiskAgent
from src.intelligence.schemas import MarketContext, Regime, RegimeAssessment, TradeProposal, TradeSide


def test_risk_agent_blocks_critic_rejection():
    market = MarketContext("NVDA", datetime.now(timezone.utc), 100, spread_pct=0.1, vwap=100, distance_from_vwap_pct=0.1)
    proposal = TradeProposal("tp-1", "NVDA", TradeSide.BUY, "VWAP_BREAKOUT", "limit", 100, 99, 103, 60, 0.8, "x", (), (), datetime.now(timezone.utc))
    critic = CriticAgent().critique(proposal, market, RegimeAssessment(Regime.CHOP, 0.6))

    risk = RiskAgent().assess(proposal, critic, market)

    assert not risk.approved
    assert "critic_rejected" in risk.reasons
