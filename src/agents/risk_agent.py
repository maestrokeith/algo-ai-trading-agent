"""Risk agent plus deterministic policy integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from src.execution import ExecutionManager
from src.intelligence.schemas import CriticAssessment, DecisionStatus, MarketContext, PolicyDecision, RiskAssessment, TradeProposal
from src.portfolio_risk import PortfolioRiskManager, PortfolioRiskState


@dataclass
class PolicyContext:
    equity: float = 100_000.0
    today: date = field(default_factory=date.today)
    daily_loss_locked: bool = False
    executed_proposals: set[str] = field(default_factory=set)


class RiskAgent:
    def assess(self, proposal: TradeProposal, critic: CriticAssessment, market: MarketContext) -> RiskAssessment:
        reasons: list[str] = []
        warnings: list[str] = []
        if not critic.approved:
            reasons.append("critic_rejected")
        if proposal.stop_price <= 0 or proposal.stop_price >= proposal.suggested_entry:
            reasons.append("invalid_stop_price")
        if proposal.target_price <= proposal.suggested_entry:
            warnings.append("target_not_above_entry")
        if market.volatility is not None and market.volatility > 0.03:
            reasons.append("volatility_abnormal")
        approved = not reasons
        score = 0.85 if approved else 0.2
        return RiskAssessment(proposal.proposal_id, approved, score, tuple(reasons), tuple(warnings))


class DeterministicPolicyEngine:
    """Final authority wrapper around existing deterministic risk and execution gates."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        portfolio_state: PortfolioRiskState | None = None,
    ) -> None:
        self.config = config or {}
        self.portfolio = PortfolioRiskManager(self.config)
        self.execution = ExecutionManager(self.config)
        self.portfolio_state = portfolio_state or PortfolioRiskState()

    def decide(
        self,
        proposal: TradeProposal,
        risk: RiskAssessment,
        market: MarketContext,
        context: PolicyContext | None = None,
    ) -> PolicyDecision:
        context = context or PolicyContext()
        reasons: list[str] = []
        if proposal.proposal_id in context.executed_proposals:
            reasons.append("duplicate_proposal")
        if context.daily_loss_locked:
            reasons.append("daily_loss_limit_reached")
            self.portfolio_state.trading_stopped_for_day = True
        if not risk.approved:
            reasons.extend(risk.reasons or ("risk_agent_rejected",))
        if market.quote_age_seconds is not None:
            max_age = float(((self.config.get("agents") or {}).get("execution") or {}).get("max_quote_age_seconds", 60))
            if market.quote_age_seconds > max_age:
                reasons.append("stale_quote")
        if market.bid is not None and market.ask is not None and market.ask < market.bid:
            reasons.append("crossed_quote")
        spread = float(market.spread_pct or 0.0)
        spread_ok, spread_reason = self.execution.can_trade_spread(spread, proposal.symbol)
        if not spread_ok:
            reasons.append(spread_reason)
        can_trade, risk_reason = self.portfolio.can_trade(
            self.portfolio_state,
            context.equity,
            proposal.symbol,
            today=context.today,
        )
        if not can_trade:
            reasons.append(risk_reason)

        if reasons:
            return PolicyDecision(proposal.proposal_id, False, DecisionStatus.BLOCKED, tuple(reasons))
        order = self.execution.build_order_for_entry(
            proposal.symbol,
            proposal.side.value,
            1,
            proposal.suggested_entry,
            spread,
            bid=market.bid,
            ask=market.ask,
        )
        if order is None:
            return PolicyDecision(
                proposal.proposal_id,
                False,
                DecisionStatus.BLOCKED,
                (self.execution.last_order_build_reject_reason or "order_build_rejected",),
            )
        return PolicyDecision(proposal.proposal_id, True, DecisionStatus.APPROVED, ("policy_passed",), order)
