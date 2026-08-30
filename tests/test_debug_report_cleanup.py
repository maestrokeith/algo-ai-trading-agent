from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.debug_report_cleanup import cleanup_debug_reports


NOW = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)


def _touch(path: Path, *, age_days: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("debug\n", encoding="utf-8")
    ts = (NOW - timedelta(days=age_days)).timestamp()
    os.utime(path, (ts, ts))


def test_cleanup_deletes_only_old_known_debug_artifacts(tmp_path: Path) -> None:
    debug_dir = tmp_path / "reports" / "debug"
    old_log = debug_dir / "algo_debug_20260601.log"
    old_gz = debug_dir / "algo_debug_20260601.log.gz"
    old_md = debug_dir / "chatgpt_analysis_20260601.md"
    fresh_log = debug_dir / "algo_debug_20260608.log"
    unknown = debug_dir / "notes_20260601.md"

    for path in (old_log, old_gz, old_md):
        _touch(path, age_days=7)
    _touch(fresh_log, age_days=1)
    _touch(unknown, age_days=7)

    events = cleanup_debug_reports(tmp_path, retention_days=5, now=NOW)

    assert not old_log.exists()
    assert not old_gz.exists()
    assert not old_md.exists()
    assert fresh_log.exists()
    assert unknown.exists()
    lines = [event.log_line() for event in events]
    assert any(f"CLEANUP_DELETED path={old_log}" == line for line in lines)
    assert any(f"CLEANUP_SKIPPED reason=within_retention path={fresh_log}" == line for line in lines)
    assert all(str(unknown) not in line for line in lines)


def test_cleanup_keeps_latest_artifact_names_and_symlinks(tmp_path: Path) -> None:
    debug_dir = tmp_path / "reports" / "debug"
    old_target = debug_dir / "algo_debug_20260601.log.gz"
    latest_plain = debug_dir / "algo_debug_latest.log"
    latest_log = debug_dir / "algo_debug_latest.log.gz"
    latest_md = debug_dir / "chatgpt_analysis_latest.md"
    symlink = debug_dir / "algo_debug_20260602.log.gz"
    old_unprotected = debug_dir / "algo_debug_20260530.log.gz"

    _touch(old_target, age_days=7)
    _touch(old_unprotected, age_days=9)
    _touch(latest_plain, age_days=7)
    _touch(latest_md, age_days=7)
    latest_log.symlink_to(old_target)
    symlink.symlink_to(old_target)

    events = cleanup_debug_reports(tmp_path, retention_days=5, now=NOW)

    assert old_target.exists()
    assert not old_unprotected.exists()
    assert latest_log.is_symlink()
    assert latest_plain.exists()
    assert latest_md.exists()
    assert symlink.is_symlink()
    lines = [event.log_line() for event in events]
    assert any(f"CLEANUP_SKIPPED reason=latest_target path={old_target}" == line for line in lines)
    assert any(f"CLEANUP_SKIPPED reason=latest_artifact path={latest_log}" == line for line in lines)
    assert any(f"CLEANUP_SKIPPED reason=latest_artifact path={latest_plain}" == line for line in lines)
    assert any(f"CLEANUP_SKIPPED reason=latest_artifact path={latest_md}" == line for line in lines)
    assert any(f"CLEANUP_SKIPPED reason=symlink path={symlink}" == line for line in lines)


def test_cleanup_no_cleanup_flag_skips_without_deleting(tmp_path: Path) -> None:
    old_log = tmp_path / "reports" / "debug" / "algo_debug_20260601.log"
    _touch(old_log, age_days=7)

    events = cleanup_debug_reports(tmp_path, retention_days=5, now=NOW, enabled=False)

    assert old_log.exists()
    assert [event.log_line() for event in events] == [
        f"CLEANUP_SKIPPED reason=no_cleanup path={tmp_path / 'reports' / 'debug'}"
    ]
