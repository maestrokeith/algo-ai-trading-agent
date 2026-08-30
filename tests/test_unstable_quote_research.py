from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.unstable_quote_research import (
    build_unstable_quote_research_report,
    render_unstable_quote_research_report,
    write_unstable_quote_research_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_scan(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": "2026-06-15T13:38:00+00:00",
        "user_id": "live_bot",
        "candidates": [
            {
                "symbol": "CAST",
                "timestamp": "2026-06-15T13:38:00+00:00",
                "accepted": False,
                "rejection_reason": "unstable quote",
                "price": 4.00,
                "bid": 3.70,
                "ask": 4.30,
                "spread_pct": 15.0,
                "quote_age_seconds": 1.2,
                "gain_pct": 80.0,
                "rel_volume": 2.5,
            },
            {
                "symbol": "CAST",
                "timestamp": "2026-06-15T13:45:00+00:00",
                "accepted": False,
                "rejection_reason": "unstable_quote",
                "price": 5.00,
                "bid": 4.50,
                "ask": 5.50,
                "spread_pct": 20.0,
                "quote_age_seconds": 2.0,
                "gain_pct": 100.0,
                "rel_volume": 2.8,
            },
            {
                "symbol": "NVDA",
                "timestamp": "2026-06-15T13:50:00+00:00",
                "accepted": False,
                "rejection_reason": "below_min_relative_volume",
                "price": 140.0,
                "spread_pct": 0.2,
                "quote_age_seconds": 0.5,
                "gain_pct": 1.0,
                "rel_volume": 0.4,
            },
            {
                "symbol": "LATE",
                "timestamp": "2026-06-15T14:05:00+00:00",
                "accepted": False,
                "rejection_reason": "unstable quote",
                "price": 2.0,
                "spread_pct": 25.0,
            },
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_bars(data_dir: Path) -> None:
    bars_dir = data_dir / "bars"
    bars_dir.mkdir(parents=True, exist_ok=True)
    (bars_dir / "CAST.json").write_text(
        json.dumps(
            {
                "bars": [
                    {"timestamp": "2026-06-15T13:53:00+00:00", "close": 4.40},
                    {"timestamp": "2026-06-15T14:08:00+00:00", "close": 4.80},
                    {"timestamp": "2026-06-15T14:38:00+00:00", "close": 5.20},
                    {"timestamp": "2026-06-15T20:00:00+00:00", "close": 5.00},
                ]
            }
        ),
        encoding="utf-8",
    )


def test_unstable_quote_report_first_30m_with_forward_returns(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_scan(data_dir / "dynamic_scan_history" / "20260615T133800000000Z_live_bot.json")
    _write_bars(data_dir)

    report = build_unstable_quote_research_report(data_dir=data_dir, day="2026-06-15", user_id="live_bot")

    assert report["summary"]["total_candidates"] == 3
    assert report["summary"]["unstable_quote_rejections"] == 2
    assert report["summary"]["unstable_quote_rejection_rate"] == pytest.approx(0.6667)
    assert report["summary"]["bid_ask_rows"] == 2
    assert report["summary"]["quote_age_rows"] == 2
    assert report["summary"]["forward_return_rows"] == 2
    assert report["distributions"]["unstable_quote_age_seconds"]["median"] == pytest.approx(1.6)
    assert report["distributions"]["unstable_spread_pct"]["median"] == pytest.approx(17.5)
    assert report["stable_vs_unstable"]["stable"]["count"] == 1
    assert report["stable_vs_unstable"]["unstable"]["count"] == 2
    assert report["stable_vs_unstable"]["stable"]["spread_pct_distribution"]["median"] == pytest.approx(0.2)
    cast = report["by_symbol"]["CAST"]
    assert cast["average_spread_pct"] == pytest.approx(17.5)
    assert cast["quote_variance"]["spread_pct_variance"] == pytest.approx(6.25)
    assert cast["quote_variance"]["price_variance"] == pytest.approx(0.25)
    assert cast["hypothetical_trades"][0]["return_15m_pct"] == pytest.approx(10.0)
    rendered = render_unstable_quote_research_report(report)
    assert "Unstable Quote Rejection Report - 2026-06-15" in rendered
    assert "Stable vs Unstable" in rendered
    assert "Quote Distributions" in rendered


def test_write_unstable_quote_report_outputs_json_and_markdown(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_scan(data_dir / "dynamic_scan_history" / "20260615T133800000000Z_live_bot.json")

    json_path, text_path, report = write_unstable_quote_research_report(
        data_dir=data_dir,
        day="2026-06-15",
        user_id="live_bot",
    )

    assert json_path == data_dir / "research" / "unstable_quote_research" / "2026-06-15_live_bot.json"
    assert text_path == data_dir / "research" / "unstable_quote_research" / "2026-06-15_live_bot.md"
    assert json.loads(json_path.read_text(encoding="utf-8"))["report"] == "unstable_quote_research"
    assert "No trading behavior changes" in text_path.read_text(encoding="utf-8")
    assert report["summary"]["forward_return_rows"] == 0


def test_generate_unstable_quote_report_cli(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_scan(data_dir / "dynamic_scan_history" / "20260615T133800000000Z_live_bot.json")

    proc = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_unstable_quote_report.py"),
            "--date",
            "2026-06-15",
            "--user",
            "live_bot",
            "--data-dir",
            str(data_dir),
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0
    assert "Unstable Quote Rejection Report - 2026-06-15" in proc.stdout
    assert (data_dir / "research" / "unstable_quote_research" / "2026-06-15_live_bot.json").exists()
