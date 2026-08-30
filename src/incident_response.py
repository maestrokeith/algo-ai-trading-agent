"""Autonomous incident response for AlgoSphere operations."""

from __future__ import annotations

import json
import logging
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Incident:
    """Detected operational, risk, startup, or broker incident."""

    incident_id: str
    classification: str
    severity: str
    summary: str
    signals: list[str] = field(default_factory=list)
    auto_restart_allowed: bool = False


@dataclass(frozen=True)
class IncidentPackage:
    """Persisted incident package paths."""

    incident: Incident
    json_path: Path
    markdown_path: Path
    diagnostics: dict[str, Any]
    logs: dict[str, str]
    remediation: dict[str, Any]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _incident_id(now: datetime | None = None) -> str:
    return (now or _utc_now()).strftime("incident-%Y%m%dT%H%M%SZ")


def _tail(path: Path, *, max_lines: int = 80) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        return f"unreadable:{type(exc).__name__}"
    return "\n".join(lines[-max_lines:])


def collect_logs(paths: Sequence[Path], *, max_lines: int = 80) -> dict[str, str]:
    """Collect log tails from existing paths."""
    return {str(path): _tail(path, max_lines=max_lines) for path in paths if path.exists()}


def classify_incident(signals: Sequence[str]) -> tuple[str, str, bool]:
    """Classify incident from signal codes."""
    signal_set = set(signals)
    if any(signal.startswith("risk:") for signal in signal_set):
        return "risk", "critical", False
    if any(signal.startswith("preflight:") for signal in signal_set):
        return "startup", "high", False
    if any(signal.startswith("broker:") for signal in signal_set):
        return "broker", "high", True
    if any(signal.startswith("process:") or signal.startswith("health:") for signal in signal_set):
        return "operational", "medium", True
    return "unknown", "low", False


def detect_failures(
    *,
    health_status: Mapping[str, Any] | None = None,
    preflight_report: Mapping[str, Any] | None = None,
    risk_status: Mapping[str, Any] | None = None,
    exception: BaseException | None = None,
) -> Incident | None:
    """Detect failures from supervisor status payloads."""
    signals: list[str] = []
    if health_status and not bool(health_status.get("ok", True)):
        for check in health_status.get("checks") or []:
            if isinstance(check, Mapping) and not check.get("ok", True):
                signals.append(f"health:{check.get('name', 'unknown')}:{check.get('reason', '')}")
                if check.get("name") == "broker":
                    signals.append(f"broker:{check.get('reason', '')}")
    if preflight_report and not bool(preflight_report.get("ok", True)):
        for check in preflight_report.get("checks") or []:
            if isinstance(check, Mapping) and not check.get("ok", True):
                signals.append(f"preflight:{check.get('name', 'unknown')}:{check.get('reason', '')}")
    if risk_status and not bool(risk_status.get("allowed", True)):
        for alert in risk_status.get("alerts") or []:
            if isinstance(alert, Mapping):
                signals.append(f"risk:{alert.get('code', 'unknown')}")
    if exception is not None:
        signals.append(f"process:{type(exception).__name__}:{exception}")

    if not signals:
        return None
    classification, severity, auto_restart = classify_incident(signals)
    return Incident(
        incident_id=_incident_id(),
        classification=classification,
        severity=severity,
        summary=f"{classification} incident detected",
        signals=signals,
        auto_restart_allowed=auto_restart,
    )


