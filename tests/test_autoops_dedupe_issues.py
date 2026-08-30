from __future__ import annotations

import json
from pathlib import Path

from scripts import run_autoops


def _issue(
    number: int,
    *,
    fingerprint: str | None,
    created_at: str,
    title: str = "Analyzer issue",
) -> dict[str, object]:
    body = (
        "Automated log analyzer detected a recurring AlgoSphere runtime problem.\n\n"
        f"- Fingerprint: {fingerprint}\n"
    ) if fingerprint else "Human-created issue without analyzer fingerprint."
    return {
        "number": number,
        "title": title,
        "body": body,
        "createdAt": created_at,
        "labels": [{"name": "algo-failure"}, {"name": "environment:live"}],
    }


def test_dedupe_closes_newer_duplicates_only(tmp_path: Path, monkeypatch, capsys) -> None:
    calls: list[list[str]] = []
    issues = [
        _issue(10, fingerprint="log-analysis:live:runtime:traceback:abc", created_at="2026-06-29T10:00:00Z"),
        _issue(11, fingerprint="log-analysis:live:runtime:traceback:abc", created_at="2026-06-29T10:05:00Z"),
        _issue(12, fingerprint="log-analysis:live:runtime:traceback:def", created_at="2026-06-29T10:06:00Z"),
    ]

    def fake_run(args, **kwargs):
        argv = list(args)
        calls.append(argv)
        if argv[:4] == ["gh", "issue", "list", "--state"]:
            return 0, json.dumps(issues)
        return 0, "ok"

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._dedupe_issues(tmp_path, environment="live")

    assert rc == 0
    assert ["gh", "issue", "comment", "11", "--body", "Duplicate of #10. Closing to reduce noise."] in calls
    assert ["gh", "issue", "close", "11", "--reason", "not planned"] in calls
    assert not any(call[:4] == ["gh", "issue", "close", "10"] for call in calls)
    assert not any(call[:4] == ["gh", "issue", "close", "12"] for call in calls)
    assert "AUTOOPS_DEDUPE_STATUS success=true groups=1 closed=1 dry_run=false human_untouched=0" in capsys.readouterr().out


def test_dedupe_dry_run_closes_nothing(tmp_path: Path, monkeypatch, capsys) -> None:
    calls: list[list[str]] = []
    issues = [
        _issue(20, fingerprint="log-analysis:live:runtime:traceback:abc", created_at="2026-06-29T10:00:00Z"),
        _issue(21, fingerprint="log-analysis:live:runtime:traceback:abc", created_at="2026-06-29T10:05:00Z"),
    ]

    def fake_run(args, **kwargs):
        argv = list(args)
        calls.append(argv)
        if argv[:4] == ["gh", "issue", "list", "--state"]:
            return 0, json.dumps(issues)
        return 0, "ok"

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._dedupe_issues(tmp_path, environment="live", dry_run=True)

    assert rc == 0
    assert not any(call[:3] == ["gh", "issue", "comment"] for call in calls)
    assert not any(call[:3] == ["gh", "issue", "close"] for call in calls)
    assert "AUTOOPS_DEDUPE_STATUS success=true groups=1 closed=0 dry_run=true" in capsys.readouterr().out


def test_dedupe_leaves_human_issues_without_fingerprint_untouched(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []
    issues = [
        _issue(30, fingerprint=None, created_at="2026-06-29T10:00:00Z", title="Human issue"),
        _issue(31, fingerprint="log-analysis:live:runtime:traceback:abc", created_at="2026-06-29T10:05:00Z"),
    ]

    def fake_run(args, **kwargs):
        argv = list(args)
        calls.append(argv)
        if argv[:4] == ["gh", "issue", "list", "--state"]:
            return 0, json.dumps(issues)
        return 0, "ok"

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._dedupe_issues(tmp_path, environment="live")

    assert rc == 0
    assert not any(call[:3] == ["gh", "issue", "comment"] for call in calls)
    assert not any(call[:3] == ["gh", "issue", "close"] for call in calls)
    assert "human_untouched=1" in capsys.readouterr().out

