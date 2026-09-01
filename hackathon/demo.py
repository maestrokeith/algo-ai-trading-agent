"""Deterministic hackathon scenarios for the Algo agent pipeline."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import pandas as pd

from src.agents.orchestrator import AgentOrchestrator
from src.agents.risk_agent import PolicyContext
from src.config_loader import load_config
from src.intelligence.schemas import MarketContext


class DemoMarketAgent:
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario

    def observe(self, symbol: str, *, bars=None, now=None) -> MarketContext:
        now = now or datetime.now(timezone.utc)
        if self.scenario == "critic_rejection":
            return MarketContext(
                symbol=symbol,
                timestamp=now,
                last_price=250.0,
                bid=249.8,
                ask=250.2,
                spread_pct=0.16,
                volume=1_000_000,
                relative_volume=1.1,
                vwap=244.0,
                distance_from_vwap_pct=2.46,
                market_session="regular",
                recent_returns=(0.004, 0.003, 0.002, -0.001, 0.002),
                volatility=0.004,
            )
        return MarketContext(
            symbol=symbol,
            timestamp=now,
            last_price=500.0,
            bid=499.9,
            ask=500.1,
            spread_pct=0.04,
            volume=3_000_000,
            relative_volume=2.1,
            vwap=498.0,
            distance_from_vwap_pct=0.40,
            market_session="regular",
            recent_returns=(0.002, 0.003, 0.001, -0.001, 0.002),
            volatility=0.006,
        )


def run_demo() -> int:
    config = load_config()
    config.setdefault("trading", {})["default_mode"] = "dry_run"
    config.setdefault("agents", {}).setdefault("memory", {})["enabled"] = False

    scenarios = [
        (
            "Scenario A - Approved Paper Trade",
            "NVDA",
            "approved",
            PolicyContext(),
            [
                "Regime: TREND_UP",
                "Strategy: VWAP_BREAKOUT",
                "Critic: PASS",
                "Risk: PASS",
                "Policy: PASS",
                "Execution: ALPACA PAPER READY (deterministic demo dry-run; no broker mutation)",
            ],
        ),
        (
            "Scenario B - Critic Rejection",
            "TSLA",
            "critic_rejection",
            PolicyContext(),
            [
                "Strategy: ENTER",
                "Critic: REJECT",
                "Reason: entry_too_extended",
                "Execution: BLOCKED",
            ],
        ),
        (
            "Scenario C - Safety Overrides AI",
            "AAPL",
            "approved",
            PolicyContext(daily_loss_locked=True),
            [
                "Agent: ENTER confidence=96%",
                "Critic: PASS",
                "AI Risk: SAFE",
                "Deterministic Policy: BLOCKED",
                "Reason: daily_loss_limit",
                "AI WANTED THE TRADE. SAFETY BLOCKED IT.",
            ],
        ),
    ]
    for title, symbol, scenario, policy_context, summary in scenarios:
        orchestrator = AgentOrchestrator(config, mode="dry_run")
        orchestrator.market_agent = DemoMarketAgent(scenario)
        trace = orchestrator.evaluate_symbol(symbol, dry_run=True, policy_context=policy_context)
        print("=" * 72)
        print(title)
        print(symbol)
        print("\nJudge summary:")
        for line in summary:
            print(line)
        print("\nAgent trace:")
        print(trace.to_text())
        print("\nTimeline:")
        for item in trace.timeline:
            print(item)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run deterministic Algo hackathon demo")
    parser.parse_args()
    raise SystemExit(run_demo())


if __name__ == "__main__":
    main()
