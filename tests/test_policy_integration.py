from datetime import datetime, timezone

from src.agents.risk_agent import DeterministicPolicyEngine, PolicyContext
from src.intelligence.schemas import MarketContext, RiskAssessment, TradeProposal, TradeSide


def _proposal():
    return TradeProposal("tp-1", "NVDA", TradeSide.BUY, "VWAP_BREAKOUT", "limit", 100, 98, 105, 60, 1.0, "x", (), (), datetime.now(timezone.utc))


def _market():
    return MarketContext("NVDA", datetime.now(timezone.utc), 100, bid=99.9, ask=100.1, spread_pct=0.2)


def test_ai_cannot_bypass_policy_daily_loss_lock():
    policy = DeterministicPolicyEngine({"execution": {"max_spread_pct": 1.0}})
    decision = policy.decide(_proposal(), RiskAssessment("tp-1", True, 1.0), _market(), PolicyContext(daily_loss_locked=True))

    assert not decision.approved
    assert "daily_loss_limit_reached" in decision.reasons
