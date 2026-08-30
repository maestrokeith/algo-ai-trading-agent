from __future__ import annotations

import os
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "run_autoops.py"
BIN = PROJECT_ROOT / "bin" / "algo"
FAILURE_TYPES = (
    "health_failed",
    "service_down",
    "premarket_missing",
    "broker_auth_failed",
    "stale_market_data",
    "allocator_silent_drop",
    "order_submit_failed",
    "paper_options_diagnostics_failed",
    "validation_failed",
    "github_issue_failed",
    "codex_processor_failed",
    "auto_merge_blocked",
)


def _runtime_config_text(
    *,
    mode: str = "shadow",
    trend_long: str = "SHADOW",
    live_pilot_enabled: bool = False,
    options_enabled: bool = False,
    options_live_pilot_enabled: bool = False,
    autoops_autostart: bool = True,
    malformed_pilot_caps: bool = False,
) -> str:
    max_trades = 2 if malformed_pilot_caps else 1
    return (
        "autoops:\n"
        f"  live_end_day_codex_autostart_enabled: {str(autoops_autostart).lower()}\n"
        "trading_control:\n"
        f"  mode: {mode}\n"
        "  strategy_states:\n"
        f"    trend_long: {trend_long}\n"
        "    momentum_breakout: SHADOW\n"
        "    dynamic_no_catalyst: SHADOW\n"
        "    news_only: DISABLED\n"
        "    options_live: DISABLED\n"
        "    options_paper: DISABLED\n"
        "  live_pilot:\n"
        f"    enabled: {str(live_pilot_enabled).lower()}\n"
        "    allowed_strategies:\n"
        "      - trend_long\n"
        f"    max_trades_per_day: {max_trades}\n"
        "    max_entry_submissions_per_day: 1\n"
        "    max_entry_fills_per_day: 1\n"
        "    max_open_positions: 1\n"
        "    max_notional_per_trade: 100\n"
        "    max_total_deployed_notional: 100\n"
        "    max_daily_loss_usd: 25\n"
        "    allow_short_selling: false\n"
        "    allow_add_to_existing: false\n"
        "    allow_replacements: false\n"
        "    allow_reallocation: false\n"
        "    allow_overnight: false\n"
        "    eod_flatten_required: true\n"
        "options:\n"
        f"  enabled: {str(options_enabled).lower()}\n"
        "  mode: live_long_premium\n"
        f"  live_pilot_enabled: {str(options_live_pilot_enabled).lower()}\n"
        "  live_pilot:\n"
        f"    enabled: {str(options_live_pilot_enabled).lower()}\n"
    )


def _write_runtime_config(root: Path, **kwargs: object) -> None:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "default.yaml").write_text(_runtime_config_text(**kwargs), encoding="utf-8")
    (config / "users.yaml").write_text("users: []\n", encoding="utf-8")


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fake_autoops_path(
    tmp_path: Path,
    *,
    include_systemctl: bool = True,
    include_launchctl: bool = False,
    launchctl_active: bool = True,
    include_pgrep: bool = False,
    pgrep_active: bool = False,
) -> tuple[Path, Path]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls.log"
    _write_executable(
        fake_bin / "gh",
        "#!/usr/bin/env bash\n"
        f"echo gh \"$@\" >> {calls}\n"
        "if [[ \"$1\" == \"--version\" ]]; then echo 'gh version 2.0.0'; exit 0; fi\n"
        "if [[ \"$1 $2\" == \"issue list\" ]]; then echo '[]'; exit 0; fi\n"
        "if [[ \"$1 $2\" == \"pr list\" ]]; then echo '[]'; exit 0; fi\n"
        "echo unexpected-gh >&2\n"
        "exit 42\n",
    )
    if include_systemctl:
        _write_executable(
            fake_bin / "systemctl",
            "#!/usr/bin/env bash\n"
            f"echo systemctl \"$@\" >> {calls}\n"
            "if [[ \"$1\" == \"is-active\" ]]; then echo active; exit 0; fi\n"
            "echo unexpected-systemctl >&2\n"
            "exit 42\n",
        )
    if include_launchctl:
        launchctl_rc = 0 if launchctl_active else 113
        _write_executable(
            fake_bin / "launchctl",
            "#!/usr/bin/env bash\n"
            f"echo launchctl \"$@\" >> {calls}\n"
            "if [[ \"$1\" == \"print\" ]]; then\n"
            f"  exit {launchctl_rc}\n"
            "fi\n"
            "echo unexpected-launchctl >&2\n"
            "exit 42\n",
        )
    if include_pgrep:
        pgrep_rc = 0 if pgrep_active else 1
        pgrep_out = "12345\\n" if pgrep_active else ""
        _write_executable(
            fake_bin / "pgrep",
            "#!/usr/bin/env bash\n"
            f"echo pgrep \"$@\" >> {calls}\n"
            "if [[ \"$1\" == \"-f\" ]]; then\n"
            f"  printf '{pgrep_out}'\n"
            f"  exit {pgrep_rc}\n"
            "fi\n"
            "echo unexpected-pgrep >&2\n"
            "exit 42\n",
        )
    return fake_bin, calls


