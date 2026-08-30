from __future__ import annotations

from datetime import datetime, time
from pathlib import Path

from src.runtime_progress import load_runtime_progress, record_runtime_event, summarize_session_activity


def test_runtime_progress_records_and_summarizes_active_session(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    ts = datetime.fromisoformat("2026-07-30T10:00:00-04:00")
    record_runtime_event(data_dir, user_id="live_bot", event="SERVICE_STARTUP", timestamp=ts, project_root=tmp_path, effective_mode="shadow")
    record_runtime_event(data_dir, user_id="live_bot", event="ACCOUNT_FETCH_SUCCESS", timestamp=ts, project_root=tmp_path, effective_mode="shadow")
    record_runtime_event(data_dir, user_id="live_bot", event="SCAN_CYCLE_COMPLETED", timestamp=ts, project_root=tmp_path, effective_mode="shadow")
    record_runtime_event(data_dir, user_id="live_bot", event="ENTRY_CYCLE_COMPLETED", timestamp=ts, project_root=tmp_path, effective_mode="shadow")

    progress = load_runtime_progress(data_dir, day="2026-07-30", user_id="live_bot")
    summary = summarize_session_activity(progress, now=datetime.fromisoformat("2026-07-30T10:05:00-04:00"))

    assert progress is not None
    assert progress["effective_mode"] == "shadow"
    assert summary["session_activity_status"] == "ACTIVE_VALIDATED"
    assert summary["account_fetch_succeeded"] is True
    assert summary["scanner_cycles_completed"] == 1
    assert summary["entry_cycles_completed"] == 1


def test_runtime_progress_missing_current_day_is_insufficient() -> None:
    summary = summarize_session_activity(None, now=datetime.fromisoformat("2026-07-30T10:05:00-04:00"))

    assert summary["session_activity_status"] == "INSUFFICIENT_CURRENT_DAY_DATA"
    assert summary["reason"] == "runtime_progress_artifact_missing"


def test_runtime_progress_open_session_missed_cycle_is_stall(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    record_runtime_event(
        data_dir,
        user_id="live_bot",
        event="SERVICE_STARTUP",
        timestamp=datetime.fromisoformat("2026-07-30T10:00:00-04:00"),
        project_root=tmp_path,
        effective_mode="shadow",
    )
    record_runtime_event(
        data_dir,
        user_id="live_bot",
        event="ACCOUNT_FETCH_SUCCESS",
        timestamp=datetime.fromisoformat("2026-07-30T10:01:00-04:00"),
        project_root=tmp_path,
        effective_mode="shadow",
    )
    progress = load_runtime_progress(data_dir, day="2026-07-30", user_id="live_bot")

    summary = summarize_session_activity(
        progress,
        now=datetime.fromisoformat("2026-07-30T10:20:30-04:00"),
        cadence_minutes=10,
        grace_minutes=5,
    )

    assert summary["session_activity_status"] == "SCANNER_STALLED"
    assert summary["reason"] == "account_ok_no_scan_after_cadence_grace"


def test_runtime_progress_after_cutoff_without_cycle_is_expected_no_activity(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    record_runtime_event(
        data_dir,
        user_id="live_bot",
        event="SERVICE_STARTUP",
        timestamp=datetime.fromisoformat("2026-07-30T15:22:00-04:00"),
        project_root=tmp_path,
        effective_mode="shadow",
    )
    record_runtime_event(
        data_dir,
        user_id="live_bot",
        event="ACCOUNT_FETCH_SUCCESS",
        timestamp=datetime.fromisoformat("2026-07-30T15:22:05-04:00"),
        project_root=tmp_path,
        effective_mode="shadow",
    )
    progress = load_runtime_progress(data_dir, day="2026-07-30", user_id="live_bot")

    summary = summarize_session_activity(
        progress,
        now=datetime.fromisoformat("2026-07-30T15:22:30-04:00"),
        entry_cutoff=time(15, 0),
    )

    assert summary["session_activity_status"] == "EXPECTED_NO_ACTIVITY_AFTER_ENTRY_CUTOFF"
    assert summary["reason"] == "service_started_or_observed_after_entry_cutoff"
