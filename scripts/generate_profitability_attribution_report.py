"""Generate a daily profitability attribution report from local artifacts."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.profitability_attribution import (
    build_profitability_report,
    format_profitability_report,
    load_profitability_report_inputs,
    write_profitability_report,
)
from src.report_dates import latest_report_date


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate daily profitability attribution.")
    parser.add_argument("--date", default=date.today().isoformat(), help="Report date YYYY-MM-DD or latest.")
    parser.add_argument("--user", default="default", help="User id for daily artifacts.")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    parser.add_argument("--order-history", type=Path, default=None, help="Optional order history JSON path.")
    parser.add_argument("--daily-summary", type=Path, default=None, help="Optional daily summary JSON path.")
    parser.add_argument("--json-only", action="store_true", help="Suppress CLI text report.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report_date = str(args.date)
    if report_date.strip().lower() == "latest":
        latest = latest_report_date(data_dir=args.data_dir, user_id=args.user)
        if latest is None:
            print(f"No report artifacts found for user {args.user!r} under {args.data_dir}")
            return 1
        report_date = latest
    attribution, order_history, daily_summary = load_profitability_report_inputs(
        data_dir=args.data_dir,
        user_id=args.user,
        day=report_date,
        order_history_path=args.order_history,
        daily_summary_path=args.daily_summary,
    )
    report = build_profitability_report(
        user_id=args.user,
        day=report_date,
        attribution_payload=attribution,
        order_history_payload=order_history,
        daily_summary_payload=daily_summary,
    )
    path = write_profitability_report(report, data_dir=args.data_dir, user_id=args.user, day=report_date)
    if not args.json_only:
        print(format_profitability_report(report))
        print(f"\nJSON artifact: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
