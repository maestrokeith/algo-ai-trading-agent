from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "process_codex_issues_local.sh"
SERVICE = PROJECT_ROOT / "deploy" / "systemd" / "user" / "algo-local-codex-processor.service"
TIMER = PROJECT_ROOT / "deploy" / "systemd" / "user" / "algo-local-codex-processor.timer"
DOCS = PROJECT_ROOT / "docs" / "OPERATIONS.md"
LEGACY_ACTIONS_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "codex-auto-fix.yml"


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run_processor_with_labels(
    tmp_path: Path,
    *,
    labels: list[str],
    processor: str,
    issue: str = "120",
) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    issue_json = json.dumps(
        {
            "number": int(issue),
            "title": "Routing test",
            "body": "Environment: PAPER" if "environment:paper" in labels else "Environment: LIVE",
            "labels": [{"name": label} for label in labels],
            "url": f"https://example.test/issues/{issue}",
        }
    )
    _write_executable(
        fake_bin / "gh",
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "if [[ \"$1 $2\" == 'pr list' ]]; then printf '[]\\n'; exit 0; fi",
                "if [[ \"$1 $2\" == 'issue view' ]]; then",
                f"  cat <<'JSON'\n{issue_json}\nJSON",
                "  exit 0",
                "fi",
                "printf '[]\\n'",
                "",
            ]
        ),
    )
    _write_executable(fake_bin / "hostname", "#!/usr/bin/env bash\necho route-host\n")
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_CODEX_PROCESSOR_LABEL": processor,
    }
    return subprocess.run(
        [str(SCRIPT), "--dry-run", "--issue", issue],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
        timeout=30,
    )


def test_local_codex_issue_processor_script_exists_and_is_executable() -> None:
    assert SCRIPT.exists()
    assert SCRIPT.stat().st_mode & 0o111


def test_local_codex_issue_processor_supports_required_options() -> None:
    text = _script_text()
    for option in ("--paper", "--live", "--dry-run", "--issue", "--limit", "--no-push", "--no-pr"):
        assert option in text
    assert "Usage: scripts/process_codex_issues_local.sh" in text


def test_local_codex_issue_processor_is_bash3_and_macos_mktemp_compatible() -> None:
    text = _script_text()
    assert "mapfile" not in text
    assert 'mktemp "/tmp/' not in text
    assert "portable_mktemp()" in text
    assert "read_lines_into_array()" in text
    assert 'mktemp "${tmpdir}/${name}.XXXXXX"' in text
    assert 'final_path="${path}${suffix}"' in text


def test_local_codex_issue_processor_uses_gh_and_local_codex_exec() -> None:
    text = _script_text()
    assert "gh issue list" in text
    assert "gh issue view" in text
    assert "gh pr create" in text
    assert "codex exec" in text
    assert "mode=chatgpt-login" in text
    assert "scripts/build_codex_issue_prompt.py" in text
    assert "## Runtime Context" in text
    assert "Environment:" in text
    assert "Hostname:" in text
    assert "Service name:" in text


def test_local_codex_issue_processor_filters_labels_and_has_idempotency() -> None:
    text = _script_text()
    assert "--label codex" in text
    assert "--label auto-fix" in text
    assert "issue must have both codex and auto-fix labels" in text
    assert "needs-human-review" in text
    assert "codex-pr-opened" in text
    assert "/tmp/algo_codex_issue_${issue}.lock" in text
    assert "git show-ref --verify --quiet \"refs/heads/${branch}\"" in text
    assert "git ls-remote --exit-code --heads origin \"$branch\"" in text
    assert "gh pr list --state open --head \"$branch\"" in text
    assert "--search \"#${issue}\"" not in text
    assert "scripts/codex_issue_precheck.py" in text
    assert "CODEX_DUPLICATE_CHECK issue=" in text
    assert "CODEX_PRECHECK" in text
    assert "ISSUE_ROUTING issue_number=" in text
    assert "ISSUE_ACCEPTED issue_number=" in text
    assert "ISSUE_REJECTED issue_number=" in text
    assert "ISSUE_SKIP issue_number=" in text
    assert "CODEX_RESULT issue=${issue} status=codex_running reason=active_lock" in text
    assert "Removing stale lock for issue #${issue}" in text
    assert "CODEX_RESULT issue=${issue} status=failed reason=dirty_worktree" in text


