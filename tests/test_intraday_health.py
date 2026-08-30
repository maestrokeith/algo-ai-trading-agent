from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.intraday_health import (
    CommandResult,
    build_intraday_health_report,
    intraday_health_output_path,
    save_intraday_health_report,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_systemd_units_are_readonly_and_use_intraday_health_command() -> None:
    service = (PROJECT_ROOT / "systemd" / "intraday-health.service").read_text(encoding="utf-8")
    timer = (PROJECT_ROOT / "systemd" / "intraday-health.timer").read_text(encoding="utf-8")

    assert "WorkingDirectory=/opt/algosphere/algo-ai-trading-agent" in service
    assert "Environment=PYTHONPATH=/opt/algosphere/algo-ai-trading-agent" in service
    assert (
        'ExecStart=/opt/algosphere/algo-ai-trading-agent/bin/algo intraday-health --live --since "30 min ago" --json'
        in service
    )
    combined = f"{service}\n{timer}".lower()
    assert "restart algo.service" not in combined
    assert "systemctl restart" not in combined
    assert "Unit=intraday-health.service" in timer
    assert "Persistent=true" in timer
    assert "OnCalendar=*:0/10" in timer


def test_install_script_references_intraday_health_timer_only() -> None:
    text = (PROJECT_ROOT / "scripts" / "install_intraday_health_timer.sh").read_text(encoding="utf-8")
    assert "intraday-health.timer" in text
    assert "intraday-health.service" in text
    assert "systemctl enable --now intraday-health.timer" in text
    assert "systemctl restart" not in text
    assert "algo.service" not in text


def test_intraday_health_json_path_is_dated_by_environment(tmp_path: Path) -> None:
    now = datetime(2026, 6, 27, 10, 30, tzinfo=ZoneInfo("America/New_York"))
    path = intraday_health_output_path(tmp_path, env_name="live", now=now)
    assert path == tmp_path / "data" / "intraday_health" / "2026-06-27" / "live_intraday_health.json"


def test_agent_writes_report_to_expected_path(tmp_path: Path) -> None:
    now = datetime(2026, 6, 27, 10, 30, tzinfo=ZoneInfo("America/New_York"))
    report = {
        "generated_at": now.isoformat(),
        "env": "live",
        "status": "healthy",
        "recommendations": ["none"],
    }

    path = save_intraday_health_report(report, tmp_path)

    assert path == tmp_path / "data" / "intraday_health" / "2026-06-27" / "live_intraday_health.json"
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "healthy"


def test_agent_handles_command_failures_gracefully(tmp_path: Path) -> None:
    def runner(command, cwd, env):
        if command[0] == "journalctl":
            return CommandResult(1, "", "journal unavailable")
        if command[0] == "systemctl":
            return CommandResult(0, "active\n", "")
        return CommandResult(2, "", "positions unavailable")

    report = build_intraday_health_report(
        root=tmp_path,
        env_name="live",
        since="30 min ago",
        runner=runner,
    )

    assert report["status"] == "degraded"
    assert report["readonly"] is True
    assert {row["name"] for row in report["errors"]} == {"journal", "positions"}
    assert "inspect_recent_journal" in report["recommendations"]


def test_no_options_lane_logs_recommends_inspect_options_lane(tmp_path: Path) -> None:
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "default.yaml").write_text(
        """
options:
  enabled: true
  mode: live_long_premium
  live_pilot_enabled: true
  live_pilot:
    enabled: true
""",
        encoding="utf-8",
    )

    def runner(command, cwd, env):
        if command[0] == "journalctl":
            return CommandResult(0, "ENTRY_EVAL symbol=QQQ\n", "")
        return CommandResult(0, "active\n", "")

    report = build_intraday_health_report(
        root=tmp_path,
        env_name="live",
        since="30 min ago",
        now=datetime(2026, 6, 29, 10, 30, tzinfo=ZoneInfo("America/New_York")),
        runner=runner,
    )

    assert "inspect_options_lane" in report["recommendations"]
    assert report["options_pilot"]["enabled"] is True