def _fake_confirmed_autoops_path(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "fake-confirmed-bin"
    fake_bin.mkdir()
    calls = tmp_path / "confirmed-calls.log"
    _write_executable(
        fake_bin / "gh",
        "#!/usr/bin/env bash\n"
        f"echo gh \"$@\" >> {calls}\n"
        "if [[ \"$1\" == \"--version\" ]]; then echo 'gh version 2.0.0'; exit 0; fi\n"
        "if [[ \"$1 $2\" == \"issue create\" ]]; then echo 'https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/issues/321'; exit 0; fi\n"
        "if [[ \"$1 $2\" == \"pr list\" ]]; then\n"
        "  echo '[{\"number\":654,\"mergedAt\":\"2026-06-24T15:00:00Z\",\"url\":\"https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/pull/654\",\"labels\":[{\"name\":\"codex-validation-passed\"}]}]'\n"
        "  exit 0\n"
        "fi\n"
        "echo '[]'\n",
    )
    _write_executable(
        fake_bin / "pytest",
        "#!/usr/bin/env bash\n"
        f"echo pytest \"$@\" >> {calls}\n"
        "exit 0\n",
    )
    return fake_bin, calls


def _fake_issue_failure_autoops_path(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "fake-issue-failure-bin"
    fake_bin.mkdir()
    calls = tmp_path / "issue-failure-calls.log"
    _write_executable(
        fake_bin / "gh",
        "#!/usr/bin/env bash\n"
        f"echo gh \"$@\" >> {calls}\n"
        "if [[ \"$1\" == \"--version\" ]]; then echo 'gh version 2.0.0'; exit 0; fi\n"
        "if [[ \"$1 $2\" == \"issue create\" ]]; then echo 'GraphQL: could not resolve label auto-fix' >&2; exit 1; fi\n"
        "echo '[]'\n",
    )
    return fake_bin, calls


def _fake_optional_label_missing_autoops_path(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "fake-optional-label-missing-bin"
    fake_bin.mkdir()
    calls = tmp_path / "optional-label-calls.log"
    _write_executable(
        fake_bin / "gh",
        "#!/usr/bin/env bash\n"
        f"echo gh \"$@\" >> {calls}\n"
        "if [[ \"$1\" == \"--version\" ]]; then echo 'gh version 2.0.0'; exit 0; fi\n"
        "if [[ \"$1 $2\" == \"issue create\" ]]; then\n"
        "  if printf '%s\\n' \"$@\" | grep -qx 'autoops-drill'; then echo 'GraphQL: could not resolve to a label with the name autoops-drill' >&2; exit 1; fi\n"
        "  echo 'https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/issues/321'; exit 0\n"
        "fi\n"
        "if [[ \"$1 $2\" == \"pr list\" ]]; then\n"
        "  echo '[{\"number\":654,\"mergedAt\":\"2026-06-24T15:00:00Z\",\"url\":\"https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/pull/654\",\"labels\":[{\"name\":\"codex-validation-passed\"}]}]'\n"
        "  exit 0\n"
        "fi\n"
        "echo '[]'\n",
    )
    _write_executable(
        fake_bin / "pytest",
        "#!/usr/bin/env bash\n"
        f"echo pytest \"$@\" >> {calls}\n"
        "exit 0\n",
    )
    return fake_bin, calls


def _fake_full_autoops_path(
    tmp_path: Path,
    *,
    labels_present: bool = True,
    validation_failed: bool = False,
    pr_rows: list[dict[str, object]] | None = None,
    issue_rows: list[dict[str, object]] | None = None,
) -> tuple[Path, Path]:
    fake_bin = tmp_path / "fake-full-bin"
    fake_bin.mkdir()
    calls = tmp_path / "full-calls.log"
    labels = [
        "codex",
        "auto-fix",
        "algo-health",
        "algo-failure",
        "LIVE",
        "PAPER",
        "environment:live",
        "environment:paper",
        "severity:high",
        "severity:medium",
        "processor:fedora-live",
        "processor:live-linux",
        "processor:mac-paper",
    ]
    if not labels_present:
        labels.remove("LIVE")
    labels_json = json.dumps([{"name": label} for label in labels])
    pr_label = "codex-validation-failed" if validation_failed else "codex-validation-passed"
    if pr_rows is None:
        pr_rows = [
            {
                "number": 177,
                "title": "Codex fix",
                "headRefName": "codex/fix",
                "url": "https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/pull/177",
                "state": "OPEN",
                "labels": [{"name": "LIVE"}, {"name": "environment:live"}, {"name": pr_label}],
            }
        ]
    pr_json = json.dumps(pr_rows)
    if issue_rows is None:
        issue_rows = [
            {
                "number": 155,
                "title": "[LIVE] Self-heal: dynamic flow",
                "body": "",
                "url": "https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/issues/155",
                "labels": [{"name": "LIVE"}, {"name": "algo-health"}],
            }
        ]
    issue_json = json.dumps(issue_rows)
    _write_executable(
        fake_bin / "gh",
        "#!/usr/bin/env bash\n"
        f"echo gh \"$@\" >> {calls}\n"
        "if [[ \"$1\" == \"--version\" ]]; then echo 'gh version 2.0.0'; exit 0; fi\n"
        "if [[ \"$1 $2\" == \"auth status\" ]]; then echo 'Logged in to github.com'; exit 0; fi\n"
        "if [[ \"$1 $2\" == \"api graphql\" ]]; then echo '{\"data\":{\"viewer\":{\"login\":\"ci-bot\"}}}'; exit 0; fi\n"
        f"if [[ \"$1 $2\" == \"label list\" ]]; then printf '%s\\n' '{labels_json}'; exit 0; fi\n"
        f"if [[ \"$1 $2\" == \"issue list\" ]]; then printf '%s\\n' '{issue_json}'; exit 0; fi\n"
        f"if [[ \"$1 $2\" == \"pr list\" ]]; then printf '%s\\n' '{pr_json}'; exit 0; fi\n"
        "echo unexpected-gh >&2\n"
        "exit 42\n",
    )
    _write_executable(
        fake_bin / "systemctl",
        "#!/usr/bin/env bash\n"
        f"echo systemctl \"$@\" >> {calls}\n"
        "if [[ \"$1\" == \"is-active\" ]]; then echo active; exit 0; fi\n"
        "echo unexpected-systemctl >&2\n"
        "exit 42\n",
    )
    _write_executable(
        fake_bin / "pgrep",
        "#!/usr/bin/env bash\n"
        f"echo pgrep \"$@\" >> {calls}\n"
        "if [[ \"$1\" == \"-f\" ]]; then echo 12345; exit 0; fi\n"
        "echo unexpected-pgrep >&2\n"
        "exit 42\n",
    )
    return fake_bin, calls


def _seed_autoops_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write_runtime_config(root)
    for rel in (
        "scripts/check_algo_health.sh",
        "scripts/report_algo_failure_to_github.sh",
        "scripts/run_self_heal.py",
        "scripts/process_codex_issues_local.sh",
        ".github/workflows/codex-pr-validation.yml",
        ".github/workflows/codex-auto-merge.yml",
    ):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith(".sh"):
            _write_executable(path, "#!/usr/bin/env bash\nexit 0\n")
        else:
            path.write_text("# test fixture\n", encoding="utf-8")
    (root / "bin").mkdir(parents=True, exist_ok=True)
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        "echo bin-algo \"$@\"\n"
        "exit 0\n",
    )
    return root


def _run_autoops(
    args: list[str],
    *,
    fake_bin: Path,
    cwd: Path = PROJECT_ROOT,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_autoops_status_command_is_read_only(tmp_path: Path) -> None:
    fake_bin, calls = _fake_autoops_path(tmp_path)
    root = _seed_autoops_root(tmp_path)

    proc = _run_autoops(["status", "--environment", "live", "--project-root", str(root)], fake_bin=fake_bin)

    assert proc.returncode == 0, proc.stderr
    assert "AUTOOPS_HEALTH_CHECK status=running mode=status read_only=true" in proc.stdout
    assert "AutoOps status" in proc.stdout
    assert "platform: Algo" in proc.stdout
    assert "failure_classes: health_failed" in proc.stdout
    assert "health_failed_recoveries: 0" in proc.stdout
    logged = calls.read_text(encoding="utf-8")
    assert "service_manager: systemd" in proc.stdout
    assert "service_active: active" in proc.stdout
    assert "systemctl is-active algo.service" in logged
    assert "gh --version" in logged
    assert "gh issue list" in logged
    assert "gh pr list" in logged
    forbidden = ("issue create", "issue comment", "issue edit", "pr create", "pr merge", "restart")
    assert not any(item in logged for item in forbidden)


def test_autoops_linux_paper_uses_systemctl_paper_service(tmp_path: Path) -> None:
    fake_bin, calls = _fake_autoops_path(tmp_path)
    root = _seed_autoops_root(tmp_path)

    proc = _run_autoops(
        ["status", "--environment", "paper", "--project-root", str(root)],
        fake_bin=fake_bin,
        extra_env={"ALGO_AUTOOPS_PLATFORM": "Linux", "ALGO_PAPER_SERVICE": "paper.service"},
    )

    assert proc.returncode == 0, proc.stderr
    assert "service_manager: systemd" in proc.stdout
    assert "service_name: paper.service" in proc.stdout
    assert "service_active: active" in proc.stdout
    assert "systemctl is-active paper.service" in calls.read_text(encoding="utf-8")


def test_autoops_status_counts_health_failed_recoveries(tmp_path: Path) -> None:
    fake_bin, _calls = _fake_autoops_path(tmp_path)
    root = _seed_autoops_root(tmp_path)
    history = root / "data" / "autoops" / "history"
    history.mkdir(parents=True)
    (history / "2026-06-24T120000.json").write_text(
        json.dumps(
            {
                "timestamp": "2026-06-24T12:00:00Z",
                "environment": "paper",
                "drill_mode": "dry_run",
                "failure_class": "health_failed",
                "failure_type": "health_failed",
                "success": True,
            }
        ),
        encoding="utf-8",
    )

    proc = _run_autoops(["status", "--environment", "paper", "--project-root", str(root)], fake_bin=fake_bin)

    assert proc.returncode == 0, proc.stderr
    assert "failure_classes: health_failed" in proc.stdout
    assert "health_failed_recoveries: 1" in proc.stdout


def test_autoops_darwin_launchd_active(tmp_path: Path) -> None:
    fake_bin, calls = _fake_autoops_path(
        tmp_path,
        include_systemctl=False,
        include_launchctl=True,
        launchctl_active=True,
    )
    root = _seed_autoops_root(tmp_path)

    proc = _run_autoops(
        ["status", "--environment", "paper", "--project-root", str(root)],
        fake_bin=fake_bin,
        extra_env={
            "ALGO_AUTOOPS_PLATFORM": "Darwin",
            "ALGO_PAPER_LAUNCHD_LABEL": "com.psuriset.algo.paper",
        },
    )

    assert proc.returncode == 0, proc.stderr
    assert "service_manager: launchd" in proc.stdout
    assert "service_name: com.psuriset.algo.paper" in proc.stdout
    assert "service_active: active" in proc.stdout
    assert "systemctl_unavailable" not in proc.stdout
    assert "launchctl print gui/" in calls.read_text(encoding="utf-8")


def test_autoops_darwin_launchd_missing_uses_process_fallback(tmp_path: Path) -> None:
    fake_bin, calls = _fake_autoops_path(
        tmp_path,
        include_systemctl=False,
        include_launchctl=True,
        launchctl_active=False,
        include_pgrep=True,
        pgrep_active=True,
    )
    root = _seed_autoops_root(tmp_path)

    proc = _run_autoops(
        ["status", "--environment", "paper", "--project-root", str(root)],
        fake_bin=fake_bin,
        extra_env={
            "ALGO_AUTOOPS_PLATFORM": "Darwin",
            "ALGO_PAPER_LAUNCHD_LABEL": "com.psuriset.algo.paper",
            "ALGO_PAPER_PROCESS_PATTERN": "algo_loop.py --paper",
        },
    )

    assert proc.returncode == 0, proc.stderr
    assert "service_manager: process_fallback" in proc.stdout
    assert "service_name: algo_loop.py --paper" in proc.stdout
    assert "service_active: active" in proc.stdout
    assert "systemctl_unavailable" not in proc.stdout
    logged = calls.read_text(encoding="utf-8")
    assert "launchctl print gui/" in logged
    assert "pgrep -f algo_loop.py --paper" in logged


def test_autoops_drill_dry_run_emits_expected_events_and_no_writes(tmp_path: Path) -> None:
    fake_bin, calls = _fake_autoops_path(tmp_path)
    root = _seed_autoops_root(tmp_path)

    proc = _run_autoops(
        ["drill", "--dry-run", "--environment", "paper", "--project-root", str(root)],
        fake_bin=fake_bin,
    )

    assert proc.returncode == 0, proc.stderr
    for event in (
        "AUTOOPS_DRILL_START",
        "AUTOOPS_HEALTH_CHECK",
        "AUTOOPS_ISSUE_CREATED",
        "AUTOOPS_CODEX_STARTED",
        "AUTOOPS_PR_CREATED",
        "AUTOOPS_VALIDATION_PASSED",
        "AUTOOPS_AUTO_MERGED",
        "AUTOOPS_DEPLOY_STARTED",
        "AUTOOPS_DEPLOYED",
        "AUTOOPS_VERIFY_STARTED",
        "AUTOOPS_VERIFIED",
        "AUTOOPS_RECOVERY_COMPLETE",
        "AUTOOPS_DRILL_SUCCESS",
    ):
        assert event in proc.stdout
    history_files = sorted((root / "data" / "autoops" / "history").glob("*.json"))
    assert len(history_files) == 1
    assert history_files[0].name.endswith(".json")
    assert "T" in history_files[0].name
    payload = json.loads(history_files[0].read_text(encoding="utf-8"))
    assert payload["environment"] == "paper"
    assert payload["drill_mode"] == "dry_run"
    assert payload["issue_created"] is False
    assert payload["pr_created"] is False
    assert payload["validation_passed"] is True
    assert payload["merged"] is False
    assert payload["deployed"] is False
    assert payload["verified"] is True
    assert payload["success"] is True
    assert payload["failure_reason"] == ""
    assert payload["failure_type"] == ""
    assert payload["diagnosis"] == ""
    assert payload["recovery_plan"] == ""
    assert payload["improved"] is False
    assert payload["duration_seconds"] >= 0.0
    logged = calls.read_text(encoding="utf-8")
    assert "gh --version" in logged
    forbidden = ("issue create", "issue comment", "issue edit", "pr create", "pr merge", "systemctl")
    assert not any(item in logged for item in forbidden)


def test_bin_algo_routes_autoops_drill_dry_run_without_mutation(tmp_path: Path) -> None:
    fake_bin, calls = _fake_autoops_path(tmp_path)
    root = _seed_autoops_root(tmp_path)
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}

    proc = subprocess.run(
        [str(BIN), "autoops", "drill", "--dry-run", "--project-root", str(root)],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "AUTOOPS_DRILL_SUCCESS dry_run=true" in proc.stdout
    logged = calls.read_text(encoding="utf-8")
    assert "gh --version" in logged
    assert "pr merge" not in logged
    assert "restart" not in logged


@pytest.mark.parametrize("failure_type", FAILURE_TYPES)
def test_autoops_drill_dry_run_injects_each_supported_failure(
    tmp_path: Path,
    failure_type: str,
) -> None:
    fake_bin, calls = _fake_autoops_path(tmp_path)
    root = _seed_autoops_root(tmp_path)

    proc = _run_autoops(
        ["drill", "--dry-run", "--failure", failure_type, "--project-root", str(root)],
        fake_bin=fake_bin,
    )

    assert proc.returncode == 0, proc.stderr
    assert f"AUTOOPS_FAILURE_INJECTED type={failure_type}" in proc.stdout
    assert f"AUTOOPS_DIAGNOSED type={failure_type}" in proc.stdout
    assert f"AUTOOPS_RECOVERY_PLAN type={failure_type}" in proc.stdout
    assert "AUTOOPS_DRILL_SUCCESS dry_run=true" in proc.stdout
    history_files = sorted((root / "data" / "autoops" / "history").glob("*.json"))
    assert len(history_files) == 1
    payload = json.loads(history_files[0].read_text(encoding="utf-8"))
    assert payload["failure_type"] == failure_type
    assert payload["diagnosis"]
    assert payload["recovery_plan"]
    if failure_type == "health_failed":
        assert "AUTOOPS_FAILURE_CLASS class=health_failed" in proc.stdout
        assert "AUTOOPS_HEALTH_CHECK dry_run=true result=unhealthy failure_class=health_failed" in proc.stdout
        assert "AUTOOPS_FAILURE_REPORTER dry_run=true result=issue_created failure_class=health_failed" in proc.stdout
        assert payload["failure_class"] == "health_failed"
        assert payload["recovery_path"] == [
            "check_algo_health_unhealthy",
            "failure_reporter_issue_created",
            "codex_processor_started",
            "validation_ran",
            "recovery_recorded",
        ]
    else:
        assert payload["failure_class"] == ""
        assert payload["recovery_path"] == []
    assert payload["improved"] is True
    assert payload["success"] is True
    assert payload["failure_reason"] == ""
    assert payload["issue_created"] is False
    assert payload["pr_created"] is False
    assert payload["deployed"] is False
    logged = calls.read_text(encoding="utf-8")
    forbidden = ("issue create", "issue comment", "issue edit", "pr create", "pr merge", "systemctl")
    assert not any(item in logged for item in forbidden)


def test_autoops_report_summarizes_history(tmp_path: Path) -> None:
    fake_bin, _calls = _fake_autoops_path(tmp_path)
    root = _seed_autoops_root(tmp_path)
    history = root / "data" / "autoops" / "history"
    history.mkdir(parents=True)
    (history / "2026-06-24T100000.json").write_text(
        json.dumps(
            {
                "timestamp": "2026-06-24T10:00:00Z",
                "host": "test-host",
                "environment": "paper",
                "drill_mode": "dry_run",
                "duration_seconds": 2.0,
                "issue_created": False,
                "pr_created": False,
                "validation_passed": True,
                "merged": False,
                "deployed": False,
                "verified": True,
                "success": True,
                "failure_reason": "",
                "failure_class": "health_failed",
                "failure_type": "health_failed",
                "recovery_path": [
                    "check_algo_health_unhealthy",
                    "failure_reporter_issue_created",
                    "codex_processor_started",
                    "validation_ran",
                    "recovery_recorded",
                ],
            }
        ),
        encoding="utf-8",
    )
    (history / "2026-06-24T110000.json").write_text(
        json.dumps(
            {
                "timestamp": "2026-06-24T11:00:00Z",
                "host": "test-host",
                "environment": "live",
                "drill_mode": "dry_run",
                "duration_seconds": 4.0,
                "issue_created": False,
                "pr_created": False,
                "validation_passed": False,
                "merged": False,
                "deployed": False,
                "verified": False,
                "success": False,
                "failure_reason": "missing_required_paths",
            }
        ),
        encoding="utf-8",
    )

    proc = _run_autoops(["report", "--project-root", str(root)], fake_bin=fake_bin)

    assert proc.returncode == 0, proc.stderr
    assert "AutoOps report" in proc.stdout
    assert "- total drills: 2" in proc.stdout
    assert "- successful drills: 1" in proc.stdout
    assert "- blocked drills: 0" in proc.stdout
    assert "- failed drills: 1" in proc.stdout
    assert "- success %: 50.0" in proc.stdout
    assert "- avg recovery time: 2.000s" in proc.stdout
    assert "- last successful drill: 2026-06-24T10:00:00Z class=health_failed" in proc.stdout
    assert "- last failed drill: 2026-06-24T11:00:00Z reason=missing_required_paths" in proc.stdout
    assert "- failure classes:" in proc.stdout
    assert "  - health_failed: 1" in proc.stdout


def test_autoops_report_classifies_old_safety_block_as_blocked(tmp_path: Path) -> None:
    fake_bin, _calls = _fake_autoops_path(tmp_path)
    root = _seed_autoops_root(tmp_path)
    history = root / "data" / "autoops" / "history"
    history.mkdir(parents=True)
    (history / "2026-06-24T120000.json").write_text(
        json.dumps(
            {
                "timestamp": "2026-06-24T12:00:00Z",
                "environment": "live",
                "drill_mode": "unknown",
                "success": False,
                "failure_reason": "non_dry_run_requires_confirm_and_paper_only",
            }
        ),
        encoding="utf-8",
    )

    proc = _run_autoops(["report", "--project-root", str(root)], fake_bin=fake_bin)

    assert proc.returncode == 0
    assert "- total drills: 1" in proc.stdout
    assert "- blocked drills: 1" in proc.stdout
    assert "- failed drills: 0" in proc.stdout


def test_autoops_plain_drill_blocks_with_actionable_dry_run_hint(tmp_path: Path) -> None:
    fake_bin, _calls = _fake_autoops_path(tmp_path)
    root = _seed_autoops_root(tmp_path)

    proc = _run_autoops(["drill", "--project-root", str(root)], fake_bin=fake_bin)

    assert proc.returncode == 0
    assert (
        'AUTOOPS_DRILL_BLOCKED reason=non_dry_run_requires_confirm_and_paper_only '
        'next="./bin/algo autoops drill --dry-run"'
    ) in proc.stderr
    assert "AUTOOPS_DRILL_FAILED" not in proc.stderr
    history = sorted((root / "data" / "autoops" / "history").glob("*.json"))
    assert len(history) == 1
    payload = json.loads(history[0].read_text(encoding="utf-8"))
    assert payload["blocked"] is True
    assert payload["success"] is False

    report = _run_autoops(["report", "--project-root", str(root)], fake_bin=fake_bin)

    assert report.returncode == 0
    assert "- total drills: 1" in report.stdout
    assert "- blocked drills: 1" in report.stdout
    assert "- failed drills: 0" in report.stdout


def test_autoops_status_full_includes_end_to_end_sections(tmp_path: Path) -> None:
    fake_bin, calls = _fake_full_autoops_path(tmp_path)
    root = _seed_autoops_root(tmp_path)
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        f"echo bin-algo \"$@\" >> {calls}\n"
        "if [[ \"$1 $2\" == \"self-heal --live\" ]]; then echo 'SELF_HEAL status=healthy env=live'; exit 0; fi\n"
        "echo bin-algo \"$@\"\n"
        "exit 0\n",
    )
    history = root / "data" / "autoops" / "history"
    history.mkdir(parents=True)
    (history / "2026-06-24T130000.json").write_text(
        json.dumps({"timestamp": "2026-06-24T13:00:00Z", "success": True}),
        encoding="utf-8",
    )

    proc = _run_autoops(["status", "--live", "--full", "--project-root", str(root)], fake_bin=fake_bin)

    assert proc.returncode == 0, proc.stderr
    assert "- environment: live" in proc.stdout
    assert "AutoOps full status" in proc.stdout
    assert "- algo service: active" in proc.stdout
    assert "- self-heal latest status: healthy" in proc.stdout
    assert "- GitHub CLI authenticated: yes" in proc.stdout
    assert "- required GitHub labels present: yes" in proc.stdout
    assert "- latest AutoOps issue number/status: #155 open" in proc.stdout
    assert "- latest Codex PR number/status: #177 passed" in proc.stdout
    assert "- Codex PR validation workflow present: yes" in proc.stdout
    assert "- guarded auto-merge workflow present: yes" in proc.stdout
    assert "- latest drill result: success" in proc.stdout
    assert "- blocked safety drills counted separately from failures: yes" in proc.stdout
    assert "- options pilot enabled: no" in proc.stdout
    assert "- recommendation: ready" in proc.stdout
    logged = calls.read_text(encoding="utf-8")
    assert "systemctl is-active algo.service" in logged
    assert "bin-algo self-heal --live --dry-run" in logged
    forbidden = ("issue create", "pr merge", "restart")
    assert not any(item in logged for item in forbidden)


def test_autoops_status_full_paper_uses_paper_service_and_self_heal_flag(tmp_path: Path) -> None:
    fake_bin, calls = _fake_full_autoops_path(tmp_path)
    root = _seed_autoops_root(tmp_path)
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        f"echo bin-algo \"$@\" >> {calls}\n"
        "if [[ \"$1 $2\" == \"self-heal --paper\" ]]; then echo 'SELF_HEAL status=healthy env=paper'; exit 0; fi\n"
        "exit 0\n",
    )

    proc = _run_autoops(
        ["status", "--paper", "--full", "--project-root", str(root)],
        fake_bin=fake_bin,
        extra_env={"ALGO_AUTOOPS_PLATFORM": "Linux", "ALGO_PAPER_SERVICE": "paper.service"},
    )

    assert proc.returncode == 0, proc.stderr
    assert "- environment: paper" in proc.stdout
    assert "- service_name: paper.service" in proc.stdout
    logged = calls.read_text(encoding="utf-8")
    assert "systemctl is-active paper.service" in logged
    assert "bin-algo self-heal --paper --dry-run" in logged
    assert "systemctl is-active algo.service" not in logged


def test_autoops_status_full_without_env_hints_live_command(tmp_path: Path) -> None:
    fake_bin, calls = _fake_full_autoops_path(tmp_path)
    root = _seed_autoops_root(tmp_path)
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        f"echo bin-algo \"$@\" >> {calls}\n"
        "if [[ \"$1 $2\" == \"self-heal --paper\" ]]; then echo 'SELF_HEAL status=healthy env=paper'; exit 0; fi\n"
        "exit 0\n",
    )

    proc = _run_autoops(
        ["status", "--full", "--project-root", str(root)],
        fake_bin=fake_bin,
        extra_env={"ALGO_AUTOOPS_PLATFORM": "Linux", "ALGO_PAPER_SERVICE": "paper.service"},
    )

    assert proc.returncode == 0, proc.stderr
    assert 'AUTOOPS_STATUS_HINT next="./bin/algo autoops status --full --live"' in proc.stdout
    assert "- environment: paper" in proc.stdout


