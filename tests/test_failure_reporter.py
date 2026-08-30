from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from datetime import date, datetime
from pathlib import Path

import pytest

from scripts import run_self_heal
from src.review_logs import market_day


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "report_algo_failure_to_github.sh"
SYSTEMD_UNIT = PROJECT_ROOT / "deploy" / "systemd" / "algo-failure-reporter@.service"


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _seed_repo(root: Path) -> None:
    root.mkdir()
    (root / "bin").mkdir()
    (root / "tests").mkdir()
    _write_executable(root / "bin" / "algo", "#!/usr/bin/env bash\necho bin-algo \"$@\"\nexit 0\n")


def _seed_paper_review_log(root: Path, text: str) -> Path:
    path = root / "data" / "review" / date.today().isoformat() / "paper_full.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _fake_failure_bin(tmp_path: Path, *, gh: str | None = None, uname: str | None = None, hostname: str | None = None) -> Path:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "systemctl",
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"is-active\" ]]; then echo failed; exit 3; fi\n"
        "if [[ \"$1\" == \"is-failed\" ]]; then echo failed; exit 0; fi\n"
        "echo 'Active: failed (Result: exit-code)'\n",
    )
    _write_executable(fake_bin / "journalctl", "#!/usr/bin/env bash\necho 'Traceback allocator exception'\n")
    _write_executable(fake_bin / "timeout", "#!/usr/bin/env bash\necho pytest skipped in test\nexit 0\n")
    if gh is not None:
        _write_executable(fake_bin / "gh", gh)
    if uname is not None:
        _write_executable(fake_bin / "uname", f"#!/usr/bin/env bash\necho {uname!r}\n")
    if hostname is not None:
        _write_executable(fake_bin / "hostname", f"#!/usr/bin/env bash\necho {hostname!r}\n")
    return fake_bin


def _fake_path(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    _write_executable(
        fake_bin / "systemctl",
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"is-active\" ]]; then echo active; exit 0; fi\n"
        "if [[ \"$1\" == \"is-failed\" ]]; then echo active; exit 1; fi\n"
        "echo 'Active: active (running)'\n",
    )
    _write_executable(fake_bin / "journalctl", "#!/usr/bin/env bash\necho 'INFO healthy'\n")
    _write_executable(fake_bin / "timeout", "#!/usr/bin/env bash\necho pytest skipped in test\nexit 0\n")
    _write_executable(fake_bin / "free", "#!/usr/bin/env bash\necho 'memory unavailable in test'\n")
    return fake_bin


def _seed_portable_script_repo(root: Path, script: Path = SCRIPT) -> Path:
    scripts_dir = root / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(script, scripts_dir / script.name)
    (scripts_dir / script.name).chmod(script.stat().st_mode | stat.S_IXUSR)
    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        "echo portable-root:$PWD\n"
        "echo bin-algo \"$@\"\n"
        "exit 0\n",
    )
    return scripts_dir / script.name


def test_failure_reporter_script_exists_and_is_executable() -> None:
    assert SCRIPT.exists()
    assert os.access(SCRIPT, os.X_OK)


def test_failure_reporter_script_has_no_hardcoded_fedora_repo_root() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "/opt/algosphere/algo-ai-trading-agent" not in text
    assert 'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"' in text
    assert 'ROOT="${ALGO_REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"' in text


