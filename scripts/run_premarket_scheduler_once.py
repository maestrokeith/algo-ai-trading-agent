"""Dry-run the premarket intelligence scheduler once."""

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
    default_state_path,
    due_premarket_jobs,
    log_premarket_startup_config,
    run_premarket_scheduler_tick,
)


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _parse_now(raw: str | None) -> datetime:
    et = ZoneInfo("America/New_York")
    if raw is None or raw == "":
        return datetime.now(et)
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=et)
    return dt.astimezone(et)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dry-run premarket intelligence scheduler")
    parser.add_argument("--live", action="store_true", help="Use live-mode config override")
    parser.add_argument("--paper", action="store_true", help="Use paper-mode config override")
    parser.add_argument(
        "--job",
        choices=["news_5am"],
        default=None,
        help="Run this premarket job immediately, regardless of due status",
    )
    parser.add_argument("--debug", action="store_true", help="Print debug logs and step output")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute network fetches and mark state on success; default is dry-run",
    )
    parser.add_argument(
        "--now",
        nargs="?",
        const="",
        default="",
        help="Use current ET time, or pass an ISO timestamp for testing",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.live and args.paper:
        raise SystemExit("Use only one of --live or --paper")

    config_path = PROJECT_ROOT / "config" / "default.yaml"
    config = load_app_config(config_path)
    if args.live:
        config.setdefault("broker", {})["paper"] = False
    elif args.paper:
        config.setdefault("broker", {})["paper"] = True

    now = _parse_now(args.now)
    state_path = default_state_path(PROJECT_ROOT)
    pm = log_premarket_startup_config(config)
    due = due_premarket_jobs(config, now, state_path=state_path)
    reason = "manual_debug" if args.debug else "manual"
    results = run_premarket_scheduler_tick(
        config,
        now,
        project_root=PROJECT_ROOT,
        reason=reason,
        dry_run=not args.execute,
        force_jobs=[args.job] if args.job else None,
    )

    print(f"CONFIG_PATH_LOADED path={config_path}")
    print(f"PREMARKET_CONFIG enabled={_bool_text(pm.enabled)}")
    print(f"PREMARKET_CONFIG keep_alive_overnight={_bool_text(pm.keep_alive_overnight)}")
    print(f"PREMARKET_CONFIG allow_trading={_bool_text(pm.allow_trading)}")
    print(f"PREMARKET_CONFIG news_scan_time={pm.news_scan_time}")
    print(f"NOW {now.isoformat()}")
    print(f"DRY_RUN {_bool_text(not args.execute)}")
    print("DUE_JOBS " + (",".join(due) if due else "none"))
    for result in results:
        status = "ran" if result.ran else "skipped"
        reason = result.skipped_reason or result.reason or "none"
        print(
            "JOB job=%s due=%s status=%s reason=%s"
            % (result.job, _bool_text(result.due), status, reason)
        )
    if any(result.error for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
