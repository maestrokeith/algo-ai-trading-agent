#!/usr/bin/env python3
"""
Download historical daily OHLCV from Alpaca and save as CSV for backtesting.

Usage:
  python scripts/download_backtest_data.py --start 2023-01-01 --end 2024-12-31 --out data/backtest

Uses universe.symbols from config unless --symbols is given. Creates --out if missing.
"""
import argparse
import sys
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_config
from src.backtest.data import load_alpaca_data


def main() -> None:
    parser = argparse.ArgumentParser(description="Download backtest data from Alpaca to CSV")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--out", type=str, default=str(PROJECT_ROOT / "data" / "backtest"), help="Output directory")
    parser.add_argument("--config", type=str, default=None, help="Config path")
    parser.add_argument("--symbols", type=str, nargs="*", default=None, help="Symbols (default: from config)")
    args = parser.parse_args()

    config_path = Path(args.config) if args.config else PROJECT_ROOT / "config" / "default.yaml"
    config = load_config(config_path)
    symbols = args.symbols or config.get("universe", {}).get("symbols", ["SPY"])
    if isinstance(symbols, str):
        symbols = [symbols]

    start_d = datetime.strptime(args.start, "%Y-%m-%d").date()
    end_d = datetime.strptime(args.end, "%Y-%m-%d").date()
    data = load_alpaca_data(
        symbols,
        datetime.combine(start_d, datetime.min.time()),
        datetime.combine(end_d, datetime.min.time()),
        config,
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for sym, df in data.items():
        df = df.copy()
        df.index.name = "date"
        path = out_dir / f"{sym}.csv"
        df.to_csv(path)
        print(f"Wrote {path} ({len(df)} rows)")
    print(f"Done. Run backtest with: python scripts/run_backtest.py --data-dir {out_dir} --start {args.start} --end {args.end}")


if __name__ == "__main__":
    main()
