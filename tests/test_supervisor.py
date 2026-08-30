from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.supervisor import AlgoSupervisor, SupervisorContext, SupervisorMCPServer


class Broker:
    def get_account_snapshot(self) -> dict:
        return {"equity": 100_000, "last_equity": 99_000, "peak_equity": 101_000}

    def get_positions(self) -> list[dict]:
        return [{"symbol": "SPY", "market_value": 10_000}]

    def get_open_orders(self) -> list[dict]:
        return [{"id": "o1", "symbol": "SPY", "side": "buy", "qty": 1}]

    def get_clock(self) -> object:
        return object()


def _supervisor(tmp_path: Path, restart_callback=None) -> AlgoSupervisor:
    context = SupervisorContext(
        config={
            "broker": {"paper": True},
            "strategy": {"enabled": True},
            "portfolio_risk": {"max_daily_loss_pct": 2},
            "market_regime": {"enabled": True},
            "global_risk": {"max_symbol_exposure_pct": 0.50},
        },
        broker=Broker(),
        trades=[
            {"symbol": "AAPL", "pnl": -12.5, "strategy": "dynamic", "return_pct": -1.0},
            {"symbol": "MSFT", "pnl": 25.0, "strategy": "core", "return_pct": 2.0},
        ],
        data_dir=tmp_path,
        reports_dir=tmp_path,
        logs_dir=tmp_path / "logs",
        incidents_dir=tmp_path / "incidents",
        restart_callback=restart_callback,
    )
    return AlgoSupervisor(context, user_id="u1")


def test_supervisor_exposes_required_tools(tmp_path) -> None:
    server = SupervisorMCPServer(_supervisor(tmp_path))

    assert server.list_tools() == [
        "change_risk_limits",
        "deploy_code",
        "enable_live_trading",
        "explain_last_trade",
        "get_account_status",
        "get_health_status",
        "get_last_trade",
        "get_latest_daily_report",
        "get_latest_premarket_report",
        "get_latest_reports",
        "get_open_incidents",
        "get_open_orders",
        "get_positions",
        "get_recent_errors",
        "get_recent_logs",
        "get_risk_status",
        "get_status",
        "get_supervisor_summary",
        "get_today_pnl",
        "pause_trading",
        "push_main_branch",
        "restart_algo",
        "resume_paper_mode",
        "run_incident_response",
        "run_preflight",
    ]
    assert server.call_tool("get_account_status")["equity"] == 100_000
    assert server.call_tool("get_status")["user_id"] == "u1"
    assert server.call_tool("get_supervisor_summary")["user_id"] == "u1"


def test_supervisor_status_methods(tmp_path) -> None:
    supervisor = _supervisor(tmp_path)

    assert supervisor.get_positions()[0]["symbol"] == "SPY"
    assert supervisor.get_open_orders()[0]["id"] == "o1"
    assert supervisor.get_today_pnl()["realized_pnl"] == 12.5
    assert supervisor.get_health_status()["ok"] is True
    assert supervisor.get_risk_status()["allowed"] is True
    assert supervisor.get_last_trade()["symbol"] == "MSFT"
    assert supervisor.explain_last_trade()["symbol"] == "MSFT"


def test_supervisor_get_status_compact_read_only_summary(tmp_path) -> None:
    supervisor = _supervisor(tmp_path)

    status = supervisor.get_status()

    assert status["available"] is True
    assert status["user_id"] == "u1"
    assert status["ok"] is True
    assert status["paper"] is True
    assert status["broker_available"] is True
    assert status["risk_allowed"] is True
    assert status["equity"] == 100_000
    assert status["positions_count"] == 1
    assert status["open_orders_count"] == 1
    assert status["today_pnl"]["realized_pnl"] == 12.5
    assert status["health"]["ok"] is True
    assert "timestamp" in status


def test_supervisor_summary_healthy_service_no_errors(tmp_path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "algo.log").write_text(
        "\n".join(
            [
                "2026-06-06T14:50:00+00:00 INFO component=service boot ok",
                "2026-06-06T14:55:00+00:00 WARNING component=broker reconnect slow",
            ]
        ),
        encoding="utf-8",
    )
    start_ts = datetime(2026, 6, 6, 14, 0, tzinfo=timezone.utc).timestamp()
    supervisor = _supervisor(tmp_path)
    supervisor.context.process_start_ts = start_ts

    summary = supervisor.get_supervisor_summary(
        since_minutes=30,
        now=datetime(2026, 6, 6, 15, 0, tzinfo=timezone.utc),
    )

    assert summary["available"] is True
    assert summary["ok"] is True
    assert summary["service_status"]["ok"] is True
    assert summary["service_status"]["service"] == "algo.service"
    assert summary["recent_errors"]["count"] == 0
    assert summary["recent_errors"]["available"] is False
    assert summary["recent_warning_count"] == 1
    assert summary["log_availability"]["available"] is True
    assert summary["log_availability"]["sources"] == ["file"]
    assert summary["uptime_seconds"] == 3600.0


