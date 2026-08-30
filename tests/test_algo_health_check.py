from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import time
from datetime import date
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "check_algo_health.sh"
SERVICE = PROJECT_ROOT / "deploy" / "systemd" / "algo-health-check.service"
TIMER = PROJECT_ROOT / "deploy" / "systemd" / "algo-health-check.timer"
DOCS = PROJECT_ROOT / "docs" / "OPERATIONS.md"


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _seed_repo(root: Path, *, user: str = "live_bot", accepted: int = 0) -> None:
    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "data" / "premarket").mkdir(parents=True, exist_ok=True)
    (root / "data" / "dynamic_scan_history").mkdir(parents=True, exist_ok=True)
    (root / "data" / "replay").mkdir(parents=True, exist_ok=True)
    _write_executable(root / "bin" / "algo", "#!/usr/bin/env bash\necho bin-algo \"$@\"\nexit 0\n")
    (root / "data" / "premarket" / "latest_event_feed.json").write_text(
        json.dumps({"events": 5}),
        encoding="utf-8",
    )
    (root / "data" / "premarket" / "latest_rankings.json").write_text(
        json.dumps({"catalyst_ranked_symbols": 5, "rankings": 5}),
        encoding="utf-8",
    )
    (root / "data" / "premarket" / "latest_catalysts.json").write_text(
        json.dumps({"catalysts": 5}),
        encoding="utf-8",
    )
    (root / "data" / "dynamic_scan_history" / f"20260611T130000000000Z_{user}.json").write_text(
        json.dumps({"counts": {"candidates": 10, "accepted": accepted, "rejected": 10 - accepted}}),
        encoding="utf-8",
    )
    (root / "data" / "replay" / f"2026-06-11_{user}.json").write_text(
        json.dumps({"submitted_orders": []}),
        encoding="utf-8",
    )


def _seed_paper_review_log(root: Path, lines: list[str]) -> Path:
    path = root / "data" / "review" / date.today().isoformat() / "paper_full.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _fake_path(
    tmp_path: Path,
    *,
    journal: str = "INFO healthy",
    gh: str | None = None,
    uname: str | None = None,
    hostname: str | None = None,
    day_of_week: int | None = None,
    systemctl_state: str = "active",
) -> Path:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "journalctl", f"#!/usr/bin/env bash\necho {journal!r}\n")
    _write_executable(
        fake_bin / "systemctl",
        "#!/usr/bin/env bash\n"
        f"state={systemctl_state!r}\n"
        "if [[ \"$1\" == \"is-active\" ]]; then\n"
        "  if [[ \"$state\" == \"active\" ]]; then echo active; exit 0; fi\n"
        "  echo failed; exit 3\n"
        "fi\n"
        "if [[ \"$1\" == \"is-failed\" ]]; then\n"
        "  if [[ \"$state\" == \"active\" ]]; then echo active; exit 1; fi\n"
        "  echo failed; exit 0\n"
        "fi\n"
        "echo \"Active: ${state}\"\n",
    )
    if gh is not None:
        _write_executable(fake_bin / "gh", gh)
    if uname is not None:
        _write_executable(fake_bin / "uname", f"#!/usr/bin/env bash\necho {uname!r}\n")
    if hostname is not None:
        _write_executable(fake_bin / "hostname", f"#!/usr/bin/env bash\necho {hostname!r}\n")
    if day_of_week is not None:
        _write_executable(
            fake_bin / "date",
            "#!/usr/bin/env bash\n"
            "if [[ \"$1\" == \"+%u\" ]]; then\n"
            f"  echo {day_of_week}\n"
            "  exit 0\n"
            "fi\n"
            "if [[ \"$1\" == \"-u\" ]]; then\n"
            "  echo 2026-06-14T12:00:00Z\n"
            "  exit 0\n"
            "fi\n"
            "if [[ \"$1\" == \"+%Y-%m-%d\" ]]; then\n"
            "  echo 2026-06-14\n"
            "  exit 0\n"
            "fi\n"
            "/bin/date \"$@\"\n",
        )
    return fake_bin


