from __future__ import annotations

import subprocess
import sys
import os
from datetime import datetime
from pathlib import Path

from scripts import run_ops_workflow

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_premarket_systemd_service_loads_shared_env_file() -> None:
    unit = (PROJECT_ROOT / "deploy" / "systemd" / "algosphere-premarket.service").read_text(encoding="utf-8")

    assert "Type=oneshot" in unit
    assert "User=algosphere" in unit
    assert "Group=algosphere" in unit
    assert "NoNewPrivileges=true" in unit
    assert "EnvironmentFile=/etc/algo.env" in unit


def test_parse_report_date_uses_new_york_calendar_date() -> None:
    now = datetime.fromisoformat("2026-06-08T01:30:00+00:00")

    assert run_ops_workflow.parse_report_date("today", now=now) == "2026-06-07"


def test_daily_summary_command_generation_uses_resolved_date_and_user(tmp_path: Path) -> None:
    paths = run_ops_workflow.build_ops_paths(project_root=tmp_path, report_date="2026-06-07")

    specs = run_ops_workflow.build_command_specs(job="daily-summary", user="live_bot", paths=paths)

    assert len(specs) == 1
    spec = specs[0]
    assert spec.argv == (str(tmp_path / "bin" / "algo"), "summary", "2026-06-07", "--user", "live_bot")
    assert spec.output_path == tmp_path / "reports" / "daily" / "2026-06-07" / "daily_summary.txt"
    assert spec.log_path == tmp_path / "data" / "logs" / "ops_daily_summary_2026-06-07.log"


def test_postmarket_command_generation_includes_catalyst_and_profitability(tmp_path: Path) -> None:
    paths = run_ops_workflow.build_ops_paths(project_root=tmp_path, report_date="2026-06-07")

    specs = run_ops_workflow.build_command_specs(job="postmarket-analytics", user="live_bot", paths=paths)

    assert [spec.name for spec in specs] == ["catalyst_stats", "profitability_attribution"]
    assert specs[0].argv[-1].endswith("show_catalyst_stats.py")
    assert "--date" in specs[1].argv
    assert "2026-06-07" in specs[1].argv
    assert specs[0].output_path.name == "catalyst_stats.txt"
    assert specs[1].output_path.name == "profitability_attribution.txt"


def test_research_feedback_command_generation(tmp_path: Path) -> None:
    paths = run_ops_workflow.build_ops_paths(project_root=tmp_path, report_date="2026-06-07")

    specs = run_ops_workflow.build_command_specs(job="research-feedback", user="live_bot", paths=paths)
    weekly = run_ops_workflow.build_command_specs(job="weekly-research-feedback", user="live_bot", paths=paths)

    assert specs[0].argv[-5].endswith("generate_research_feedback.py")
    assert specs[0].argv[-4:] == ("--date", "2026-06-07", "--user", "live_bot")
    assert specs[0].output_path.name == "research_feedback.txt"
    assert weekly[0].argv[-1] == "--weekly"
    assert weekly[0].output_path.name == "weekly_research_feedback.txt"


def test_run_command_persists_stdout_and_stderr(tmp_path: Path) -> None:
    paths = run_ops_workflow.build_ops_paths(project_root=tmp_path, report_date="2026-06-07")
    spec = run_ops_workflow.CommandSpec(
        name="example",
        argv=(sys.executable, "-c", "import sys; print('ok'); print('warn', file=sys.stderr)"),
        output_path=paths.reports_dir / "example.txt",
        log_path=paths.logs_dir / "example.log",
    )

    assert run_ops_workflow.run_command(spec, cwd=tmp_path) == 0

    assert "ok" in spec.output_path.read_text(encoding="utf-8")
    assert "warn" in spec.log_path.read_text(encoding="utf-8")


def test_startup_validation_accepts_fresh_premarket_artifact_log(tmp_path: Path) -> None:
    paths = run_ops_workflow.build_ops_paths(project_root=tmp_path, report_date="2026-06-07")
    log_path = tmp_path / "data" / "logs" / "algo.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        "INFO PREMARKET_STARTUP_ARTIFACTS status=fresh present=true fresh=true "
        "missing=none stale=none catalyst_ranked_symbols=12 rankings=12 catalysts=6 events=9\n",
        encoding="utf-8",
    )

    assert run_ops_workflow.validate_startup_logs(project_root=tmp_path, paths=paths, algo_logs=[]) == 0

    report = (paths.reports_dir / "startup_validation.txt").read_text(encoding="utf-8")
    assert "catalyst_ranked_symbols=12" in report
    assert "startup_validation=ok" in report


def test_startup_validation_fails_when_log_line_missing(tmp_path: Path) -> None:
    paths = run_ops_workflow.build_ops_paths(project_root=tmp_path, report_date="2026-06-07")

    assert run_ops_workflow.validate_startup_logs(project_root=tmp_path, paths=paths, algo_logs=[]) == 1

    report = (paths.reports_dir / "startup_validation.txt").read_text(encoding="utf-8")
    assert "PREMARKET_STARTUP_ARTIFACTS not found" in report


def test_install_ops_timers_dry_run_lists_default_and_optional_timers(tmp_path: Path) -> None:
    proc = subprocess.run(
        ["bash", str(PROJECT_ROOT / "scripts" / "install_ops_timers.sh"), "--dry-run", "--enable-replay"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "SYSTEMD_DIR": str(tmp_path), "SUDO": "sudo"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "algosphere-premarket.timer" in proc.stdout
    assert "algosphere-ops-daily-summary.timer" in proc.stdout
    assert "algosphere-ops-research-feedback.timer" in proc.stdout
    assert "algosphere-ops-weekly-research-feedback.timer" in proc.stdout
    assert "algosphere-ops-replay-summary.timer" in proc.stdout


def test_install_node_dry_run_generates_systemd_install_and_timer_enablement(tmp_path: Path) -> None:
    proc = subprocess.run(
        [
            "bash",
            str(PROJECT_ROOT / "scripts" / "install_node.sh"),
            "--dry-run",
            "--skip-tests",
            "--enable-replay",
            "--user",
            "live_bot",
        ],
        cwd=PROJECT_ROOT,
        env={
            **os.environ,
            "SYSTEMD_DIR": str(tmp_path),
            "SUDO": "sudo",
            "ALPACA_LIVE_API_KEY_ID": "dummy",
            "ALPACA_LIVE_API_SECRET_KEY": "dummy",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "sudo install -m 0644" in proc.stdout
    assert str(tmp_path) in proc.stdout
    assert "sudo systemctl daemon-reload" in proc.stdout
    assert "sudo systemctl enable --now" in proc.stdout
    assert "algosphere-premarket.timer" in proc.stdout
    assert "algosphere-ops-postmarket-analytics.timer" in proc.stdout
    assert "algosphere-ops-research-feedback.timer" in proc.stdout
    assert "algosphere-ops-weekly-research-feedback.timer" in proc.stdout
    assert "algosphere-ops-replay-summary.timer" in proc.stdout
    assert "systemctl list-timers algosphere\\*" in proc.stdout


def test_install_node_validation_rejects_unknown_user() -> None:
    proc = subprocess.run(
        [
            "bash",
            str(PROJECT_ROOT / "scripts" / "install_node.sh"),
            "--dry-run",
            "--skip-tests",
            "--user",
            "missing_user",
        ],
        cwd=PROJECT_ROOT,
        env={
            **os.environ,
            "ALPACA_LIVE_API_KEY_ID": "dummy",
            "ALPACA_LIVE_API_SECRET_KEY": "dummy",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 1
    assert "config/users.yaml does not define user 'missing_user'" in proc.stderr
