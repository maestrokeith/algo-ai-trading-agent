#!/usr/bin/env python3
"""Generate a read-only dynamic momentum gate research report."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.dynamic_gate_research import (
    latest_dynamic_gate_research_date,
    render_dynamic_gate_research_report,
    write_dynamic_gate_research_report,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a read-only report explaining downstream gates for dynamic momentum candidates."
    )
    parser.add_argument("--date", required=True, help="Trading date YYYY-MM-DD or latest.")
    parser.add_argument("--user", default="live_bot", help="User id for local artifacts.")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--log-file",
        action="append",
        type=Path,
        default=[],
        help="Optional additional local log file to parse. Can be repeated.",
    )
    parser.add_argument(
        "--bars-dir",
        type=Path,
        default=None,
        help="Optional local intraday bar directory for forward-return research.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    day = str(args.date).strip()
    if day.lower() == "latest":
        latest = latest_dynamic_gate_research_date(
            project_root=args.project_root,
            data_dir=args.data_dir,
            user_id=args.user,
        )
        if latest is None:
            print("No local dynamic gate research date available.", file=sys.stderr)
            return 1
        day = latest
    try:
        json_path, txt_path, report = write_dynamic_gate_research_report(
            project_root=args.project_root,
            data_dir=args.data_dir,
            day=day,
            user_id=args.user,
            log_paths=args.log_file,
            bars_dir=args.bars_dir,
        )
    except Exception as exc:
        print(f"DYNAMIC_GATE_RESEARCH_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(render_dynamic_gate_research_report(report))
    print(f"JSON: {json_path}")
    print(f"Text: {txt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
