"""Structured contracts exchanged by Algo trading agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


class Regime(str, Enum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    CHOP = "CHOP"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    UNKNOWN = "UNKNOWN"


class TradeSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class DecisionStatus(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    EXECUTED = "executed"
    DRY_RUN = "dry_run"
    NO_TRADE = "no_trade"
    ERROR = "error"


@dataclass(frozen=True)
class MarketContext:
    symbol: str
    timestamp: datetime
    last_price: float
    bid: float | None = None
    ask: float | None = None
    spread_pct: float | None = None
    volume: float | None = None
    relative_volume: float | None = None
    vwap: float | None = None
    distance_from_vwap_pct: float | None = None
    market_session: str = "unknown"
    recent_returns: tuple[float, ...] = ()
    volatility: float | None = None
    recent_bars: tuple[dict[str, Any], ...] = ()
    quote_age_seconds: float | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RegimeAssessment:
    regime: Regime
    confidence: float
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class StrategyStats:
    strategy: str
    regime: str
    trades: int = 0
    win_rate: float | None = None
    avg_return: float | None = None
    lessons: tuple[str, ...] = ()


@dataclass(frozen=True)
class TradeProposal:
    proposal_id: str
    symbol: str
    side: TradeSide
    strategy: str
    entry_type: str
    suggested_entry: float
    stop_price: float
    target_price: float
    max_holding_minutes: int
    confidence: float
    reasoning_summary: str
    supporting_evidence: tuple[str, ...]
    invalidating_conditions: tuple[str, ...]
    timestamp: datetime


@dataclass(frozen=True)
class CriticAssessment:
    proposal_id: str
    approved: bool
    critic_score: float
    risks: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    rejection_reasons: tuple[str, ...] = ()
    recommended_adjustments: tuple[str, ...] = ()


@dataclass(frozen=True)
class RiskAssessment:
    proposal_id: str
    approved: bool
    risk_score: float
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyDecision:
    proposal_id: str
    approved: bool
    status: DecisionStatus
    reasons: tuple[str, ...] = ()
    order: Any | None = None


@dataclass(frozen=True)
class ExecutionResult:
    proposal_id: str | None
    status: DecisionStatus
    symbol: str
    side: str | None = None
    mode: str = "dry_run"
    broker_order_id: str | None = None
    reason: str | None = None
    timestamp: datetime = field(default_factory=utc_now)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PostTradeReview:
    strategy: str
    regime_at_entry: str
    entry_price: float
    exit_price: float
    return_pct: float
    holding_time_minutes: float
    maximum_favorable_excursion: float | None
    maximum_adverse_excursion: float | None
    expected_behavior: str
    actual_behavior: str
    what_worked: tuple[str, ...]
    what_failed: tuple[str, ...]
    lesson: str
    timestamp: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class AgentRequest:
    agent: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class AgentResponse:
    ok: bool
    payload: dict[str, Any]
    error: str | None = None


@dataclass(frozen=True)
class AgentDecisionTrace:
    evaluation_id: str
    symbol: str
    market: MarketContext | None
    regime: RegimeAssessment | None
    proposals: tuple[TradeProposal, ...]
    critic_reviews: tuple[CriticAssessment, ...]
    risk_assessments: tuple[RiskAssessment, ...]
    policy_decisions: tuple[PolicyDecision, ...]
    executions: tuple[ExecutionResult, ...]
    timeline: tuple[str, ...]
    mode: str = "dry_run"

    def to_text(self) -> str:
        lines = [self.symbol, ""]
        if self.market:
            lines.append(f"Market: {self.market.last_price:.2f} spread={self.market.spread_pct}")
        if self.regime:
            lines.append(f"Market regime: {self.regime.regime.value} - {self.regime.confidence:.0%}")
        for proposal in self.proposals:
            lines.append(f"Strategy: {proposal.strategy}")
            lines.append(f"Signal: {proposal.side.value.upper()} - {proposal.confidence:.0%}")
            if proposal.supporting_evidence:
                lines.append("Evidence:")
                lines.extend(f"+ {item}" for item in proposal.supporting_evidence)
        for critic in self.critic_reviews:
            lines.append(f"Critic: {'PASS' if critic.approved else 'REJECT'}")
            for reason in critic.rejection_reasons or critic.risks:
                lines.append(f"- {reason}")
        for risk in self.risk_assessments:
            lines.append(f"Risk: {'PASS' if risk.approved else 'REJECT'}")
            for reason in risk.reasons:
                lines.append(f"- {reason}")
        for policy in self.policy_decisions:
            lines.append(f"Policy: {'PASS' if policy.approved else 'REJECT'}")
            for reason in policy.reasons:
                lines.append(f"- {reason}")
        for execution in self.executions:
            label = execution.status.value.upper()
            lines.append(f"Execution: {label}")
            if execution.reason:
                lines.append(f"Reason: {execution.reason}")
        return "\n".join(lines)
