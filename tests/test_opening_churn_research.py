from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.opening_churn_research import (
    build_opening_churn_report,
    latest_opening_churn_date,
    render_opening_churn_report,
    write_opening_churn_report,
)
from src.trade_attribution import record_exit, record_order_event

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")


def _seed_opening_churn(data_dir: Path) -> None:
    record_order_event(
        data_dir=data_dir,
        user_id="live_bot",
        timestamp=datetime(2026, 6, 12, 9, 40, tzinfo=ET),
        symbol="XLF",
        action="buy",
        route="trend_long",
        source="trend_long",
        notional=1312.5,
        submit_attempt=True,
        submitted=True,
        order_id="xlf-1",
    )
    record_order_event(
        data_dir=data_dir,
        user_id="live_bot",
        timestamp=datetime(2026, 6, 12, 9, 44, 26, tzinfo=ET),
        symbol="JPM",
        action="buy",
        route="trend_long",
        source="trend_long",
        notional=1312.5,
        submit_attempt=True,
        submitted=True,
        order_id="jpm-1",
    )
    record_order_event(
        data_dir=data_dir,
        user_id="live_bot",
        timestamp=datetime(2026, 6, 12, 10, 5, tzinfo=ET),
        symbol="MSFT",
        action="buy",
        route="trend_long",
        source="trend_long",
        notional=1500.0,
        submit_attempt=True,
        submitted=True,
        order_id="msft-1",
    )
    record_exit(
        data_dir=data_dir,
        user_id="live_bot",
        timestamp=datetime(2026, 6, 12, 9, 48, 27, tzinfo=ET),
        symbol="JPM",
        exit_reason="stop_loss",
        pnl=-8.25,
        pnl_pct=-0.63,
        hold_minutes=4.016,
        entry_route="trend_long",
    )
    record_exit(
        data_dir=data_dir,
        user_id="live_bot",
        timestamp=datetime(2026, 6, 12, 11, 0, tzinfo=ET),
        symbol="MSFT",
        exit_reason="take_profit",
        pnl=12.5,
        pnl_pct=0.8,
        hold_minutes=55.0,
        entry_route="trend_long",
    )


def _write_opening_log(tmp_path: Path) -> Path:
    log_path = tmp_path / "algo_2026-06-12.log"
    log_path.write_text(
        "\n".join(
            [
                "Jun 12 09:40:00 host python[1]: SKIP AAPL: reason=below MAs (trend prefilter); no news override or alternate entry (breakout / mean reversion / vol)",
                "Jun 12 09:40:02 host python[1]: SKIP MSFT: reason=below MAs (trend prefilter); no news override or alternate entry (breakout / mean reversion / vol)",
                "Jun 12 09:42:00 host python[1]: SKIP AAPL: reason=below MAs (trend prefilter); no news override or alternate entry (breakout / mean reversion / vol)",
                "Jun 12 09:44:26 host python[1]: JPM ENTRY_EVAL route=trend_long trend=T pullback=T momentum=T vol=T regime=T spread=T pos=T cooldown=T final=T reason=ok",
                "Jun 12 09:48:27 host python[1]: 09:48 ET JPM SELL 4 shares — stop_loss",
                "Jun 12 09:49:00 host python[1]: JPM ENTRY_EVAL route=trend_long trend=T pullback=T momentum=T vol=T regime=T spread=T pos=T cooldown=F final=F reason=cooldown after stop loss (1 min < 30 min)",
                "Jun 12 09:51:00 host python[1]: JPM ENTRY_EVAL route=trend_long trend=T pullback=T momentum=T vol=T regime=T spread=T pos=T cooldown=F final=F reason=post-exit re-entry wait (3 min < 60 min, entries+execution max)",
                "Jun 12 10:05:00 host python[1]: AAPL ENTRY_EVAL route=trend_long trend=T pullback=T momentum=T vol=T regime=T spread=T pos=T cooldown=T final=T reason=ok",
            ]
        ),
        encoding="utf-8",
    )
    return log_path


