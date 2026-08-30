from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.dynamic_candidate_blockers import (
    build_dynamic_candidate_blockers_report,
    render_dynamic_candidate_blockers_report,
    write_dynamic_candidate_blockers_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    data_dir = tmp_path / "data"
    history = data_dir / "dynamic_scan_history"
    bars = data_dir / "historical_bars"
    history.mkdir(parents=True)
    bars.mkdir(parents=True)
    (history / "20260612T150000000000Z_live_bot.json").write_text(
        json.dumps(
            {
                "user_id": "live_bot",
                "generated_at": "2026-06-12T13:40:00+00:00",
                "selected": ["WBI", "DSY"],
                "candidates": [
                    {"symbol": "WBI", "accepted": True, "score": 25.76, "price": 10.0},
                    {"symbol": "DSY", "accepted": True, "score": 98.21, "price": 12.0},
                    {"symbol": "VRA", "accepted": False, "score": 12.0, "price": 4.0},
                ],
            }
        ),
        encoding="utf-8",
    )
    log_path = tmp_path / "algo_2026-06-12.log"
    log_path.write_text(
        "\n".join(
            [
                "Jun 12 10:55:07 host python[1]: DYNAMIC_SELECTED symbol=WBI score=25.76 news_score=0",
                "Jun 12 10:57:10 host python[1]: SKIP WBI: reason=not enough bars (got 186, need 200)",
                "Jun 12 10:08:46 host python[1]: DYNAMIC_SCAN DSY: price=12.00 gain=30.19 rel=2.84 spread=1.18%",
                "Jun 12 10:08:46 host python[1]: DYNAMIC_SCAN reject DSY: entry_alignment: need 5m breakout OR new intraday high OR strong green 1m OR opening-range breakout (got breakout=False nh=False green=False orb=False)",
                "Jun 12 10:09:00 host python[1]: DYNAMIC_SCAN reject VRA: entry_alignment: need 5m breakout OR new intraday high OR strong green 1m OR opening-range breakout (got breakout=False nh=False green=False orb=False)",
            ]
        ),
        encoding="utf-8",
    )
    (bars / "DSY_2026-06-12_1Min.csv").write_text(
        "\n".join(
            [
                "timestamp,open,high,low,close,volume",
                "2026-06-12T14:08:46+00:00,12.0,12.1,11.9,12.0,1000",
                "2026-06-12T15:00:00+00:00,12.5,13.2,12.4,13.0,1000",
                "2026-06-12T19:59:00+00:00,12.7,12.8,12.5,12.6,1000",
            ]
        ),
        encoding="utf-8",
    )
    return data_dir, bars, log_path


def test_dynamic_candidate_blockers_reports_selected_blockers_and_outcomes(tmp_path: Path) -> None:
    data_dir, bars_dir, log_path = _write_fixture(tmp_path)

    report = build_dynamic_candidate_blockers_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-12",
        user_id="live_bot",
        log_paths=[log_path],
        bars_dir=bars_dir,
    )
    by_symbol = {row["symbol"]: row for row in report["candidates"]}

    assert set(by_symbol) == {"DSY", "WBI"}
    assert report["summary"]["blocked_selected_candidates"] == 2
    assert report["summary"]["short_history_need_200"] == 1
    assert report["summary"]["entry_alignment_breakout_new_high_orb"] == 1
    assert by_symbol["DSY"]["scanner_score"] == pytest.approx(98.21)
    assert by_symbol["DSY"]["subsequent_intraday_return_pct"] == pytest.approx(5.0)
    assert by_symbol["DSY"]["max_gain_after_rejection_pct"] == pytest.approx(10.0)
    assert by_symbol["DSY"]["prevented_profitable_trade"] is True
    assert by_symbol["WBI"]["scanner_score"] == pytest.approx(25.76)
    assert by_symbol["WBI"]["missing_reason"] == "no_matching_bar_files"
    assert by_symbol["WBI"]["missing_bar_reason"] == "no_matching_bar_files"
    assert by_symbol["WBI"]["bar_diagnostics"]["searched_roots"] == [str(bars_dir)]
    assert by_symbol["DSY"]["bar_diagnostics"]["found_file"].endswith("DSY_2026-06-12_1Min.csv")
    assert report["summary"]["symbols_with_bars"] == ["DSY"]
    assert report["summary"]["symbols_missing_bars"] == ["WBI"]
    assert "VRA" not in by_symbol
    assert "Dynamic Candidate Blockers - 2026-06-12 user=live_bot" in render_dynamic_candidate_blockers_report(report)


