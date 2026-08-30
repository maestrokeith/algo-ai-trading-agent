from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import boot_health
from src.boot_health import BootHealthChecker, BootHealthConfig, CommandResult


class FakeProbe:
    def __init__(self, responses: dict[tuple[str, ...], CommandResult] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, ...]] = []

    def run(self, args, *, timeout: float = 10.0) -> CommandResult:
        key = tuple(str(a) for a in args)
        self.calls.append(key)
        return self.responses.get(key, CommandResult(0, "", ""))


def cfg(tmp_path: Path, **kwargs) -> BootHealthConfig:
    root = tmp_path / "algo"
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(exist_ok=True)
    (root / "bin").mkdir(exist_ok=True)
    (root / "data").mkdir(exist_ok=True)
    (root / "config" / "default.yaml").write_text("broker:\n  paper: false\n", encoding="utf-8")
    (root / "scripts" / "algo_loop.py").write_text("", encoding="utf-8")
    health = root / "scripts" / "check_algo_health.sh"
    health.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    health.chmod(0o755)
    (root / "bin" / "algo").write_text("", encoding="utf-8")
    return BootHealthConfig(
        project_root=root,
        report_dir=root / "reports" / "boot_health",
        health_script=health,
        heartbeat_paths=(root / "data" / "heartbeat" / "live.json", root / "data" / "live_heartbeat.json"),
        **kwargs,
    )


def checker(tmp_path: Path, responses: dict[tuple[str, ...], CommandResult] | None = None, **kwargs) -> BootHealthChecker:
    return BootHealthChecker(cfg(tmp_path, **kwargs), probe=FakeProbe(responses), now=lambda: datetime(2026, 7, 21, 14, 0, tzinfo=timezone.utc))


def test_timezone_pass_fail(tmp_path: Path) -> None:
    good = checker(tmp_path, {("timedatectl", "show", "-p", "Timezone", "--value"): CommandResult(0, "America/New_York\n")})
    bad = checker(tmp_path, {("timedatectl", "show", "-p", "Timezone", "--value"): CommandResult(0, "UTC\n")})
    assert good.check_timezone().status == "pass"
    assert bad.check_timezone().status == "fail"


def test_time_sync_pass_fail(tmp_path: Path) -> None:
    assert checker(tmp_path, {("timedatectl", "show", "-p", "SystemClockSynchronized", "--value"): CommandResult(0, "yes\n")}).check_time_sync().status == "pass"
    assert checker(tmp_path, {("timedatectl", "show", "-p", "SystemClockSynchronized", "--value"): CommandResult(0, "no\n")}).check_time_sync().status == "fail"


@pytest.mark.parametrize(("state", "expected"), [("full", "pass"), ("limited", "fail"), ("none", "fail")])
def test_network_full_limited_offline(tmp_path: Path, state: str, expected: str) -> None:
    c = checker(tmp_path, {("nmcli", "-t", "-f", "CONNECTIVITY", "general"): CommandResult(0, state)})
    assert c.check_network().status == expected


def test_missing_default_route(tmp_path: Path) -> None:
    c = checker(tmp_path, {("ip", "route", "show", "default"): CommandResult(0, "")})
    assert c.check_default_route().status == "fail"


def test_service_enabled_disabled_and_active_inactive(tmp_path: Path) -> None:
    c = checker(
        tmp_path,
        {
            ("systemctl", "is-enabled", "algo.service"): CommandResult(0, "enabled\n"),
            ("systemctl", "is-active", "algo.service"): CommandResult(0, "active\n"),
        },
    )
    assert c.check_unit_enabled("service_enabled", "algo.service").status == "pass"
    assert c.check_unit_active("service_active", "algo.service").status == "pass"
    c = checker(
        tmp_path,
        {
            ("systemctl", "is-enabled", "algo.service"): CommandResult(1, "disabled\n"),
            ("systemctl", "is-active", "algo.service"): CommandResult(3, "inactive\n"),
        },
    )
    assert c.check_unit_enabled("service_enabled", "algo.service").status == "fail"
    assert c.check_unit_active("service_active", "algo.service").status == "fail"


def test_timer_enabled_disabled(tmp_path: Path) -> None:
    c = checker(tmp_path, {("systemctl", "is-enabled", "algo-health-check.timer"): CommandResult(1, "disabled\n")})
    assert c.check_unit_enabled("timer_enabled", "algo-health-check.timer").status == "fail"


