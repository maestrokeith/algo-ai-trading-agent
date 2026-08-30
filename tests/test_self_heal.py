from __future__ import annotations

from pathlib import Path

import pytest

from scripts import run_self_heal


class _Runner:
    def __init__(self, responses: dict[tuple[str, ...], run_self_heal.CommandResult] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        args: list[str] | tuple[str, ...],
        *,
        cwd: Path = run_self_heal.ROOT,
        check: bool = False,
    ) -> run_self_heal.CommandResult:
        key = tuple(args)
        self.calls.append(key)
        return self.responses.get(key, run_self_heal.CommandResult(0, "", ""))


class _MissingJournalRunner(_Runner):
    def run(
        self,
        args: list[str] | tuple[str, ...],
        *,
        cwd: Path = run_self_heal.ROOT,
        check: bool = False,
    ) -> run_self_heal.CommandResult:
        key = tuple(args)
        self.calls.append(key)
        if key and key[0] == "journalctl":
            raise FileNotFoundError("journalctl")
        return super().run(args, cwd=cwd, check=check)


def test_collect_logs_linux_uses_journalctl(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ALGO_SELF_HEAL_LOG_FILE", raising=False)
    monkeypatch.delenv("ALGO_PAPER_LOG_FILE", raising=False)
    monkeypatch.setenv("ALGO_AUTOOPS_PLATFORM", "Linux")
    runner = _Runner(
        {
            ("journalctl", "-u", "paper.service", "--since", "30 minutes ago", "--no-pager"): run_self_heal.CommandResult(
                0,
                "INFO linux paper log\n",
                "",
            )
        }
    )

    logs = run_self_heal.collect_logs(runner, "PAPER", "30 minutes ago")

    assert logs == "INFO linux paper log\n"
    assert ("journalctl", "-u", "paper.service", "--since", "30 minutes ago", "--no-pager") in runner.calls
    assert "SELF_HEAL_LOG_SOURCE source=journalctl" in capsys.readouterr().out


def test_collect_logs_darwin_uses_paper_file_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_path = tmp_path / "paper_full.log"
    log_path.write_text("Traceback paper runtime\n", encoding="utf-8")
    monkeypatch.setenv("ALGO_AUTOOPS_PLATFORM", "Darwin")
    monkeypatch.setenv("ALGO_SELF_HEAL_LOG_FILE", str(log_path))
    runner = _Runner()

    logs = run_self_heal.collect_logs(runner, "PAPER", "30 minutes ago")

    assert logs == "Traceback paper runtime\n"
    assert runner.calls == []
    out = capsys.readouterr().out
    assert "SELF_HEAL_LOG_SOURCE source=file" in out
    assert str(log_path) in out


def test_collect_logs_missing_journalctl_is_graceful(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ALGO_SELF_HEAL_LOG_FILE", raising=False)
    monkeypatch.delenv("ALGO_PAPER_LOG_FILE", raising=False)
    monkeypatch.setenv("ALGO_AUTOOPS_PLATFORM", "Linux")
    runner = _MissingJournalRunner()

    logs = run_self_heal.collect_logs(runner, "PAPER", "30 minutes ago")

    assert logs == ""
    assert ("journalctl", "-u", "paper.service", "--since", "30 minutes ago", "--no-pager") in runner.calls
    assert "SELF_HEAL_LOG_SOURCE source=none reason=journalctl_unavailable" in capsys.readouterr().out


def test_collect_logs_darwin_missing_logs_does_not_call_journalctl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ALGO_SELF_HEAL_LOG_FILE", raising=False)
    monkeypatch.delenv("ALGO_PAPER_LOG_FILE", raising=False)
    monkeypatch.setenv("ALGO_AUTOOPS_PLATFORM", "Darwin")
    monkeypatch.setattr(run_self_heal, "ROOT", tmp_path)
    runner = _MissingJournalRunner()

    logs = run_self_heal.collect_logs(runner, "PAPER", "30 minutes ago")

    assert "PAPER_REVIEW_LOG_MISSING path=" in logs
    assert str(tmp_path / "data" / "review") in logs
    assert runner.calls == []
    assert "SELF_HEAL_LOG_SOURCE source=none reason=missing_review_log" in capsys.readouterr().out
