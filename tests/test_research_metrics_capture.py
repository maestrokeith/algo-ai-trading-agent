from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from scripts import capture_research_metrics as crm


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, args: list[str] | tuple[str, ...], *, timeout: int = 20) -> crm.CommandResult:
        key = tuple(args)
        self.calls.append(key)
        if key[:3] == ("git", "rev-parse", "--abbrev-ref"):
            return crm.CommandResult(0, "main\n", "")
        if key[:3] == ("git", "rev-parse", "--short"):
            return crm.CommandResult(0, "abc1234\n", "")
        if key[:2] == ("systemctl", "is-active"):
            return crm.CommandResult(0, "active\n", "")
        if "summary" in key:
            return crm.CommandResult(0, "Equity: 100000\nBuying Power: 50000\n", "")
        if "positions" in key:
            return crm.CommandResult(0, "Open positions: 1\nBLZE 10\n", "")
        if "show_open_orders.py" in " ".join(key):
            return crm.CommandResult(0, "symbol\tside\tqty\tstatus\tsubmitted_at\n", "")
        return crm.CommandResult(0, "", "")


def sample_logs() -> str:
    return "\n".join(
        [
            "INFO DYNAMIC_SCAN selected=['BLZE', 'SNDQ']",
            "INFO DYNAMIC_SCAN reject symbol=LOW reason=price_filter",
            "INFO BLZE ENTRY_EVAL route=dynamic_momentum_override final=T reason=ok",
            "INFO ENTRY_EVAL_PASS symbol=BLZE route=dynamic_momentum_override reason=ok",
            "INFO ENTRY_TO_ALLOCATOR_TRACE symbol=BLZE route=dynamic_momentum_override",
            "INFO ALLOCATOR_ACTION_SUBMITTED symbol=BLZE action=buy",
            "INFO ORDER_SUBMITTED symbol=BLZE side=buy notional=1000 source=capital_allocator order_id=o1 status=accepted",
            "INFO ORDER_STATUS symbol=BLZE order_id=o1 status=filled",
            "INFO ENTRY_TO_ALLOCATOR symbol=XYZ route=dynamic_momentum_override reject_reasons=no_catalyst",
            "INFO SKIP symbol=WIDE reason=spread too wide",
            "INFO SKIP symbol=ATRX reason=atr range too narrow",
            "INFO SKIP symbol=PENNY reason=price below min",
            "INFO allocator reject symbol=NOQ reason=no_quote",
            "INFO allocator reject symbol=NOQ reason=blocked_after_no_quote",
            "ERROR APCA_API_SECRET_KEY=supersecret failed to fetch optional metric",
        ]
    )


def build_report(tmp_path: Path, *, phase: str = "begin_day", env: str = "live") -> dict:
    runner = FakeRunner()
    context = crm.collect_context(
        runner,
        env=env,
        user=crm.default_user(env),
        service=crm.default_service(env),
        report_date="2026-06-23",
        dry_run=False,
    )
    return crm.build_report(
        phase=phase,
        env=env,
        report_date="2026-06-23",
        user=crm.default_user(env),
        service=crm.default_service(env),
        since="2026-06-23 09:25:00",
        logs=sample_logs(),
        context=context,
        dry_run=False,
    )


def test_research_metrics_writes_json_and_markdown(tmp_path: Path) -> None:
    report = build_report(tmp_path)

    json_path, md_path = crm.write_report(report, data_dir=tmp_path)

    assert json_path == tmp_path / "research_metrics" / "2026-06-23" / "begin_day_live.json"
    assert md_path == tmp_path / "research_metrics" / "2026-06-23" / "begin_day_live.md"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    text = md_path.read_text(encoding="utf-8")
    assert payload["environment"] == "LIVE"
    assert payload["summary"]["dynamic_selected_count"] == 2
    assert payload["portfolio_metrics"]["equity"] == 100000.0
    assert payload["portfolio_metrics"]["buying_power"] == 50000.0
    assert "## Dynamic Scanner" in text
    assert "## Research Notes / Anomalies" in text