def test_autoops_status_full_shows_stale_failed_pr_as_historical(tmp_path: Path) -> None:
    fake_bin, calls = _fake_full_autoops_path(
        tmp_path,
        pr_rows=[
            {
                "number": 151,
                "title": "[PAPER] Codex fix",
                "headRefName": "codex/paper-fix",
                "url": "https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/pull/151",
                "state": "CLOSED",
                "closedAt": "2026-06-01T12:00:00Z",
                "labels": [
                    {"name": "PAPER"},
                    {"name": "environment:paper"},
                    {"name": "codex-validation-failed"},
                ],
            }
        ],
    )
    root = _seed_autoops_root(tmp_path)
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        f"echo bin-algo \"$@\" >> {calls}\n"
        "if [[ \"$1 $2\" == \"self-heal --live\" ]]; then echo 'SELF_HEAL status=healthy env=live'; exit 0; fi\n"
        "exit 0\n",
    )

    proc = _run_autoops(["status", "--live", "--full", "--project-root", str(root)], fake_bin=fake_bin)

    assert proc.returncode == 0, proc.stderr
    assert "- latest Codex PR number/status: #151 failed historical/paper" in proc.stdout
    assert "- stale failed Codex PR: #151 paper/closed" in proc.stdout
    assert "- recommendation: ready" in proc.stdout


