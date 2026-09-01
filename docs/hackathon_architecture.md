# Hackathon Architecture

Algo is organized as a conservative agent pipeline around Alpaca paper trading primitives.

```text
Alpaca market data
    -> Market Agent
    -> Regime Agent
    -> Strategy Agent
    -> Critic Agent
    -> Risk Agent
    -> Deterministic Policy Engine
    -> Execution Agent
    -> Trade Memory
    -> Post-Trade Agent
```

## Components

- Alpaca integration: `src/brokers/alpaca_client.py` and `src/alpaca/mcp_adapter.py`
- Agent orchestration: `src/agents/orchestrator.py`
- Agent contracts and memory: `src/intelligence/`
- Deterministic execution and risk controls: `src/execution.py`, `src/portfolio_risk.py`, `src/compliance.py`, and `src/universe.py`
- Demo entry point: `hackathon/demo.py`
- Dashboard: `frontend/src/pages/AgentDashboard.tsx`
- API: `src/api/main.py`

## Agent Responsibilities

- Market Agent builds symbol-level context from bars, quotes, and calendar state.
- Regime Agent labels broad market conditions from structured inputs.
- Strategy Agent proposes a candidate trade with rationale and guardrails.
- Critic Agent challenges the proposal before it reaches risk review.
- Risk Agent applies portfolio, exposure, quote, and execution constraints.
- Execution Agent prepares an order intent only after deterministic approval.
- Trade Memory stores decisions and feedback for later strategy improvement.
- Post-Trade Agent converts completed outcomes into learning records.

## Safety Model

The AI layer cannot directly place orders. Proposals must pass:

- critic approval
- risk-agent approval
- deterministic portfolio risk approval
- quote freshness and spread checks
- duplicate-decision checks
- paper/live mode checks

The public hackathon demo defaults to dry-run/paper-safe behavior.