def test_successful_one_shot_inactive_is_not_failed_unit(tmp_path: Path) -> None:
    c = checker(tmp_path, {("systemctl", "--failed", "--plain", "--no-legend"): CommandResult(0, "")})
    result = c.check_failed_units()
    assert result.status == "pass"


def test_unexpected_failed_units(tmp_path: Path) -> None:
    out = "bad.service loaded failed failed Bad unit\n"
    c = checker(tmp_path, {("systemctl", "--failed", "--plain", "--no-legend"): CommandResult(0, out)})
    result = c.check_failed_units()
    assert result.status == "fail"
    assert result.details["unexpected"] == ["bad.service"]


def test_masked_serial_getty_handling(tmp_path: Path) -> None:
    c = checker(tmp_path, {("systemctl", "is-enabled", "serial-getty@ttyS0.service"): CommandResult(1, "masked\n")})
    assert c.check_serial_getty_masked().status == "pass"


def test_stuck_bridge_br0(tmp_path: Path) -> None:
    out = "cherry:wlp2s0:activated\nBridge br0:br0:activating\n"
    c = checker(tmp_path, {("nmcli", "-t", "-f", "NAME,DEVICE,STATE", "connection", "show", "--active"): CommandResult(0, out)})
    assert c.check_bridge().status == "fail"


def test_selinux_context_parsing_and_check(tmp_path: Path) -> None:
    assert boot_health.parse_selinux_type("system_u:object_r:bin_t:s0 /x") == "bin_t"
    c = checker(tmp_path, {("ls", "-Z", str(cfg(tmp_path).health_script)): CommandResult(0, "system_u:object_r:user_home_t:s0 /x")})
    assert c.check_health_script_selinux().status == "fail"


def test_disk_and_memory_thresholds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(boot_health.shutil, "disk_usage", lambda _p: SimpleNamespace(free=200 * 1024 * 1024))
    monkeypatch.setattr(boot_health, "available_memory_mb", lambda: 128)
    c = checker(tmp_path, disk_min_free_mb=1024, memory_min_free_mb=512)
    assert c.check_disk().status == "fail"
    assert c.check_memory().status == "fail"


def test_fresh_and_stale_heartbeat(tmp_path: Path) -> None:
    conf = cfg(tmp_path, heartbeat_max_age_minutes=10)
    hb = conf.project_root / "data" / "heartbeat" / "live.json"
    hb.parent.mkdir()
    hb.write_text("{}", encoding="utf-8")
    os.utime(hb, (datetime(2026, 7, 21, 13, 55, tzinfo=timezone.utc).timestamp(),) * 2)
    c = BootHealthChecker(conf, probe=FakeProbe(), now=lambda: datetime(2026, 7, 21, 14, 0, tzinfo=timezone.utc))
    c.market_open = True
    assert c.check_heartbeat().status == "pass"
    os.utime(hb, (datetime(2026, 7, 21, 13, 0, tzinfo=timezone.utc).timestamp(),) * 2)
    assert c.check_heartbeat().status == "fail"


def test_market_closed_heartbeat_behavior(tmp_path: Path) -> None:
    c = BootHealthChecker(cfg(tmp_path), probe=FakeProbe(), now=lambda: datetime(2026, 7, 19, 14, 0, tzinfo=timezone.utc))
    c.market_open = False
    assert c.check_heartbeat().status == "pass"


def test_json_output_schema_and_atomic_report_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c = checker(tmp_path)
    report = c._build_report([boot_health.CheckResult("timezone", "pass", "America/New_York")], True)
    c.write_reports(report)
    data = json.loads((c.config.report_dir / "latest.json").read_text(encoding="utf-8"))
    assert {"timestamp_utc", "timestamp_new_york", "hostname", "boot_id", "uptime", "git_commit", "environment", "checks", "ready"} <= set(data)
    assert (c.config.report_dir / "latest.md").read_text(encoding="utf-8").startswith("# AlgoSphere Boot Health")
    assert not list(c.config.report_dir.glob(".latest.*"))


