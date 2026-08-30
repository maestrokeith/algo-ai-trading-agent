#!/usr/bin/env python3
"""
Run backtest over historical OHLCV data.

Data source:
  --data-dir DIR   Load CSV files DIR/SYMBOL.csv (columns: date, open, high, low, close, volume).
  --alpaca         Fetch from Alpaca API (requires --start and --end).

Example:
  python scripts/run_backtest.py --data-dir data/backtest --start 2023-01-01 --end 2024-12-31
  python scripts/run_backtest.py --alpaca --start 2023-01-01 --end 2024-12-31
"""
import argparse
import sys
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_config
from src.backtest import BacktestEngine, load_csv_data, load_alpaca_data, compute_metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="Run strategy backtest")
    parser.add_argument("--config", type=str, default=None, help="Config YAML path (default: config/default.yaml)")
    parser.add_argument("--data-dir", type=str, default=None, help="Directory with SYMBOL.csv files")
    parser.add_argument("--alpaca", action="store_true", help="Fetch data from Alpaca instead of CSV")
    parser.add_argument("--start", type=str, default=None, help="Start date YYYY-MM-DD (for Alpaca or filter)")
    parser.add_argument("--end", type=str, default=None, help="End date YYYY-MM-DD")
    parser.add_argument("--equity", type=float, default=100_000.0, help="Initial equity")
    parser.add_argument("--spread-pct", type=float, default=0.15, help="Assumed spread %% for entry/exit checks")
    parser.add_argument("--symbols", type=str, nargs="*", default=None, help="Override symbols (default: from config)")
    args = parser.parse_args()

    config_path = Path(args.config) if args.config else PROJECT_ROOT / "config" / "default.yaml"
    config = load_config(config_path)
    symbols = args.symbols or config.get("universe", {}).get("symbols", ["SPY"])
    if isinstance(symbols, str):
        symbols = [symbols]

    start_date = None
    end_date = None
    if args.start:
        start_date = datetime.strptime(args.start, "%Y-%m-%d").date()
    if args.end:
        end_date = datetime.strptime(args.end, "%Y-%m-%d").date()

    if args.alpaca:
        if not start_date or not end_date:
            print("--alpaca requires --start and --end (YYYY-MM-DD)")
            sys.exit(1)
        print(f"Fetching data from Alpaca for {len(symbols)} symbols ({args.start} to {args.end})...")
        data = load_alpaca_data(symbols, datetime.combine(start_date, datetime.min.time()), datetime.combine(end_date, datetime.min.time()), config)
    elif args.data_dir:
        data_dir = Path(args.data_dir)
        if not data_dir.is_dir():
            print(f"Data directory not found: {data_dir}")
            sys.exit(1)
        print(f"Loading CSV from {data_dir} for {symbols}...")
        data = load_csv_data(data_dir, symbols)
    else:
        print("Provide --data-dir DIR or --alpaca with --start and --end")
        sys.exit(1)

    if not data:
        print("No data loaded. Check paths/symbols and CSV format (date, open, high, low, close, volume).")
        sys.exit(1)

    print(f"Loaded {len(data)} symbols: {list(data.keys())}")
    engine = BacktestEngine(config=config)
    result = engine.run(
        data,
        initial_equity=args.equity,
        spread_pct=args.spread_pct,
        start_date=start_date,
        end_date=end_date,
    )

    metrics = compute_metrics(result)
    print()
    print("=" * 50)
    print("BACKTEST RESULTS")
    print("=" * 50)
    print(f"  Initial equity:  ${result.initial_equity:,.0f}")
    print(f"  Final equity:    ${result.final_equity:,.0f}")
    print(f"  Total return:    {metrics.total_return_pct:+.2f}%")
    if metrics.cagr_pct is not None:
        print(f"  CAGR:            {metrics.cagr_pct:+.2f}%")
    print(f"  Max drawdown:    {metrics.max_drawdown_pct:.2f}%")
    if metrics.sharpe_ratio is not None:
        print(f"  Sharpe (ann.):   {metrics.sharpe_ratio:.2f}")
    print(f"  Trades:          {metrics.num_trades} (win rate {metrics.win_rate_pct:.1f}%)")
    print(f"  Winners/Losers:  {metrics.num_winners} / {metrics.num_losers}")
    if metrics.avg_trade_pnl_pct is not None:
        print(f"  Avg trade PnL:   {metrics.avg_trade_pnl_pct:+.2f}%")
    if metrics.avg_bars_held is not None:
        print(f"  Avg bars held:   {metrics.avg_bars_held:.1f}")
    print("=" * 50)

    if result.trades:
        print("\nLast 10 trades:")
        for t in result.trades[-10:]:
            print(f"  {t.entry_date} -> {t.exit_date}  {t.symbol}  qty={t.qty}  PnL={t.pnl:+.2f} ({t.pnl_pct:+.2f}%)  [{t.exit_reason}]")


if __name__ == "__main__":
    main()