def test_supervisor_summary_unavailable_service_handles_missing_logs(tmp_path) -> None:
    def _provider(_service: str, _max_lines: int) -> list[str]:
        raise RuntimeError("journal unavailable")

    supervisor = AlgoSupervisor(
        SupervisorContext(
            config={"broker": {"paper": True}},
            broker=None,
            broker_error="credentials missing",
            data_dir=tmp_path,
            logs_dir=tmp_path / "missing-logs",
            log_service_name="missing.service",
            log_provider=_provider,
        ),
        user_id="u1",
    )

    summary = supervisor.get_supervisor_summary(
        since_minutes=30,
        now=datetime(2026, 6, 6, 15, 0, tzinfo=timezone.utc),
    )

    assert summary["available"] is True
    assert summary["ok"] is False
    assert summary["service_status"]["ok"] is False
    assert summary["service_status"]["broker_available"] is False
    assert summary["log_availability"]["available"] is False
    assert summary["log_availability"]["reason"] == "logs_unavailable"
    assert "journal unavailable" in summary["log_availability"]["error"]
    assert summary["recent_errors"]["count"] == 0
    assert summary["recent_warning_count"] == 0


def test_supervisor_summary_recent_errors_present(tmp_path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "algo.log").write_text(
        "\n".join(
            [
                "2026-06-06T14:45:00+00:00 WARNING component=broker slow quote",
                "2026-06-06T14:55:00+00:00 ERROR component=broker order rejected",
                "2026-06-06T14:56:00+00:00 TypeError: bad operand",
            ]
        ),
        encoding="utf-8",
    )
    supervisor = _supervisor(tmp_path)

    summary = supervisor.get_supervisor_summary(
        since_minutes=30,
        now=datetime(2026, 6, 6, 15, 0, tzinfo=timezone.utc),
    )

    assert summary["ok"] is False
    assert summary["recent_errors"]["available"] is True
    assert summary["recent_errors"]["count"] == 2
    assert summary["recent_errors"]["sources"] == ["file"]
    assert summary["recent_errors"]["sample"] == [
        "2026-06-06T14:55:00+00:00 ERROR component=broker order rejected",
        "2026-06-06T14:56:00+00:00 TypeError: bad operand",
    ]
    assert summary["recent_warning_count"] == 1


def test_supervisor_summary_missing_log_provider_is_stable(tmp_path) -> None:
    def _provider(_service: str, _max_lines: int) -> list[str]:
        raise RuntimeError("journal unavailable")

    supervisor = AlgoSupervisor(
        SupervisorContext(
            config={"broker": {"paper": True}},
            broker=Broker(),
            data_dir=tmp_path,
            logs_dir=tmp_path / "missing-logs",
            log_provider=_provider,
        ),
        user_id="u1",
    )

    summary = supervisor.get_supervisor_summary(
        since_minutes=30,
        now=datetime(2026, 6, 6, 15, 0, tzinfo=timezone.utc),
    )

    assert summary["available"] is True
    assert summary["service_status"]["ok"] is True
    assert summary["log_availability"]["available"] is False
    assert summary["recent_errors"]["available"] is False
    assert summary["recent_errors"]["count"] == 0


def test_supervisor_summary_malformed_log_entries_do_not_crash(tmp_path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "algo.log").write_text(
        "\n".join(
            [
                "{not-json WARNING component=broker",
                "ERROR component=broker missing timestamp",
                '{"timestamp":"2026-06-06T14:55:00+00:00","level":"ERROR","component":"broker","message":"broker failure"}',
            ]
        ),
        encoding="utf-8",
    )
    supervisor = _supervisor(tmp_path)

    summary = supervisor.get_supervisor_summary(
        since_minutes=30,
        now=datetime(2026, 6, 6, 15, 0, tzinfo=timezone.utc),
    )

    assert summary["available"] is True
    assert summary["recent_errors"]["count"] == 1
    assert summary["recent_errors"]["sample"] == [
        '{"timestamp":"2026-06-06T14:55:00+00:00","level":"ERROR","component":"broker","message":"broker failure"}',
    ]
    assert summary["recent_warning_count"] == 0


