"""CLI entrypoint for the live trading loop."""

from __future__ import annotations

import argparse
import logging
import os

from src.app.live_cycle import run_live_cycle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run trading loop until market close")
    parser.add_argument("--live", action="store_true", help="Use live account (real money)")
    parser.add_argument("--paper", action="store_true", help="Use paper account (default)")
    parser.add_argument(
        "--mode",
        choices=["live", "paper", "shadow", "entries-disabled"],
        default=None,
        help="Explicit trading-control mode.",
    )
    parser.add_argument(
        "--user",
        type=str,
        default=None,
        help="Run only this user_id (multi-user mode)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print skip reasons for all symbols in trend scan; inverse (SQQQ/SPXS) skips always print without -v",
    )
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="Allow multiple loop processes for the same user (unsafe — can duplicate orders)",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.live and args.paper:
        parser.error("Use only one of --live or --paper")
    verbose = getattr(args, "verbose", False)
    if verbose or os.environ.get("LOGLEVEL", "").strip().upper() == "DEBUG":
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_live_cycle(args)
