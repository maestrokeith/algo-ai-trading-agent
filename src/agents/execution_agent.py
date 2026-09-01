"""Controlled execution agent."""

from __future__ import annotations

from typing import Any

from src.intelligence.schemas import DecisionStatus, ExecutionResult, MarketContext, PolicyDecision, TradeProposal


class ExecutionAgent:
    """Execute only policy-approved orders through the configured broker."""

    def __init__(self, broker: Any | None = None, *, mode: str = "dry_run") -> None:
        self.broker = broker
        self.mode = mode if mode in {"dry_run", "paper", "live"} else "dry_run"

    def execute(
        self,
        proposal: TradeProposal,
        policy: PolicyDecision,
        market: MarketContext,
    ) -> ExecutionResult:
        if not policy.approved or policy.order is None:
            return ExecutionResult(
                proposal_id=proposal.proposal_id,
                status=DecisionStatus.BLOCKED,
                symbol=proposal.symbol,
                side=proposal.side.value,
                mode=self.mode,
                reason="; ".join(policy.reasons) or "policy_blocked",
            )
        if self.mode == "dry_run":
            return ExecutionResult(
                proposal_id=proposal.proposal_id,
                status=DecisionStatus.DRY_RUN,
                symbol=proposal.symbol,
                side=proposal.side.value,
                mode="dry_run",
                reason="dry_run_no_broker_order",
            )
        if self.broker is None:
            return ExecutionResult(
                proposal_id=proposal.proposal_id,
                status=DecisionStatus.BLOCKED,
                symbol=proposal.symbol,
                side=proposal.side.value,
                mode=self.mode,
                reason="broker_unavailable",
            )
        if self.mode == "live":
            return ExecutionResult(
                proposal_id=proposal.proposal_id,
                status=DecisionStatus.BLOCKED,
                symbol=proposal.symbol,
                side=proposal.side.value,
                mode="live",
                reason="agent_live_execution_requires_existing_live_interlocks",
            )
        order = self.broker.submit_order(policy.order)
        broker_order_id = str(getattr(order, "id", None) or getattr(order, "broker_order_id", "") or "")
        if not broker_order_id:
            return ExecutionResult(
                proposal_id=proposal.proposal_id,
                status=DecisionStatus.ERROR,
                symbol=proposal.symbol,
                side=proposal.side.value,
                mode=self.mode,
                reason="broker_confirmation_missing",
                raw={"order": repr(order)},
            )
        return ExecutionResult(
            proposal_id=proposal.proposal_id,
            status=DecisionStatus.EXECUTED,
            symbol=proposal.symbol,
            side=proposal.side.value,
            mode="paper",
            broker_order_id=broker_order_id,
            raw={"order_status": str(getattr(order, "status", ""))},
        )
