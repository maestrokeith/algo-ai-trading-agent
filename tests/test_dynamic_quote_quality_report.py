from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from src.dynamic_quote_quality_report import (
    build_dynamic_quote_quality_report,
    render_dynamic_quote_quality_report,
    write_dynamic_quote_quality_report,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_history(path: Path) -> None:
    payload = {
        "generated_at": "2026-07-01T14:00:00+00:00",
        "user_id": "live_bot",
        "candidates": [
            {
                "symbol": "WIDE",
                "timestamp": "2026-07-01T14:00:01+00:00",
                "accepted": False,
                "rejection_reason": "unstable quote",
                "bid": 90.0,
                "ask": 110.0,
                "spread_pct": 20.0,
                "quote_age_seconds": 2.0,
                "volume": 100000.0,
            },
            {
                "symbol": "CROSS",
                "timestamp": "2026-07-01T14:01:01+00:00",
                "accepted": False,
                "rejection_reason": "unstable quote",
                "bid": 12.0,
                "ask": 11.0,
                "spread_pct": 8.695652,
                "quote_age_seconds": 1.0,
                "volume": 100000.0,
            },
            {
                "symbol": "STALE",
                "timestamp": "2026-07-01T14:02:01+00:00",
                "accepted": False,
                "rejection_reason": "unstable quote",
                "bid": 20.0,
                "ask": 20.2,
                "spread_pct": 0.995,
                "quote_age_seconds": 90.0,
                "volume": 100000.0,
            },
            {
                "symbol": "NOBID",
                "timestamp": "2026-07-01T14:03:01+00:00",
                "accepted": False,
                "rejection_reason": "unstable quote",
                "bid": 0.0,
                "ask": 10.0,
                "spread_pct": 20.0,
                "quote_age_seconds": 1.0,
                "volume": 100000.0,
            },
            {
                "symbol": "NOASK",
                "timestamp": "2026-07-01T14:04:01+00:00",
                "accepted": False,
                "rejection_reason": "unstable quote",
                "bid": 10.0,
                "ask": 0.0,
                "spread_pct": 20.0,
                "quote_age_seconds": 1.0,
                "volume": 100000.0,
            },
            {
                "symbol": "ZVOL",
                "timestamp": "2026-07-01T14:05:01+00:00",
                "accepted": False,
                "rejection_reason": "unstable quote",
                "bid": 10.0,
                "ask": 10.2,
                "spread_pct": 1.9802,
                "quote_age_seconds": 1.0,
                "volume": 0.0,
            },
            {
                "symbol": "HALT",
                "timestamp": "2026-07-01T14:06:01+00:00",
                "accepted": False,
                "rejection_reason": "unstable quote",
                "bid": 10.0,
                "ask": 10.2,
                "spread_pct": 1.9802,
                "quote_age_seconds": 1.0,
                "volume": 100000.0,
                "security_state": "halted",
            },
            {
                "symbol": "LATER",
                "timestamp": "2026-07-01T14:07:01+00:00",
                "accepted": False,
                "rejection_reason": "unstable quote",
                "bid": 40.0,
                "ask": 50.0,
                "spread_pct": 22.2222,
                "quote_age_seconds": 1.0,
                "volume": 100000.0,
            },
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _sample_log() -> str:
    return "\n".join(
        [
            "2026-07-01T10:00:00 INFO QUOTE_RETRY_START symbol=WIDE reason=unstable_quote attempt=1",
            "2026-07-01T10:00:01 INFO QUOTE_RETRY_SUCCESS symbol=WIDE attempt=1",
            "2026-07-01T10:01:00 INFO QUOTE_RETRY_START symbol=NOBID reason=unstable_quote attempt=1",
            "2026-07-01T10:01:01 INFO QUOTE_RETRY_FAILED symbol=NOBID attempts=2",
            "2026-07-01T10:01:02 INFO QUOTE_RETRY_FINAL_REJECT symbol=NOBID reason=unstable_quote",
            "2026-07-01T10:05:00 INFO DYNAMIC_SELECTED symbol=LATER score=33.0",
            (
                "2026-07-01T10:06:00 INFO DYNAMIC_SCAN reject LOGWIDE: unstable quote "
                "spread=18.50% bid=8.00 ask=9.62 quote_age_seconds=3 volume=25000"
            ),
        ]
    )


def test_quote_quality_parser_classifies_root_causes(tmp_path: Path) -> None:
    history_dir = tmp_path / "data" / "dynamic_scan_history"
    history_dir.mkdir(parents=True)
    _write_history(history_dir / "20260701T140000000000Z_live_bot.json")

    report = build_dynamic_quote_quality_report(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="2026-07-01",
        user_id="live_bot",
        log_text=_sample_log(),
    )

    counts = report["summary"]["root_cause_counts"]
    assert counts["spread_too_wide"] == 3
    assert counts["crossed_market"] == 1
    assert counts["stale_quote"] == 1
    assert counts["missing_bid"] == 1
    assert counts["missing_ask"] == 1
    assert counts["zero_volume"] == 1
    assert counts["halted_security_state"] == 1
    assert report["summary"]["total_unstable_quotes"] == 9
    assert report["summary"]["average_quote_age"] == 11.2222

    by_symbol = {event["symbol"]: event for event in report["events"]}
    assert by_symbol["NOBID"]["root_causes"] == ["missing_bid", "spread_too_wide"]
    assert by_symbol["HALT"]["root_cause"] == "halted_security_state"
    assert by_symbol["LOGWIDE"]["source"] == "logs"


def test_quote_quality_retry_classification_and_later_tradable(tmp_path: Path) -> None:
    history_dir = tmp_path / "data" / "dynamic_scan_history"
    history_dir.mkdir(parents=True)
    _write_history(history_dir / "20260701T140000000000Z_live_bot.json")

    report = build_dynamic_quote_quality_report(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="2026-07-01",
        user_id="live_bot",
        log_text=_sample_log(),
    )

    by_symbol = {event["symbol"]: event for event in report["events"]}
    assert by_symbol["WIDE"]["retry_attempted"] is True
    assert by_symbol["WIDE"]["retry_succeeded"] is True
    assert by_symbol["NOBID"]["retry_attempted"] is True
    assert by_symbol["NOBID"]["retry_failed"] is True
    assert by_symbol["LATER"]["became_tradable_later"] is True
    assert report["summary"]["retry_attempted"] == 2
    assert report["summary"]["retry_succeeded"] == 1
    assert report["summary"]["retry_failed"] == 1
    assert report["summary"]["retry_recovery_rate"] == 0.5
    assert report["summary"]["symbols_that_became_tradable_later"] == ["LATER"]


def test_quote_quality_report_writes_artifacts_and_cli(tmp_path: Path) -> None:
    history_dir = tmp_path / "data" / "dynamic_scan_history"
    history_dir.mkdir(parents=True)
    _write_history(history_dir / "20260701T140000000000Z_live_bot.json")
    log_dir = tmp_path / "data" / "review" / "2026-07-01"
    log_dir.mkdir(parents=True)
    (log_dir / "live.log").write_text(_sample_log(), encoding="utf-8")

    json_path, text_path, report = write_dynamic_quote_quality_report(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="2026-07-01",
        user_id="live_bot",
    )

    assert json_path == tmp_path / "data" / "research_metrics" / "2026-07-01" / "dynamic_quote_quality.json"
    assert text_path == tmp_path / "data" / "research_metrics" / "2026-07-01" / "dynamic_quote_quality.md"
    assert report["summary"]["root_cause_counts"]["spread_too_wide"] == 3
    assert json.loads(json_path.read_text(encoding="utf-8"))["summary"]["retry_recovery_rate"] == 0.5
    text = text_path.read_text(encoding="utf-8")
    assert "Dynamic Quote Quality Report 2026-07-01 user=live_bot" in text
    assert "| LOGWIDE |" in text
    assert "retry recovery rate: 0.5" in render_dynamic_quote_quality_report(report)

    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_dynamic_quote_quality_report.py"),
            "--date",
            "2026-07-01",
            "--user",
            "live_bot",
            "--project-root",
            str(tmp_path),
            "--data-dir",
            str(tmp_path / "data"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Dynamic Quote Quality Report 2026-07-01 user=live_bot" in result.stdout
    assert "JSON:" in result.stdout
    assert "Markdown:" in result.stdout