def test_supervisor_pause_resume_never_enables_live(tmp_path) -> None:
    supervisor = _supervisor(tmp_path)

    paused = supervisor.pause_trading("maintenance")
    blocked = supervisor.get_risk_status()
    resumed = supervisor.resume_paper_mode()

    assert paused["mode"] == "paused"
    assert blocked["allowed"] is False
    assert resumed["mode"] == "paper"
    assert resumed["live_enabled"] is False
    assert supervisor.get_risk_status()["allowed"] is True


def test_supervisor_preflight_reports_config_mismatch(tmp_path) -> None:
    supervisor = AlgoSupervisor(
        SupervisorContext(config={"broker": {}}, broker=Broker(), data_dir=tmp_path)
    )

    report = supervisor.run_preflight()

    assert report["ok"] is False
    assert any(check["name"] == "config:strategy" for check in report["checks"])


def test_supervisor_latest_reports_and_restart(tmp_path) -> None:
    report = tmp_path / "daily.md"
    report.write_text("# daily\n", encoding="utf-8")
    premarket = tmp_path / "premarket_health.html"
    premarket.write_text("<h1>premarket</h1>\n", encoding="utf-8")
    called = []
    supervisor = _supervisor(tmp_path, restart_callback=lambda: called.append("restart") or "ok")

    assert supervisor.get_latest_daily_report()["content"] == "# daily\n"
    assert "premarket" in supervisor.get_latest_premarket_report()["content"]
    assert supervisor.get_latest_reports(limit=2)[0]["name"] in {"daily.md", "premarket_health.html"}
    assert supervisor.restart_algo() == {"restarted": True, "result": "ok"}
    assert called == ["restart"]


def test_supervisor_restart_without_callback(tmp_path) -> None:
    assert _supervisor(tmp_path).restart_algo()["restarted"] is False


def test_supervisor_logs_errors_and_open_incidents(tmp_path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "algo.log").write_text("INFO ok\nERROR failed\nTraceback line\n", encoding="utf-8")
    incident_dir = tmp_path / "incidents" / "incident-1"
    incident_dir.mkdir(parents=True)
    (incident_dir / "incident.json").write_text(
        json.dumps({"incident": {"incident_id": "incident-1", "severity": "high"}}),
        encoding="utf-8",
    )
    supervisor = _supervisor(tmp_path)

    assert supervisor.get_recent_logs()["logs"][0]["lines"][-1] == "Traceback line"
    assert supervisor.get_recent_errors()["errors"][0]["lines"] == ["ERROR failed", "Traceback line"]
    assert supervisor.get_open_incidents()[0]["incident_id"] == "incident-1"


def test_supervisor_recent_logs_falls_back_to_journal_provider(tmp_path) -> None:
    calls = []

    def _provider(service: str, max_lines: int) -> list[str]:
        calls.append((service, max_lines))
        return ["INFO boot", "INFO running"]

    supervisor = AlgoSupervisor(
        SupervisorContext(
            config={"broker": {"paper": True}},
            data_dir=tmp_path,
            logs_dir=tmp_path / "missing-logs",
            log_service_name="custom.service",
            log_provider=_provider,
        ),
        user_id="u1",
    )

    payload = supervisor.get_recent_logs(max_lines=1)

    assert payload["available"] is True
    assert payload["logs"][0]["source"] == "journalctl"
    assert payload["logs"][0]["service"] == "custom.service"
    assert payload["logs"][0]["lines"] == ["INFO running"]
    assert calls == [("custom.service", 1)]


def test_supervisor_recent_logs_missing_service_or_log_source(tmp_path) -> None:
    def _provider(_service: str, _max_lines: int) -> list[str]:
        raise RuntimeError("journal unavailable")

    supervisor = AlgoSupervisor(
        SupervisorContext(
            config={"broker": {"paper": True}},
            data_dir=tmp_path,
            logs_dir=tmp_path / "missing-logs",
            log_service_name="missing.service",
            log_provider=_provider,
        ),
        user_id="u1",
    )

    payload = supervisor.get_recent_logs(max_lines=5)

    assert payload["available"] is False
    assert payload["logs"] == []
    assert payload["reason"] == "logs_unavailable"
    assert payload["service"] == "missing.service"
    assert "journal unavailable" in payload["error"]


