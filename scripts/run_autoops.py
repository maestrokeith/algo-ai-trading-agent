#!/usr/bin/env python3
"""Read-only AutoOps status and drill commands for Algo."""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import socket
import getpass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LIVE_DEPLOY_HOSTNAME = "algosphere-live-host"
SYSTEMCTL = "/usr/bin/systemctl"
AUTOOPS_EVENTS = (
    "AUTOOPS_HEALTH_CHECK",
    "AUTOOPS_ISSUE_CREATED",
    "AUTOOPS_CODEX_STARTED",
    "AUTOOPS_PR_CREATED",
    "AUTOOPS_VALIDATION_PASSED",
    "AUTOOPS_VALIDATION_FAILED",
    "AUTOOPS_AUTO_MERGED",
    "AUTOOPS_DEPLOY_STARTED",
    "AUTOOPS_DEPLOYED",
    "AUTOOPS_VERIFY_STARTED",
    "AUTOOPS_VERIFIED",
    "AUTOOPS_RECOVERY_COMPLETE",
    "AUTOOPS_DRILL_START",
    "AUTOOPS_DRILL_SUCCESS",
    "AUTOOPS_DRILL_FAILED",
)

REQUIRED_AUTOOPS_PATHS = (
    "scripts/check_algo_health.sh",
    "scripts/report_algo_failure_to_github.sh",
    "scripts/run_self_heal.py",
    "scripts/process_codex_issues_local.sh",
    ".github/workflows/codex-pr-validation.yml",
    ".github/workflows/codex-auto-merge.yml",
)

REQUIRED_GITHUB_LABELS = (
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
)

GITHUB_LABEL_METADATA = {
    "codex": ("0366d6", "Issue can be routed to Codex automation."),
    "auto-fix": ("0e8a16", "Automation may attempt a code fix."),
    "algo-health": ("d93f0b", "Algo health or trading-flow regression."),
    "algo-failure": ("d93f0b", "Algo failure requiring investigation."),
    "LIVE": ("b60205", "Live environment issue."),
    "PAPER": ("c5def5", "Paper environment issue."),
    "environment:live": ("b60205", "Live trading environment."),
    "environment:paper": ("c5def5", "Paper trading environment."),
    "severity:high": ("b60205", "High severity automation issue."),
    "severity:medium": ("fbca04", "Medium severity automation issue."),
    "processor:fedora-live": ("5319e7", "Routed to Fedora live processor."),
    "processor:live-linux": ("5319e7", "Live Linux Codex processor"),
    "processor:mac-paper": ("5319e7", "Routed to macOS paper processor."),
}

AUTOOPS_FAILURE_SCENARIOS: dict[str, tuple[str, str]] = {
    "health_failed": (
        "check_algo_health.sh returned unhealthy and the failure reporter opened an actionable issue",
        "route health evidence into a Codex repair issue, run the local processor, require validation, and record recovery verification",
    ),
    "service_down": (
        "systemd service is inactive or failed",
        "collect journal evidence, open/route issue, validate fix, then run guarded service restart verification",
    ),
    "premarket_missing": (
        "premarket artifacts are missing or stale",
        "rerun premarket readiness diagnostics and route data collection or provider failure evidence",
    ),
    "broker_auth_failed": (
        "broker authentication failed before trading actions",
        "verify credential environment wiring and block trading until paper/live auth is restored",
    ),
    "stale_market_data": (
        "market data heartbeat or quote freshness is stale",
        "diagnose provider freshness, block unsafe entries, and verify fresh quotes before recovery",
    ),
    "allocator_silent_drop": (
        "entry flow dropped between entry evaluation and allocator decision",
        "capture missing-flow logs and route a Codex fix for queue or allocator trace wiring",
    ),
    "order_submit_failed": (
        "order submit path failed after order intent",
        "collect broker response and execution diagnostics, then verify order status handling",
    ),
    "paper_options_diagnostics_failed": (
        "paper options diagnostic route failed before confirming option selection",
        "run mock paper-options diagnostics and fix option chain or routing regressions",
    ),
    "validation_failed": (
        "Codex PR validation failed",
        "keep merge blocked, attach validation logs, and route follow-up fix to Codex or human review",
    ),
    "github_issue_failed": (
        "GitHub issue creation failed or gh CLI was unavailable",
        "write local evidence artifact and retry issue creation after GitHub CLI/auth recovery",
    ),
    "codex_processor_failed": (
        "Codex issue processor failed before opening a PR",
        "preserve prompt/log artifacts, label for human review, and rerun processor after fixing tooling",
    ),
    "auto_merge_blocked": (
        "guarded auto-merge found a blocking PR condition",
        "keep deploy blocked until validation labels, merge state, and review gates are corrected",
    ),
}


def _run(
    args: Sequence[str],
    *,
    cwd: Path = PROJECT_ROOT,
    timeout: float = 5.0,
) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            list(args),
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return 127, "command_not_found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    return proc.returncode, proc.stdout.strip()


def _load_autoops_config(root: Path) -> dict[str, bool]:
    path = root / "config" / "default.yaml"
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        payload = {}
    section = payload.get("autoops") if isinstance(payload, Mapping) else {}
    if not isinstance(section, Mapping):
        section = {}
    return {
        "live_auto_deploy_enabled": bool(section.get("live_auto_deploy_enabled", False)),
        "live_auto_restart_enabled": bool(section.get("live_auto_restart_enabled", False)),
        "live_post_deploy_verify_enabled": bool(section.get("live_post_deploy_verify_enabled", False)),
        "live_end_day_analysis_enabled": bool(section.get("live_end_day_analysis_enabled", False)),
        "live_end_day_issue_enabled": bool(section.get("live_end_day_issue_enabled", False)),
        "live_end_day_codex_enabled": bool(section.get("live_end_day_codex_enabled", False)),
        "live_end_day_codex_autostart_enabled": bool(section.get("live_end_day_codex_autostart_enabled", False)),
    }


def _git_tree_clean(root: Path) -> tuple[bool, str]:
    rc, out = _run(["git", "status", "--porcelain"], cwd=root, timeout=10.0)
    if rc != 0:
        return False, out or f"git_status_rc_{rc}"
    return out.strip() == "", out


def _analyzer_has_hard_error(output: str) -> bool:
    text = str(output or "").lower()
    return "classification=hard_error" in text or " hard_error" in text or "runtime traceback" in text


def _passwordless_sudo_status(root: Path = PROJECT_ROOT) -> tuple[bool, str]:
    rc, out = _run(["sudo", "-n", "true"], cwd=root, timeout=5.0)
    return rc == 0, out or f"sudo_true_rc_{rc}"


def _user_systemd_timer_ready(unit: str, root: Path = PROJECT_ROOT) -> tuple[bool, str]:
    if not shutil.which("systemctl"):
        return False, "systemctl_unavailable"
    enabled_rc, enabled_out = _run(["systemctl", "--user", "is-enabled", unit], cwd=root, timeout=3.0)
    active_rc, active_out = _run(["systemctl", "--user", "is-active", unit], cwd=root, timeout=3.0)
    enabled = (enabled_out.splitlines()[0] if enabled_out else f"rc_{enabled_rc}").strip()
    active = (active_out.splitlines()[0] if active_out else f"rc_{active_rc}").strip()
    return enabled_rc == 0 and enabled == "enabled" and active_rc == 0 and active == "active", (
        f"enabled={enabled} active={active}"
    )


def _sudoers_guidance() -> str:
    username = getpass.getuser()
    lines = [
        "Configure:",
        "",
        "sudo visudo -f /etc/sudoers.d/algo-autoops",
        "",
        "Add:",
        "",
        f"{username} ALL=(root) NOPASSWD: {SYSTEMCTL} restart algo.service",
        f"{username} ALL=(root) NOPASSWD: {SYSTEMCTL} is-active algo.service",
        f"{username} ALL=(root) NOPASSWD: {SYSTEMCTL} status algo.service",
    ]
    return "\n".join(lines)


def _github_auth_guidance() -> str:
    return "\n".join(
        [
            "GitHub auth guidance:",
            "",
            "Run:",
            "gh auth status",
            "gh auth login",
            "gh api graphql -f query='{ viewer { login } }'",
            "",
            "If interactive shell works but systemd fails, export a token into the user systemd environment:",
            "systemctl --user import-environment GH_TOKEN GITHUB_TOKEN",
            "systemctl --user restart algosphere-live-endday-analysis.timer",
            "",
            "Or configure the service EnvironmentFile:",
            "~/.config/algosphere/github.env",
            "containing:",
            "GH_TOKEN=...",
            "with permissions 600.",
            "",
            "Setup:",
            "mkdir -p ~/.config/algosphere",
            "chmod 700 ~/.config/algosphere",
            "chmod 600 ~/.config/algosphere/github.env",
        ]
    )


def _github_auth_error_reason(output: str, *, default: str) -> str:
    low = str(output or "").lower()
    if "http 401" in low or "bad credentials" in low:
        return "bad_credentials_graphql"
    if "not found" in low or "could not resolve" in low:
        return "repo_access_failed"
    if "gh_auth_rc_" in low:
        return "gh_auth_failed"
    return default


def _github_auth_check(root: Path = PROJECT_ROOT) -> tuple[bool, str]:
    if not shutil.which("gh"):
        return False, "gh_unavailable"
    checks: tuple[tuple[str, Sequence[str]], ...] = (
        ("auth_status", ["gh", "auth", "status"]),
        ("graphql", ["gh", "api", "graphql", "-f", "query={ viewer { login } }"]),
        ("issue_list", ["gh", "issue", "list", "--repo", "YOUR_GITHUB_ORG/algo-ai-trading-agent", "--limit", "1"]),
        ("pr_list", ["gh", "pr", "list", "--repo", "YOUR_GITHUB_ORG/algo-ai-trading-agent", "--limit", "1"]),
    )
    details: list[str] = []
    for name, command in checks:
        rc, out = _run(command, cwd=root, timeout=15.0)
        if rc != 0:
            reason = _github_auth_error_reason(out, default=f"{name}_failed")
            summary = (out.splitlines()[0] if out else f"{name}_rc_{rc}")[:300]
            return False, f"reason={reason} check={name} detail={summary!r}"
        details.append(f"{name}=ok")
    return True, " ".join(details)


def _github_auth_output_has_error(output: str) -> bool:
    low = str(output or "").lower()
    return "http 401" in low or "bad credentials" in low or "try authenticating with: gh auth login" in low


def _deploy_latest(root: Path, *, environment: str) -> int:
    cfg = _load_autoops_config(root)
    host = socket.gethostname()
    print(f"AUTOOPS_DEPLOY_START env={environment} host={host}")
    if environment != "live":
        print("AUTOOPS_DEPLOY_STATUS success=false reason=live_only")
        return 1
    if not bool(cfg["live_auto_deploy_enabled"]):
        print("AUTOOPS_DEPLOY_STATUS success=false reason=deploy_disabled")
        return 1
    if host != LIVE_DEPLOY_HOSTNAME:
        print(f"AUTOOPS_DEPLOY_STATUS success=false reason=wrong_hostname expected={LIVE_DEPLOY_HOSTNAME} actual={host}")
        return 1
    clean, detail = _git_tree_clean(root)
    if not clean:
        print(f"AUTOOPS_DEPLOY_STATUS success=false reason=dirty_git_tree detail={detail!r}")
        return 1
    rc, out = _run(["git", "pull", "--rebase"], cwd=root, timeout=120.0)
    print(f"AUTOOPS_DEPLOY_CHECK git_pull=rc_{rc}")
    if rc != 0:
        print(f"AUTOOPS_DEPLOY_STATUS success=false reason=git_pull_failed detail={out!r}")
        return 1
    if not bool(cfg["live_auto_restart_enabled"]):
        print("AUTOOPS_DEPLOY_STATUS success=false reason=restart_disabled")
        return 1
    sudo_ok, sudo_detail = _passwordless_sudo_status(root)
    print(f"AUTOOPS_DEPLOY_CHECK passwordless_sudo={'yes' if sudo_ok else 'no'}")
    if not sudo_ok:
        print("AUTOOPS_DEPLOY_STATUS failed reason=passwordless_sudo_not_configured")
        print(_sudoers_guidance())
        if sudo_detail:
            print(f"AUTOOPS_DEPLOY_DETAIL sudo={sudo_detail!r}")
        return 1
    rc, out = _run(["sudo", "-n", SYSTEMCTL, "restart", "algo.service"], cwd=root, timeout=30.0)
    print(f"AUTOOPS_DEPLOY_CHECK restart=rc_{rc}")
    if rc != 0:
        print(f"AUTOOPS_DEPLOY_STATUS success=false reason=restart_failed detail={out!r}")
        return 1
    time.sleep(3.0)
    rc, out = _run(["sudo", "-n", SYSTEMCTL, "is-active", "algo.service"], cwd=root, timeout=10.0)
    active = out.splitlines()[0] if out else ""
    print(f"AUTOOPS_DEPLOY_CHECK service_active={active or f'rc_{rc}'}")
    if rc != 0 or active != "active":
        status_rc, status_out = _run(["sudo", "-n", SYSTEMCTL, "status", "algo.service"], cwd=root, timeout=10.0)
        print(f"AUTOOPS_DEPLOY_CHECK service_status=rc_{status_rc}")
        if status_out:
            print(status_out)
        print(f"AUTOOPS_DEPLOY_STATUS success=false reason=service_inactive detail={out!r}")
        return 1
    if not bool(cfg["live_post_deploy_verify_enabled"]):
        print("AUTOOPS_DEPLOY_STATUS success=false reason=post_deploy_verify_disabled")
        return 1
    rc, out = _run(
        [sys.executable, "scripts/analyze_algo_logs.py", "--live", "--lookback-minutes", "10", "--dry-run"],
        cwd=root,
        timeout=60.0,
    )
    print(f"AUTOOPS_DEPLOY_CHECK analyzer=rc_{rc}")
    if out:
        print(out)
    if rc != 0 or _analyzer_has_hard_error(out):
        print("AUTOOPS_DEPLOY_STATUS success=false reason=analyzer_hard_error")
        return 1
    print("AUTOOPS_DEPLOY_STATUS success=true reason=deployed_and_verified")
    return 0