def _fake_path_with_journal_lines(
    tmp_path: Path,
    *,
    lines: list[str],
    gh: str | None = None,
    systemctl_state: str = "active",
) -> Path:
    fake_bin = _fake_path(tmp_path, journal="INFO healthy", gh=gh, systemctl_state=systemctl_state)
    journal_body = "\n".join(lines)
    _write_executable(
        fake_bin / "journalctl",
        "#!/usr/bin/env bash\n"
        "cat <<'LOG'\n"
        f"{journal_body}\n"
        "LOG\n",
    )
    return fake_bin


def _make_premarket_artifacts_stale(root: Path) -> None:
    old = time.time() - 48 * 60 * 60
    for name in ("latest_event_feed.json", "latest_rankings.json", "latest_catalysts.json"):
        os.utime(root / "data" / "premarket" / name, (old, old))


def _seed_portable_script_repo(root: Path, script: Path = SCRIPT) -> Path:
    scripts_dir = root / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(script, scripts_dir / script.name)
    (scripts_dir / script.name).chmod(script.stat().st_mode | stat.S_IXUSR)
    _seed_repo(root, user="paper_bot", accepted=2)
    _seed_repo(root, user="live_bot", accepted=2)
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        "echo portable-root:$PWD\n"
        "echo bin-algo \"$@\"\n"
        "exit 0\n",
    )
    return scripts_dir / script.name


def test_algo_health_check_script_exists_and_is_executable() -> None:
    assert SCRIPT.exists()
    assert os.access(SCRIPT, os.X_OK)


def test_algo_health_script_has_no_hardcoded_fedora_repo_root() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "/opt/algosphere/algo-ai-trading-agent" not in text
    assert 'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"' in text
    assert 'ROOT="${ALGO_REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"' in text


