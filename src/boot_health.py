"""Post-reboot readiness checks and constrained recovery for live trading."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo

from src.config_loader import load_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NY_TZ = ZoneInfo("America/New_York")


@dataclass
class CommandResult:
    """Result from an external command."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass
class CheckResult:
    """One boot-health check result."""

    name: str
    status: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status in {"pass", "skip"}


@dataclass
class BootHealthConfig:
    """Runtime configuration for boot health checks."""

    project_root: Path = PROJECT_ROOT
    expected_timezone: str = "America/New_York"
    disk_min_free_mb: int = int(os.environ.get("ALGO_BOOT_HEALTH_DISK_MIN_MB", "1024"))
    memory_min_free_mb: int = int(os.environ.get("ALGO_BOOT_HEALTH_MEMORY_MIN_MB", "512"))
    heartbeat_max_age_minutes: int = int(os.environ.get("ALGO_BOOT_HEALTH_MAX_HEARTBEAT_MINUTES", "45"))
    report_dir: Path = Path(os.environ.get("ALGO_BOOT_HEALTH_REPORT_DIR", str(PROJECT_ROOT / "reports" / "boot_health")))
    failed_unit_allowlist: tuple[str, ...] = tuple(
        unit.strip()
        for unit in os.environ.get("ALGO_BOOT_HEALTH_FAILED_UNIT_ALLOWLIST", "").split(",")
        if unit.strip()
    )
    restart_loop_threshold: int = int(os.environ.get("ALGO_BOOT_HEALTH_RESTART_THRESHOLD", "3"))
    environment: str = os.environ.get("ALGO_ENV", "live").strip().lower() or "live"
    health_script: Path = PROJECT_ROOT / "scripts" / "check_algo_health.sh"
    heartbeat_paths: tuple[Path, ...] = (
        PROJECT_ROOT / "data" / "heartbeat" / "live.json",
        PROJECT_ROOT / "data" / "live_heartbeat.json",
    )


class SystemProbe:
    """Thin wrapper around OS commands so tests can mock all host state."""

    def run(self, args: Sequence[str], *, timeout: float = 10.0) -> CommandResult:
        try:
            proc = subprocess.run(
                list(args),
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            return CommandResult(127, "", str(exc))
        except subprocess.TimeoutExpired as exc:
            return CommandResult(124, exc.stdout or "", exc.stderr or "timeout")
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)