def _today_local() -> str:
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def _endday_commands(root: Path, *, dry_run: bool = False) -> list[list[str]]:
    algo = str(root / "bin" / "algo")
    today = _today_local()
    commands = [
        [algo, "capture-metrics", "--end-day", "--live"],
        [algo, "end-day", "--live"],
        [algo, "research-feedback", today, "--user", "live_bot"],
        [sys.executable, "scripts/check_positions.py"],
        [sys.executable, "scripts/analyze_algo_logs.py", "--live", "--lookback-minutes", "390"],
    ]
    if dry_run:
        for command in commands:
            if command[:3] == [sys.executable, "scripts/analyze_algo_logs.py", "--live"]:
                command.append("--dry-run")
            elif "capture-metrics" in command:
                command.append("--dry-run")
    return commands


def _output_has_actionable_issue(text: str) -> bool:
    low = str(text or "").lower()
    if _log_analyzer_output_is_suppressed_only(str(text or "")):
        return False
    if re.search(r"issues detected=([1-9]\d*)", low):
        return True
    markers = (
        "traceback",
        "modulenotfounderror",
        "exception:",
        "status=failure",
        "failure_detected",
        "autoops_verify_status ready=false",
        "self_heal status=failure_detected",
    )
    return any(marker in low for marker in markers)


def _log_analyzer_output_is_suppressed_only(text: str) -> bool:
    """Return true when analyze_algo_logs found only already-suppressed findings."""
    if "log_analyzer" not in text.lower():
        return False
    detected = re.search(r"issues detected=([0-9]+)", text, re.IGNORECASE)
    suppressed = re.search(r"issues suppressed=([0-9]+)", text, re.IGNORECASE)
    if not detected or not suppressed:
        return False
    detected_count = int(detected.group(1))
    suppressed_count = int(suppressed.group(1))
    if detected_count <= 0 or suppressed_count < detected_count:
        return False
    issue_created = re.search(r"github issue created=([0-9]+)", text, re.IGNORECASE)
    if issue_created and int(issue_created.group(1)) > 0:
        return False
    routable_markers = (
        "ISSUE_CREATED",
        "ISSUE_DUPLICATE",
        "ISSUE_ROUTING",
        "LOG_ANALYZER_DRY_RUN_ISSUE",
    )
    return not any(marker in text for marker in routable_markers)


def _endday_severity(results: Sequence[tuple[list[str], int, str]]) -> str:
    joined = "\n".join(output for _command, _rc, output in results).lower()
    if any(rc != 0 for _command, rc, _output in results) or "traceback" in joined or "modulenotfounderror" in joined:
        return "high"
    return "medium"


def _build_endday_issue_body(results: Sequence[tuple[list[str], int, str]], *, severity: str) -> str:
    sections: list[str] = [
        "Automated live end-day analysis found actionable issues.\n",
        f"- Environment: LIVE\n- Severity: {severity}\n- Generated: {_utc_timestamp()}\n",
        "## Command Output",
    ]
    for command, rc, output in results:
        rendered = " ".join(command)
        sections.append(
            "\n### `%s`\n\n- exit_code: %s\n\n```text\n%s\n```"
            % (rendered, rc, (output or "").strip()[-4000:])
        )
    sections.append(
        "\n## Report Paths\n\n"
        "- data/review/\n"
        "- reports/\n"
        "- data/log_analysis/\n"
        "\n## Codex instructions\n\n"
        "- git pull --rebase\n"
        "- investigate root cause\n"
        "- add regression test\n"
        "- run targeted tests\n"
        "- git push\n"
    )
    return "\n".join(sections)


def _create_endday_issue(root: Path, results: Sequence[tuple[list[str], int, str]], *, severity: str) -> tuple[int, str]:
    processor_label = "processor:live-linux"
    labels = ["codex", "auto-fix", "algo-failure", "environment:live", processor_label, f"severity:{severity}"]
    available_labels, failed_labels = _ensure_github_labels(labels, root=root)
    missing_core = [label for label in labels if label in failed_labels or label not in available_labels]
    if missing_core:
        return 1, "missing_required_labels=" + ",".join(missing_core)
    body = _build_endday_issue_body(results, severity=severity)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as handle:
        handle.write(body)
        body_path = handle.name
    try:
        args = [
            "gh",
            "issue",
            "create",
            "--title",
            f"[LIVE] End-day analysis found actionable issues ({_today_local()})",
            "--body-file",
            body_path,
        ]
        for label in labels:
            args.extend(["--label", label])
        rc, out = _run(args, cwd=root, timeout=30.0)
        if rc != 0:
            return rc, out
        return rc, out
    finally:
        Path(body_path).unlink(missing_ok=True)


def _parse_issue_numbers(text: str) -> list[int]:
    return [int(match.group(1)) for match in re.finditer(r"/issues/(\d+)", text or "")]


def _parse_analyzer_issue_numbers(text: str) -> list[int]:
    numbers: list[int] = []
    for match in re.finditer(r"ISSUE_CREATED\s+.*?\bissue=(\d+)\b", text or ""):
        numbers.append(int(match.group(1)))
    for match in re.finditer(r"ISSUE_DUPLICATE\s+(?:existing_issue|issue)=(\d+)", text or ""):
        numbers.append(int(match.group(1)))
    return numbers


def _endday_analyzer_issue_numbers(results: Sequence[tuple[list[str], int, str]]) -> list[int]:
    numbers: list[int] = []
    for _command, _rc, output in results:
        for issue_number in _parse_analyzer_issue_numbers(output):
            if issue_number not in numbers:
                numbers.append(issue_number)
    return numbers


def _endday_only_analyzer_routable_issues(results: Sequence[tuple[list[str], int, str]]) -> bool:
    analyzer_numbers = _endday_analyzer_issue_numbers(results)
    if not analyzer_numbers:
        return False
    for command, rc, output in results:
        if rc != 0:
            return False
        if not _output_has_actionable_issue(output):
            continue
        if "scripts/analyze_algo_logs.py" not in command:
            return False
        if "ISSUE_DUPLICATE" not in output and "ISSUE_CREATED" not in output:
            return False
        if "LOG_ANALYZER_DRY_RUN_ISSUE" in output:
            return False
    return True


def _issue_codex_routable(root: Path, issue_number: int) -> bool:
    rc, out = _run(
        ["gh", "issue", "view", str(issue_number), "--json", "number,state,labels"],
        cwd=root,
        timeout=30.0,
    )
    if rc != 0:
        print(f"AUTOOPS_ENDDAY_DUPLICATE_ROUTING issue={issue_number} routable=false reason=issue_view_failed")
        return False
    try:
        payload = json.loads(out or "{}")
    except json.JSONDecodeError:
        print(f"AUTOOPS_ENDDAY_DUPLICATE_ROUTING issue={issue_number} routable=false reason=issue_view_json_failed")
        return False
    state = str(payload.get("state") or "OPEN").strip().upper()
    labels = {
        str(item.get("name") or "").strip()
        for item in payload.get("labels") or []
        if isinstance(item, Mapping) and str(item.get("name") or "").strip()
    }
    required = {"codex", "auto-fix", "environment:live"}
    routable = state == "OPEN" and required.issubset(labels)
    print(
        "AUTOOPS_ENDDAY_DUPLICATE_ROUTING issue=%s routable=%s labels=%s"
        % (issue_number, str(routable).lower(), ",".join(sorted(labels)) or "none")
    )
    return routable


def _extract_issue_fingerprint(text: str) -> str | None:
    match = re.search(r"Fingerprint:\s*(`?)([A-Za-z0-9_.:-]+)\1", text or "")
    return match.group(2) if match else None


def _issue_labels_from_payload(issue: Mapping[str, Any]) -> set[str]:
    return {
        str(item.get("name") or "").strip()
        for item in issue.get("labels") or []
        if isinstance(item, Mapping) and str(item.get("name") or "").strip()
    }


def _dedupe_issues(root: Path, *, environment: str, dry_run: bool = False) -> int:
    print(f"AUTOOPS_DEDUPE_START env={environment} dry_run={str(dry_run).lower()}")
    if environment != "live":
        print("AUTOOPS_DEDUPE_STATUS success=false reason=live_only")
        return 1
    rc, out = _run(
        [
            "gh",
            "issue",
            "list",
            "--state",
            "open",
            "--label",
            "algo-failure",
            "--label",
            "environment:live",
            "--json",
            "number,title,body,labels,createdAt",
            "--limit",
            "200",
        ],
        cwd=root,
        timeout=30.0,
    )
    if rc != 0:
        print(f"AUTOOPS_DEDUPE_STATUS success=false reason=issue_list_failed detail={out!r}")
        return 1
    try:
        payload = json.loads(out or "[]")
    except json.JSONDecodeError:
        print("AUTOOPS_DEDUPE_STATUS success=false reason=issue_list_json_failed")
        return 1
    groups: dict[str, list[Mapping[str, Any]]] = {}
    human_untouched = 0
    for issue in payload if isinstance(payload, list) else []:
        if not isinstance(issue, Mapping):
            continue
        labels = _issue_labels_from_payload(issue)
        if "algo-failure" not in labels or "environment:live" not in labels:
            continue
        fingerprint = _extract_issue_fingerprint(str(issue.get("body") or ""))
        if not fingerprint:
            human_untouched += 1
            continue
        groups.setdefault(fingerprint, []).append(issue)
    closed = 0
    duplicate_groups = 0
    for fingerprint, issues in sorted(groups.items()):
        if len(issues) < 2:
            continue
        duplicate_groups += 1
        ordered = sorted(
            issues,
            key=lambda item: (str(item.get("createdAt") or ""), int(item.get("number") or 0)),
        )
        canonical = ordered[0]
        canonical_number = int(canonical.get("number") or 0)
        for duplicate in ordered[1:]:
            duplicate_number = int(duplicate.get("number") or 0)
            print(
                "AUTOOPS_DEDUPE_DUPLICATE fingerprint=%s canonical=%s duplicate=%s"
                % (fingerprint, canonical_number, duplicate_number)
            )
            if dry_run:
                continue
            comment = f"Duplicate of #{canonical_number}. Closing to reduce noise."
            comment_rc, comment_out = _run(
                ["gh", "issue", "comment", str(duplicate_number), "--body", comment],
                cwd=root,
                timeout=30.0,
            )
            if comment_rc != 0:
                print(f"AUTOOPS_DEDUPE_COMMENT_FAILED issue={duplicate_number} detail={comment_out!r}")
                continue
            close_rc, close_out = _run(
                ["gh", "issue", "close", str(duplicate_number), "--reason", "not planned"],
                cwd=root,
                timeout=30.0,
            )
            if close_rc != 0:
                print(f"AUTOOPS_DEDUPE_CLOSE_FAILED issue={duplicate_number} detail={close_out!r}")
                continue
            closed += 1
    print(
        "AUTOOPS_DEDUPE_STATUS success=true groups=%d closed=%d dry_run=%s human_untouched=%d"
        % (duplicate_groups, closed, str(dry_run).lower(), human_untouched)
    )
    return 0