def test_autoops_verify_live_is_read_only_and_ready(tmp_path: Path) -> None:
    fake_bin, calls = _fake_full_autoops_path(tmp_path)
    root = _seed_autoops_root(tmp_path)
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        f"echo bin-algo \"$@\" >> {calls}\n"
        "if [[ \"$1 $2\" == \"self-heal --live\" ]]; then echo 'SELF_HEAL status=healthy env=live'; exit 0; fi\n"
        "exit 0\n",
    )
    history = root / "data" / "autoops" / "history"
    history.mkdir(parents=True)
    (history / "2026-06-24T130000.json").write_text(
        json.dumps({"timestamp": "2026-06-24T13:00:00Z", "success": True}),
        encoding="utf-8",
    )

    proc = _run_autoops(["verify", "--live", "--project-root", str(root)], fake_bin=fake_bin)

    assert proc.returncode == 0, proc.stderr
    assert "AUTOOPS_VERIFY_READ_ONLY true" in proc.stdout
    assert "AUTOOPS_VERIFY_CHECK latest_pr_validation=passed" in proc.stdout
    assert "AUTOOPS_VERIFY_STATUS ready=true reason=all_checks_passed" in proc.stdout
    logged = calls.read_text(encoding="utf-8")
    assert "systemctl is-active algo.service" in logged
    assert "bin-algo self-heal --live --dry-run" in logged
    assert "gh auth status" in logged
    assert "gh label list" in logged
    assert "gh pr list" in logged
    forbidden = (
        "issue create",
        "issue comment",
        "issue edit",
        "pr create",
        "pr merge",
        "restart",
        "process_codex",
        "paper-options-diagnostics",
        "submit_order",
    )
    assert not any(item in logged for item in forbidden)