def run_diagnostics(
    *,
    supervisor: Any | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run diagnostics through available supervisor methods."""
    diagnostics: dict[str, Any] = {"timestamp": _utc_now().isoformat()}
    if supervisor is not None:
        for name in ("get_health_status", "get_account_status", "get_risk_status", "get_positions"):
            method = getattr(supervisor, name, None)
            if not callable(method):
                continue
            try:
                diagnostics[name] = method()
            except Exception as exc:
                diagnostics[name] = {"error": f"{type(exc).__name__}: {exc}"}
    if extra:
        diagnostics["extra"] = dict(extra)
    return diagnostics


def prepare_codex_remediation_workflow(incident: Incident, package_dir: Path) -> dict[str, Any]:
    """Prepare a concise remediation workflow for Codex follow-up."""
    return {
        "objective": f"Remediate {incident.classification} incident {incident.incident_id}",
        "package_dir": str(package_dir),
        "steps": [
            "Review incident.json and incident.md",
            "Inspect included log tails and diagnostics",
            "Reproduce failure with targeted pytest or preflight command",
            "Patch root cause without changing secrets or enabling live trading",
            "Run full pytest before handoff",
        ],
    }


def generate_incident_package(
    incident: Incident,
    *,
    supervisor: Any | None = None,
    log_paths: Sequence[Path] = (),
    incidents_dir: Path | None = None,
    exception: BaseException | None = None,
) -> IncidentPackage:
    """Write incident package with logs, diagnostics, and remediation steps."""
    root = incidents_dir or (Path(__file__).resolve().parents[1] / "data" / "incidents")
    package_dir = root / incident.incident_id
    package_dir.mkdir(parents=True, exist_ok=True)
    logs = collect_logs(log_paths)
    diagnostics = run_diagnostics(supervisor=supervisor)
    if exception is not None:
        diagnostics["exception_traceback"] = "".join(
            traceback.format_exception(type(exception), exception, exception.__traceback__)
        )
    remediation = prepare_codex_remediation_workflow(incident, package_dir)

    payload = {
        "incident": incident.__dict__,
        "diagnostics": diagnostics,
        "logs": logs,
        "remediation": remediation,
    }
    json_path = package_dir / "incident.json"
    markdown_path = package_dir / "incident.md"
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    markdown_path.write_text(
        "\n".join(
            [
                f"# {incident.incident_id}",
                "",
                f"- classification: {incident.classification}",
                f"- severity: {incident.severity}",
                f"- auto_restart_allowed: {incident.auto_restart_allowed}",
                "",
                "## Signals",
                *[f"- {signal}" for signal in incident.signals],
                "",
                "## Remediation",
                *[f"- {step}" for step in remediation["steps"]],
                "",
            ]
        ),
        encoding="utf-8",
    )
    return IncidentPackage(
        incident=incident,
        json_path=json_path,
        markdown_path=markdown_path,
        diagnostics=diagnostics,
        logs=logs,
        remediation=remediation,
    )


def auto_restart_for_operational_failure(
    incident: Incident,
    restart_callback: Callable[[], Any] | None,
) -> dict[str, Any]:
    """Restart only when classification allows operational restart."""
    if not incident.auto_restart_allowed:
        return {"restarted": False, "reason": "restart_not_allowed", "classification": incident.classification}
    if restart_callback is None:
        return {"restarted": False, "reason": "restart_callback_missing"}
    result = restart_callback()
    return {"restarted": True, "result": result}


def respond_to_incident(
    *,
    health_status: Mapping[str, Any] | None = None,
    preflight_report: Mapping[str, Any] | None = None,
    risk_status: Mapping[str, Any] | None = None,
    exception: BaseException | None = None,
    supervisor: Any | None = None,
    log_paths: Sequence[Path] = (),
    incidents_dir: Path | None = None,
    restart_callback: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Detect, package, diagnose, and optionally restart after an incident."""
    incident = detect_failures(
        health_status=health_status,
        preflight_report=preflight_report,
        risk_status=risk_status,
        exception=exception,
    )
    if incident is None:
        return {"incident": None, "package": None, "restart": {"restarted": False, "reason": "no_incident"}}
    package = generate_incident_package(
        incident,
        supervisor=supervisor,
        log_paths=log_paths,
        incidents_dir=incidents_dir,
        exception=exception,
    )
    restart = auto_restart_for_operational_failure(incident, restart_callback)
    return {
        "incident": incident.__dict__,
        "package": {"json_path": str(package.json_path), "markdown_path": str(package.markdown_path)},
        "restart": restart,
    }


__all__ = [
    "Incident",
    "IncidentPackage",
    "auto_restart_for_operational_failure",
    "classify_incident",
    "collect_logs",
    "detect_failures",
    "generate_incident_package",
    "prepare_codex_remediation_workflow",
    "respond_to_incident",
    "run_diagnostics",
]
