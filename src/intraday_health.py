from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from src.options_pilot_status import build_options_pilot_status


CommandRunner = Callable[[Sequence[str], Path, Mapping[str, str] | None], "CommandResult"]


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def run_readonly_command(
    command: Sequence[str],
    cwd: Path,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """Run one read-only diagnostic command and capture failure details."""
    proc = subprocess.run(
        list(command),
        cwd=str(cwd),
        env=dict(env) if env is not None else None,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def intraday_health_output_path(
    root: str | Path,
    *,
    env_name: str,
    now: datetime | None = None,
) -> Path:
    now_et = now or datetime.now(ZoneInfo("America/New_York"))
    day = now_et.strftime("%Y-%m-%d")
    env_norm = str(env_name or "live").strip().lower()
    return Path(root) / "data" / "intraday_health" / day / f"{env_norm}_intraday_health.json"


def _truncate(text: str, limit: int = 4000) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[-limit:]


def _command_check(name: str, command: Sequence[str], *, root: Path, runner: CommandRunner, env: Mapping[str, str]) -> dict[str, Any]:
    try:
        result = runner(command, root, env)
    except Exception as exc:
        return {
            "name": name,
            "ok": False,
            "returncode": None,
            "command": list(command),
            "error": str(exc),
            "stdout_tail": "",
            "stderr_tail": "",
        }
    ok = int(result.returncode) == 0
    return {
        "name": name,
        "ok": ok,
        "returncode": int(result.returncode),
        "command": list(command),
        "stdout_tail": _truncate(result.stdout),
        "stderr_tail": _truncate(result.stderr),
    }


def _regular_market_hours(now_et: datetime) -> bool:
    if now_et.weekday() >= 5:
        return False
    minutes = now_et.hour * 60 + now_et.minute
    return (9 * 60 + 30) <= minutes <= (16 * 60)


def build_intraday_health_report(
    *,
    root: str | Path,
    env_name: str = "live",
    since: str = "30 min ago",
    now: datetime | None = None,
    runner: CommandRunner = run_readonly_command,
) -> dict[str, Any]:
    """Build a read-only intraday health report for the trading runtime."""
    repo_root = Path(root).resolve()
    env_norm = str(env_name or "live").strip().lower()
    if env_norm not in {"live", "paper"}:
        raise ValueError("env_name must be live or paper")
    now_et = now or datetime.now(ZoneInfo("America/New_York"))
    cmd_env = {**os.environ, "PYTHONPATH": str(repo_root)}
    service_name = "algo.service" if env_norm == "live" else "paper.service"
    env_flag = f"--{env_norm}"
    checks = [
        _command_check(
            "service_active",
            ("systemctl", "is-active", service_name),
            root=repo_root,
            runner=runner,
            env=cmd_env,
        ),
        _command_check(
            "journal",
            ("journalctl", "-u", service_name, "--since", since, "--no-pager"),
            root=repo_root,
            runner=runner,
            env=cmd_env,
        ),
        _command_check(
            "positions",
            (str(repo_root / "bin" / "algo"), "positions", env_flag),
            root=repo_root,
            runner=runner,
            env=cmd_env,
        ),
    ]
    failed = [row for row in checks if not row.get("ok")]
    status = "healthy" if not failed else "degraded"
    recommendations = ["none"] if status == "healthy" else [
        "inspect_intraday_health_json",
        "inspect_recent_journal",
    ]
    journal_text = ""
    for row in checks:
        if row.get("name") == "journal":
            journal_text = str(row.get("stdout_tail") or "")
            break
    options_status = build_options_pilot_status(
        root=repo_root,
        env_name=env_norm,
        log_text=journal_text,
    )
    if (
        env_norm == "live"
        and _regular_market_hours(now_et)
        and options_status.config_enabled
        and options_status.live_pilot_enabled
        and not options_status.latest_entry_lane_logs
    ):
        recommendations = [item for item in recommendations if item != "none"]
        recommendations.append("inspect_options_lane")
    return {
        "generated_at": now_et.isoformat(),
        "env": env_norm,
        "since": since,
        "status": status,
        "service_name": service_name,
        "checks": checks,
        "errors": [
            {
                "name": row.get("name"),
                "returncode": row.get("returncode"),
                "error": row.get("error") or row.get("stderr_tail") or row.get("stdout_tail"),
            }
            for row in failed
        ],
        "recommendations": recommendations,
        "options_pilot": {
            "enabled": options_status.live_pilot_enabled,
            "lane_logs_seen": bool(options_status.latest_entry_lane_logs),
            "reason_if_no_orders": options_status.reason_if_no_orders,
        },
        "readonly": True,
    }


def save_intraday_health_report(report: Mapping[str, Any], root: str | Path) -> Path:
    generated_at = str(report.get("generated_at") or "").strip()
    parsed_now: datetime | None = None
    if generated_at:
        try:
            parsed_now = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        except ValueError:
            parsed_now = None
    path = intraday_health_output_path(root, env_name=str(report.get("env") or "live"), now=parsed_now)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path
