#!/usr/bin/env python3
"""Generate a read-only dynamic weak-catalyst review."""

from __future__ import annotations

import argparse
from pathlib import Path

from src.dynamic_weak_catalyst_report import (
    build_dynamic_weak_catalyst_report,
    format_dynamic_weak_catalyst_report,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="Trading date, YYYY-MM-DD.")
    parser.add_argument("--user", default="live_bot", help="User id, e.g. live_bot.")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data", help=argparse.SUPPRESS)
    parser.add_argument("--log-file", type=Path, default=None, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_text = None
    if args.log_file is not None:
        log_text = args.log_file.read_text(encoding="utf-8", errors="replace")
    report = build_dynamic_weak_catalyst_report(
        data_dir=args.data_dir,
        user_id=args.user,
        day=args.date,
        log_text=log_text,
    )
    print(format_dynamic_weak_catalyst_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
