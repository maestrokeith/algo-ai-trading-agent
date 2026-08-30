#!/usr/bin/env python3
"""Print current Alpaca positions and equity. Use --live or --paper to choose account."""
import argparse
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_config
from src.brokers.alpaca_client import AlpacaBroker


def _side_str(side: Any) -> str:
    """Alpaca returns enums; show LONG/SHORT in a fixed width."""
    if side is None:
        return ""
    if hasattr(side, "name"):
        return str(getattr(side, "name", ""))[:6]
    s = str(side)
    if "." in s:
        return s.rsplit(".", 1)[-1][:6]
    return s[:6]


def _unrealized_pct(p: dict) -> str:
    """Return unrealized P&L as % of cost basis (Alpaca long: cost_basis > 0)."""
    cb = float(p.get("cost_basis") or 0)
    pl = float(p.get("unrealized_pl") or 0)
    if abs(cb) < 1e-9:
        return "n/a"
    return "%+.2f%%" % (100.0 * pl / cb,)


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
    positions = broker.get_positions()
    mode = "LIVE" if not broker.paper else "paper"
    print(f"[{mode}] Equity: ${equity:,.2f}")
    print(f"Open positions: {len(positions)}")
    if positions:
        print()
        sym_w = max(9, min(24, max(len(str(p["symbol"])) for p in positions)))
        hdr = (
            f"{'Symbol':<{sym_w}}  {'Qty':>5}  {'Side':<6}  {'Market $':>12}  {'Unreal $':>12}  {'P/L %':>9}"
        )
        print(hdr)
        print("-" * len(hdr))
        for p in positions:
            sym = str(p["symbol"])
            if len(sym) > sym_w:
                sym = sym[: sym_w - 1] + "…"
            print(
                f"{sym:<{sym_w}}  {p['qty']:>5}  {_side_str(p.get('side')):<6}  "
                f"${p['market_value']:>11,.2f}  ${p['unrealized_pl']:>+11,.2f}  {_unrealized_pct(p):>9}"
            )
        total_upl = sum(float(p.get("unrealized_pl") or 0) for p in positions)
        total_cb = sum(float(p.get("cost_basis") or 0) for p in positions)
        print("-" * len(hdr))
        pct_total = "%+.2f%%" % (100.0 * total_upl / total_cb,) if abs(total_cb) > 1e-9 else "n/a"
        print(
            f"{'TOTAL':<{sym_w}}  {'':>5}  {'':6}  "
            f"${sum(p['market_value'] for p in positions):>11,.2f}  ${total_upl:>+11,.2f}  {pct_total:>9}"
        )
        if equity > 0:
            print(
                "  (total unrealized = %+.2f%% of account equity)" % (100.0 * total_upl / equity,)
            )
    else:
        print("(No open positions)")

if __name__ == "__main__":
    main()
