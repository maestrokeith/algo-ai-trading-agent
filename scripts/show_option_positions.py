#!/usr/bin/env python3
"""Print current option positions and P&L for Alpaca paper/live accounts."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.brokers.alpaca_client import AlpacaBroker
from src.config_loader import load_config
from src.options_exit import compute_option_pnl_pct
from src.options_premium_risk import is_option_position


def _side_str(side: Any) -> str:
    if side is None:
        return ""
    if hasattr(side, "name"):
        return str(getattr(side, "name", ""))
    return str(side)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Use live account")
    parser.add_argument("--paper", action="store_true", help="Use paper account")
    args = parser.parse_args()
    if args.live and args.paper:
        parser.error("Use only one of --live or --paper")

    config = load_config(PROJECT_ROOT / "config" / "default.yaml")
    if args.live:
        config.setdefault("broker", {})["paper"] = False
    elif args.paper:
        config.setdefault("broker", {})["paper"] = True

    broker = AlpacaBroker(config)
    positions = [p for p in broker.get_positions() if is_option_position(p)]
    mode = "LIVE" if not broker.paper else "paper"
    equity = float(broker.get_equity())
    print(f"[{mode}] Equity: ${equity:,.2f}")
    print(f"Open option positions: {len(positions)}")
    if not positions:
        print("(No open option positions)")
        return

    sym_w = max(18, min(32, max(len(str(p.get("symbol") or "")) for p in positions)))
    hdr = (
        f"{'Symbol':<{sym_w}}  {'Qty':>4}  {'Side':<5}  {'Entry$':>8}  {'Mark$':>8}  {'P/L $':>10}  {'P/L %':>8}"
    )
    print(hdr)
    print("-" * len(hdr))
    total_upl = 0.0
    total_cost = 0.0
    for p in positions:
        symbol = str(p.get("symbol") or "").upper()
        qty = abs(int(float(p.get("qty") or 0)))
        side = _side_str(p.get("side"))[:5]
        cost_basis = float(p.get("cost_basis") or 0.0)
        avg_entry = abs(cost_basis) / (qty * 100.0) if qty > 0 and abs(cost_basis) > 1e-9 else 0.0
        mark = float(p.get("current_price") or 0.0)
        if mark <= 0 and hasattr(broker, "get_option_latest_quote"):
            q = broker.get_option_latest_quote(symbol)
            if q is not None:
                mark = float(q.mid)
        pnl = float(p.get("unrealized_pl") or 0.0)
        pnl_pct = compute_option_pnl_pct(
            unrealized_pl=pnl,
            cost_basis=cost_basis,
            market_value=float(p.get("market_value") or 0.0),
        )
        total_upl += pnl
        total_cost += abs(cost_basis)
        print(
            f"{symbol:<{sym_w}}  {qty:>4}  {side:<5}  ${avg_entry:>7.2f}  ${mark:>7.2f}  ${pnl:>+9.2f}  "
            f"{('n/a' if pnl_pct is None else f'{pnl_pct:+.2f}%'):>8}"
        )
    print("-" * len(hdr))
    total_pct = (100.0 * total_upl / total_cost) if abs(total_cost) > 1e-9 else 0.0
    print(f"TOTAL{'':<{sym_w - 5}}          ${total_upl:>+9.2f}  {total_pct:+.2f}%")


if __name__ == "__main__":
    main()
