# Hackathon Pitch Deck Outline

## Slide 1 - Algo

Self-Improving Autonomous Trading Agent

Autonomous reasoning. Self-criticism. Deterministic safety. Trade memory.

## Slide 2 - The Problem

Trading automation is split between rigid bots and opaque AI agents.

Rigid bots miss changing context. Opaque agents can make unsafe decisions. Traders need agentic reasoning that cannot bypass risk policy.

## Slide 3 - The Solution

Algo is a multi-agent trading system for Alpaca.

It observes the market, classifies the regime, proposes trades, critiques decisions, applies deterministic policy, executes through controlled Alpaca paper/live paths, and learns from outcomes.

## Slide 4 - Agent Pipeline

Market Agent -> Regime Agent -> Strategy Agent -> Critic Agent -> Risk Agent -> Deterministic Policy Engine -> Execution Agent -> Alpaca -> Post-Trade Agent -> Trade Memory

Key message: no LLM-to-broker direct path.

## Slide 5 - Self-Criticism

The Critic Agent challenges every proposal:

- Is the entry late?
- Is volume weak?
- Is the spread too wide?
- Is the regime inconsistent?
- Is price extended from VWAP?
- Have similar setups underperformed?

## Slide 6 - Deterministic Safety

AI can propose. Policy decides.

Demo example:

AI Proposal: BUY AAPL, 86% confidence  
Critic: PASS  
Risk Agent: PASS  
Policy Engine: REJECT  
Reason: daily_loss_limit_reached  
Execution: BLOCKED

## Slide 7 - Learning

Trade memory stores proposals, reviews, decisions, executions, closed trades, lessons, and strategy statistics.

Learning adjusts confidence, strategy ranking, and filtering by regime. It does not rewrite trading code.

## Slide 8 - Demo

Three deterministic scenarios:

- NVDA: approved dry-run trade
- TSLA: critic rejection
- AAPL: policy rejection despite AI approval

Dashboard shows Market, Agent Decisions, Trade Timeline, Memory, and Safety.

## Slide 9 - Tech Stack

Python, FastAPI, React, TypeScript, SQLite, Alpaca Trading API, Alpaca MCP-ready adapter, existing live loop, existing risk engine, structured dataclasses.

## Slide 10 - Why It Matters

Algo makes autonomous trading explainable, auditable, and safer.

It turns an AI trading agent from a black box into a decision trace that traders and judges can inspect.
