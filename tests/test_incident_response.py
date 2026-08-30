from __future__ import annotations

import json

from src.incident_response import (
    auto_restart_for_operational_failure,
    detect_failures,
    generate_incident_package,
    respond_to_incident,
)


class Supervisor:
    def get_health_status(self) -> dict:
        return {"ok": False}

    def get_account_status(self) -> dict:
        return {"equity": 100}

    def get_risk_status(self) -> dict:
        return {"allowed": True}

    def get_positions(self) -> list:
        return []


def test_detect_failures_classifies_risk_as_critical_no_restart() -> None:
    incident = detect_failures(
        risk_status={"allowed": False, "alerts": [{"code": "daily_loss_limit"}]}
    )

    assert incident is not None
    assert incident.classification == "risk"
    assert incident.severity == "critical"
    assert incident.auto_restart_allowed is False
    assert "risk:daily_loss_limit" in incident.signals


def test_detect_failures_classifies_broker_health_as_restartable() -> None:
    incident = detect_failures(
        health_status={
            "ok": False,
            "checks": [{"name": "broker", "ok": False, "reason": "get_clock:TimeoutError"}],
        }
    )

    assert incident is not None
    assert incident.classification == "broker"
    assert incident.auto_restart_allowed is True


def test_generate_incident_package_collects_logs_and_diagnostics(tmp_path) -> None:
    log_path = tmp_path / "algo.log"
    log_path.write_text("line1\nline2\n", encoding="utf-8")
    incident = detect_failures(exception=RuntimeError("loop crashed"))
    assert incident is not None

    package = generate_incident_package(
        incident,
        supervisor=Supervisor(),
        log_paths=[log_path],
        incidents_dir=tmp_path / "incidents",
        exception=RuntimeError("loop crashed"),
    )

    assert package.json_path.exists()
    assert package.markdown_path.exists()
    payload = json.loads(package.json_path.read_text(encoding="utf-8"))
    assert payload["incident"]["classification"] == "operational"
    assert str(log_path) in payload["logs"]
    assert payload["diagnostics"]["get_account_status"]["equity"] == 100
    assert "Remediate operational incident" in payload["remediation"]["objective"]


def test_auto_restart_only_for_allowed_incidents() -> None:
    operational = detect_failures(exception=RuntimeError("boom"))
    risk = detect_failures(risk_status={"allowed": False, "alerts": [{"code": "kill_switch"}]})
    called = []

    assert operational is not None
    assert risk is not None
    assert auto_restart_for_operational_failure(
        operational, lambda: called.append("restart") or "ok"
    ) == {"restarted": True, "result": "ok"}
    assert auto_restart_for_operational_failure(risk, lambda: "bad")["restarted"] is False
    assert called == ["restart"]


def test_respond_to_incident_noops_without_failure(tmp_path) -> None:
    result = respond_to_incident(health_status={"ok": True}, incidents_dir=tmp_path)

    assert result["incident"] is None
    assert result["restart"]["reason"] == "no_incident"


def test_respond_to_incident_packages_and_restarts_broker_failure(tmp_path) -> None:
    calls = []
    result = respond_to_incident(
        health_status={
            "ok": False,
            "checks": [{"name": "broker", "ok": False, "reason": "ConnectionError"}],
        },
        supervisor=Supervisor(),
        incidents_dir=tmp_path,
        restart_callback=lambda: calls.append("restart") or "ok",
    )

    assert result["incident"]["classification"] == "broker"
    assert result["restart"]["restarted"] is True
    assert calls == ["restart"]
