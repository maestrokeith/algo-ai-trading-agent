#!/usr/bin/env python3
"""Show paper options performance and promotion-gate eligibility."""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.brokers.alpaca_client import AlpacaBroker
from src.config_loader import load_config
from src.options_performance import (
    compare_stock_vs_options,
    evaluate_options_promotion_gate,
    format_options_performance_dashboard,
    summarize_options_performance,
)
from src.options_position_manager import sync_options_positions
from src.options_safety import build_options_promotion_report

log = logging.getLogger(__name__)


def _fmt_num(v: float) -> str:
    if v == float("inf"):
        return "inf"
    return f"{v:.2f}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paper", action="store_true", help="Use paper account")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    config = load_config(PROJECT_ROOT / "config" / "default.yaml")
    config.setdefault("broker", {})["paper"] = True
    broker = AlpacaBroker(config)
    user_id = str((config.get("user") or {}).get("id") or "default")
    sync_options_positions(
        broker,
        config,
        user_id=user_id,
        data_dir=PROJECT_ROOT / "data",
        execution_manager=None,
    )
    summary = summarize_options_performance(user_id=user_id, data_dir=PROJECT_ROOT / "data")
    summary_line = (
        "OPTIONS_PERFORMANCE_SUMMARY "
        f"total_trades={summary.total_trades} "
        f"wins={summary.wins} "
        f"losses={summary.losses} "
        f"win_rate={summary.win_rate:.2f}% "
        f"total_pnl={summary.total_pnl:.2f} "
        f"avg_trade_pnl={summary.avg_trade_pnl:.2f} "
        f"avg_win_pct={summary.avg_win_pct:.2f}% "
        f"avg_loss_pct={summary.avg_loss_pct:.2f}% "
        f"profit_factor={_fmt_num(summary.profit_factor)} "
        f"max_drawdown={summary.max_drawdown_pct:.2f}% "
        f"avg_hold_time_min={summary.avg_hold_time_minutes:.2f} "
        f"best_contract={summary.best_contract or 'none'} "
        f"worst_contract={summary.worst_contract or 'none'} "
        f"best_symbol={summary.best_symbol or 'none'} "
        f"worst_symbol={summary.worst_symbol or 'none'} "
        f"best_entry_reason={summary.best_entry_reason or 'none'} "
        f"worst_entry_reason={summary.worst_entry_reason or 'none'} "
        f"avg_entry_spread={summary.avg_entry_spread_pct:.2f}% "
        f"avg_entry_slippage_bps={summary.avg_entry_slippage_bps:.2f} "
        f"avg_entry_slippage_pct={summary.avg_entry_slippage_pct:.2f}% "
        f"kill_switch_days_last_5={summary.last_5_sessions_kill_switch_days or []} "
        f"exit_reasons={summary.exit_reason_breakdown}"
    )
    log.info(summary_line)
    print(summary_line, flush=True)
    stock_trades = []
    if callable(getattr(broker, "get_orders_for_date", None)):
        try:
            raw_orders = broker.get_orders_for_date(date.today())
            stock_trades = [r for r in (raw_orders or []) if isinstance(r, dict)]
        except Exception:
            stock_trades = []
    comparison = compare_stock_vs_options(summary, stock_trades)
    print(format_options_performance_dashboard(summary, comparison=comparison), flush=True)
    if summary.closed_trades:
        print("exit_reason_breakdown:", summary.exit_reason_breakdown, flush=True)
        print("best/worst contracts:", summary.best_contract or "none", "/", summary.worst_contract or "none", flush=True)
        print(
            "best/worst entry reasons:",
            summary.best_entry_reason or "none",
            "/",
            summary.worst_entry_reason or "none",
            flush=True,
        )

    passed, reasons = evaluate_options_promotion_gate(summary)
    promotion = build_options_promotion_report(
        config,
        user_id=user_id,
        data_dir=PROJECT_ROOT / "data",
    )
    print(
        "OPTIONS_PROMOTION_REPORT "
        f"promotion_verdict={promotion['promotion_verdict']} "
        f"total_trades={promotion['total_trades']} "
        f"total_paper_option_trades={promotion['total_paper_option_trades']} "
        f"win_rate={promotion['win_rate']:.2f}% "
        f"avg_win={promotion['avg_win']:.2f}% "
        f"avg_loss={promotion['avg_loss']:.2f}% "
        f"profit_factor={_fmt_num(float(promotion['profit_factor']))} "
        f"max_drawdown={promotion['max_drawdown']:.2f}% "
        f"max_drawdown_largest_loss={promotion['max_drawdown_largest_loss']:.2f}% "
        f"average_spread_paid={promotion['average_spread_paid']:.2f}% "
        f"average_hold_duration={promotion['average_hold_duration']:.2f} "
        f"missing_exits={promotion['missing_exits']} "
        f"missing_exits_count={promotion['missing_exits_count']} "
        f"stuck_positions={promotion['stuck_positions']} "
        f"stuck_positions_count={promotion['stuck_positions_count']} "
        f"lifecycle_failures={promotion['lifecycle_failures']} "
        f"promotion_pass={str(bool(promotion['promotion_pass'])).lower()} "
        "report_only=true keep_options_mode=paper_only",
        flush=True,
    )
    if passed:
        gate_line = "OPTIONS_PROMOTION_GATE_PASS eligible=true"
        log.info(gate_line)
        print(gate_line, flush=True)
    else:
        gate_line = "OPTIONS_PROMOTION_GATE_FAIL reasons=%s keep_options_mode=paper_only" % (
            "; ".join(reasons) if reasons else "unknown"
        )
        log.info(gate_line)
        print(gate_line, flush=True)


if __name__ == "__main__":
    main()
