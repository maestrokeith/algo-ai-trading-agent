#!/usr/bin/env python3
"""Generate research-only unstable quote rejection diagnostics."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.unstable_quote_research import (  # noqa: E402
    render_unstable_quote_research_report,
    write_unstable_quote_research_report,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Research-only first-window analysis of dynamic unstable_quote rejections."
    )
    parser.add_argument("--date", required=True, help="Trading date YYYY-MM-DD, or latest.")
    parser.add_argument("--user", default="live_bot", help="User id for dynamic scan artifacts.")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    parser.add_argument("--history-dir", type=Path, default=None, help="Optional dynamic_scan_history directory.")
    parser.add_argument("--bars-dir", type=Path, default=None, help="Optional local intraday bar directory.")
    parser.add_argument("--window-minutes", type=int, default=30, help="Minutes after 09:30 ET to analyze.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        json_path, text_path, report = write_unstable_quote_research_report(
            data_dir=args.data_dir,
            day=args.date,
            user_id=args.user,
            history_dir=args.history_dir,
            bars_dir=args.bars_dir,
            window_minutes=args.window_minutes,
        )
    except Exception as exc:
        print(f"UNSTABLE_QUOTE_REPORT_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(render_unstable_quote_research_report(report))
    print(f"JSON: {json_path}")
    print(f"Markdown: {text_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
