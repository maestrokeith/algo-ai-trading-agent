from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "verify_codex_fix_health.sh"
DOCS = PROJECT_ROOT / "docs" / "OPERATIONS.md"
PAPER_OPTIONS_LOOP = PROJECT_ROOT / "scripts" / "run_paper_options_stabilization_loop.sh"


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _seed_paper_options_repo(tmp_path: Path, *, healthy: bool, gh: str | None = None) -> tuple[Path, Path]:
    root = tmp_path / "paper-options-repo"
    (root / "bin").mkdir(parents=True)
    (root / "data" / "review" / "2026-06-14").mkdir(parents=True)
    if healthy:
        algo_script = "\n".join(
            [
                "#!/usr/bin/env bash",
                "echo 'OPTIONS_CONFIG enabled=true mode=paper_only'",
                "echo 'OPTION_SIGNAL symbol=QQQ underlying=QQQ direction=bullish'",
                "echo 'OPTION_CHAIN_LOADED symbol=QQQ right=call chain_rows=4 path=ranked_budget'",
                "echo 'OPTION_SELECTED symbol=QQQ right=call contract=QQQ260630C00350000'",
                "echo 'PASS paper options diagnostics user=paper_bot symbol=QQQ chain_source=mock options_placed=0'",
                "exit 0",
                "",
            ]
        )
    else:
        algo_script = "#!/usr/bin/env bash\necho 'PAPER_OPTIONS_DIAGNOSTICS_FAILED RuntimeError: bad chain' >&2\nexit 2\n"
    _write_executable(root / "bin" / "algo", algo_script)
    (root / "data" / "review" / "2026-06-14" / "paper_full.log").write_text(
        "OPTION_ROUTE_CHECK symbol=QQQ route=paper_options\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "paper-options-fake-bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "hostname", "#!/usr/bin/env bash\necho paper-mac\n")
    if gh is not None:
        _write_executable(fake_bin / "gh", gh)
    return root, fake_bin


def _run_paper_options_postfix(
    root: Path,
    fake_bin: Path,
    state_dir: Path,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "PAPER_OPTIONS_HEALTH_SCRIPT": str(PROJECT_ROOT / "scripts" / "check_paper_options_health.sh"),
        "PAPER_OPTIONS_HEALTH_STATE_DIR": str(state_dir / "health"),
        "PAPER_OPTIONS_STABILIZATION_STATE_DIR": str(state_dir / "loop"),
    }
    return subprocess.run(
        [str(PAPER_OPTIONS_LOOP), "--env", "paper", *args],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )


def _seed_repo(tmp_path: Path, *, health_report: str, gh: str | None = None) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    scripts_dir = root / "scripts"
    scripts_dir.mkdir(parents=True)
    (root / "bin").mkdir()
    _write_executable(
        scripts_dir / "check_algo_health.sh",
        "#!/usr/bin/env bash\n"
        "env_name=LIVE\n"
        "while [[ $# -gt 0 ]]; do\n"
        "  case \"$1\" in\n"
        "    --env) env_name=\"${2^^}\"; shift 2 ;;\n"
        "    LIVE|live|PAPER|paper) env_name=\"${1^^}\"; shift ;;\n"
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        "report_path=\"${ALGO_HEALTH_REPORT_PATH:-/tmp/algo_health_report_${env_name}.md}\"\n"
        "cat > \"$report_path\" <<'REPORT'\n"
        f"{health_report}\n"
        "REPORT\n"
        "echo \"Health report written to /tmp/algo_health_report.md\"\n",
    )
    _write_executable(root / "bin" / "algo", "#!/usr/bin/env bash\nexit 0\n")

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "hostname", "#!/usr/bin/env bash\necho verify-host\n")
    if gh is not None:
        _write_executable(fake_bin / "gh", gh)
    return root, fake_bin


def _run_verify(root: Path, fake_bin: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
    }
    return subprocess.run(
        [str(SCRIPT), *args],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )


def _report(*, severity: str, condition: str, detail: str = "detail", suppressed: str = "none") -> str:
    return "\n".join(
        [
            f"# HEALTH [LIVE] {condition} 2026-06-14",
            "",
            "- Environment: LIVE",
            f"- Severity: {severity}",
            f"- Detected Condition: {condition}",
            f"- Detail: {detail}",
            "- market_closed=true",
            "- stale_premarket_artifacts_suppressed=true",
            "- suppression_reason=weekend_market_closed",
            "",
            "## Suppressed Conditions",
            "",
            "```",
            suppressed,
            "```",
        ]
    )


def test_postfix_verifier_script_exists_and_parses() -> None:
    assert SCRIPT.exists()
    assert SCRIPT.stat().st_mode & 0o111
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], text=True, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr


def test_postfix_healthy_does_not_create_issue(tmp_path: Path) -> None:
    root, fake_bin = _seed_repo(
        tmp_path,
        health_report=_report(
            severity="none",
            condition="healthy",
            detail="no health issue detected",
            suppressed="premarket artifacts stale|latest_rankings.json age_minutes=3000|weekend_market_closed",
        ),
        gh="#!/usr/bin/env bash\nexit 42\n",
    )

    proc = _run_verify(root, fake_bin, "--env", "live", "--issue", "120", "--pr", "45")

    assert proc.returncode == 0
    assert "POST_FIX_VERIFICATION status=healthy env=live" in proc.stdout


def test_postfix_unhealthy_creates_environment_issue(tmp_path: Path) -> None:
    calls = tmp_path / "gh_calls.log"
    root, fake_bin = _seed_repo(
        tmp_path,
        health_report=_report(severity="medium", condition="dynamic scanner producing no candidates"),
        gh=(
            "#!/usr/bin/env bash\n"
            f"echo \"$@\" >> {calls}\n"
            "if [[ \"$1 $2\" == \"issue list\" ]]; then printf '[]\\n'; exit 0; fi\n"
            "if [[ \"$1 $2\" == \"issue create\" ]]; then exit 0; fi\n"
        ),
    )

    proc = _run_verify(root, fake_bin, "--env", "live", "--issue", "120", "--pr", "45")

    assert proc.returncode == 0
    assert "POST_FIX_VERIFICATION status=unhealthy env=live" in proc.stdout
    gh_calls = calls.read_text(encoding="utf-8")
    assert "--title POSTFIX [LIVE] repair still unhealthy after PR #45" in gh_calls
    assert "--label codex" in gh_calls
    assert "--label auto-fix" in gh_calls
    assert "--label algo-health" in gh_calls
    assert "--label environment:live" in gh_calls
    assert "--label processor:live-linux" in gh_calls
    body_file = gh_calls.split("--body-file ", 1)[1].split()[0]
    body = Path(body_file).read_text(encoding="utf-8")
    assert "Original Issue: #120" in body
    assert "Repair PR: #45" in body
    assert "Environment: LIVE" in body
    assert "Hostname: verify-host" in body
    assert "dynamic scanner producing no candidates" in body
    assert "Suppressed market-closed stale premarket artifacts should be ignored." in body


def test_postfix_paper_issue_gets_mac_processor_label(tmp_path: Path) -> None:
    calls = tmp_path / "gh_calls.log"
    root, fake_bin = _seed_repo(
        tmp_path,
        health_report=_report(severity="medium", condition="options engine inactive"),
        gh=(
            "#!/usr/bin/env bash\n"
            f"echo \"$@\" >> {calls}\n"
            "if [[ \"$1 $2\" == \"issue list\" ]]; then printf '[]\\n'; exit 0; fi\n"
            "if [[ \"$1 $2\" == \"issue create\" ]]; then exit 0; fi\n"
        ),
    )

    proc = _run_verify(root, fake_bin, "--env", "paper", "--issue", "120", "--pr", "45")

    assert proc.returncode == 0
    assert "POST_FIX_VERIFICATION status=unhealthy env=paper" in proc.stdout
    gh_calls = calls.read_text(encoding="utf-8")
    assert "--label environment:paper" in gh_calls
    assert "--label processor:mac-paper" in gh_calls


def test_postfix_dry_run_does_not_create_issue(tmp_path: Path) -> None:
    root, fake_bin = _seed_repo(
        tmp_path,
        health_report=_report(severity="medium", condition="dynamic scanner producing no candidates"),
        gh="#!/usr/bin/env bash\nexit 42\n",
    )

    proc = _run_verify(root, fake_bin, "--env", "paper", "--issue", "120", "--pr", "45", "--dry-run")

    assert proc.returncode == 0
    assert "POST_FIX_VERIFICATION status=unhealthy env=paper" in proc.stdout
    assert "Dry run: would create GitHub issue: POSTFIX [PAPER] repair still unhealthy after PR #45" in proc.stdout


def test_postfix_duplicate_detection_skips_create(tmp_path: Path) -> None:
    calls = tmp_path / "gh_calls.log"
    root, fake_bin = _seed_repo(
        tmp_path,
        health_report=_report(severity="critical", condition="service down"),
        gh=(
            "#!/usr/bin/env bash\n"
            f"echo \"$@\" >> {calls}\n"
            "if [[ \"$1 $2\" == \"issue list\" ]]; then\n"
            "  printf '[{\"number\":99,\"body\":\"postfix:live:issue-120:pr-45\"}]\\n'\n"
            "  exit 0\n"
            "fi\n"
            "if [[ \"$1 $2\" == \"issue create\" ]]; then exit 42; fi\n"
        ),
    )

    proc = _run_verify(root, fake_bin, "--env", "live", "--issue", "120", "--pr", "45")

    assert proc.returncode == 0
    assert "Existing POSTFIX issue found for postfix:live:issue-120:pr-45" in proc.stdout
    gh_calls = calls.read_text(encoding="utf-8")
    assert "issue list" in gh_calls
    assert "issue create" not in gh_calls


