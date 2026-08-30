#!/usr/bin/env python3
"""
Show candlestick charts for each open position.

Fetches daily OHLCV for each symbol you hold, plots candles and your entry price.
Requires: matplotlib (pip install matplotlib).
"""
import argparse
import sys
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.patches import Rectangle
except ImportError:
    print("This script requires matplotlib. Install with: pip install matplotlib")
    sys.exit(1)

import pandas as pd

from src.config_loader import load_config
from src.brokers.alpaca_client import AlpacaBroker
from src.position_tracker import load as load_tracked


def _ensure_datetime_index(df: pd.DataFrame) -> pd.DataFrame:
    """If index is MultiIndex (e.g. symbol, timestamp), use the timestamp level so we don't pass symbol to date2num."""
    if df.empty:
        return df
    idx = df.index
    if isinstance(idx, pd.MultiIndex):
        # Timestamp is usually the last level; first level can be symbol (e.g. "JD")
        for level in range(idx.nlevels - 1, -1, -1):
            lev = idx.get_level_values(level)
            try:
                pd.to_datetime(lev[:1])
                df = df.copy()
                df.index = pd.to_datetime(lev)
                break
            except (TypeError, ValueError):
                continue
    return df


def _candlestick(ax, df: pd.DataFrame, bar_width: float = 0.6) -> None:
    """Draw candlesticks on axes. df must have open, high, low, close and a datetime index."""
    if df.empty or len(df) < 1:
        return
    use_date_axis = True
    try:
        dates = mdates.date2num(pd.to_datetime(df.index).to_pydatetime())
    except Exception:
        dates = list(range(len(df)))
        use_date_axis = False
    for i, (t, row) in enumerate(zip(dates, df.itertuples(index=False))):
        o, h, l, c = getattr(row, "open", 0), getattr(row, "high", 0), getattr(row, "low", 0), getattr(row, "close", 0)
        if pd.isna(o) or pd.isna(h) or pd.isna(l) or pd.isna(c):
            continue
        # Wick: vertical line low -> high
        ax.plot([t, t], [l, h], color="black", linewidth=0.8, zorder=1)
        # Body
        top = max(o, c)
        bottom = min(o, c)
        height = top - bottom if top != bottom else (h - l) * 0.01
        if height == 0:
            height = (h - l) * 0.02
        color = "green" if c >= o else "red"
        rect = Rectangle((t - bar_width / 2, bottom), bar_width, height, facecolor=color, edgecolor="black", linewidth=0.5, zorder=2)
        ax.add_patch(rect)
    if use_date_axis:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha="right")


def main() -> None:
    parser = argparse.ArgumentParser(description="Show candlestick charts for open positions")
    parser.add_argument("--live", action="store_true", help="Live account")
    parser.add_argument("--paper", action="store_true", help="Paper account (default)")
    parser.add_argument("--bars", type=int, default=60, help="Number of daily bars to show (default 60)")
    parser.add_argument("--save", type=str, default="", metavar="PATH", help="Save figure to file instead of showing")
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
    tracker_path = PROJECT_ROOT / "data" / "positions_tracked.json"
    tracked = load_tracked(tracker_path)

    positions = broker.get_positions()
    if not positions:
        print("No open positions. Nothing to chart.")
        return

    n = len(positions)
    ncols = min(2, n)
    nrows = (n + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols, figsize=(7 * ncols, 5 * nrows))
    if n == 1:
        axes = [axes]
    else:
        axes = axes.flatten()

    for idx, p in enumerate(positions):
        symbol = p["symbol"]
        qty = int(float(p.get("qty") or 0))
        cost_basis = float(p.get("cost_basis") or 0)
        entry_price = cost_basis / qty if qty else 0.0
        info = tracked.get(symbol.upper(), {})
        if info.get("entry_price"):
            entry_price = float(info["entry_price"])

        ax = axes[idx]
        df = broker.get_bars(symbol, timeframe="1Day", limit=args.bars)
        if df.empty or len(df) < 2:
            ax.set_title(f"{symbol} (no data)")
            ax.text(0.5, 0.5, "No bar data", ha="center", va="center", transform=ax.transAxes)
            continue

        df = _ensure_datetime_index(df)
        df = df.sort_index()
        _candlestick(ax, df)
        ax.axhline(y=entry_price, color="blue", linestyle="--", linewidth=1.5, label=f"Entry ${entry_price:.2f}")
        ax.set_title(f"{symbol}  qty={qty}  entry=${entry_price:.2f}")
        ax.set_ylabel("Price")
        ax.legend(loc="upper left", fontsize=8)
        ax.grid(True, alpha=0.3)
        try:
            dt_min = pd.to_datetime(df.index.min())
            dt_max = pd.to_datetime(df.index.max())
            ax.set_xlim(mdates.date2num([dt_min, dt_max]))
        except (TypeError, ValueError):
            pass

    for j in range(n, len(axes)):
        axes[j].set_visible(False)

    fig.suptitle(f"Position charts  [{mode}]", fontsize=12)
    plt.tight_layout()
    if args.save:
        fig.savefig(args.save, dpi=150, bbox_inches="tight")
        print("Saved to", args.save)
    else:
        plt.show()


if __name__ == "__main__":
    main()
