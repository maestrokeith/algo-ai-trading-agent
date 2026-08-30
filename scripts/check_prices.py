#!/usr/bin/env python3
"""Show latest stock prices for tracked symbols. Use --live or --paper to choose account."""
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

    symbols = config.get("universe", {}).get("symbols", ["SPY"])
    broker = AlpacaBroker(config)

    print("Symbol    Price         Bid        Ask        (source)")
    print("-" * 55)
    for symbol in symbols:
        q = broker.get_latest_quote(symbol)
        if q:
            print(f"{symbol:<9} ${q.mid:>8.2f}   ${q.bid:>8.2f}   ${q.ask:>8.2f}   quote")
        else:
            # When market is closed, quote can be empty; use last daily bar close
            df = broker.get_bars(symbol, timeframe="1Day", limit=1)
            if not df.empty:
                close = df["close"].iloc[-1]
                print(f"{symbol:<9} ${close:>8.2f}   --         --         last close")
            else:
                print(f"{symbol:<9} (no data)")

if __name__ == "__main__":
    main()
