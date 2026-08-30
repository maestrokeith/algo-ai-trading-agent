#!/usr/bin/env python
"""Run market-open readiness smoke tests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.preflight import run_preflight


def main() -> int:
    parser = argparse.ArgumentParser(description="Run AlgoSphere preflight smoke tests")
    parser.add_argument("--config", default=None, help="Path to YAML config")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args()

    report = run_preflight(config_path=args.config)
    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        status = "PASS" if report.ok else "FAIL"
        print(f"preflight: {status}")
        for check in report.checks:
            mark = "ok" if check.ok else "fail"
            print(f"{mark}\t{check.name}\t{check.reason}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
