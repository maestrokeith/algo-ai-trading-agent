#!/usr/bin/env python3
"""Backfill local intraday bars for research reports."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.research_bars import backfill_research_bars, render_research_bars_backfill  # noqa: E402


def _symbols(value: str) -> list[str]:
    return [part.strip().upper() for part in value.split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Research-only 1Min bar backfill. Fetches market data only; does not trade."
    )
    parser.add_argument("--date", required=True, help="Trading date YYYY-MM-DD.")
    parser.add_argument("--user", required=True, help="User id, e.g. live_bot or paper_bot.")
    parser.add_argument("--symbols", required=True, help="Comma-separated symbols to backfill.")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    args = parser.parse_args(argv)
    try:
        report = backfill_research_bars(
            data_dir=args.data_dir,
            day=args.date,
            user_id=args.user,
            symbols=_symbols(args.symbols),
        )
    except Exception as exc:
        print(f"RESEARCH_BARS_BACKFILL_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    out_dir = args.data_dir / "research" / "bars_backfill"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{args.date}_{args.user}.json"
    text_path = out_dir / f"{args.date}_{args.user}.txt"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    text_path.write_text(render_research_bars_backfill(report), encoding="utf-8")
    print(render_research_bars_backfill(report))
    print(f"JSON: {json_path}")
    print(f"Text: {text_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
