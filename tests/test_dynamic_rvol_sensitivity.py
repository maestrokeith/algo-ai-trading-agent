from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.dynamic_rvol_sensitivity import (
    build_dynamic_rvol_sensitivity_report,
    latest_dynamic_rvol_sensitivity_date,
    render_dynamic_rvol_sensitivity_report,
    write_dynamic_rvol_sensitivity_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_scan_fixture(tmp_path: Path) -> tuple[Path, Path]:
    data_dir = tmp_path / "data"
    history_dir = data_dir / "dynamic_scan_history"
    bars_dir = data_dir / "historical_bars"
    history_dir.mkdir(parents=True)
    bars_dir.mkdir(parents=True)
    payload = {
        "user_id": "default",
        "generated_at": "2026-06-12T14:38:00+00:00",
        "accepted": [],
        "selected": [],
        "rejected": [],
        "candidates": [
            {
                "symbol": "ASTN",
                "accepted": False,
                "timestamp": "2026-06-12T14:38:00+00:00",
                "price": 10.00,
                "gain_pct": 21.66,
                "rel_volume": 0.93,
                "spread_pct": 0.37,
                "avg_volume": 250000,
                "rejection_reason": "below_min_relative_volume",
            },
            {
                "symbol": "NOK",
                "accepted": False,
                "timestamp": "2026-06-12T14:39:00+00:00",
                "price": 4.20,
                "gain_pct": 6.03,
                "rel_volume": 0.43,
                "spread_pct": 0.07,
                "avg_volume": 900000,
                "rejection_reason": "below_min_relative_volume",
            },
            {
                "symbol": "INTC",
                "accepted": False,
                "timestamp": "2026-06-12T14:40:00+00:00",
                "price": 31.00,
                "gain_pct": 4.97,
                "rel_volume": 0.27,
                "spread_pct": 0.04,
                "avg_volume": 1200000,
                "rejection_reason": "below_min_relative_volume",
            },
            {
                "symbol": "POEL",
                "accepted": False,
                "timestamp": "2026-06-12T14:41:00+00:00",
                "price": 8.00,
                "gain_pct": 7.20,
                "rel_volume": 0.55,
                "spread_pct": 0.20,
                "avg_volume": 80000,
                "rejection_reason": "below_min_relative_volume",
            },
            {
                "symbol": "DSY",
                "accepted": False,
                "timestamp": "2026-06-12T14:42:00+00:00",
                "price": 12.00,
                "gain_pct": 30.19,
                "rel_volume": 2.84,
                "spread_pct": 1.18,
                "avg_volume": 600000,
                "quality": {"atr_expansion_ratio": 0.08},
                "rejection_reason": "ATR expansion 0.08 < 0.25",
            },
            {
                "symbol": "GOOD",
                "accepted": True,
                "timestamp": "2026-06-12T14:43:00+00:00",
                "price": 20.00,
                "gain_pct": 12.0,
                "rel_volume": 1.4,
                "spread_pct": 0.1,
                "avg_volume": 500000,
                "rejection_reason": None,
            },
        ],
    }
    (history_dir / "20260612T143800000000Z_default.json").write_text(json.dumps(payload), encoding="utf-8")
    (bars_dir / "ASTN_2026-06-12_1Min.csv").write_text(
        "\n".join(
            [
                "timestamp,close",
                "2026-06-12T14:38:00+00:00,10.00",
                "2026-06-12T14:53:00+00:00,10.50",
                "2026-06-12T15:08:00+00:00,9.80",
                "2026-06-12T15:38:00+00:00,11.00",
                "2026-06-12T19:59:00+00:00,10.20",
            ]
        ),
        encoding="utf-8",
    )
    return data_dir, bars_dir


def test_dynamic_rvol_sensitivity_compares_thresholds_and_forward_returns(tmp_path: Path) -> None:
    data_dir, bars_dir = _write_scan_fixture(tmp_path)

    report = build_dynamic_rvol_sensitivity_report(
        data_dir=data_dir,
        day="2026-06-12",
        user_id="paper_bot",
        bars_dir=bars_dir,
    )

    assert report["source_mode"] == "default_fallback"
    assert report["summary"]["total_scanned_candidates"] == 6
    assert report["summary"]["rvol_only_rejections"] == 4
    assert report["summary"]["reason_counts"]["below_min_relative_volume"] == 4
    assert report["summary"]["reason_counts"]["atr_expansion"] == 1
    assert report["thresholds"]["current_100"]["candidates_that_would_pass_if_rvol_relaxed"] == 0
    assert report["thresholds"]["relaxed_075"]["symbols"] == ["ASTN"]
    assert report["thresholds"]["relaxed_050"]["symbols"] == ["ASTN", "POEL"]

    astn = report["key_symbol_examples"]["ASTN"]
    assert astn["return_15m_pct"] == pytest.approx(5.0)
    assert astn["return_30m_pct"] == pytest.approx(-2.0)
    assert astn["return_60m_pct"] == pytest.approx(10.0)
    assert astn["return_eod_pct"] == pytest.approx(2.0)
    assert astn["other_gate_would_still_block"] is False
    assert report["key_symbol_examples"]["DSY"]["other_gate_would_still_block"] is True


def test_dynamic_rvol_sensitivity_latest_writes_artifacts_and_cli(tmp_path: Path) -> None:
    data_dir, bars_dir = _write_scan_fixture(tmp_path)

    assert latest_dynamic_rvol_sensitivity_date(data_dir=data_dir, user_id="paper_bot") == "2026-06-12"

    json_path, text_path, report = write_dynamic_rvol_sensitivity_report(
        data_dir=data_dir,
        day="latest",
        user_id="paper_bot",
        bars_dir=bars_dir,
    )

    assert json_path == data_dir / "research" / "dynamic_rvol_sensitivity" / "2026-06-12_paper_bot.json"
    assert text_path == data_dir / "research" / "dynamic_rvol_sensitivity" / "2026-06-12_paper_bot.txt"
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved["research_only"] is True
    text = text_path.read_text(encoding="utf-8")
    assert "Dynamic RVOL Sensitivity - 2026-06-12 user=paper_bot" in text
    assert "ASTN" in render_dynamic_rvol_sensitivity_report(report)

    proc = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_dynamic_rvol_sensitivity.py"),
            "--date",
            "2026-06-12",
            "--user",
            "paper_bot",
            "--data-dir",
            str(data_dir),
            "--bars-dir",
            str(bars_dir),
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "Dynamic RVOL Sensitivity - 2026-06-12 user=paper_bot" in proc.stdout
    assert "JSON:" in proc.stdout
    assert "relaxed_050" in proc.stdout


def test_dynamic_rvol_sensitivity_ingests_dynamic_scan_logs(tmp_path: Path) -> None:
    data_dir, bars_dir = _write_scan_fixture(tmp_path)
    log_path = tmp_path / "algo_2026-06-12.log"
    log_path.write_text(
        "\n".join(
            [
                "Jun 12 10:38:00 host python[1]: DYNAMIC_SCAN ASTN: price=5.165 gain=21.66 vol=1395.0 avg=37365.9 rel=0.93 spread=0.37% range=14.13% vwap_above=True trend5m=None atr_exp=0.94 news_score=0",
                "Jun 12 10:38:00 host python[1]: DYNAMIC_SCAN reject ASTN: below_min_relative_volume rel=0.93 min=1.00 catalyst_score=0.00",
            ]
        ),
        encoding="utf-8",
    )

    report = build_dynamic_rvol_sensitivity_report(
        data_dir=data_dir,
        day="2026-06-12",
        user_id="paper_bot",
        bars_dir=bars_dir,
        log_paths=[log_path],
    )

    assert str(log_path) in report["log_files"]
    assert report["summary"]["rvol_only_rejections"] == 5
    assert report["thresholds"]["relaxed_075"]["candidates_that_would_pass_if_rvol_relaxed"] == 2
    astn_rows = [row for row in report["rvol_only_examples"] if row["symbol"] == "ASTN"]
    assert astn_rows
    assert any(row["rel_volume"] == pytest.approx(0.93) for row in astn_rows)
