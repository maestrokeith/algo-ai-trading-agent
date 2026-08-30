#!/usr/bin/env python3
"""Generate allocator threshold and dynamic RVOL research artifacts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.allocator_threshold_research import (  # noqa: E402
    latest_allocator_threshold_date,
    render_allocator_threshold_research_report,
    write_allocator_threshold_research_report,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Research-only report for allocator minimum-deploy and dynamic RVOL blockers."
    )
    parser.add_argument("--date", required=True, help="Trading date YYYY-MM-DD or latest.")
    parser.add_argument("--user", default="paper_bot", help="User id for local artifacts.")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--recent-days", type=int, default=5)
    parser.add_argument("--log-path", action="append", default=[], help="Extra log path to parse.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    day = str(args.date).strip()
    if day.lower() == "latest":
        latest = latest_allocator_threshold_date(data_dir=args.data_dir, user_id=args.user)
        if latest is None:
            print("No local allocator threshold research date available.", file=sys.stderr)
            return 1
        day = latest
    try:
        json_path, txt_path, report = write_allocator_threshold_research_report(
            project_root=args.project_root,
            data_dir=args.data_dir,
            day=day,
            user_id=args.user,
            log_paths=[Path(p) for p in args.log_path],
            recent_days=max(1, int(args.recent_days)),
        )
    except Exception as exc:
        print(f"ALLOCATOR_THRESHOLD_RESEARCH_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(render_allocator_threshold_research_report(report))
    print(f"JSON: {json_path}")
    print(f"Text: {txt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
