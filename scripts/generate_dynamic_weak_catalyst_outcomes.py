#!/usr/bin/env python3
"""Generate read-only weak-catalyst dynamic skip outcome research."""

from __future__ import annotations

import argparse
from pathlib import Path

from src.dynamic_weak_catalyst_outcomes import (
    render_dynamic_weak_catalyst_outcomes,
    write_dynamic_weak_catalyst_outcomes,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="Trading date, YYYY-MM-DD.")
    parser.add_argument("--user", default="live_bot", help="User id, e.g. live_bot.")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data", help=argparse.SUPPRESS)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--bars-dir", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--log-file", action="append", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--debug", action="store_true", help="Print parsed ORDER_SKIP and ALLOCATOR ACTIONS context.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    json_path, text_path, report = write_dynamic_weak_catalyst_outcomes(
        project_root=args.project_root,
        data_dir=args.data_dir,
        day=args.date,
        user_id=args.user,
        bars_dir=args.bars_dir,
        log_files=args.log_file,
    )
    if args.debug:
        debug = report.get("debug") if isinstance(report.get("debug"), dict) else {}
        print("DEBUG dynamic-weak-catalyst-outcomes")
        print(f"LOG_SOURCE {debug.get('LOG_SOURCE', 'unknown')}")
        print(f"used_journalctl={'yes' if debug.get('used_journalctl') else 'no'}")
        print(f"total journal lines={debug.get('journal_lines_total', 0)}")
        print(f"log_lines_read={debug.get('log_lines_read', 0)}")
        print("grep_counts:")
        for key, value in (debug.get("grep_counts") or {}).items():
            print(f"  {key}={value}")
        print("parsed_allocator_actions:")
        for row in debug.get("parsed_allocator_actions") or []:
            print(
                "  timestamp={timestamp} symbol={symbol} entry_price={entry_price} "
                "relative_volume={relative_volume} gain_pct={gain_pct} "
                "catalyst_age_minutes={catalyst_age_minutes} market_regime={market_regime}".format(**row)
            )
        print("parsed_order_skips:")
        for row in debug.get("parsed_order_skips") or []:
            print(
                "  timestamp={timestamp} symbol={symbol} matched_allocator_context={matched_allocator_context} "
                "matched_scan_context={matched_scan_context} entry_price={entry_price} "
                "relative_volume={relative_volume} gain_pct={gain_pct} "
                "catalyst_age_minutes={catalyst_age_minutes}".format(**row)
            )
    print(render_dynamic_weak_catalyst_outcomes(report))
    print(f"JSON: {json_path}")
    print(f"Markdown: {text_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
