#!/usr/bin/env python3

"""Manual dry-run for the live premarket intelligence job."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_app_config
from src.premarket_intelligence import (
    log_premarket_startup_config,
    run_premarket_scheduler_tick,
)


def _parse_now(raw: str | None) -> datetime:
    et = ZoneInfo("America/New_York")
    if raw is None or raw == "":
        return datetime.now(et)
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=et)
    return dt.astimezone(et)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the premarket intelligence job once in dry-run mode")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Run without allowing trading or state mutation",
    )
    parser.add_argument(
        "--now",
        nargs="?",
        const="",
        default="",
        help="Use current ET time, or pass an ISO timestamp for testing",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose logging",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    config_path = PROJECT_ROOT / "config" / "default.yaml"
    config = load_app_config(config_path)
    config.setdefault("premarket_intelligence", {})["allow_trading"] = False

    now = _parse_now(args.now)
    log_premarket_startup_config(config)
    run_premarket_scheduler_tick(
        config,
        now,
        project_root=PROJECT_ROOT,
        reason="manual_debug",
        dry_run=True,
        force_jobs=["news_5am"],
    )


if __name__ == "__main__":
    main()
