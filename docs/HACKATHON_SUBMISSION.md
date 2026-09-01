# Algo Hackathon Submission

## Event

Alpaca AI Trading Agents Hackathon  
Dates: August 28 - September 4, 2026  
Event page: https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon

## Paste-Ready Submission Fields

### Project Title

Algo - Self-Improving Autonomous Trading Agent

### Short Description

Algo is a multi-agent Alpaca trading platform that observes markets, detects regime, proposes trades, critiques itself, applies deterministic risk controls, executes only through safe Alpaca paper/live paths, and learns from completed trades.

### Long Description

Algo is a self-improving autonomous trading agent platform built on an existing production-grade Alpaca trading engine. Instead of letting an LLM place orders directly, Algo separates agent reasoning from deterministic execution authority.

The system observes market data, quote quality, session state, recent returns, spread, volume, VWAP distance, and volatility. A Market Agent produces structured market context, a Regime Agent classifies the current tape into explainable regimes such as TREND_UP, TREND_DOWN, CHOP, HIGH_VOLATILITY, LOW_VOLATILITY, or UNKNOWN, and a Strategy Agent proposes candidate trades as validated structured objects.

Every proposed trade is challenged by a Critic Agent that asks why the setup could fail: whether the entry is late, the spread is too wide, volume is weak, the regime is inconsistent, price is too extended from VWAP, or similar setups have underperformed. A Risk Agent then evaluates the proposal, but final authority belongs to the deterministic policy engine. Existing portfolio risk controls, spread checks, quote freshness rules, session handling, sizing logic, paper/live modes, and broker safety layers remain intact.

For the hackathon demo, Algo defaults to paper trading or dry-run mode. The demo scenarios show three judge-friendly outcomes: an approved trade, a self-criticized rejection, and the key safety case where AI wants a trade but deterministic policy blocks it because a daily-loss lock is active.

Algo also includes structured trade memory in SQLite. Completed trades and post-trade reviews update strategy statistics by regime, enabling learning through confidence adjustment, strategy ranking, and filtering rather than unsafe self-modifying trading code.

### Technology Tags

Alpaca Trading API, Alpaca MCP-ready adapter, Python, FastAPI, React, TypeScript, SQLite, autonomous agents, algorithmic trading, paper trading, risk management, self-improving agents, structured decision traces.

### Category Tags

AI Trading Agents, Autonomous Agents, FinTech, Algorithmic Trading, Risk Management, Developer Tools.

### GitHub Repository

TODO: Add public GitHub URL after pushing the `hackathon/alpaca-ai-agents` branch.

### Demo Application Platform

Local prototype today. Recommended public demo options: Render, Railway, Fly.io, or a short hosted video walkthrough if live deployment is not feasible.

### Application URL

TODO: Add deployed dashboard URL.

### Video Presentation URL

TODO: Add YouTube, Loom, or X/Twitter video URL.

### Slide Presentation URL

TODO: Add Google Slides, Canva, or uploaded PPTX/PDF URL.

Local editable deck: `docs/algo_hackathon_pitch_deck.pptx`

Local deck previews: `docs/hackathon_deck_preview/`

### Additional Information

Algo is intentionally safety-first. The agentic layer can propose and critique trades, but it cannot bypass deterministic policy controls or place live broker orders directly. The default development and demo path is paper trading/dry-run. Live trading remains guarded by the existing repository safeguards.

## Judging Alignment

### Application of Technology

Algo integrates Alpaca as the broker and market-data execution path, adds agentic market/regime/strategy/critic/risk layers, and prepares an Alpaca MCP abstraction without exposing unrestricted order placement.

### Presentation

The dashboard and demo are organized around a complete decision trace, making it easy to see why the agent considered a trade, why it challenged the trade, what policy decided, and what memory learns afterward.

### Business Value

Retail and small-team algo traders need automation that is explainable, auditable, and bounded by risk controls. Algo turns agentic trading from "black box bot" into a supervised decision system with deterministic safety.

### Originality

The core differentiator is self-criticism plus deterministic authority: the AI can want a trade and still be blocked by policy. Learning improves confidence/ranking/filtering, not arbitrary source code.

## Required Assets Checklist

- [ ] Public GitHub repo/branch
- [ ] Working prototype available online
- [ ] Application URL
- [x] Cover image, 16:9: `assets/algo-hackathon-cover.png`
- [ ] Video presentation, 5 minutes or less
- [x] Slide presentation draft: `docs/algo_hackathon_pitch_deck.pptx`
- [ ] Short description under 255 characters if the form enforces it
- [ ] Long description over 100 words
- [ ] Team created on lablab.ai
- [ ] Every team member registered individually

## Sources

- Hackathon page: https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon
- Lablab guide: https://lablab.ai/guide
- Lablab submission article: https://lablab.ai/ai-articles/hackathon-guidelines
