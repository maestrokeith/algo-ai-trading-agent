#!/usr/bin/env python3
"""Generate a read-only dynamic scanner rejection outcome report."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.dynamic_rejection_research import (
    latest_dynamic_rejection_date,
    render_dynamic_rejection_report,
    write_dynamic_rejection_report,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a read-only report of rejected dynamic scanner candidates that later moved."
    )
    parser.add_argument("--date", required=True, help="Trading date YYYY-MM-DD or latest.")
    parser.add_argument("--user", default="live_bot", help="User id for local dynamic scan artifacts.")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--history-dir",
        type=Path,
        default=None,
        help="Override dynamic scan history directory. Defaults to DATA_DIR/dynamic_scan_history.",
    )
    parser.add_argument(
        "--bars-dir",
        type=Path,
        default=None,
        help="Optional local intraday bar directory used to backfill later same-day outcomes.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    day = str(args.date).strip()
    if day.lower() == "latest":
        latest = latest_dynamic_rejection_date(
            data_dir=args.data_dir,
            user_id=args.user,
            history_dir=args.history_dir,
        )
        if latest is None:
            print("No local dynamic rejection artifact date available.", file=sys.stderr)
            return 1
        day = latest
    try:
        md_path, json_path, report = write_dynamic_rejection_report(
            project_root=args.project_root,
            data_dir=args.data_dir,
            user_id=args.user,
            day=day,
            history_dir=args.history_dir,
            bars_dir=args.bars_dir,
        )
    except Exception as exc:
        print(f"DYNAMIC_REJECTION_REPORT_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(render_dynamic_rejection_report(report))
    print(f"Markdown: {md_path}")
    print(f"JSON: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
