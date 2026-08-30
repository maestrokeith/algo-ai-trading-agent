from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "codex-pr-validation.yml"
HELPER = PROJECT_ROOT / "scripts" / "validate_codex_pr.sh"
DOCS = PROJECT_ROOT / "docs" / "OPERATIONS.md"


def test_codex_pr_validation_workflow_exists_and_triggers_on_pull_request() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert WORKFLOW.exists()
    assert "pull_request:" in text
    assert "opened" in text
    assert "synchronize" in text
    assert "reopened" in text
    assert "ready_for_review" in text


def test_codex_pr_validation_targets_codex_prs() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "startsWith(github.head_ref, 'codex/')" in text
    assert "contains(github.event.pull_request.labels.*.name, 'codex-pr-opened')" in text
    assert "startsWith(github.event.pull_request.title, 'Codex auto-fix:')" in text


def test_codex_pr_validation_workflow_permissions_and_labels() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "contents: read" in text
    assert "pull-requests: write" in text
    assert "issues: write" in text
    assert "codex-validation-passed" in text
    assert "codex-validation-failed" in text
    assert "--remove-label \"codex-validation-failed\"" in text
    assert "--remove-label \"needs-human-review\"" not in text


def test_codex_pr_validation_workflow_safety_boundaries() -> None:
    text = WORKFLOW.read_text(encoding="utf-8").lower()
    helper = HELPER.read_text(encoding="utf-8").lower()
    forbidden = [
        "gh pr merge",
        "merge pull-request",
        "kubectl apply",
        "scp ",
        "rsync ",
        "systemctl restart",
        "/etc/algo.env",
        "alpaca_live",
        "apca_api_key_id",
        "apca_api_secret_key",
        "openai_api_key",
    ]
    for needle in forbidden:
        assert needle not in text
        assert needle not in helper
    assert "no auto-merge" in text
    assert "no deploy" in text
    assert "no live restart" in text
    assert "no live broker credentials used" in text


def test_codex_pr_validation_installs_dependencies_for_github_runner() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "actions/setup-python@v5" in text
    assert 'python-version: "3.12"' in text
    assert "python -m pip install -U pip" in text
    assert "pip install -r requirements.txt" in text
    assert "pip install pytest" in text


def test_codex_pr_validation_checks_out_workspace_before_git_diagnostics() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "working-directory: ${{ github.workspace }}" in text
    assert "actions/checkout@v4" in text
    assert "fetch-depth: 0" in text
    assert text.index("actions/checkout@v4") < text.index("Repository diagnostic")
    assert "pwd" in text
    assert "git status --short" in text
    assert "git rev-parse --show-toplevel" in text


def test_codex_pr_validation_runs_required_commands_and_safe_diagnostics() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    helper = HELPER.read_text(encoding="utf-8")
    assert "bash scripts/validate_codex_pr.sh" in workflow
    assert "bash -n scripts/report_algo_failure_to_github.sh" in helper
    assert "bash -n scripts/check_algo_health.sh" in helper
    assert "python -m py_compile scripts/analyze_algo_health_report.py" in helper
    assert (
        "pytest tests/test_failure_reporter.py tests/test_algo_health_check.py "
        "tests/test_algo_health_analyzer.py tests/test_codex_auto_fix_workflow.py "
        "tests/test_codex_pr_validation_workflow.py tests/test_local_codex_issue_processor.py -v"
    ) in helper
    assert "pytest tests/ -q" in helper
    assert 'run_optional_with_artifacts "premarket readiness" data/premarket ./bin/algo premarket-ready' in helper
    assert 'run_optional_with_artifacts "paper summary" data ./bin/algo summary latest --user paper_bot' in helper
    assert (
        'run_optional_with_artifacts "paper options diagnostics" data ./bin/algo '
        "paper-options-diagnostics --user paper_bot --symbol QQQ"
    ) in helper
    assert 'run_optional_with_artifacts "paper health dry-run" data scripts/check_algo_health.sh --dry-run PAPER' in helper
    assert "SKIPPED $label: missing local artifacts" in helper
    assert "/opt/algosphere/algo-ai-trading-agent" not in workflow
    assert "/opt/algosphere/algo-ai-trading-agent" not in helper


def test_codex_pr_validation_updates_existing_comment() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "Find previous validation comment" in text
    assert "issues.updateComment" in text
    assert "issues.createComment" in text
    assert "Codex PR Validation:" in text


def test_operations_docs_cover_codex_pr_validation() -> None:
    text = DOCS.read_text(encoding="utf-8")
    assert "Level 3 Codex PR Validation" in text
    assert "codex-validation-passed" in text
    assert "codex-validation-failed" in text
    assert "no auto-merge" in text.lower()
    assert "no deploy" in text.lower()
    assert "no live restart" in text.lower()
    assert "human review is still required" in text.lower()
