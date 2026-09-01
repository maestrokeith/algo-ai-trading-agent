"""Agent orchestration for complete decision traces."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from .critic_agent import CriticAgent
from .execution_agent import ExecutionAgent
from .market_agent import MarketAgent
from .regime_agent import RegimeAgent
from .risk_agent import DeterministicPolicyEngine, PolicyContext, RiskAgent
from .strategy_agent import StrategyAgent
from src.intelligence.schemas import AgentDecisionTrace, DecisionStatus, ExecutionResult, new_id
from src.intelligence.trade_memory import TradeMemory

log = logging.getLogger(__name__)


@dataclass
class EvaluationInputs:
    bars: pd.DataFrame | None = None
    policy_context: PolicyContext | None = None


class AgentOrchestrator:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        broker: Any | None = None,
        memory: TradeMemory | None = None,
        mode: str | None = None,
    ) -> None:
        self.config = config or {}
        agents_cfg = self.config.get("agents") or {}
        default_mode = str(((self.config.get("trading") or {}).get("default_mode") or "paper")).lower()
        self.mode = mode or ("dry_run" if default_mode == "dry_run" else "paper")
        self.market_agent = MarketAgent(self.config, broker)
        self.regime_agent = RegimeAgent()
        self.strategy_agent = StrategyAgent()
        self.critic_agent = CriticAgent()
        self.risk_agent = RiskAgent()
        self.policy_engine = DeterministicPolicyEngine(self.config)
        self.execution_agent = ExecutionAgent(broker, mode=self.mode)
        self.memory = memory if agents_cfg.get("memory", {}).get("enabled", True) else None

    def evaluate_symbol(
        self,
        symbol: str,
        *,
        dry_run: bool = False,
        bars: pd.DataFrame | None = None,
        policy_context: PolicyContext | None = None,
    ) -> AgentDecisionTrace:
        evaluation_id = new_id("ev")
        mode = "dry_run" if dry_run else self.mode
        self.execution_agent.mode = mode
        timeline: list[str] = []
        now = datetime.now(timezone.utc)

        market = self.market_agent.observe(symbol, bars=bars, now=now)
        timeline.append(f"{now.isoformat()} Market data updated")
        self._log(evaluation_id, market.symbol, "market", "observed", None)

        regime = self.regime_agent.assess(market)
        timeline.append(f"{datetime.now(timezone.utc).isoformat()} Regime -> {regime.regime.value}")
        self._log(evaluation_id, market.symbol, "regime", regime.regime.value, None)

        stats = self.memory.strategy_stats() if self.memory else []
        proposals = self.strategy_agent.propose(market, regime, stats)
        if not proposals:
            execution = ExecutionResult(None, status=DecisionStatus.NO_TRADE, symbol=market.symbol, mode=mode, reason="strategy_agent_no_trade")
            return AgentDecisionTrace(
                evaluation_id,
                market.symbol,
                market,
                regime,
                (),
                (),
                (),
                (),
                (execution,),
                tuple(timeline),
                mode,
            )

        critics = []
        risks = []
        policies = []
        executions = []
        context = policy_context or PolicyContext()
        for proposal in proposals:
            timeline.append(f"{datetime.now(timezone.utc).isoformat()} Strategy -> {proposal.strategy}")
            if self.memory:
                self.memory.record_proposal(proposal, regime.regime.value)
            critic = self.critic_agent.critique(proposal, market, regime)
            critics.append(critic)
            timeline.append(f"{datetime.now(timezone.utc).isoformat()} Critic -> {'PASS' if critic.approved else 'REJECT'}")
            if self.memory:
                self.memory.record_critic(critic)
            risk = self.risk_agent.assess(proposal, critic, market)
            risks.append(risk)
            timeline.append(f"{datetime.now(timezone.utc).isoformat()} Risk -> {'PASS' if risk.approved else 'REJECT'}")
            policy = self.policy_engine.decide(proposal, risk, market, context)
            policies.append(policy)
            timeline.append(f"{datetime.now(timezone.utc).isoformat()} Policy -> {'PASS' if policy.approved else 'REJECT'}")
            if self.memory:
                self.memory.record_risk(policy)
            execution = self.execution_agent.execute(proposal, policy, market)
            executions.append(execution)
            timeline.append(f"{datetime.now(timezone.utc).isoformat()} Execution -> {execution.status.value}")
            if self.memory:
                self.memory.record_execution(execution)
            if execution.broker_order_id or execution.status.value in {"dry_run", "executed"}:
                context.executed_proposals.add(proposal.proposal_id)

        return AgentDecisionTrace(
            evaluation_id,
            market.symbol,
            market,
            regime,
            tuple(proposals),
            tuple(critics),
            tuple(risks),
            tuple(policies),
            tuple(executions),
            tuple(timeline),
            mode,
        )

    def _log(self, evaluation_id: str, symbol: str, agent: str, decision: str, reason: str | None) -> None:
        log.info(
            "AGENT_DECISION evaluation_id=%s symbol=%s agent=%s decision=%s reason=%s",
            evaluation_id,
            symbol,
            agent,
            decision,
            reason or "",
        )