def test_supervisor_recent_logs_filters_file_lines(tmp_path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "algo.log").write_text(
        "\n".join(
            [
                "2026-06-06T14:29:59+00:00 INFO component=broker old heartbeat",
                "2026-06-06T14:30:00+00:00 INFO component=broker boundary heartbeat",
                "2026-06-06T14:40:00+00:00 WARNING component=news provider slow",
                "2026-06-06T14:45:00+00:00 ERROR component=broker order rejected",
                "2026-06-06T15:01:00+00:00 ERROR component=broker future failure",
            ]
        ),
        encoding="utf-8",
    )
    supervisor = _supervisor(tmp_path)

    payload = supervisor.get_recent_logs(
        max_lines=10,
        since_minutes=30,
        component="broker",
        severity="WARNING",
        text="order",
        now=datetime(2026, 6, 6, 15, 0, tzinfo=timezone.utc),
    )

    assert payload["available"] is True
    assert payload["logs"][0]["lines"] == [
        "2026-06-06T14:45:00+00:00 ERROR component=broker order rejected",
    ]


def test_supervisor_recent_logs_ignores_malformed_lines_with_time_filter(tmp_path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "algo.log").write_text(
        "\n".join(
            [
                "{not-json INFO component=broker",
                "INFO component=broker timestamp missing",
                '{"timestamp":"2026-06-06T14:55:00+00:00","level":"INFO","component":"broker","message":"valid heartbeat"}',
            ]
        ),
        encoding="utf-8",
    )
    supervisor = _supervisor(tmp_path)

    payload = supervisor.get_recent_logs(
        since_minutes=30,
        component="broker",
        now=datetime(2026, 6, 6, 15, 0, tzinfo=timezone.utc),
    )

    assert payload["available"] is True
    assert payload["logs"][0]["lines"] == [
        '{"timestamp":"2026-06-06T14:55:00+00:00","level":"INFO","component":"broker","message":"valid heartbeat"}',
    ]


def test_supervisor_recent_errors_searches_journal_markers(tmp_path) -> None:
    def _provider(_service: str, _max_lines: int) -> list[str]:
        return [
            "INFO ok",
            "TypeError: bad operand",
            "order rejected by broker",
            "insufficient qty available",
            "Traceback line",
            "DYNAMIC_SCAN_BATCH candidates=50 accepted=0 rejected=50 elapsed_ms=1200",
        ]

    supervisor = AlgoSupervisor(
        SupervisorContext(
            config={"broker": {"paper": True}},
            data_dir=tmp_path,
            logs_dir=tmp_path / "missing-logs",
            log_provider=_provider,
        ),
        user_id="u1",
    )

    payload = supervisor.get_recent_errors(max_lines=10)

    assert payload["available"] is True
    assert payload["errors"][0]["source"] == "journalctl"
    assert payload["errors"][0]["service"] == "algo.service"
    assert payload["errors"][0]["lines"] == [
        "TypeError: bad operand",
        "order rejected by broker",
        "insufficient qty available",
        "Traceback line",
    ]


def test_supervisor_recent_errors_file_markers_include_rejected_and_insufficient_qty(tmp_path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "algo.log").write_text(
        "INFO ok\norder rejected by broker\ninsufficient qty available\n",
        encoding="utf-8",
    )
    supervisor = _supervisor(tmp_path)

    payload = supervisor.get_recent_errors()

    assert payload["available"] is True
    assert payload["errors"][0]["lines"] == ["order rejected by broker", "insufficient qty available"]


def test_supervisor_recent_errors_ignores_normal_dynamic_scan_rejected_counts(tmp_path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "algo.log").write_text(
        "\n".join(
            [
                "INFO DYNAMIC_SCAN_BATCH candidates=50 accepted=0 rejected=50 elapsed_ms=1200",
                "INFO DYNAMIC_SCAN reject SOXS: below_min_relative_volume rel=1.20 min=2.00",
                "WARNING normal scanner rejection rejected=12",
            ]
        ),
        encoding="utf-8",
    )
    supervisor = _supervisor(tmp_path)

    payload = supervisor.get_recent_errors()

    assert payload["available"] is False
    assert payload["errors"] == []