def test_autoops_verify_paper_allows_degraded_missing_review_log_detail(tmp_path: Path) -> None:
    fake_bin, calls = _fake_full_autoops_path(
        tmp_path,
        pr_rows=[
            {
                "number": 188,
                "title": "[PAPER] Codex fix",
                "headRefName": "codex/paper-fix",
                "url": "https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/pull/188",
                "state": "OPEN",
                "labels": [
                    {"name": "PAPER"},
                    {"name": "environment:paper"},
                    {"name": "codex-validation-passed"},
                ],
            }
        ],
        issue_rows=[
            {
                "number": 159,
                "title": "[PAPER] Auto-create dated review log directory before paper runtime/end-day",
                "body": "",
                "url": "https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/issues/159",
                "labels": [{"name": "PAPER"}, {"name": "environment:paper"}],
            }
        ],
    )
    root = _seed_autoops_root(tmp_path)
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        f"echo bin-algo \"$@\" >> {calls}\n"
        "if [[ \"$1 $2\" == \"self-heal --paper\" ]]; then\n"
        "  echo 'SELF_HEAL_LOG_SOURCE source=none reason=missing_review_log path=data/review/2026-06-28/paper_full.log'\n"
        "  echo 'SELF_HEAL status=degraded env=paper reason=missing_review_log path=data/review/2026-06-28/paper_full.log'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
    )
    history = root / "data" / "autoops" / "history"
    history.mkdir(parents=True)
    (history / "2026-06-28T130000.json").write_text(
        json.dumps({"timestamp": "2026-06-28T13:00:00Z", "success": True}),
        encoding="utf-8",
    )

    proc = _run_autoops(
        ["verify", "--paper", "--project-root", str(root)],
        fake_bin=fake_bin,
        extra_env={"ALGO_AUTOOPS_PLATFORM": "Linux", "ALGO_PAPER_SERVICE": "paper.service"},
    )

    assert proc.returncode == 0, proc.stderr
    assert (
        "AUTOOPS_VERIFY_CHECK self_heal=degraded detail=\"SELF_HEAL_LOG_SOURCE "
        "source=none reason=missing_review_log path=data/review/2026-06-28/paper_full.log\""
    ) in proc.stdout
    assert "AUTOOPS_VERIFY_STATUS ready=true reason=all_checks_passed" in proc.stdout
    assert "self_heal_blocked" not in proc.stdout


def test_autoops_verify_live_ignores_old_closed_paper_failed_pr(tmp_path: Path) -> None:
    fake_bin, calls = _fake_full_autoops_path(
        tmp_path,
        pr_rows=[
            {
                "number": 151,
                "title": "[PAPER] Codex fix",
                "headRefName": "codex/paper-fix",
                "url": "https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/pull/151",
                "state": "CLOSED",
                "closedAt": "2026-06-01T12:00:00Z",
                "labels": [
                    {"name": "PAPER"},
                    {"name": "environment:paper"},
                    {"name": "codex-validation-failed"},
                ],
            }
        ],
    )
    root = _seed_autoops_root(tmp_path)
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        f"echo bin-algo \"$@\" >> {calls}\n"
        "if [[ \"$1 $2\" == \"self-heal --live\" ]]; then echo 'SELF_HEAL status=healthy env=live'; exit 0; fi\n"
        "exit 0\n",
    )

    proc = _run_autoops(["verify", "--live", "--project-root", str(root)], fake_bin=fake_bin)

    assert proc.returncode == 0, proc.stderr
    assert 'AUTOOPS_VERIFY_CHECK latest_pr_validation=ignored_stale detail="#151 paper/closed"' in proc.stdout
    assert "AUTOOPS_VERIFY_STATUS ready=true reason=all_checks_passed" in proc.stdout


def test_autoops_verify_live_ignores_failed_pr_with_linked_paper_issue(tmp_path: Path) -> None:
    fake_bin, calls = _fake_full_autoops_path(
        tmp_path,
        pr_rows=[
            {
                "number": 151,
                "title": "Fix AutoOps drill routing",
                "headRefName": "codex/autoops-fix",
                "url": "https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/pull/151",
                "state": "OPEN",
                "labels": [{"name": "codex-validation-failed"}],
                "closingIssuesReferences": [
                    {
                        "number": 150,
                        "title": "[PAPER] AutoOps drill: validation_failed",
                        "body": "Paper drill issue",
                        "labels": [
                            {"name": "environment:paper"},
                            {"name": "processor:mac-paper"},
                        ],
                    }
                ],
            }
        ],
    )
    root = _seed_autoops_root(tmp_path)
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        f"echo bin-algo \"$@\" >> {calls}\n"
        "if [[ \"$1 $2\" == \"self-heal --live\" ]]; then echo 'SELF_HEAL status=healthy env=live'; exit 0; fi\n"
        "exit 0\n",
    )

    proc = _run_autoops(["verify", "--live", "--project-root", str(root)], fake_bin=fake_bin)

    assert proc.returncode == 0, proc.stderr
    assert 'AUTOOPS_VERIFY_CHECK latest_pr_validation=ignored_environment detail="#151 paper/open while verifying live"' in proc.stdout
    assert "AUTOOPS_VERIFY_STATUS ready=true reason=all_checks_passed" in proc.stdout


def test_autoops_verify_live_uses_latest_paper_issue_when_pr_env_unknown(tmp_path: Path) -> None:
    fake_bin, calls = _fake_full_autoops_path(
        tmp_path,
        pr_rows=[
            {
                "number": 151,
                "title": "Fix AutoOps drill routing and issue handling",
                "headRefName": "codex/autoops-fix",
                "url": "https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/pull/151",
                "state": "OPEN",
                "labels": [{"name": "codex-validation-failed"}],
            }
        ],
        issue_rows=[
            {
                "number": 150,
                "title": "[PAPER] AutoOps drill: validation_failed",
                "body": "Paper validation drill",
                "url": "https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/issues/150",
                "labels": [
                    {"name": "environment:paper"},
                    {"name": "processor:mac-paper"},
                ],
            }
        ],
    )
    root = _seed_autoops_root(tmp_path)
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        f"echo bin-algo \"$@\" >> {calls}\n"
        "if [[ \"$1 $2\" == \"self-heal --live\" ]]; then echo 'SELF_HEAL status=healthy env=live'; exit 0; fi\n"
        "exit 0\n",
    )

    proc = _run_autoops(["verify", "--live", "--project-root", str(root)], fake_bin=fake_bin)

    assert proc.returncode == 0, proc.stderr
    assert 'AUTOOPS_VERIFY_CHECK latest_pr_validation=ignored_environment detail="#151 paper/open while verifying live"' in proc.stdout
    assert "AUTOOPS_VERIFY_STATUS ready=true reason=all_checks_passed" in proc.stdout


