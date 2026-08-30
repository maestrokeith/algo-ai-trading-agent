from __future__ import annotations

import subprocess
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "codex-auto-merge.yml"
POST_MERGE = PROJECT_ROOT / "scripts" / "post_merge_paper_apply.sh"
LOCAL_PROCESSOR = PROJECT_ROOT / "scripts" / "process_codex_issues_local.sh"
DOCS = PROJECT_ROOT / "docs" / "OPERATIONS.md"


def _workflow_yaml() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_codex_auto_merge_workflow_yaml_has_name_and_trigger_section() -> None:
    workflow = _workflow_yaml()
    assert workflow["name"] == "Codex Guarded Auto-Merge"
    assert "on" in workflow
    assert True not in workflow
    assert set(workflow["on"]) == {
        "pull_request",
        "pull_request_review",
        "check_suite",
        "workflow_run",
    }
    assert workflow["on"]["workflow_run"]["workflows"] == ["Codex PR Validation"]


def test_codex_auto_merge_workflow_exists_and_has_required_triggers() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert WORKFLOW.exists()
    assert "'on':" in text
    assert "pull_request_review:" in text
    assert "check_suite:" in text
    assert "workflow_run:" in text
    assert "Codex PR Validation" in text
    assert "pull_request:" in text
    assert "labeled" in text


def test_codex_auto_merge_checks_out_repo_before_resolving_pr() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "working-directory: ${{ github.workspace }}" in text
    assert "actions/checkout@v4" in text
    assert "fetch-depth: 0" in text
    assert text.index("actions/checkout@v4") < text.index("Resolve pull request")
    assert "Repository diagnostic" in text
    assert "pwd" in text
    assert "git status --short" in text
    assert "git rev-parse --show-toplevel" in text


def test_codex_auto_merge_workflow_guards_eligible_codex_prs() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert 'head.startswith("codex/")' in text
    assert '"codex-validation-passed" not in labels' in text
    assert '"codex-validation-failed" in labels' in text
    assert '"needs-human-review" in labels' in text
    assert "pr.get(\"isDraft\")" in text
    assert "mergeStateStatus" in text
    assert "merge_state_" in text


def test_codex_auto_merge_workflow_merges_but_never_restarts_services() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    lower = text.lower()
    assert "gh pr merge" in text
    assert "--squash" in text
    assert "Merged. Live restart is manual." in text
    assert "Paper post-merge apply must run on the local paper host" in text
    forbidden = [
        "systemctl",
        "algo.service",
        "/etc/algo.env",
        "alpaca_live",
        "apca_api_key_id",
        "apca_api_secret_key",
    ]
    for needle in forbidden:
        assert needle not in lower


def test_post_merge_paper_apply_script_exists_and_is_safe() -> None:
    text = POST_MERGE.read_text(encoding="utf-8")
    assert POST_MERGE.exists()
    assert POST_MERGE.stat().st_mode & 0o111
    assert "paper.service" in text
    assert "systemctl restart \"$paper_service\"" in text
    assert "scripts/check_algo_health.sh --dry-run PAPER" in text
    assert "./bin/algo summary latest --user paper_bot" in text
    assert "AUTOFAIL [PAPER] post-merge smoke failed" in text
    assert "--label \"algo-failure\"" in text
    assert "--label \"environment:paper\"" in text
    assert "--label \"severity:high\"" in text
    assert "algo.service" not in text
    assert "/etc/algo.env" not in text


def test_post_merge_paper_apply_supports_dry_run_pr_and_scan_modes() -> None:
    text = POST_MERGE.read_text(encoding="utf-8")
    assert "--dry-run" in text
    assert "--pr" in text
    assert "--limit" in text
    assert "gh pr list" in text
    assert "gh pr view" in text
    assert "environment:paper" in text
    assert "[PAPER]" in text
    assert "data/local_codex_post_merge" in text


def test_local_processor_invokes_post_merge_paper_apply() -> None:
    text = LOCAL_PROCESSOR.read_text(encoding="utf-8")
    assert "scripts/post_merge_paper_apply.sh --limit \"$limit\"" in text
    assert "scripts/post_merge_paper_apply.sh --dry-run --limit \"$limit\"" in text


def test_codex_auto_merge_docs_cover_level_4() -> None:
    text = DOCS.read_text(encoding="utf-8")
    assert "Level 4 Guarded Codex Auto-Merge" in text
    assert "codex-validation-passed" in text
    assert "Merged. Live restart is manual." in text
    assert "scripts/post_merge_paper_apply.sh" in text
    assert "never restart live" in text.lower()
    assert "paper smoke checks" in text.lower()


def test_codex_auto_merge_shell_scripts_parse() -> None:
    for script in (POST_MERGE, LOCAL_PROCESSOR):
        proc = subprocess.run(
            ["bash", "-n", str(script)],
            cwd=PROJECT_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
