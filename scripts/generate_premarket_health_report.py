#!/usr/bin/env python3
"""Generate and optionally deliver the daily pre-market health report."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.brokers.alpaca_client import AlpacaBroker
from src.config_loader import load_app_config
from src.premarket_health_report import (
    build_premarket_health_report,
    deliver_premarket_health_report,
    render_premarket_health_text,
    save_premarket_health_report,
)


def _slug(value: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in str(value or "default"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate pre-market readiness report")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "default.yaml")
    parser.add_argument("--output", type=Path, default=None, help="HTML output path")
    parser.add_argument("--user-label", default="default")
    parser.add_argument("--max-artifact-age-hours", type=float, default=6.0)
    parser.add_argument("--deliver", action="store_true", help="Send via configured Telegram/SMTP env")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--paper", action="store_true", help="Use paper account (default)")
    mode.add_argument("--live", action="store_true", help="Use live account for read-only health checks")
    args = parser.parse_args(argv)

    config = load_app_config(args.config)
    config.setdefault("broker", {})["paper"] = not args.live
    broker = AlpacaBroker(config, paper=not args.live)
    report = build_premarket_health_report(
        broker=broker,
        config=config,
        project_root=PROJECT_ROOT,
        now=datetime.now().astimezone(),
        max_artifact_age_hours=args.max_artifact_age_hours,
    )
    output = args.output or PROJECT_ROOT / "reports" / f"premarket_health_{_slug(args.user_label)}.html"
    written = save_premarket_health_report(report, output, user_label=args.user_label)
    print(render_premarket_health_text(report, user_label=args.user_label))
    print(f"Report: {written}")
    if args.deliver:
        deliver_premarket_health_report(report, html_path=written, user_label=args.user_label)
    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