def test_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    class ReadyChecker:
        def run_all(self, *, repair: bool = False):
            return {"ready": True, "checks": [], "result": "READY"}

    class FailedChecker:
        def run_all(self, *, repair: bool = False):
            return {"ready": False, "checks": [], "result": "NOT READY"}

    monkeypatch.setattr(boot_health, "BootHealthChecker", lambda: ReadyChecker())
    assert boot_health.main(["--quiet"]) == 0
    monkeypatch.setattr(boot_health, "BootHealthChecker", lambda: FailedChecker())
    assert boot_health.main(["--quiet"]) == 1
    monkeypatch.setattr(boot_health, "BootHealthChecker", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert boot_health.main(["--quiet"]) == 2


def test_repair_mode_action_boundaries(tmp_path: Path) -> None:
    conf = cfg(tmp_path)
    responses = {
        ("systemctl", "is-active", "algo.service"): CommandResult(3, "inactive\n"),
        ("systemctl", "is-enabled", "algo.service"): CommandResult(0, "enabled\n"),
        ("systemctl", "start", "algo.service"): CommandResult(0, ""),
    }

    class StartThenActive(FakeProbe):
        def run(self, args, *, timeout: float = 10.0) -> CommandResult:
            key = tuple(str(a) for a in args)
            self.calls.append(key)
            if key == ("systemctl", "is-active", "algo.service") and ("systemctl", "start", "algo.service") in self.calls:
                return CommandResult(0, "active\n")
            return responses.get(key, CommandResult(0, ""))

    probe = StartThenActive()
    c = BootHealthChecker(conf, probe=probe)
    assert c.check_unit_active("algo_service_active", "algo.service", repair=True).status == "pass"
    assert ("systemctl", "start", "algo.service") in probe.calls
    assert all(call[:2] != ("systemctl", "restart") for call in probe.calls)


def test_alpaca_and_market_clock_use_existing_broker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    @dataclass
    class Clock:
        is_open: bool = True

    class Trading:
        def get_clock(self):
            return Clock(True)

        def get_calendar(self):
            return [object()]

    class Broker:
        _trading = Trading()

        def get_account_snapshot(self):
            return {"equity": 1000}

    c = checker(tmp_path)
    monkeypatch.setattr(c, "_make_broker", lambda: Broker())
    assert c.check_alpaca_auth().status == "pass"
    assert c.check_market_clock().status == "pass"
    assert c.market_open is True


def test_run_all_ready_report_covers_success_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conf = cfg(tmp_path, disk_min_free_mb=1, memory_min_free_mb=1)
    responses = {
        ("timedatectl", "show", "-p", "Timezone", "--value"): CommandResult(0, "America/New_York\n"),
        ("timedatectl", "show", "-p", "SystemClockSynchronized", "--value"): CommandResult(0, "true\n"),
        ("nmcli", "-t", "-f", "CONNECTIVITY", "general"): CommandResult(0, "full\n"),
        ("ip", "route", "show", "default"): CommandResult(0, "default via 192.168.1.1 dev wlan0\n"),
        ("getent", "hosts", "api.alpaca.markets"): CommandResult(0, "1.2.3.4 api.alpaca.markets\n"),
        ("systemctl", "is-enabled", "algo.service"): CommandResult(0, "enabled\n"),
        ("systemctl", "is-active", "algo.service"): CommandResult(0, "active\n"),
        ("systemctl", "is-enabled", "algo-health-check.timer"): CommandResult(0, "enabled\n"),
        ("systemctl", "is-active", "algo-health-check.timer"): CommandResult(0, "active\n"),
        ("systemctl", "--failed", "--plain", "--no-legend"): CommandResult(0, "\n"),
        ("nmcli", "-t", "-f", "NAME,DEVICE,STATE", "connection", "show", "--active"): CommandResult(0, "cherry:wlan0:activated\n"),
        ("systemctl", "is-enabled", "serial-getty@ttyS0.service"): CommandResult(1, "masked\n"),
        ("ls", "-Z", str(conf.health_script)): CommandResult(0, "system_u:object_r:shell_exec_t:s0 script\n"),
        ("journalctl", "-u", "algo.service", "--since", "45 minutes ago", "--no-pager"): CommandResult(0, "INFO LIVE_LOOP_SLEEP seconds=60\n"),
        ("systemctl", "show", "algo.service", "-p", "NRestarts", "--value"): CommandResult(0, "2\n"),
        ("systemctl", "list-timers", "algo-health-check.timer", "--no-legend", "--all"): CommandResult(0, "Tue 2026-07-21 10:00:00 EDT algo-health-check.timer\n"),
        ("git", "-c", f"safe.directory={conf.project_root}", "-C", str(conf.project_root), "rev-parse", "HEAD"): CommandResult(0, "abc123\n"),
    }

    class Trading:
        def get_clock(self):
            return SimpleNamespace(is_open=True)

        def get_calendar(self):
            return [object()]

    class Broker:
        _trading = Trading()

        def get_account_snapshot(self):
            return {"equity": 1000}

    monkeypatch.setattr(boot_health, "available_memory_mb", lambda: 2048)
    monkeypatch.setattr(boot_health.shutil, "disk_usage", lambda _p: SimpleNamespace(free=4096 * 1024 * 1024))
    c = BootHealthChecker(conf, probe=FakeProbe(responses), now=lambda: datetime(2026, 7, 21, 14, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(c, "_make_broker", lambda: Broker())

    report = c.run_all()
    assert report["ready"] is True
    assert report["result"] == "READY"
    assert "PASS `timezone`" in (conf.report_dir / "latest.md").read_text(encoding="utf-8")
    assert "abc123" == report["git_commit"]


def test_failed_units_allowlist_and_reset_repair(tmp_path: Path) -> None:
    conf = cfg(tmp_path, failed_unit_allowlist=("allowed.service",))
    responses = {
        ("systemctl", "--failed", "--plain", "--no-legend"): CommandResult(
            0,
            "allowed.service loaded failed failed harmless\nstale.service loaded failed failed stale\n",
        ),
        ("systemctl", "is-active", "stale.service"): CommandResult(0, "active\n"),
        ("systemctl", "reset-failed", "stale.service"): CommandResult(0, ""),
    }
    c = BootHealthChecker(conf, probe=FakeProbe(responses))
    result = c.check_failed_units(repair=True)
    assert result.status == "pass"
    assert c.repairs == [{"action": "reset_failed", "unit": "stale.service", "returncode": 0}]


def test_selinux_restorecon_repair_requires_persistent_rule(tmp_path: Path) -> None:
    conf = cfg(tmp_path)

    class RestoreProbe(FakeProbe):
        def run(self, args, *, timeout: float = 10.0) -> CommandResult:
            key = tuple(str(a) for a in args)
            self.calls.append(key)
            if key == ("ls", "-Z", str(conf.health_script)) and ("restorecon", "-v", str(conf.health_script)) in self.calls:
                return CommandResult(0, "system_u:object_r:bin_t:s0 script\n")
            if key == ("ls", "-Z", str(conf.health_script)):
                return CommandResult(0, "system_u:object_r:user_home_t:s0 script\n")
            if key == ("semanage", "fcontext", "-l"):
                return CommandResult(0, f"{conf.health_script} all files system_u:object_r:bin_t:s0\n")
            if key == ("restorecon", "-v", str(conf.health_script)):
                return CommandResult(0, "")
            return CommandResult(0, "")

    c = BootHealthChecker(conf, probe=RestoreProbe())
    assert c.check_health_script_selinux(repair=True).status == "pass"
    assert c.repairs[0]["action"] == "restorecon"


def test_project_and_health_script_missing_paths(tmp_path: Path) -> None:
    conf = cfg(tmp_path)
    conf.health_script.unlink()
    c = BootHealthChecker(conf, probe=FakeProbe())
    assert c.check_health_script().status == "fail"
    assert c.check_project_paths().status == "fail"


def test_restart_loop_timer_and_journal_heartbeat_failures(tmp_path: Path) -> None:
    c = checker(
        tmp_path,
        {
            ("systemctl", "show", "algo.service", "-p", "NRestarts", "--value"): CommandResult(0, "9\n"),
            ("systemctl", "list-timers", "algo-health-check.timer", "--no-legend", "--all"): CommandResult(0, "n/a n/a n/a\n"),
            ("journalctl", "-u", "algo.service", "--since", "45 minutes ago", "--no-pager"): CommandResult(0, "quiet\n"),
        },
        restart_loop_threshold=3,
    )
    c.market_open = True
    assert c.check_restart_loop().status == "fail"
    assert c.check_timer_next_trigger().status == "fail"
    assert c.check_heartbeat().status == "fail"


def test_main_json_prints_report(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    class ReadyChecker:
        def run_all(self, *, repair: bool = False):
            return {"ready": True, "checks": [], "result": "READY", "repairs": []}

    monkeypatch.setattr(boot_health, "BootHealthChecker", lambda: ReadyChecker())
    assert boot_health.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["result"] == "READY"


def test_system_probe_handles_missing_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*_args, **_kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(boot_health.subprocess, "run", missing)
    assert boot_health.SystemProbe().run(["missing"]).returncode == 127

    def timeout(*_args, **_kwargs):
        raise boot_health.subprocess.TimeoutExpired(["slow"], 1, output="out", stderr="err")

    monkeypatch.setattr(boot_health.subprocess, "run", timeout)
    result = boot_health.SystemProbe().run(["slow"])
    assert result.returncode == 124
    assert result.stdout == "out"
