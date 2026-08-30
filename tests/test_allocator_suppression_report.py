from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import pandas as pd

from src.allocator_suppression_report import (
    build_allocator_suppression_report,
    render_allocator_suppression_report,
    write_allocator_suppression_report,
)
from src.dynamic_universe import persist_allocator_candidate_bar_snapshot

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    data_dir = tmp_path / "data"
    bars_dir = data_dir / "research" / "allocator_candidate_bars" / "2026-06-12" / "live_bot"
    bars_dir.mkdir(parents=True)
    log_path = tmp_path / "algo_2026-06-12.log"
    log_path.write_text(
        "\n".join(
            [
                "Jun 12 10:00:00 host python[1]: XLF ENTRY_EVAL route=trend_long price=40.00 final=T reason=ok",
                "Jun 12 10:00:01 host python[1]: ALLOCATOR_INPUT count=1 symbols=XLF scores=1.45",
                "Jun 12 10:00:02 host python[1]: POST_PLANNER_ACTION_TRACE before_actions=buy:XLF:1312.50 after_actions=none",
                "Jun 12 10:05:00 host python[1]: JPM ENTRY_EVAL route=trend_long price=100.00 final=T reason=ok",
                "Jun 12 10:05:01 host python[1]: ALLOCATOR_SKIP symbol=JPM reason=stop_loss_cooldown",
                "Jun 12 10:06:00 host python[1]: XLE ENTRY_EVAL route=trend_long price=80.00 final=T reason=ok",
                "Jun 12 10:06:01 host python[1]: ORDER_SKIP symbol=XLE reason=add-ons today 1 >= max 1",
                "Jun 12 10:07:00 host python[1]: IWM ENTRY_EVAL route=trend_long price=200.00 final=T reason=ok",
                "Jun 12 10:07:01 host python[1]: ALLOCATOR_REJECT symbol=IWM reason=correlation",
                "Jun 12 10:08:00 host python[1]: QQQ ENTRY_EVAL route=trend_long price=500.00 final=T reason=ok",
                "Jun 12 10:08:01 host python[1]: ALLOCATOR_NO_ACTION_DETAIL symbol=QQQ reason=allocator_returned_no_actions detail=minimum_cash_to_deploy",
                "Jun 12 10:09:00 host python[1]: SPY ENTRY_EVAL route=trend_long price=600.00 final=T reason=ok",
                "Jun 12 10:09:01 host python[1]: ALLOCATOR_ACTION_BLOCKED symbol=SPY reason=risk_exposure_cap",
            ]
        ),
        encoding="utf-8",
    )
    (bars_dir / "XLF.json").write_text(
        json.dumps(
            {
                "symbol": "XLF",
                "bars": [
                    {"timestamp": "2026-06-12T14:00:00+00:00", "open": 40, "high": 40, "low": 40, "close": 40},
                    {"timestamp": "2026-06-12T14:15:00+00:00", "open": 40, "high": 42, "low": 40, "close": 41},
                    {"timestamp": "2026-06-12T14:30:00+00:00", "open": 41, "high": 43, "low": 41, "close": 42},
                    {"timestamp": "2026-06-12T15:00:00+00:00", "open": 42, "high": 44, "low": 42, "close": 43},
                    {"timestamp": "2026-06-12T15:01:00+00:00", "open": 43, "high": 43, "low": 43, "close": 43},
                ],
            }
        ),
        encoding="utf-8",
    )
    (bars_dir / "JPM.json").write_text(
        json.dumps(
            {
                "symbol": "JPM",
                "bars": [
                    {"timestamp": "2026-06-12T14:05:00+00:00", "open": 100, "high": 100, "low": 100, "close": 100},
                    {"timestamp": "2026-06-12T14:20:00+00:00", "open": 100, "high": 101, "low": 99, "close": 99},
                ],
            }
        ),
        encoding="utf-8",
    )
    return data_dir, bars_dir, log_path


