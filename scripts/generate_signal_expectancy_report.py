#!/usr/bin/env python3
"""Generate read-only signal expectancy report."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.signal_expectancy_report import render_signal_expectancy_report, write_signal_expectancy_report  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="Trading date, YYYY-MM-DD.")
    parser.add_argument("--user", default="live_bot", help="User id, e.g. live_bot.")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data", help=argparse.SUPPRESS)
    parser.add_argument("--bars-dir", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--log-file", action="append", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--debug", action="store_true", help="Print bar-loading diagnostics.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        json_path, text_path, report = write_signal_expectancy_report(
            project_root=args.project_root,
            data_dir=args.data_dir,
            day=args.date,
            user_id=args.user,
            bars_dir=args.bars_dir,
            log_files=args.log_file,
        )
    except Exception as exc:
        print(f"SIGNAL_EXPECTANCY_REPORT_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(render_signal_expectancy_report(report))
    if args.debug:
        quality = report.get("data_quality", {})
        print("DEBUG")
        print(f"  bars_directories_checked={quality.get('bars_directories_checked', [])}")
        print(f"  bars_files_found={len(quality.get('bars_files_found', []))}")
        for path in quality.get("bars_files_found", [])[:20]:
            print(f"    file={path}")
        print(f"  symbols_with_bars={quality.get('symbols_with_bars', [])}")
        print(f"  missing_symbols={quality.get('missing_symbols', [])}")
    print(f"JSON: {json_path}")
    print(f"Markdown: {text_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
