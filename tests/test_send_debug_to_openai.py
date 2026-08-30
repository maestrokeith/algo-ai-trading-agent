from __future__ import annotations

import gzip
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.send_debug_to_openai as debug_report


def test_dry_run_writes_filtered_logs_and_does_not_require_openai_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_dir = tmp_path / "reports" / "debug"

    def fake_run(*_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stdout="\n".join(
                [
                    "Jun 08 info heartbeat",
                    "Jun 08 info DYNAMIC_SCAN selected=[]",
                    "Jun 08 info ENTRY_EVAL symbol=ABAT final=T reason=ok",
                    "Jun 08 info ALLOCATOR ACTIONS: []",
                ]
            ),
            stderr="",
        )

    def fail_api(**_: object) -> str:
        raise AssertionError("OpenAI API should not be called in dry-run")

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(debug_report.subprocess, "run", fake_run)
    monkeypatch.setattr(debug_report, "call_openai_responses_api", fail_api)
    monkeypatch.setattr(debug_report, "_timestamp", lambda: "20260608T120000Z")

    rc = debug_report.main(
        [
            "--since",
            "30 minutes ago",
            "--dry-run",
            "--output-dir",
            str(output_dir),
            "--no-cleanup",
        ]
    )

    assert rc == 0
    plain = output_dir / "algo_debug_20260608T120000Z.log"
    latest = output_dir / "algo_debug_latest.log"
    gz_latest = output_dir / "algo_debug_latest.log.gz"
    analysis = output_dir / "chatgpt_analysis_latest.md"
    assert plain.exists()
    assert latest.exists()
    assert gz_latest.exists()
    assert analysis.exists()
    text = latest.read_text(encoding="utf-8")
    assert "DYNAMIC_SCAN selected=[]" in text
    assert "ENTRY_EVAL symbol=ABAT final=T" in text
    assert "heartbeat" not in text
    with gzip.open(gz_latest, "rt", encoding="utf-8") as fh:
        assert "ALLOCATOR ACTIONS: []" in fh.read()
    assert "DRY RUN OpenAI Algo Debug Report" in analysis.read_text(encoding="utf-8")
    assert "openai_api_called: false" in analysis.read_text(encoding="utf-8")
    assert "DEBUG_LOG_LATEST_GZ" in capsys.readouterr().out


def test_missing_journalctl_writes_warning_and_empty_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "reports" / "debug"

    def fake_run(*_: object, **__: object) -> SimpleNamespace:
        raise FileNotFoundError("journalctl")

    monkeypatch.setattr(debug_report.subprocess, "run", fake_run)
    monkeypatch.setattr(debug_report, "_timestamp", lambda: "20260608T120000Z")

    rc = debug_report.main(
        [
            "--dry-run",
            "--output-dir",
            str(output_dir),
            "--no-cleanup",
        ]
    )

    assert rc == 0
    log_text = (output_dir / "algo_debug_latest.log").read_text(encoding="utf-8")
    analysis = (output_dir / "chatgpt_analysis_latest.md").read_text(encoding="utf-8")
    assert "# warning=journalctl_missing" in log_text
    assert "# no matching important log lines found" in log_text
    assert "collection_warning=journalctl_missing" in analysis


def test_empty_journal_logs_are_handled_gracefully(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "reports" / "debug"

    def fake_run(*_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(debug_report.subprocess, "run", fake_run)
    monkeypatch.setattr(debug_report, "_timestamp", lambda: "20260608T120000Z")

    rc = debug_report.main(
        [
            "--dry-run",
            "--output-dir",
            str(output_dir),
            "--no-cleanup",
        ]
    )

    assert rc == 0
    assert "# no matching important log lines found" in (
        output_dir / "algo_debug_latest.log"
    ).read_text(encoding="utf-8")


def test_collect_journal_logs_reports_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stdout="", stderr="No journal files were found.")

    monkeypatch.setattr(debug_report.subprocess, "run", fake_run)

    raw, warning = debug_report.collect_journal_logs("30 minutes ago")

    assert raw == ""
    assert warning == "journalctl_failed:No journal files were found."


def test_collect_journal_logs_reports_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_: object, **__: object) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(cmd=["journalctl"], timeout=30)

    monkeypatch.setattr(debug_report.subprocess, "run", fake_run)

    raw, warning = debug_report.collect_journal_logs("30 minutes ago")

    assert raw == ""
    assert warning == "journalctl_timeout"
