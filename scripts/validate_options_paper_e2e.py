#!/usr/bin/env python3
"""Run offline paper-options end-to-end validation."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.options_paper_validation import (
    PaperValidationBroker,
    paper_validation_config,
    sample_validation_chain,
    validate_options_paper_e2e,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="QQQ", help="Underlying symbol to validate")
    parser.add_argument("--user-id", default="paper_validation", help="Validation user id")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "paper_validation",
        help="Directory for temporary validation position state",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON report")
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    cfg = paper_validation_config(args.symbol)
    chain = sample_validation_chain(args.symbol, now)
    broker = PaperValidationBroker(chain)
    report = validate_options_paper_e2e(
        cfg,
        broker=broker,
        symbol=args.symbol,
        user_id=args.user_id,
        data_dir=args.data_dir,
        now=now,
        chain_candidates=chain,
    )
    if args.json:
        print(report.to_json())
    else:
        status = "PASS" if report.passed else "FAIL"
        print(f"{status} options paper E2E validation for {report.symbol}")
        for step in report.steps:
            marker = "PASS" if step.passed else "FAIL"
            print(f"{marker} {step.name}: {step.detail}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
