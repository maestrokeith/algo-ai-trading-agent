from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import replay_market_session as session_replay
from tests.test_replay_live_cycle import _write_replay_project


def test_market_session_replay_requires_broker_mock(tmp_path: Path) -> None:
    _write_replay_project(tmp_path)

    with pytest.raises(RuntimeError, match="broker_mock_required"):
        session_replay.run_market_session_replay(
            project_root=tmp_path,
            day="2026-06-05",
            user="live_bot",
            broker_mock=False,
            max_ticks=1,
        )


def test_market_session_replay_blocks_live_mode_without_mock(tmp_path: Path) -> None:
    _write_replay_project(tmp_path)

    with pytest.raises(RuntimeError, match="unsafe_live_broker_mode"):
        session_replay.run_market_session_replay(
            project_root=tmp_path,
            day="2026-06-05",
            user="live_bot",
            broker_mock=False,
            broker_mode="LIVE",
            max_ticks=1,
        )


def test_market_session_replay_outputs_session_summary_json(tmp_path: Path) -> None:
    _write_replay_project(tmp_path)

    summary = session_replay.run_market_session_replay(
        project_root=tmp_path,
        day="2026-06-05",
        user="live_bot",
        broker_mock=True,
        max_ticks=2,
    )

    assert summary["mode"] == "market_session_replay"
    assert summary["broker_mock"] is True
    assert summary["clock"]["tick_count"] == 2
    assert summary["clock"]["cycles_with_data"] == 2
    assert summary["mock_orders"]
    assert summary["selected_candidates"]
    assert "route_level_pnl_estimate" in summary
    assert "churn_same_day_reversal_stats" in summary
    assert summary["churn_same_day_reversal_stats"]["repeated_buy_symbols"] == ["XOS"]
    assert "core_rebuild_logs" in summary
    assert "dynamic_high_conviction_logs" in summary

    path = tmp_path / summary["summary_path"]
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["mock_orders"][0]["symbol"] == "XOS"
    assert saved["historical_artifacts"]["dynamic_scan_history"]


def test_market_session_replay_latest_resolves_newest_snapshot_date(tmp_path: Path) -> None:
    _write_replay_project(tmp_path)

    summary = session_replay.run_market_session_replay(
        project_root=tmp_path,
        day="latest",
        user="live_bot",
        broker_mock=True,
        max_ticks=1,
    )

    assert summary["date"] == "2026-06-05"
    assert summary["clock"]["cycles_with_data"] == 1


def test_market_session_replay_cadence_starts_at_open() -> None:
    config = {
        "timing": {"exit_interval_min": 15, "entry_interval_min": 10},
        "dynamic_universe": {},
    }

    ticks = session_replay.build_session_ticks(day="2026-06-05", config=config, max_ticks=3)

    assert [tick.astimezone(session_replay.ET).strftime("%H:%M") for tick in ticks] == [
        "09:30",
        "09:31",
        "09:32",
    ]
