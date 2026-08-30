#!/usr/bin/env python3
"""Print read-only options pilot status and recent lane/order evidence."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.options_pilot_status import build_options_pilot_status, format_options_pilot_status


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    env = parser.add_mutually_exclusive_group()
    env.add_argument("--live", action="store_true", help="Inspect live options pilot.")
    env.add_argument("--paper", action="store_true", help="Inspect paper options lane.")
    parser.add_argument("--user", default=None, help="Optional configured user id.")
    parser.add_argument("--since", default="2 hours ago", help="Recent journal lookback.")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    env_name = "live" if args.live or not args.paper else "paper"
    status = build_options_pilot_status(
        root=args.project_root,
        env_name=env_name,
        user_id=args.user,
        since=args.since,
    )
    for line in format_options_pilot_status(status):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