def test_autoops_verify_live_ignores_failed_pr_with_paper_labels(tmp_path: Path) -> None:
    fake_bin, calls = _fake_full_autoops_path(
        tmp_path,
        pr_rows=[
            {
                "number": 151,
                "title": "Fix AutoOps drill routing",
                "headRefName": "codex/autoops-fix",
                "url": "https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/pull/151",
                "state": "OPEN",
                "labels": [
                    {"name": "environment:paper"},
                    {"name": "processor:mac-paper"},
                    {"name": "codex-validation-failed"},
                ],
            }
        ],
    )
    root = _seed_autoops_root(tmp_path)
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        f"echo bin-algo \"$@\" >> {calls}\n"
        "if [[ \"$1 $2\" == \"self-heal --live\" ]]; then echo 'SELF_HEAL status=healthy env=live'; exit 0; fi\n"
        "exit 0\n",
    )

    proc = _run_autoops(["verify", "--live", "--project-root", str(root)], fake_bin=fake_bin)

    assert proc.returncode == 0, proc.stderr
    assert 'AUTOOPS_VERIFY_CHECK latest_pr_validation=ignored_environment detail="#151 paper/open while verifying live"' in proc.stdout


def test_autoops_verify_live_ignores_failed_pr_with_paper_title(tmp_path: Path) -> None:
    fake_bin, calls = _fake_full_autoops_path(
        tmp_path,
        pr_rows=[
            {
                "number": 151,
                "title": "[PAPER] Fix AutoOps drill routing",
                "headRefName": "codex/autoops-fix",
                "url": "https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/pull/151",
                "state": "OPEN",
                "labels": [{"name": "codex-validation-failed"}],
            }
        ],
    )
    root = _seed_autoops_root(tmp_path)
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        f"echo bin-algo \"$@\" >> {calls}\n"
        "if [[ \"$1 $2\" == \"self-heal --live\" ]]; then echo 'SELF_HEAL status=healthy env=live'; exit 0; fi\n"
        "exit 0\n",
    )

    proc = _run_autoops(["verify", "--live", "--project-root", str(root)], fake_bin=fake_bin)

    assert proc.returncode == 0, proc.stderr
    assert 'AUTOOPS_VERIFY_CHECK latest_pr_validation=ignored_environment detail="#151 paper/open while verifying live"' in proc.stdout


def test_autoops_verify_missing_github_labels_not_ready(tmp_path: Path) -> None:
    fake_bin, calls = _fake_full_autoops_path(tmp_path, labels_present=False)
    root = _seed_autoops_root(tmp_path)
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        f"echo bin-algo \"$@\" >> {calls}\n"
        "if [[ \"$1 $2\" == \"self-heal --live\" ]]; then echo 'SELF_HEAL status=healthy env=live'; exit 0; fi\n"
        "exit 0\n",
    )

    proc = _run_autoops(["verify", "--live", "--project-root", str(root)], fake_bin=fake_bin)

    assert proc.returncode == 1
    assert "AUTOOPS_VERIFY_CHECK github_labels_present=no missing=LIVE" in proc.stdout
    assert "AUTOOPS_VERIFY_STATUS ready=false reason=missing_github_labels" in proc.stdout


def test_autoops_verify_failed_latest_pr_validation_not_ready(tmp_path: Path) -> None:
    fake_bin, calls = _fake_full_autoops_path(tmp_path, validation_failed=True)
    root = _seed_autoops_root(tmp_path)
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        f"echo bin-algo \"$@\" >> {calls}\n"
        "if [[ \"$1 $2\" == \"self-heal --live\" ]]; then echo 'SELF_HEAL status=healthy env=live'; exit 0; fi\n"
        "exit 0\n",
    )

    proc = _run_autoops(["verify", "--live", "--project-root", str(root)], fake_bin=fake_bin)

    assert proc.returncode == 1
    assert 'AUTOOPS_VERIFY_CHECK latest_pr_validation=failed detail="#177 live/open"' in proc.stdout
    assert "AUTOOPS_VERIFY_STATUS ready=false reason=latest_pr_validation_failed" in proc.stdout


def test_autoops_verify_open_paper_failed_pr_blocks_paper(tmp_path: Path) -> None:
    fake_bin, calls = _fake_full_autoops_path(
        tmp_path,
        pr_rows=[
            {
                "number": 188,
                "title": "[PAPER] Codex fix",
                "headRefName": "codex/paper-fix",
                "url": "https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/pull/188",
                "state": "OPEN",
                "labels": [
                    {"name": "PAPER"},
                    {"name": "environment:paper"},
                    {"name": "codex-validation-failed"},
                ],
            }
        ],
    )
    root = _seed_autoops_root(tmp_path)
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        f"echo bin-algo \"$@\" >> {calls}\n"
        "if [[ \"$1 $2\" == \"self-heal --paper\" ]]; then echo 'SELF_HEAL status=healthy env=paper'; exit 0; fi\n"
        "exit 0\n",
    )

    proc = _run_autoops(["verify", "--paper", "--project-root", str(root)], fake_bin=fake_bin)

    assert proc.returncode == 1
    assert 'AUTOOPS_VERIFY_CHECK latest_pr_validation=failed detail="#188 paper/open"' in proc.stdout
    assert "AUTOOPS_VERIFY_STATUS ready=false reason=latest_pr_validation_failed" in proc.stdout


def test_autoops_verify_unknown_open_failed_pr_blocks_live(tmp_path: Path) -> None:
    fake_bin, calls = _fake_full_autoops_path(
        tmp_path,
        pr_rows=[
            {
                "number": 199,
                "title": "Fix AutoOps validation",
                "headRefName": "codex/autoops-validation",
                "url": "https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/pull/199",
                "state": "OPEN",
                "labels": [{"name": "codex-validation-failed"}],
            }
        ],
        issue_rows=[],
    )
    root = _seed_autoops_root(tmp_path)
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        f"echo bin-algo \"$@\" >> {calls}\n"
        "if [[ \"$1 $2\" == \"self-heal --live\" ]]; then echo 'SELF_HEAL status=healthy env=live'; exit 0; fi\n"
        "exit 0\n",
    )

    proc = _run_autoops(["verify", "--live", "--project-root", str(root)], fake_bin=fake_bin)

    assert proc.returncode == 1
    assert 'AUTOOPS_VERIFY_CHECK latest_pr_validation=failed detail="#199 unknown/open"' in proc.stdout
    assert "AUTOOPS_VERIFY_STATUS ready=false reason=latest_pr_validation_failed" in proc.stdout


def test_autoops_verify_closed_or_merged_failed_pr_does_not_block_either_env(tmp_path: Path) -> None:
    fake_bin, calls = _fake_full_autoops_path(
        tmp_path,
        pr_rows=[
            {
                "number": 201,
                "title": "[LIVE] Old failed validation",
                "headRefName": "codex/live-old",
                "url": "https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/pull/201",
                "state": "MERGED",
                "mergedAt": "2026-06-01T12:00:00Z",
                "labels": [
                    {"name": "environment:live"},
                    {"name": "codex-validation-failed"},
                ],
            }
        ],
        issue_rows=[],
    )
    root = _seed_autoops_root(tmp_path)
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        f"echo bin-algo \"$@\" >> {calls}\n"
        "if [[ \"$1 $2\" == \"self-heal --live\" ]]; then echo 'SELF_HEAL status=healthy env=live'; exit 0; fi\n"
        "if [[ \"$1 $2\" == \"self-heal --paper\" ]]; then echo 'SELF_HEAL status=healthy env=paper'; exit 0; fi\n"
        "exit 0\n",
    )

    live = _run_autoops(["verify", "--live", "--project-root", str(root)], fake_bin=fake_bin)
    paper = _run_autoops(["verify", "--paper", "--project-root", str(root)], fake_bin=fake_bin)

    assert live.returncode == 0, live.stderr
    assert paper.returncode == 0, paper.stderr
    assert 'AUTOOPS_VERIFY_CHECK latest_pr_validation=ignored_stale detail="#201 live/closed"' in live.stdout
    assert 'AUTOOPS_VERIFY_CHECK latest_pr_validation=ignored_stale detail="#201 live/closed"' in paper.stdout


