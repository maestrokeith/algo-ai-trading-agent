from datetime import datetime, timedelta, timezone

from src.agents.risk_agent import DeterministicPolicyEngine, PolicyContext
from src.intelligence.schemas import MarketContext, RiskAssessment, TradeProposal, TradeSide


def _proposal(pid="tp-1"):
    return TradeProposal(pid, "NVDA", TradeSide.BUY, "VWAP_BREAKOUT", "limit", 100, 98, 105, 60, 1.0, "x", (), (), datetime.now(timezone.utc))


def test_stale_quote_is_blocked():
    policy = DeterministicPolicyEngine({"agents": {"execution": {"max_quote_age_seconds": 60}}, "execution": {"max_spread_pct": 1.0}})
    market = MarketContext("NVDA", datetime.now(timezone.utc), 100, bid=99.9, ask=100.1, spread_pct=0.2, quote_age_seconds=120)

    decision = policy.decide(_proposal(), RiskAssessment("tp-1", True, 1.0), market)

    assert not decision.approved
    assert "stale_quote" in decision.reasons


def test_duplicate_proposal_does_not_execute_twice():
    policy = DeterministicPolicyEngine({"execution": {"max_spread_pct": 1.0}})
    market = MarketContext("NVDA", datetime.now(timezone.utc), 100, bid=99.9, ask=100.1, spread_pct=0.2)
    context = PolicyContext(executed_proposals={"tp-1"})

    decision = policy.decide(_proposal(), RiskAssessment("tp-1", True, 1.0), market, context)

    assert not decision.approved
    assert "duplicate_proposal" in decision.reasons


def test_policy_blocks_crossed_quote():
    policy = DeterministicPolicyEngine({"execution": {"max_spread_pct": 1.0}})
    market = MarketContext("NVDA", datetime.now(timezone.utc), 100, bid=101, ask=100, spread_pct=0.2)

    decision = policy.decide(_proposal(), RiskAssessment("tp-1", True, 1.0), market)

    assert not decision.approved
    assert "crossed_quote" in decision.reasons
