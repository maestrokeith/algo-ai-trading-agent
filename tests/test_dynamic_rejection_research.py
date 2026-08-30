from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.dynamic_rejection_research import (
    build_dynamic_rejection_report,
    latest_dynamic_rejection_date,
    load_dynamic_rejection_rows,
    render_dynamic_rejection_report,
    write_dynamic_rejection_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_history(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-06-08T14:31:00+00:00",
                "user_id": "live_bot",
                "rejected": [
                    {
                        "symbol": "FIVE",
                        "timestamp": "2026-06-08T14:31:00+00:00",
                        "price": 10.0,
                        "gain_pct": 4.4,
                        "rel_volume": 2.0,
                        "spread_pct": 0.2,
                        "news_score": 0,
                        "catalyst_score": 0.0,
                        "rejection_reason": "below_min_day_gain",
                        "later_same_day_high": 10.6,
                        "later_same_day_return_pct": 6.0,
                    },
                    {
                        "symbol": "TEN",
                        "timestamp": "2026-06-08T14:32:00+00:00",
                        "price": 20.0,
                        "gain_pct": 100.0,
                        "rel_volume": 5.0,
                        "spread_pct": 0.4,
                        "news_score": 3,
                        "catalyst_score": 0.7,
                        "rejection_reason": "gain filter",
                        "later_same_day_high": 22.5,
                        "later_same_day_return_pct": 12.5,
                    },
                    {
                        "symbol": "TWEN",
                        "timestamp": "2026-06-08T14:33:00+00:00",
                        "price": 5.0,
                        "gain_pct": -2.8,
                        "rel_volume": 1.5,
                        "spread_pct": 0.3,
                        "news_score": 1,
                        "catalyst_score": 0.1,
                        "rejection_reason": "below_min_day_gain",
                        "later_same_day_high": 6.25,
                        "later_same_day_return_pct": 25.0,
                    },
                    {
                        "symbol": "NONE",
                        "timestamp": "2026-06-08T14:34:00+00:00",
                        "price": 30.0,
                        "gain_pct": 8.0,
                        "rel_volume": 0.5,
                        "spread_pct": 0.2,
                        "news_score": 0,
                        "catalyst_score": 0.0,
                        "rejection_reason": "below_min_relative_volume",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


def test_dynamic_rejection_report_buckets_later_movers(tmp_path: Path) -> None:
    history = tmp_path / "data" / "dynamic_scan_history"
    _write_history(history / "20260608T143100000000Z_live_bot.json")

    assert latest_dynamic_rejection_date(data_dir=tmp_path / "data", user_id="live_bot") == "2026-06-08"
    report = build_dynamic_rejection_report(
        data_dir=tmp_path / "data",
        user_id="live_bot",
        day="2026-06-08",
    )

    assert report["total_rejected"] == 4
    assert report["with_later_outcomes"] == 3
    assert [row["symbol"] for row in report["buckets"]["+5%"]] == ["TWEN", "TEN", "FIVE"]
    assert [row["symbol"] for row in report["buckets"]["+10%"]] == ["TWEN", "TEN"]
    assert [row["symbol"] for row in report["buckets"]["+20%"]] == ["TWEN"]
    markdown = render_dynamic_rejection_report(report)
    assert "Dynamic Rejection Outcome Report - 2026-06-08" in markdown
    assert "| TWEN | 25.00%" in markdown


def test_dynamic_rejection_rows_backfill_later_same_day_outcomes_from_local_bars(tmp_path: Path) -> None:
    history = tmp_path / "data" / "dynamic_scan_history"
    history.mkdir(parents=True)
    (history / "20260609T143100000000Z_live_bot.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-06-09T14:31:00+00:00",
                "user_id": "live_bot",
                "rejected": [
                    {
                        "symbol": "MOVE",
                        "timestamp": "2026-06-09T14:31:00+00:00",
                        "price": 10.0,
                        "gain_pct": 4.4,
                        "rel_volume": 2.0,
                        "spread_pct": 0.2,
                        "rejection_reason": "below_min_day_gain",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    bars_dir = tmp_path / "data" / "historical_bars"
    bars_dir.mkdir(parents=True)
    (bars_dir / "MOVE_2026-06-09_1Min.csv").write_text(
        "\n".join(
            [
                "timestamp,high,close",
                "2026-06-09T14:30:00+00:00,10.25,10.10",
                "2026-06-09T14:32:00+00:00,10.80,10.70",
                "2026-06-09T19:59:00+00:00,11.25,11.00",
                "2026-06-10T14:32:00+00:00,30.00,30.00",
            ]
        ),
        encoding="utf-8",
    )

    rows = load_dynamic_rejection_rows(
        data_dir=tmp_path / "data",
        user_id="live_bot",
        day="2026-06-09",
    )
    report = build_dynamic_rejection_report(
        data_dir=tmp_path / "data",
        user_id="live_bot",
        day="2026-06-09",
    )

    assert rows[0].later_same_day_high == 11.25
    assert rows[0].later_same_day_return_pct == 12.5
    assert report["with_later_outcomes"] == 1
    assert report["with_forward_outcomes"] == 1
    assert report["top_missed"][0]["later_same_day_high"] == 11.25
    assert report["top_missed"][0]["return_15m_pct"] == pytest.approx(10.0)


def test_dynamic_rejection_rows_backfill_later_same_day_outcomes_for_paper_bot(tmp_path: Path) -> None:
    history = tmp_path / "data" / "dynamic_scan_history"
    history.mkdir(parents=True)
    (history / "20260609T150000000000Z_paper_bot.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-06-09T15:00:00+00:00",
                "user_id": "paper_bot",
                "rejected": [
                    {
                        "symbol": "PAPR",
                        "timestamp": "2026-06-09T15:00:00+00:00",
                        "price": 20.0,
                        "rejection_reason": "below_min_relative_volume",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    bars_dir = tmp_path / "data" / "historical_bars"
    bars_dir.mkdir(parents=True)
    (bars_dir / "PAPR_20260609.json").write_text(
        json.dumps(
            {
                "bars": [
                    {"timestamp": "2026-06-09T15:01:00+00:00", "high": 21.0},
                    {"timestamp": "2026-06-09T19:55:00+00:00", "high": 22.0},
                ]
            }
        ),
        encoding="utf-8",
    )

    report = build_dynamic_rejection_report(
        data_dir=tmp_path / "data",
        user_id="paper_bot",
        day="2026-06-09",
    )

    assert report["with_later_outcomes"] == 1
    assert report["top_missed"][0]["symbol"] == "PAPR"
    assert report["top_missed"][0]["later_same_day_high"] == 22.0
    assert report["top_missed"][0]["later_same_day_return_pct"] == pytest.approx(10.0)


def test_dynamic_rejection_report_falls_back_to_paper_replay_rejections_with_local_bars(tmp_path: Path) -> None:
    replay_dir = tmp_path / "data" / "replay"
    replay_dir.mkdir(parents=True)
    (replay_dir / "2026-06-09_paper_bot.json").write_text(
        json.dumps(
            {
                "rejected_candidates": [
                    {
                        "symbol": "RPLY",
                        "price": 10.0,
                        "relative_volume": 2.0,
                        "spread_pct": 0.4,
                        "reason": "unstable quote",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    bars_dir = tmp_path / "data" / "historical_bars"
    bars_dir.mkdir(parents=True)
    (bars_dir / "RPLY_2026-06-09_1Min.csv").write_text(
        "\n".join(
            [
                "timestamp,high",
                "2026-06-09T13:29:00+00:00,20.00",
                "2026-06-09T13:31:00+00:00,10.50",
                "2026-06-09T19:59:00+00:00,12.00",
            ]
        ),
        encoding="utf-8",
    )

    assert latest_dynamic_rejection_date(data_dir=tmp_path / "data", user_id="paper_bot") == "2026-06-09"
    report = build_dynamic_rejection_report(
        data_dir=tmp_path / "data",
        user_id="paper_bot",
        day="2026-06-09",
    )

    assert report["total_rejected"] == 1
    assert report["with_later_outcomes"] == 1
    assert report["top_missed"][0]["later_same_day_high"] == 12.0
    assert report["top_missed"][0]["later_same_day_return_pct"] == pytest.approx(20.0)


def test_dynamic_rejection_report_uses_replay_session_history_and_candidate_bars(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    history_dir = data_dir / "dynamic_scan_history"
    history_dir.mkdir(parents=True)
    history_path = history_dir / "20260618T143000000000Z_live_bot.json"
    history_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-06-18T14:30:00+00:00",
                "user_id": "live_bot",
                "rejected": [
                    {
                        "symbol": "ATPC",
                        "timestamp": "2026-06-18T14:30:00+00:00",
                        "price": 5.0,
                        "relative_volume": 2.0,
                        "rejection_reason": "spread too wide",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    replay_dir = data_dir / "replay_market_session"
    replay_dir.mkdir(parents=True)
    (replay_dir / "2026-06-18_live_bot.json").write_text(
        json.dumps(
            {
                "cycle_summaries": [
                    {
                        "tick_et": "2026-06-18T10:30:00-04:00",
                        "history_path": str(history_path),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    bars_dir = data_dir / "research" / "dynamic_candidate_bars" / "2026-06-18" / "live_bot"
    bars_dir.mkdir(parents=True)
    (bars_dir / "ATPC.json").write_text(
        json.dumps(
            {
                "symbol": "ATPC",
                "bars": [
                    {"timestamp": "2026-06-18T14:45:00+00:00", "high": 5.5, "close": 5.25},
                    {"timestamp": "2026-06-18T15:00:00+00:00", "high": 6.0, "close": 5.75},
                    {"timestamp": "2026-06-18T15:30:00+00:00", "high": 6.25, "close": 6.0},
                    {"timestamp": "2026-06-18T19:59:00+00:00", "high": 6.5, "close": 6.2},
                ],
            }
        ),
        encoding="utf-8",
    )

    report = build_dynamic_rejection_report(
        data_dir=data_dir,
        user_id="live_bot",
        day="2026-06-18",
        history_dir=tmp_path / "empty_history",
    )

    assert report["total_rejected"] == 1
    assert report["with_later_outcomes"] == 1
    assert report["with_forward_outcomes"] == 1
    row = report["top_missed"][0]
    assert row["symbol"] == "ATPC"
    assert row["later_same_day_high"] == 6.5
    assert row["later_same_day_return_pct"] == pytest.approx(30.0)
    assert row["return_60m_pct"] == pytest.approx(20.0)
    assert row["return_eod_pct"] == pytest.approx(24.0)


def test_dynamic_rejection_report_tracks_missing_timestamp_and_missing_local_bars(
    tmp_path: Path,
) -> None:
    history = tmp_path / "data" / "dynamic_scan_history"
    history.mkdir(parents=True)
    (history / "20260618T143000000000Z_live_bot.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-06-18T14:30:00+00:00",
                "user_id": "live_bot",
                "rejected": [
                    {
                        "symbol": "NOBAR",
                        "price": 10.0,
                        "rejection_reason": "below_min_relative_volume",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = build_dynamic_rejection_report(
        data_dir=tmp_path / "data",
        user_id="live_bot",
        day="2026-06-18",
    )

    assert report["total_rejected"] == 1
    assert report["with_later_outcomes"] == 0
    assert report["missing_outcome_reasons"]["missing_local_bars"] == 1
    assert report["top_missed"] == []


def test_write_dynamic_rejection_report_outputs_markdown_and_json(tmp_path: Path) -> None:
    history = tmp_path / "data" / "dynamic_scan_history"
    _write_history(history / "20260608T143100000000Z_live_bot.json")

    md_path, json_path, report = write_dynamic_rejection_report(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        user_id="live_bot",
        day="2026-06-08",
    )

    assert md_path == tmp_path / "reports" / "research_feedback" / "dynamic_rejections_2026-06-08_live_bot.md"
    assert json_path.exists()
    assert report["buckets"]["+20%"][0]["symbol"] == "TWEN"
    assert "Later Move +10%" in md_path.read_text(encoding="utf-8")


def test_generate_dynamic_rejection_report_cli_latest(tmp_path: Path) -> None:
    history = tmp_path / "data" / "dynamic_scan_history"
    _write_history(history / "20260608T143100000000Z_live_bot.json")

    proc = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_dynamic_rejection_report.py"),
            "--date",
            "latest",
            "--user",
            "live_bot",
            "--data-dir",
            str(tmp_path / "data"),
            "--project-root",
            str(tmp_path),
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "Dynamic Rejection Outcome Report - 2026-06-08" in proc.stdout
    assert (tmp_path / "reports" / "research_feedback" / "dynamic_rejections_2026-06-08_live_bot.md").exists()
