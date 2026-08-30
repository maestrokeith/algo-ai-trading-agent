from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from src.dynamic_gate_research import (
    build_dynamic_gate_research_report,
    latest_dynamic_gate_research_date,
    normalize_gate_reason,
    render_dynamic_gate_research_report,
    write_dynamic_gate_research_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _seed_scan_history(data_dir: Path) -> Path:
    history_dir = data_dir / "dynamic_scan_history"
    history_dir.mkdir(parents=True, exist_ok=True)
    path = history_dir / "20260608T143000000000Z_live_bot.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-06-08T14:30:00+00:00",
                "user_id": "live_bot",
                "selected": ["PAYO", "HTCO", "IREZ", "SOXS"],
                "accepted": [
                    {"symbol": "PAYO", "score": 0.91, "accepted": True},
                    {"symbol": "HTCO", "score": 0.82, "accepted": True},
                    {"symbol": "IREZ", "score": 0.74, "accepted": True},
                    {"symbol": "SOXS", "score": 0.69, "accepted": True},
                ],
                "rejected": [
                    {
                        "symbol": "LITZ",
                        "score": 0.44,
                        "accepted": False,
                        "rejection_reason": "unstable quote",
                    },
                    {
                        "symbol": "LOWP",
                        "score": 0.2,
                        "accepted": False,
                        "rejection_reason": "below_min_price",
                    },
                    {
                        "symbol": "RUNR",
                        "score": 0.3,
                        "accepted": False,
                        "rejection_reason": "gain filter",
                    },
                    {
                        "symbol": "RVOL",
                        "score": 0.25,
                        "accepted": False,
                        "rejection_reason": "below_min_relative_volume",
                    },
                    {
                        "symbol": "VWAP",
                        "score": 0.24,
                        "accepted": False,
                        "rejection_reason": "not above VWAP",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _seed_log(tmp_path: Path) -> Path:
    path = tmp_path / "algo_2026-06-08.log"
    path.write_text(
        "\n".join(
            [
                "INFO DYNAMIC_SCAN selected=['PAYO', 'HTCO', 'IREZ', 'SOXS']",
                "INFO DYNAMIC_SELECTED symbol=PAYO score=0.91 news_score=1 event_score=1 catalyst_score=0.1",
                "INFO DYNAMIC_SELECTED symbol=HTCO score=0.82 news_score=1 event_score=1 catalyst_score=0.1",
                "INFO DYNAMIC_SELECTED symbol=IREZ score=0.74 news_score=1 event_score=1 catalyst_score=0.1",
                "INFO PAYO ENTRY_EVAL route=dynamic_momentum_override final=T reason=ok",
                "INFO HTCO ENTRY_EVAL route=dynamic_momentum_override final=T reason=ok",
                "INFO SOXS ENTRY_EVAL route=dynamic_momentum_override final=F reason=entry_alignment: gain_pct 2.0 < 6.0",
                "INFO ALLOCATOR_FILTER_REJECT symbol=PAYO reason=no_catalyst score=0.91 catalyst_score=0.0",
                "INFO ALLOCATOR_REJECT HTCO reason=dynamic spread 6.200% > 3.500%",
                "INFO DYNAMIC_NOT_TRADABLE symbol=IREZ reason=not enough bars (got 96, need 200) detail=catalyst_score 1.00 < 3.00",
                "INFO DYNAMIC_REJECT symbol=LOWP reason=below_min_price",
                "INFO DYNAMIC_REJECT symbol=RUNR reason=gain filter",
                "INFO DYNAMIC_REJECT symbol=RVOL reason=below_min_relative_volume",
                "INFO DYNAMIC_REJECT symbol=VWAP reason=not above VWAP",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_normalize_gate_reason_maps_required_buckets() -> None:
    assert normalize_gate_reason("no_catalyst") == "no_catalyst"
    assert normalize_gate_reason("not enough bars (got 96, need 200)") == "short_history"
    assert normalize_gate_reason("dynamic spread 6.2 > 3.5") == "spread_cap"
    assert normalize_gate_reason("entry_alignment: gain low") == "entry_alignment"
    assert normalize_gate_reason("not above VWAP") == "vwap_extension"
    assert normalize_gate_reason("below_min_relative_volume") == "relative_volume"
    assert normalize_gate_reason("unstable quote") == "unstable_quote"
    assert normalize_gate_reason("below_min_price") == "below_min_price"
    assert normalize_gate_reason("gain filter") == "excessive_gain"
    assert normalize_gate_reason("no_decision") == "no_decision"
    assert normalize_gate_reason("bad quote") == "bad_quote"
    assert normalize_gate_reason("mystery", "INFO ALLOCATOR_REJECT ABC reason=mystery") == "allocator_reject"
    assert normalize_gate_reason("mystery", "INFO ABC ENTRY_EVAL final=F reason=mystery") == "entry_eval_reject"
    assert normalize_gate_reason("mystery", "INFO DYNAMIC_NOT_TRADABLE symbol=ABC reason=mystery") == "prefilter_reject"
    assert normalize_gate_reason("no_trade_cycle_allowed") == "trade_cycle"
    assert normalize_gate_reason("notional below min_realloc_leg") == "min_notional"
    assert normalize_gate_reason("post_sell_rebuy_cooldown") == "same_day_sold_guard"


def test_dynamic_gate_research_parses_selected_entry_eval_and_allocator_rejects(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed_scan_history(data_dir)
    log_path = _seed_log(tmp_path)

    report = build_dynamic_gate_research_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-08",
        user_id="live_bot",
        log_paths=[log_path],
    )

    assert report["summary"]["dynamic_candidates_seen"] == 9
    assert report["summary"]["selected_dynamic_candidates"] == 4
    assert report["summary"]["entry_eval_passed"] == 2
    assert report["rejection_counts_by_gate"]["no_catalyst"] == 1
    assert report["rejection_counts_by_gate"]["spread_cap"] == 1
    assert report["rejection_counts_by_gate"]["short_history"] == 1
    assert report["rejection_counts_by_gate"]["entry_alignment"] == 1
    assert report["rejection_counts_by_gate"]["vwap_extension"] == 1
    assert report["rejection_counts_by_gate"]["relative_volume"] == 1
    assert report["rejection_counts_by_gate"]["unstable_quote"] == 1
    assert report["rejection_counts_by_gate"]["below_min_price"] == 1
    assert report["rejection_counts_by_gate"]["excessive_gain"] == 1
    assert report["gate_buckets"]["no_catalyst_rejects"] == ["PAYO"]
    assert report["gate_buckets"]["short_history_rejects"] == ["IREZ"]
    assert set(report["symbols_passed_entry_eval_rejected_by_allocator"]) == {"HTCO", "PAYO"}
    assert report["top_missed_candidates_by_dynamic_score"][0]["symbol"] == "PAYO"


def test_dynamic_gate_research_writes_json_and_text(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed_scan_history(data_dir)
    log_path = _seed_log(tmp_path)

    json_path, txt_path, report = write_dynamic_gate_research_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-08",
        user_id="live_bot",
        log_paths=[log_path],
    )

    assert json_path == data_dir / "research" / "dynamic_gate_research" / "2026-06-08_live_bot.json"
    assert txt_path == data_dir / "research" / "dynamic_gate_research" / "2026-06-08_live_bot.txt"
    assert json.loads(json_path.read_text(encoding="utf-8"))["summary"]["dynamic_candidates_seen"] == 9
    text = txt_path.read_text(encoding="utf-8")
    assert "Dynamic Momentum Gate Research - 2026-06-08 user=live_bot" in text
    assert "- no_catalyst: 1" in text
    assert "PAYO" in render_dynamic_gate_research_report(report)


def test_dynamic_gate_research_reports_entry_alignment_forward_returns(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    history_dir = data_dir / "dynamic_scan_history"
    history_dir.mkdir(parents=True)
    target_reason = (
        "entry_alignment: need 5m breakout OR new intraday high OR strong green 1m OR opening-range breakout "
        "(breakout=False nh=False green=False orb=False)"
    )
    (history_dir / "20260610T143000000000Z_live_bot.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-06-10T14:30:00+00:00",
                "user_id": "live_bot",
                "rejected": [
                    {
                        "symbol": "WIN",
                        "timestamp": "2026-06-10T14:30:00+00:00",
                        "price": 10.0,
                        "score": 0.9,
                        "rejection_reason": target_reason,
                    },
                    {
                        "symbol": "LOSE",
                        "timestamp": "2026-06-10T14:30:00+00:00",
                        "price": 20.0,
                        "score": 0.8,
                        "rejection_reason": target_reason,
                    },
                    {
                        "symbol": "FLAT",
                        "timestamp": "2026-06-10T14:30:00+00:00",
                        "price": 5.0,
                        "score": 0.7,
                        "rejection_reason": target_reason,
                    },
                    {
                        "symbol": "GAIN",
                        "timestamp": "2026-06-10T14:30:00+00:00",
                        "price": 5.0,
                        "score": 0.6,
                        "rejection_reason": "entry_alignment: gain_pct 2.0 < 3.0",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    bars_dir = data_dir / "historical_bars"
    bars_dir.mkdir(parents=True)
    bar_rows = {
        "WIN": [10.2, 10.5, 10.8, 11.2],
        "LOSE": [19.8, 19.6, 19.2, 18.8],
        "FLAT": [5.0, 5.05, 5.10, 4.95],
        "GAIN": [6.0, 7.0, 8.0, 9.0],
    }
    for symbol, closes in bar_rows.items():
        (bars_dir / f"{symbol}_2026-06-10_1Min.csv").write_text(
            "\n".join(
                [
                    "timestamp,close",
                    f"2026-06-10T14:45:00+00:00,{closes[0]}",
                    f"2026-06-10T15:00:00+00:00,{closes[1]}",
                    f"2026-06-10T15:30:00+00:00,{closes[2]}",
                    f"2026-06-10T19:59:00+00:00,{closes[3]}",
                ]
            ),
            encoding="utf-8",
        )

    report = build_dynamic_gate_research_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-10",
        user_id="live_bot",
    )
    research = report["entry_alignment_forward_returns"]

    assert research["total_rejects"] == 3
    assert research["rows_with_any_return"] == 3
    assert research["summary_by_horizon"]["15m"]["average_return_pct"] == pytest.approx(1.0 / 3.0)
    assert research["summary_by_horizon"]["15m"]["median_return_pct"] == pytest.approx(0.0)
    assert research["summary_by_horizon"]["15m"]["win_rate"] == pytest.approx(1.0 / 3.0)
    assert research["summary_by_horizon"]["30m"]["win_rate"] == pytest.approx(2.0 / 3.0)
    assert research["summary_by_horizon"]["60m"]["best_examples"][0]["symbol"] == "WIN"
    assert research["summary_by_horizon"]["eod"]["worst_examples"][0]["symbol"] == "LOSE"
    rendered = render_dynamic_gate_research_report(report)
    assert "Entry-alignment forward returns:" in rendered
    assert "- 15m: count=3 avg=0.33% median=0.00% win_rate=33.3%" in rendered
    assert "best: WIN=2.00%" in rendered
    assert "worst: LOSE=-1.00%" in rendered
    assert "| WIN | 2.00% | 5.00% | 8.00% | 12.00% | 10.00 |" in rendered


def test_dynamic_gate_research_latest_and_cli(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed_scan_history(data_dir)
    log_path = _seed_log(tmp_path)

    assert latest_dynamic_gate_research_date(
        project_root=tmp_path,
        data_dir=data_dir,
        user_id="live_bot",
    ) == "2026-06-08"

    proc = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_dynamic_gate_research.py"),
            "--date",
            "latest",
            "--user",
            "live_bot",
            "--data-dir",
            str(data_dir),
            "--project-root",
            str(tmp_path),
            "--log-file",
            str(log_path),
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "Dynamic Momentum Gate Research - 2026-06-08 user=live_bot" in proc.stdout
    assert (data_dir / "research" / "dynamic_gate_research" / "2026-06-08_live_bot.json").exists()


def test_dynamic_gate_research_classifies_former_unknown_outcomes(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    history_dir = data_dir / "dynamic_scan_history"
    history_dir.mkdir(parents=True)
    (history_dir / "20260609T143000000000Z_live_bot.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-06-09T14:30:00+00:00",
                "user_id": "live_bot",
                "selected": ["TNGX", "ABAT", "ALLOC", "PREF"],
                "accepted": [
                    {"symbol": "TNGX", "score": 65.04, "accepted": True},
                    {"symbol": "CBRG", "score": 56.72, "accepted": True},
                    {"symbol": "ABAT", "score": 40.60, "accepted": True},
                    {"symbol": "ALLOC", "score": 22.0, "accepted": True},
                    {"symbol": "PREF", "score": 21.0, "accepted": True},
                    {"symbol": "UNKN", "score": 10.0, "accepted": True},
                    {"symbol": "MYST", "score": 9.0, "accepted": True},
                ],
                "rejected": [
                    {
                        "symbol": "CBRG",
                        "score": 56.72,
                        "accepted": False,
                        "rejection_reason": "bad quote",
                    },
                    {
                        "symbol": "UNKN",
                        "score": 10.0,
                        "accepted": False,
                        "rejection_reason": "unmapped test reason",
                    },
                    {
                        "symbol": "MYST",
                        "score": 9.0,
                        "accepted": False,
                        "rejection_reason": "unknown",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    log_path = tmp_path / "algo_2026-06-09.log"
    log_path.write_text(
        "\n".join(
            [
                "INFO TNGX ENTRY_EVAL route=dynamic_momentum_override final=F reason=no_decision",
                "INFO ABAT ENTRY_EVAL route=dynamic_momentum_override final=T reason=ok",
                "INFO AAPL ENTRY_EVAL route=trend_long final=T reason=ok",
                "INFO XLF ENTRY_EVAL route=trend_long final=T reason=ok",
                "INFO ALLOCATOR_REJECT ALLOC reason=unmapped allocator reason",
                "INFO DYNAMIC_NOT_TRADABLE symbol=PREF reason=unmapped prefilter reason",
                "INFO DYNAMIC_SCAN reject CBRG: bad quote price=0.0 bid=0.0 ask=0.0",
            ]
        ),
        encoding="utf-8",
    )

    report = build_dynamic_gate_research_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-09",
        user_id="live_bot",
        log_paths=[log_path],
    )
    by_symbol = {row["symbol"]: row for row in report["candidates"]}

    assert by_symbol["TNGX"]["final_gate"] == "no_decision"
    assert by_symbol["CBRG"]["final_gate"] == "bad_quote"
    assert by_symbol["ABAT"]["final_gate"] == "missing_outcome"
    assert by_symbol["AAPL"]["final_gate"] == "core_symbol_non_dynamic"
    assert by_symbol["XLF"]["final_gate"] == "core_symbol_non_dynamic"
    assert by_symbol["ALLOC"]["final_gate"] == "allocator_reject"
    assert by_symbol["PREF"]["final_gate"] == "prefilter_reject"
    assert by_symbol["UNKN"]["final_gate"] == "unmapped test reason"
    assert by_symbol["MYST"]["final_gate"] == "unknown"
    assert report["gate_buckets"]["no_decision_rejects"] == ["TNGX"]
    assert report["gate_buckets"]["bad_quote_rejects"] == ["CBRG"]
    assert report["gate_buckets"]["missing_outcome"] == ["ABAT"]
    assert report["gate_buckets"]["core_symbol_non_dynamic"] == ["AAPL", "XLF"]
    assert report["gate_buckets"]["allocator_rejects"] == ["ALLOC"]
    assert report["gate_buckets"]["prefilter_rejects"] == ["PREF"]
    assert report["unclassified_examples"] == [
        {"symbol": "MYST", "reason": "unknown", "dynamic_score": 9.0}
    ]


def test_dynamic_gate_research_traces_selected_and_final_rows_from_sqlite(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "algo_live.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            create table dynamic_scans (
                id integer primary key,
                ts text not null,
                user_id text,
                selected_json text,
                candidates_json text,
                payload_json text
            );
            create table entry_evaluations (
                id integer primary key,
                ts text not null,
                user_id text,
                symbol text,
                route text,
                final integer,
                reason text,
                payload_json text
            );
            create table trades (
                id integer primary key,
                ts text not null,
                user_id text,
                symbol text,
                side text,
                qty real,
                notional real,
                price real,
                order_id text,
                status text
            );
            """
        )
        conn.execute(
            "insert into dynamic_scans (ts, user_id, selected_json, candidates_json, payload_json) values (?, ?, ?, ?, ?)",
            (
                "2026-06-09T14:31:00+00:00",
                "live_bot",
                json.dumps(["FINAL", "SELECTED_ONLY"]),
                json.dumps(
                    [
                        {"symbol": "FINAL", "score": 90.0, "accepted": True},
                        {"symbol": "SELECTED_ONLY", "score": 80.0, "accepted": True},
                        {
                            "symbol": "REJECTED",
                            "score": 10.0,
                            "accepted": False,
                            "rejection_reason": "bad quote",
                        },
                    ]
                ),
                "{}",
            ),
        )
        conn.execute(
            "insert into entry_evaluations (ts, user_id, symbol, route, final, reason, payload_json) values (?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-06-09T14:32:00+00:00",
                "live_bot",
                "FINAL",
                "dynamic_momentum_override",
                1,
                "ok",
                "{}",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    report = build_dynamic_gate_research_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-09",
        user_id="live_bot",
    )
    by_symbol = {row["symbol"]: row for row in report["candidates"]}

    assert report["source_files"]["sqlite_event_store"] == str(db_path)
    assert by_symbol["FINAL"]["selected"] is True
    assert by_symbol["FINAL"]["final"] is True
    assert by_symbol["FINAL"]["order_submitted"] is False
    assert by_symbol["FINAL"]["downstream_block_stage"] == "legacy_missing_terminal_outcome"
    assert by_symbol["FINAL"]["downstream_block_reason"] == "legacy_missing_terminal_outcome_no_event_store_record"
    assert by_symbol["SELECTED_ONLY"]["selected"] is True
    assert by_symbol["SELECTED_ONLY"]["final"] is False
    assert by_symbol["SELECTED_ONLY"]["downstream_block_reason"] == "missing_outcome"


def test_dynamic_gate_research_reports_entry_passed_allocator_reject_details(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    history_dir = data_dir / "dynamic_scan_history"
    history_dir.mkdir(parents=True)
    (history_dir / "20260609T143000000000Z_live_bot.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-06-09T14:30:00+00:00",
                "user_id": "live_bot",
                "selected": ["GATE"],
                "accepted": [
                    {"symbol": "GATE", "score": 88.0, "accepted": True},
                    {"symbol": "FILL", "score": 77.0, "accepted": True},
                ],
                "rejected": [],
            }
        ),
        encoding="utf-8",
    )
    log_path = tmp_path / "algo_2026-06-09.log"
    log_path.write_text(
        "\n".join(
            [
                "INFO GATE ENTRY_EVAL route=dynamic_momentum_override final=T reason=ok",
                "INFO ALLOCATOR_NO_ACTION_DETAIL symbol=GATE reason=size = 0 "
                "detail=trade_size $200.00 + buffer $5 < minimum_cash_to_deploy 840 "
                "available_cash=6000.00 cash_reserve=2800.00 gross_headroom=200.00 "
                "dynamic_sleeve_cap=7000.00 min_order_notional=1200.00",
                "INFO FILL ENTRY_EVAL route=dynamic_momentum_override final=T reason=ok",
                "INFO ALLOCATOR_ACTION_CREATED symbol=FILL action=buy notional=1300.00 route=dynamic_universe",
                "INFO ALLOCATOR_ACTION_SUBMITTED symbol=FILL action=buy notional=1300.00 order_id=mock route=dynamic_universe",
            ]
        ),
        encoding="utf-8",
    )

    report = build_dynamic_gate_research_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-09",
        user_id="live_bot",
        log_paths=[log_path],
    )
    by_symbol = {row["symbol"]: row for row in report["candidates"]}
    detail_rows = {
        row["symbol"]: row for row in report["entry_eval_passed_downstream_rejections"]
    }

    assert by_symbol["FILL"]["allocator_action_created"] is True
    assert by_symbol["FILL"]["order_submitted"] is True
    assert "FILL" not in detail_rows
    assert detail_rows["GATE"]["downstream_block_stage"] == "min_notional"
    assert detail_rows["GATE"]["downstream_block_reason"] == "size = 0"
    assert detail_rows["GATE"]["config_values"]["minimum_cash_to_deploy"] == "840"
    assert detail_rows["GATE"]["config_values"]["min_order_notional"] == "1200.00"
    assert detail_rows["GATE"]["config_values"]["gross_headroom"] == "200.00"
    assert by_symbol["GATE"]["downstream_block_stage"] == "min_notional"
    assert by_symbol["GATE"]["downstream_block_config_values"]["cash_reserve"] == "2800.00"


def test_dynamic_gate_research_reads_entry_terminal_outcomes_from_sqlite(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "algo_live.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            create table dynamic_scans (
                id integer primary key,
                ts text not null,
                user_id text,
                selected_json text,
                candidates_json text,
                payload_json text
            );
            create table entry_evaluations (
                id integer primary key,
                ts text not null,
                user_id text,
                symbol text,
                route text,
                final integer,
                reason text,
                payload_json text
            );
            create table trades (
                id integer primary key,
                ts text not null,
                user_id text,
                symbol text,
                side text,
                qty real,
                notional real,
                price real,
                order_id text,
                status text
            );
            create table entry_terminal_outcomes (
                id integer primary key,
                ts text not null,
                user_id text,
                symbol text,
                route text,
                stage text,
                reason text,
                terminal integer,
                payload_json text
            );
            """
        )
        conn.execute(
            "insert into dynamic_scans (ts, user_id, selected_json, candidates_json, payload_json) values (?, ?, ?, ?, ?)",
            (
                "2026-06-09T14:30:00+00:00",
                "live_bot",
                json.dumps(["PAYO"]),
                json.dumps([{"symbol": "PAYO", "score": 46.6, "accepted": True}]),
                "{}",
            ),
        )
        conn.execute(
            "insert into entry_evaluations (ts, user_id, symbol, route, final, reason, payload_json) values (?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-06-09T14:31:00+00:00",
                "live_bot",
                "PAYO",
                "dynamic_momentum_override",
                1,
                "ok",
                "{}",
            ),
        )
        conn.execute(
            "insert into entry_terminal_outcomes (ts, user_id, symbol, route, stage, reason, terminal, payload_json) values (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-06-09T14:32:00+00:00",
                "live_bot",
                "PAYO",
                "dynamic_universe",
                "allocator_no_action",
                "minimum_cash_to_deploy",
                1,
                json.dumps({"minimum_cash_to_deploy": 840, "gross_headroom": 200}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    report = build_dynamic_gate_research_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-09",
        user_id="live_bot",
    )
    row = {
        item["symbol"]: item for item in report["entry_eval_passed_downstream_rejections"]
    }["PAYO"]

    assert row["downstream_block_stage"] == "allocator_no_action"
    assert row["downstream_block_reason"] == "minimum_cash_to_deploy"
    assert row["config_values"]["minimum_cash_to_deploy"] == "840"
    assert row["config_values"]["gross_headroom"] == "200"


def test_dynamic_gate_research_backfills_legacy_attribution_terminal_hint(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    history_dir = data_dir / "dynamic_scan_history"
    history_dir.mkdir(parents=True)
    (history_dir / "20260609T143000000000Z_live_bot.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-06-09T14:30:00+00:00",
                "user_id": "live_bot",
                "selected": ["ABAT"],
                "accepted": [{"symbol": "ABAT", "score": 40.6, "accepted": True}],
                "rejected": [],
            }
        ),
        encoding="utf-8",
    )
    attr_dir = data_dir / "trade_attribution" / "daily"
    attr_dir.mkdir(parents=True)
    (attr_dir / "2026-06-09_live_bot.json").write_text(
        json.dumps(
            {
                "candidates": [
                    {
                        "symbol": "ABAT",
                        "accepted": True,
                        "dynamic": True,
                        "route": "dynamic_momentum_override",
                    }
                ],
                "allocator_candidates": [],
                "orders": [],
            }
        ),
        encoding="utf-8",
    )
    log_path = tmp_path / "algo_2026-06-09.log"
    log_path.write_text(
        "INFO ABAT ENTRY_EVAL route=dynamic_momentum_override final=T reason=ok\n",
        encoding="utf-8",
    )

    report = build_dynamic_gate_research_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-09",
        user_id="live_bot",
        log_paths=[log_path],
    )
    row = {
        item["symbol"]: item for item in report["entry_eval_passed_downstream_rejections"]
    }["ABAT"]

    assert row["downstream_block_stage"] == "allocator_input_missing"
    assert row["downstream_block_reason"] == "legacy_no_allocator_candidate_recorded"


def test_dynamic_gate_research_does_not_backfill_legacy_missing_after_terminal_stage(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    history_dir = data_dir / "dynamic_scan_history"
    history_dir.mkdir(parents=True)
    (history_dir / "20260611T143000000000Z_live_bot.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-06-11T14:30:00+00:00",
                "user_id": "live_bot",
                "selected": ["CPNG"],
                "accepted": [{"symbol": "CPNG", "score": 40.6, "accepted": True}],
                "rejected": [],
            }
        ),
        encoding="utf-8",
    )
    attr_dir = data_dir / "trade_attribution" / "daily"
    attr_dir.mkdir(parents=True)
    (attr_dir / "2026-06-11_live_bot.json").write_text(
        json.dumps(
            {
                "candidates": [
                    {
                        "symbol": "CPNG",
                        "accepted": True,
                        "dynamic": True,
                        "route": "dynamic_momentum_override",
                    }
                ],
                "allocator_candidates": [],
                "orders": [],
            }
        ),
        encoding="utf-8",
    )
    log_path = tmp_path / "algo_2026-06-11.log"
    log_path.write_text(
        "\n".join(
            [
                "INFO CPNG ENTRY_EVAL route=dynamic_momentum_override final=T reason=ok",
                (
                    "INFO ENTRY_TERMINAL_OUTCOME symbol=CPNG stage=allocator_input "
                    "reason=allocator_pass_start route=dynamic_momentum_override"
                ),
            ]
        ),
        encoding="utf-8",
    )

    report = build_dynamic_gate_research_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-11",
        user_id="live_bot",
        log_paths=[log_path],
    )
    row = {
        item["symbol"]: item for item in report["entry_eval_passed_downstream_rejections"]
    }["CPNG"]

    assert row["downstream_block_stage"] == "allocator_input"
    assert row["downstream_block_reason"] == "allocator_pass_start"


def test_dynamic_gate_research_ingests_replay_traces_and_downstream_categories(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    replay_dir = data_dir / "replay_market_session"
    replay_dir.mkdir(parents=True)
    (replay_dir / "2026-06-18_live_bot.json").write_text(
        json.dumps(
            {
                "per_symbol_trace": [
                    {
                        "symbol": "CAT",
                        "route": "dynamic_momentum_override",
                        "source": "dynamic_universe",
                        "scan_selected": True,
                        "entry_eval": {"result": True, "reason": "ok"},
                        "allocator_candidate": {"result": False, "reason": "no_catalyst"},
                        "allocator_action": {"result": False, "reason": "no_catalyst"},
                        "order_build": {"result": False, "reason": "no_catalyst"},
                        "simulated_submit": {"result": False, "reason": "no_catalyst"},
                    },
                    {
                        "symbol": "RANK",
                        "route": "dynamic_momentum_override",
                        "source": "dynamic_universe",
                        "scan_selected": True,
                        "entry_eval": {"result": True, "reason": "ok"},
                        "allocator_candidate": {"result": False, "reason": "rank_cap"},
                        "simulated_submit": {"result": False, "reason": "rank_cap"},
                    },
                    {
                        "symbol": "RVOL",
                        "route": "dynamic_momentum_override",
                        "source": "dynamic_universe",
                        "scan_selected": True,
                        "entry_eval": {"result": True, "reason": "ok"},
                        "allocator_action": {"result": True, "reason": "created"},
                        "order_build": {"result": False, "reason": "dynamic_relative_volume"},
                        "simulated_submit": {"result": False, "reason": "dynamic_relative_volume"},
                    },
                    {
                        "symbol": "VWAP",
                        "route": "dynamic_momentum_override",
                        "source": "dynamic_universe",
                        "scan_selected": True,
                        "entry_eval": {"result": True, "reason": "ok"},
                        "allocator_action": {"result": True, "reason": "created"},
                        "order_build": {"result": False, "reason": "dynamic_vwap"},
                        "simulated_submit": {"result": False, "reason": "dynamic_vwap"},
                    },
                    {
                        "symbol": "NOQT",
                        "route": "dynamic_momentum_override",
                        "source": "dynamic_universe",
                        "scan_selected": True,
                        "entry_eval": {"result": True, "reason": "ok"},
                        "order_build": {"result": False, "reason": "no_quote"},
                        "simulated_submit": {"result": False, "reason": "no_quote"},
                    },
                    {
                        "symbol": "MISS",
                        "route": "dynamic_momentum_override",
                        "source": "dynamic_universe",
                        "scan_selected": True,
                        "entry_eval": {"result": True, "reason": "ok"},
                        "allocator_candidate": {"result": False, "reason": "not_seen_by_allocator"},
                        "simulated_submit": {"result": False, "reason": "not_reached"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    report = build_dynamic_gate_research_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-18",
        user_id="live_bot",
    )
    counts = report["downstream_rejection_counts_by_category"]
    by_symbol = {
        row["symbol"]: row for row in report["dynamic_entry_eval_passed_downstream_rejections"]
    }

    assert counts["allocator_filtered_no_catalyst"] == 1
    assert counts["allocator_filtered_rank_cap"] == 1
    assert counts["dispatch_dynamic_relative_volume"] == 1
    assert counts["dispatch_dynamic_vwap"] == 1
    assert counts["no_quote"] == 1
    assert counts["missing_terminal_record"] == 1
    assert by_symbol["CAT"]["downstream_category"] == "allocator_filtered_no_catalyst"
    assert by_symbol["RVOL"]["downstream_category"] == "dispatch_dynamic_relative_volume"
    assert by_symbol["MISS"]["downstream_category"] == "missing_terminal_record"
    rendered = render_dynamic_gate_research_report(report)
    assert "Downstream rejection attribution:" in rendered
    assert "- dispatch_dynamic_relative_volume: 1" in rendered