def test_failure_reporter_resolves_repo_root_from_script_location_for_mac_paper(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Users" / "psuriset" / "cursor" / "algo"
    script = _seed_portable_script_repo(root)
    fake_bin = _fake_path(tmp_path)
    report_path = tmp_path / "algo_failure_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_FAILURE_REPORT_PATH": str(report_path),
        "ALGO_FAILURE_PYTEST_TIMEOUT": "1",
    }
    env.pop("ALGO_REPO_ROOT", None)

    proc = subprocess.run(
        [str(script), "--dry-run", "PAPER"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
        cwd=tmp_path,
    )

    assert proc.returncode == 0
    assert "Repo root not found" not in proc.stderr
    text = report_path.read_text(encoding="utf-8")
    assert "- Environment: PAPER" in text
    assert f"- Repo: {root}" in text
    assert f"portable-root:{root}" in text


def test_failure_reporter_paper_mac_missing_review_log_is_reportable(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root)
    fake_bin = _fake_path(tmp_path)
    _write_executable(fake_bin / "uname", "#!/usr/bin/env bash\necho Darwin\n")
    _write_executable(
        fake_bin / "date",
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"+%Y-%m-%d\" ]]; then echo 2026-06-14; exit 0; fi\n"
        "if [[ \"$1\" == \"-u\" ]]; then echo 2026-06-14T12:00:00Z; exit 0; fi\n"
        "/bin/date \"$@\"\n",
    )
    report_path = tmp_path / "algo_failure_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_FAILURE_REPORT_PATH": str(report_path),
        "ALGO_FAILURE_PYTEST_TIMEOUT": "1",
    }

    proc = subprocess.run(
        [str(SCRIPT), "--dry-run", "PAPER"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    review_dir = root / "data" / "review" / "2026-06-14"
    assert review_dir.is_dir()
    assert not (review_dir / "paper_full.log").exists()
    text = report_path.read_text(encoding="utf-8")
    assert "- Severity: medium" in text
    assert "PAPER_REVIEW_LOG_MISSING" in text
    assert "No reportable failure detected for PAPER" not in proc.stdout


def test_failure_reporter_resolves_repo_root_from_script_location_for_fedora_live(
    tmp_path: Path,
) -> None:
    root = tmp_path / "home" / "algosphere" / "algo"
    script = _seed_portable_script_repo(root)
    fake_bin = _fake_path(tmp_path)
    report_path = tmp_path / "algo_failure_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_FAILURE_REPORT_PATH": str(report_path),
        "ALGO_FAILURE_PYTEST_TIMEOUT": "1",
    }
    env.pop("ALGO_REPO_ROOT", None)

    proc = subprocess.run(
        [str(script), "--dry-run", "LIVE"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
        cwd=tmp_path,
    )

    assert proc.returncode == 0
    assert "Repo root not found" not in proc.stderr
    text = report_path.read_text(encoding="utf-8")
    assert "- Environment: LIVE" in text
    assert f"- Repo: {root}" in text
    assert f"portable-root:{root}" in text


def test_failure_reporter_algo_repo_root_override_still_wins(tmp_path: Path) -> None:
    script_root = tmp_path / "wrong" / "repo"
    override_root = tmp_path / "override" / "repo"
    script = _seed_portable_script_repo(script_root)
    _seed_portable_script_repo(override_root)
    fake_bin = _fake_path(tmp_path)
    report_path = tmp_path / "algo_failure_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(override_root),
        "ALGO_FAILURE_REPORT_PATH": str(report_path),
        "ALGO_FAILURE_PYTEST_TIMEOUT": "1",
    }

    proc = subprocess.run(
        [str(script), "--dry-run", "PAPER"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
        cwd=tmp_path,
    )

    assert proc.returncode == 0
    text = report_path.read_text(encoding="utf-8")
    assert f"- Repo: {override_root}" in text
    assert f"portable-root:{override_root}" in text
    assert f"portable-root:{script_root}" not in text


def test_failure_reporter_systemd_unit_references_script() -> None:
    assert SYSTEMD_UNIT.exists()
    text = SYSTEMD_UNIT.read_text(encoding="utf-8")
    assert "ExecStart=/opt/algosphere/algo-ai-trading-agent/scripts/report_algo_failure_to_github.sh %i" in text
    assert "NoNewPrivileges=true" in text


def test_failure_reporter_dry_run_generates_live_report(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "systemctl",
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"is-active\" ]]; then echo failed; exit 3; fi\n"
        "if [[ \"$1\" == \"is-failed\" ]]; then echo failed; exit 0; fi\n"
        "echo 'Active: failed (Result: exit-code)'\n",
    )
    _write_executable(
        fake_bin / "journalctl",
        "#!/usr/bin/env bash\n"
        "echo 'CRITICAL allocator exception stack trace'\n",
    )
    _write_executable(fake_bin / "timeout", "#!/usr/bin/env bash\necho pytest skipped in test\nexit 0\n")

    report_path = tmp_path / "algo_failure_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_FAILURE_REPORT_PATH": str(report_path),
        "ALGO_FAILURE_PYTEST_TIMEOUT": "1",
    }
    proc = subprocess.run(
        [str(SCRIPT), "--dry-run", "LIVE", "algo.service"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    assert "Dry run: would create GitHub issue: AUTOFAIL [LIVE]" in proc.stdout
    text = report_path.read_text(encoding="utf-8")
    assert "- Environment: LIVE" in text
    assert "- Severity: critical" in text
    assert "- User: live_bot" in text
    assert "CRITICAL allocator exception stack trace" in text


def test_failure_reporter_ignores_prior_paper_options_health_metadata(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root)
    _seed_paper_review_log(
        root,
        "\n".join(
            [
                "PAPER_OPTIONS_HEALTH issue=critical_options_errors",
                "fingerprint=paper-options:critical_options_errors",
                "PAPER_OPTIONS [PAPER] unstable: critical_options_errors",
                "POSTFIX [PAPER_OPTIONS] still unstable after PR #77 critical_options_errors",
                "OPTION_ROUTE_CHECK symbol=QQQ route=paper_options",
            ]
        ),
    )
    fake_bin = _fake_path(tmp_path)
    _write_executable(
        fake_bin / "journalctl",
        "#!/usr/bin/env bash\n"
        "cat <<'LOG'\n"
        "PAPER_OPTIONS_HEALTH issue=critical_options_errors\n"
        "fingerprint=paper-options:critical_options_errors\n"
        "PAPER_OPTIONS [PAPER] unstable: critical_options_errors\n"
        "POSTFIX [PAPER_OPTIONS] still unstable after PR #77 critical_options_errors\n"
        "OPTION_ROUTE_CHECK symbol=QQQ route=paper_options\n"
        "LOG\n",
    )
    report_path = tmp_path / "algo_failure_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_FAILURE_REPORT_PATH": str(report_path),
        "ALGO_FAILURE_PYTEST_TIMEOUT": "1",
    }

    proc = subprocess.run(
        [str(SCRIPT), "--dry-run", "--env", "paper"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    assert "No reportable failure detected for PAPER" in proc.stdout
    text = report_path.read_text(encoding="utf-8")
    assert "- Severity: none" in text
    assert "PAPER_OPTIONS_HEALTH issue=critical_options_errors" in text
    matched = text.split("## Matched Failure Signals", 1)[1].split("## Diagnostics", 1)[0]
    assert "critical_options_errors" not in matched


def test_failure_reporter_preserves_real_critical_option_errors(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root)
    _seed_paper_review_log(root, "CRITICAL option chain loader failed for QQQ\n")
    fake_bin = _fake_path(tmp_path)
    _write_executable(
        fake_bin / "journalctl",
        "#!/usr/bin/env bash\n"
        "echo 'CRITICAL option chain loader failed for QQQ'\n",
    )
    report_path = tmp_path / "algo_failure_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_FAILURE_REPORT_PATH": str(report_path),
        "ALGO_FAILURE_PYTEST_TIMEOUT": "1",
    }

    proc = subprocess.run(
        [str(SCRIPT), "--dry-run", "--env", "paper"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    assert "Dry run: would create GitHub issue: AUTOFAIL [PAPER] CRITICAL option chain loader failed for QQQ" in proc.stdout
    text = report_path.read_text(encoding="utf-8")
    assert "- Severity: critical" in text
    assert "CRITICAL option chain loader failed for QQQ" in text


def test_failure_reporter_duplicate_issue_detection_skips_create(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root)

    calls = tmp_path / "gh_calls.log"
    fake_bin = _fake_failure_bin(
        tmp_path,
        gh=(
            "#!/usr/bin/env bash\n"
            f"echo \"$@\" >> {calls}\n"
            "if [[ \"$1 $2\" == \"issue list\" ]]; then\n"
            "  printf '[{\"number\":110,\"title\":\"existing autofail\"}]\\n'\n"
            "  exit 0\n"
            "fi\n"
            "if [[ \"$1 $2\" == \"issue create\" ]]; then\n"
            "  echo unexpected-create >&2\n"
            "  exit 42\n"
            "fi\n"
        ),
    )

    report_path = tmp_path / "algo_failure_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_FAILURE_REPORT_PATH": str(report_path),
        "ALGO_FAILURE_PYTEST_TIMEOUT": "1",
    }
    proc = subprocess.run(
        [str(SCRIPT), "--env", "live", "algo.service"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    assert "Existing issue found" in proc.stdout
    gh_calls = calls.read_text(encoding="utf-8")
    assert "issue list" in gh_calls
    assert "issue create" not in gh_calls
    assert "- Environment: LIVE" in report_path.read_text(encoding="utf-8")


def test_failure_reporter_no_args_checks_live_and_paper_in_dry_run(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "bin").mkdir()
    (root / "tests").mkdir()
    _write_executable(root / "bin" / "algo", "#!/usr/bin/env bash\necho bin-algo \"$@\"\nexit 0\n")

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "systemctl",
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"is-active\" ]]; then echo active; exit 0; fi\n"
        "if [[ \"$1\" == \"is-failed\" ]]; then echo active; exit 1; fi\n"
        "echo 'Active: active (running)'\n",
    )
    _write_executable(fake_bin / "journalctl", "#!/usr/bin/env bash\necho 'INFO healthy'\n")
    _write_executable(fake_bin / "timeout", "#!/usr/bin/env bash\necho pytest skipped in test\nexit 0\n")

    report_path = tmp_path / "algo_failure_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_FAILURE_REPORT_PATH": str(report_path),
        "ALGO_FAILURE_PYTEST_TIMEOUT": "1",
    }
    proc = subprocess.run(
        [str(SCRIPT), "--dry-run", "--env", "live"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    assert "No reportable failure detected for LIVE" in proc.stdout
    text = report_path.read_text(encoding="utf-8")
    assert "- Environment: LIVE" in text


def test_failure_reporter_macos_defaults_to_paper(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root)
    fake_bin = _fake_failure_bin(tmp_path, uname="Darwin", hostname="macbook")
    report_path = tmp_path / "algo_failure_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_FAILURE_REPORT_PATH": str(report_path),
        "ALGO_FAILURE_PYTEST_TIMEOUT": "1",
    }

    proc = subprocess.run(
        [str(SCRIPT), "--dry-run"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    assert "Dry run: would create GitHub issue: AUTOFAIL [PAPER]" in proc.stdout
    text = report_path.read_text(encoding="utf-8")
    assert "- Environment: PAPER" in text
    assert "- environment=paper" in text
    assert "- Hostname: macbook" in text
    assert "- Service Name: paper.service" in text
    assert "- Failure Source: failure-reporter" in text


def test_failure_reporter_fedora_host_defaults_to_live(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root)
    fake_bin = _fake_failure_bin(tmp_path, uname="Linux", hostname="algosphere-live-host")
    report_path = tmp_path / "algo_failure_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_FAILURE_REPORT_PATH": str(report_path),
        "ALGO_FAILURE_PYTEST_TIMEOUT": "1",
    }

    proc = subprocess.run(
        [str(SCRIPT), "--dry-run"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    assert "Dry run: would create GitHub issue: AUTOFAIL [LIVE]" in proc.stdout
    text = report_path.read_text(encoding="utf-8")
    assert "- Environment: LIVE" in text
    assert "- environment=live" in text
    assert "- Hostname: algosphere-live-host" in text


def test_failure_reporter_explicit_env_overrides_default(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root)
    fake_bin = _fake_failure_bin(tmp_path, uname="Darwin", hostname="macbook")
    report_path = tmp_path / "algo_failure_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_FAILURE_REPORT_PATH": str(report_path),
        "ALGO_FAILURE_PYTEST_TIMEOUT": "1",
    }

    proc = subprocess.run(
        [str(SCRIPT), "--dry-run", "--env", "live"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    assert "Dry run: would create GitHub issue: AUTOFAIL [LIVE]" in proc.stdout
    assert "- Environment: LIVE" in report_path.read_text(encoding="utf-8")


def test_failure_reporter_labels_and_title_are_environment_specific(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root)
    _seed_paper_review_log(root, "Traceback allocator exception\n")
    calls = tmp_path / "gh_calls.log"
    fake_bin = _fake_failure_bin(
        tmp_path,
        gh=(
            "#!/usr/bin/env bash\n"
            f"echo \"$@\" >> {calls}\n"
            "if [[ \"$1 $2\" == \"issue list\" ]]; then\n"
            "  printf '[]\\n'\n"
            "  exit 0\n"
            "fi\n"
            "if [[ \"$1 $2\" == \"issue create\" ]]; then\n"
            "  exit 0\n"
            "fi\n"
        ),
    )
    report_path = tmp_path / "algo_failure_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_FAILURE_REPORT_PATH": str(report_path),
        "ALGO_FAILURE_PYTEST_TIMEOUT": "1",
    }

    proc = subprocess.run(
        [str(SCRIPT), "--env", "paper"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    gh_calls = calls.read_text(encoding="utf-8")
    assert "--title AUTOFAIL [PAPER] Traceback allocator exception" in gh_calls
    assert "--label environment:paper" in gh_calls
    assert "--label processor:mac-paper" in gh_calls
    assert "--label severity:critical" in gh_calls


def test_failure_reporter_live_issue_gets_fedora_processor_label(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root)
    calls = tmp_path / "gh_calls.log"
    fake_bin = _fake_failure_bin(
        tmp_path,
        gh=(
            "#!/usr/bin/env bash\n"
            f"echo \"$@\" >> {calls}\n"
            "if [[ \"$1 $2\" == \"issue list\" ]]; then printf '[]\\n'; exit 0; fi\n"
            "if [[ \"$1 $2\" == \"issue create\" ]]; then exit 0; fi\n"
        ),
    )
    report_path = tmp_path / "algo_failure_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_FAILURE_REPORT_PATH": str(report_path),
        "ALGO_FAILURE_PYTEST_TIMEOUT": "1",
    }

    proc = subprocess.run(
        [str(SCRIPT), "--env", "live"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    gh_calls = calls.read_text(encoding="utf-8")
    assert "--label environment:live" in gh_calls
    assert "--label processor:live-linux" in gh_calls


def test_failure_reporter_paper_duplicate_detection_only_matches_paper(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root)
    calls = tmp_path / "gh_calls.log"
    fake_bin = _fake_failure_bin(
        tmp_path,
        gh=(
            "#!/usr/bin/env bash\n"
            f"echo \"$@\" >> {calls}\n"
            "if [[ \"$1 $2\" == \"issue list\" ]]; then\n"
            "  printf '[{\"number\":110,\"body\":\"autofail:live:critical:traceback-allocator-exception\"}]\\n'\n"
            "  exit 0\n"
            "fi\n"
            "if [[ \"$1 $2\" == \"issue create\" ]]; then\n"
            "  exit 0\n"
            "fi\n"
        ),
    )
    report_path = tmp_path / "algo_failure_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_FAILURE_REPORT_PATH": str(report_path),
        "ALGO_FAILURE_PYTEST_TIMEOUT": "1",
    }

    proc = subprocess.run(
        [str(SCRIPT), "--env", "paper"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    gh_calls = calls.read_text(encoding="utf-8")
    assert "issue list" in gh_calls
    assert "issue create" in gh_calls


def test_failure_reporter_live_duplicate_detection_only_matches_live(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root)
    calls = tmp_path / "gh_calls.log"
    fake_bin = _fake_failure_bin(
        tmp_path,
        gh=(
            "#!/usr/bin/env bash\n"
            f"echo \"$@\" >> {calls}\n"
            "if [[ \"$1 $2\" == \"issue list\" ]]; then\n"
            "  printf '[{\"number\":110,\"body\":\"autofail:paper:critical:traceback-allocator-exception\"}]\\n'\n"
            "  exit 0\n"
            "fi\n"
            "if [[ \"$1 $2\" == \"issue create\" ]]; then\n"
            "  exit 0\n"
            "fi\n"
        ),
    )
    report_path = tmp_path / "algo_failure_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_FAILURE_REPORT_PATH": str(report_path),
        "ALGO_FAILURE_PYTEST_TIMEOUT": "1",
    }

    proc = subprocess.run(
        [str(SCRIPT), "--env", "live"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    gh_calls = calls.read_text(encoding="utf-8")
    assert "issue list" in gh_calls
    assert "issue create" in gh_calls


class _SelfHealFakeRunner:
    def __init__(self, responses: dict[tuple[str, ...], run_self_heal.CommandResult] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        args: list[str] | tuple[str, ...],
        *,
        cwd: Path = PROJECT_ROOT,
        check: bool = False,
    ) -> run_self_heal.CommandResult:
        key = tuple(args)
        self.calls.append(key)
        result = self.responses.get(key, run_self_heal.CommandResult(0, "", ""))
        if key[:3] == ("gh", "issue", "create") and key not in self.responses:
            result = run_self_heal.CommandResult(0, "https://github.com/org/repo/issues/144\n", "")
        if check and result.returncode != 0:
            raise RuntimeError(f"fake command failed: {' '.join(key)}")
        return result


class _SelfHealMissingCommandRunner(_SelfHealFakeRunner):
    def run(
        self,
        args: list[str] | tuple[str, ...],
        *,
        cwd: Path = PROJECT_ROOT,
        check: bool = False,
    ) -> run_self_heal.CommandResult:
        key = tuple(args)
        self.calls.append(key)
        if key and key[0] == "journalctl":
            raise FileNotFoundError("journalctl")
        return super().run(args, cwd=cwd, check=check)


def test_self_heal_collect_logs_linux_uses_journalctl(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ALGO_SELF_HEAL_LOG_FILE", raising=False)
    monkeypatch.delenv("ALGO_PAPER_LOG_FILE", raising=False)
    monkeypatch.setenv("ALGO_AUTOOPS_PLATFORM", "Linux")
    runner = _SelfHealFakeRunner(
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


def test_self_heal_collect_logs_darwin_uses_paper_file_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_path = tmp_path / "paper_full.log"
    log_path.write_text("Traceback paper runtime\n", encoding="utf-8")
    monkeypatch.setenv("ALGO_AUTOOPS_PLATFORM", "Darwin")
    monkeypatch.setenv("ALGO_SELF_HEAL_LOG_FILE", str(log_path))
    runner = _SelfHealFakeRunner()

    logs = run_self_heal.collect_logs(runner, "PAPER", "30 minutes ago")

    assert logs == "Traceback paper runtime\n"
    assert runner.calls == []
    out = capsys.readouterr().out
    assert "SELF_HEAL_LOG_SOURCE source=file" in out
    assert str(log_path) in out


def test_self_heal_collect_logs_missing_journalctl_is_graceful(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ALGO_SELF_HEAL_LOG_FILE", raising=False)
    monkeypatch.delenv("ALGO_PAPER_LOG_FILE", raising=False)
    monkeypatch.setenv("ALGO_AUTOOPS_PLATFORM", "Linux")
    runner = _SelfHealMissingCommandRunner()

    logs = run_self_heal.collect_logs(runner, "PAPER", "30 minutes ago")

    assert logs == ""
    assert ("journalctl", "-u", "paper.service", "--since", "30 minutes ago", "--no-pager") in runner.calls
    assert "SELF_HEAL_LOG_SOURCE source=none reason=journalctl_unavailable" in capsys.readouterr().out


def test_self_heal_collect_logs_darwin_missing_logs_does_not_call_journalctl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ALGO_SELF_HEAL_LOG_FILE", raising=False)
    monkeypatch.delenv("ALGO_PAPER_LOG_FILE", raising=False)
    monkeypatch.setenv("ALGO_AUTOOPS_PLATFORM", "Darwin")
    monkeypatch.setattr(run_self_heal, "ROOT", tmp_path)
    runner = _SelfHealMissingCommandRunner()

    logs = run_self_heal.collect_logs(runner, "PAPER", "30 minutes ago")

    today = market_day(datetime.now().astimezone())
    expected = tmp_path / "data" / "review" / today / "paper_full.log"
    assert logs == f"PAPER_REVIEW_LOG_MISSING path={expected}\n"
    assert runner.calls == []
    assert f"SELF_HEAL_LOG_SOURCE source=none reason=missing_review_log path={expected}" in capsys.readouterr().out


def test_self_heal_collect_logs_darwin_uses_today_review_log_not_stale(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ALGO_SELF_HEAL_LOG_FILE", raising=False)
    monkeypatch.delenv("ALGO_PAPER_LOG_FILE", raising=False)
    monkeypatch.setenv("ALGO_AUTOOPS_PLATFORM", "Darwin")
    monkeypatch.setattr(run_self_heal, "ROOT", tmp_path)
    today = market_day(datetime.now().astimezone())
    stale = tmp_path / "data" / "review" / "2026-06-25" / "paper_full.log"
    stale.parent.mkdir(parents=True)
    stale.write_text("Traceback stale paper log\n", encoding="utf-8")
    current = tmp_path / "data" / "review" / today / "paper_full.log"
    current.parent.mkdir(parents=True)
    current.write_text("INFO today paper log\n", encoding="utf-8")
    runner = _SelfHealMissingCommandRunner()

    logs = run_self_heal.collect_logs(runner, "PAPER", "30 minutes ago")

    assert logs == "INFO today paper log\n"
    assert runner.calls == []
    out = capsys.readouterr().out
    assert f"SELF_HEAL_LOG_SOURCE source=file path={current}" in out
    assert str(stale) not in out


def test_self_heal_paper_missing_review_log_reports_degraded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ALGO_SELF_HEAL_LOG_FILE", raising=False)
    monkeypatch.delenv("ALGO_PAPER_LOG_FILE", raising=False)
    monkeypatch.setenv("ALGO_AUTOOPS_PLATFORM", "Darwin")
    monkeypatch.setattr(run_self_heal, "ROOT", tmp_path)
    args = run_self_heal.parse_args(["--paper", "--dry-run"])

    assert run_self_heal.run_self_heal(args, _SelfHealMissingCommandRunner()) == 0

    today = market_day(datetime.now().astimezone())
    expected = tmp_path / "data" / "review" / today / "paper_full.log"
    out = capsys.readouterr().out
    assert f"SELF_HEAL_LOG_SOURCE source=none reason=missing_review_log path={expected}" in out
    assert f"SELF_HEAL status=degraded env=paper reason=missing_review_log path={expected}" in out
    assert expected.parent.is_dir()


def test_self_heal_dynamic_silent_drop_creates_issue_body() -> None:
    logs = "\n".join(
        [
            "2026-06-24T10:00:00 INFO DYNAMIC_SCAN selected=['BLZE', 'SOXS']",
            "2026-06-24T10:00:01 INFO DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=BLZE source=scanner_selected",
            "2026-06-24T10:00:01 INFO DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=SOXS source=scanner_selected",
            "2026-06-24T10:04:30 INFO regime entry policy...",
        ]
    )

    evidence = run_self_heal.detect_failure(logs, "LIVE", "30 minutes ago")

    assert evidence is not None
    assert evidence.short_failure == "missing_terminal_state"
    assert "missing terminal state" in evidence.actual_missing_step
    body = run_self_heal.build_issue_body("LIVE", evidence, "30 minutes ago")
    assert "- Environment: LIVE" in body
    assert "grep command:" in body
    assert "expected flow:" in body
    assert "actual missing step:" in body
    assert "Do not auto-fix strategy thresholds" in body
    assert "BLZE" in body
    assert "SOXS" in body


def test_self_heal_dynamic_selected_entry_eval_start_is_pending(capsys: pytest.CaptureFixture[str]) -> None:
    logs = "\n".join(
        [
            "2026-06-24T10:00:00 INFO DYNAMIC_SCAN selected=['WEN']",
            "2026-06-24T10:00:01 INFO DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=WEN source=scanner_selected",
            "2026-06-24T10:00:02 INFO DYNAMIC_ENTRY_EVAL_START symbol=WEN source=scanner_selected route=dynamic_momentum_override",
        ]
    )
    runner = _SelfHealFakeRunner(
        {
            ("journalctl", "-u", "algo.service", "--since", "30 minutes ago", "--no-pager"): run_self_heal.CommandResult(
                0, logs, ""
            )
        }
    )
    args = run_self_heal.parse_args(["--live"])

    assert run_self_heal.run_self_heal(args, runner) == 0

    assert "SELF_HEAL status=blocked reason=entry_eval_pending" in capsys.readouterr().out
    assert not any(call[:3] == ("gh", "issue", "create") for call in runner.calls)


def test_self_heal_dynamic_selected_entry_eval_pass_is_healthy(capsys: pytest.CaptureFixture[str]) -> None:
    logs = "\n".join(
        [
            "2026-06-24T10:00:00 INFO DYNAMIC_SCAN selected=['RUN']",
            "2026-06-24T10:00:01 INFO DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=RUN source=scanner_selected",
            "2026-06-24T10:00:02 INFO ENTRY_EVAL_PASS symbol=RUN route=dynamic_momentum_override",
        ]
    )
    runner = _SelfHealFakeRunner(
        {
            ("journalctl", "-u", "algo.service", "--since", "30 minutes ago", "--no-pager"): run_self_heal.CommandResult(
                0, logs, ""
            )
        }
    )
    args = run_self_heal.parse_args(["--live"])

    assert run_self_heal.run_self_heal(args, runner) == 0

    assert "SELF_HEAL status=healthy reason=dynamic_entry_eval_observed" in capsys.readouterr().out
    assert not any(call[:3] == ("gh", "issue", "create") for call in runner.calls)


def test_self_heal_dynamic_selected_order_submitted_is_healthy(capsys: pytest.CaptureFixture[str]) -> None:
    logs = "\n".join(
        [
            "2026-06-24T10:00:00 INFO DYNAMIC_SCAN selected=['WEN']",
            "2026-06-24T10:00:01 INFO DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=WEN source=scanner_selected",
            "2026-06-24T10:00:05 INFO ORDER_SUBMITTED symbol=WEN side=buy",
        ]
    )
    runner = _SelfHealFakeRunner(
        {
            ("journalctl", "-u", "algo.service", "--since", "30 minutes ago", "--no-pager"): run_self_heal.CommandResult(
                0, logs, ""
            )
        }
    )
    args = run_self_heal.parse_args(["--live"])

    assert run_self_heal.run_self_heal(args, runner) == 0

    assert "SELF_HEAL status=healthy reason=dynamic_entry_eval_observed" in capsys.readouterr().out
    assert not any(call[:3] == ("gh", "issue", "create") for call in runner.calls)


def test_self_heal_dynamic_selected_business_skip_is_healthy(capsys: pytest.CaptureFixture[str]) -> None:
    logs = "\n".join(
        [
            "2026-06-24T10:00:00 INFO DYNAMIC_SCAN selected=['RUN']",
            "2026-06-24T10:00:01 INFO DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=RUN source=scanner_selected",
            "2026-06-24T10:00:04 INFO DYNAMIC_ENTRY_CANDIDATE_SKIPPED symbol=RUN reason=cooldown",
        ]
    )
    runner = _SelfHealFakeRunner(
        {
            ("journalctl", "-u", "algo.service", "--since", "30 minutes ago", "--no-pager"): run_self_heal.CommandResult(
                0, logs, ""
            )
        }
    )
    args = run_self_heal.parse_args(["--live"])

    assert run_self_heal.run_self_heal(args, runner) == 0

    assert "SELF_HEAL status=healthy reason=dynamic_entry_eval_observed" in capsys.readouterr().out
    assert not any(call[:3] == ("gh", "issue", "create") for call in runner.calls)


def test_self_heal_dynamic_selected_pending_before_grace_is_blocked(capsys: pytest.CaptureFixture[str]) -> None:
    logs = "\n".join(
        [
            "2026-06-24T10:00:00 INFO DYNAMIC_SCAN selected=['WEN']",
            "2026-06-24T10:00:01 INFO DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=WEN source=scanner_selected",
            "2026-06-24T10:01:00 INFO heartbeat healthy",
        ]
    )
    runner = _SelfHealFakeRunner(
        {
            ("journalctl", "-u", "algo.service", "--since", "30 minutes ago", "--no-pager"): run_self_heal.CommandResult(
                0, logs, ""
            )
        }
    )
    args = run_self_heal.parse_args(["--live"])

    assert run_self_heal.run_self_heal(args, runner) == 0

    assert "SELF_HEAL status=blocked reason=entry_eval_pending" in capsys.readouterr().out
    assert not any(call[:3] == ("gh", "issue", "create") for call in runner.calls)


def test_self_heal_dynamic_selected_no_eval_after_grace_is_failure() -> None:
    logs = "\n".join(
        [
            "2026-06-24T10:00:00 INFO DYNAMIC_SCAN selected=['WEN']",
            "2026-06-24T10:00:01 INFO DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=WEN source=scanner_selected",
            "2026-06-24T10:04:15 INFO heartbeat healthy",
        ]
    )

    evidence = run_self_heal.detect_failure(logs, "LIVE", "30 minutes ago")

    assert evidence is not None
    assert evidence.short_failure == "missing_terminal_state"
    assert "WEN" in evidence.actual_missing_step


def test_self_heal_stale_dynamic_missing_eval_ignored_after_newer_recovery_marker(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logs = "\n".join(
        [
            "2026-06-24T10:00:00 INFO DYNAMIC_SCAN selected=['WEN']",
            "2026-06-24T10:00:01 INFO DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=WEN source=scanner_selected",
            "2026-06-24T10:04:15 INFO heartbeat healthy",
            "2026-06-24T10:05:00 INFO DYNAMIC_SCAN selected=['WEN']",
            "2026-06-24T10:05:01 INFO DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=WEN source=scanner_selected",
            "2026-06-24T10:05:03 INFO ENTRY_TO_ALLOCATOR_TRACE symbol=WEN route=dynamic_momentum_override",
        ]
    )
    runner = _SelfHealFakeRunner(
        {
            ("journalctl", "-u", "algo.service", "--since", "30 minutes ago", "--no-pager"): run_self_heal.CommandResult(
                0, logs, ""
            )
        }
    )
    args = run_self_heal.parse_args(["--live"])

    assert run_self_heal.run_self_heal(args, runner) == 0

    assert "SELF_HEAL status=healthy reason=dynamic_entry_eval_observed" in capsys.readouterr().out
    assert not any(call[:3] == ("gh", "issue", "create") for call in runner.calls)


def test_self_heal_duplicate_issue_suppression_comments_instead_of_creating() -> None:
    evidence = run_self_heal.FailureEvidence(
        short_failure="dynamic scanner selected symbols without entry eval",
        expected_flow="expected",
        actual_missing_step="missing",
        grep_command="grep dynamic",
        matching_logs=("DYNAMIC_SCAN selected=['BLZE']",),
        fingerprint="self-heal:live:dynamic:abc123",
    )
    runner = _SelfHealFakeRunner(
        {
            (
                "gh",
                "issue",
                "list",
                "--state",
                "open",
                "--search",
                evidence.fingerprint,
                "--json",
                "number,title,body",
                "--limit",
                "20",
            ): run_self_heal.CommandResult(
                0,
                '[{"number":77,"title":"existing","body":"Fingerprint: self-heal:live:dynamic:abc123"}]',
                "",
            )
        }
    )

    issue = run_self_heal.create_or_update_issue(runner, "LIVE", evidence, "30 minutes ago", dry_run=False)

    assert issue == 77
    assert any(call[:3] == ("gh", "issue", "comment") for call in runner.calls)
    assert not any(call[:3] == ("gh", "issue", "create") for call in runner.calls)


def test_self_heal_bootstraps_missing_labels_before_issue_create() -> None:
    evidence = run_self_heal.FailureEvidence(
        short_failure="dynamic scanner selected symbols without entry eval",
        expected_flow="expected",
        actual_missing_step="missing",
        grep_command="grep dynamic",
        matching_logs=("DYNAMIC_SCAN selected=['WEN']",),
        fingerprint="self-heal:live:labels:abc123",
    )
    runner = _SelfHealFakeRunner(
        {
            (
                "gh",
                "issue",
                "list",
                "--state",
                "open",
                "--search",
                evidence.fingerprint,
                "--json",
                "number,title,body",
                "--limit",
                "20",
            ): run_self_heal.CommandResult(0, "[]", ""),
            ("gh", "label", "list", "--json", "name", "--limit", "300"): run_self_heal.CommandResult(
                0, '[{"name":"codex"}]', ""
            ),
        }
    )

    issue = run_self_heal.create_or_update_issue(runner, "LIVE", evidence, "30 minutes ago", dry_run=False)

    assert issue == 144
    label_creates = [call for call in runner.calls if call[:3] == ("gh", "label", "create")]
    created_labels = {call[3] for call in label_creates}
    assert "auto-fix" in created_labels
    assert "LIVE" in created_labels
    assert "environment:live" in created_labels
    assert "processor:live-linux" in created_labels
    create_index = next(i for i, call in enumerate(runner.calls) if call[:3] == ("gh", "issue", "create"))
    first_label_index = next(i for i, call in enumerate(runner.calls) if call[:3] == ("gh", "label", "create"))
    assert first_label_index < create_index


def test_self_heal_dirty_worktree_reports_blocked_status(capsys: pytest.CaptureFixture[str]) -> None:
    runner = _SelfHealFakeRunner(
        {
            (
                "scripts/process_codex_issues_local.sh",
                "--limit",
                "1",
                "--issue",
                "152",
            ): run_self_heal.CommandResult(
                0,
                "CODEX_RESULT issue=152 status=failed reason=dirty_worktree\n",
                "Worktree is dirty; refusing to run local Codex issue processor.\n",
            )
        }
    )

    result = run_self_heal.route_to_codex(runner, 152, dry_run=False)

    assert result.status == "failed"
    assert result.reason == "dirty_worktree"
    assert "SELF_HEAL status=blocked reason=dirty_worktree issue=152" in capsys.readouterr().out


def test_self_heal_codex_active_lock_reports_running(capsys: pytest.CaptureFixture[str]) -> None:
    runner = _SelfHealFakeRunner(
        {
            (
                "scripts/process_codex_issues_local.sh",
                "--limit",
                "1",
                "--issue",
                "152",
            ): run_self_heal.CommandResult(
                0,
                "CODEX_RESULT issue=152 status=codex_running reason=active_lock\n",
                "",
            )
        }
    )

    result = run_self_heal.route_to_codex(runner, 152, dry_run=False)

    assert result.status == "codex_running"
    assert "SELF_HEAL status=codex_running issue=152" in capsys.readouterr().out


def test_self_heal_codex_local_only_result_reports_manual_action(capsys: pytest.CaptureFixture[str]) -> None:
    runner = _SelfHealFakeRunner(
        {
            (
                "scripts/process_codex_issues_local.sh",
                "--limit",
                "1",
                "--issue",
                "152",
            ): run_self_heal.CommandResult(
                0,
                "CODEX_RESULT issue=152 status=fix_local_only reason=pr_create_failed\n",
                "",
            )
        }
    )

    result = run_self_heal.route_to_codex(runner, 152, dry_run=False)

    assert result.status == "fix_local_only"
    assert result.reason == "pr_create_failed"
    out = capsys.readouterr().out
    assert "SELF_HEAL status=blocked reason=codex_fix_local_only issue=152" in out
    assert "next_action=push_branch_and_open_pr" in out


def test_self_heal_successful_pr_merge_triggers_deploy() -> None:
    issue_number = 44
    runner = _SelfHealFakeRunner(
        {
            (
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--search",
                f"#{issue_number}",
                "--json",
                "number,title,body",
                "--limit",
                "20",
            ): run_self_heal.CommandResult(0, '[{"number":12,"title":"Codex auto-fix","body":"Fixes #44"}]', ""),
            ("git", "status", "--short"): run_self_heal.CommandResult(0, "", ""),
            ("systemctl", "is-active", "paper.service"): run_self_heal.CommandResult(0, "active\n", ""),
            ("journalctl", "-u", "paper.service", "--since", "2 minutes ago", "--no-pager"): run_self_heal.CommandResult(
                0, "INFO healthy\n", ""
            ),
        }
    )

    pr_number = run_self_heal.validate_and_merge_pr(runner, issue_number, dry_run=False)
    deployed = run_self_heal.deploy_and_verify(
        runner,
        "PAPER",
        issue_number,
        pr_number,
        dry_run=False,
        manual_override=False,
    )

    assert pr_number == 12
    assert deployed is True
    assert ("scripts/validate_codex_pr.sh",) in runner.calls
    assert ("gh", "pr", "checks", "12", "--watch", "--fail-fast") in runner.calls
    assert ("gh", "pr", "merge", "12", "--squash", "--delete-branch") in runner.calls
    assert ("git", "pull", "--ff-only") in runner.calls
    assert ("systemctl", "restart", "paper.service") in runner.calls
    assert ("gh", "issue", "close", "44", "--reason", "completed") in runner.calls


def test_self_heal_failed_verification_creates_followup_issue() -> None:
    runner = _SelfHealFakeRunner(
        {
            ("git", "status", "--short"): run_self_heal.CommandResult(0, "", ""),
            ("systemctl", "is-active", "algo.service"): run_self_heal.CommandResult(0, "active\n", ""),
            ("journalctl", "-u", "algo.service", "--since", "2 minutes ago", "--no-pager"): run_self_heal.CommandResult(
                0, "Traceback runtime failure\n", ""
            ),
        }
    )

    deployed = run_self_heal.deploy_and_verify(
        runner,
        "LIVE",
        120,
        45,
        dry_run=False,
        manual_override=True,
    )

    assert deployed is False
    assert any(call[:3] == ("gh", "issue", "comment") and call[3] == "120" for call in runner.calls)
    assert not any(call[:3] == ("gh", "issue", "create") for call in runner.calls)
    assert not any(call[:3] == ("gh", "issue", "close") for call in runner.calls)


def test_self_heal_issue_labels_route_paper_and_live_correctly() -> None:
    evidence = run_self_heal.FailureEvidence(
        short_failure="entry eval pass without allocator trace",
        expected_flow="expected",
        actual_missing_step="missing",
        grep_command="grep entry",
        matching_logs=("ENTRY_EVAL_PASS symbol=QQQ",),
        fingerprint="self-heal:paper:entry:abc123",
    )
    runner = _SelfHealFakeRunner(
        {
            (
                "gh",
                "issue",
                "list",
                "--state",
                "open",
                "--search",
                evidence.fingerprint,
                "--json",
                "number,title,body",
                "--limit",
                "20",
            ): run_self_heal.CommandResult(0, "[]", ""),
            (
                "gh",
                "issue",
                "create",
                "--title",
                "[PAPER] Self-heal: entry eval pass without allocator trace",
                "--body-file",
                "__any__",
                "--label",
                "codex",
            ): run_self_heal.CommandResult(0, "https://github.com/org/repo/issues/91\n", ""),
        }
    )

    run_self_heal.create_or_update_issue(runner, "PAPER", evidence, "30 minutes ago", dry_run=False)

    create_call = next(call for call in runner.calls if call[:3] == ("gh", "issue", "create"))
    assert "--label" in create_call
    assert "PAPER" in create_call
    assert "environment:paper" in create_call
    assert "processor:mac-paper" in create_call


def _write_metrics_report(tmp_path: Path, *, env: str = "LIVE", failure: str = "missing_terminal_state") -> Path:
    report = {
        "environment": env,
        "environment_label": env.lower(),
        "date": "2026-06-23",
        "phase": "end_day",
        "logs": {
            "missing_flow_diagnostics": [
                {
                    "short_failure": failure,
                    "expected_flow": "expected downstream handoff",
                    "actual_missing_step": "missing downstream handoff",
                    "fingerprint": f"self-heal:{env.lower()}:metrics-test",
                }
            ]
        },
    }
    path = tmp_path / "research_metrics" / "2026-06-23" / f"end_day_{env.lower()}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def test_self_heal_uses_research_metrics_missing_flow_and_stops_before_deploy(tmp_path: Path) -> None:
    metrics_path = _write_metrics_report(tmp_path)
    runner = _SelfHealFakeRunner(
        {
            (
                "gh",
                "issue",
                "list",
                "--state",
                "open",
                "--search",
                "self-heal:live:metrics-test",
                "--json",
                "number,title,body",
                "--limit",
                "20",
            ): run_self_heal.CommandResult(0, "[]", ""),
            (
                "gh",
                "issue",
                "create",
                "--title",
                "[LIVE] Self-heal: missing_terminal_state",
                "--body-file",
                "__any__",
                "--label",
                "codex",
            ): run_self_heal.CommandResult(0, "https://github.com/org/repo/issues/144\n", ""),
        }
    )
    args = run_self_heal.parse_args(
        [
            "--live",
            "--data-dir",
            str(tmp_path),
            "--metrics-report",
            str(metrics_path),
        ]
    )

    assert run_self_heal.run_self_heal(args, runner) == 0

    create_call = next(call for call in runner.calls if call[:3] == ("gh", "issue", "create"))
    assert "LIVE" in create_call
    assert "codex" in create_call
    assert "auto-fix" in create_call
    assert "environment:live" in create_call
    assert ("scripts/process_codex_issues_local.sh", "--limit", "1", "--issue", "144") in runner.calls
    assert not any(call[:3] == ("gh", "pr", "merge") for call in runner.calls)
    assert not any(call[:2] == ("systemctl", "restart") for call in runner.calls)
    assert not any(call == ("git", "pull", "--ff-only") for call in runner.calls)


def test_self_heal_metrics_duplicate_suppression_comments_existing_issue(tmp_path: Path) -> None:
    metrics_path = _write_metrics_report(tmp_path)
    runner = _SelfHealFakeRunner(
        {
            (
                "gh",
                "issue",
                "list",
                "--state",
                "open",
                "--search",
                "self-heal:live:metrics-test",
                "--json",
                "number,title,body",
                "--limit",
                "20",
            ): run_self_heal.CommandResult(
                0,
                '[{"number":155,"title":"existing","body":"self-heal:live:metrics-test"}]',
                "",
            )
        }
    )
    args = run_self_heal.parse_args(["--live", "--data-dir", str(tmp_path), "--metrics-report", str(metrics_path)])

    assert run_self_heal.run_self_heal(args, runner) == 0

    assert any(call[:3] == ("gh", "issue", "comment") for call in runner.calls)
    assert not any(call[:3] == ("gh", "issue", "create") for call in runner.calls)
    assert ("scripts/process_codex_issues_local.sh", "--limit", "1", "--issue", "155") in runner.calls
