#!/usr/bin/env python3
"""
Reset paper trading account: close all positions, cancel open orders, clear local state.

Use this to "restart" paper trading with a clean slate. Your cash balance is unchanged
by this script; to set paper balance (e.g. back to $100k or $200k), use the Alpaca
dashboard: Paper Trading → Reset account.

Safety: This script runs ONLY when config has broker.paper: true. It will not run on live.
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_config
from src.brokers.alpaca_client import AlpacaBroker
from src.position_tracker import load, clear_all


def main() -> None:
    config = load_config(PROJECT_ROOT / "config" / "default.yaml")
    broker_cfg = config.get("broker", {})
    if not broker_cfg.get("paper", True):
        print("Refusing to run: broker.paper is not true. Reset is only for paper accounts.")
        sys.exit(1)

    broker = AlpacaBroker(config)
    tracker_path = PROJECT_ROOT / "data" / "positions_tracked.json"

    equity_before = broker.get_equity()
    positions = broker.get_positions()
    tracked = load(tracker_path)

    if not positions and not tracked:
        print("No open positions and no tracked state. Nothing to reset.")
        print(f"Equity: ${equity_before:,.2f}")
        return

    if "--yes" not in sys.argv and "-y" not in sys.argv:
        print(f"Paper account: {len(positions)} position(s), {len(tracked)} in local tracker.")
        print(f"Equity: ${equity_before:,.2f}")
        print("This will: close all positions, cancel open orders, clear local position tracker.")
        try:
            r = input("Proceed? [y/N]: ").strip().lower()
        except EOFError:
            r = "n"
        if r not in ("y", "yes"):
            print("Aborted.")
            sys.exit(0)

    try:
        responses = broker.close_all_positions(cancel_orders=True)
        print(f"Closed {len(responses)} position(s).")
        for r in (responses or []):
            sid = getattr(r, "symbol", None) or getattr(r, "order_id", str(r))
            print(f"  — {sid}")
    except Exception as e:
        print(f"Error closing positions: {e}")
        sys.exit(1)

    clear_all(tracker_path)
    print("Cleared local position tracker.")

    equity_after = broker.get_equity()
    print(f"Equity now: ${equity_after:,.2f}")
    print()
    print("To set your paper balance (e.g. $100k or $200k), use Alpaca dashboard:")
    print("  Paper Trading → Reset account")


if __name__ == "__main__":
    main()
