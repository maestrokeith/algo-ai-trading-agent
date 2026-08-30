#!/usr/bin/env python3
"""Print current Alpaca account equity. Use --live or --paper to choose account."""
import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_config
from src.brokers.alpaca_client import AlpacaBroker

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Live account")
    parser.add_argument("--paper", action="store_true", help="Paper account (default)")
    args = parser.parse_args()
    if args.live and args.paper:
        parser.error("Use only one of --live or --paper")

    config = load_config(PROJECT_ROOT / "config" / "default.yaml")
    if args.live:
        config.setdefault("broker", {})["paper"] = False
    elif args.paper:
        config.setdefault("broker", {})["paper"] = True

    broker = AlpacaBroker(config)
    equity = broker.get_equity()
    mode = "LIVE" if not broker.paper else "paper"
    print(f"[{mode}] Equity: ${equity:,.2f}")

if __name__ == "__main__":
    main()
