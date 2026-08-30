from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from src.allocator_threshold_research import (
    build_allocator_threshold_research_report,
    latest_allocator_threshold_date,
    render_allocator_threshold_research_report,
    write_allocator_threshold_research_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_fixture(tmp_path: Path) -> tuple[Path, Path]:
    data_dir = tmp_path / "data"
    hist = data_dir / "dynamic_scan_history"
    hist.mkdir(parents=True)
    (hist / "20260612T140000000000Z_default.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-06-12T14:00:00+00:00",
                "user_id": "default",
                "candidates": [
                    {
                        "symbol": "ASTN",
                        "accepted": False,
                        "rejection_reason": "below_min_relative_volume",
                        "relative_volume": 0.82,
                        "day_gain_pct": 7.2,
                        "later_same_day_return_pct": 1.4,
                    },
                    {
                        "symbol": "NOK",
                        "accepted": False,
                        "rejection_reason": "below_min_relative_volume",
                        "relative_volume": 0.43,
                        "day_gain_pct": 4.0,
                        "later_same_day_return_pct": -0.8,
                    },
                    {
                        "symbol": "INTC",
                        "accepted": False,
                        "rejection_reason": "below_min_day_gain",
                        "relative_volume": 1.4,
                        "day_gain_pct": 2.0,
                    },
                    {
                        "symbol": "POEL",
                        "accepted": False,
                        "rejection_reason": "below_min_price",
                        "relative_volume": 5.0,
                        "day_gain_pct": 20.0,
                    },
                    {
                        "symbol": "VRA",
                        "accepted": False,
                        "rejection_reason": (
                            "entry_alignment: need 5m breakout OR new intraday high "
                            "OR strong green 1m OR opening-range breakout"
                        ),
                        "relative_volume": 2.1,
                        "day_gain_pct": 12.0,
                    },
                    {
                        "symbol": "HPE",
                        "accepted": True,
                        "rejection_reason": None,
                        "relative_volume": 2.0,
                        "day_gain_pct": 9.0,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    daily = hist / "daily"
    daily.mkdir()
    (daily / "2026-06-12_default.json").write_text(
        json.dumps(
            {
                "date": "2026-06-12",
                "user_id": "default",
                "rejection_counts": {
                    "unstable_quote": 3,
                    "below_min_relative_volume": 2,
                },
            }
        ),
        encoding="utf-8",
    )
    log_path = tmp_path / "paper_2026-06-12.log"
    log_path.write_text(
        "\n".join(
            [
                "IWM ENTRY_EVAL route=trend_long final=T reason=ok",
                "XLF ENTRY_EVAL route=trend_long final=T reason=ok",
                "JPM ENTRY_EVAL route=trend_long final=T reason=ok",
                "AVGO ENTRY_EVAL route=trend_long final=F reason=trend filter: close below 20 EMA",
                "GOOGL ENTRY_EVAL route=trend_long final=F reason=trend filter: close below 20 EMA",
                (
                    "ALLOCATOR_NO_ACTION_DETAIL symbol=IWM reason=size = 0 "
                    "detail=trade_size $1312.50 + buffer $5 < minimum_cash_to_deploy 3469 "
                    "available_cash=69000.00 gross_headroom=5700.00 candidate_notional=1312.50 "
                    "final_trade_size=1312.50 limiting_cap=none min_order_notional=1200.00 source=trend_long score=1.45"
                ),
                (
                    "ALLOCATOR_NO_ACTION_DETAIL symbol=XLF reason=size = 0 "
                    "detail=trade_size $1312.50 + buffer $5 < minimum_cash_to_deploy 3469 "
                    "available_cash=69000.00 gross_headroom=5700.00 candidate_notional=1312.50 "
                    "final_trade_size=1312.50 limiting_cap=none min_order_notional=1200.00 source=trend_long score=1.45"
                ),
                (
                    "SKIP JPM: reason=size = 0 "
                    "detail=trade_size $1312.50 + buffer $5 < minimum_cash_to_deploy 3469"
                ),
            ]
        ),
        encoding="utf-8",
    )
    return data_dir, log_path


def test_allocator_threshold_research_explains_minimum_deploy_and_rvol(tmp_path: Path) -> None:
    data_dir, log_path = _write_fixture(tmp_path)

    report = build_allocator_threshold_research_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-12",
        user_id="paper_bot",
        log_paths=[log_path],
    )

    allocator = report["allocator_analysis"]
    assert allocator["symbols"]["IWM"]["root_cause"] == "minimum_cash_to_deploy_after_gross_headroom_clip"
    assert allocator["symbols"]["XLF"]["allocator_no_action"]["minimum_cash_to_deploy"] == 3469.0
    assert allocator["symbols"]["JPM"]["allocator_no_action"]["trade_size"] == 1312.5
    assert allocator["minimum_cash_to_deploy_derivation"]["classification"] == "configuration_driven_intentional_floor"
    assert allocator["historical_minimum_cash_to_deploy"]["minimum_cash_to_deploy_blocks"] == 3
    summary = allocator["summary"]
    assert summary["total_entry_eval_passed"] == 3
    assert summary["total_allocator_no_action"] == 3
    assert summary["total_skipped_by_min_cash"] == 3
    assert summary["average_proposed_trade_size"] == 1312.5
    assert summary["average_minimum_cash_to_deploy"] == 3469.0
    assert summary["average_trade_size_to_minimum_ratio"] == 0.3784
    assert dict(summary["symbols_most_often_skipped"]) == {"IWM": 1, "XLF": 1, "JPM": 1}

    rows = {row["symbol"]: row for row in allocator["candidate_rows"]}
    assert rows["IWM"]["route"] == "trend_long"
    assert rows["IWM"]["candidate_type"] == "core"
    assert rows["IWM"]["proposed_trade_size"] == 1312.5
    assert rows["IWM"]["minimum_cash_to_deploy"] == 3469.0
    assert rows["IWM"]["gross_headroom"] == 5700.0
    assert rows["IWM"]["available_cash"] == 69000.0
    assert rows["IWM"]["reason_skipped"] == "minimum_cash_to_deploy"

    what_if = allocator["what_if"]
    assert what_if["current_threshold"]["would_clear_threshold_count"] == 0
    assert what_if["threshold_75pct"]["would_clear_threshold_count"] == 0
    assert what_if["threshold_50pct"]["would_clear_threshold_count"] == 0
    assert what_if["min_realloc_leg_only"]["would_clear_threshold_count"] == 3
    assert what_if["no_minimum_deployment_floor"]["would_clear_threshold_count"] == 3

    rvol = report["dynamic_rvol_analysis"]
    assert rvol["relative_volume_rejections"] == 2
    assert rvol["hypothetical_thresholds"]["1.00"]["rvol_rejects_at_or_above_threshold"] == 0
    assert rvol["hypothetical_thresholds"]["0.75"]["rvol_rejects_at_or_above_threshold"] == 1
    assert rvol["hypothetical_thresholds"]["0.50"]["rvol_rejects_at_or_above_threshold"] == 1
    assert rvol["relative_volume_forward_returns"]["count"] == 2

    quality = report["dynamic_candidate_quality"]
    assert quality["ASTN"]["final_blocker"] == "below_min_relative_volume"
    assert quality["NOK"]["final_blocker"] == "below_min_relative_volume"
    assert quality["INTC"]["final_blocker"] == "below_min_day_gain"
    assert quality["POEL"]["final_blocker"] == "below_min_price"
    assert quality["VRA"]["final_blocker"] == "entry_alignment"
    assert quality["AVGO"]["final_blocker"] == "trend_filter"
    assert quality["GOOGL"]["final_blocker"] == "trend_filter"


def test_allocator_threshold_research_writes_json_text_and_cli(tmp_path: Path) -> None:
    data_dir, log_path = _write_fixture(tmp_path)

    json_path, txt_path, report = write_allocator_threshold_research_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-12",
        user_id="paper_bot",
        log_paths=[log_path],
    )

    assert json_path == data_dir / "research" / "allocator_threshold_research" / "2026-06-12_paper_bot.json"
    assert txt_path == data_dir / "research" / "allocator_threshold_research" / "2026-06-12_paper_bot.txt"
    assert json.loads(json_path.read_text(encoding="utf-8"))["research_only"] is True
    text = txt_path.read_text(encoding="utf-8")
    assert "Allocator Threshold Research - 2026-06-12 user=paper_bot" in text
    assert "minimum_cash_to_deploy_after_gross_headroom_clip" in text
    assert "Allocator minimum-deployment summary:" in text
    assert "What-if analysis:" in text
    assert "IWM: route=trend_long candidate_type=core" in text
    assert "threshold 0.75" in render_allocator_threshold_research_report(report)
    assert latest_allocator_threshold_date(data_dir=data_dir, user_id="paper_bot") == "2026-06-12"

    proc = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_allocator_threshold_research.py"),
            "--date",
            "latest",
            "--user",
            "paper_bot",
            "--data-dir",
            str(data_dir),
            "--project-root",
            str(tmp_path),
            "--log-path",
            str(log_path),
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "Allocator Threshold Research - 2026-06-12 user=paper_bot" in proc.stdout
    assert "JSON:" in proc.stdout


def test_allocator_threshold_research_reads_persisted_events(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    con = sqlite3.connect(data_dir / "algo_live.db")
    try:
        con.execute(
            "create table entry_evaluations ("
            "ts text, user_id text, symbol text, route text, final integer, reason text, payload_json text)"
        )
        con.execute(
            "create table entry_terminal_outcomes ("
            "ts text, user_id text, symbol text, route text, stage text, reason text, payload_json text)"
        )
        con.execute(
            "insert into entry_evaluations values (?,?,?,?,?,?,?)",
            ("2026-06-12T15:00:00-04:00", "paper_bot", "IWM", "trend_long", 1, "ok", "{}"),
        )
        con.execute(
            "insert into entry_terminal_outcomes values (?,?,?,?,?,?,?)",
            (
                "2026-06-12T15:01:00-04:00",
                "paper_bot",
                "IWM",
                "trend_long",
                "allocator_no_action",
                "minimum_cash_to_deploy",
                json.dumps(
                    {
                        "detail": "trade_size $1312.50 + buffer $5 < minimum_cash_to_deploy 3469",
                        "final_trade_size": 1312.5,
                        "minimum_cash_to_deploy": 3469.0,
                        "available_cash": 69000.0,
                        "gross_headroom": 5700.0,
                        "min_order_notional": 1200.0,
                    }
                ),
            ),
        )
        con.commit()
    finally:
        con.close()

    report = build_allocator_threshold_research_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-12",
        user_id="paper_bot",
        log_paths=[],
    )

    allocator = report["allocator_analysis"]
    assert allocator["summary"]["total_entry_eval_passed"] == 1
    assert allocator["summary"]["total_allocator_no_action"] == 1
    assert allocator["candidate_rows"][0]["symbol"] == "IWM"
    assert allocator["candidate_rows"][0]["route"] == "trend_long"
    assert allocator["candidate_rows"][0]["reason_skipped"] == "minimum_cash_to_deploy"
