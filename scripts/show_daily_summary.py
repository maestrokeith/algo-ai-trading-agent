#!/usr/bin/env python3
"""Show one concise read-only daily summary from local report artifacts."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.combined_daily_summary import build_combined_daily_summary, format_combined_daily_summary
from src.report_dates import latest_report_date


def _today_et() -> str:
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Show combined daily summary from trade, catalyst, churn, profitability, and replay artifacts."
    )
    parser.add_argument("date", nargs="?", default=None, help="Report date YYYY-MM-DD, latest, or omitted for today in ET.")
    parser.add_argument("--date", dest="date_flag", default=None, help="Report date YYYY-MM-DD or latest.")
    parser.add_argument("--user", default="default", help="User id for daily artifacts.")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    parser.add_argument("--catalyst-path", type=Path, default=None, help="Optional catalyst outcome JSON path.")
    parser.add_argument("--order-history", type=Path, default=None, help="Optional order history JSON path.")
    parser.add_argument("--daily-summary", type=Path, default=None, help="Optional daily summary JSON path.")
    parser.add_argument("--replay-summary", type=Path, default=None, help="Optional replay summary JSON path.")
    parser.add_argument("--journalctl-unit", default="algo.service", help="Systemd unit to inspect for live order activity.")
    parser.add_argument("--no-journal", action="store_true", help="Do not inspect journalctl for live order activity.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    day = args.date_flag or args.date or _today_et()
    if str(day).strip().lower() == "latest":
        latest = latest_report_date(data_dir=args.data_dir, user_id=args.user)
        if latest is None:
            print(f"No report artifacts found for user {args.user!r} under {args.data_dir}", file=sys.stderr)
            return 1
        day = latest
    try:
        date.fromisoformat(day)
    except ValueError:
        print("Use date as YYYY-MM-DD, e.g. 2026-06-06", file=sys.stderr)
        return 2
    report = build_combined_daily_summary(
        data_dir=args.data_dir,
        user_id=args.user,
        day=day,
        catalyst_path=args.catalyst_path,
        order_history_path=args.order_history,
        daily_summary_path=args.daily_summary,
        replay_summary_path=args.replay_summary,
        include_journal=not args.no_journal,
        journalctl_unit=args.journalctl_unit,
    )
    print(format_combined_daily_summary(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
