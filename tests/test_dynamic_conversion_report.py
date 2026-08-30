from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from src.dynamic_conversion_report import (
    build_dynamic_conversion_report,
    latest_dynamic_conversion_date,
    render_dynamic_conversion_report,
    write_dynamic_conversion_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_fixture(tmp_path: Path) -> tuple[Path, Path]:
    data_dir = tmp_path / "data"
    history_dir = data_dir / "dynamic_scan_history"
    review_dir = data_dir / "review" / "2026-06-12"
    history_dir.mkdir(parents=True)
    review_dir.mkdir(parents=True)
    payload = {
        "user_id": "default",
        "generated_at": "2026-06-12T14:00:00+00:00",
        "accepted": [
            {"symbol": "ASTN", "timestamp": "2026-06-12T14:00:00+00:00", "score": 77.7},
            {"symbol": "RKLZ", "timestamp": "2026-06-12T14:01:00+00:00", "score": 88.8},
            {"symbol": "CIIT", "timestamp": "2026-06-12T14:02:00+00:00", "score": 66.6},
        ],
        "selected": [
            {"symbol": "ASTN", "score": 77.7},
            {"symbol": "RKLZ", "score": 88.8},
            {"symbol": "CIIT", "score": 66.6},
        ],
        "candidates": [
            {"symbol": "DSY", "accepted": True, "selected": True, "score": 55.5},
        ],
    }
    (history_dir / "20260612T140000000000Z_default.json").write_text(json.dumps(payload), encoding="utf-8")
    log_path = review_dir / "paper_full.log"
    log_path.write_text(
        "\n".join(
            [
                "Jun 12 10:00:00 host python[1]: DYNAMIC_SELECTED symbol=ASTN score=77.70 news_score=0",
                "Jun 12 10:01:00 host python[1]: DYNAMIC_SELECTED symbol=RKLZ score=88.80 news_score=0",
                "Jun 12 10:02:00 host python[1]: DYNAMIC_SELECTED symbol=CIIT score=66.60 news_score=0",
                "Jun 12 10:03:00 host python[1]: DYNAMIC_SELECTED symbol=DSY score=55.50 news_score=0",
                "Jun 12 10:04:00 host python[1]: DYNAMIC_UNIVERSE: base=20 added=['ASTN','RKLZ','DSY'] total=23",
                "Jun 12 10:05:00 host python[1]: DYNAMIC_SELECTED_ENTRY_TRACE symbol=ASTN in_universe=true will_evaluate=true reason=ok",
                "Jun 12 10:05:01 host python[1]: ASTN ENTRY_EVAL route=dynamic_momentum final=F reason=not enough bars (got 87, need 200)",
                "Jun 12 10:06:00 host python[1]: DYNAMIC_SELECTED_ENTRY_TRACE symbol=RKLZ in_universe=true will_evaluate=true reason=ok",
                "Jun 12 10:06:01 host python[1]: RKLZ ENTRY_EVAL route=dynamic_momentum final=T reason=ok",
                "Jun 12 10:06:02 host python[1]: ENTRY_TO_ALLOCATOR_TRACE symbol=RKLZ route=dynamic_momentum decision_present=true order_request_present=true ohlcv_present=true",
                "Jun 12 10:06:03 host python[1]: ALLOCATOR_INPUT_SYMBOLS count=1 symbols=RKLZ",
                "Jun 12 10:06:04 host python[1]: ORDER_INTENT symbol=RKLZ side=buy notional=500.00 source=capital_allocator",
                "Jun 12 10:06:05 host python[1]: ALLOCATOR_ACTION_SUBMITTED symbol=RKLZ side=buy qty=10",
                "Jun 12 10:07:00 host python[1]: DYNAMIC_SELECTED_ENTRY_TRACE symbol=DSY in_universe=true will_evaluate=false reason=entry_alignment",
                "Jun 12 10:07:01 host python[1]: DYNAMIC_SELECTED_ENTRY_SKIPPED symbol=DSY reason=entry_alignment",
                "Jun 12 10:08:00 host python[1]: AVGO ENTRY_EVAL route=trend_long final=F reason=trend filter",
            ]
        ),
        encoding="utf-8",
    )
    return data_dir, log_path


def test_dynamic_conversion_report_tracks_focus_symbol_funnel(tmp_path: Path) -> None:
    data_dir, log_path = _write_fixture(tmp_path)

    report = build_dynamic_conversion_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-12",
        user_id="paper_bot",
        log_paths=[log_path],
    )

    assert report["research_only"] is True
    assert report["summary"]["total_dynamic_accepted"] == 4
    assert report["summary"]["total_selected"] == 4
    assert report["summary"]["reached_entry_eval"] == 2
    assert report["summary"]["entry_eval_passed"] == 1
    assert report["summary"]["reached_allocator"] == 1
    assert report["summary"]["produced_order_intent"] == 1
    assert report["summary"]["bought_or_submitted"] == 1
    assert report["answers"]["did_accepted_dynamic_candidates_reach_entry_eval"] is True
    assert report["answers"]["silent_loss_between_scanner_and_entry_loop"] is True

    focus = report["focus_symbols"]
    assert focus["ASTN"]["entry_eval_route"] == "dynamic_momentum"
    assert focus["ASTN"]["entry_eval_final"] is False
    assert focus["ASTN"]["final_observed_pipeline_stage"] == "ENTRY_EVAL"
    assert "not enough bars" in focus["ASTN"]["inferred_drop_reason"]
    assert focus["RKLZ"]["final_observed_pipeline_stage"] == "BUY_SUBMITTED"
    assert focus["RKLZ"]["order_intent_produced"] is True
    assert focus["CIIT"]["final_observed_pipeline_stage"] == "DYNAMIC_SELECTED"
    assert focus["CIIT"]["inferred_drop_reason"] == "selected_but_not_seen_in_dynamic_universe_or_entry_eval"
    assert focus["DSY"]["inferred_drop_reason"] == "entry_alignment"
    assert focus["AVGO"]["accepted_count"] == 0