@pytest.mark.parametrize(
    ("mode", "trend_long", "live_pilot_enabled", "expected_profile"),
    [
        ("shadow", "SHADOW", False, "shadow"),
        ("live", "LIVE", True, "bounded_live_pilot"),
        ("live", "SHADOW", False, "unrestricted_live"),
    ],
)
def test_autoops_verify_live_github_classification_independent_of_runtime_mode(
    tmp_path: Path,
    mode: str,
    trend_long: str,
    live_pilot_enabled: bool,
    expected_profile: str,
) -> None:
    fake_bin, calls = _fake_full_autoops_path(
        tmp_path,
        pr_rows=[
            {
                "number": 151,
                "title": "[PAPER] Fix AutoOps drill routing",
                "headRefName": "codex/autoops-fix",
                "url": "https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/pull/151",
                "state": "OPEN",
                "labels": [{"name": "codex-validation-failed"}],
            }
        ],
    )
    root = _seed_autoops_root(tmp_path)
    _write_runtime_config(
        root,
        mode=mode,
        trend_long=trend_long,
        live_pilot_enabled=live_pilot_enabled,
    )
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        f"echo bin-algo \"$@\" >> {calls}\n"
        "if [[ \"$1 $2\" == \"self-heal --live\" ]]; then echo 'SELF_HEAL status=healthy env=live'; exit 0; fi\n"
        "exit 0\n",
    )

    before = (root / "config" / "default.yaml").read_text(encoding="utf-8")
    proc = _run_autoops(["verify", "--live", "--project-root", str(root)], fake_bin=fake_bin)
    after = (root / "config" / "default.yaml").read_text(encoding="utf-8")

    assert proc.returncode == 0, proc.stderr
    assert before == after
    assert f"AUTOOPS_VERIFY_CHECK runtime_profile={expected_profile}" in proc.stdout
    assert 'AUTOOPS_VERIFY_CHECK latest_pr_validation=ignored_environment detail="#151 paper/open while verifying live"' in proc.stdout
    assert "AUTOOPS_VERIFY_STATUS ready=true reason=all_checks_passed" in proc.stdout
    logged = calls.read_text(encoding="utf-8")
    forbidden = ("issue create", "issue comment", "issue edit", "pr create", "pr merge", "restart", "submit_order")
    assert not any(item in logged for item in forbidden)


def test_autoops_verify_live_bounded_pilot_disabled_options_do_not_block(tmp_path: Path) -> None:
    fake_bin, calls = _fake_full_autoops_path(tmp_path)
    root = _seed_autoops_root(tmp_path)
    _write_runtime_config(root, mode="live", trend_long="LIVE", live_pilot_enabled=True)
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        f"echo bin-algo \"$@\" >> {calls}\n"
        "if [[ \"$1 $2\" == \"self-heal --live\" ]]; then echo 'SELF_HEAL status=healthy env=live'; exit 0; fi\n"
        "exit 0\n",
    )

    proc = _run_autoops(["verify", "--live", "--project-root", str(root)], fake_bin=fake_bin)

    assert proc.returncode == 0, proc.stderr
    assert "AUTOOPS_VERIFY_CHECK runtime_profile=bounded_live_pilot" in proc.stdout
    assert "AUTOOPS_VERIFY_CHECK options_enabled=no" in proc.stdout
    assert "AUTOOPS_VERIFY_CHECK options_route_active=no" in proc.stdout
    assert "AUTOOPS_VERIFY_DETAIL options_gate=not_applicable_for_bounded_live_pilot" in proc.stdout
    assert "AUTOOPS_VERIFY_STATUS ready=true reason=all_checks_passed" in proc.stdout


def test_autoops_verify_live_bounded_pilot_malformed_caps_block(tmp_path: Path) -> None:
    fake_bin, calls = _fake_full_autoops_path(tmp_path)
    root = _seed_autoops_root(tmp_path)
    _write_runtime_config(
        root,
        mode="live",
        trend_long="LIVE",
        live_pilot_enabled=True,
        malformed_pilot_caps=True,
    )
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        f"echo bin-algo \"$@\" >> {calls}\n"
        "if [[ \"$1 $2\" == \"self-heal --live\" ]]; then echo 'SELF_HEAL status=healthy env=live'; exit 0; fi\n"
        "exit 0\n",
    )

    proc = _run_autoops(["verify", "--live", "--project-root", str(root)], fake_bin=fake_bin)

    assert proc.returncode == 1
    assert "AUTOOPS_VERIFY_CHECK runtime_profile=bounded_live_pilot" in proc.stdout
    assert "AUTOOPS_VERIFY_DETAIL runtime_profile_blocking_reasons=live_pilot_max_trades_per_day_invalid" in proc.stdout
    assert "AUTOOPS_VERIFY_STATUS ready=false reason=live_pilot_max_trades_per_day_invalid" in proc.stdout


def test_autoops_confirmed_paper_drill_refuses_live_fedora_before_writes(tmp_path: Path) -> None:
    fake_bin, calls = _fake_confirmed_autoops_path(tmp_path)
    root = _seed_autoops_root(tmp_path)

    proc = _run_autoops(
        [
            "drill",
            "--environment",
            "live",
            "--paper-only",
            "--confirm",
            "--failure",
            "service_down",
            "--project-root",
            str(root),
        ],
        fake_bin=fake_bin,
        extra_env={"ALGO_AUTOOPS_PLATFORM": "Linux"},
    )

    assert proc.returncode == 2
    assert "AUTOOPS_DRILL_FAILED reason=paper_only_drill_refuses_live_environment" in proc.stderr
    assert not calls.exists()
    assert not (root / "data" / "autoops" / "history").exists()


def test_autoops_confirmed_paper_drill_refuses_linux_before_history(tmp_path: Path) -> None:
    fake_bin, calls = _fake_confirmed_autoops_path(tmp_path)
    root = _seed_autoops_root(tmp_path)

    proc = _run_autoops(
        [
            "drill",
            "--paper-only",
            "--confirm",
            "--failure",
            "service_down",
            "--project-root",
            str(root),
        ],
        fake_bin=fake_bin,
        extra_env={"ALGO_AUTOOPS_PLATFORM": "Linux"},
    )

    assert proc.returncode == 2
    assert "AUTOOPS_DRILL_FAILED reason=paper_only_drill_requires_mac" in proc.stderr
    assert not calls.exists()
    assert not (root / "data" / "autoops" / "history").exists()


def test_autoops_confirmed_paper_drill_creates_issue_processes_pr_and_verifies(tmp_path: Path) -> None:
    fake_bin, calls = _fake_confirmed_autoops_path(tmp_path)
    root = _seed_autoops_root(tmp_path)
    _write_executable(
        root / "scripts" / "process_codex_issues_local.sh",
        "#!/usr/bin/env bash\n"
        f"echo processor \"$@\" >> {calls}\n"
        "exit 0\n",
    )
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        f"echo bin-algo \"$@\" >> {calls}\n"
        "exit 0\n",
    )
    _write_executable(
        root / "scripts" / "check_algo_health.sh",
        "#!/usr/bin/env bash\n"
        f"echo check-health \"$@\" >> {calls}\n"
        "exit 0\n",
    )

    proc = _run_autoops(
        [
            "drill",
            "--paper-only",
            "--confirm",
            "--failure",
            "allocator_silent_drop",
            "--project-root",
            str(root),
        ],
        fake_bin=fake_bin,
        extra_env={"ALGO_AUTOOPS_PLATFORM": "Darwin"},
    )

    assert proc.returncode == 0, proc.stderr
    assert "AUTOOPS_ISSUE_CREATED issue=321 labels=autoops-drill,environment:paper,processor:mac-paper,codex,auto-fix" in proc.stdout
    assert "AUTOOPS_CODEX_STARTED issue=321" in proc.stdout
    assert "AUTOOPS_PR_CREATED pr=654 issue=321" in proc.stdout
    assert "AUTOOPS_VALIDATION_PASSED pr=654" in proc.stdout
    assert "AUTOOPS_AUTO_MERGED pr=654" in proc.stdout
    assert "AUTOOPS_VERIFIED environment=paper" in proc.stdout
    assert "AUTOOPS_DRILL_SUCCESS dry_run=false environment=paper" in proc.stdout
    logged = calls.read_text(encoding="utf-8")
    assert "gh issue create" in logged
    assert "--label autoops-drill" in logged
    assert "--label environment:paper" in logged
    assert "--label processor:mac-paper" in logged
    assert "--label codex" in logged
    assert "--label auto-fix" in logged
    assert "--label paper" not in logged
    assert "processor --issue 321 --limit 1" in logged
    assert "gh pr list --state all --search #321" in logged
    assert "pytest tests/test_autoops" in logged
    assert "bin-algo paper-options-diagnostics --user paper_bot --symbol QQQ" in logged
    assert "check-health --dry-run PAPER" in logged
    assert "restart" not in logged

    history_files = sorted((root / "data" / "autoops" / "history").glob("*.json"))
    assert len(history_files) == 1
    payload = json.loads(history_files[0].read_text(encoding="utf-8"))
    assert payload["environment"] == "paper"
    assert payload["drill_mode"] == "paper"
    assert payload["failure_type"] == "allocator_silent_drop"
    assert payload["issue_created"] is True
    assert payload["issue_number"] == 321
    assert payload["issue_labels"] == [
        "autoops-drill",
        "environment:paper",
        "processor:mac-paper",
        "codex",
        "auto-fix",
    ]
    assert payload["pr_created"] is True
    assert payload["pr_number"] == 654
    assert payload["validation_passed"] is True
    assert payload["merged"] is True
    assert payload["deployed"] is False
    assert payload["verified"] is True
    assert payload["improved"] is True
    assert payload["success"] is True
    assert payload["failure_reason"] == ""