def test_postfix_suppressed_weekend_stale_artifacts_do_not_mark_unhealthy(tmp_path: Path) -> None:
    root, fake_bin = _seed_repo(
        tmp_path,
        health_report=_report(
            severity="none",
            condition="healthy",
            detail="no health issue detected",
            suppressed="premarket artifacts stale|latest_event_feed.json age_minutes=3000|weekend_market_closed",
        ),
        gh="#!/usr/bin/env bash\nexit 42\n",
    )

    proc = _run_verify(root, fake_bin, "--env", "live", "--issue", "120", "--pr", "45")

    assert proc.returncode == 0
    assert "POST_FIX_VERIFICATION status=healthy env=live" in proc.stdout


def test_postfix_service_down_still_marks_unhealthy(tmp_path: Path) -> None:
    calls = tmp_path / "gh_calls.log"
    root, fake_bin = _seed_repo(
        tmp_path,
        health_report=_report(
            severity="critical",
            condition="service down",
            suppressed="premarket artifacts stale|latest_event_feed.json age_minutes=3000|weekend_market_closed",
        ),
        gh=(
            "#!/usr/bin/env bash\n"
            f"echo \"$@\" >> {calls}\n"
            "if [[ \"$1 $2\" == \"issue list\" ]]; then printf '[]\\n'; exit 0; fi\n"
            "if [[ \"$1 $2\" == \"issue create\" ]]; then exit 0; fi\n"
        ),
    )

    proc = _run_verify(root, fake_bin, "--env", "live", "--issue", "120", "--pr", "45")

    assert proc.returncode == 0
    assert "POST_FIX_VERIFICATION status=unhealthy env=live" in proc.stdout
    assert "--title POSTFIX [LIVE] repair still unhealthy after PR #45" in calls.read_text(encoding="utf-8")


def test_paper_options_postfix_unhealthy_creates_followup(tmp_path: Path) -> None:
    calls = tmp_path / "gh_calls.log"
    root, fake_bin = _seed_paper_options_repo(
        tmp_path,
        healthy=False,
        gh=(
            "#!/usr/bin/env bash\n"
            f"echo \"$@\" >> {calls}\n"
            "if [[ \"$1 $2\" == 'issue list' ]]; then printf '[]\\n'; exit 0; fi\n"
            "if [[ \"$1 $2\" == 'issue create' ]]; then exit 0; fi\n"
        ),
    )

    proc = _run_paper_options_postfix(
        root,
        fake_bin,
        tmp_path / "paper-options-state",
        "--postfix-pr",
        "77",
        "--issue",
        "131",
    )

    assert proc.returncode == 1
    assert "PAPER_OPTIONS_STABILIZATION status=postfix_unhealthy" in proc.stdout
    gh_calls = calls.read_text(encoding="utf-8")
    assert "--title POSTFIX [PAPER_OPTIONS] still unstable after PR #77" in gh_calls
    assert "--label environment:paper" in gh_calls
    assert "--label processor:mac-paper" in gh_calls
    assert "--label paper-options" in gh_calls
    body_file = gh_calls.split("--body-file ", 1)[1].split()[0]
    body = Path(body_file).read_text(encoding="utf-8")
    assert "Paper options remain unstable after repair PR #77." in body
    assert "Original issue: #131" in body
    assert "environment=paper" in body


def test_paper_options_postfix_stable_creates_no_issue(tmp_path: Path) -> None:
    root, fake_bin = _seed_paper_options_repo(
        tmp_path,
        healthy=True,
        gh="#!/usr/bin/env bash\necho unexpected-gh >&2\nexit 42\n",
    )
    state = tmp_path / "paper-options-state"
    stable_file = state / "health" / "stable_ticks"
    stable_file.parent.mkdir(parents=True)
    stable_file.write_text("2\n", encoding="utf-8")

    proc = _run_paper_options_postfix(
        root,
        fake_bin,
        state,
        "--postfix-pr",
        "77",
        "--issue",
        "131",
    )

    assert proc.returncode == 0
    assert "PAPER_OPTIONS_STABILIZATION status=stable" in proc.stdout
    assert "unexpected-gh" not in proc.stderr


def test_operations_docs_cover_postfix_health_verification() -> None:
    text = DOCS.read_text(encoding="utf-8")
    assert "Post-Fix Health Verification" in text
    assert "scripts/verify_codex_fix_health.sh --env live --issue 120 --pr 45 --dry-run" in text
    assert "issue -> Codex PR -> validation -> merge -> health verification" in text
