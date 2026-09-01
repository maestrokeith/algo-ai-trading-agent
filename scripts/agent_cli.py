"""CLI for Algo autonomous agent evaluation."""

from __future__ import annotations

import argparse

from src.agents.orchestrator import AgentOrchestrator
from src.config_loader import load_config
from src.intelligence.trade_memory import TradeMemory


def _memory(config: dict) -> TradeMemory:
    path = (((config.get("agents") or {}).get("memory") or {}).get("path") or "data/algo_memory.db")
    return TradeMemory(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Algo agent commands")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ev = sub.add_parser("evaluate", help="Evaluate a symbol")
    ev.add_argument("symbol")
    ev.add_argument("--dry-run", action="store_true")
    ev.add_argument("--paper", action="store_true")
    watch = sub.add_parser("watch", help="Evaluate multiple symbols once")
    watch.add_argument("symbols", nargs="+")
    watch.add_argument("--dry-run", action="store_true")
    sub.add_parser("decisions", help="Show where decision traces are stored")
    sub.add_parser("memory", help="Show strategy memory")
    sub.add_parser("strategies", help="Show strategy performance")
    args = parser.parse_args()

    config = load_config()
    memory = _memory(config)
    if args.cmd == "evaluate":
        mode = "dry_run" if args.dry_run else "paper"
        orch = AgentOrchestrator(config, memory=memory, mode=mode)
        print(orch.evaluate_symbol(args.symbol, dry_run=args.dry_run).to_text())
        return 0
    if args.cmd == "watch":
        orch = AgentOrchestrator(config, memory=memory, mode="dry_run" if args.dry_run else "paper")
        for symbol in args.symbols:
            print(orch.evaluate_symbol(symbol, dry_run=args.dry_run).to_text())
            print()
        return 0
    if args.cmd == "decisions":
        print("Decision traces are recorded in data/algo_memory.db when agents.memory.enabled=true.")
        return 0
    if args.cmd in {"memory", "strategies"}:
        rows = memory.strategy_stats()
        if not rows:
            print("No strategy memory yet.")
            return 0
        for row in rows:
            wr = "n/a" if row.win_rate is None else f"{row.win_rate:.0%}"
            ar = "n/a" if row.avg_return is None else f"{row.avg_return:+.2f}%"
            print(f"{row.strategy} {row.regime}: trades={row.trades} win_rate={wr} avg_return={ar}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