def test_local_codex_issue_processor_emits_machine_readable_results() -> None:
    text = _script_text()
    assert "CODEX_RESULT issue=${issue} status=fix_local_only reason=push_failed" in text
    assert "CODEX_RESULT issue=${issue} status=fix_local_only reason=pr_create_failed" in text
    assert "CODEX_RESULT issue=${issue} status=pr_created" in text
    assert "CODEX_RESULT issue=${issue} status=no_change" in text
    assert "CODEX_RESULT issue=${issue} status=failed reason=validation_failed" in text


def test_local_codex_issue_processor_distinguishes_active_and_stale_locks() -> None:
    text = _script_text()
    assert "kill -0 \"$existing_pid\"" in text
    assert "status=codex_running reason=active_lock" in text
    assert "rm -f \"$lock\"" in text
    assert "status=codex_running reason=lock_race" in text


def test_mac_processor_accepts_paper_issue(tmp_path: Path) -> None:
    proc = _run_processor_with_labels(
        tmp_path,
        labels=["codex", "auto-fix", "environment:paper", "processor:mac-paper"],
        processor="processor:mac-paper",
    )

    assert proc.returncode == 0
    assert "ISSUE_ROUTING issue_number=120 environment=paper processor=processor:mac-paper" in proc.stdout
    assert "ISSUE_ACCEPTED issue_number=120 environment=paper processor=processor:mac-paper" in proc.stdout
    assert "ISSUE_SKIP" not in proc.stdout


def test_paper_flag_filters_paper_issues_only(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    calls = tmp_path / "gh_calls.log"
    issue_json = json.dumps(
        {
            "number": 120,
            "title": "Paper routing test",
            "body": "Environment: PAPER",
            "labels": [
                {"name": "codex"},
                {"name": "auto-fix"},
                {"name": "environment:paper"},
                {"name": "processor:mac-paper"},
            ],
            "url": "https://example.test/issues/120",
        }
    )
    _write_executable(
        fake_bin / "gh",
        "\n".join(
            [
                "#!/usr/bin/env bash",
                f"printf '%s\\n' \"$*\" >> {calls}",
                "if [[ \"$1 $2\" == 'issue list' ]]; then echo 120; exit 0; fi",
                "if [[ \"$1 $2\" == 'issue view' ]]; then",
                f"  cat <<'JSON'\n{issue_json}\nJSON",
                "  exit 0",
                "fi",
                "if [[ \"$1 $2\" == 'pr list' ]]; then printf '[]\\n'; exit 0; fi",
                "printf '[]\\n'",
            ]
        ),
    )
    _write_executable(fake_bin / "hostname", "#!/usr/bin/env bash\necho route-host\n")
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}

    proc = subprocess.run(
        [str(SCRIPT), "--paper", "--dry-run", "--limit", "1"],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
        timeout=30,
    )

    assert proc.returncode == 0
    logged = calls.read_text(encoding="utf-8")
    assert "--label codex" in logged
    assert "--label auto-fix" in logged
    assert "--label environment:paper" in logged
    assert "--label processor:mac-paper" in logged
    assert "environment:live" not in logged
    assert "processor:live-linux" not in logged
    assert "CODEX_ELIGIBLE issue_number=120 environment=paper processor=processor:mac-paper" in proc.stdout


def test_mac_processor_accepts_autoops_drill_paper_issue(tmp_path: Path) -> None:
    proc = _run_processor_with_labels(
        tmp_path,
        labels=["autoops-drill", "codex", "auto-fix", "environment:paper", "processor:mac-paper"],
        processor="processor:mac-paper",
    )

    assert proc.returncode == 0
    assert "ISSUE_ROUTING issue_number=120 environment=paper processor=processor:mac-paper" in proc.stdout
    assert "ISSUE_ACCEPTED issue_number=120 environment=paper processor=processor:mac-paper" in proc.stdout
    assert "ISSUE_SKIP" not in proc.stdout


