from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.trend_prefilter_research import (
    build_trend_prefilter_research_report,
    render_trend_prefilter_research_report,
    write_trend_prefilter_research_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    data_dir = tmp_path / "data"
    bars_dir = data_dir / "historical_bars"
    bars_dir.mkdir(parents=True)
    log_path = tmp_path / "algo_2026-06-12.log"
    log_path.write_text(
        "\n".join(
            [
                "Jun 12 09:44:39 host python[1]: SKIP AAPL: reason=below MAs (trend prefilter); no news override or alternate entry (breakout / mean reversion / vol)",
                "Jun 12 09:48:11 host python[1]: SKIP AAPL: reason=below MAs (trend prefilter); no news override or alternate entry (breakout / mean reversion / vol)",
                "Jun 12 10:05:00 host python[1]: AAPL ENTRY_EVAL route=trend_long trend=T pullback=T momentum=T vol=T regime=T spread=T pos=T cooldown=T final=T reason=ok",
                "Jun 12 09:44:46 host python[1]: SKIP SNOW: reason=below MAs (trend prefilter); no news override or alternate entry (breakout / mean reversion / vol)",
                "Jun 12 09:50:10 host python[1]: SKIP SNOW: reason=below MAs (trend prefilter); no news override or alternate entry (breakout / mean reversion / vol)",
                "Jun 12 10:45:12 host python[1]: SKIP SNOW: reason=below MAs (trend prefilter); no news override or alternate entry (breakout / mean reversion / vol)",
            ]
        ),
        encoding="utf-8",
    )
    (bars_dir / "AAPL_2026-06-12_1Min.csv").write_text(
        "\n".join(
            [
                "timestamp,high,close",
                "2026-06-12T13:44:39+00:00,100.20,100.00",
                "2026-06-12T14:00:00+00:00,102.00,101.00",
                "2026-06-12T19:59:00+00:00,103.00,102.50",
            ]
        ),
        encoding="utf-8",
    )
    (bars_dir / "SNOW_20260612.json").write_text(
        json.dumps(
            {
                "bars": [
                    {"timestamp": "2026-06-12T13:44:46+00:00", "high": 80.5, "close": 80.0},
                    {"timestamp": "2026-06-12T14:30:00+00:00", "high": 81.0, "close": 79.0},
                    {"timestamp": "2026-06-12T19:59:00+00:00", "high": 79.5, "close": 78.0},
                ]
            }
        ),
        encoding="utf-8",
    )
    return data_dir, bars_dir, log_path


def test_trend_prefilter_research_computes_outcomes(tmp_path: Path) -> None:
    data_dir, bars_dir, log_path = _write_fixture(tmp_path)

    report = build_trend_prefilter_research_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-12",
        user_id="live_bot",
        log_paths=[log_path],
        bars_dir=bars_dir,
    )
    by_symbol = {row["symbol"]: row for row in report["symbols"]}

    assert report["summary"]["symbols"] == 2
    assert report["summary"]["total_rejections"] == 5
    assert report["summary"]["later_became_eligible"] == 1
    assert report["summary"]["stayed_blocked_all_day"] == 1
    assert report["summary"]["profitable_to_close_if_relaxed"] == 1
    assert by_symbol["AAPL"]["first_rejection_time"] == "2026-06-12T09:44:39-04:00"
    assert by_symbol["AAPL"]["rejection_count"] == 2
    assert by_symbol["AAPL"]["later_became_eligible"] is True
    assert by_symbol["AAPL"]["price_change_to_close_pct"] == pytest.approx(2.5)
    assert by_symbol["AAPL"]["max_gain_after_first_rejection_pct"] == pytest.approx(3.0)
    assert by_symbol["AAPL"]["would_have_been_profitable_to_close"] is True
    assert by_symbol["SNOW"]["rejection_count"] == 3
    assert by_symbol["SNOW"]["later_became_eligible"] is False
    assert by_symbol["SNOW"]["price_change_to_close_pct"] == pytest.approx(-2.5)
    assert by_symbol["SNOW"]["would_have_been_profitable_to_close"] is False


def test_trend_prefilter_research_writes_json_text_and_cli(tmp_path: Path) -> None:
    data_dir, bars_dir, log_path = _write_fixture(tmp_path)

    json_path, txt_path, report = write_trend_prefilter_research_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-12",
        user_id="live_bot",
        log_paths=[log_path],
        bars_dir=bars_dir,
    )

    assert json_path == data_dir / "research" / "trend_prefilter_research" / "2026-06-12_live_bot.json"
    assert txt_path == data_dir / "research" / "trend_prefilter_research" / "2026-06-12_live_bot.txt"
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved["summary"]["outcomes_available"] == 2
    text = txt_path.read_text(encoding="utf-8")
    assert "Trend Prefilter Research - 2026-06-12 user=live_bot" in text
    assert "AAPL" in render_trend_prefilter_research_report(report)

    proc = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_trend_prefilter_research.py"),
            "--date",
            "2026-06-12",
            "--user",
            "live_bot",
            "--data-dir",
            str(data_dir),
            "--project-root",
            str(tmp_path),
            "--bars-dir",
            str(bars_dir),
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
    assert "Trend Prefilter Research - 2026-06-12 user=live_bot" in proc.stdout
    assert "JSON:" in proc.stdout
