#!/usr/bin/env python3
"""Validate premarket artifacts before market open."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.premarket_readiness import check_premarket_readiness, format_premarket_readiness


def _parse_now(raw: str | None) -> datetime | None:
    if not raw:
        return None
    et = ZoneInfo("America/New_York")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=et)
    return dt.astimezone(et)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check premarket artifact readiness before market open.")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--now", default=None, help="Optional ISO timestamp for testing.")
    parser.add_argument("--warn-only", action="store_true", help="Always exit 0 after printing readiness.")
    args = parser.parse_args(argv)

    readiness = check_premarket_readiness(args.project_root, now=_parse_now(args.now))
    print(format_premarket_readiness(readiness))
    if args.warn_only:
        return 0
    return 0 if readiness.fresh and readiness.catalyst_ranked_symbols > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