def test_supervisor_recent_errors_filters_recent_boundary_timestamps(tmp_path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "algo.log").write_text(
        "\n".join(
            [
                "2026-06-06T14:29:59+00:00 ERROR component=broker old failure",
                "2026-06-06T14:30:00+00:00 ERROR component=broker boundary failure",
                "2026-06-06T14:45:00+00:00 ERROR component=broker recent failure",
                "2026-06-06T15:01:00+00:00 ERROR component=broker future failure",
            ]
        ),
        encoding="utf-8",
    )
    supervisor = _supervisor(tmp_path)

    payload = supervisor.get_recent_errors(
        since_minutes=30,
        now=datetime(2026, 6, 6, 15, 0, tzinfo=timezone.utc),
    )

    assert payload["available"] is True
    assert payload["errors"][0]["lines"] == [
        "2026-06-06T14:30:00+00:00 ERROR component=broker boundary failure",
        "2026-06-06T14:45:00+00:00 ERROR component=broker recent failure",
    ]


def test_supervisor_recent_errors_filters_by_severity(tmp_path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "algo.log").write_text(
        "\n".join(
            [
                "2026-06-06T14:50:00+00:00 WARNING component=broker Exception warning only",
                "2026-06-06T14:51:00+00:00 ERROR component=broker order rejected",
                "2026-06-06T14:52:00+00:00 CRITICAL component=broker service crash",
            ]
        ),
        encoding="utf-8",
    )
    supervisor = _supervisor(tmp_path)

    payload = supervisor.get_recent_errors(
        severity="ERROR",
        since_minutes=30,
        now=datetime(2026, 6, 6, 15, 0, tzinfo=timezone.utc),
    )

    assert payload["errors"][0]["lines"] == [
        "2026-06-06T14:51:00+00:00 ERROR component=broker order rejected",
        "2026-06-06T14:52:00+00:00 CRITICAL component=broker service crash",
    ]


def test_supervisor_recent_errors_filters_by_component_and_text(tmp_path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "algo.log").write_text(
        "\n".join(
            [
                "2026-06-06T14:50:00+00:00 ERROR component=news provider timeout",
                "2026-06-06T14:51:00+00:00 ERROR component=broker order rejected by broker",
                "2026-06-06T14:52:00+00:00 ERROR component=broker insufficient qty available",
            ]
        ),
        encoding="utf-8",
    )
    supervisor = _supervisor(tmp_path)

    payload = supervisor.get_recent_errors(
        component="broker",
        text="insufficient",
        since_minutes=30,
        now=datetime(2026, 6, 6, 15, 0, tzinfo=timezone.utc),
    )

    assert payload["available"] is True
    assert payload["errors"][0]["lines"] == [
        "2026-06-06T14:52:00+00:00 ERROR component=broker insufficient qty available",
    ]


def test_supervisor_recent_errors_empty_when_no_filter_match(tmp_path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "algo.log").write_text(
        "2026-06-06T14:50:00+00:00 ERROR component=news provider timeout\n",
        encoding="utf-8",
    )
    supervisor = _supervisor(tmp_path)

    payload = supervisor.get_recent_errors(
        component="broker",
        since_minutes=30,
        now=datetime(2026, 6, 6, 15, 0, tzinfo=timezone.utc),
    )

    assert payload["available"] is False
    assert payload["errors"] == []


def test_supervisor_recent_errors_ignores_malformed_lines_with_time_filter(tmp_path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "algo.log").write_text(
        "\n".join(
            [
                "{not-json ERROR component=broker",
                "ERROR component=broker timestamp missing",
                "2026-06-06T14:55:00+00:00 ERROR component=broker valid failure",
            ]
        ),
        encoding="utf-8",
    )
    supervisor = _supervisor(tmp_path)

    payload = supervisor.get_recent_errors(
        since_minutes=30,
        now=datetime(2026, 6, 6, 15, 0, tzinfo=timezone.utc),
    )

    assert payload["available"] is True
    assert payload["errors"][0]["lines"] == [
        "2026-06-06T14:55:00+00:00 ERROR component=broker valid failure",
    ]


def test_approval_required_actions_are_blocked_by_default(tmp_path) -> None:
    supervisor = _supervisor(tmp_path)

    for name in ("enable_live_trading", "deploy_code", "push_main_branch"):
        result = getattr(supervisor, name)(approved=True)
        assert result["approval_required"] is True
        assert result["ok"] is False
    risk = supervisor.change_risk_limits({"max_daily_loss_pct": 1}, approved=True)
    assert risk["approval_required"] is True
