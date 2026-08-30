#!/usr/bin/env python3
"""Generate read-only trend-long loss review report."""

from __future__ import annotations

import argparse
from pathlib import Path

from src.trend_long_loss_review import render_trend_long_loss_review, write_trend_long_loss_review


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="Trading date, YYYY-MM-DD.")
    parser.add_argument("--user", default="live_bot", help="User id, e.g. live_bot.")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data", help=argparse.SUPPRESS)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--bars-dir", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--log-file", action="append", type=Path, default=None, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    json_path, text_path, report = write_trend_long_loss_review(
        project_root=args.project_root,
        data_dir=args.data_dir,
        day=args.date,
        user_id=args.user,
        bars_dir=args.bars_dir,
        log_files=args.log_file,
    )
    print(render_trend_long_loss_review(report))
    print(f"JSON: {json_path}")
    print(f"Markdown: {text_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
