#!/usr/bin/env python3
"""Report local intraday bar availability for research reports."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.research_bars import render_research_bars_status, write_research_bars_status  # noqa: E402


def _symbols(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip().upper() for part in value.split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Research-only local intraday bar availability diagnostic.")
    parser.add_argument("--date", required=True, help="Trading date YYYY-MM-DD.")
    parser.add_argument("--user", required=True, help="User id, e.g. live_bot or paper_bot.")
    parser.add_argument("--symbols", default="", help="Optional comma-separated symbol override.")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    args = parser.parse_args(argv)
    try:
        json_path, text_path, report = write_research_bars_status(
            data_dir=args.data_dir,
            day=args.date,
            user_id=args.user,
            symbols=_symbols(args.symbols),
        )
    except Exception as exc:
        print(f"RESEARCH_BARS_STATUS_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(render_research_bars_status(report))
    print(f"JSON: {json_path}")
    print(f"Text: {text_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
