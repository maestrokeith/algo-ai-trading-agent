# Algo — Self-Improving Autonomous Trading Agent

Algo is an autonomous multi-agent trading system that observes markets, identifies market regimes, selects strategies, critiques its own decisions, executes through Alpaca with deterministic safety controls, and learns from completed trades.

## Why Algo

Most trading bots answer:

"What should I buy?"

Algo asks:

- What market regime are we in?
- Which strategy fits?
- Why might this trade fail?
- Is it safe?
- What did we learn from previous trades?

## Architecture

```text
Market Data
    ↓
Market Agent
    ↓
Regime Agent
    ↓
Strategy Agent
    ↓
Critic Agent
    ↓
Risk Agent
    ↓
Deterministic Safety Layer
    ↓
Alpaca Paper Trading
    ↓
Post-Trade Agent
    ↓
Trade Memory
    ↺
```

The trust boundary is intentional:

```text
AI Agents
    ↓
TradeProposal
    ↓
TRUST BOUNDARY
    ↓
Deterministic Policy
    ↓
Validated Execution
    ↓
Alpaca
```

Never:

```text
LLM → place_order()
```

## Key Differentiator

**AI can propose trades. AI cannot bypass deterministic safety controls.**

## Agents

- Market Agent: converts Alpaca market data into structured symbol context.
- Regime Agent: identifies whether the market favors trend, chop, or defensive behavior.
- Strategy Agent: selects an entry idea and produces a structured trade proposal.
- Critic Agent: challenges the proposal and fails it when the setup is weak or extended.
- Risk Agent: checks AI-side risk concerns before deterministic policy runs.
- Execution Agent: prepares a paper-safe execution intent only after policy approval.
- Post-Trade Agent: turns completed trade outcomes into review notes.
- Memory Layer: stores lessons and aggregate strategy performance for future decisions.

## Self-Improvement

```text
Observe
→ Decide
→ Critique
→ Execute
→ Analyze
→ Learn
→ Improve next decision
```

## Alpaca Integration

Implemented Alpaca pieces:

- Alpaca market data through the existing Alpaca broker client
- Alpaca paper trading support through the Trading API client
- Alpaca Trading API order-shape preparation and broker validation paths
- Alpaca MCP adapter in `src/alpaca/mcp_adapter.py`

The deterministic demo defaults to dry-run/paper-safe behavior and does not mutate a real broker account.

## Quick Demo

Run the judge-facing deterministic scenarios:

```bash
PYTHONPATH=. python -m hackathon.demo
```

The demo includes:

- Scenario A: approved NVDA paper-trade path
- Scenario B: TSLA critic rejection for `entry_too_extended`
- Scenario C: AAPL safety override where AI wants the trade and deterministic policy blocks it for `daily_loss_limit`

Run the agent CLI in dry-run mode:

```bash
PYTHONPATH=. python scripts/agent_cli.py --symbol SPY --dry-run
```

## Dashboard

Start the backend:

```bash
PYTHONPATH=. python scripts/run_api.py
```

Start the frontend:

```bash
cd frontend
npm install
npm run dev
```

Open the Vite URL and navigate to `/agents`. The Agent Dashboard shows symbol, market regime, regime confidence, selected strategy, strategy confidence, critic decision and concerns, AI risk decision, deterministic policy decision, Alpaca paper execution status, post-trade analysis, and learned memory.

## Safety

- Paper-first public demo design
- Deterministic risk controls after AI proposal and critique
- Malformed AI output fails closed
- Stale quotes are rejected
- Duplicate execution is blocked
- There is no direct LLM-to-broker path

## Tests

Relevant public tests include:

- `tests/test_regime_agent.py`
- `tests/test_strategy_agent.py`
- `tests/test_critic_agent.py`
- `tests/test_risk_agent.py`
- `tests/test_policy_integration.py`
- `tests/test_execution_safety.py`
- `tests/test_trade_memory.py`
- `tests/test_post_trade_agent.py`
- `tests/test_orchestrator.py`
- `tests/test_llm_provider.py`
- Alpaca broker, credential, API, auth, execution, compliance, and portfolio-risk tests

Run the remaining public suite:

```bash
PYTHONPATH=. pytest tests/ -v
```

## Hackathon Assets

- [Architecture](docs/hackathon_architecture.md)
- [Agents](docs/agents.md)
- [Alpaca MCP](docs/alpaca_mcp.md)
- [Trade Memory](docs/trade_memory.md)
- [Demo Guide](docs/demo.md)
- [Submission Notes](docs/HACKATHON_SUBMISSION.md)
- [Runbook](docs/HACKATHON_RUNBOOK.md)
- [Video Script](docs/HACKATHON_VIDEO_SCRIPT.md)
- [Pitch Deck Outline](docs/HACKATHON_PITCH_DECK_OUTLINE.md)
- [Pitch Deck](docs/algo_hackathon_pitch_deck.pptx)
- [Cover Image](assets/algo-hackathon-cover.png)