def _active_codex_locks() -> list[Path]:
    active: list[Path] = []
    for lock in Path("/tmp").glob("algo_codex_issue_*.lock"):
        try:
            pid_text = lock.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if pid_text.isdigit() and Path(f"/proc/{pid_text}").exists():
            active.append(lock)
    return sorted(active)


def _autostart_endday_codex(root: Path, *, issue_numbers: Sequence[int]) -> int:
    issues = ",".join(str(number) for number in issue_numbers) or "unknown"
    active_locks = _active_codex_locks()
    if active_locks:
        print(
            "AUTOOPS_ENDDAY_CODEX_SKIPPED reason=codex_already_running "
            f"issue_count={len(issue_numbers)} issue_numbers={issues} locks={','.join(str(lock) for lock in active_locks)}"
        )
        return 0
    processor = root / "scripts" / "process_codex_issues_local.sh"
    if not processor.exists():
        print(f"AUTOOPS_ENDDAY_CODEX_SKIPPED reason=processor_missing issue_count={len(issue_numbers)} issue_numbers={issues}")
        return 1
    auth_ok, auth_detail = _github_auth_check(root)
    if not auth_ok:
        print(
            "AUTOOPS_ENDDAY_CODEX_SKIPPED reason=github_auth_failed "
            f"issue_count={len(issue_numbers)} issue_numbers={issues} detail={auth_detail!r}"
        )
        print(_github_auth_guidance())
        return 1
    print(f"AUTOOPS_ENDDAY_CODEX_START issue_count={len(issue_numbers)} issue_numbers={issues}")
    started = time.monotonic()
    print(f"AUTOOPS_ENDDAY_CODEX_RUNNING issue_count={len(issue_numbers)} issue_numbers={issues}")
    rc, out = _run([str(processor), "--live"], cwd=root, timeout=3600.0)
    elapsed = time.monotonic() - started
    if out:
        print(out)
    auth_error = _github_auth_output_has_error(out)
    print(
        "AUTOOPS_ENDDAY_CODEX_COMPLETE "
        f"issue_count={len(issue_numbers)} issue_numbers={issues} processor_exit_code={rc} "
        f"auth_error={str(auth_error).lower()} elapsed_seconds={elapsed:.3f}"
    )
    return 1 if auth_error and rc == 0 else rc


def _end_day_analysis(root: Path, *, environment: str, dry_run: bool = False) -> int:
    cfg = _load_autoops_config(root)
    host = socket.gethostname()
    print(f"AUTOOPS_ENDDAY_START env={environment} host={host} dry_run={str(dry_run).lower()}")
    if environment != "live":
        print("AUTOOPS_ENDDAY_STATUS success=false reason=live_only")
        print("AUTOOPS_ENDDAY_CODEX_SKIPPED reason=paper_environment issue_count=0 issue_numbers=none")
        return 1
    if host != LIVE_DEPLOY_HOSTNAME:
        print(f"AUTOOPS_ENDDAY_STATUS success=false reason=wrong_hostname expected={LIVE_DEPLOY_HOSTNAME} actual={host}")
        print("AUTOOPS_ENDDAY_CODEX_SKIPPED reason=wrong_hostname issue_count=0 issue_numbers=none")
        return 1
    if not bool(cfg["live_end_day_analysis_enabled"]):
        print("AUTOOPS_ENDDAY_STATUS success=true reason=disabled")
        print("AUTOOPS_ENDDAY_CODEX_SKIPPED reason=end_day_analysis_disabled issue_count=0 issue_numbers=none")
        return 0

    if dry_run:
        print("AUTOOPS_ENDDAY_DRYRUN_SKIP command=end-day reason=no_dry_run_support")

    results: list[tuple[list[str], int, str]] = []
    for command in _endday_commands(root, dry_run=dry_run):
        print(f"AUTOOPS_ENDDAY_COMMAND command={' '.join(command)}")
        rc, out = _run(command, cwd=root, timeout=900.0)
        results.append((command, rc, out))
        print(f"AUTOOPS_ENDDAY_COMMAND_RESULT rc={rc}")
        if out:
            print(out)

    actionable = any(rc != 0 or _output_has_actionable_issue(out) for _command, rc, out in results)
    if not actionable:
        print("AUTOOPS_ENDDAY_STATUS success=true actionable=false issue_created=false")
        print("AUTOOPS_ENDDAY_CODEX_SKIPPED reason=no_actionable_issues issue_count=0 issue_numbers=none")
        return 0
    analyzer_issue_numbers = _endday_analyzer_issue_numbers(results)
    if _endday_only_analyzer_routable_issues(results):
        analyzer_created_issue = any("ISSUE_CREATED" in output for _command, _rc, output in results)
        analyzer_status_flag = "analyzer_issue=true" if analyzer_created_issue else "duplicate_issue=true"
        routable_numbers = [
            issue_number for issue_number in analyzer_issue_numbers if _issue_codex_routable(root, issue_number)
        ]
        if bool(cfg["live_end_day_codex_autostart_enabled"]) and routable_numbers and not dry_run:
            processor_rc = _autostart_endday_codex(root, issue_numbers=routable_numbers)
            if processor_rc != 0:
                print(f"AUTOOPS_ENDDAY_STATUS success=false actionable=true {analyzer_status_flag} codex_exit_code={processor_rc}")
                return 1
        else:
            issue_not_routable_reason = "analyzer_issue_not_routable" if analyzer_created_issue else "duplicate_issue_not_routable"
            reason = "dry_run" if dry_run else issue_not_routable_reason if not routable_numbers else "codex_autostart_disabled"
            print(
                "AUTOOPS_ENDDAY_CODEX_SKIPPED reason=%s issue_count=%d issue_numbers=%s"
                % (reason, len(analyzer_issue_numbers), ",".join(str(n) for n in analyzer_issue_numbers) or "none")
            )
        print(
            "AUTOOPS_ENDDAY_STATUS success=true actionable=true issue_created=false %s issue_numbers=%s"
            % (analyzer_status_flag, ",".join(str(n) for n in analyzer_issue_numbers) or "none")
        )
        return 0
    severity = _endday_severity(results)
    if dry_run or not bool(cfg["live_end_day_issue_enabled"]):
        print(f"AUTOOPS_ENDDAY_STATUS success=false actionable=true issue_created=false severity={severity}")
        print(
            "AUTOOPS_ENDDAY_CODEX_SKIPPED "
            f"reason={'dry_run' if dry_run else 'issue_creation_disabled'} issue_count=0 issue_numbers=none"
        )
        return 1
    if not bool(cfg["live_end_day_codex_enabled"]):
        print("AUTOOPS_ENDDAY_STATUS success=false reason=codex_issue_disabled")
        print("AUTOOPS_ENDDAY_CODEX_SKIPPED reason=codex_issue_disabled issue_count=0 issue_numbers=none")
        return 1
    rc, out = _create_endday_issue(root, results, severity=severity)
    if rc != 0:
        print(f"AUTOOPS_ENDDAY_STATUS success=false reason=github_issue_failed detail={out!r}")
        print("AUTOOPS_ENDDAY_CODEX_SKIPPED reason=github_issue_failed issue_count=0 issue_numbers=none")
        return 1
    issue_numbers = _parse_issue_numbers(out)
    print(f"AUTOOPS_ENDDAY_ISSUE_CREATED output={out!r} severity={severity} issue_numbers={','.join(str(n) for n in issue_numbers) or 'unknown'}")
    if bool(cfg["live_end_day_codex_autostart_enabled"]):
        processor_rc = _autostart_endday_codex(root, issue_numbers=issue_numbers)
        if processor_rc != 0:
            print(f"AUTOOPS_ENDDAY_STATUS success=false actionable=true issue_created=true codex_exit_code={processor_rc}")
            return 1
    else:
        print(
            "AUTOOPS_ENDDAY_CODEX_SKIPPED reason=codex_autostart_disabled "
            f"issue_count={len(issue_numbers)} issue_numbers={','.join(str(n) for n in issue_numbers) or 'unknown'}"
        )
    print("AUTOOPS_ENDDAY_STATUS success=true actionable=true issue_created=true")
    return 0


def _endday_service_text() -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=AlgoSphere live end-day analysis",
            "",
            "[Service]",
            "Type=oneshot",
            "WorkingDirectory=/opt/algosphere/algo-ai-trading-agent",
            "EnvironmentFile=-%h/.config/algosphere/github.env",
            "ExecStart=/opt/algosphere/algo-ai-trading-agent/bin/algo autoops end-day-analysis --live",
            "",
        ]
    )


def _endday_timer_text() -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=Run AlgoSphere live end-day analysis after market close",
            "",
            "[Timer]",
            "OnCalendar=Mon..Fri *-*-* 17:30:00 America/New_York",
            "Persistent=true",
            "Unit=algosphere-live-endday-analysis.service",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )


def _install_endday_timer(root: Path, *, environment: str, user_systemd_dir: Path | None = None) -> int:
    host = socket.gethostname()
    print(f"AUTOOPS_ENDDAY_TIMER_INSTALL env={environment} host={host}")
    if environment != "live":
        print("AUTOOPS_ENDDAY_TIMER_STATUS success=false reason=live_only")
        return 1
    if host != LIVE_DEPLOY_HOSTNAME:
        print(f"AUTOOPS_ENDDAY_TIMER_STATUS success=false reason=wrong_hostname expected={LIVE_DEPLOY_HOSTNAME} actual={host}")
        return 1
    _available_labels, missing_labels = _ensure_github_labels(REQUIRED_GITHUB_LABELS, root=root)
    if missing_labels:
        print(f"AUTOOPS_ENDDAY_TIMER_LABEL_WARNING missing={','.join(missing_labels)}")
    target = user_systemd_dir or Path.home() / ".config" / "systemd" / "user"
    target.mkdir(parents=True, exist_ok=True)
    service = target / "algosphere-live-endday-analysis.service"
    timer = target / "algosphere-live-endday-analysis.timer"
    service.write_text(_endday_service_text(), encoding="utf-8")
    timer.write_text(_endday_timer_text(), encoding="utf-8")
    for command in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "algosphere-live-endday-analysis.timer"],
        ["systemctl", "--user", "start", "algosphere-live-endday-analysis.timer"],
    ):
        rc, out = _run(command, cwd=root, timeout=30.0)
        print(f"AUTOOPS_ENDDAY_TIMER_COMMAND rc={rc} command={' '.join(command)}")
        if rc != 0:
            print(f"AUTOOPS_ENDDAY_TIMER_STATUS success=false reason=systemctl_failed detail={out!r}")
            return 1
    print("Verification:")
    print("systemctl --user status algosphere-live-endday-analysis.timer")
    print("systemctl --user list-timers algosphere-live-endday-analysis.timer")
    print("journalctl --user -u algosphere-live-endday-analysis.service")
    print("Configured time: 17:30 ET Monday-Friday")
    print("Optional GitHub token EnvironmentFile: ~/.config/algosphere/github.env")
    print("EnvironmentFile setup: mkdir -p ~/.config/algosphere && chmod 700 ~/.config/algosphere && chmod 600 ~/.config/algosphere/github.env")
    print("AUTOOPS_ENDDAY_TIMER_STATUS success=true")
    return 0


def _exists_status(root: Path, rel_path: str) -> str:
    path = root / rel_path
    if not path.exists():
        return "missing"
    if path.is_file() and rel_path.endswith(".sh"):
        return "present_executable" if path.stat().st_mode & 0o111 else "present_not_executable"
    return "present"


def _workflow_status(root: Path) -> list[dict[str, str]]:
    return [
        {"path": rel, "status": _exists_status(root, rel)}
        for rel in REQUIRED_AUTOOPS_PATHS
    ]


def _platform_name() -> str:
    override = os.environ.get("ALGO_AUTOOPS_PLATFORM")
    if override:
        return override.strip() or platform.system()
    return platform.system()