def test_research_metrics_live_paper_and_begin_end_paths_distinct(tmp_path: Path) -> None:
    begin_live = build_report(tmp_path, phase="begin_day", env="live")
    end_live = build_report(tmp_path, phase="end_day", env="live")
    begin_paper = build_report(tmp_path, phase="begin_day", env="paper")

    begin_live_json, _ = crm.write_report(begin_live, data_dir=tmp_path)
    end_live_json, _ = crm.write_report(end_live, data_dir=tmp_path)
    begin_paper_json, _ = crm.write_report(begin_paper, data_dir=tmp_path)

    assert begin_live_json.name == "begin_day_live.json"
    assert end_live_json.name == "end_day_live.json"
    assert begin_paper_json.name == "begin_day_paper.json"
    assert json.loads(begin_paper_json.read_text(encoding="utf-8"))["environment"] == "PAPER"


def test_research_metrics_log_parsing_marks_recent_missing_flow_as_pending() -> None:
    logs = "\n".join(
        [
            "INFO DYNAMIC_SCAN selected=['BLZE', 'SOXS']",
            "INFO DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=BLZE source=scanner_selected",
            "INFO DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=SOXS source=scanner_selected",
        ]
    )

    parsed = crm.parse_log_metrics(logs, env="live", since="30 minutes ago")

    assert parsed["missing_flow_diagnostics"] == []
    assert parsed.get("dynamic_entry_eval_status") in {"pending", "observed", None}


def test_research_metrics_redacts_secrets() -> None:
    parsed = crm.parse_log_metrics("ERROR APCA_API_SECRET_KEY=supersecret Traceback\n", env="live", since="30 minutes ago")

    assert parsed["exceptions"]
    assert "supersecret" not in parsed["exceptions"][0]
    assert "APCA_API_SECRET_KEY=[redacted]" in parsed["exceptions"][0]


def test_research_metrics_dry_run_cli_does_not_write_files(tmp_path: Path) -> None:
    log_path = tmp_path / "algo.log"
    log_path.write_text(sample_logs(), encoding="utf-8")
    env = {**os.environ, "ALGO_REPO_ROOT": str(PROJECT_ROOT)}

    proc = subprocess.run(
        [
            str(PROJECT_ROOT / "bin" / "algo"),
            "capture-metrics",
            "--end-day",
            "--live",
            "--dry-run",
            "--date",
            "2026-06-23",
            "--data-dir",
            str(tmp_path),
            "--log-file",
            str(log_path),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0
    assert "DRY_RUN would write" in proc.stdout
    assert not (tmp_path / "research_metrics" / "2026-06-23" / "end_day_live.json").exists()


def test_research_metrics_systemd_units_are_wired() -> None:
    begin_timer = (PROJECT_ROOT / "deploy" / "systemd" / "algosphere-ops-research-metrics-begin.timer").read_text(
        encoding="utf-8"
    )
    end_timer = (PROJECT_ROOT / "deploy" / "systemd" / "algosphere-ops-research-metrics-end.timer").read_text(
        encoding="utf-8"
    )
    begin_service = (PROJECT_ROOT / "deploy" / "systemd" / "algosphere-ops-research-metrics-begin.service").read_text(
        encoding="utf-8"
    )
    installer = (PROJECT_ROOT / "scripts" / "install_ops_timers.sh").read_text(encoding="utf-8")

    assert "OnCalendar=Mon..Fri 09:25:00" in begin_timer
    assert "OnCalendar=Mon..Fri 16:05:00" in end_timer
    assert "capture-metrics --begin-day --live" in begin_service
    assert "algosphere-ops-research-metrics-begin.timer" in installer
    assert "algosphere-ops-research-metrics-end.timer" in installer
