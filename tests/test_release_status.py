"""Tests for release status helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import src.release_status as rs
from src.release_status import ReleaseStatus, collect_release_status, format_release_status


def test_collect_release_status_reads_git_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_git(repo_root: Path, *args: str) -> str:
        calls.append(args)
        if args == ("rev-parse", "--short=12", "HEAD"):
            return "abc123"
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main"
        if args == ("describe", "--tags", "--always", "--dirty"):
            return "prod-20260605-1"
        if args == ("status", "--porcelain"):
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(rs, "_git", fake_git)

    status = collect_release_status(tmp_path)

    assert status == ReleaseStatus(
        commit="abc123",
        branch="main",
        describe="prod-20260605-1",
        dirty=False,
    )
    assert ("status", "--porcelain") in calls


def test_collect_release_status_falls_back_when_describe_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_git(repo_root: Path, *args: str) -> str:
        if args == ("rev-parse", "--short=12", "HEAD"):
            return "abc123"
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main"
        if args == ("describe", "--tags", "--always", "--dirty"):
            raise subprocess.CalledProcessError(1, ["git"])
        if args == ("status", "--porcelain"):
            return " M file.py"
        raise AssertionError(args)

    monkeypatch.setattr(rs, "_git", fake_git)

    status = collect_release_status(tmp_path)

    assert status.describe == "abc123"
    assert status.dirty is True


def test_format_release_status() -> None:
    text = format_release_status(
        ReleaseStatus(commit="abc123", branch="main", describe="prod-20260605-1", dirty=False)
    )

    assert "branch=main" in text
    assert "commit=abc123" in text
    assert "version=prod-20260605-1" in text
    assert "worktree=clean" in text
