#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.intraday_health import build_intraday_health_report, save_intraday_health_report


log = logging.getLogger("intraday_health_agent")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only intraday runtime health agent.")
    env = parser.add_mutually_exclusive_group()
    env.add_argument("--live", action="store_true", help="Check live runtime health")
    env.add_argument("--paper", action="store_true", help="Check paper runtime health")
    parser.add_argument("--since", default="30 min ago", help='Journal lookback, e.g. "30 min ago"')
    parser.add_argument("--json", action="store_true", help="Print the JSON report to stdout")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args(argv)
    env_name = "live" if args.live or not args.paper else "paper"
    report = build_intraday_health_report(root=PROJECT_ROOT, env_name=env_name, since=args.since)
    path = save_intraday_health_report(report, PROJECT_ROOT)
    recommendations = ",".join(str(x) for x in report.get("recommendations") or [])
    log.info(
        "INTRADAY_HEALTH env=%s status=%s recommendations=%s path=%s",
        report.get("env"),
        report.get("status"),
        recommendations,
        path,
    )
    if args.json:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
