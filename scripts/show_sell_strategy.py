#!/usr/bin/env python3
"""
Show sell strategy and timeline for each open position.

For every position: entry price/date, stop-loss level, take-profit level,
time-based exit (bars held vs exit-after), and kill-switch conditions.
Optionally fetches current price to show distance to stop/target and unrealized P&L.
"""
import argparse
import sys
from pathlib import Path
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_config
from src.brokers.alpaca_client import AlpacaBroker
from src.strategy import TrendFollowingStrategy
from src.position_tracker import load, bars_held


def _format_date(entry_time_iso: str | None) -> str:
    if not entry_time_iso:
        return "—"
    try:
        s = entry_time_iso.replace("Z", "+00:00")
        t = datetime.fromisoformat(s)
        return t.strftime("%Y-%m-%d %H:%M") if t else "—"
    except Exception:
        return str(entry_time_iso)[:16] if entry_time_iso else "—"


def main() -> None:
    parser = argparse.ArgumentParser(description="Sell strategy and timeline per position")
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
    mode = "LIVE" if not broker.paper else "paper"
    strategy = TrendFollowingStrategy(config)
    tracker_path = PROJECT_ROOT / "data" / "positions_tracked.json"
    tracked = load(tracker_path)

    positions = broker.get_positions()
    if not positions:
        print("No open positions.")
        return

    stop_pct_default = strategy.stop_loss_pct
    partial_pct = strategy.partial_take_profit_pct
    partial_ratio = strategy.partial_exit_ratio
    trail_pct = strategy.trailing_stop_pct
    time_bars = strategy.time_bars_exit
    ks_spread = strategy.kill_switch_max_spread_pct
    ks_atr_pct = strategy.kill_switch_max_atr_pct
    ks_mode = getattr(strategy, "kill_switch_mode", "full")
    ks_frac = float(getattr(strategy, "kill_switch_sell_fraction", 0.5) or 0.5)
    _ks_beh = (
        "partial (sell ~%.0f%% of shares)" % (ks_frac * 100.0) if ks_mode == "partial" else "full position"
    )

    print(f"Sell strategy and timeline  [{mode}]")
    print("=" * 70)
    print(
        f"Strategy: stop {stop_pct_default}% | partial {partial_pct}% (sell {int(partial_ratio*100)}%) | "
        f"trail rest {trail_pct}% | time exit {time_bars} bars | kill-switch ({_ks_beh}): "
        f"spread > {ks_spread}% or ATR% > {ks_atr_pct}%"
    )
    print()

    for p in positions:
        symbol = p["symbol"]
        qty = p["qty"]
        market_value = float(p["market_value"] or 0)
        cost_basis = float(p["cost_basis"] or 0)
        unrealized_pl = float(p.get("unrealized_pl") or 0)

        info = tracked.get(symbol.upper(), {})
        entry_price = float(info.get("entry_price") or (cost_basis / qty if qty else 0))
        entry_time_iso = info.get("entry_time")
        stop_pct = float(info.get("stop_pct") or stop_pct_default)

        bars = bars_held(entry_time_iso) if entry_time_iso else None
        stop_price = entry_price * (1 - stop_pct / 100.0)
        partial_price = entry_price * (1 + partial_pct / 100.0)
        partial_taken = info.get("partial_taken", False)
        trail_high = info.get("trail_high")

        current_price = (market_value / qty) if qty else entry_price
        ret_pct = (current_price - entry_price) / entry_price * 100 if entry_price else 0
        dist_stop_pct = (current_price - stop_price) / current_price * 100 if current_price and stop_price else 0
        dist_partial_pct = (partial_price - current_price) / current_price * 100 if (current_price and partial_price) else None

        print(f"  {symbol}  qty={qty}  entry=${entry_price:.2f}  entry_date={_format_date(entry_time_iso)}" + ("  [partial taken]" if partial_taken else ""))
        print(f"    Stop-loss:    ${stop_price:.2f}  ({stop_pct}% below entry)  —  current ~{dist_stop_pct:+.1f}% above stop")
        if not partial_taken:
            print(f"    Partial:      ${partial_price:.2f}  (sell {int(partial_ratio*100)}% at +{partial_pct}%)" + (f"  —  ~{dist_partial_pct:+.1f}% to partial" if dist_partial_pct is not None else ""))
        else:
            th = float(trail_high) if trail_high is not None else current_price
            trail_stop = th * (1 - trail_pct / 100.0)
            print(f"    Trailing:     high ${th:.2f}  →  sell rest if price ≤ ${trail_stop:.2f}  ({trail_pct}% below high)")
        if bars is not None:
            time_line = f"    Time exit:    bar {bars} of {time_bars}  (exit after {time_bars} bars)" + (f"  →  {time_bars - bars} bars left" if bars < time_bars else "  →  would exit on next check")
        else:
            time_line = f"    Time exit:    after {time_bars} bars  (entry date unknown; not in tracker)"
        print(time_line)
        print(f"    Kill-switch:  sell if spread > {ks_spread}% or ATR% > {ks_atr_pct}%")
        print(f"    Unrealized P&L: ${unrealized_pl:+,.2f}  ({ret_pct:+.2f}%)")
        print()

    print("(To see live prices, ensure market data is available; P&L above uses broker position values.)")


if __name__ == "__main__":
    main()