def _paper_service_name(service: str | None) -> str:
    return str(service or os.environ.get("ALGO_PAPER_SERVICE") or "paper.service")


def _launchd_target(label: str) -> str:
    return f"gui/{os.getuid()}/{label}"


def _systemd_service_status(service: str) -> dict[str, str]:
    if not shutil.which("systemctl"):
        return {
            "service_active": "systemctl_unavailable",
            "service_manager": "systemd",
            "service_name": service,
        }
    rc, out = _run(["systemctl", "is-active", service], timeout=3.0)
    text = out.splitlines()[0] if out else ""
    if rc == 0:
        active = text or "active"
    else:
        active = text or f"inactive_rc_{rc}"
    return {
        "service_active": active,
        "service_manager": "systemd",
        "service_name": service,
    }


def _launchd_service_status(label: str) -> dict[str, str]:
    if not shutil.which("launchctl"):
        return {
            "service_active": "launchctl_unavailable",
            "service_manager": "launchd",
            "service_name": label,
        }
    rc, out = _run(["launchctl", "print", _launchd_target(label)], timeout=3.0)
    if rc == 0:
        return {
            "service_active": "active",
            "service_manager": "launchd",
            "service_name": label,
        }
    return {
        "service_active": "inactive",
        "service_manager": "launchd",
        "service_name": label,
        "detail": out or f"launchctl_rc_{rc}",
    }


def _process_fallback_status(pattern: str) -> dict[str, str]:
    if not pattern:
        return {
            "service_active": "inactive",
            "service_manager": "process_fallback",
            "service_name": "unconfigured",
            "detail": "ALGO_PAPER_PROCESS_PATTERN not set",
        }
    if not shutil.which("pgrep"):
        return {
            "service_active": "pgrep_unavailable",
            "service_manager": "process_fallback",
            "service_name": pattern,
        }
    rc, out = _run(["pgrep", "-f", pattern], timeout=3.0)
    if rc == 0 and out:
        return {
            "service_active": "active",
            "service_manager": "process_fallback",
            "service_name": pattern,
        }
    return {
        "service_active": "inactive",
        "service_manager": "process_fallback",
        "service_name": pattern,
    }


def _service_status(*, environment: str, service: str | None = None) -> dict[str, str]:
    env = str(environment or "paper").strip().lower()
    system = _platform_name()
    if system == "Darwin" and env == "paper":
        label = str(os.environ.get("ALGO_PAPER_LAUNCHD_LABEL") or "").strip()
        if label:
            launchd = _launchd_service_status(label)
            if launchd["service_active"] == "active":
                return launchd
        else:
            launchd = {
                "service_active": "inactive",
                "service_manager": "launchd",
                "service_name": "unconfigured",
                "detail": "ALGO_PAPER_LAUNCHD_LABEL not set",
            }
        pattern = str(os.environ.get("ALGO_PAPER_PROCESS_PATTERN") or "algo_loop.py --paper").strip()
        fallback = _process_fallback_status(pattern)
        if fallback["service_active"] == "active":
            return fallback
        if label:
            return launchd
        return fallback
    if env == "paper":
        return _systemd_service_status(_paper_service_name(service))
    return _systemd_service_status(str(service or "algo.service"))


def _gh_version_status() -> str:
    if not shutil.which("gh"):
        return "gh_unavailable"
    rc, out = _run(["gh", "--version"], timeout=3.0)
    first = out.splitlines()[0] if out else ""
    return first if rc == 0 and first else f"gh_error_rc_{rc}"


def _gh_authenticated() -> tuple[bool, str]:
    return _github_auth_check(PROJECT_ROOT)


def _gh_json(args: Sequence[str]) -> Any | None:
    if not shutil.which("gh"):
        return None
    rc, out = _run(["gh", *args], timeout=8.0)
    if rc != 0 or not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _github_label_names() -> set[str] | None:
    payload = _gh_json(["label", "list", "--json", "name", "--limit", "300"])
    if not isinstance(payload, list):
        return None
    return {
        str(item.get("name") or "")
        for item in payload
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }


def _ensure_github_labels(labels: Sequence[str], *, root: Path = PROJECT_ROOT) -> tuple[set[str], list[str]]:
    existing = _github_label_names()
    if existing is None:
        return set(), list(labels)
    available = set(existing)
    failed: list[str] = []
    for label in labels:
        if label in available:
            continue
        color, description = GITHUB_LABEL_METADATA.get(label, ("5319e7", f"AlgoSphere automation label: {label}"))
        rc, out = _run(
            [
                "gh",
                "label",
                "create",
                label,
                "--color",
                color,
                "--description",
                description,
            ],
            cwd=root,
            timeout=30.0,
        )
        if rc == 0 or "already exists" in str(out or "").lower():
            available.add(label)
            print(f"AUTOOPS_GITHUB_LABEL_READY name={label}")
            continue
        failed.append(label)
        print(f"AUTOOPS_GITHUB_LABEL_CREATE_FAILED name={label} rc={rc} detail={out!r}")
    return available, failed


def _latest_autoops_issue() -> dict[str, Any] | None:
    payload = _gh_json(
        [
            "issue",
            "list",
            "--state",
            "open",
            "--search",
            "AutoOps OR AUTOOPS OR self-heal",
            "--json",
            "number,title,body,labels,url",
            "--limit",
            "1",
        ]
    )
    if isinstance(payload, list) and payload:
        return payload[0] if isinstance(payload[0], dict) else None
    return None


def _autoops_prs() -> list[dict[str, Any]]:
    payload = _gh_json(
        [
            "pr",
            "list",
            "--state",
            "all",
            "--search",
            "codex-validation-passed OR codex-validation-failed",
            "--json",
            "number,title,body,labels,url,headRefName,state,updatedAt,closedAt,mergedAt,closingIssuesReferences",
            "--limit",
            "20",
        ]
    )
    if not isinstance(payload, list):
        return []
    return [dict(item) for item in payload if isinstance(item, dict)]


def _latest_autoops_pr() -> dict[str, Any] | None:
    rows = _autoops_prs()
    return rows[0] if rows else None


def _label_names(row: dict[str, Any] | None) -> list[str]:
    labels = row.get("labels") if isinstance(row, dict) else []
    out: list[str] = []
    for item in labels or []:
        if isinstance(item, dict) and item.get("name"):
            out.append(str(item["name"]))
    return sorted(out)


def _text_environment(text: str) -> str:
    low = str(text or "").lower()
    if re.search(r"(\[live\]|\blive\b|environment:live|processor:fedora-live|processor:live-linux)", low):
        return "live"
    if re.search(r"(\[paper\]|\bpaper\b|environment:paper|processor:mac-paper)", low):
        return "paper"
    return "unknown"


def _issue_environment(row: Mapping[str, Any] | None) -> str:
    if not isinstance(row, Mapping):
        return "unknown"
    labels = {label.lower() for label in _label_names(dict(row) if isinstance(row, dict) else None)}
    if labels.intersection({"live", "environment:live", "processor:fedora-live", "processor:live-linux"}):
        return "live"
    if labels.intersection({"paper", "environment:paper", "processor:mac-paper"}):
        return "paper"
    for key in ("title", "body"):
        env = _text_environment(str(row.get(key) or ""))
        if env != "unknown":
            return env
    return "unknown"


def _pr_environment(row: Mapping[str, Any] | None, linked_issue: Mapping[str, Any] | None = None) -> str:
    labels = {label.lower() for label in _label_names(dict(row) if isinstance(row, dict) else None)}
    if labels.intersection({"live", "environment:live", "processor:fedora-live", "processor:live-linux"}):
        return "live"
    if labels.intersection({"paper", "environment:paper", "processor:mac-paper"}):
        return "paper"
    if isinstance(row, Mapping):
        for key in ("title", "body", "headRefName"):
            env = _text_environment(str(row.get(key) or ""))
            if env != "unknown":
                return env
        linked = row.get("closingIssuesReferences")
        if isinstance(linked, list):
            for issue in linked:
                if not isinstance(issue, Mapping):
                    continue
                env = _issue_environment(issue)
                if env != "unknown":
                    return env
    issue_env = _issue_environment(linked_issue)
    if issue_env != "unknown":
        return issue_env
    return "unknown"


def _pr_is_open(row: Mapping[str, Any] | None) -> bool:
    if not isinstance(row, Mapping):
        return False
    state = str(row.get("state") or "").strip().lower()
    if state:
        return state == "open"
    return not bool(row.get("closedAt") or row.get("mergedAt"))


def _pr_validation_status(row: Mapping[str, Any] | None) -> str:
    labels = _label_names(dict(row) if isinstance(row, dict) else None)
    if "codex-validation-failed" in labels:
        return "failed"
    if "codex-validation-passed" in labels:
        return "passed"
    if row is None:
        return "unavailable"
    return "missing"


def _pr_validation_detail(row: Mapping[str, Any], linked_issue: Mapping[str, Any] | None = None) -> str:
    number = row.get("number")
    env = _pr_environment(row, linked_issue)
    state = "open" if _pr_is_open(row) else "closed"
    prefix = f"#{number}" if number is not None else "#unknown"
    return f"{prefix} {env}/{state}"