def test_dynamic_conversion_report_writes_artifacts_and_cli(tmp_path: Path) -> None:
    data_dir, log_path = _write_fixture(tmp_path)

    assert latest_dynamic_conversion_date(data_dir=data_dir, user_id="paper_bot") == "2026-06-12"
    json_path, text_path, report = write_dynamic_conversion_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="latest",
        user_id="paper_bot",
        log_paths=[log_path],
    )

    assert json_path == data_dir / "research" / "dynamic_conversion" / "2026-06-12_paper_bot.json"
    assert text_path == data_dir / "research" / "dynamic_conversion" / "2026-06-12_paper_bot.txt"
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved["report"] == "dynamic_conversion"
    text = text_path.read_text(encoding="utf-8")
    assert "Dynamic Conversion Report - 2026-06-12 user=paper_bot" in text
    assert "Focus Symbols" in render_dynamic_conversion_report(report)
    assert "ASTN" in text

    proc = subprocess.run(
        [
            str(PROJECT_ROOT / "bin" / "algo"),
            "dynamic-conversion-report",
            "--date",
            "2026-06-12",
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
    assert "Dynamic Conversion Report - 2026-06-12 user=paper_bot" in proc.stdout
    assert "JSON:" in proc.stdout


def test_dynamic_conversion_report_reads_sqlite_events(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    history_dir = data_dir / "dynamic_scan_history"
    history_dir.mkdir(parents=True)
    (history_dir / "20260612T140000000000Z_default.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-06-12T14:00:00+00:00",
                "accepted": [{"symbol": "ASTN", "score": 80.0}],
                "selected": ["ASTN"],
            }
        ),
        encoding="utf-8",
    )
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
            ("2026-06-12T14:10:00+00:00", "paper_bot", "ASTN", "dynamic_momentum", 1, "ok", "{}"),
        )
        con.execute(
            "insert into entry_terminal_outcomes values (?,?,?,?,?,?,?)",
            (
                "2026-06-12T14:11:00+00:00",
                "paper_bot",
                "ASTN",
                "dynamic_momentum",
                "allocator_no_action",
                "minimum_cash_to_deploy",
                "{}",
            ),
        )
        con.commit()
    finally:
        con.close()

    report = build_dynamic_conversion_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-12",
        user_id="paper_bot",
    )

    astn = report["focus_symbols"]["ASTN"]
    assert astn["reached_entry_eval"] is True
    assert astn["entry_eval_final"] is True
    assert astn["reached_allocator"] is True
    assert astn["final_observed_pipeline_stage"] == "ALLOCATOR_DECISION"
    assert astn["inferred_drop_reason"] == "minimum_cash_to_deploy"