def test_allocator_suppression_report_groups_blocks_and_outcomes(tmp_path: Path) -> None:
    data_dir, _bars_dir, log_path = _write_fixture(tmp_path)

    report = build_allocator_suppression_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-12",
        user_id="live_bot",
        log_paths=[log_path],
    )

    assert report["entry_eval_passed"]["count"] == 6
    assert "XLF" in report["allocator_selected"]["symbols"]
    assert report["summary"]["blocked_candidates"] == 6
    counts = report["summary"]["suppression_type_counts"]
    assert counts["post_planner_filter"] == 1
    assert counts["cooldown"] == 1
    assert counts["add_on_once_per_day"] == 1
    assert counts["correlation"] == 1
    assert counts["capital_limit"] == 1
    assert counts["risk_limit"] == 1

    by_symbol = {row["symbol"]: row for row in report["blocked_candidates"]}
    assert by_symbol["XLF"]["max_gain_after_suppression_pct"] == pytest.approx(10.0)
    assert by_symbol["XLF"]["max_drawdown_after_suppression_pct"] == pytest.approx(0.0)
    assert by_symbol["XLF"]["return_after_15m_pct"] == pytest.approx(5.0)
    assert by_symbol["XLF"]["return_after_30m_pct"] == pytest.approx(7.5)
    assert by_symbol["XLF"]["return_after_60m_pct"] == pytest.approx(7.5)
    assert by_symbol["XLF"]["subsequent_15m_return_pct"] == pytest.approx(5.0)
    assert by_symbol["XLF"]["subsequent_30m_return_pct"] == pytest.approx(7.5)
    assert by_symbol["XLF"]["subsequent_60m_return_pct"] == pytest.approx(7.5)
    assert by_symbol["XLF"]["time_to_max_gain_minutes"] == pytest.approx(59.9667)
    assert by_symbol["XLF"]["reached_plus_1pct"] is True
    assert by_symbol["XLF"]["reached_plus_2pct"] is True
    assert by_symbol["XLF"]["reached_plus_3pct"] is True
    assert by_symbol["JPM"]["suppression_type"] == "cooldown"
    assert by_symbol["JPM"]["max_gain_after_suppression_pct"] == pytest.approx(1.0)
    assert by_symbol["JPM"]["max_drawdown_after_suppression_pct"] == pytest.approx(-1.0)
    assert report["summary"]["symbols_with_bars"] == ["JPM", "XLF"]
    assert "XLE" in report["summary"]["symbols_missing_bars"]
    assert report["bar_diagnostics"]["XLF"]["found_file"].endswith("allocator_candidate_bars/2026-06-12/live_bot/XLF.json")
    assert report["average_return_by_suppression_type"]["post_planner_filter"]["missed_profitable_opportunities"] == 1
    post_planner = report["suppression_type_analysis"]["post_planner_filter"]
    assert post_planner["count"] == 1
    assert post_planner["outcomes_available"] == 1
    assert post_planner["avg_max_gain"] == pytest.approx(10.0)
    assert post_planner["median_max_gain"] == pytest.approx(10.0)
    assert post_planner["avg_drawdown"] == pytest.approx(0.0)
    assert post_planner["avg_15m_return"] == pytest.approx(5.0)
    assert post_planner["avg_30m_return"] == pytest.approx(7.5)
    assert post_planner["avg_60m_return"] == pytest.approx(7.5)
    assert post_planner["percent_positive_15m"] == pytest.approx(100.0)
    assert post_planner["percent_positive_30m"] == pytest.approx(100.0)
    assert post_planner["percent_positive_60m"] == pytest.approx(100.0)
    assert post_planner["percent_reached_1pct"] == pytest.approx(100.0)
    assert post_planner["percent_reached_2pct"] == pytest.approx(100.0)
    assert post_planner["percent_reached_3pct"] == pytest.approx(100.0)
    assert post_planner["effectiveness_score"] == "harmful"
    assert post_planner["recommendation"] == "likely too restrictive"
    assert report["recommendations"]["post_planner_filter"]["recommendation"] == "likely too restrictive"
    assert report["symbol_analysis"]["XLF"]["suppressions"] == 1
    assert report["symbol_analysis"]["XLF"]["average_outcome"]["avg_max_gain"] == pytest.approx(10.0)
    assert report["symbol_analysis"]["XLF"]["best_outcome"]["max_gain_after_suppression_pct"] == pytest.approx(10.0)
    assert report["symbol_analysis"]["QQQ"]["suppressions"] == 1
    assert report["symbol_analysis"]["XLY"]["suppressions"] == 0
    assert report["top_suppressed_winners"][0]["symbol"] == "XLF"
    rendered = render_allocator_suppression_report(report)
    assert "Allocator Suppression Report - 2026-06-12 user=live_bot" in rendered
    assert "Suppression Effectiveness" in rendered
    assert "Symbol-Level Analysis" in rendered


