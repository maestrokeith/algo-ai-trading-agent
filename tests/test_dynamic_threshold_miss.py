from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.dynamic_threshold_miss import (
    build_dynamic_threshold_miss_report,
    render_dynamic_threshold_miss_report,
    write_dynamic_threshold_miss_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_fixture(tmp_path: Path) -> tuple[Path, Path]:
    data_dir = tmp_path / "data"
    history = data_dir / "dynamic_scan_history"
    bars = data_dir / "research" / "dynamic_candidate_bars" / "2026-06-12" / "live_bot"
    history.mkdir(parents=True)
    bars.mkdir(parents=True)
    (history / "20260612T143800000000Z_live_bot.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-06-12T14:38:00+00:00",
                "candidates": [
                    {
                        "symbol": "INTC",
                        "accepted": False,
                        "timestamp": "2026-06-12T14:38:00+00:00",
                        "price": 30.0,
                        "gain_pct": 6.89,
                        "rel_volume": 0.80,
                        "spread_pct": 0.10,
                        "avg_volume": 500_000,
                        "rejection_reason": "below_min_relative_volume rel=0.80 min=1.00",
                    },
                    {
                        "symbol": "TQQQ",
                        "accepted": False,
                        "timestamp": "2026-06-12T14:39:00+00:00",
                        "price": 60.0,
                        "gain_pct": 2.19,
                        "rel_volume": 1.30,
                        "spread_pct": 0.05,
                        "avg_volume": 900_000,
                        "rejection_reason": "below_min_day_gain gain=2.19 min=3.00",
                    },
                    {
                        "symbol": "AAL",
                        "accepted": False,
                        "timestamp": "2026-06-12T14:40:00+00:00",
                        "price": 12.0,
                        "gain_pct": 2.05,
                        "rel_volume": 0.40,
                        "spread_pct": 0.20,
                        "avg_volume": 1_000,
                        "rejection_reason": "below_min_avg_volume avg=1000 min=10000",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (bars / "INTC.json").write_text(
        json.dumps(
            {
                "symbol": "INTC",
                "bars": [
                    {"timestamp": "2026-06-12T14:38:00+00:00", "open": 30, "high": 30.0, "low": 30.0, "close": 30.0},
                    {"timestamp": "2026-06-12T14:53:00+00:00", "open": 30, "high": 31.2, "low": 29.9, "close": 31.0},
                    {"timestamp": "2026-06-12T15:08:00+00:00", "open": 31, "high": 31.5, "low": 30.5, "close": 30.6},
                    {"timestamp": "2026-06-12T15:38:00+00:00", "open": 30.6, "high": 32.0, "low": 30.2, "close": 31.5},
                ],
            }
        ),
        encoding="utf-8",
    )
    (bars / "TQQQ.json").write_text(
        json.dumps(
            {
                "symbol": "TQQQ",
                "bars": [
                    {"timestamp": "2026-06-12T14:39:00+00:00", "open": 60, "high": 60.0, "low": 60.0, "close": 60.0},
                    {"timestamp": "2026-06-12T14:54:00+00:00", "open": 60, "high": 60.2, "low": 59.0, "close": 59.4},
                    {"timestamp": "2026-06-12T15:09:00+00:00", "open": 59.4, "high": 59.6, "low": 58.9, "close": 59.0},
                ],
            }
        ),
        encoding="utf-8",
    )
    return data_dir, bars


def test_dynamic_threshold_miss_classifies_gaps_and_outcomes(tmp_path: Path) -> None:
    data_dir, _bars = _write_fixture(tmp_path)

    report = build_dynamic_threshold_miss_report(data_dir=data_dir, day="2026-06-12", user_id="live_bot")
    by_symbol = {row["symbol"]: row for row in report["candidates"]}

    assert report["summary"]["rejected_candidates"] == 3
    assert by_symbol["INTC"]["rel_volume_gap"] == pytest.approx(0.2)
    assert by_symbol["INTC"]["distance_class"] == "moderate_miss"
    assert by_symbol["INTC"]["max_gain_after_rejection_pct"] == pytest.approx(6.6667)
    assert by_symbol["INTC"]["return_after_15m_pct"] == pytest.approx(3.3333)
    assert by_symbol["INTC"]["return_after_30m_pct"] == pytest.approx(2.0)
    assert by_symbol["INTC"]["return_after_60m_pct"] == pytest.approx(5.0)
    assert by_symbol["TQQQ"]["gain_gap"] == pytest.approx(0.27)
    assert by_symbol["TQQQ"]["return_after_15m_pct"] == pytest.approx(-1.0)
    assert by_symbol["AAL"]["avg_volume_gap"] == pytest.approx(0.9)
    assert by_symbol["AAL"]["distance_class"] == "severe_miss"
    assert report["outcomes_by_rejection_type"]["below_min_relative_volume"]["average_return_after_15m_pct"] == pytest.approx(3.3333)
    assert report["outcomes_by_threshold_distance"]["moderate_miss"]["count"] == 2
    assert report["top_profitable_near_misses"] == []
    assert "Dynamic Threshold Miss Research - 2026-06-12 user=live_bot" in render_dynamic_threshold_miss_report(report)


def test_dynamic_threshold_miss_writes_artifacts_cli_and_logs(tmp_path: Path) -> None:
    data_dir, bars = _write_fixture(tmp_path)
    log_path = tmp_path / "algo_2026-06-12.log"
    log_path.write_text(
        "\n".join(
            [
                "Jun 12 10:41:00 host python[1]: DYNAMIC_SCAN NOK: price=4.20 gain=5.07 rel=0.61 spread=0.07% avg=900000",
                "Jun 12 10:41:00 host python[1]: DYNAMIC_SCAN reject NOK: below_min_relative_volume rel=0.61 min=1.00",
            ]
        ),
        encoding="utf-8",
    )

    json_path, text_path, report = write_dynamic_threshold_miss_report(
        data_dir=data_dir,
        day="2026-06-12",
        user_id="live_bot",
        log_paths=[log_path],
    )

    assert json_path == data_dir / "research" / "dynamic_threshold_miss" / "2026-06-12_live_bot.json"
    assert text_path == data_dir / "research" / "dynamic_threshold_miss" / "2026-06-12_live_bot.txt"
    assert any(row["symbol"] == "NOK" for row in report["candidates"])
    assert "Top Profitable Near Misses" in text_path.read_text(encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_dynamic_threshold_miss.py"),
            "--date",
            "2026-06-12",
            "--user",
            "live_bot",
            "--data-dir",
            str(data_dir),
            "--bars-dir",
            str(bars),
            "--log-path",
            str(log_path),
            "--no-journal",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "Dynamic Threshold Miss Research - 2026-06-12 user=live_bot" in proc.stdout
    assert "JSON:" in proc.stdout
