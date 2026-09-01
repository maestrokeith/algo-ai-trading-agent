# Agents

Algo adds independently testable agents that exchange dataclasses from `src/intelligence/schemas.py`.

Pipeline:

```text
Market Agent
Regime Agent
Strategy Agent
Critic Agent
Risk Agent
Deterministic Policy Engine
Execution Agent
Post-Trade Agent
Trade Memory
```

The Strategy Agent can create a `TradeProposal`. The Critic Agent can reject it. The Risk Agent scores risk, but the deterministic policy engine is the final authority and reuses existing risk and execution controls.

The default LLM provider is `DisabledLLMProvider`. Malformed or unavailable LLM output is treated as failure and cannot permit trading.
