#!/usr/bin/env python3
"""Backfill local 1Min bars for forward-outcome analytics."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.research_bars import render_forward_bars_backfill, write_forward_bars_backfill  # noqa: E402


def _symbols(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [part.strip().upper() for part in value.split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Research-only forward bar backfill. Fetches market data only; does not trade."
    )
    parser.add_argument("--date", required=True, help="Trading date YYYY-MM-DD.")
    parser.add_argument("--user", required=True, help="User id, e.g. live_bot.")
    parser.add_argument("--symbols", help="Optional comma-separated symbols. Defaults to canonical lifecycle discovery.")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--retry-sleep-seconds", type=float, default=1.0)
    parser.add_argument("--rate-limit-sleep-seconds", type=float, default=0.0)
    parser.add_argument("--force", action="store_true", help="Refetch symbols even when a complete local file exists.")
    parser.add_argument("--force-refetch", action="store_true", help="Alias for --force.")
    parser.add_argument("--repair", action="store_true", help="Explicitly allow corrupted-cache repair; this is the safe default.")
    parser.add_argument(
        "--diagnostic",
        action="store_true",
        help="Include provider request/response shape diagnostics. Market-data read only; never trades.",
    )
    args = parser.parse_args(argv)
    try:
        json_path, text_path, report = write_forward_bars_backfill(
            project_root=args.project_root,
            data_dir=args.data_dir,
            day=args.date,
            user_id=args.user,
            symbols=_symbols(args.symbols),
            max_attempts=args.max_attempts,
            retry_sleep_seconds=args.retry_sleep_seconds,
            rate_limit_sleep_seconds=args.rate_limit_sleep_seconds,
            force=bool(args.force or args.force_refetch),
            diagnostic=bool(args.diagnostic),
        )
    except Exception as exc:
        print(f"FORWARD_BARS_BACKFILL_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(render_forward_bars_backfill(report))
    print(f"JSON: {json_path}")
    print(f"Text: {text_path}")
    return 0 if int((report.get("summary") or {}).get("failed") or 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