def test_opening_churn_report_flags_jpm_style_stop_under_5_minutes(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed_opening_churn(data_dir)

    report = build_opening_churn_report(
        data_dir=data_dir,
        user_id="live_bot",
        day="2026-06-12",
    )
    by_symbol = {row["symbol"]: row for row in report["entries"]}

    assert report["summary"]["entries_total"] == 3
    assert report["summary"]["entries_before_0945"] == 2
    assert report["summary"]["entries_before_1000"] == 2
    assert report["summary"]["entries_at_or_after_1000"] == 1
    assert report["summary"]["stopped_out_under_5m"] == 1
    assert report["summary"]["symbols_stopped_out_under_5m"] == ["JPM"]
    assert by_symbol["JPM"]["exit_reason"] == "stop_loss"
    assert by_symbol["JPM"]["stopped_out_under_5m"] is True
    assert by_symbol["JPM"]["hold_minutes"] == 4.016
    assert by_symbol["JPM"]["realized_pnl"] == -8.25
    assert by_symbol["XLF"]["exit_time"] is None
    assert by_symbol["MSFT"]["phase"] == "at_or_after_1000"
    assert report["phase_summary"]["before_0945"]["stopped_out_under_5m"] == 1
    assert report["phase_summary"]["early_before_1000_vs_later"]["early_entries"] == 2
    assert report["phase_summary"]["early_before_1000_vs_later"]["later_entries"] == 1
    assert report["summary"]["early_entry_pnl_available"] == 1
    assert report["summary"]["early_entry_losses"] == 1


def test_opening_churn_report_writes_json_and_text(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed_opening_churn(data_dir)
    log_path = _write_opening_log(tmp_path)

    json_path, txt_path, report = write_opening_churn_report(
        data_dir=data_dir,
        user_id="live_bot",
        day="2026-06-12",
        project_root=tmp_path,
        log_paths=[log_path],
    )

    assert json_path == data_dir / "research" / "opening_churn" / "2026-06-12_live_bot.json"
    assert txt_path == data_dir / "research" / "opening_churn" / "2026-06-12_live_bot.txt"
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved["summary"]["symbols_stopped_out_under_5m"] == ["JPM"]
    assert saved["summary"]["symbols_with_reentry_blocked"] == ["JPM"]
    trend_rows = saved["log_analysis"]["trend_prefilter"]["top_skipped_symbols"]
    assert trend_rows[0]["symbol"] == "AAPL"
    assert trend_rows[0]["count"] == 2
    assert trend_rows[0]["status"] == "later_eligible"
    assert trend_rows[1]["symbol"] == "MSFT"
    assert trend_rows[1]["status"] == "stayed_blocked_in_logs"
    reentry_rows = saved["log_analysis"]["reentry_blocks"]["rows"]
    assert reentry_rows[0]["symbol"] == "JPM"
    assert reentry_rows[0]["count"] == 2
    text = txt_path.read_text(encoding="utf-8")
    assert "Opening Churn Research - 2026-06-12 user=live_bot" in text
    assert "STOP_UNDER_5M" in text
    assert "not available" in text
    assert "Trend prefilter skips:" in text
    assert "Re-entry blocked by cooldown/post-exit wait:" in text
    assert "JPM" in render_opening_churn_report(report)


def test_opening_churn_latest_and_cli(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed_opening_churn(data_dir)

    assert latest_opening_churn_date(data_dir=data_dir, user_id="live_bot") == "2026-06-12"

    proc = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_opening_churn_research.py"),
            "--date",
            "latest",
            "--user",
            "live_bot",
            "--data-dir",
            str(data_dir),
            "--no-journal",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "Opening Churn Research - 2026-06-12 user=live_bot" in proc.stdout
    assert "symbols_stopped_out_under_5m: JPM" in proc.stdout
    assert (data_dir / "research" / "opening_churn" / "2026-06-12_live_bot.json").exists()

    alias_proc = subprocess.run(
        [
            str(PROJECT_ROOT / "bin" / "algo"),
            "opening-churn-report",
            "--date",
            "2026-06-12",
            "--user",
            "live_bot",
            "--data-dir",
            str(data_dir),
            "--no-journal",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert alias_proc.returncode == 0, alias_proc.stderr
    assert "Opening Churn Research - 2026-06-12 user=live_bot" in alias_proc.stdout
