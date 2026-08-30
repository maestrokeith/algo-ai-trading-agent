from __future__ import annotations

import getpass
import json
from pathlib import Path

import pytest

from src.artifact_writability import (
    ArtifactWriteError,
    artifact_target_diagnostics,
    atomic_write_text,
    check_atomic_writability,
    repair_command_for_project_artifacts,
)


def test_atomic_write_text_creates_missing_directory_and_replaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALGO_RUNTIME_USER", getpass.getuser())
    target = tmp_path / "data" / "research" / "bars_status" / "2026-07-24_live_bot.json"

    atomic_write_text(target, json.dumps({"ok": True}) + "\n", generator="test")
    atomic_write_text(target, json.dumps({"ok": "updated"}) + "\n", generator="test")

    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": "updated"}
    assert not list(target.parent.glob("*.tmp"))


def test_artifact_writability_check_exercises_atomic_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALGO_RUNTIME_USER", getpass.getuser())
    directory = tmp_path / "data" / "research" / "bars_consistency"
    result = check_atomic_writability(directory)

    assert result["ok"] is True
    assert result["atomic_create"] is True
    assert result["atomic_rename"] is True
    assert result["cleanup"] is True


def test_unwritable_directory_reports_permission_diagnostic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALGO_RUNTIME_USER", getpass.getuser())
    directory = tmp_path / "locked"
    directory.mkdir()
    directory.chmod(0o555)
    try:
        diag = artifact_target_diagnostics(directory)
        assert diag["target_user_writable"] is False
        with pytest.raises(ArtifactWriteError) as excinfo:
            atomic_write_text(directory / "report.json", "{}\n", generator="test")
        assert excinfo.value.reason == "directory_not_writable_by_runtime_user"
        assert excinfo.value.as_dict()["error_type"] == "artifact_write_permission_error"
    finally:
        directory.chmod(0o755)


def test_repair_command_is_narrowly_scoped_to_artifact_directories(tmp_path: Path) -> None:
    cmd = repair_command_for_project_artifacts(tmp_path)

    assert str(tmp_path / "data" / "research_metrics") in cmd
    assert str(tmp_path / "reports") in cmd
    assert str(tmp_path / "data" / "logs") in cmd
    assert "chmod 777" not in cmd
    assert "chown -R algosphere:algosphere" in cmd


def test_artifact_writability_cli_is_registered() -> None:
    source = (Path(__file__).resolve().parents[1] / "bin" / "algo").read_text(encoding="utf-8")
    assert "artifact-writability-check)" in source
    assert "artifact-writability-check   verify diagnostic artifact directories" in source
    assert "shadow-readiness)" in source
    assert "shadow-readiness   verify guarded shadow mode readiness" in source