def test_dynamic_candidate_blockers_writes_artifacts_and_cli(tmp_path: Path) -> None:
    data_dir, bars_dir, log_path = _write_fixture(tmp_path)

    json_path, text_path, report = write_dynamic_candidate_blockers_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-12",
        user_id="live_bot",
        log_paths=[log_path],
        bars_dir=bars_dir,
    )

    assert json_path == data_dir / "research" / "dynamic_candidate_blockers" / "2026-06-12_live_bot.json"
    assert text_path == data_dir / "research" / "dynamic_candidate_blockers" / "2026-06-12_live_bot.txt"
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved["summary"]["blocked_selected_candidates"] == 2
    assert "DSY" in text_path.read_text(encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_dynamic_candidate_blockers.py"),
            "--date",
            "2026-06-12",
            "--user",
            "live_bot",
            "--data-dir",
            str(data_dir),
            "--project-root",
            str(tmp_path),
            "--log-path",
            str(log_path),
            "--bars-dir",
            str(bars_dir),
            "--no-journal",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "Dynamic Candidate Blockers - 2026-06-12 user=live_bot" in proc.stdout
    assert "JSON:" in proc.stdout
    assert report["research_only"] is True


def test_dynamic_candidate_blockers_uses_replay_json_bar_fallback(tmp_path: Path) -> None:
    data_dir, bars_dir, log_path = _write_fixture(tmp_path)
    for path in bars_dir.glob("*"):
        path.unlink()
    replay_path = data_dir / "replay" / "2026-06-12_live_bot.json"
    replay_path.parent.mkdir(parents=True)
    replay_path.write_text(
        json.dumps(
            {
                "historical_artifacts": {
                    "bars": [
                        {
                            "symbol": "DSY",
                            "timestamp": "2026-06-12T14:08:46+00:00",
                            "open": 12.0,
                            "high": 12.1,
                            "low": 11.9,
                            "close": 12.0,
                        },
                        {
                            "symbol": "DSY",
                            "timestamp": "2026-06-12T19:59:00+00:00",
                            "open": 12.2,
                            "high": 13.8,
                            "low": 12.1,
                            "close": 13.2,
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    report = build_dynamic_candidate_blockers_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-12",
        user_id="live_bot",
        log_paths=[log_path],
    )
    by_symbol = {row["symbol"]: row for row in report["candidates"]}

    assert by_symbol["DSY"]["outcome_available"] is True
    assert by_symbol["DSY"]["subsequent_intraday_return_pct"] == pytest.approx(10.0)
    assert by_symbol["DSY"]["max_gain_after_rejection_pct"] == pytest.approx(15.0)
    assert by_symbol["DSY"]["bar_diagnostics"]["found_file"] == str(replay_path)
    assert by_symbol["DSY"]["bar_diagnostics"]["found_format"] == "json_nested"


def test_dynamic_candidate_blockers_uses_dynamic_candidate_bar_snapshots(tmp_path: Path) -> None:
    data_dir, bars_dir, log_path = _write_fixture(tmp_path)
    for path in bars_dir.glob("*"):
        path.unlink()
    snapshot_path = (
        data_dir
        / "research"
        / "dynamic_candidate_bars"
        / "2026-06-12"
        / "live_bot"
        / "DSY.json"
    )
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_text(
        json.dumps(
            {
                "symbol": "DSY",
                "user": "live_bot",
                "captured_at": "2026-06-12T10:08:46-04:00",
                "source": "dynamic_selected",
                "timeframe": "1Min",
                "bars": [
                    {
                        "timestamp": "2026-06-12T14:08:46+00:00",
                        "open": 12.0,
                        "high": 12.1,
                        "low": 11.9,
                        "close": 12.0,
                        "volume": 1000,
                    },
                    {
                        "timestamp": "2026-06-12T19:59:00+00:00",
                        "open": 12.2,
                        "high": 13.8,
                        "low": 12.1,
                        "close": 13.2,
                        "volume": 2000,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    report = build_dynamic_candidate_blockers_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-12",
        user_id="live_bot",
        log_paths=[log_path],
    )
    by_symbol = {row["symbol"]: row for row in report["candidates"]}

    assert by_symbol["DSY"]["outcome_available"] is True
    assert by_symbol["DSY"]["subsequent_intraday_return_pct"] == pytest.approx(10.0)
    assert by_symbol["DSY"]["max_gain_after_rejection_pct"] == pytest.approx(15.0)
    assert by_symbol["DSY"]["bar_diagnostics"]["found_file"] == str(snapshot_path)
    assert by_symbol["DSY"]["bar_diagnostics"]["found_format"] == "json"
    assert str(data_dir / "research" / "dynamic_candidate_bars") in by_symbol["WBI"]["bar_diagnostics"][
        "searched_roots"
    ]


def test_dynamic_candidate_blockers_reports_astn_scalability_metrics(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    history = data_dir / "dynamic_scan_history"
    bars_dir = data_dir / "research" / "dynamic_candidate_bars" / "2026-06-12" / "live_bot"
    history.mkdir(parents=True)
    bars_dir.mkdir(parents=True)
    (history / "20260612T140000000000Z_live_bot.json").write_text(
        json.dumps(
            {
                "user_id": "live_bot",
                "generated_at": "2026-06-12T14:00:00+00:00",
                "selected": ["ASTN"],
                "candidates": [
                    {
                        "symbol": "ASTN",
                        "accepted": True,
                        "score": 77.7,
                        "price": 10.0,
                        "timestamp": "2026-06-12T14:00:00+00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    log_path = tmp_path / "algo_2026-06-12.log"
    log_path.write_text(
        "\n".join(
            [
                "Jun 12 10:00:00 host python[1]: DYNAMIC_SELECTED symbol=ASTN score=77.70 news_score=0",
                "Jun 12 10:00:00 host python[1]: DYNAMIC_SCAN ASTN: price=10.00 gain=21.66 rel=0.93 spread=0.37%",
                "Jun 12 10:00:00 host python[1]: SKIP ASTN: reason=not enough bars (got 186, need 200)",
                "Jun 12 10:10:00 host python[1]: DYNAMIC_SCAN ASTN: price=10.00 gain=22.10 rel=1.02 spread=0.35%",
                "Jun 12 10:10:00 host python[1]: DYNAMIC_SCAN reject ASTN: entry_alignment: need 5m breakout OR new intraday high OR strong green 1m OR opening-range breakout (got breakout=False nh=False green=False orb=False)",
            ]
        ),
        encoding="utf-8",
    )
    (bars_dir / "ASTN.json").write_text(
        json.dumps(
            {
                "symbol": "ASTN",
                "user": "live_bot",
                "captured_at": "2026-06-12T10:00:00-04:00",
                "source": "dynamic_selected",
                "timeframe": "1Min",
                "bars": [
                    {
                        "timestamp": "2026-06-12T14:00:00+00:00",
                        "open": 10.0,
                        "high": 10.0,
                        "low": 10.0,
                        "close": 10.0,
                        "volume": 1000,
                    },
                    {
                        "timestamp": "2026-06-12T14:05:00+00:00",
                        "open": 10.0,
                        "high": 10.7,
                        "low": 9.8,
                        "close": 10.5,
                        "volume": 1000,
                    },
                    {
                        "timestamp": "2026-06-12T14:10:00+00:00",
                        "open": 10.5,
                        "high": 10.4,
                        "low": 10.0,
                        "close": 10.2,
                        "volume": 1000,
                    },
                    {
                        "timestamp": "2026-06-12T14:15:00+00:00",
                        "open": 10.2,
                        "high": 10.3,
                        "low": 10.1,
                        "close": 10.3,
                        "volume": 1000,
                    },
                    {
                        "timestamp": "2026-06-12T14:30:00+00:00",
                        "open": 10.3,
                        "high": 10.0,
                        "low": 9.7,
                        "close": 9.9,
                        "volume": 1000,
                    },
                    {
                        "timestamp": "2026-06-12T15:00:00+00:00",
                        "open": 9.9,
                        "high": 10.2,
                        "low": 9.9,
                        "close": 10.1,
                        "volume": 1000,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    report = build_dynamic_candidate_blockers_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-12",
        user_id="live_bot",
        log_paths=[log_path],
    )

    astn_rows = [row for row in report["candidates"] if row["symbol"] == "ASTN"]
    assert len(astn_rows) == 2
    first, second = astn_rows
    assert first["blocker"] == "short_history_need_200"
    assert first["max_gain_after_block_pct"] == pytest.approx(7.0)
    assert first["time_to_max_gain_minutes"] == pytest.approx(5.0)
    assert first["max_drawdown_after_block_pct"] == pytest.approx(-3.0)
    assert first["return_after_5m_pct"] == pytest.approx(5.0)
    assert first["return_after_15m_pct"] == pytest.approx(3.0)
    assert first["return_after_30m_pct"] == pytest.approx(-1.0)
    assert first["return_after_60m_pct"] == pytest.approx(1.0)
    assert first["reached_plus_1pct_within_15m"] is True
    assert first["reached_plus_2pct_within_15m"] is True
    assert first["reached_plus_3pct_within_15m"] is True
    assert second["blocker"] == "entry_alignment_breakout_new_high_orb"
    assert second["max_gain_after_block_pct"] == pytest.approx(4.0)
    assert second["time_to_max_gain_minutes"] == pytest.approx(0.0)

    blocker_summary = report["blocker_summary"]
    assert blocker_summary["short_history_need_200"]["average_max_gain_pct"] == pytest.approx(7.0)
    assert blocker_summary["entry_alignment_breakout_new_high_orb"]["median_max_gain_pct"] == pytest.approx(4.0)
    assert blocker_summary["short_history_need_200"]["percent_reached_plus_3pct"] == pytest.approx(100.0)
    symbol_summary = report["symbol_summary"]["ASTN"]
    assert symbol_summary["occurrences"] == 2
    assert symbol_summary["average_max_gain_pct"] == pytest.approx(5.5)
    assert symbol_summary["average_final_return_pct"] == pytest.approx(1.0)
    assert symbol_summary["average_drawdown_pct"] == pytest.approx(-3.0)
    assert symbol_summary["best_outcome"]["max_gain_after_block_pct"] == pytest.approx(7.0)
    astn = report["astn_analysis"]
    assert astn["occurrences"] == 2
    assert astn["average_max_gain_pct"] == pytest.approx(5.5)
    assert astn["average_time_to_max_gain_minutes"] == pytest.approx(2.5)
    assert astn["percent_reached_plus_1pct"] == pytest.approx(100.0)
    assert "ASTN Analysis" in render_dynamic_candidate_blockers_report(report)