def test_algo_health_resolves_repo_root_from_script_location_for_mac_paper(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Users" / "psuriset" / "cursor" / "algo"
    script = _seed_portable_script_repo(root)
    fake_bin = _fake_path(tmp_path)
    report_path = tmp_path / "algo_health_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_HEALTH_REPORT_PATH": str(report_path),
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
    assert f"portable-root:{root}" in text


def test_algo_health_resolves_repo_root_from_script_location_for_fedora_live(
    tmp_path: Path,
) -> None:
    root = tmp_path / "home" / "algosphere" / "algo"
    script = _seed_portable_script_repo(root)
    fake_bin = _fake_path(tmp_path)
    report_path = tmp_path / "algo_health_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_HEALTH_REPORT_PATH": str(report_path),
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
    assert f"portable-root:{root}" in text


def test_algo_health_algo_repo_root_override_still_wins(tmp_path: Path) -> None:
    script_root = tmp_path / "wrong" / "repo"
    override_root = tmp_path / "override" / "repo"
    script = _seed_portable_script_repo(script_root)
    _seed_portable_script_repo(override_root)
    fake_bin = _fake_path(tmp_path)
    report_path = tmp_path / "algo_health_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(override_root),
        "ALGO_HEALTH_REPORT_PATH": str(report_path),
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
    assert f"portable-root:{override_root}" in text
    assert f"portable-root:{script_root}" not in text


def test_algo_health_systemd_service_and_timer_exist() -> None:
    assert SERVICE.exists()
    assert TIMER.exists()
    service_text = SERVICE.read_text(encoding="utf-8")
    timer_text = TIMER.read_text(encoding="utf-8")
    assert "ExecStart=/usr/bin/bash /opt/algosphere/algo-ai-trading-agent/scripts/check_algo_health.sh" in service_text
    assert "NoNewPrivileges=true" in service_text
    assert "OnUnitActiveSec=30min" in timer_text
    assert "Unit=algo-health-check.service" in timer_text


def test_algo_health_dry_run_reports_medium_dynamic_issue(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root, user="live_bot", accepted=0)
    fake_bin = _fake_path(tmp_path)
    report_path = tmp_path / "algo_health_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_HEALTH_REPORT_PATH": str(report_path),
        "ALGO_HEALTH_ACCEPTED_ZERO_THRESHOLD": "0",
    }

    proc = subprocess.run(
        [str(SCRIPT), "--dry-run", "LIVE"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    assert "Dry run: would create GitHub issue: HEALTH [LIVE] dynamic scanner producing no candidates" in proc.stdout
    text = report_path.read_text(encoding="utf-8")
    assert "- Environment: LIVE" in text
    assert "- Severity: medium" in text
    assert "accepted=0" in text


def test_algo_health_duplicate_detection_skips_create(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root, user="live_bot", accepted=0)
    calls = tmp_path / "gh_calls.log"
    fake_bin = _fake_path(
        tmp_path,
        gh=(
            "#!/usr/bin/env bash\n"
            f"echo \"$@\" >> {calls}\n"
            "if [[ \"$1 $2\" == \"issue list\" ]]; then\n"
            "  echo \"$@\"\n"
            "  printf '[{\"number\":1,\"title\":\"health\",\"body\":\"health:live:medium:dynamic-scanner-producing-no-candidates\"}]\\n'\n"
            "  exit 0\n"
            "fi\n"
            "if [[ \"$1 $2\" == \"issue create\" ]]; then\n"
            "  echo unexpected-create >&2\n"
            "  exit 42\n"
            "fi\n"
        ),
    )
    report_path = tmp_path / "algo_health_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_HEALTH_REPORT_PATH": str(report_path),
        "ALGO_HEALTH_ACCEPTED_ZERO_THRESHOLD": "0",
    }

    proc = subprocess.run(
        [str(SCRIPT), "LIVE"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    assert "Existing health issue found" in proc.stdout
    gh_calls = calls.read_text(encoding="utf-8")
    assert "issue list" in gh_calls
    assert "issue create" not in gh_calls


def test_algo_health_no_arg_defaults_to_detected_live_host(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root, user="live_bot", accepted=2)
    _seed_repo(root, user="paper_bot", accepted=2)
    fake_bin = _fake_path(
        tmp_path,
        journal="OPTION_ROUTE_CHECK symbol=QQQ",
        uname="Linux",
        hostname="algosphere-live-host",
    )
    report_path = tmp_path / "algo_health_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_HEALTH_REPORT_PATH": str(report_path),
    }

    proc = subprocess.run(
        [str(SCRIPT), "--dry-run"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    assert "No health issue detected for LIVE" in proc.stdout
    text = report_path.read_text(encoding="utf-8")
    assert "- Environment: LIVE" in text
    assert "- environment=live" in text
    assert "- Hostname: algosphere-live-host" in text
    assert "- Service Name: algo.service" in text


def test_algo_health_explicit_env_overrides_default(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root, user="paper_bot", accepted=2)
    fake_bin = _fake_path(
        tmp_path,
        journal="OPTION_ROUTE_CHECK symbol=QQQ",
        uname="Linux",
        hostname="algosphere-live-host",
    )
    report_path = tmp_path / "algo_health_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_HEALTH_REPORT_PATH": str(report_path),
    }

    proc = subprocess.run(
        [str(SCRIPT), "--dry-run", "--env", "paper"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    assert "No health issue detected for PAPER" in proc.stdout
    text = report_path.read_text(encoding="utf-8")
    assert "- Environment: PAPER" in text
    assert "- environment=paper" in text


def test_algo_health_paper_mac_missing_review_log_reports_degraded(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root, user="paper_bot", accepted=2)
    fake_bin = _fake_path(
        tmp_path,
        uname="Darwin",
        day_of_week=1,
    )
    report_path = tmp_path / "algo_health_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_HEALTH_REPORT_PATH": str(report_path),
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
    assert "- Detected Condition: paper review log missing" in text
    assert f"PAPER_REVIEW_LOG_MISSING path={review_dir / 'paper_full.log'}" in text
    assert "No health issue detected for PAPER" not in proc.stdout


def test_algo_health_weekend_stale_premarket_artifacts_do_not_create_issue(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root, user="live_bot", accepted=2)
    _make_premarket_artifacts_stale(root)
    calls = tmp_path / "gh_calls.log"
    fake_bin = _fake_path(
        tmp_path,
        day_of_week=7,
        gh=(
            "#!/usr/bin/env bash\n"
            f"echo \"$@\" >> {calls}\n"
            "if [[ \"$1 $2\" == \"issue create\" ]]; then\n"
            "  echo unexpected-create >&2\n"
            "  exit 42\n"
            "fi\n"
            "printf '[]\\n'\n"
        ),
    )
    report_path = tmp_path / "algo_health_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_HEALTH_REPORT_PATH": str(report_path),
        "ALGO_HEALTH_PREMARKET_MAX_AGE_MINUTES": "1",
    }

    proc = subprocess.run(
        [str(SCRIPT), "LIVE"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    assert "No health issue detected for LIVE" in proc.stdout
    assert not calls.exists()
    text = report_path.read_text(encoding="utf-8")
    assert "- Environment: LIVE" in text
    assert "market_closed=true" in text
    assert "stale_premarket_artifacts_suppressed=true" in text
    assert "suppression_reason=weekend_market_closed" in text
    assert "premarket artifacts stale" in text


def test_algo_health_weekday_stale_premarket_artifacts_create_environment_issue(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root, user="live_bot", accepted=2)
    _make_premarket_artifacts_stale(root)
    calls = tmp_path / "gh_calls.log"
    fake_bin = _fake_path(
        tmp_path,
        day_of_week=1,
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
    report_path = tmp_path / "algo_health_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_HEALTH_REPORT_PATH": str(report_path),
        "ALGO_HEALTH_PREMARKET_MAX_AGE_MINUTES": "1",
    }

    proc = subprocess.run(
        [str(SCRIPT), "LIVE"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    gh_calls = calls.read_text(encoding="utf-8")
    assert "--title HEALTH [LIVE] premarket artifacts stale" in gh_calls
    assert "--label environment:live" in gh_calls
    assert "--label processor:live-linux" in gh_calls
    assert "--label severity:medium" in gh_calls
    text = report_path.read_text(encoding="utf-8")
    assert "market_closed=false" in text
    assert "stale_premarket_artifacts_suppressed=false" in text
    assert "suppression_reason=none" in text


def test_algo_health_paper_issue_gets_mac_processor_label(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root, user="paper_bot", accepted=0)
    calls = tmp_path / "gh_calls.log"
    fake_bin = _fake_path(
        tmp_path,
        journal="OPTION_ROUTE_CHECK symbol=QQQ",
        gh=(
            "#!/usr/bin/env bash\n"
            f"echo \"$@\" >> {calls}\n"
            "if [[ \"$1 $2\" == \"issue list\" ]]; then printf '[]\\n'; exit 0; fi\n"
            "if [[ \"$1 $2\" == \"issue create\" ]]; then exit 0; fi\n"
        ),
    )
    report_path = tmp_path / "algo_health_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_HEALTH_REPORT_PATH": str(report_path),
        "ALGO_HEALTH_ACCEPTED_ZERO_THRESHOLD": "0",
    }

    proc = subprocess.run(
        [str(SCRIPT), "PAPER"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    gh_calls = calls.read_text(encoding="utf-8")
    assert "--label environment:paper" in gh_calls
    assert "--label processor:mac-paper" in gh_calls


def test_algo_health_paper_single_corrupt_artifact_occurrence_does_not_create_issue(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root, user="paper_bot", accepted=2)
    _seed_paper_review_log(
        root,
        [
            "OPTION_ROUTE_CHECK symbol=QQQ",
            "TRADE_ATTRIBUTION_CORRUPT_ARTIFACT path=data/trade_attribution/daily/2026-06-13_paper_bot.json error=Invalid control character",
        ],
    )
    fake_bin = _fake_path_with_journal_lines(
        tmp_path,
        lines=[
            "OPTION_ROUTE_CHECK symbol=QQQ",
            "TRADE_ATTRIBUTION_CORRUPT_ARTIFACT path=data/trade_attribution/daily/2026-06-13_paper_bot.json error=Invalid control character",
        ],
    )
    report_path = tmp_path / "algo_health_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_HEALTH_REPORT_PATH": str(report_path),
    }

    proc = subprocess.run(
        [str(SCRIPT), "--dry-run", "PAPER"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    assert "No health issue detected for PAPER" in proc.stdout
    assert "paper:trade_attribution_corrupt_artifact" not in report_path.read_text(encoding="utf-8")


def test_algo_health_paper_repeated_corrupt_artifact_creates_stable_fingerprint_issue(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root, user="paper_bot", accepted=2)
    calls = tmp_path / "gh_calls.log"
    corrupt_lines = [
        "OPTION_ROUTE_CHECK symbol=QQQ",
        "09:31 TRADE_ATTRIBUTION_CORRUPT_ARTIFACT path=data/trade_attribution/daily/2026-06-13_paper_bot.json error=Invalid control character",
        "09:32 json.decoder.JSONDecodeError: Invalid control character at: line 1461 column 27",
        "09:33 TRADE_ATTRIBUTION_CORRUPT_ARTIFACT path=data/trade_attribution/daily/2026-06-13_paper_bot.json error=Invalid control character",
    ]
    _seed_paper_review_log(root, corrupt_lines)
    fake_bin = _fake_path_with_journal_lines(
        tmp_path,
        lines=corrupt_lines,
        gh=(
            "#!/usr/bin/env bash\n"
            f"echo \"$@\" >> {calls}\n"
            "if [[ \"$1 $2\" == \"issue list\" ]]; then printf '[]\\n'; exit 0; fi\n"
            "if [[ \"$1 $2\" == \"issue create\" ]]; then exit 0; fi\n"
        ),
    )
    report_path = tmp_path / "algo_health_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_HEALTH_REPORT_PATH": str(report_path),
    }

    proc = subprocess.run(
        [str(SCRIPT), "PAPER"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    gh_calls = calls.read_text(encoding="utf-8")
    assert "--title HEALTH [PAPER] trade attribution corrupt artifact" in gh_calls
    assert "--label environment:paper" in gh_calls
    assert "--label processor:mac-paper" in gh_calls
    assert "--label severity:high" in gh_calls
    assert "paper:trade_attribution_corrupt_artifact" in report_path.read_text(encoding="utf-8")
    assert "occurrence_count=3" in report_path.read_text(encoding="utf-8")
    assert "artifact_path=data/trade_attribution/daily/2026-06-13_paper_bot.json" in report_path.read_text(encoding="utf-8")


def test_algo_health_paper_repeated_core_rebuild_error_uses_stable_fingerprint(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root, user="paper_bot", accepted=2)
    report_path = tmp_path / "algo_health_report.md"
    churn_lines = [
        "OPTION_ROUTE_CHECK symbol=QQQ",
        "09:31 CORE_REBUILD_CHURN_GUARD_ERROR symbol=SPY",
        "09:32 CORE_REBUILD_CHURN_GUARD_ERROR symbol=QQQ",
    ]
    _seed_paper_review_log(root, churn_lines)
    fake_bin = _fake_path_with_journal_lines(
        tmp_path,
        lines=churn_lines,
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_HEALTH_REPORT_PATH": str(report_path),
    }

    proc = subprocess.run(
        [str(SCRIPT), "--dry-run", "PAPER"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    assert "Dry run fingerprint: paper:core_rebuild_churn_guard_error" in proc.stdout
    text = report_path.read_text(encoding="utf-8")
    assert "occurrence_count=2" in text
    assert "first_seen=09:31 CORE_REBUILD_CHURN_GUARD_ERROR symbol=SPY" in text
    assert "last_seen=09:32 CORE_REBUILD_CHURN_GUARD_ERROR symbol=QQQ" in text


def test_algo_health_paper_recoverable_runtime_duplicate_issue_suppressed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root, user="paper_bot", accepted=2)
    calls = tmp_path / "gh_calls.log"
    corrupt_lines = [
        "OPTION_ROUTE_CHECK symbol=QQQ",
        "TRADE_ATTRIBUTION_CORRUPT_ARTIFACT path=data/trade_attribution/daily/2026-06-13_paper_bot.json error=Invalid control character",
        "TRADE_ATTRIBUTION_CORRUPT_ARTIFACT path=data/trade_attribution/daily/2026-06-13_paper_bot.json error=Invalid control character",
    ]
    _seed_paper_review_log(root, corrupt_lines)
    fake_bin = _fake_path_with_journal_lines(
        tmp_path,
        lines=corrupt_lines,
        gh=(
            "#!/usr/bin/env bash\n"
            f"echo \"$@\" >> {calls}\n"
            "if [[ \"$1 $2\" == \"issue list\" ]]; then\n"
            "  printf '[{\"number\":139,\"title\":\"paper:trade_attribution_corrupt_artifact\"}]\\n'\n"
            "  exit 0\n"
            "fi\n"
            "if [[ \"$1 $2\" == \"issue create\" ]]; then echo unexpected-create >&2; exit 42; fi\n"
        ),
    )
    report_path = tmp_path / "algo_health_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_HEALTH_REPORT_PATH": str(report_path),
    }

    proc = subprocess.run(
        [str(SCRIPT), "PAPER"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    assert "Existing health issue found for paper:trade_attribution_corrupt_artifact" in proc.stdout
    gh_calls = calls.read_text(encoding="utf-8")
    assert "issue list" in gh_calls
    assert "issue create" not in gh_calls


def test_algo_health_recoverable_runtime_detection_is_paper_only(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root, user="live_bot", accepted=2)
    fake_bin = _fake_path_with_journal_lines(
        tmp_path,
        lines=[
            "TRADE_ATTRIBUTION_CORRUPT_ARTIFACT path=data/trade_attribution/daily/2026-06-13_live_bot.json error=Invalid control character",
            "TRADE_ATTRIBUTION_CORRUPT_ARTIFACT path=data/trade_attribution/daily/2026-06-13_live_bot.json error=Invalid control character",
            "CORE_REBUILD_CHURN_GUARD_ERROR symbol=SPY",
            "CORE_REBUILD_CHURN_GUARD_ERROR symbol=QQQ",
        ],
    )
    report_path = tmp_path / "algo_health_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_HEALTH_REPORT_PATH": str(report_path),
    }

    proc = subprocess.run(
        [str(SCRIPT), "--dry-run", "LIVE"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    assert "No health issue detected for LIVE" in proc.stdout
    text = report_path.read_text(encoding="utf-8")
    assert "paper:trade_attribution_corrupt_artifact" not in text
    assert "paper:core_rebuild_churn_guard_error" not in text


def test_algo_health_weekend_service_down_is_not_suppressed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_repo(root, user="live_bot", accepted=2)
    _make_premarket_artifacts_stale(root)
    calls = tmp_path / "gh_calls.log"
    fake_bin = _fake_path(
        tmp_path,
        day_of_week=7,
        systemctl_state="failed",
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
    report_path = tmp_path / "algo_health_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_HEALTH_REPORT_PATH": str(report_path),
        "ALGO_HEALTH_PREMARKET_MAX_AGE_MINUTES": "1",
    }

    proc = subprocess.run(
        [str(SCRIPT), "LIVE"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    gh_calls = calls.read_text(encoding="utf-8")
    assert "--title HEALTH [LIVE] service down" in gh_calls
    assert "--label environment:live" in gh_calls
    assert "--label processor:live-linux" in gh_calls
    assert "--label severity:critical" in gh_calls
    text = report_path.read_text(encoding="utf-8")
    assert "- Severity: critical" in text
    assert "market_closed=true" in text
    assert "stale_premarket_artifacts_suppressed=true" in text
    assert "suppression_reason=weekend_market_closed" in text


def test_operations_docs_cover_health_monitoring() -> None:
    text = DOCS.read_text(encoding="utf-8")
    assert "Level 1.5 Silent Health Monitoring" in text
    assert "scripts/check_algo_health.sh --dry-run" in text
    assert "algo-health-check.timer" in text
    assert "HEALTH [LIVE]" in text
