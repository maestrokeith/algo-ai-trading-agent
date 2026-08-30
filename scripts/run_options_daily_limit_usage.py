#!/usr/bin/env python3
"""Read-only options daily-limit usage report."""
from __future__ import annotations

import argparse
from pathlib import Path

from src.options_daily_limit import build_options_daily_limit_usage, format_options_daily_limit_usage
from src.options_readiness import load_effective_runtime_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _max_trades(config: dict) -> int:
    opts = config.get("options") if isinstance(config.get("options"), dict) else {}
    try:
        return int(float(opts.get("max_option_trades_per_day") or 0))
    except (TypeError, ValueError):
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    env = parser.add_mutually_exclusive_group(required=True)
    env.add_argument("--live", action="store_true")
    env.add_argument("--paper", action="store_true")
    parser.add_argument("--user", default=None)
    parser.add_argument("--date", default=None, help="America/New_York trading date YYYY-MM-DD")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    environment = "live" if args.live else "paper"
    user_id = args.user or ("live_bot" if environment == "live" else "paper_bot")
    config = load_effective_runtime_config(args.project_root, environment=environment, user_id=user_id)
    usage = build_options_daily_limit_usage(
        root=args.project_root,
        user_id=user_id,
        environment=environment,
        limit=_max_trades(config),
        trading_date=args.date,
    )
    for line in format_options_daily_limit_usage(usage):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
