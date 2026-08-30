#!/usr/bin/env python3
"""Generate the read-only strategy quality report."""

from __future__ import annotations

import argparse
from pathlib import Path

from src.strategy_quality_report import build_strategy_quality_report, render_strategy_quality_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="Session date YYYY-MM-DD")
    parser.add_argument("--user", default="default", help="User id")
    parser.add_argument("--data-dir", default="data", help="Runtime data directory")
    parser.add_argument("--reports-dir", default="reports", help="Reports output directory")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_strategy_quality_report(
        data_dir=Path(args.data_dir),
        reports_dir=Path(args.reports_dir),
        user_id=args.user,
        day=args.date,
    )
    print(render_strategy_quality_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
