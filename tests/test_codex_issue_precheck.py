from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

from scripts.codex_issue_precheck import duplicate_check, precheck


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fake_gh(tmp_path: Path, *, head_prs: list[dict] | None = None, open_prs: list[dict] | None = None) -> Path:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "gh",
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "if [[ \"$1 $2\" == 'pr list' && \"$*\" == *'--head codex/issue-200-auto-fix'* ]]; then",
                f"  cat <<'JSON'\n{json.dumps(head_prs or [])}\nJSON",
                "  exit 0",
                "fi",
                "if [[ \"$1 $2\" == 'pr list' ]]; then",
                f"  cat <<'JSON'\n{json.dumps(open_prs or [])}\nJSON",
                "  exit 0",
                "fi",
                "printf '[]\\n'",
            ]
        ),
    )
    return fake_bin


def test_duplicate_detection_uses_exact_branch(tmp_path: Path, monkeypatch) -> None:
    fake_bin = _fake_gh(tmp_path, head_prs=[{"number": 202, "url": "https://example/pr/202"}])
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")

    result = duplicate_check(200, "codex/issue-200-auto-fix", root=PROJECT_ROOT)

    assert result.duplicate is True
    assert result.source == "exact_branch"
    assert result.pr == 202


def test_false_textual_reference_does_not_duplicate(tmp_path: Path, monkeypatch) -> None:
    fake_bin = _fake_gh(
        tmp_path,
        open_prs=[
            {
                "number": 197,
                "url": "https://example/pr/197",
                "title": "Complete Codex issue lifecycle",
                "body": "Test output mentioned #200 casually, but this PR fixes #195.",
                "labels": [],
            }
        ],
    )
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")

    result = duplicate_check(200, "codex/issue-200-auto-fix", root=PROJECT_ROOT)

    assert result.duplicate is False
    assert result.source == "none"


def test_explicit_closes_link_duplicates(tmp_path: Path, monkeypatch) -> None:
    fake_bin = _fake_gh(
        tmp_path,
        open_prs=[{"number": 203, "url": "https://example/pr/203", "title": "Fix", "body": "Closes #200", "labels": []}],
    )
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")

    result = duplicate_check(200, "codex/issue-200-auto-fix", root=PROJECT_ROOT)

    assert result.duplicate is True
    assert result.source == "explicit_link"
    assert result.pr == 203


def test_precheck_suppresses_stale_and_market_data_noise(tmp_path: Path, monkeypatch) -> None:
    fake_bin = _fake_gh(tmp_path)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    stale = precheck(
        {"number": 201, "title": "Old analyzer issue", "body": "No recurrence after latest deployment.", "labels": []},
        root=PROJECT_ROOT,
        branch="codex/issue-201-auto-fix",
    )
    noisy = precheck(
        {"number": 202, "title": "bad_quote burst", "body": "bad_quote only; no traceback", "labels": []},
        root=PROJECT_ROOT,
        branch="codex/issue-202-auto-fix",
    )

    assert stale.classification == "stale_not_reproduced"
    assert stale.codex_required is False
    assert noisy.classification == "market_data_noise"
    assert noisy.codex_required is False


def test_large_issue_requires_decomposition(tmp_path: Path, monkeypatch) -> None:
    fake_bin = _fake_gh(tmp_path)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "default.yaml").write_text(
        "autoops:\n  codex_max_issue_tokens: 5\n  codex_large_issue_requires_decomposition: true\n",
        encoding="utf-8",
    )

    result = precheck(
        {
            "number": 204,
            "title": "Broad end-day issue",
            "body": "order_count_reconciliation journal_filled missing attribution under-trading " * 20,
            "labels": [],
        },
        root=tmp_path,
        branch="codex/issue-204-auto-fix",
    )

    assert result.classification == "needs_human_review"
    assert result.codex_required is False
    assert "order_reconciliation_mismatch" in result.work_units
    assert "strategy_opportunity_loss" in result.work_units


def test_precheck_cli_emits_machine_readable_lines(tmp_path: Path, monkeypatch) -> None:
    fake_bin = _fake_gh(tmp_path)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    issue = tmp_path / "issue.json"
    issue.write_text(json.dumps({"number": 205, "title": "Fixed", "body": "already fixed on main", "labels": []}), encoding="utf-8")

    proc = subprocess.run(
        [
            "python",
            str(PROJECT_ROOT / "scripts" / "codex_issue_precheck.py"),
            "--issue-json",
            str(issue),
            "--issue",
            "205",
            "--branch",
            "codex/issue-205-auto-fix",
            "--repo-root",
            str(PROJECT_ROOT),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert proc.returncode == 10
    assert "CODEX_DUPLICATE_CHECK issue=205 result=not_duplicate source=none pr=none" in proc.stdout
    assert "CODEX_PRECHECK issue=205 classification=already_fixed_on_main codex_required=false" in proc.stdout
    assert "CODEX_PRECHECK_LABEL label=resolved-by-existing-fix" in proc.stdout
