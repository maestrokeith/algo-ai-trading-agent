#!/usr/bin/env python3
"""Show shadow-live options validation report."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_config
from src.live.options_shadow import summarize_shadow_report

log = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Read the live shadow ledger")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    config = load_config(PROJECT_ROOT / "config" / "default.yaml")
    user_id = str((config.get("user") or {}).get("id") or "default")
    data_dir = PROJECT_ROOT / "data"
    summary = summarize_shadow_report(user_id=user_id, data_dir=data_dir)
    line = (
        "OPTIONS_SHADOW_REPORT_SUMMARY "
        f"total_intents={summary.total_intents} "
        f"total_fills={summary.total_fills} "
        f"open_positions={summary.open_positions} "
        f"closed_positions={summary.closed_positions} "
        f"win_rate={summary.win_rate:.2f}% "
        f"realized_pl={summary.realized_pl:.2f} "
        f"unrealized_pl={summary.unrealized_pl:.2f} "
        f"total_pl={summary.total_pl:.2f} "
        f"avg_slippage_bps={summary.avg_slippage_bps:.2f} "
        f"avg_hold_minutes={summary.avg_hold_minutes:.2f} "
        f"best_symbol={summary.best_symbol or 'none'} "
        f"worst_symbol={summary.worst_symbol or 'none'} "
        f"exit_reasons={summary.exit_reason_breakdown}"
    )
    log.info(line)
    print(line, flush=True)
    if summary.exit_reason_breakdown:
        print("exit_reason_breakdown:", summary.exit_reason_breakdown, flush=True)


if __name__ == "__main__":
    main()

