# Hackathon Video Script

Target length: 3-4 minutes.

## 0:00 - 0:20 Opening

This is Algo, a self-improving autonomous trading agent for Alpaca.

The core idea is simple: Algo observes the market, determines the regime, proposes trades, criticizes its own decisions, applies deterministic risk controls, executes only through safe Alpaca paths, and learns from completed trades.

## 0:20 - 0:55 Problem

Most trading bots are either fully manual rule engines or opaque AI systems. Both are risky in different ways. Rule engines can miss context, while unconstrained AI agents can make confident but unsafe decisions.

Algo combines both: agents reason and explain, but deterministic policy has final authority.

## 0:55 - 1:35 Architecture

The pipeline starts with the Market Agent, which collects structured quote and market context: last price, bid, ask, spread, volume, VWAP distance, session state, recent returns, and volatility.

The Regime Agent classifies the tape into explainable regimes like TREND_UP, CHOP, or HIGH_VOLATILITY.

The Strategy Agent proposes structured TradeProposal objects. Then the Critic Agent challenges every proposal: Is volume weak? Is the spread too wide? Is the entry late? Is price extended from VWAP?

The Risk Agent evaluates the proposal, but the Deterministic Policy Engine makes the final call using existing risk controls.

## 1:35 - 2:30 Demo

In Scenario 1, NVDA is in TREND_UP. Price is above VWAP, relative volume is strong, critic passes, risk passes, policy passes, and execution is dry-run or paper-ready.

In Scenario 2, TSLA gets a buy proposal, but the Critic Agent rejects it because price is too extended above VWAP. Execution is blocked.

In Scenario 3, AAPL gets a high-confidence buy proposal. The critic passes. The risk agent passes. But deterministic policy rejects the trade because a daily-loss lock is active. This is the most important behavior: AI wanted the trade, policy blocked it.

## 2:30 - 3:10 Learning

Algo records proposals, critic reviews, risk decisions, executions, closed trades, and post-trade reviews in structured SQLite memory.

Learning updates strategy statistics by regime: for example, VWAP_BREAKOUT in TREND_UP versus VWAP_BREAKOUT in CHOP. Future proposals can adjust confidence, ranking, and eligibility based on actual outcomes.

Algo does not rewrite arbitrary trading code automatically.

## 3:10 - 3:45 Safety

Paper trading is the default. The demo does not require live credentials and does not place real orders.

No LLM output can bypass validation. No LLM gets direct broker access. Live trading remains protected by the existing safeguards.

## 3:45 - 4:00 Close

Algo is a trading agent that is autonomous enough to reason, humble enough to criticize itself, and disciplined enough to let deterministic risk controls say no.

## Demo Commands To Show

```bash
bin/algo hackathon demo
bin/algo agent evaluate NVDA --dry-run
bin/algo paper --user paper_bot
ALGOSPHERE_LOCAL_SQLITE=1 python scripts/run_api.py
cd frontend && npm run dev -- --host 127.0.0.1
```
