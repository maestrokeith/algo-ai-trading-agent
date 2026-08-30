#!/usr/bin/env python3
"""Generate the research-only historical catalyst outcome database."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.catalyst_outcomes import latest_historical_catalyst_date, write_historical_catalyst_outcome_report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a read-only historical catalyst outcome database from local artifacts."
    )
    parser.add_argument("--date", required=True, help="Trading date YYYY-MM-DD or latest.")
    parser.add_argument("--user", default="live_bot", help="User id for local artifacts.")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    day = str(args.date).strip()
    if day.lower() == "latest":
        latest = latest_historical_catalyst_date(data_dir=args.data_dir, user_id=args.user)
        if latest is None:
            print("No local catalyst outcome date available.", file=sys.stderr)
            return 1
        day = latest
    try:
        json_path, summary_path, text = write_historical_catalyst_outcome_report(
            data_dir=args.data_dir,
            user_id=args.user,
            day=day,
        )
    except Exception as exc:
        print(f"CATALYST_OUTCOMES_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(text)
    print(f"Database: {json_path}")
    print(f"Summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