def test_allocator_suppression_report_writes_artifacts_and_cli(tmp_path: Path) -> None:
    data_dir, bars_dir, log_path = _write_fixture(tmp_path)

    json_path, text_path, report = write_allocator_suppression_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-12",
        user_id="live_bot",
        log_paths=[log_path],
    )

    assert json_path == data_dir / "research" / "allocator_suppression" / "2026-06-12_live_bot.json"
    assert text_path == data_dir / "research" / "allocator_suppression" / "2026-06-12_live_bot.txt"
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved["research_only"] is True
    assert report["summary"]["outcomes_available"] >= 2

    proc = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_allocator_suppression_report.py"),
            "--date",
            "2026-06-12",
            "--user",
            "live_bot",
            "--project-root",
            str(tmp_path),
            "--data-dir",
            str(data_dir),
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
    assert "Allocator Suppression Report - 2026-06-12 user=live_bot" in proc.stdout
    assert "JSON:" in proc.stdout


def test_allocator_candidate_bar_snapshot_is_written_and_consumed(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    log_path = tmp_path / "algo_2026-06-12.log"
    log_path.write_text(
        "\n".join(
            [
                "Jun 12 10:00:00 host python[1]: XLF ENTRY_EVAL route=trend_long price=40.00 final=T reason=ok",
                "Jun 12 10:00:01 host python[1]: POST_PLANNER_ACTION_TRACE before_actions=buy:XLF:1312.50 after_actions=none",
            ]
        ),
        encoding="utf-8",
    )
    bars = pd.DataFrame(
        {
            "open": [40.0, 41.0],
            "high": [40.0, 42.0],
            "low": [40.0, 41.0],
            "close": [40.0, 42.0],
            "volume": [1000, 1200],
        },
        index=pd.to_datetime(["2026-06-12T14:00:00Z", "2026-06-12T14:20:00Z"], utc=True),
    )

    path = persist_allocator_candidate_bar_snapshot(
        symbol="XLF",
        user_id="live_bot",
        bars=bars,
        timeframe="1Min",
        project_root=tmp_path,
        now=pd.Timestamp("2026-06-12T10:00:00-04:00").to_pydatetime(),
        source="entry_eval_pass",
        route="trend_long",
    )

    assert path == data_dir / "research" / "allocator_candidate_bars" / "2026-06-12" / "live_bot" / "XLF.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["source"] == "entry_eval_pass"
    assert payload["route"] == "trend_long"
    assert payload["bars"][1]["close"] == pytest.approx(42.0)

    report = build_allocator_suppression_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-12",
        user_id="live_bot",
        log_paths=[log_path],
    )

    row = report["blocked_candidates"][0]
    assert row["symbol"] == "XLF"
    assert row["outcome_available"] is True
    assert row["max_gain_after_suppression_pct"] == pytest.approx(5.0)
    assert row["max_drawdown_after_suppression_pct"] == pytest.approx(0.0)
    assert row["return_after_15m_pct"] == pytest.approx(5.0)
    assert row["reached_plus_1pct"] is True
    assert report["summary"]["symbols_with_bars"] == ["XLF"]
