#!/usr/bin/env python3
"""Print a live summary of option positions, P&L, and kill-switch status."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.brokers.alpaca_client import AlpacaBroker
from src.config_loader import load_config
from src.options_position_manager import sync_options_positions
from src.options_safety import build_options_daily_report


def _fmt(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.2f}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Use live account")
    parser.add_argument("--paper", action="store_true", help="Use paper account")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.live and args.paper:
        parser.error("Use only one of --live or --paper")

    config = load_config(PROJECT_ROOT / "config" / "default.yaml")
    if args.live:
        config.setdefault("broker", {})["paper"] = False
    elif args.paper:
        config.setdefault("broker", {})["paper"] = True

    broker = AlpacaBroker(config)
    user_id = str((config.get("user") or {}).get("id") or "default")
    snapshot = sync_options_positions(
        broker,
        config,
        user_id=user_id,
        data_dir=args.data_dir,
        execution_manager=None,
    )

    mode = "LIVE" if not broker.paper else "paper"
    print(f"[{mode}] open option positions: {snapshot.open_count}")
    print(
        "daily realized=%s unrealized=%s total=%s equity=%s"
        % (
            _fmt(snapshot.daily_realized_pl),
            _fmt(snapshot.daily_unrealized_pl),
            _fmt(snapshot.daily_total_pl),
            "n/a" if snapshot.daily_equity is None else f"{snapshot.daily_equity:.2f}",
        )
    )
    print(
        "kill_switch=%s reasons=%s"
        % (
            "ON" if snapshot.kill_switch_on else "off",
            ",".join(snapshot.kill_switch_reasons) or "none",
        )
    )
    print(
        "entry_blocked=%s reasons=%s"
        % (
            "yes" if snapshot.block_new_entries else "no",
            snapshot.block_reason or "none",
        )
    )
    daily_report = build_options_daily_report(user_id=user_id, data_dir=args.data_dir)
    print(
        "OPTIONS_DAILY_REPORT date=%s trades=%d missing_exits=%d stuck_positions=%d"
        % (
            daily_report["date"],
            len(daily_report["trades"]),
            int(daily_report["missing_exits"]),
            int(daily_report["stuck_positions"]),
        )
    )
    for trade in daily_report["trades"]:
        print(
            "OPTIONS_DAILY_TRADE underlying=%s contract=%s type=%s strike=%s expiration=%s "
            "dte=%s qty=%s entry_time=%s entry_price=%s exit_time=%s exit_price=%s "
            "spread=%s volume=%s open_interest=%s entry_reason=%s exit_reason=%s "
            "realized_pnl=%s unrealized_pnl=%s hold_duration=%s"
            % (
                trade.get("underlying") or "",
                trade.get("option_symbol") or "",
                trade.get("call_put") or "",
                trade.get("strike"),
                trade.get("expiration"),
                trade.get("dte"),
                trade.get("quantity"),
                trade.get("entry_time"),
                trade.get("entry_price"),
                trade.get("exit_time"),
                trade.get("exit_price"),
                trade.get("bid_ask_spread_at_entry"),
                trade.get("volume"),
                trade.get("open_interest"),
                trade.get("entry_reason"),
                trade.get("exit_reason"),
                trade.get("realized_pnl"),
                trade.get("unrealized_pnl"),
                trade.get("hold_duration"),
            )
        )
    if not snapshot.positions:
        print("(No open option positions)")
        return
    print("symbol | qty | right | entry | current | uPL | dte | delta | iv | quote_stale")
    for row in snapshot.positions:
        print(
            "%s | %s | %s | %.2f | %.2f | %.2f | %s | %s | %s | %s"
            % (
                row.get("symbol", ""),
                row.get("qty", 0),
                row.get("right", ""),
                float(row.get("entry_price") or 0.0),
                float(row.get("current_price") or 0.0),
                float(row.get("unrealized_pl") or 0.0),
                "n/a" if row.get("dte") is None else int(row.get("dte") or 0),
                "n/a" if row.get("delta") is None else f"{float(row.get('delta')):.3f}",
                "n/a" if row.get("iv") is None else f"{float(row.get('iv')):.3f}",
                bool(row.get("quote_stale")),
            )
        )


if __name__ == "__main__":
    main()