class BootHealthChecker:
    """Runs boot readiness checks and optional low-risk repairs."""

    def __init__(
        self,
        config: BootHealthConfig | None = None,
        *,
        probe: SystemProbe | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config or BootHealthConfig()
        self.probe = probe or SystemProbe()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.repairs: list[dict[str, Any]] = []
        self.market_open: bool | None = None

    def run_all(self, *, repair: bool = False) -> dict[str, Any]:
        if repair:
            self.config.report_dir.mkdir(parents=True, exist_ok=True)
        checks: list[CheckResult] = []
        check_methods = (
            self.check_timezone,
            self.check_time_sync,
            self.check_network,
            self.check_default_route,
            self.check_dns,
            lambda: self.check_unit_enabled("algo_service_enabled", "algo.service"),
            lambda: self.check_unit_active("algo_service_active", "algo.service", repair=repair),
            lambda: self.check_unit_enabled("health_timer_enabled", "algo-health-check.timer"),
            lambda: self.check_unit_active("health_timer_active", "algo-health-check.timer", repair=repair),
            lambda: self.check_failed_units(repair=repair),
            self.check_bridge,
            self.check_serial_getty_masked,
            self.check_health_script,
            lambda: self.check_health_script_selinux(repair=repair),
            self.check_project_paths,
            self.check_disk,
            self.check_memory,
            self.check_broker_auth,
            self.check_market_clock,
            self.check_heartbeat,
            self.check_restart_loop,
            self.check_timer_next_trigger,
        )
        for method in check_methods:
            try:
                checks.append(method())
            except Exception as exc:  # fail closed for individual check bugs
                checks.append(CheckResult(getattr(method, "__name__", "check"), "fail", f"{type(exc).__name__}: {exc}"))
        if repair:
            self.run_regular_health_check()
        ready = all(check.passed for check in checks)
        report = self._build_report(checks, ready)
        self.write_reports(report)
        return report

    def _build_report(self, checks: list[CheckResult], ready: bool) -> dict[str, Any]:
        now_utc = self.now().astimezone(timezone.utc)
        now_ny = now_utc.astimezone(NY_TZ)
        return {
            "timestamp_utc": now_utc.isoformat().replace("+00:00", "Z"),
            "timestamp_new_york": now_ny.isoformat(),
            "hostname": socket.gethostname(),
            "boot_id": self._read_text(Path("/proc/sys/kernel/random/boot_id")).strip(),
            "uptime": self._read_text(Path("/proc/uptime")).split()[0] if Path("/proc/uptime").exists() else "unknown",
            "git_commit": self._git_commit(),
            "environment": self.config.environment,
            "ready": ready,
            "result": "READY" if ready else "NOT READY",
            "checks": [check.__dict__ for check in checks],
            "repairs": self.repairs,
        }

    def _read_text(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return "unknown"

    def _git_commit(self) -> str:
        result = self.probe.run(["git", "-c", f"safe.directory={self.config.project_root}", "-C", str(self.config.project_root), "rev-parse", "HEAD"])
        return result.stdout.strip() if result.returncode == 0 else "unknown"

    def _ok(self, name: str, message: str, **details: Any) -> CheckResult:
        return CheckResult(name, "pass", message, details)

    def _fail(self, name: str, message: str, **details: Any) -> CheckResult:
        return CheckResult(name, "fail", message, details)

    def _skip(self, name: str, message: str, **details: Any) -> CheckResult:
        return CheckResult(name, "skip", message, details)

    def check_timezone(self) -> CheckResult:
        result = self.probe.run(["timedatectl", "show", "-p", "Timezone", "--value"])
        tz = result.stdout.strip()
        if result.returncode == 0 and tz == self.config.expected_timezone:
            return self._ok("timezone", tz)
        return self._fail("timezone", f"expected {self.config.expected_timezone}, got {tz or 'unknown'}")

    def check_time_sync(self) -> CheckResult:
        result = self.probe.run(["timedatectl", "show", "-p", "SystemClockSynchronized", "--value"])
        synced = result.stdout.strip().lower()
        if result.returncode == 0 and synced in {"yes", "true"}:
            return self._ok("time_sync", "synchronized")
        return self._fail("time_sync", "system clock is not synchronized", value=synced)

    def check_network(self) -> CheckResult:
        result = self.probe.run(["nmcli", "-t", "-f", "CONNECTIVITY", "general"])
        state = result.stdout.strip().lower()
        if result.returncode == 0 and state == "full":
            return self._ok("network", "full connectivity")
        return self._fail("network", f"NetworkManager connectivity is {state or 'unknown'}")

    def check_default_route(self) -> CheckResult:
        result = self.probe.run(["ip", "route", "show", "default"])
        if result.returncode == 0 and result.stdout.strip():
            return self._ok("default_route", "default route exists", route=result.stdout.strip().splitlines()[0])
        return self._fail("default_route", "default route missing")

    def check_dns(self) -> CheckResult:
        result = self.probe.run(["getent", "hosts", "api.alpaca.markets"])
        if result.returncode == 0 and result.stdout.strip():
            return self._ok("dns", "api.alpaca.markets resolves")
        return self._fail("dns", "DNS resolution failed for api.alpaca.markets")

    def check_unit_enabled(self, name: str, unit: str) -> CheckResult:
        result = self.probe.run(["systemctl", "is-enabled", unit])
        state = result.stdout.strip()
        if result.returncode == 0 and state == "enabled":
            return self._ok(name, f"{unit} enabled", unit=unit, state=state)
        return self._fail(name, f"{unit} is {state or 'not enabled'}", unit=unit, state=state)

    def check_unit_active(self, name: str, unit: str, *, repair: bool = False) -> CheckResult:
        result = self.probe.run(["systemctl", "is-active", unit])
        state = result.stdout.strip()
        if result.returncode == 0 and state == "active":
            return self._ok(name, f"{unit} active", unit=unit, state=state)
        if repair and self._is_enabled(unit):
            start = self.probe.run(["systemctl", "start", unit], timeout=30)
            self.repairs.append({"action": "start_unit", "unit": unit, "returncode": start.returncode})
            after = self.probe.run(["systemctl", "is-active", unit])
            if after.returncode == 0 and after.stdout.strip() == "active":
                return self._ok(name, f"{unit} started by repair", unit=unit, state="active", repaired=True)
        return self._fail(name, f"{unit} is {state or 'inactive'}", unit=unit, state=state)

    def _is_enabled(self, unit: str) -> bool:
        result = self.probe.run(["systemctl", "is-enabled", unit])
        return result.returncode == 0 and result.stdout.strip() == "enabled"

    def check_failed_units(self, *, repair: bool = False) -> CheckResult:
        result = self.probe.run(["systemctl", "--failed", "--plain", "--no-legend"])
        failed = parse_failed_units(result.stdout)
        unexpected = [u for u in failed if u not in self.config.failed_unit_allowlist]
        if repair:
            for unit in list(unexpected):
                active = self.probe.run(["systemctl", "is-active", unit])
                if active.returncode == 0 and active.stdout.strip() == "active":
                    reset = self.probe.run(["systemctl", "reset-failed", unit])
                    self.repairs.append({"action": "reset_failed", "unit": unit, "returncode": reset.returncode})
                    unexpected.remove(unit)
        if unexpected:
            return self._fail("failed_units", "unexpected failed units exist", unexpected=unexpected, allowed=list(self.config.failed_unit_allowlist))
        return self._ok("failed_units", "no unexpected failed units", allowed=list(self.config.failed_unit_allowlist), failed=failed)

    def check_bridge(self) -> CheckResult:
        result = self.probe.run(["nmcli", "-t", "-f", "NAME,DEVICE,STATE", "connection", "show", "--active"])
        bad = [line for line in result.stdout.splitlines() if line.startswith("Bridge br0:") and re.search(r":(activated|activating|connecting)$", line)]
        if bad:
            return self._fail("bridge_br0", "Bridge br0 is active or connecting", matches=bad)
        return self._ok("bridge_br0", "Bridge br0 is not active")

    def check_serial_getty_masked(self) -> CheckResult:
        result = self.probe.run(["systemctl", "is-enabled", "serial-getty@ttyS0.service"])
        state = result.stdout.strip()
        if state == "masked":
            return self._ok("serial_getty_ttys0", "serial-getty@ttyS0.service is masked")
        return self._fail("serial_getty_ttys0", f"serial-getty@ttyS0.service is {state or 'unknown'}")

    def check_health_script(self) -> CheckResult:
        path = self.config.health_script
        if path.exists() and os.access(path, os.X_OK):
            return self._ok("health_script", "health-check script exists and is executable", path=str(path))
        return self._fail("health_script", "health-check script missing or not executable", path=str(path))

    def check_health_script_selinux(self, *, repair: bool = False) -> CheckResult:
        path = self.config.health_script
        result = self.probe.run(["ls", "-Z", str(path)])
        selinux_type = parse_selinux_type(result.stdout)
        if selinux_type in {"bin_t", "shell_exec_t"}:
            return self._ok("health_script_selinux", f"SELinux type {selinux_type}", type=selinux_type)
        if repair and self._has_persistent_fcontext(path):
            restore = self.probe.run(["restorecon", "-v", str(path)])
            self.repairs.append({"action": "restorecon", "path": str(path), "returncode": restore.returncode})
            after = self.probe.run(["ls", "-Z", str(path)])
            selinux_type = parse_selinux_type(after.stdout)
            if selinux_type in {"bin_t", "shell_exec_t"}:
                return self._ok("health_script_selinux", f"SELinux type {selinux_type} restored", type=selinux_type, repaired=True)
        return self._fail("health_script_selinux", f"unexpected SELinux type {selinux_type or 'unknown'}", type=selinux_type)

    def _has_persistent_fcontext(self, path: Path) -> bool:
        result = self.probe.run(["semanage", "fcontext", "-l"])
        return result.returncode == 0 and str(path) in result.stdout and "bin_t" in result.stdout

    def check_project_paths(self) -> CheckResult:
        required = [
            self.config.project_root / "config" / "default.yaml",
            self.config.project_root / "scripts" / "algo_loop.py",
            self.config.project_root / "scripts" / "check_algo_health.sh",
            self.config.project_root / "bin" / "algo",
            self.config.project_root / "data",
        ]
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            return self._fail("project_paths", "required project paths missing", missing=missing)
        return self._ok("project_paths", "required project paths exist")

    def check_disk(self) -> CheckResult:
        usage = shutil.disk_usage(self.config.project_root)
        free_mb = int(usage.free / 1024 / 1024)
        if free_mb >= self.config.disk_min_free_mb:
            return self._ok("disk_space", f"{free_mb} MB free", free_mb=free_mb, threshold_mb=self.config.disk_min_free_mb)
        return self._fail("disk_space", f"{free_mb} MB free below threshold", free_mb=free_mb, threshold_mb=self.config.disk_min_free_mb)

    def check_memory(self) -> CheckResult:
        free_mb = available_memory_mb()
        if free_mb >= self.config.memory_min_free_mb:
            return self._ok("memory", f"{free_mb} MB available", available_mb=free_mb, threshold_mb=self.config.memory_min_free_mb)
        return self._fail("memory", f"{free_mb} MB available below threshold", available_mb=free_mb, threshold_mb=self.config.memory_min_free_mb)

    def check_broker_auth(self) -> CheckResult:
        return self.check_alpaca_auth()

    def check_alpaca_auth(self) -> CheckResult:
        try:
            broker = self._make_broker()
            snapshot = broker.get_account_snapshot()
        except Exception as exc:
            return self._fail("alpaca_auth", f"Alpaca account check failed: {type(exc).__name__}", error=str(exc))
        return self._ok("alpaca_auth", "Alpaca account reachable", equity=snapshot.get("equity"))

    def check_market_clock(self) -> CheckResult:
        try:
            broker = self._make_broker()
            trading = getattr(broker, "_trading", None)
            clock = trading.get_clock() if trading is not None else None
            if trading is not None and hasattr(trading, "get_calendar"):
                trading.get_calendar()
            self.market_open = bool(getattr(clock, "is_open", False))
        except Exception as exc:
            return self._fail("market_clock", f"Alpaca market clock/calendar failed: {type(exc).__name__}", error=str(exc))
        return self._ok("market_clock", "Alpaca market clock/calendar reachable", is_open=self.market_open)

    def _make_broker(self) -> Any:
        from src.brokers.broker_factory import get_broker

        cfg = load_config(self.config.project_root / "config" / "default.yaml")
        paper = self.config.environment != "live"
        return get_broker(cfg, paper=paper)

    def check_heartbeat(self) -> CheckResult:
        if self.market_open is False or not is_regular_trading_window(self.now()):
            return self._ok("heartbeat", "market closed or outside regular trading window; heartbeat not required")
        paths = configured_heartbeat_paths(self.config)
        fresh = newest_existing_path(paths)
        if fresh is not None:
            age = int((self.now().timestamp() - fresh.stat().st_mtime) / 60)
            if age <= self.config.heartbeat_max_age_minutes:
                return self._ok("heartbeat", f"fresh heartbeat file age {age} minutes", path=str(fresh), age_minutes=age)
            return self._fail("heartbeat", f"heartbeat file stale: {age} minutes", path=str(fresh), age_minutes=age)
        result = self.probe.run(["journalctl", "-u", "algo.service", "--since", f"{self.config.heartbeat_max_age_minutes} minutes ago", "--no-pager"])
        if result.returncode == 0 and re.search(r"heartbeat|LIVE_LOOP_SLEEP|TRADE_CYCLE_GATE|OPTIONS_CYCLE_SUMMARY", result.stdout, re.I):
            return self._ok("heartbeat", "recent algo.service journal activity found")
        return self._fail("heartbeat", "no recent heartbeat, cycle log, or journal activity found")

    def check_restart_loop(self) -> CheckResult:
        result = self.probe.run(["systemctl", "show", "algo.service", "-p", "NRestarts", "--value"])
        text = result.stdout.strip()
        try:
            restarts = int(text)
        except ValueError:
            restarts = 0
        if restarts <= self.config.restart_loop_threshold:
            return self._ok("restart_loop", f"NRestarts={restarts}", restarts=restarts)
        return self._fail("restart_loop", "algo.service restart count exceeds threshold", restarts=restarts, threshold=self.config.restart_loop_threshold)

    def check_timer_next_trigger(self) -> CheckResult:
        result = self.probe.run(["systemctl", "list-timers", "algo-health-check.timer", "--no-legend", "--all"])
        line = result.stdout.strip()
        if result.returncode == 0 and line and not line.startswith("n/a"):
            return self._ok("health_timer_next", "health-check timer has a next trigger", timer=line)
        return self._fail("health_timer_next", "health-check timer has no valid next trigger", timer=line)

    def run_regular_health_check(self) -> None:
        result = self.probe.run([str(self.config.health_script), "--dry-run", "LIVE"], timeout=120)
        self.repairs.append({"action": "run_regular_health_check", "returncode": result.returncode})

    def write_reports(self, report: dict[str, Any]) -> None:
        self.config.report_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(self.config.report_dir / "latest.json", json.dumps(report, indent=2, sort_keys=True) + "\n")
        atomic_write(self.config.report_dir / "latest.md", render_markdown(report))


def parse_failed_units(output: str) -> list[str]:
    units: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        first = line.split()[0]
        if first.endswith(".service") or first.endswith(".timer") or first.endswith(".target"):
            units.append(first)
    return units


def parse_selinux_type(output: str) -> str | None:
    match = re.search(r"\b[a-zA-Z0-9_]+_u:[a-zA-Z0-9_]+_r:([a-zA-Z0-9_]+):", output)
    return match.group(1) if match else None


def available_memory_mb() -> int:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def configured_heartbeat_paths(config: BootHealthConfig) -> list[Path]:
    extra = os.environ.get("ALGO_BOOT_HEALTH_HEARTBEAT_FILE", "").strip()
    paths = list(config.heartbeat_paths)
    if extra:
        paths.insert(0, Path(extra))
    return paths


def newest_existing_path(paths: Sequence[Path]) -> Path | None:
    existing = [p for p in paths if p.exists()]
    return max(existing, key=lambda p: p.stat().st_mtime) if existing else None


def is_regular_trading_window(now_utc: datetime) -> bool:
    et = now_utc.astimezone(NY_TZ)
    if et.weekday() >= 5:
        return False
    return time(9, 30) <= et.time() <= time(16, 5)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def render_human(report: dict[str, Any]) -> str:
    lines = ["AlgoSphere Boot Health", ""]
    for check in report["checks"]:
        prefix = "PASS" if check["status"] in {"pass", "skip"} else "FAIL"
        lines.append(f"{prefix} {check['name']}: {check['message']}")
    lines.extend(["", f"RESULT: {report['result']}"])
    return "\n".join(lines) + "\n"


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# AlgoSphere Boot Health",
        "",
        f"- Result: {report['result']}",
        f"- UTC: {report['timestamp_utc']}",
        f"- New York: {report['timestamp_new_york']}",
        f"- Hostname: {report['hostname']}",
        f"- Boot ID: {report['boot_id']}",
        f"- Uptime seconds: {report['uptime']}",
        f"- Git commit: {report['git_commit']}",
        f"- Environment: {report['environment']}",
        "",
        "## Checks",
        "",
    ]
    for check in report["checks"]:
        mark = "PASS" if check["status"] in {"pass", "skip"} else "FAIL"
        lines.append(f"- {mark} `{check['name']}`: {check['message']}")
    lines.extend(["", "## Repairs", ""])
    if report["repairs"]:
        for repair in report["repairs"]:
            lines.append(f"- `{repair.get('action')}`: {json.dumps(repair, sort_keys=True)}")
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate post-reboot AlgoSphere live readiness.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--quiet", action="store_true", help="Suppress terminal output.")
    parser.add_argument("--repair", action="store_true", help="Perform only approved low-risk repairs.")
    args = parser.parse_args(argv)

    try:
        checker = BootHealthChecker()
        report = checker.run_all(repair=args.repair)
        if not args.quiet:
            if args.json:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print(render_human(report), end="")
        return 0 if report["ready"] else 1
    except Exception as exc:
        if not args.quiet:
            print(f"boot-health command failed unexpectedly: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
