#!/usr/bin/env python3
"""Dedicated premarket news collection entrypoint for systemd timers."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_app_config
from src.premarket_intelligence import PremarketJobResult, log_premarket_startup_config, run_premarket_scheduler_tick

ET = ZoneInfo("America/New_York")


def _parse_now(raw: str | None) -> datetime:
    if raw is None or raw == "":
        return datetime.now(ET)
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return dt.astimezone(ET)


def _parse_time(raw: str, fallback: time) -> time:
    try:
        hour_s, minute_s = str(raw).strip().split(":", 1)
        return time(hour=max(0, min(23, int(hour_s))), minute=max(0, min(59, int(minute_s))))
    except (TypeError, ValueError):
        return fallback


def _pm_time(config: dict, key: str, fallback: time) -> time:
    raw = config.get("premarket_intelligence")
    value = raw.get(key) if isinstance(raw, dict) else None
    return _parse_time(str(value or fallback.strftime("%H:%M")), fallback)


def _in_collection_window(config: dict, now: datetime) -> bool:
    start = _pm_time(config, "collection_start_time", time(hour=5, minute=15))
    end = _pm_time(config, "collection_end_time", time(hour=9, minute=25))
    current = now.timetz().replace(tzinfo=None)
    return start <= current <= end


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one premarket news collection refresh.")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--config", type=Path, default=None, help="Config path; defaults to config/default.yaml.")
    parser.add_argument("--now", default="", help="Use current ET time, or pass an ISO timestamp for testing.")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--force", action="store_true", help="Run even outside the configured collection window.")
    parser.add_argument("--dry-run", action="store_true", help="Do not fetch providers or mutate state.")
    return parser


def _result_persisted(result: PremarketJobResult, *, dry_run: bool) -> bool:
    """Return true when the scheduler ran a non-dry-run job without error."""
    return bool(result.ran) and not bool(dry_run) and not bool(result.error)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config_path = args.config or args.project_root / "config" / "default.yaml"
    config = load_app_config(config_path)
    config.setdefault("premarket_intelligence", {})["allow_trading"] = False
    now = _parse_now(args.now)
    pm = log_premarket_startup_config(config)
    if not pm.enabled:
        print("PREMARKET_COLLECTION_SKIP reason=disabled")
        return 0
    if not args.force and not _in_collection_window(config, now):
        print(f"PREMARKET_COLLECTION_SKIP reason=outside_window now={now.isoformat()}")
        return 0
    results = run_premarket_scheduler_tick(
        config,
        now,
        project_root=args.project_root,
        reason="premarket_collection",
        dry_run=args.dry_run,
        force_jobs=["news_5am"],
    )
    for result in results:
        print(
            "PREMARKET_COLLECTION_RESULT "
            f"job={result.job} ran={str(result.ran).lower()} "
            f"persisted={str(_result_persisted(result, dry_run=args.dry_run)).lower()} ranked={result.ranked} "
            f"news={result.news} filings={result.filings} reason={result.skipped_reason or result.reason or 'ok'}"
        )
    return 1 if any(result.error for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
