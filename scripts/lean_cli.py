#!/usr/bin/env python3
"""
Lean-style CLI for the algo engine (inspired by QuantConnect Lean).

Commands:
  lean backtest    Run backtest (CSV or Alpaca data)
  lean live        Run live/paper trading loop until market close
  lean research    (Optional) Start Jupyter for research

Usage (from repo root; root ``lean`` delegates here)::

  python lean backtest --data-dir data/backtest --start 2023-01-01 --end 2024-12-31
  python lean backtest --alpaca --start 2023-01-01 --end 2024-12-31
  python lean live

Or: ``python scripts/lean_cli.py <subcommand> ...``
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def cmd_backtest(args: argparse.Namespace) -> int:
    from src.algorithm.trend_following import TrendFollowingAlgorithm
    from src.backtest.data import load_alpaca_data, load_csv_data
    from src.backtest.metrics import compute_metrics
    from src.config_loader import load_config
    from src.engine.backtest_engine import EngineBacktest

    config = load_config(Path(args.config) if args.config else PROJECT_ROOT / "config" / "default.yaml")
    symbols = args.symbols or config.get("universe", {}).get("symbols", ["SPY"])
    if isinstance(symbols, str):
        symbols = [symbols]

    start_date = datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else None
    end_date = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else None

    if args.alpaca:
        if not args.start or not args.end:
            print("--alpaca requires --start and --end (YYYY-MM-DD)")
            return 1
        print(f"Fetching data from Alpaca for {len(symbols)} symbols ({args.start} to {args.end})...")
        data = load_alpaca_data(
            symbols,
            datetime.combine(start_date, datetime.min.time()),
            datetime.combine(end_date, datetime.min.time()),
            config,
        )
    elif args.data_dir:
        data_dir = Path(args.data_dir)
        if not data_dir.is_dir():
            print(f"Data directory not found: {data_dir}")
            return 1
        print(f"Loading CSV from {data_dir}...")
        data = load_csv_data(data_dir, symbols)
    else:
        print("Provide --data-dir DIR or --alpaca with --start and --end")
        return 1

    if not data:
        print("No data loaded.")
        return 1

    print(f"Running backtest with TrendFollowingAlgorithm ({len(data)} symbols)...")
    engine = EngineBacktest(config=config)
    algorithm = TrendFollowingAlgorithm(config=config)
    result = engine.run(algorithm, data, initial_equity=args.equity, start_date=start_date, end_date=end_date)

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
    if metrics.avg_trade_pnl_pct is not None:
        print(f"  Avg trade PnL:   {metrics.avg_trade_pnl_pct:+.2f}%")
    print("=" * 50)
    if result.trades:
        print("\nLast 10 trades:")
        for t in result.trades[-10:]:
            print(f"  {t.entry_date} -> {t.exit_date}  {t.symbol}  qty={t.qty}  PnL={t.pnl:+.2f} ({t.pnl_pct:+.2f}%)")
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    """Run live/paper trading loop (delegate to run_alpaca_loop)."""
    cmd = [sys.executable, str(PROJECT_ROOT / "scripts" / "run_alpaca_loop.py")]
    if args.live:
        cmd.append("--live")
    else:
        cmd.append("--paper")
    if args.verbose:
        cmd.append("-v")
    return subprocess.run(cmd, cwd=str(PROJECT_ROOT)).returncode


def main() -> int:
    parser = argparse.ArgumentParser(prog="lean", description="Lean-style CLI for algo engine")
    sub = parser.add_subparsers(dest="command", required=True)

    bp = sub.add_parser("backtest", help="Run backtest")
    bp.add_argument("--data-dir", type=str, help="Directory with SYMBOL.csv files")
    bp.add_argument("--alpaca", action="store_true", help="Fetch data from Alpaca")
    bp.add_argument("--start", type=str, help="Start date YYYY-MM-DD")
    bp.add_argument("--end", type=str, help="End date YYYY-MM-DD")
    bp.add_argument("--equity", type=float, default=100_000.0, help="Initial equity")
    bp.add_argument("--config", type=str, help="Config YAML path")
    bp.add_argument("--symbols", type=str, nargs="*", help="Symbols (default: from config)")
    bp.set_defaults(func=cmd_backtest)

    lp = sub.add_parser("live", help="Run live/paper trading loop")
    lp.add_argument("--live", action="store_true", help="Use live account")
    lp.add_argument("--paper", action="store_true", help="Use paper account (default)")
    lp.add_argument("-v", "--verbose", action="store_true")
    lp.set_defaults(func=cmd_live)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