def test_mac_processor_rejects_live_issue(tmp_path: Path) -> None:
    proc = _run_processor_with_labels(
        tmp_path,
        labels=["codex", "auto-fix", "environment:live", "processor:fedora-live"],
        processor="processor:mac-paper",
    )

    assert proc.returncode == 0
    assert "ISSUE_SKIP issue_number=120 reason=environment_mismatch environment=live processor=processor:mac-paper" in proc.stdout
    assert "ISSUE_REJECTED issue_number=120 environment=live processor=processor:mac-paper" in proc.stdout


def test_fedora_processor_accepts_live_issue(tmp_path: Path) -> None:
    proc = _run_processor_with_labels(
        tmp_path,
        labels=["codex", "auto-fix", "environment:live", "processor:fedora-live"],
        processor="processor:fedora-live",
    )

    assert proc.returncode == 0
    assert "ISSUE_ACCEPTED issue_number=120 environment=live processor=processor:fedora-live" in proc.stdout
    assert "ISSUE_SKIP" not in proc.stdout


def test_live_linux_processor_accepts_live_issue(tmp_path: Path) -> None:
    proc = _run_processor_with_labels(
        tmp_path,
        labels=["codex", "auto-fix", "environment:live", "processor:live-linux"],
        processor="processor:live-linux",
    )

    assert proc.returncode == 0
    assert "ISSUE_ACCEPTED issue_number=120 environment=live processor=processor:live-linux" in proc.stdout
    assert "ISSUE_SKIP" not in proc.stdout


def test_fedora_processor_rejects_paper_issue(tmp_path: Path) -> None:
    proc = _run_processor_with_labels(
        tmp_path,
        labels=["codex", "auto-fix", "environment:paper", "processor:mac-paper", "paper-options"],
        processor="processor:fedora-live",
    )

    assert proc.returncode == 0
    assert "ISSUE_SKIP issue_number=120 reason=environment_mismatch environment=paper processor=processor:fedora-live" in proc.stdout


def test_live_flag_filters_live_issues_only(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    calls = tmp_path / "gh_calls.log"
    issue_json = json.dumps(
        {
            "number": 120,
            "title": "Live routing test",
            "body": "Environment: LIVE",
            "labels": [
                {"name": "codex"},
                {"name": "auto-fix"},
                {"name": "environment:live"},
                {"name": "processor:live-linux"},
            ],
            "url": "https://example.test/issues/120",
        }
    )
    _write_executable(
        fake_bin / "gh",
        "\n".join(
            [
                "#!/usr/bin/env bash",
                f"printf '%s\\n' \"$*\" >> {calls}",
                "if [[ \"$1 $2\" == 'issue list' ]]; then echo 120; exit 0; fi",
                "if [[ \"$1 $2\" == 'issue view' ]]; then",
                f"  cat <<'JSON'\n{issue_json}\nJSON",
                "  exit 0",
                "fi",
                "if [[ \"$1 $2\" == 'pr list' ]]; then printf '[]\\n'; exit 0; fi",
                "printf '[]\\n'",
            ]
        ),
    )
    _write_executable(fake_bin / "hostname", "#!/usr/bin/env bash\necho route-host\n")
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}

    proc = subprocess.run(
        [str(SCRIPT), "--live", "--dry-run", "--limit", "1"],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
        timeout=30,
    )

    assert proc.returncode == 0
    logged = calls.read_text(encoding="utf-8")
    assert "--label environment:live" in logged
    assert "--label processor:live-linux" in logged
    assert "Skipping PR #" not in proc.stdout
    assert "environment:paper" not in logged
    assert "processor:mac-paper" not in logged


def test_processor_skips_missing_processor_label(tmp_path: Path) -> None:
    proc = _run_processor_with_labels(
        tmp_path,
        labels=["codex", "auto-fix", "environment:paper"],
        processor="processor:mac-paper",
    )

    assert proc.returncode == 0
    assert "ISSUE_SKIP issue_number=120 reason=missing_processor_label environment=paper processor=processor:mac-paper" in proc.stdout


