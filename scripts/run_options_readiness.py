#!/usr/bin/env python3
"""Read-only options readiness check."""
from __future__ import annotations

import argparse
from pathlib import Path

from src.options_readiness import (
    build_options_readiness,
    format_options_readiness,
    load_effective_runtime_config,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    env = parser.add_mutually_exclusive_group(required=True)
    env.add_argument("--live", action="store_true")
    env.add_argument("--paper", action="store_true")
    parser.add_argument("--user", default=None)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    environment = "live" if args.live else "paper"
    user_id = args.user or ("live_bot" if environment == "live" else "paper_bot")
    config = load_effective_runtime_config(args.project_root, environment=environment, user_id=user_id)
    readiness = build_options_readiness(
        config,
        environment=environment,
        user_id=user_id,
        root=args.project_root,
    )
    for line in format_options_readiness(readiness):
        print(line)
    return 0 if readiness.final_status == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
