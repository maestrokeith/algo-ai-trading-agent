from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.dynamic_history_gate_research import (
    build_dynamic_history_gate_research_report,
    render_dynamic_history_gate_research_report,
    write_dynamic_history_gate_research_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_history_fixture(data_dir: Path) -> None:
    history = data_dir / "dynamic_scan_history"
    history.mkdir(parents=True)
    (history / "20260615T141500000000Z_live_bot.json").write_text(
        json.dumps(
            {
                "user_id": "live_bot",
                "generated_at": "2026-06-15T14:15:00+00:00",
                "selected": ["BTQ"],
                "candidates": [
                    {
                        "symbol": "BTQ",
                        "accepted": True,
                        "score": 19.5,
                        "price": 10.0,
                        "timestamp": "2026-06-15T14:15:00+00:00",
                    },
                    {
                        "symbol": "NOPE",
                        "accepted": False,
                        "score": 7.0,
                        "price": 5.0,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


def _write_log_fixture(tmp_path: Path) -> Path:
    log_path = tmp_path / "algo_2026-06-15.log"
    log_path.write_text(
        "\n".join(
            [
                "Jun 15 10:15:00 host python[1]: DYNAMIC_SCAN accept BTQ",
                "Jun 15 10:15:01 host python[1]: DYNAMIC_SELECTED symbol=BTQ score=19.50 news_score=0",
                "Jun 15 10:16:00 host python[1]: SKIP BTQ: reason=not enough bars (got 180, need 200)",
                "Jun 15 10:17:00 host python[1]: RAND ENTRY_EVAL route=trend_long final=T reason=ok",
                "Jun 15 10:18:00 host python[1]: SKIP RAND: reason=not enough bars (got 190, need 200)",
            ]
        ),
        encoding="utf-8",
    )
    return log_path


def test_dynamic_history_gate_parses_not_enough_bars_and_thresholds(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_history_fixture(data_dir)
    log_path = _write_log_fixture(tmp_path)

    report = build_dynamic_history_gate_research_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-15",
        user_id="live_bot",
        log_paths=[log_path],
    )

    rows = report["history_blocked_candidates"]
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "BTQ"
    assert row["got_bars"] == 180
    assert row["required_bars"] == 200
    assert row["got_required_ratio"] == pytest.approx(0.9)
    assert row["selected"] is True
    assert row["accepted"] is True
    assert row["all_other_dynamic_gates_passed"] is True
    assert report["summary"]["selected_history_blocked_count"] == 1
    assert report["summary"]["history_blocked_symbols"] == ["BTQ"]
    assert report["threshold_comparison"]["200"]["symbols"] == []
    assert report["threshold_comparison"]["180"]["symbols"] == ["BTQ"]
    assert report["threshold_comparison"]["160"]["symbols"] == ["BTQ"]
    assert report["threshold_comparison"]["150"]["symbols"] == ["BTQ"]
    assert report["threshold_comparison"]["120"]["symbols"] == ["BTQ"]
    assert "RAND" not in report["summary"]["history_blocked_symbols"]


def test_dynamic_history_gate_missing_forward_bars_inconclusive(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_history_fixture(data_dir)
    log_path = _write_log_fixture(tmp_path)

    report = build_dynamic_history_gate_research_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-15",
        user_id="live_bot",
        log_paths=[log_path],
    )

    row = report["history_blocked_candidates"][0]
    assert row["forward_returns"]["forward_returns_available"] is False
    assert row["forward_returns"]["missing_forward_bar_reason"] in {
        "no_bar_roots_exist",
        "no_matching_bar_files",
    }
    rendered = render_dynamic_history_gate_research_report(report)
    assert "missing/inconclusive forward bars: 1" in rendered
    assert "No trading behavior" in rendered


def test_dynamic_history_gate_forward_returns_from_bar_file(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    bars_dir = data_dir / "research" / "dynamic_candidate_bars" / "2026-06-15" / "live_bot"
    bars_dir.mkdir(parents=True)
    _write_history_fixture(data_dir)
    log_path = _write_log_fixture(tmp_path)
    (bars_dir / "BTQ.json").write_text(
        json.dumps(
            {
                "symbol": "BTQ",
                "user": "live_bot",
                "captured_at": "2026-06-15T10:15:00-04:00",
                "bars": [
                    {"timestamp": "2026-06-15T14:15:00+00:00", "close": 10.0},
                    {"timestamp": "2026-06-15T14:21:00+00:00", "close": 10.5},
                    {"timestamp": "2026-06-15T14:26:00+00:00", "close": 10.2},
                    {"timestamp": "2026-06-15T14:36:00+00:00", "close": 11.0},
                    {"timestamp": "2026-06-15T14:46:00+00:00", "close": 9.5},
                ],
            }
        ),
        encoding="utf-8",
    )

    report = build_dynamic_history_gate_research_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-15",
        user_id="live_bot",
        log_paths=[log_path],
    )

    fwd = report["history_blocked_candidates"][0]["forward_returns"]
    assert fwd["forward_returns_available"] is True
    assert fwd["return_5m_pct"] == pytest.approx(5.0)
    assert fwd["return_10m_pct"] == pytest.approx(2.0)
    assert fwd["return_20m_pct"] == pytest.approx(10.0)
    assert fwd["return_30m_pct"] == pytest.approx(-5.0)
    assert report["summary"]["outcomes_available"] == 1


def test_dynamic_history_gate_writes_outputs_and_cli(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_history_fixture(data_dir)
    log_path = _write_log_fixture(tmp_path)

    json_path, md_path, report = write_dynamic_history_gate_research_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-15",
        user_id="live_bot",
        log_paths=[log_path],
    )

    assert json_path == data_dir / "research" / "dynamic_history_gate" / "2026-06-15_live_bot.json"
    assert md_path == data_dir / "research" / "dynamic_history_gate" / "2026-06-15_live_bot.md"
    assert json.loads(json_path.read_text(encoding="utf-8"))["report"] == "dynamic_history_gate_research"
    assert "BTQ" in md_path.read_text(encoding="utf-8")
    assert report["research_only"] is True

    proc = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_dynamic_history_gate_research.py"),
            "--date",
            "2026-06-15",
            "--user",
            "live_bot",
            "--data-dir",
            str(data_dir),
            "--project-root",
            str(tmp_path),
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
    assert "Dynamic History Gate Research - 2026-06-15 user=live_bot" in proc.stdout
    assert "JSON:" in proc.stdout