def _latest_validation_status_for_environment(
    prs: Sequence[Mapping[str, Any]],
    *,
    environment: str,
    linked_issue: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    env = str(environment or "paper").strip().lower()
    open_current = [row for row in prs if _pr_is_open(row)]
    ignored_environment = [
        row
        for row in open_current
        if _pr_validation_status(row) == "failed"
        and _pr_environment(row, linked_issue) not in {env, "unknown"}
    ]
    open_relevant: list[Mapping[str, Any]] = [
        row for row in open_current if _pr_environment(row, linked_issue) in {env, "unknown"}
    ]
    for row in open_relevant:
        status = _pr_validation_status(row)
        if status == "failed":
            return "failed", _pr_validation_detail(row, linked_issue)
    for row in open_relevant:
        status = _pr_validation_status(row)
        if status == "passed":
            return "passed", _pr_validation_detail(row, linked_issue)
    stale_failed = [
        row for row in prs if _pr_validation_status(row) == "failed" and not _pr_is_open(row)
    ]
    if ignored_environment:
        row = ignored_environment[0]
        return "ignored_environment", f"{_pr_validation_detail(row, linked_issue)} while verifying {env}"
    if stale_failed:
        return "ignored_stale", _pr_validation_detail(stale_failed[0], linked_issue)
    return "not_applicable", ""


def _required_github_labels_present() -> tuple[bool, list[str]]:
    names = _github_label_names()
    if names is None:
        return False, list(REQUIRED_GITHUB_LABELS)
    missing = [label for label in REQUIRED_GITHUB_LABELS if label not in names]
    return not missing, missing


def _autoops_history_dir(root: Path) -> Path:
    return root / "data" / "autoops" / "history"


def _utc_timestamp() -> datetime:
    return datetime.now(timezone.utc)


def _history_filename(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H%M%S.json")


def _write_drill_history(root: Path, record: dict[str, Any]) -> Path:
    history_dir = _autoops_history_dir(root)
    history_dir.mkdir(parents=True, exist_ok=True)
    ts_text = str(record.get("timestamp") or "")
    try:
        ts = datetime.fromisoformat(ts_text.replace("Z", "+00:00"))
    except ValueError:
        ts = _utc_timestamp()
    path = history_dir / _history_filename(ts)
    if path.exists():
        stem = path.stem
        suffix = path.suffix
        idx = 1
        while (history_dir / f"{stem}_{idx}{suffix}").exists():
            idx += 1
        path = history_dir / f"{stem}_{idx}{suffix}"
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _base_drill_record(
    *,
    environment: str,
    drill_mode: str,
    started_at: datetime,
) -> dict[str, Any]:
    return {
        "timestamp": started_at.isoformat().replace("+00:00", "Z"),
        "host": socket.gethostname(),
        "environment": environment,
        "drill_mode": drill_mode,
        "duration_seconds": 0.0,
        "issue_created": False,
        "pr_created": False,
        "validation_passed": False,
        "merged": False,
        "deployed": False,
        "verified": False,
        "success": False,
        "failure_reason": "",
        "failure_class": "",
        "failure_type": "",
        "diagnosis": "",
        "recovery_plan": "",
        "recovery_path": [],
        "improved": False,
        "issue_number": None,
        "pr_number": None,
    }


def _failure_class_for(failure_type: str | None) -> str:
    if str(failure_type or "").strip() == "health_failed":
        return "health_failed"
    return ""


def _recovery_path_for(failure_type: str | None) -> list[str]:
    if str(failure_type or "").strip() == "health_failed":
        return [
            "check_algo_health_unhealthy",
            "failure_reporter_issue_created",
            "codex_processor_started",
            "validation_ran",
            "recovery_recorded",
        ]
    return []


def _load_history_records(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    history_dir = _autoops_history_dir(root)
    if not history_dir.exists():
        return rows
    for path in sorted(history_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            row = dict(payload)
            row["_path"] = str(path)
            rows.append(row)
    return rows


def _safe_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _safe_float(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if out == out else 0.0


def _is_blocked_history_row(row: Mapping[str, Any]) -> bool:
    if _safe_bool(row.get("blocked")):
        return True
    reason = str(row.get("failure_reason") or "").strip()
    return reason == "non_dry_run_requires_confirm_and_paper_only"


def _latest_history_row(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    if not rows:
        return None
    return sorted(rows, key=lambda item: str(item.get("timestamp") or ""))[-1]


def _history_result(row: Mapping[str, Any] | None) -> str:
    if row is None:
        return "unavailable"
    if _safe_bool(row.get("success")):
        return "success"
    if _is_blocked_history_row(row):
        return "blocked"
    return "failed"


def _workflow_present(root: Path, rel_path: str) -> bool:
    return (root / rel_path).exists()


def _options_pilot_enabled(root: Path) -> bool:
    try:
        from src.options_pilot_status import build_options_pilot_status
    except Exception:
        return False
    return bool(build_options_pilot_status(root=root, env_name="live").live_pilot_enabled)


def _options_readiness(root: Path, *, environment: str):
    from src.options_readiness import build_options_readiness, load_effective_runtime_config

    env = str(environment or "paper").strip().lower()
    user_id = "live_bot" if env == "live" else "paper_bot"
    config = load_effective_runtime_config(root, environment=env, user_id=user_id)
    return build_options_readiness(config, environment=env, user_id=user_id, root=root)


def _effective_runtime_config(root: Path, *, environment: str) -> Mapping[str, Any]:
    from src.options_readiness import load_effective_runtime_config

    env = str(environment or "paper").strip().lower()
    user_id = "live_bot" if env == "live" else "paper_bot"
    return load_effective_runtime_config(root, environment=env, user_id=user_id)


def _effective_trading_mode(root: Path, *, environment: str) -> str:
    try:
        config = _effective_runtime_config(root, environment=environment)
        trading_control = config.get("trading_control") if isinstance(config, Mapping) else {}
        if isinstance(trading_control, Mapping):
            mode = str(trading_control.get("mode") or "").strip().lower()
            if mode:
                return mode
    except Exception:
        return "unknown"
    return "unknown"


def _runtime_profile(root: Path, *, environment: str) -> tuple[str, tuple[str, ...]]:
    """Classify runtime activation without changing configuration or contacting brokers."""
    try:
        config = _effective_runtime_config(root, environment=environment)
    except Exception:
        return "unknown", ("runtime_config_unavailable",)
    trading_control = config.get("trading_control") if isinstance(config, Mapping) else {}
    if not isinstance(trading_control, Mapping):
        return "unknown", ("trading_control_missing",)
    mode = str(trading_control.get("mode") or "").strip().lower()
    if mode != "live":
        return mode or "unknown", ()
    try:
        from src.controlled_live_equity import controlled_live_limit_blockers, runtime_profile as _profile

        configured_profile = _profile(config)
    except Exception:
        configured_profile = str(trading_control.get("runtime_profile") or "").strip().lower()
    reasons: list[str] = []
    states_raw = trading_control.get("strategy_states") if isinstance(trading_control.get("strategy_states"), Mapping) else {}
    states = {str(k): str(v).strip().upper() for k, v in states_raw.items()}
    live_strategies = sorted(name for name, state in states.items() if state == "LIVE")
    if live_strategies != ["trend_long"]:
        reasons.append("live_strategy_set_not_trend_long_only")
    if states.get("options_live") != "DISABLED" or states.get("options_paper") != "DISABLED":
        reasons.append("options_strategy_state_not_disabled")
    options_cfg = config.get("options") if isinstance(config.get("options"), Mapping) else {}
    nested_options_pilot = (
        options_cfg.get("live_pilot")
        if isinstance(options_cfg.get("live_pilot"), Mapping)
        else {}
    )
    if bool(options_cfg.get("enabled")) or bool(options_cfg.get("live_pilot_enabled")) or bool((nested_options_pilot or {}).get("enabled")):
        reasons.append("options_active")
    if configured_profile == "controlled_live_equity":
        try:
            reasons.extend(controlled_live_limit_blockers(config))
        except Exception:
            reasons.append("controlled_live_caps_unavailable")
        return "controlled_live_equity", tuple(reasons)
    pilot = trading_control.get("live_pilot") if isinstance(trading_control.get("live_pilot"), Mapping) else {}
    if not bool((pilot or {}).get("enabled", False)):
        return "unrestricted_live", ()

    allowed = [str(item) for item in (pilot or {}).get("allowed_strategies", [])]
    if allowed != ["trend_long"]:
        reasons.append("live_pilot_allowed_strategies_invalid")
    numeric_caps = {
        "max_trades_per_day": 1,
        "max_entry_submissions_per_day": 1,
        "max_entry_fills_per_day": 1,
        "max_open_positions": 1,
        "max_notional_per_trade": 100,
        "max_total_deployed_notional": 100,
        "max_daily_loss_usd": 25,
    }
    for key, expected in numeric_caps.items():
        try:
            actual = float((pilot or {}).get(key))
        except (TypeError, ValueError):
            reasons.append(f"live_pilot_{key}_invalid")
            continue
        if actual != float(expected):
            reasons.append(f"live_pilot_{key}_invalid")
    false_flags = ("allow_short_selling", "allow_add_to_existing", "allow_replacements", "allow_reallocation", "allow_overnight")
    for key in false_flags:
        if bool((pilot or {}).get(key, True)):
            reasons.append(f"live_pilot_{key}_invalid")
    if not bool((pilot or {}).get("eod_flatten_required", False)):
        reasons.append("live_pilot_eod_flatten_required_invalid")
    return "bounded_live_pilot", tuple(reasons)


def _options_gate_applies(*, env_name: str, options_ready: Any) -> bool:
    if env_name != "live":
        return False
    return bool(
        getattr(options_ready, "config_enabled", False)
        or getattr(options_ready, "live_pilot_enabled", False)
        or getattr(options_ready, "route_active", False)
    )


def _market_hours_now() -> bool:
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= minutes <= (16 * 60)


def _service_active_or_expected_inactive(status: Mapping[str, str]) -> tuple[bool, str]:
    active = str(status.get("service_active") or "unknown")
    if active == "active":
        return True, "active"
    if not _market_hours_now():
        return True, "expected_inactive_outside_market_hours"
    return False, active


def _systemd_unit_state(unit: str) -> str:
    if not shutil.which("systemctl"):
        return "systemctl_unavailable"
    rc, out = _run(["systemctl", "is-active", unit], timeout=3.0)
    first = out.splitlines()[0] if out else ""
    if rc == 0:
        return first or "active"
    return first or f"inactive_rc_{rc}"


def _latest_intraday_health_json(root: Path, *, environment: str) -> tuple[str, str]:
    health_dir = root / "data" / "intraday_health"
    if not health_dir.exists():
        return "unavailable", "missing"
    paths = sorted(health_dir.glob(f"*/{environment}_intraday_health.json"))
    if not paths:
        return "unavailable", "missing"
    path = paths[-1]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unavailable", str(path)
    status = str(payload.get("status") or "unknown").strip().lower() or "unknown"
    return status, str(path)


def _parse_self_heal_status(text: str, rc: int) -> str:
    for line in str(text or "").splitlines():
        if "SELF_HEAL status=healthy" in line or "SELF_HEAL status=recovered" in line:
            return "healthy"
        if "SELF_HEAL status=degraded" in line:
            return "degraded"
        if "SELF_HEAL status=blocked" in line or "SELF_HEAL status=codex_running" in line:
            return "blocked"
        if "SELF_HEAL status=failure_detected" in line or "SELF_HEAL status=verification_failed" in line:
            return "failure"
    return "failure" if rc != 0 else "unknown"


def _run_self_heal_dry_run(root: Path, *, environment: str) -> tuple[str, str]:
    env = str(environment or "paper").strip().lower()
    flag = "--live" if env == "live" else "--paper"
    rc, out = _run([str(root / "bin" / "algo"), "self-heal", flag, "--dry-run"], cwd=root, timeout=60.0)
    return _parse_self_heal_status(out, rc), out


def _self_heal_verify_detail(output: str) -> str:
    for line in str(output or "").splitlines():
        if "SELF_HEAL status=degraded" in line or "SELF_HEAL_LOG_SOURCE" in line:
            return line.strip()
    return ""


def _readiness_recommendation(
    *,
    service_active: str,
    service_ok: bool,
    self_heal_status: str,
    github_authenticated: bool,
    labels_present: bool,
    validation_status: str,
    workflows_present: bool,
    latest_drill_result: str,
    intraday_health_timer_active: bool = True,
    required_paths_ready: bool = True,
) -> tuple[str, str]:
    if not service_ok and service_active != "active":
        return "blocked", "service_not_active"
    if not intraday_health_timer_active:
        return "blocked", "intraday_health_timer_disabled"
    if not required_paths_ready:
        return "blocked", "required_autoops_path_not_ready"
    if self_heal_status not in {"healthy", "unknown", "degraded"}:
        return "blocked", f"self_heal_{self_heal_status}"
    if not github_authenticated:
        return "blocked", "github_auth_unavailable"
    if not labels_present:
        return "blocked", "missing_github_labels"
    if not workflows_present:
        return "blocked", "missing_workflow"
    if validation_status == "failed":
        return "blocked", "latest_pr_validation_failed"
    if latest_drill_result == "failed":
        return "needs review", "latest_drill_failed"
    return "ready", "all_checks_passed"


def _print_status(
    root: Path,
    *,
    environment: str,
    service: str | None,
    full: bool = False,
    explicit_environment: bool = False,
) -> int:
    status = _service_status(environment=environment, service=service)
    history_rows = _load_history_records(root)
    health_failed_count = sum(
        1
        for row in history_rows
        if str(row.get("failure_class") or row.get("failure_type") or "").strip() == "health_failed"
    )
    print("AUTOOPS_HEALTH_CHECK status=running mode=status read_only=true")
    print("AutoOps status")
    print(f"- subsystem: AutoOps")
    print(f"- platform: Algo")
    print(f"- environment: {environment}")
    print(f"- repo: {root}")
    print(f"- health_reporter: {_exists_status(root, 'scripts/check_algo_health.sh')}")
    print(f"- failure_reporter: {_exists_status(root, 'scripts/report_algo_failure_to_github.sh')}")
    print(f"- service_manager: {status.get('service_manager', 'unknown')}")
    print(f"- service_name: {status.get('service_name', 'unknown')}")
    print(f"- service_active: {status.get('service_active', 'unknown')}")
    if status.get("detail"):
        print(f"- service_detail: {status.get('detail')}")
    print("- failure_classes: health_failed")
    print(f"- health_failed_recoveries: {health_failed_count}")
    print(f"- github_cli: {_gh_version_status()}")
    print("- components:")
    for row in _workflow_status(root):
        print(f"  - {row['path']}: {row['status']}")

    issue = _latest_autoops_issue()
    prs = _autoops_prs()
    pr = prs[0] if prs else None
    if issue:
        print(
            "- latest_autoops_issue: "
            f"#{issue.get('number')} {issue.get('title')} labels={','.join(_label_names(issue)) or 'none'}"
        )
    else:
        print("- latest_autoops_issue: unavailable")
    if pr:
        print(
            "- latest_autoops_pr: "
            f"#{pr.get('number')} {pr.get('title')} labels={','.join(_label_names(pr)) or 'none'}"
        )
        validation_labels = [
            label for label in _label_names(pr)
            if label in {"codex-validation-passed", "codex-validation-failed"}
        ]
        print(f"- validation_labels: {','.join(validation_labels) or 'none'}")
    else:
        print("- latest_autoops_pr: unavailable")
        print("- validation_labels: unavailable")
    if full:
        if not explicit_environment and str(environment).strip().lower() == "paper":
            print('AUTOOPS_STATUS_HINT next="./bin/algo autoops status --full --live"')
        self_heal_status, _self_heal_output = _run_self_heal_dry_run(root, environment=environment)
        gh_authed, gh_auth_detail = _gh_authenticated()
        labels_present, missing_labels = _required_github_labels_present()
        validation_status, validation_detail = _latest_validation_status_for_environment(
            prs,
            environment=environment,
            linked_issue=issue,
        )
        validation_workflow = _workflow_present(root, ".github/workflows/codex-pr-validation.yml")
        auto_merge_workflow = _workflow_present(root, ".github/workflows/codex-auto-merge.yml")
        latest_drill = _latest_history_row(history_rows)
        latest_drill_result = _history_result(latest_drill)
        workflows_present = bool(validation_workflow and auto_merge_workflow)
        service_ok, _service_reason = _service_active_or_expected_inactive(status)
        recommendation, reason = _readiness_recommendation(
            service_active=str(status.get("service_active", "unknown")),
            service_ok=service_ok,
            self_heal_status=self_heal_status,
            github_authenticated=gh_authed,
            labels_present=labels_present,
            validation_status=validation_status,
            workflows_present=workflows_present,
            latest_drill_result=latest_drill_result,
        )
        print("AutoOps full status")
        print(f"- algo service: {status.get('service_active', 'unknown')}")
        print(f"- self-heal latest status: {self_heal_status}")
        print(f"- GitHub CLI authenticated: {'yes' if gh_authed else 'no'}")
        if not gh_authed:
            print(f"- GitHub auth detail: {gh_auth_detail}")
        print(f"- required GitHub labels present: {'yes' if labels_present else 'no'}")
        if missing_labels:
            print(f"- missing GitHub labels: {','.join(missing_labels)}")
        issue_state = "open" if issue else "unavailable"
        print(f"- latest AutoOps issue number/status: #{issue.get('number')} {issue_state}" if issue else "- latest AutoOps issue number/status: unavailable")
        if validation_status in {"ignored_environment", "ignored_stale"} and validation_detail:
            detail_parts = validation_detail.split()
            env_state = detail_parts[1] if len(detail_parts) > 1 else "unknown/unknown"
            pr_env = env_state.split("/", 1)[0]
            pr_state = f"failed historical/{pr_env}"
        else:
            pr_state = validation_status if pr else "unavailable"
        print(f"- latest Codex PR number/status: #{pr.get('number')} {pr_state}" if pr else "- latest Codex PR number/status: unavailable")
        if validation_status in {"ignored_stale", "ignored_environment"} and validation_detail:
            print(f"- stale failed Codex PR: {validation_detail}")
        print(f"- Codex PR validation workflow present: {'yes' if validation_workflow else 'no'}")
        print(f"- guarded auto-merge workflow present: {'yes' if auto_merge_workflow else 'no'}")
        print(f"- latest drill result: {latest_drill_result}")
        print(f"- blocked safety drills counted separately from failures: yes")
        print(f"- options pilot enabled: {'yes' if _options_pilot_enabled(root) else 'no'}")
        print(f"- recommendation: {recommendation}")
        print(f"- recommendation_reason: {reason}")
    return 0


def _drill_dry_run(root: Path, *, environment: str, failure_type: str | None = None) -> int:
    started_at = _utc_timestamp()
    start_monotonic = time.monotonic()
    record = _base_drill_record(
        environment=environment,
        drill_mode="dry_run",
        started_at=started_at,
    )
    env_norm = str(environment or "paper").strip().lower()
    issue_labels = [
        "live" if env_norm == "live" else "paper",
        "codex",
        "auto-fix",
        f"environment:{env_norm}",
        "processor:live-linux" if env_norm == "live" else "processor:mac-paper",
    ]
    print("AUTOOPS_DRILL_START dry_run=true")
    missing = [rel for rel in REQUIRED_AUTOOPS_PATHS if not (root / rel).exists()]
    for rel in REQUIRED_AUTOOPS_PATHS:
        print(f"AUTOOPS_DRILL_CHECK path={rel} status={_exists_status(root, rel)}")
    print(f"AUTOOPS_DRILL_CHECK github_cli={_gh_version_status()}")
    if missing:
        reason = f"missing_required_paths:{','.join(missing)}"
        record["duration_seconds"] = round(time.monotonic() - start_monotonic, 6)
        record["failure_reason"] = reason
        path = _write_drill_history(root, record)
        print(f"AUTOOPS_DRILL_HISTORY path={path}")
        print(f"AUTOOPS_DRILL_FAILED reason=missing_required_paths paths={','.join(missing)}")
        return 1

    if failure_type:
        diagnosis, recovery_plan = AUTOOPS_FAILURE_SCENARIOS[failure_type]
        print(f"AUTOOPS_FAILURE_INJECTED type={failure_type}")
        if _failure_class_for(failure_type):
            print(f"AUTOOPS_FAILURE_CLASS class={_failure_class_for(failure_type)}")
        print(f"AUTOOPS_DIAGNOSED type={failure_type} diagnosis={diagnosis}")
        print(f"AUTOOPS_RECOVERY_PLAN type={failure_type} plan={recovery_plan}")
        if failure_type == "health_failed":
            print("AUTOOPS_HEALTH_CHECK dry_run=true result=unhealthy failure_class=health_failed")
            print("AUTOOPS_FAILURE_REPORTER dry_run=true result=issue_created failure_class=health_failed")
        record.update(
            {
                "failure_class": _failure_class_for(failure_type),
                "failure_type": failure_type,
                "diagnosis": diagnosis,
                "recovery_plan": recovery_plan,
                "recovery_path": _recovery_path_for(failure_type),
                "improved": True,
            }
        )

    if failure_type != "health_failed":
        print("AUTOOPS_HEALTH_CHECK dry_run=true result=simulated")
    print("AUTOOPS_HEALTH_FAILURE_DETECTED dry_run=true simulated=true")
    print("AUTOOPS_ISSUE_PAYLOAD dry_run=true generated=true labels=%s" % ",".join(issue_labels))
    print("AUTOOPS_CODEX_PROCESSOR dry_run=true would_accept_issue=true")
    print("AUTOOPS_PR_VALIDATION dry_run=true required=true")
    print("AUTOOPS_RESTART_GATED dry_run=true executed=false command=systemctl_restart_algo_service")
    print("AUTOOPS_ISSUE_CREATED dry_run=true skipped_github_write=true")
    print("AUTOOPS_CODEX_STARTED dry_run=true skipped_codex_exec=true")
    print("AUTOOPS_PR_CREATED dry_run=true skipped_github_write=true")
    print("AUTOOPS_VALIDATION_PASSED dry_run=true simulated=true")
    print("AUTOOPS_AUTO_MERGED dry_run=true skipped_github_write=true")
    print("AUTOOPS_DEPLOY_STARTED dry_run=true skipped_deploy=true")
    print("AUTOOPS_DEPLOYED dry_run=true skipped_deploy=true")
    print("AUTOOPS_VERIFY_STARTED dry_run=true simulated=true")
    print("AUTOOPS_VERIFIED dry_run=true simulated=true")
    print("AUTOOPS_RECOVERY_COMPLETE dry_run=true simulated=true")
    record.update(
        {
            "validation_passed": True,
            "verified": True,
            "success": True,
            "duration_seconds": round(time.monotonic() - start_monotonic, 6),
        }
    )
    path = _write_drill_history(root, record)
    print(f"AUTOOPS_DRILL_HISTORY path={path}")
    print("AUTOOPS_DRILL_SUCCESS dry_run=true")
    return 0


def _print_report(root: Path) -> int:
    rows = _load_history_records(root)
    total = len(rows)
    success_rows = [row for row in rows if _safe_bool(row.get("success"))]
    blocked_rows = [row for row in rows if not _safe_bool(row.get("success")) and _is_blocked_history_row(row)]
    failed_rows = [
        row for row in rows
        if not _safe_bool(row.get("success")) and not _is_blocked_history_row(row)
    ]
    success_pct = (len(success_rows) / total * 100.0) if total else 0.0
    avg_recovery = (
        sum(_safe_float(row.get("duration_seconds")) for row in success_rows) / len(success_rows)
        if success_rows
        else 0.0
    )

    def _latest(rows_in: list[dict[str, Any]]) -> str:
        if not rows_in:
            return "none"
        row = sorted(rows_in, key=lambda item: str(item.get("timestamp") or ""))[-1]
        reason = str(row.get("failure_reason") or "")
        failure_class = str(row.get("failure_class") or "")
        failure_type = str(row.get("failure_type") or "")
        context = ""
        if failure_class:
            context = f" class={failure_class}"
        elif failure_type:
            context = f" type={failure_type}"
        if reason:
            return f"{row.get('timestamp')}{context} reason={reason}"
        return f"{row.get('timestamp') or 'unknown'}{context}"

    failure_class_counts: dict[str, int] = {}
    for row in rows:
        failure_class = str(row.get("failure_class") or "").strip()
        if failure_class:
            failure_class_counts[failure_class] = failure_class_counts.get(failure_class, 0) + 1

    print("AutoOps report")
    print(f"- total drills: {total}")
    print(f"- successful drills: {len(success_rows)}")
    print(f"- blocked drills: {len(blocked_rows)}")
    print(f"- failed drills: {len(failed_rows)}")
    print(f"- success %: {success_pct:.1f}")
    print(f"- avg recovery time: {avg_recovery:.3f}s")
    print(f"- last successful drill: {_latest(success_rows)}")
    print(f"- last failed drill: {_latest(failed_rows)}")
    print("- failure classes:")
    if failure_class_counts:
        for key in sorted(failure_class_counts):
            print(f"  - {key}: {failure_class_counts[key]}")
    else:
        print("  - none: 0")
    return 0


def _block_drill(
    root: Path,
    record: dict[str, Any],
    *,
    start_monotonic: float,
    reason: str,
    next_command: str,
) -> int:
    record["duration_seconds"] = round(time.monotonic() - start_monotonic, 6)
    record["failure_reason"] = reason
    record["blocked"] = True
    path = _write_drill_history(root, record)
    print(f"AUTOOPS_DRILL_HISTORY path={path}", file=sys.stderr)
    print(f'AUTOOPS_DRILL_BLOCKED reason={reason} next="{next_command}"', file=sys.stderr)
    return 0


def _parse_number_from_url(text: str) -> int | None:
    tail = str(text or "").strip().rstrip("/").rsplit("/", 1)[-1]
    try:
        return int(tail)
    except (TypeError, ValueError):
        return None


def _fail_confirmed_drill(
    root: Path,
    record: dict[str, Any],
    *,
    start_monotonic: float,
    reason: str,
) -> int:
    record["duration_seconds"] = round(time.monotonic() - start_monotonic, 6)
    record["failure_reason"] = reason
    path = _write_drill_history(root, record)
    print(f"AUTOOPS_DRILL_HISTORY path={path}", file=sys.stderr)
    print(f"AUTOOPS_DRILL_FAILED reason={reason}", file=sys.stderr)
    return 2


def _fail_confirmed_drill_with_gh_output(
    root: Path,
    record: dict[str, Any],
    *,
    start_monotonic: float,
    reason: str,
    rc: int,
    output: str,
) -> int:
    output_clean = str(output or "").strip()
    record["github_issue_create_rc"] = int(rc)
    record["github_issue_create_output"] = output_clean
    detail = f"{reason} output={output_clean[:500]}" if output_clean else reason
    print(f"AUTOOPS_GITHUB_ISSUE_CREATE_FAILED rc={rc} output={output_clean[:500]}", file=sys.stderr)
    return _fail_confirmed_drill(root, record, start_monotonic=start_monotonic, reason=detail)


def _create_drill_issue(
    root: Path,
    *,
    failure_type: str,
    body_file: Path,
) -> tuple[int, str, list[str]]:
    required_labels = ["environment:paper", "processor:mac-paper", "codex", "auto-fix"]
    labels = ["autoops-drill", *required_labels]
    args = [
        "gh",
        "issue",
        "create",
        "--title",
        f"[PAPER] AutoOps drill: {failure_type}",
        "--body-file",
        str(body_file),
    ]
    for label in labels:
        args.extend(["--label", label])
    rc, out = _run(args, cwd=root, timeout=30.0)
    if rc == 0:
        return rc, out, labels
    if "autoops-drill" not in str(out):
        return rc, out, labels
    labels = required_labels
    args = [
        "gh",
        "issue",
        "create",
        "--title",
        f"[PAPER] AutoOps drill: {failure_type}",
        "--body-file",
        str(body_file),
    ]
    for label in labels:
        args.extend(["--label", label])
    retry_rc, retry_out = _run(args, cwd=root, timeout=30.0)
    return retry_rc, retry_out, labels


def _confirmed_paper_drill_allowed(environment: str) -> tuple[bool, str]:
    if str(environment or "").strip().lower() != "paper":
        return False, "paper_only_drill_refuses_live_environment"
    system = _platform_name()
    if system != "Darwin":
        return False, "paper_only_drill_requires_mac"
    return True, "paper_mac_confirmed"


def _write_synthetic_issue_body(
    root: Path,
    *,
    failure_type: str,
    diagnosis: str,
    recovery_plan: str,
) -> Path:
    path = root / "data" / "autoops" / "autoops_drill_issue.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "# AutoOps Drill Synthetic Failure",
                "",
                "This is a controlled paper-only AutoOps drill for Algo.",
                "",
                f"- failure_type: {failure_type}",
                f"- failure_class: {_failure_class_for(failure_type) or 'n/a'}",
                f"- diagnosis: {diagnosis}",
                f"- recovery_plan: {recovery_plan}",
                f"- recovery_path: {','.join(_recovery_path_for(failure_type)) or 'n/a'}",
                "",
                "Scope:",
                "- Do not change trading thresholds.",
                "- Do not call broker APIs.",
                "- Do not deploy or restart live services.",
                "- Make only a harmless AutoOps documentation or test-safe change if a code change is needed.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _gh_label_names(row: Mapping[str, Any] | None) -> set[str]:
    if not isinstance(row, dict):
        return set()
    return set(_label_names(row))


def _run_paper_verification(root: Path) -> tuple[bool, str]:
    checks: tuple[tuple[str, Sequence[str]], ...] = (
        ("pytest_autoops", ["/bin/bash", "-c", "PYTHONPATH=. pytest tests/test_autoops*.py -v"]),
        ("paper_options_diagnostics", ["./bin/algo", "paper-options-diagnostics", "--user", "paper_bot", "--symbol", "QQQ"]),
        ("paper_health_dry_run", ["scripts/check_algo_health.sh", "--dry-run", "PAPER"]),
    )
    for label, command in checks:
        print(f"AUTOOPS_VERIFY_STARTED check={label}")
        rc, out = _run(command, cwd=root, timeout=180.0)
        if rc != 0:
            return False, f"{label}_failed rc={rc} output={out[:500]}"
    return True, "verified"


def _drill_confirmed_paper(root: Path, *, environment: str, failure_type: str) -> int:
    allowed, allow_reason = _confirmed_paper_drill_allowed(environment)
    if not allowed:
        print(f"AUTOOPS_DRILL_FAILED reason={allow_reason}", file=sys.stderr)
        return 2

    started_at = _utc_timestamp()
    start_monotonic = time.monotonic()
    diagnosis, recovery_plan = AUTOOPS_FAILURE_SCENARIOS[failure_type]
    record = _base_drill_record(
        environment=environment,
        drill_mode="paper",
        started_at=started_at,
    )
    record.update(
        {
            "failure_class": _failure_class_for(failure_type),
            "failure_type": failure_type,
            "diagnosis": diagnosis,
            "recovery_plan": recovery_plan,
            "recovery_path": _recovery_path_for(failure_type),
        }
    )
    if not shutil.which("gh"):
        return _fail_confirmed_drill(root, record, start_monotonic=start_monotonic, reason="gh_unavailable")

    print(f"AUTOOPS_DRILL_START dry_run=false environment=paper failure={failure_type}")
    print(f"AUTOOPS_FAILURE_INJECTED type={failure_type}")
    if _failure_class_for(failure_type):
        print(f"AUTOOPS_FAILURE_CLASS class={_failure_class_for(failure_type)}")
    print(f"AUTOOPS_DIAGNOSED type={failure_type} diagnosis={diagnosis}")
    print(f"AUTOOPS_RECOVERY_PLAN type={failure_type} plan={recovery_plan}")
    if failure_type == "health_failed":
        print("AUTOOPS_HEALTH_CHECK result=unhealthy failure_class=health_failed")
    body_file = _write_synthetic_issue_body(
        root,
        failure_type=failure_type,
        diagnosis=diagnosis,
        recovery_plan=recovery_plan,
    )
    rc, out, issue_labels = _create_drill_issue(root, failure_type=failure_type, body_file=body_file)
    if rc != 0:
        return _fail_confirmed_drill_with_gh_output(
            root,
            record,
            start_monotonic=start_monotonic,
            reason=f"github_issue_failed rc={rc}",
            rc=rc,
            output=out,
        )
    issue_number = _parse_number_from_url(out)
    if issue_number is None:
        return _fail_confirmed_drill_with_gh_output(
            root,
            record,
            start_monotonic=start_monotonic,
            reason="github_issue_number_missing",
            rc=0,
            output=out,
        )
    record["issue_created"] = True
    record["issue_number"] = issue_number
    record["issue_labels"] = issue_labels
    print(f"AUTOOPS_ISSUE_CREATED issue={issue_number} labels={','.join(issue_labels)}")
    if failure_type == "health_failed":
        print("AUTOOPS_FAILURE_REPORTER result=issue_created failure_class=health_failed")

    processor = root / "scripts" / "process_codex_issues_local.sh"
    rc, out = _run([str(processor), "--issue", str(issue_number), "--limit", "1"], cwd=root, timeout=600.0)
    if rc != 0:
        return _fail_confirmed_drill(root, record, start_monotonic=start_monotonic, reason=f"codex_processor_failed rc={rc}")
    print(f"AUTOOPS_CODEX_STARTED issue={issue_number}")

    pr_payload = _gh_json(
        [
            "pr",
            "list",
            "--state",
            "all",
            "--search",
            f"#{issue_number}",
            "--json",
            "number,labels,mergedAt,url",
            "--limit",
            "1",
        ]
    )
    pr_row = pr_payload[0] if isinstance(pr_payload, list) and pr_payload and isinstance(pr_payload[0], dict) else None
    if pr_row is None:
        return _fail_confirmed_drill(root, record, start_monotonic=start_monotonic, reason="pr_not_found")
    pr_number = int(pr_row.get("number") or 0)
    record["pr_created"] = True
    record["pr_number"] = pr_number
    print(f"AUTOOPS_PR_CREATED pr={pr_number} issue={issue_number}")

    labels = _gh_label_names(pr_row)
    if "codex-validation-passed" not in labels:
        print(f"AUTOOPS_VALIDATION_FAILED pr={pr_number}")
        return _fail_confirmed_drill(root, record, start_monotonic=start_monotonic, reason="validation_not_passed")
    record["validation_passed"] = True
    print(f"AUTOOPS_VALIDATION_PASSED pr={pr_number}")
    if failure_type == "health_failed":
        print("AUTOOPS_VALIDATION_RAN result=passed failure_class=health_failed")

    if not str(pr_row.get("mergedAt") or "").strip():
        return _fail_confirmed_drill(root, record, start_monotonic=start_monotonic, reason="auto_merge_not_complete")
    record["merged"] = True
    print(f"AUTOOPS_AUTO_MERGED pr={pr_number}")

    verified, verify_reason = _run_paper_verification(root)
    if not verified:
        return _fail_confirmed_drill(root, record, start_monotonic=start_monotonic, reason=verify_reason)
    record["verified"] = True
    record["improved"] = True
    record["success"] = True
    record["duration_seconds"] = round(time.monotonic() - start_monotonic, 6)
    path = _write_drill_history(root, record)
    print("AUTOOPS_VERIFIED environment=paper")
    print("AUTOOPS_RECOVERY_COMPLETE environment=paper")
    if failure_type == "health_failed":
        print("AUTOOPS_RECOVERY_PATH_RECORDED failure_class=health_failed")
    print(f"AUTOOPS_DRILL_HISTORY path={path}")
    print("AUTOOPS_DRILL_SUCCESS dry_run=false environment=paper")
    return 0


def _verify_readiness(root: Path, *, environment: str, service: str | None = None) -> int:
    env_name = str(environment or "").strip().lower()
    paper_mac = env_name == "paper" and _platform_name() == "Darwin"
    status = _service_status(environment=environment, service=service)
    service_ok, service_reason = _service_active_or_expected_inactive(status)
    self_heal_status, _self_heal_output = _run_self_heal_dry_run(root, environment=environment)
    self_heal_detail = _self_heal_verify_detail(_self_heal_output)
    paper_self_heal_reads_file_log = (
        paper_mac
        and self_heal_status in {"healthy", "degraded"}
        and (
            "SELF_HEAL_LOG_SOURCE source=file" in _self_heal_output
            or "file-log fallback" in _self_heal_output.lower()
            or "paper_full.log" in _self_heal_output
        )
    )
    paper_file_log_fallback_ok = (
        paper_mac
        and str(status.get("service_active") or "") == "systemctl_unavailable"
        and paper_self_heal_reads_file_log
    )
    if paper_file_log_fallback_ok:
        service_ok = True
        service_reason = "paper_file_log_fallback_healthy"
    rows = _load_history_records(root)
    latest_drill_result = _history_result(_latest_history_row(rows))
    gh_authed, gh_auth_detail = _gh_authenticated()
    labels_present, missing_labels = _required_github_labels_present()
    prs = _autoops_prs()
    issue = _latest_autoops_issue()
    validation_status, validation_detail = _latest_validation_status_for_environment(
        prs,
        environment=environment,
        linked_issue=issue,
    )
    validation_workflow = _workflow_present(root, ".github/workflows/codex-pr-validation.yml")
    auto_merge_workflow = _workflow_present(root, ".github/workflows/codex-auto-merge.yml")
    workflows_present = bool(validation_workflow and auto_merge_workflow)
    intraday_timer = _systemd_unit_state("intraday-health.timer")
    intraday_latest_unit = _systemd_unit_state("intraday-health.service")
    end_day_timer_ready, end_day_timer_detail = _user_systemd_timer_ready(
        "algosphere-live-endday-analysis.timer",
        root,
    )
    intraday_timer_active = intraday_timer == "active" or (
        paper_mac and intraday_timer == "systemctl_unavailable"
    )
    intraday_json_status, intraday_json_path = _latest_intraday_health_json(root, environment=environment)
    autoops_cfg = _load_autoops_config(root)
    passwordless_sudo_ok = False
    if environment == "live":
        passwordless_sudo_ok, _sudo_detail = _passwordless_sudo_status(root)
    path_statuses = {rel: _exists_status(root, rel) for rel in REQUIRED_AUTOOPS_PATHS}
    required_paths_ready = all(
        status_text == "present" or status_text == "present_executable"
        for status_text in path_statuses.values()
    )
    if paper_mac and not paper_self_heal_reads_file_log:
        required_paths_ready = False
    options_ready = _options_readiness(root, environment=environment)
    effective_trading_mode = _effective_trading_mode(root, environment=environment)
    runtime_profile, runtime_profile_reasons = _runtime_profile(root, environment=environment)
    options_gate_ok = True
    options_gate_reason = ""
    options_gate_applies = _options_gate_applies(env_name=env_name, options_ready=options_ready)
    if options_gate_applies:
        options_gate_ok = options_ready.final_status == "ready"
        options_gate_reason = ",".join(options_ready.blocking_reasons) or "ready"
    recommendation, reason = _readiness_recommendation(
        service_active=str(status.get("service_active", "unknown")),
        service_ok=service_ok,
        self_heal_status=self_heal_status,
        github_authenticated=gh_authed,
        labels_present=labels_present,
        validation_status=validation_status,
        workflows_present=workflows_present,
        latest_drill_result=latest_drill_result,
        intraday_health_timer_active=intraday_timer_active,
        required_paths_ready=required_paths_ready,
    )
    if env_name == "live" and not options_gate_ok:
        recommendation = "blocked"
        reason = f"options_{options_gate_reason}"
    if env_name == "live" and runtime_profile_reasons:
        recommendation = "blocked"
        reason = ",".join(runtime_profile_reasons)
    print("AUTOOPS_VERIFY_READ_ONLY true")
    print(
        "AUTOOPS_VERIFY_CHECK service_active=%s expected=%s"
        % (status.get("service_active", "unknown"), service_reason)
    )
    print(f"AUTOOPS_VERIFY_CHECK intraday_health_timer={intraday_timer}")
    print(f"AUTOOPS_VERIFY_CHECK intraday_health_latest={intraday_json_status} unit={intraday_latest_unit} path={intraday_json_path}")
    print(
        "AUTOOPS_VERIFY_CHECK passwordless_sudo=%s"
        % ("yes" if passwordless_sudo_ok else "no" if environment == "live" else "not_applicable")
    )
    if environment == "live" and not passwordless_sudo_ok:
        print(_sudoers_guidance())
        if _sudo_detail:
            print(f"AUTOOPS_VERIFY_DETAIL sudo={_sudo_detail!r}")
    print(
        "AUTOOPS_VERIFY_CHECK end_day_timer=%s detail=%s"
        % ("yes" if end_day_timer_ready else "no", end_day_timer_detail)
    )
    print(
        "AUTOOPS_VERIFY_CHECK end_day_codex_autostart=%s"
        % ("yes" if bool(autoops_cfg["live_end_day_codex_autostart_enabled"]) else "no")
    )
    if self_heal_detail:
        print(f"AUTOOPS_VERIFY_CHECK self_heal={self_heal_status} detail=\"{self_heal_detail}\"")
    else:
        print(f"AUTOOPS_VERIFY_CHECK self_heal={self_heal_status}")
    if paper_mac:
        print(
            "AUTOOPS_VERIFY_CHECK paper_full_log_self_heal=%s"
            % ("readable" if paper_self_heal_reads_file_log else "not_readable")
        )
    print(f"AUTOOPS_VERIFY_CHECK github_authenticated={'yes' if gh_authed else 'no'}")
    print(f"AUTOOPS_VERIFY_DETAIL github_auth={gh_auth_detail}")
    if not gh_authed:
        print(_github_auth_guidance())
    print(
        "AUTOOPS_VERIFY_CHECK github_labels_present=%s missing=%s"
        % ("yes" if labels_present else "no", ",".join(missing_labels) or "none")
    )
    print(f"AUTOOPS_VERIFY_CHECK failure_reporter={path_statuses['scripts/report_algo_failure_to_github.sh']}")
    print(f"AUTOOPS_VERIFY_CHECK self_heal_script={path_statuses['scripts/run_self_heal.py']}")
    print(f"AUTOOPS_VERIFY_CHECK codex_processor={path_statuses['scripts/process_codex_issues_local.sh']}")
    print(
        "AUTOOPS_VERIFY_CHECK workflows validation=%s auto_merge=%s"
        % ("yes" if validation_workflow else "no", "yes" if auto_merge_workflow else "no")
    )
    if validation_detail:
        print(f"AUTOOPS_VERIFY_CHECK latest_pr_validation={validation_status} detail=\"{validation_detail}\"")
    else:
        print(f"AUTOOPS_VERIFY_CHECK latest_pr_validation={validation_status}")
    print(f"AUTOOPS_VERIFY_CHECK latest_drill={latest_drill_result}")
    print(f"AUTOOPS_VERIFY_CHECK options_pilot={'enabled' if _options_pilot_enabled(root) else 'disabled'}")
    print(f"AUTOOPS_VERIFY_CHECK trading_mode={effective_trading_mode}")
    print(f"AUTOOPS_VERIFY_CHECK runtime_profile={runtime_profile}")
    if runtime_profile_reasons:
        print(f"AUTOOPS_VERIFY_DETAIL runtime_profile_blocking_reasons={','.join(runtime_profile_reasons)}")
    print(f"AUTOOPS_VERIFY_CHECK options_enabled={'yes' if options_ready.config_enabled else 'no'}")
    print(f"AUTOOPS_VERIFY_CHECK options_mode={options_ready.mode}")
    print(f"AUTOOPS_VERIFY_CHECK options_live_pilot_enabled={'yes' if options_ready.live_pilot_enabled else 'no'}")
    print(f"AUTOOPS_VERIFY_CHECK options_long_premium_only={'yes' if options_ready.long_premium_only else 'no'}")
    print(f"AUTOOPS_VERIFY_CHECK options_broker_supported={'yes' if options_ready.broker_supported else 'no'}")
    print(f"AUTOOPS_VERIFY_CHECK options_risk_limits_safe={'yes' if options_ready.risk_limits_safe else 'no'}")
    print(f"AUTOOPS_VERIFY_CHECK options_route_active={'yes' if options_ready.route_active else 'no'}")
    if options_ready.blocking_reasons:
        print(f"AUTOOPS_VERIFY_DETAIL options_blocking_reasons={','.join(options_ready.blocking_reasons)}")
    if env_name == "live" and not options_gate_applies:
        print(f"AUTOOPS_VERIFY_DETAIL options_gate=not_applicable_for_{runtime_profile}")
    print(f"AUTOOPS_VERIFY_STATUS ready={str(recommendation == 'ready').lower()} reason={reason}")
    return 0 if recommendation == "ready" else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="Show read-only AutoOps status.")
    status_env = status.add_mutually_exclusive_group()
    status_env.add_argument("--live", action="store_true", help="Show live automation status.")
    status_env.add_argument("--paper", action="store_true", help="Show paper automation status.")
    status_env.add_argument(
        "--environment",
        choices=("paper", "live"),
        default=None,
        help="Compatibility alias for --live/--paper.",
    )
    status.add_argument(
        "--service",
        default=None,
        help="Service name override. Paper defaults to ALGO_PAPER_SERVICE or paper.service; live defaults to algo.service.",
    )
    status.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    status.add_argument("--full", action="store_true", help="Run read-only full AutoOps readiness status.")

    drill = sub.add_parser("drill", help="Run an AutoOps safety drill.")
    drill.add_argument("--dry-run", action="store_true", help="Simulate the full AutoOps flow without writes.")
    drill_env = drill.add_mutually_exclusive_group()
    drill_env.add_argument("--live", action="store_true", help="Run a live-environment drill simulation.")
    drill_env.add_argument("--paper", action="store_true", help="Run a paper-environment drill simulation.")
    drill.add_argument("--environment", choices=("paper", "live"), default="paper")
    drill.add_argument("--failure", choices=sorted(AUTOOPS_FAILURE_SCENARIOS), default=None)
    drill.add_argument("--confirm", action="store_true", help="Required for any future non-dry-run drill.")
    drill.add_argument("--paper-only", action="store_true", help="Required for any future non-dry-run drill.")
    drill.add_argument("--confirm-deploy", action="store_true", help="Reserved; never deploys live without this.")
    drill.add_argument("--project-root", type=Path, default=PROJECT_ROOT)

    report = sub.add_parser("report", help="Summarize AutoOps drill history.")
    report.add_argument("--project-root", type=Path, default=PROJECT_ROOT)

    verify = sub.add_parser("verify", help="Run read-only AutoOps readiness verification.")
    verify_env = verify.add_mutually_exclusive_group(required=True)
    verify_env.add_argument("--live", action="store_true", help="Verify live automation readiness.")
    verify_env.add_argument("--paper", action="store_true", help="Verify paper automation readiness.")
    verify.add_argument("--service", default=None, help=argparse.SUPPRESS)
    verify.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    verify.add_argument("--full", action="store_true", help="Compatibility flag; verify always runs the full read-only checks.")

    deploy = sub.add_parser("deploy-latest", help="Deploy latest main on the guarded live host.")
    deploy_env = deploy.add_mutually_exclusive_group(required=True)
    deploy_env.add_argument("--live", action="store_true", help="Deploy live algo.service on the live host.")
    deploy_env.add_argument("--paper", action="store_true", help="Blocked; paper deploy is intentionally unsupported.")
    deploy.add_argument("--project-root", type=Path, default=PROJECT_ROOT)

    endday = sub.add_parser("end-day-analysis", help="Run guarded live end-day analysis and issue routing.")
    endday_env = endday.add_mutually_exclusive_group(required=True)
    endday_env.add_argument("--live", action="store_true", help="Run live end-day analysis.")
    endday_env.add_argument("--paper", action="store_true", help="Blocked; paper end-day automation is intentionally unsupported.")
    endday.add_argument("--dry-run", action="store_true", help="Run analysis without creating GitHub issues.")
    endday.add_argument("--project-root", type=Path, default=PROJECT_ROOT)

    install_endday = sub.add_parser("install-endday-timer", help="Install the live end-day analysis user timer.")
    install_env = install_endday.add_mutually_exclusive_group(required=True)
    install_env.add_argument("--live", action="store_true", help="Install live timer on the live host.")
    install_env.add_argument("--paper", action="store_true", help="Blocked; paper timer is intentionally unsupported.")
    install_endday.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    install_endday.add_argument("--user-systemd-dir", type=Path, default=None, help=argparse.SUPPRESS)

    dedupe = sub.add_parser("dedupe-issues", help="Close duplicate analyzer-created live issues by fingerprint.")
    dedupe_env = dedupe.add_mutually_exclusive_group(required=True)
    dedupe_env.add_argument("--live", action="store_true", help="Dedupe live analyzer issues.")
    dedupe_env.add_argument("--paper", action="store_true", help="Blocked; live-only dedupe for now.")
    dedupe.add_argument("--dry-run", action="store_true", help="Report duplicates without commenting or closing.")
    dedupe.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    root = Path(getattr(args, "project_root", PROJECT_ROOT)).resolve()
    if args.command == "status":
        explicit_environment = bool(args.live or args.paper or args.environment)
        if args.live:
            environment = "live"
        elif args.paper:
            environment = "paper"
        else:
            environment = str(args.environment or "paper")
        return _print_status(
            root,
            environment=environment,
            service=args.service,
            full=bool(args.full),
            explicit_environment=explicit_environment,
        )
    if args.command == "drill":
        drill_environment = "live" if bool(args.live) else "paper" if bool(args.paper) else str(args.environment)
        if args.dry_run:
            return _drill_dry_run(root, environment=drill_environment, failure_type=args.failure)
        if args.confirm and args.paper_only and args.failure:
            return _drill_confirmed_paper(
                root,
                environment=drill_environment,
                failure_type=str(args.failure),
            )
        if args.failure:
            started_at = _utc_timestamp()
            start_monotonic = time.monotonic()
            record = _base_drill_record(
                environment=drill_environment,
                drill_mode="paper" if args.paper_only else "unknown",
                started_at=started_at,
            )
            return _block_drill(
                root,
                record,
                start_monotonic=start_monotonic,
                reason="non_dry_run_requires_confirm_and_paper_only",
                next_command="./bin/algo autoops drill --dry-run",
            )
        started_at = _utc_timestamp()
        start_monotonic = time.monotonic()
        record = _base_drill_record(
            environment=drill_environment,
            drill_mode="paper" if args.paper_only else "unknown",
            started_at=started_at,
        )
        if not (args.confirm and args.paper_only):
            return _block_drill(
                root,
                record,
                start_monotonic=start_monotonic,
                reason="non_dry_run_requires_confirm_and_paper_only",
                next_command="./bin/algo autoops drill --dry-run",
            )
        return _block_drill(
            root,
            record,
            start_monotonic=start_monotonic,
            reason="non_dry_run_not_implemented_safe_default",
            next_command="./bin/algo autoops drill --dry-run",
        )
    if args.command == "report":
        return _print_report(root)
    if args.command == "verify":
        return _verify_readiness(
            root,
            environment="live" if bool(args.live) else "paper",
            service=args.service,
        )
    if args.command == "deploy-latest":
        return _deploy_latest(
            root,
            environment="live" if bool(args.live) else "paper",
        )
    if args.command == "end-day-analysis":
        return _end_day_analysis(
            root,
            environment="live" if bool(args.live) else "paper",
            dry_run=bool(args.dry_run),
        )
    if args.command == "install-endday-timer":
        return _install_endday_timer(
            root,
            environment="live" if bool(args.live) else "paper",
            user_systemd_dir=args.user_systemd_dir,
        )
    if args.command == "dedupe-issues":
        return _dedupe_issues(
            root,
            environment="live" if bool(args.live) else "paper",
            dry_run=bool(args.dry_run),
        )
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