def test_autoops_confirmed_health_failed_records_full_recovery_path(tmp_path: Path) -> None:
    fake_bin, calls = _fake_confirmed_autoops_path(tmp_path)
    root = _seed_autoops_root(tmp_path)
    _write_executable(
        root / "scripts" / "process_codex_issues_local.sh",
        "#!/usr/bin/env bash\n"
        f"echo processor \"$@\" >> {calls}\n"
        "exit 0\n",
    )
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        f"echo bin-algo \"$@\" >> {calls}\n"
        "exit 0\n",
    )
    _write_executable(
        root / "scripts" / "check_algo_health.sh",
        "#!/usr/bin/env bash\n"
        f"echo check-health \"$@\" >> {calls}\n"
        "exit 0\n",
    )

    proc = _run_autoops(
        [
            "drill",
            "--paper-only",
            "--confirm",
            "--failure",
            "health_failed",
            "--project-root",
            str(root),
        ],
        fake_bin=fake_bin,
        extra_env={"ALGO_AUTOOPS_PLATFORM": "Darwin"},
    )

    assert proc.returncode == 0, proc.stderr
    assert "AUTOOPS_FAILURE_CLASS class=health_failed" in proc.stdout
    assert "AUTOOPS_HEALTH_CHECK result=unhealthy failure_class=health_failed" in proc.stdout
    assert "AUTOOPS_ISSUE_CREATED issue=321 labels=autoops-drill,environment:paper,processor:mac-paper,codex,auto-fix" in proc.stdout
    assert "AUTOOPS_FAILURE_REPORTER result=issue_created failure_class=health_failed" in proc.stdout
    assert "AUTOOPS_CODEX_STARTED issue=321" in proc.stdout
    assert "AUTOOPS_VALIDATION_PASSED pr=654" in proc.stdout
    assert "AUTOOPS_VALIDATION_RAN result=passed failure_class=health_failed" in proc.stdout
    assert "AUTOOPS_RECOVERY_PATH_RECORDED failure_class=health_failed" in proc.stdout

    logged = calls.read_text(encoding="utf-8")
    assert "processor --issue 321 --limit 1" in logged
    assert "check-health --dry-run PAPER" in logged
    body = root / "data" / "autoops" / "autoops_drill_issue.md"
    body_text = body.read_text(encoding="utf-8")
    assert "- failure_class: health_failed" in body_text
    assert "- recovery_path: check_algo_health_unhealthy,failure_reporter_issue_created,codex_processor_started,validation_ran,recovery_recorded" in body_text

    history_files = sorted((root / "data" / "autoops" / "history").glob("*.json"))
    assert len(history_files) == 1
    payload = json.loads(history_files[0].read_text(encoding="utf-8"))
    assert payload["failure_class"] == "health_failed"
    assert payload["failure_type"] == "health_failed"
    assert payload["issue_created"] is True
    assert payload["pr_created"] is True
    assert payload["validation_passed"] is True
    assert payload["verified"] is True
    assert payload["success"] is True
    assert payload["recovery_path"] == [
        "check_algo_health_unhealthy",
        "failure_reporter_issue_created",
        "codex_processor_started",
        "validation_ran",
        "recovery_recorded",
    ]


def test_autoops_confirmed_paper_drill_retries_without_optional_drill_label(tmp_path: Path) -> None:
    fake_bin, calls = _fake_optional_label_missing_autoops_path(tmp_path)
    root = _seed_autoops_root(tmp_path)
    _write_executable(
        root / "scripts" / "process_codex_issues_local.sh",
        "#!/usr/bin/env bash\n"
        f"echo processor \"$@\" >> {calls}\n"
        "exit 0\n",
    )
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        f"echo bin-algo \"$@\" >> {calls}\n"
        "exit 0\n",
    )
    _write_executable(
        root / "scripts" / "check_algo_health.sh",
        "#!/usr/bin/env bash\n"
        f"echo check-health \"$@\" >> {calls}\n"
        "exit 0\n",
    )

    proc = _run_autoops(
        [
            "drill",
            "--paper-only",
            "--confirm",
            "--failure",
            "validation_failed",
            "--project-root",
            str(root),
        ],
        fake_bin=fake_bin,
        extra_env={"ALGO_AUTOOPS_PLATFORM": "Darwin"},
    )

    assert proc.returncode == 0, proc.stderr
    assert "AUTOOPS_ISSUE_CREATED issue=321 labels=environment:paper,processor:mac-paper,codex,auto-fix" in proc.stdout
    logged = calls.read_text(encoding="utf-8")
    assert logged.count("gh issue create") == 2
    assert "--label environment:paper" in logged
    assert "--label processor:mac-paper" in logged
    assert "--label codex" in logged
    assert "--label auto-fix" in logged
    assert "--label paper" not in logged
    history_files = sorted((root / "data" / "autoops" / "history").glob("*.json"))
    payload = json.loads(history_files[0].read_text(encoding="utf-8"))
    assert payload["issue_labels"] == ["environment:paper", "processor:mac-paper", "codex", "auto-fix"]


def test_autoops_confirmed_paper_drill_records_github_issue_failure_output(tmp_path: Path) -> None:
    fake_bin, calls = _fake_issue_failure_autoops_path(tmp_path)
    root = _seed_autoops_root(tmp_path)

    proc = _run_autoops(
        [
            "drill",
            "--paper-only",
            "--confirm",
            "--failure",
            "validation_failed",
            "--project-root",
            str(root),
        ],
        fake_bin=fake_bin,
        extra_env={"ALGO_AUTOOPS_PLATFORM": "Darwin"},
    )

    assert proc.returncode == 2
    assert "AUTOOPS_GITHUB_ISSUE_CREATE_FAILED rc=1" in proc.stderr
    assert "GraphQL:_could_not_resolve_label_auto-fix" in proc.stderr.replace(" ", "_")
    assert "AUTOOPS_DRILL_FAILED reason=github_issue_failed rc=1 output=GraphQL: could not resolve label auto-fix" in proc.stderr
    logged = calls.read_text(encoding="utf-8")
    assert "--label environment:paper" in logged
    assert "--label processor:mac-paper" in logged
    assert "--label paper" not in logged

    history_files = sorted((root / "data" / "autoops" / "history").glob("*.json"))
    assert len(history_files) == 1
    payload = json.loads(history_files[0].read_text(encoding="utf-8"))
    assert payload["success"] is False
    assert payload["failure_reason"] == "github_issue_failed rc=1 output=GraphQL: could not resolve label auto-fix"
    assert payload["github_issue_create_rc"] == 1
    assert payload["github_issue_create_output"] == "GraphQL: could not resolve label auto-fix"
