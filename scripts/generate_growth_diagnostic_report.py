#!/usr/bin/env python3
"""Generate a read-only account growth diagnostic report."""
from __future__ import annotations

import argparse
from pathlib import Path

from src.growth_diagnostic_report import render_growth_diagnostic_report, write_growth_diagnostic_report

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default="latest")
    parser.add_argument("--lookback-days", type=int, default=10)
    parser.add_argument("--user", default="live_bot")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    artifacts = write_growth_diagnostic_report(
        args.project_root,
        end_date=args.date,
        lookback_days=args.lookback_days,
        user_id=args.user,
    )
    print(render_growth_diagnostic_report(artifacts.report))
    print(f"GROWTH_DIAGNOSTIC_JSON path={artifacts.json_path}")
    print(f"GROWTH_DIAGNOSTIC_HTML path={artifacts.html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
