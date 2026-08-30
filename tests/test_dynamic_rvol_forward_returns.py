from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.dynamic_rvol_forward_returns import (
    build_dynamic_rvol_forward_returns_report,
    render_dynamic_rvol_forward_returns_report,
    write_dynamic_rvol_forward_returns_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_bars(bars_dir: Path, symbol: str, rows: list[tuple[str, float]]) -> None:
    (bars_dir / f"{symbol}_2026-06-12_1Min.csv").write_text(
        "\n".join(["timestamp,close", *[f"{ts},{close}" for ts, close in rows]]),
        encoding="utf-8",
    )


def _write_fixture(tmp_path: Path) -> tuple[Path, Path]:
    data_dir = tmp_path / "data"
    history_dir = data_dir / "dynamic_scan_history"
    bars_dir = data_dir / "historical_bars"
    reports_dir = data_dir / "research_feedback"
    history_dir.mkdir(parents=True)
    bars_dir.mkdir(parents=True)
    reports_dir.mkdir(parents=True)
    payload = {
        "user_id": "default",
        "generated_at": "2026-06-12T14:30:00+00:00",
        "candidates": [
            {
                "symbol": "ASTN",
                "accepted": False,
                "timestamp": "2026-06-12T14:30:00+00:00",
                "price": 10.00,
                "gain_pct": 20.0,
                "rel_volume": 0.93,
                "spread_pct": 0.30,
                "avg_volume": 500000,
                "rejection_reason": "below_min_relative_volume",
            },
            {
                "symbol": "AXTX",
                "accepted": False,
                "timestamp": "2026-06-12T14:31:00+00:00",
                "price": 20.00,
                "gain_pct": 8.0,
                "rel_volume": 0.60,
                "spread_pct": 0.20,
                "avg_volume": 600000,
                "rejection_reason": "below_min_relative_volume",
            },
            {
                "symbol": "RZLV",
                "accepted": False,
                "timestamp": "2026-06-12T14:32:00+00:00",
                "price": 5.00,
                "gain_pct": 12.0,
                "rel_volume": 0.80,
                "spread_pct": 0.25,
                "avg_volume": 700000,
                "rejection_reason": "below_min_relative_volume",
            },
            {
                "symbol": "NOK",
                "accepted": False,
                "timestamp": "2026-06-12T14:33:00+00:00",
                "price": 4.00,
                "gain_pct": 6.0,
                "rel_volume": 0.55,
                "spread_pct": 0.05,
                "avg_volume": 900000,
                "rejection_reason": "below_min_relative_volume",
            },
            {
                "symbol": "INTC",
                "accepted": False,
                "timestamp": "2026-06-12T14:34:00+00:00",
                "price": 30.00,
                "gain_pct": 4.0,
                "rel_volume": 0.42,
                "spread_pct": 0.04,
                "avg_volume": 1200000,
                "rejection_reason": "below_min_relative_volume",
            },
            {
                "symbol": "BADQ",
                "accepted": False,
                "timestamp": "2026-06-12T14:35:00+00:00",
                "price": 9.00,
                "gain_pct": 12.0,
                "rel_volume": 0.90,
                "spread_pct": 0.20,
                "avg_volume": 500000,
                "quote_stable": False,
                "rejection_reason": "unstable_quote",
            },
            {
                "symbol": "GOOD",
                "accepted": True,
                "timestamp": "2026-06-12T14:36:00+00:00",
                "price": 25.00,
                "gain_pct": 10.0,
                "rel_volume": 1.2,
                "spread_pct": 0.10,
                "avg_volume": 800000,
                "rejection_reason": None,
            },
            {
                "symbol": "ALGN",
                "accepted": False,
                "timestamp": "2026-06-12T14:39:00+00:00",
                "price": 40.00,
                "gain_pct": 9.0,
                "rel_volume": 1.50,
                "spread_pct": 0.10,
                "avg_volume": 900000,
                "rejection_reason": "entry_alignment",
            },
        ],
    }
    (history_dir / "20260612T143000000000Z_default.json").write_text(json.dumps(payload), encoding="utf-8")
    (reports_dir / "dynamic_rejections_2026-06-12_paper_bot.json").write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "symbol": "RKLZ",
                        "timestamp": "2026-06-12T14:37:00+00:00",
                        "price": 6.00,
                        "gain_pct": 18.0,
                        "rel_volume": 1.10,
                        "spread_pct": 0.15,
                        "avg_volume": 400000,
                        "rejection_reason": "relative_volume 1.10 <= 1.80",
                    },
                    {
                        "symbol": "AAL",
                        "timestamp": "2026-06-12T14:38:00+00:00",
                        "price": 11.00,
                        "gain_pct": 7.0,
                        "rel_volume": 0.70,
                        "spread_pct": 0.18,
                        "avg_volume": 600000,
                        "quote_stable": False,
                        "rejection_reason": "below_min_relative_volume",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    _write_bars(
        bars_dir,
        "ASTN",
        [
            ("2026-06-12T14:45:00+00:00", 10.50),
            ("2026-06-12T15:00:00+00:00", 10.30),
            ("2026-06-12T15:30:00+00:00", 11.00),
            ("2026-06-12T19:59:00+00:00", 10.20),
        ],
    )
    _write_bars(
        bars_dir,
        "AXTX",
        [
            ("2026-06-12T14:46:00+00:00", 19.00),
            ("2026-06-12T15:01:00+00:00", 18.00),
            ("2026-06-12T15:31:00+00:00", 18.50),
            ("2026-06-12T19:59:00+00:00", 18.00),
        ],
    )
    _write_bars(
        bars_dir,
        "RZLV",
        [
            ("2026-06-12T14:47:00+00:00", 4.90),
            ("2026-06-12T15:02:00+00:00", 4.80),
            ("2026-06-12T15:32:00+00:00", 4.75),
            ("2026-06-12T19:59:00+00:00", 4.60),
        ],
    )
    _write_bars(
        bars_dir,
        "RKLZ",
        [
            ("2026-06-12T14:52:00+00:00", 6.30),
            ("2026-06-12T15:07:00+00:00", 6.60),
            ("2026-06-12T15:37:00+00:00", 6.90),
            ("2026-06-12T19:59:00+00:00", 7.20),
        ],
    )
    _write_bars(
        bars_dir,
        "ALGN",
        [
            ("2026-06-12T14:54:00+00:00", 41.00),
            ("2026-06-12T15:09:00+00:00", 42.00),
            ("2026-06-12T15:39:00+00:00", 43.00),
            ("2026-06-12T19:59:00+00:00", 44.00),
        ],
    )
    return data_dir, bars_dir


def test_dynamic_rvol_forward_returns_buckets_thresholds_and_focus_symbols(tmp_path: Path) -> None:
    data_dir, bars_dir = _write_fixture(tmp_path)

    report = build_dynamic_rvol_forward_returns_report(
        data_dir=data_dir,
        day="2026-06-12",
        user_id="paper_bot",
        bars_dir=bars_dir,
    )

    assert report["research_only"] is True
    assert report["summary"]["rvol_rejections_considered"] == 6
    assert report["summary"]["unique_symbols_considered"] == 6
    assert report["summary"]["missing_forward_return_rows"] == 2
    assert report["summary"]["excluded_other_gate_blockers"] == {"unstable_quote": 1}
    assert report["summary"]["rejection_reason_counts"]["entry_alignment"] == 1

    buckets = report["bucket_analysis"]
    assert buckets["0.50-0.75"]["candidate_count"] == 2
    assert buckets["0.75-1.00"]["candidate_count"] == 2
    assert buckets["1.00+"]["candidate_count"] == 1
    assert buckets["0.75-1.00"]["average_rvol"] == pytest.approx(0.865)
    assert buckets["0.75-1.00"]["forward_returns"]["eod"]["average_return_pct"] == pytest.approx(-3.0)
    assert buckets["1.00+"]["forward_returns"]["eod"]["win_rate"] == pytest.approx(1.0)

    thresholds = report["threshold_analysis"]
    assert thresholds["current_100"]["additional_candidates_admitted"] == 1
    assert thresholds["relaxed_075"]["additional_candidates_admitted"] == 3
    assert thresholds["relaxed_050"]["additional_candidates_admitted"] == 5
    assert thresholds["relaxed_050"]["forward_returns"]["eod"]["average_return_pct"] == pytest.approx(1.0)
    assert thresholds["relaxed_050"]["forward_returns"]["eod"]["win_rate"] == pytest.approx(0.5)

    per_symbol = report["per_symbol_analysis"]
    assert per_symbol["ASTN"]["candidate_count"] == 1
    assert per_symbol["ASTN"]["forward_returns"]["15m"]["average_return_pct"] == pytest.approx(5.0)
    assert per_symbol["AXTX"]["forward_returns"]["eod"]["average_return_pct"] == pytest.approx(-10.0)
    assert per_symbol["NOK"]["forward_returns"]["eod"]["available"] == 0
    assert per_symbol["AAL"]["candidate_count"] == 0

    alignment = report["entry_alignment_forward_returns"]
    assert alignment["candidate_count"] == 1
    assert alignment["unique_symbol_count"] == 1
    assert alignment["symbols"] == ["ALGN"]
    assert alignment["forward_returns"]["eod"]["average_return_pct"] == pytest.approx(10.0)


def test_dynamic_rvol_forward_returns_writes_artifacts_and_cli(tmp_path: Path) -> None:
    data_dir, bars_dir = _write_fixture(tmp_path)

    json_path, text_path, report = write_dynamic_rvol_forward_returns_report(
        data_dir=data_dir,
        day="latest",
        user_id="paper_bot",
        bars_dir=bars_dir,
    )

    assert json_path == data_dir / "research" / "dynamic_rvol_forward_returns" / "2026-06-12_paper_bot.json"
    assert text_path == data_dir / "research" / "dynamic_rvol_forward_returns" / "2026-06-12_paper_bot.txt"
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved["report"] == "dynamic_rvol_forward_returns"
    text = text_path.read_text(encoding="utf-8")
    assert "Dynamic RVOL Forward Returns - 2026-06-12 user=paper_bot" in text
    assert "0.75-1.00" in text
    assert "Entry Alignment Rejects" in text
    assert "Focus Symbols" in render_dynamic_rvol_forward_returns_report(report)

    proc = subprocess.run(
        [
            str(PROJECT_ROOT / "bin" / "algo"),
            "dynamic-rvol-forward-returns",
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
    assert "Dynamic RVOL Forward Returns - 2026-06-12 user=paper_bot" in proc.stdout
    assert "JSON:" in proc.stdout
