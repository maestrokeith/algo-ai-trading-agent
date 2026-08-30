#!/usr/bin/env python3
"""Generate research-only allocator versus dispatch mismatch diagnostics."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.allocator_dispatch_mismatch_report import (  # noqa: E402
    render_allocator_dispatch_mismatch_report,
    write_allocator_dispatch_mismatch_report,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Research-only diagnostics for allocator-vs-dispatch mismatches."
    )
    parser.add_argument("--date", required=True, help="Trading date YYYY-MM-DD, or latest.")
    parser.add_argument("--user", default="paper_bot", help="User id for local artifact selection and output naming.")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--log-path", action="append", default=[], help="Extra log file to parse.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        json_path, text_path, report = write_allocator_dispatch_mismatch_report(
            project_root=args.project_root,
            data_dir=args.data_dir,
            day=args.date,
            user_id=args.user,
            log_paths=[Path(path) for path in args.log_path],
        )
    except Exception as exc:
        print(f"ALLOCATOR_DISPATCH_MISMATCH_REPORT_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(render_allocator_dispatch_mismatch_report(report))
    print(f"JSON: {json_path}")
    print(f"Text: {text_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
