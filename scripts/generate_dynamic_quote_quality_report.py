#!/usr/bin/env python3
"""Generate a read-only dynamic quote quality report."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.dynamic_quote_quality_report import (  # noqa: E402
    render_dynamic_quote_quality_report,
    write_dynamic_quote_quality_report,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="Trading date, YYYY-MM-DD.")
    parser.add_argument("--user", default="live_bot", help="User id, e.g. live_bot.")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data", help=argparse.SUPPRESS)
    parser.add_argument("--history-dir", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--log-file", action="append", type=Path, default=None, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the report generator."""
    args = build_parser().parse_args(argv)
    json_path, text_path, report = write_dynamic_quote_quality_report(
        project_root=args.project_root,
        data_dir=args.data_dir,
        day=args.date,
        user_id=args.user,
        history_dir=args.history_dir,
        log_files=args.log_file,
    )
    print(render_dynamic_quote_quality_report(report))
    print(f"JSON: {json_path}")
    print(f"Markdown: {text_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
