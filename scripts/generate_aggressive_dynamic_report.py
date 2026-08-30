#!/usr/bin/env python3
"""Generate read-only aggressive dynamic-entry comparison report."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.aggressive_dynamic_report import render_aggressive_dynamic_report, write_aggressive_dynamic_report  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--user", default="live_bot")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data", help=argparse.SUPPRESS)
    parser.add_argument("--reports-dir", type=Path, default=PROJECT_ROOT / "reports", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    json_path, report = write_aggressive_dynamic_report(
        data_dir=args.data_dir,
        reports_dir=args.reports_dir,
        user_id=args.user,
        day=args.date,
    )
    print(render_aggressive_dynamic_report(report))
    print(f"JSON: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