def test_processor_skips_missing_environment_label(tmp_path: Path) -> None:
    proc = _run_processor_with_labels(
        tmp_path,
        labels=["codex", "auto-fix", "processor:mac-paper"],
        processor="processor:mac-paper",
    )

    assert proc.returncode == 0
    assert "ISSUE_SKIP issue_number=120 reason=missing_environment_label environment=unknown processor=processor:mac-paper" in proc.stdout


def test_local_codex_issue_processor_runs_required_validation() -> None:
    text = _script_text()
    assert "bash -n scripts/report_algo_failure_to_github.sh" in text
    assert "bash -n scripts/check_algo_health.sh" in text
    assert "python -m py_compile scripts/analyze_algo_health_report.py" in text
    assert (
        "PYTHONPATH=. pytest tests/test_failure_reporter.py tests/test_algo_health_check.py "
        "tests/test_algo_health_analyzer.py tests/test_codex_pr_validation_workflow.py -v"
    ) in text
    assert "PYTHONPATH=. pytest tests/ -q" in text


def test_post_merge_paper_apply_is_bash3_and_macos_mktemp_compatible() -> None:
    text = (PROJECT_ROOT / "scripts" / "post_merge_paper_apply.sh").read_text(
        encoding="utf-8"
    )
    assert "mapfile" not in text
    assert 'mktemp "/tmp/' not in text
    assert "portable_mktemp()" in text
    assert "read_lines_into_array()" in text
    assert 'mktemp "${tmpdir}/${name}.XXXXXX"' in text
    assert 'final_path="${path}${suffix}"' in text
    assert 'uname -s 2>/dev/null' in text
    assert "skipping paper service restart and continuing smoke checks" in text


def test_operations_docs_include_local_codex_processor_validation_sentence() -> None:
    text = DOCS.read_text(encoding="utf-8")

    assert "Local Codex processor validation test." in text


def test_local_codex_issue_processor_safety_boundaries() -> None:
    text = _script_text().lower()
    forbidden = [
        "openai_api_key",
        "gh pr merge",
        "systemctl restart",
        "/etc/algo.env",
        "alpaca_live",
        "apca_api_key_id",
        "apca_api_secret_key",
    ]
    for needle in forbidden:
        assert needle not in text
    assert "gh issue edit" in text
    assert "codex-validation-failed" in text


def test_local_codex_issue_processor_dry_run_specific_issue_smoke() -> None:
    proc = subprocess.run(
        [str(SCRIPT), "--dry-run", "--issue", "120"],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0
    assert "Preparing issue #120" in proc.stdout
    assert "[dry-run]" in proc.stdout


def test_local_codex_user_systemd_files_exist_and_are_user_level() -> None:
    assert SERVICE.exists()
    assert TIMER.exists()
    service = SERVICE.read_text(encoding="utf-8")
    timer = TIMER.read_text(encoding="utf-8")
    assert "/opt/algosphere/algo-ai-trading-agent/scripts/process_codex_issues_local.sh --limit 1" in service
    assert "WorkingDirectory=/opt/algosphere/algo-ai-trading-agent" in service
    assert "OnUnitActiveSec=30min" in timer
    assert "WantedBy=timers.target" in timer
    assert "systemctl restart" not in service.lower()
    assert "systemctl restart" not in timer.lower()


def test_operations_docs_cover_zero_api_cost_local_processor() -> None:
    text = DOCS.read_text(encoding="utf-8")
    assert "zero API cost local automation" in text
    assert "ChatGPT authentication" in text
    assert "OPENAI_API_KEY is not required" in text
    assert "should remain disabled" in text
    assert "codex-pr-validation.yml" in text
    assert "scripts/process_codex_issues_local.sh --dry-run --issue 120" in text
    assert "systemctl --user enable --now algo-local-codex-processor.timer" in text
    assert "Environment-routed ownership" in text
    assert "processor:mac-paper" in text
    assert "processor:live-linux" in text
    assert "ISSUE_SKIP issue_number=... reason=environment_mismatch" in text


def test_legacy_actions_codex_auto_fix_job_is_disabled() -> None:
    text = LEGACY_ACTIONS_WORKFLOW.read_text(encoding="utf-8")
    assert "false &&" in text
    assert "scripts/process_codex_issues_local.sh" in text
