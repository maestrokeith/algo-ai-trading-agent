from hackathon.demo import DemoMarketAgent
from src.agents.orchestrator import AgentOrchestrator
from src.agents.risk_agent import PolicyContext
from src.intelligence.schemas import DecisionStatus


def test_orchestrator_produces_policy_block_trace():
    orch = AgentOrchestrator({"trading": {"default_mode": "dry_run"}, "execution": {"max_spread_pct": 1.0}}, mode="dry_run")
    orch.market_agent = DemoMarketAgent("approved")

    trace = orch.evaluate_symbol("AAPL", dry_run=True, policy_context=PolicyContext(daily_loss_locked=True))

    assert trace.policy_decisions
    assert not trace.policy_decisions[0].approved
    assert trace.executions[0].status == DecisionStatus.BLOCKED


def test_paper_default_without_credentials():
    orch = AgentOrchestrator({"trading": {"default_mode": "paper"}, "execution": {"max_spread_pct": 1.0}})

    assert orch.mode == "paper"
