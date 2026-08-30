#!/usr/bin/env python3
"""Generate research-only dynamic RVOL forward-return diagnostics."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.dynamic_rvol_forward_returns import (  # noqa: E402
    render_dynamic_rvol_forward_returns_report,
    write_dynamic_rvol_forward_returns_report,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Research-only forward-return analysis of dynamic candidates rejected by RVOL."
    )
    parser.add_argument("--date", required=True, help="Trading date YYYY-MM-DD, or latest.")
    parser.add_argument("--user", default="paper_bot", help="User id for input selection and output naming.")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    parser.add_argument("--history-dir", type=Path, default=None, help="Optional dynamic_scan_history directory.")
    parser.add_argument("--bars-dir", type=Path, default=None, help="Optional local intraday bar directory.")
    parser.add_argument("--log-path", action="append", default=[], help="Extra log file to parse.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        json_path, text_path, report = write_dynamic_rvol_forward_returns_report(
            data_dir=args.data_dir,
            day=args.date,
            user_id=args.user,
            history_dir=args.history_dir,
            bars_dir=args.bars_dir,
            log_paths=[Path(path) for path in args.log_path],
        )
    except Exception as exc:
        print(f"DYNAMIC_RVOL_FORWARD_RETURNS_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(render_dynamic_rvol_forward_returns_report(report))
    print(f"JSON: {json_path}")
    print(f"Text: {text_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
