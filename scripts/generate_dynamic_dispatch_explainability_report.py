#!/usr/bin/env python3
"""Generate dispatcher-stage explainability for dynamic allocator actions."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.dynamic_dispatch_explainability_report import (  # noqa: E402
    render_dynamic_dispatch_explainability_report,
    write_dynamic_dispatch_explainability_report,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dispatcher-stage explainability for allocator-created dynamic actions."
    )
    parser.add_argument("--date", required=True, help="Trading date YYYY-MM-DD, or latest.")
    parser.add_argument("--user", default="live_bot", help="User id for artifact selection and output naming.")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--log-path", action="append", default=[], help="Extra log file to parse.")
    parser.add_argument("--log-file", action="append", default=[], help="Alias for --log-path.")
    parser.add_argument("--bars-dir", type=Path, default=None, help="Optional local OHLCV directory for forward returns.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    log_paths = [Path(path) for path in [*args.log_path, *args.log_file]]
    try:
        json_path, text_path, report = write_dynamic_dispatch_explainability_report(
            project_root=args.project_root,
            data_dir=args.data_dir,
            day=args.date,
            user_id=args.user,
            log_paths=log_paths,
            bars_dir=args.bars_dir,
        )
    except Exception as exc:
        print(f"DYNAMIC_DISPATCH_EXPLAINABILITY_REPORT_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(render_dynamic_dispatch_explainability_report(report))
    print(f"JSON: {json_path}")
    print(f"Text: {text_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
