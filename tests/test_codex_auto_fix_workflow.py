from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts.build_codex_issue_prompt import build_prompt


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "codex-auto-fix.yml"
MANUAL_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "codex-from-issue.yml"
PROMPT_BUILDER = PROJECT_ROOT / "scripts" / "build_codex_issue_prompt.py"
CODEX_RUNNER = PROJECT_ROOT / "scripts" / "run_codex_in_actions.sh"
DOCS = PROJECT_ROOT / "docs" / "OPERATIONS.md"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_codex_auto_fix_workflow_exists_and_triggers_on_issues() -> None:
    text = _workflow_text()
    assert WORKFLOW.exists()
    assert "issues:" in text
    assert "opened" in text
    assert "labeled" in text
    assert "edited" in text
    assert "workflow_dispatch" not in text


def test_codex_auto_fix_workflow_requires_codex_and_auto_fix_labels() -> None:
    text = _workflow_text()
    assert "contains(github.event.issue.labels.*.name, 'codex')" in text
    assert "contains(github.event.issue.labels.*.name, 'auto-fix')" in text
    assert "!contains(github.event.issue.labels.*.name, 'needs-human-review')" in text
    assert "codex-running" in text
    assert "codex-pr-opened" in text
    assert "codex-validation-failed" in text
    assert "needs-human-review" in text


def test_codex_auto_fix_workflow_has_duplicate_pr_protection() -> None:
    text = _workflow_text()
    assert "gh pr list --state open --head \"$BRANCH_NAME\"" in text
    assert "git ls-remote --exit-code --heads origin \"$BRANCH_NAME\"" in text
    assert "open pull request already references issue" in text


def test_codex_auto_fix_workflow_safety_boundaries() -> None:
    text = _workflow_text().lower()
    forbidden = [
        "merge pull-request",
        "gh pr merge",
        "auto-merge",
        "systemctl restart",
        "/etc/algo.env",
        "alpaca_live",
        "apca_api_key_id",
        "apca_api_secret_key",
    ]
    for needle in forbidden:
        assert needle not in text
    assert "no live service restart or deploy was performed" in text
    assert "requires human review before merge" in text


def test_codex_auto_fix_workflow_runs_required_validation() -> None:
    text = _workflow_text()
    assert "bash -n scripts/report_algo_failure_to_github.sh" in text
    assert "bash -n scripts/check_algo_health.sh" in text
    assert "python -m py_compile scripts/analyze_algo_health_report.py" in text
    assert (
        "PYTHONPATH=. pytest tests/test_failure_reporter.py tests/test_algo_health_check.py "
        "tests/test_algo_health_analyzer.py -v"
    ) in text
    assert "PYTHONPATH=. pytest tests/ -q" in text


def test_codex_workflows_use_github_actions_sandbox_wrapper() -> None:
    auto_fix = _workflow_text()
    manual = MANUAL_WORKFLOW.read_text(encoding="utf-8")

    assert "scripts/run_codex_in_actions.sh /tmp/codex_prompt.txt /tmp/codex_result.txt" in auto_fix
    assert "scripts/run_codex_in_actions.sh /tmp/codex_prompt.txt /tmp/codex_result.txt" in manual
    assert "--sandbox workspace-write" not in auto_fix
    assert "--sandbox workspace-write" not in manual


def test_codex_actions_runner_selects_compatible_sandbox_and_diagnostics() -> None:
    text = CODEX_RUNNER.read_text(encoding="utf-8")

    assert CODEX_RUNNER.exists()
    assert CODEX_RUNNER.stat().st_mode & 0o111
    assert 'if [ "${GITHUB_ACTIONS:-}" = "true" ]' in text
    assert "CODEX_GITHUB_ACTIONS_SANDBOX:-danger-full-access" in text
    assert "bwrap loopback RTM_NEWADDR Operation not permitted" in text
    assert "CODEX_VERSION_BEGIN" in text
    assert "codex --version" in text
    assert "CODEX_SANDBOX_MODE selected=" in text
    assert "CODEX_SANDBOX_FALLBACK_REASON" in text
    assert "--sandbox \"$sandbox_mode\"" in text


def test_codex_actions_runner_keeps_no_deploy_safety_boundary() -> None:
    text = CODEX_RUNNER.read_text(encoding="utf-8").lower()
    forbidden = [
        "gh pr merge",
        "systemctl restart",
        "/etc/algo.env",
        "alpaca_live",
        "apca_api_key_id",
        "apca_api_secret_key",
    ]
    for needle in forbidden:
        assert needle not in text


def test_prompt_builder_includes_safety_and_research_rules(tmp_path: Path) -> None:
    issue = {
        "number": 110,
        "title": "HEALTH [LIVE] unusual scanner rejection rate",
        "url": "https://github.com/example/repo/issues/110",
        "labels": [
            {"name": "codex"},
            {"name": "auto-fix"},
            {"name": "environment:live"},
            {"name": "severity:research"},
        ],
        "body": "\n".join(
            [
                "- Hostname: algosphere-live-host",
                "- Service Name: algo.service",
                "- Failure Source: health-monitor",
                "",
                "Rejection rate is high. Investigate without changing strategy thresholds.",
            ]
        ),
    }
    prompt = build_prompt(issue)
    assert "Issue title: HEALTH [LIVE] unusual scanner rejection rate" in prompt
    assert "Issue labels: codex, auto-fix, environment:live, severity:research" in prompt
    assert "Environment: LIVE" in prompt
    assert "Hostname: algosphere-live-host" in prompt
    assert "Service name: algo.service" in prompt
    assert "Failure source: health-monitor" in prompt
    assert "Do not modify live trading risk limits." in prompt
    assert "Do not change allocation percentages." in prompt
    assert "Do not enable live options." in prompt
    assert "If the issue appears to be a strategy, research, performance" in prompt

    issue_json = tmp_path / "issue.json"
    output = tmp_path / "prompt.txt"
    issue_json.write_text(json.dumps(issue), encoding="utf-8")
    proc = subprocess.run(
        [
            "python",
            str(PROMPT_BUILDER),
            "--issue-json",
            str(issue_json),
            "--output",
            str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "Fix GitHub issue #110" in output.read_text(encoding="utf-8")


def test_operations_docs_cover_codex_auto_fix_workflow() -> None:
    text = DOCS.read_text(encoding="utf-8")
    assert "Level 2 Codex Auto-Fix PR Workflow" in text
    assert "codex" in text
    assert "auto-fix" in text
    assert "OPENAI_API_KEY" in text
    assert "no auto-merge" in text.lower()
    assert "no live restart or deploy" in text.lower()
    assert "needs-human-review" in text
