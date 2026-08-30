#!/usr/bin/env python3
"""Generate read-only daily or weekly research feedback reports."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.research_feedback import (
    build_research_feedback_report,
    resolve_research_date,
    write_research_feedback_outputs,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate read-only strategy research feedback reports. DATE may be positional or passed with --date."
    )
    parser.add_argument("date", nargs="?", default=None, help="Report date YYYY-MM-DD, latest, or today.")
    parser.add_argument(
        "--date",
        dest="date_flag",
        default=None,
        help="Report date YYYY-MM-DD, latest, or today. Overrides positional DATE.",
    )
    parser.add_argument("--user", default="live_bot", help="User id for local artifacts.")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--weekly", action="store_true", help="Generate a weekend/weekly summary ending on DATE.")
    parser.add_argument("--lookback-days", type=int, default=None, help="Override weekly lookback window.")
    parser.add_argument("--min-samples", type=int, default=1, help="Minimum samples for ranked recommendations.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    date_value = args.date_flag or args.date or "today"
    try:
        day = resolve_research_date(data_dir=args.data_dir, user_id=args.user, value=date_value)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    lookback = args.lookback_days if args.lookback_days is not None else (7 if args.weekly else 1)
    try:
        report = build_research_feedback_report(
            data_dir=args.data_dir,
            user_id=args.user,
            day=day,
            lookback_days=lookback,
            min_samples=args.min_samples,
        )
    except Exception as exc:
        print(f"RESEARCH_FEEDBACK_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    paths = write_research_feedback_outputs(report, project_root=args.project_root)
    print(f"Research feedback report: {paths['markdown']}")
    print(f"Research dashboard JSON: {paths['dashboard_json']}")
    print(f"Research dashboard HTML: {paths['dashboard_html']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
