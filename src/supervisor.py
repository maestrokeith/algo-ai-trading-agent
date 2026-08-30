"""Autonomous supervisor and MCP tool surface for AlgoSphere."""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src import position_tracker
from src.global_risk import evaluate_global_risk, set_kill_switch
from src.health_monitor import HealthCheckResult, check_broker_connectivity, check_process_alive
from src.incident_response import respond_to_incident
from src.preflight import run_preflight
from src.trade_postmortem import explain_trade

logger = logging.getLogger(__name__)
DEFAULT_LOG_SERVICE_NAME = "algo.service"
ERROR_MARKERS = (
    "ERROR",
    "Traceback",
    "Exception",
    "TypeError",
    "ValueError",
    "order rejected",
    "insufficient qty",
    "broker failure",
    "failed health check",
    "service crash",
)
SEVERITY_RANK = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "WARN": 30,
    "ERROR": 40,
    "TRACEBACK": 40,
    "EXCEPTION": 40,
    "TYPEERROR": 40,
    "VALUEERROR": 40,
    "CRITICAL": 50,
}


def _data_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "data"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _state_path(user_id: str, data_dir: Path | None = None) -> Path:
    return (data_dir or _data_dir()) / f"supervisor_state_{user_id}.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Could not read JSON from %s", path, exc_info=True)
        return default


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _obj_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    out: dict[str, Any] = {}
    for name in dir(value):
        if name.startswith("_"):
            continue
        try:
            attr = getattr(value, name)
        except Exception:
            continue
        if callable(attr):
            continue
        if isinstance(attr, (str, int, float, bool)) or attr is None:
            out[name] = attr
    return out


def _call_first(obj: Any, names: Sequence[str]) -> Any:
    for name in names:
        method = getattr(obj, name, None)
        if callable(method):
            return method()
    return None


def _latest_file(roots: Sequence[Path | None], patterns: Sequence[str]) -> Path | None:
    files: list[Path] = []
    for root in roots:
        if root is None or not root.exists():
            continue
        for pattern in patterns:
            files.extend(path for path in root.glob(pattern) if path.is_file())
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def _read_text_tail(path: Path, *, max_chars: int = 20_000) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _tool_error(name: str, exc: Exception) -> dict[str, Any]:
    logger.warning("Supervisor tool %s failed", name, exc_info=True)
    return {"available": False, "error": f"{type(exc).__name__}: {exc}"}


def _journalctl_log_lines(service_name: str, *, max_lines: int = 100) -> list[str]:
    service = str(service_name or DEFAULT_LOG_SERVICE_NAME).strip() or DEFAULT_LOG_SERVICE_NAME
    limit = max(1, int(max_lines or 100))
    result = subprocess.run(
        ["journalctl", "-u", service, "-n", str(limit), "--no-pager", "--output=short-iso"],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(stderr or f"journalctl exited {result.returncode}")
    return result.stdout.splitlines()


def _line_has_error_marker(line: str) -> bool:
    text = str(line or "")
    lower = text.lower()
    for marker in ERROR_MARKERS:
        marker_text = str(marker)
        if marker_text.islower():
            if marker_text in lower:
                return True
        elif marker_text in text:
            return True
    return False


def _parse_log_timestamp(raw: Any) -> datetime | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalise_severity(raw: Any, line: str = "") -> str | None:
    if raw is not None:
        text = str(raw).strip().upper()
        if text in SEVERITY_RANK:
            return text
    words = str(line or "").replace(":", " ").replace(",", " ").split()
    for word in words:
        upper = word.strip("[]()").upper()
        if upper in SEVERITY_RANK:
            return upper
    if "Traceback" in str(line or ""):
        return "TRACEBACK"
    return None


@dataclass(frozen=True)
class _ParsedLogLine:
    line: str
    timestamp: datetime | None = None
    severity: str | None = None
    component: str | None = None
    message: str = ""


def _parse_log_line(line: str) -> _ParsedLogLine:
    raw_line = str(line or "")
    stripped = raw_line.strip()
    if not stripped:
        return _ParsedLogLine(line=raw_line, message="")
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except Exception:
            payload = None
        if isinstance(payload, Mapping):
            timestamp = _parse_log_timestamp(
                payload.get("timestamp")
                or payload.get("time")
                or payload.get("created_at")
                or payload.get("ts")
            )
            severity = _normalise_severity(
                payload.get("severity") or payload.get("level") or payload.get("levelname"),
                raw_line,
            )
            component = payload.get("component") or payload.get("module") or payload.get("service")
            message = str(payload.get("message") or payload.get("msg") or raw_line)
            return _ParsedLogLine(
                line=raw_line,
                timestamp=timestamp,
                severity=severity,
                component=str(component).strip() if component else None,
                message=message,
            )

    parts = stripped.split(maxsplit=1)
    timestamp = _parse_log_timestamp(parts[0]) if parts else None
    remainder = parts[1] if timestamp is not None and len(parts) > 1 else stripped
    if ":" in remainder:
        _prefix, possible_message = remainder.split(":", 1)
        if any(marker in possible_message for marker in ("ERROR", "Traceback", "Exception")):
            remainder = possible_message.strip()
    severity = _normalise_severity(None, remainder)
    component: str | None = None
    words = remainder.split()
    for idx, word in enumerate(words):
        clean = word.strip(",")
        if clean.startswith("component="):
            component = clean.split("=", 1)[1].strip("[],:")
            break
        if clean.lower() in {"component", "component:"} and idx + 1 < len(words):
            component = words[idx + 1].strip("[],:")
            break
    if component is None and words and words[0].startswith("[") and words[0].endswith("]"):
        component = words[0].strip("[]")
    return _ParsedLogLine(
        line=raw_line,
        timestamp=timestamp,
        severity=severity,
        component=component or None,
        message=remainder,
    )


def _line_matches_recent_error_filters(
    line: str,
    *,
    since: datetime | None = None,
    severity: str | None = None,
    component: str | None = None,
    text: str | None = None,
    now: datetime | None = None,
) -> bool:
    if not _line_has_error_marker(line):
        return False
    parsed = _parse_log_line(line)
    if since is not None:
        if parsed.timestamp is None:
            return False
        upper = now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)
        if parsed.timestamp < since or parsed.timestamp > upper:
            return False
    if severity:
        wanted = _normalise_severity(severity)
        actual = _normalise_severity(parsed.severity, parsed.line)
        if wanted is None or actual is None:
            return False
        if SEVERITY_RANK.get(actual, -1) < SEVERITY_RANK.get(wanted, 10_000):
            return False
    if component:
        actual_component = str(parsed.component or "").strip().lower()
        if actual_component != str(component).strip().lower():
            return False
    if text:
        haystack = f"{parsed.line}\n{parsed.message}".lower()
        if str(text).strip().lower() not in haystack:
            return False
    return True


def _line_matches_log_filters(
    line: str,
    *,
    since: datetime | None = None,
    severity: str | None = None,
    component: str | None = None,
    text: str | None = None,
    now: datetime | None = None,
) -> bool:
    parsed = _parse_log_line(line)
    if since is not None:
        if parsed.timestamp is None:
            return False
        upper = now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)
        if parsed.timestamp < since or parsed.timestamp > upper:
            return False
    if severity:
        wanted = _normalise_severity(severity)
        actual = _normalise_severity(parsed.severity, parsed.line)
        if wanted is None or actual is None:
            return False
        if SEVERITY_RANK.get(actual, -1) < SEVERITY_RANK.get(wanted, 10_000):
            return False
    if component:
        actual_component = str(parsed.component or "").strip().lower()
        if actual_component != str(component).strip().lower():
            return False
    if text:
        haystack = f"{parsed.line}\n{parsed.message}".lower()
        if str(text).strip().lower() not in haystack:
            return False
    return True


@dataclass
class SupervisorContext:
    """Dependencies used by the autonomous supervisor."""

    config: Mapping[str, Any] = field(default_factory=dict)
    broker: Any = None
    trades: Sequence[Mapping[str, Any]] = ()
    reports_dir: Path | None = None
    data_dir: Path | None = None
    logs_dir: Path | None = None
    incidents_dir: Path | None = None
    log_service_name: str = DEFAULT_LOG_SERVICE_NAME
    log_provider: Callable[[str, int], list[str]] | None = None
    broker_error: str | None = None
    process_start_ts: float | None = None
    restart_callback: Callable[[], Any] | None = None
    deploy_callback: Callable[[], Any] | None = None
    allow_approved_actions: bool = False


class AlgoSupervisor:
    """Operational control plane for account, health, risk, and recovery tools."""

    def __init__(self, context: SupervisorContext | None = None, *, user_id: str = "default") -> None:
        self.context = context or SupervisorContext()
        self.user_id = user_id

    def get_account_status(self) -> dict[str, Any]:
        """Return account status from broker when available."""
        broker = self.context.broker
        if broker is None:
            out = {"available": False, "user_id": self.user_id, "reason": "broker_missing"}
            if self.context.broker_error:
                out["error"] = self.context.broker_error
            return out
        try:
            snapshot = _call_first(broker, ("get_account_snapshot", "get_account", "account"))
        except Exception as exc:
            return {"available": False, "user_id": self.user_id, **_tool_error("get_account_status", exc)}
        data = _obj_to_dict(snapshot)
        equity = data.get("equity")
        if equity is None:
            try:
                equity = _call_first(broker, ("get_equity",))
            except Exception as exc:
                return {"available": False, "user_id": self.user_id, **_tool_error("get_account_status", exc)}
            if equity is not None:
                data["equity"] = equity
        data.setdefault("available", True)
        data.setdefault("user_id", self.user_id)
        data.setdefault("paper", bool((self.context.config.get("broker") or {}).get("paper", True)))
        return data

    def get_positions(self) -> list[dict[str, Any]]:
        """Return broker positions, falling back to persisted tracker rows."""
        try:
            broker_positions = _call_first(self.context.broker, ("get_positions", "list_positions"))
        except Exception:
            logger.warning("[%s] Broker positions unavailable; using tracker fallback", self.user_id, exc_info=True)
            broker_positions = None
        if broker_positions is not None:
            return [_jsonable(_obj_to_dict(row)) for row in broker_positions]
        tracked = position_tracker.load(self.user_id, data_dir=self.context.data_dir)
        return [_jsonable({"symbol": symbol, **dict(row)}) for symbol, row in tracked.items()]

    def get_open_orders(self) -> list[dict[str, Any]]:
        """Return open broker orders when the broker exposes an order read API."""
        try:
            orders = _call_first(self.context.broker, ("get_open_orders",))
            if orders is None and self.context.broker is not None:
                list_orders = getattr(self.context.broker, "list_orders", None)
                if callable(list_orders):
                    orders = list_orders(status="open")
        except Exception:
            logger.warning("[%s] Broker open orders unavailable", self.user_id, exc_info=True)
            orders = None
        if orders is None:
            return []
        return [_jsonable(_obj_to_dict(order)) for order in orders]

    def get_today_pnl(self) -> dict[str, Any]:
        """Return realized and account-level P/L for the current session."""
        today = datetime.now(timezone.utc).date().isoformat()
        realized = 0.0
        for trade in self.context.trades:
            ts = str(trade.get("timestamp") or trade.get("time") or trade.get("created_at") or "")
            if ts and not ts.startswith(today):
                continue
            try:
                realized += float(trade.get("pnl") or 0.0)
            except (TypeError, ValueError):
                continue
        account = self.get_account_status()
        equity = float(account.get("equity") or 0.0)
        last_equity = float(account.get("last_equity") or equity or 0.0)
        session_pct = ((equity - last_equity) / last_equity * 100.0) if last_equity else 0.0
        return {"date": today, "realized_pnl": realized, "session_pnl_pct": session_pct}

    def get_health_status(self) -> dict[str, Any]:
        """Return process and broker health."""
        start_ts = self.context.process_start_ts or datetime.now(timezone.utc).timestamp()
        checks: list[HealthCheckResult] = [check_process_alive(process_start_ts=start_ts)]
        if self.context.broker is not None:
            checks.append(check_broker_connectivity(self.context.broker))
        else:
            checks.append(HealthCheckResult("broker", False, self.context.broker_error or "missing"))
        for name, path in (
            ("data_dir", self.context.data_dir),
            ("reports_dir", self.context.reports_dir),
            ("logs_dir", self.context.logs_dir),
            ("incidents_dir", self.context.incidents_dir),
        ):
            if path is not None and not path.exists():
                checks.append(HealthCheckResult(name, True, "missing_optional"))
            else:
                checks.append(HealthCheckResult(name, True, "ok"))
        return {
            "ok": all(check.ok for check in checks),
            "user_id": self.user_id,
            "checks": [check.__dict__ for check in checks],
            "timestamp": _utc_now(),
        }

    def get_status(self) -> dict[str, Any]:
        """Return compact read-only bot status for MCP clients."""
        health = self.get_health_status()
        account = self.get_account_status()
        risk = self.get_risk_status()
        positions = self.get_positions()
        open_orders = self.get_open_orders()
        today_pnl = self.get_today_pnl()
        return {
            "available": True,
            "user_id": self.user_id,
            "ok": bool(health.get("ok")) and bool(risk.get("allowed", True)),
            "paper": bool(account.get("paper", (self.context.config.get("broker") or {}).get("paper", True))),
            "broker_available": bool(account.get("available", True)),
            "risk_allowed": bool(risk.get("allowed", True)),
            "equity": account.get("equity"),
            "positions_count": len(positions),
            "open_orders_count": len(open_orders),
            "today_pnl": today_pnl,
            "health": {
                "ok": bool(health.get("ok")),
                "checks": health.get("checks", []),
            },
            "timestamp": _utc_now(),
        }

    def get_supervisor_summary(
        self,
        max_lines: int = 100,
        *,
        since_minutes: int | float | None = 60,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Return one compact read-only supervisor view for MCP clients."""
        now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        status = self.get_status()
        logs = self.get_recent_logs(
            max_lines=max_lines,
            since_minutes=since_minutes,
            now=now_utc,
        )
        errors = self.get_recent_errors(
            max_lines=max_lines,
            since_minutes=since_minutes,
            now=now_utc,
        )

        log_sources: list[str] = []
        warning_count = 0
        for group in logs.get("logs", []) or []:
            source = str(group.get("source") or group.get("service") or "unknown")
            if source not in log_sources:
                log_sources.append(source)
            for line in group.get("lines", []) or []:
                parsed = _parse_log_line(str(line or ""))
                severity = _normalise_severity(parsed.severity, parsed.line)
                if severity in {"WARNING", "WARN"}:
                    warning_count += 1

        error_lines: list[str] = []
        error_sources: list[str] = []
        for group in errors.get("errors", []) or []:
            source = str(group.get("source") or group.get("service") or "unknown")
            if source not in error_sources:
                error_sources.append(source)
            error_lines.extend(str(line or "") for line in group.get("lines", []) or [])

        uptime_seconds: float | None = None
        if self.context.process_start_ts is not None:
            try:
                uptime_seconds = max(
                    0.0,
                    now_utc.timestamp() - float(self.context.process_start_ts),
                )
            except (TypeError, ValueError):
                uptime_seconds = None

        log_availability: dict[str, Any] = {
            "available": bool(logs.get("available")),
            "sources": log_sources,
        }
        if not logs.get("available"):
            log_availability["reason"] = logs.get("reason")
            if logs.get("error"):
                log_availability["error"] = logs.get("error")

        return {
            "available": True,
            "user_id": self.user_id,
            "ok": bool(status.get("ok")) and not bool(error_lines),
            "service_status": {
                "ok": bool(status.get("ok")),
                "service": str(self.context.log_service_name or DEFAULT_LOG_SERVICE_NAME),
                "broker_available": bool(status.get("broker_available")),
                "risk_allowed": bool(status.get("risk_allowed")),
                "paper": bool(status.get("paper")),
            },
            "recent_errors": {
                "available": bool(errors.get("available")),
                "count": len(error_lines),
                "sources": error_sources,
                "sample": error_lines[-5:],
            },
            "recent_warning_count": int(warning_count),
            "log_availability": log_availability,
            "uptime_seconds": uptime_seconds,
            "timestamp": _utc_now(),
        }

    def get_risk_status(self) -> dict[str, Any]:
        """Return global risk status for current account and positions."""
        account = self.get_account_status()
        equity = float(account.get("equity") or 0.0)
        peak = float(account.get("peak_equity") or equity)
        last = float(account.get("last_equity") or equity)
        status = evaluate_global_risk(
            equity=equity,
            peak_equity=peak,
            last_equity=last,
            positions=self.get_positions(),
            trades=self.context.trades,
            config=self.context.config,
            user_id=self.user_id,
            data_dir=self.context.data_dir,
        )
        return status.as_dict()

    def explain_last_trade(self) -> dict[str, Any]:
        """Explain the most recent trade with P/L."""
        last = self.get_last_trade()
        if not last.get("available", True):
            return {"available": False, "reason": "no_trades"}
        explanation = explain_trade(last)
        if explanation is None:
            return {"available": False, "reason": "last_trade_not_realized"}
        return {"available": True, **explanation.__dict__}

    def get_last_trade(self) -> dict[str, Any]:
        """Return the most recent trade row available to the supervisor."""
        if self.context.trades:
            return _jsonable({"available": True, **dict(self.context.trades[-1])})
        broker = self.context.broker
        if broker is not None and callable(getattr(broker, "get_orders_for_date", None)):
            try:
                orders = list(broker.get_orders_for_date(datetime.now(timezone.utc).date()) or [])
            except Exception as exc:
                return _tool_error("get_last_trade", exc)
            if orders:
                return _jsonable({"available": True, **_obj_to_dict(orders[-1])})
        return {"available": False, "reason": "no_trades"}

    def _latest_report(self, kind: str) -> dict[str, Any]:
        reports_dir = self.context.reports_dir or (_project_root() / "reports")
        docs_dir = _project_root() / "docs"
        if kind == "premarket":
            patterns = ("premarket*.html", "premarket*.md", "*premarket*.json")
        else:
            patterns = ("daily*.html", "daily*.md", "*daily*.json", "codex_nightly_report.md")
        path = _latest_file((reports_dir, docs_dir), patterns)
        if path is None:
            return {"available": False, "reason": "report_missing", "kind": kind}
        return {
            "available": True,
            "kind": kind,
            "path": str(path),
            "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            "content": _read_text_tail(path),
        }

    def get_latest_premarket_report(self) -> dict[str, Any]:
        """Return the newest pre-market report content from reports/docs."""
        return self._latest_report("premarket")

    def get_latest_daily_report(self) -> dict[str, Any]:
        """Return the newest daily report content from reports/docs."""
        return self._latest_report("daily")

    def get_latest_reports(self, limit: int = 5) -> list[dict[str, Any]]:
        """Return newest markdown/json reports from reports and docs directories."""
        roots = [
            self.context.reports_dir or (_project_root() / "reports"),
            _project_root() / "docs",
        ]
        files: list[Path] = []
        for root in roots:
            if root is None or not root.exists():
                continue
            for pattern in ("*.md", "*.json", "*.html"):
                files.extend(root.glob(pattern))
        newest = sorted(set(files), key=lambda path: path.stat().st_mtime, reverse=True)[:limit]
        return [
            {
                "path": str(path),
                "name": path.name,
                "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            }
            for path in newest
        ]

    def _log_paths(self) -> list[Path]:
        if self.context.logs_dir is not None:
            roots = [self.context.logs_dir]
        else:
            roots = [
                _project_root() / "logs",
                _project_root() / "data" / "logs",
            ]
        files: list[Path] = []
        for root in roots:
            if root is None or not root.exists():
                continue
            files.extend(path for path in root.glob("*.log") if path.is_file())
        return sorted(set(files), key=lambda path: path.stat().st_mtime, reverse=True)

    def _journal_log_lines(self, max_lines: int) -> tuple[list[str], str | None]:
        provider = self.context.log_provider or (
            lambda service, limit: _journalctl_log_lines(service, max_lines=limit)
        )
        service = str(self.context.log_service_name or DEFAULT_LOG_SERVICE_NAME).strip() or DEFAULT_LOG_SERVICE_NAME
        try:
            return list(provider(service, max(1, int(max_lines or 100))) or []), None
        except Exception as exc:
            logger.warning(
                "[%s] journal log provider unavailable service=%s: %s",
                self.user_id,
                service,
                exc,
            )
            return [], f"{type(exc).__name__}: {exc}"

    def get_recent_logs(
        self,
        max_lines: int = 100,
        *,
        since_minutes: int | float | None = None,
        component: str | None = None,
        severity: str | None = None,
        text: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Return tails from recent log files, falling back to journalctl."""
        limit = max(1, int(max_lines or 100))
        now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        since: datetime | None = None
        if since_minutes is not None:
            try:
                since_value = float(since_minutes)
            except (TypeError, ValueError):
                since_value = 0.0
            since = now_utc - timedelta(minutes=max(0.0, since_value))

        filter_active = any(value is not None for value in (since, component, severity, text))

        def _filter_lines(lines: Sequence[str]) -> list[str]:
            if not filter_active:
                return list(lines)
            return [
                line
                for line in lines
                if _line_matches_log_filters(
                    line,
                    since=since,
                    component=component,
                    severity=severity,
                    text=text,
                    now=now_utc,
                )
            ]

        payload = []
        for path in self._log_paths()[:5]:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            filtered = _filter_lines(lines)
            if filter_active and not filtered:
                continue
            payload.append(
                {
                    "source": "file",
                    "path": str(path),
                    "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                    "lines": filtered[-limit:],
                }
            )
        if payload:
            return {"available": True, "logs": payload}

        provider_limit = limit
        if filter_active:
            provider_limit = max(limit, min(limit * 5, 1000))
        lines, error = self._journal_log_lines(provider_limit)
        filtered = _filter_lines(lines)
        if filtered:
            return {
                "available": True,
                "logs": [
                    {
                        "source": "journalctl",
                        "service": str(self.context.log_service_name or DEFAULT_LOG_SERVICE_NAME),
                        "lines": filtered[-limit:],
                    }
                ],
            }
        out: dict[str, Any] = {
            "available": False,
            "logs": [],
            "reason": "logs_unavailable",
            "service": str(self.context.log_service_name or DEFAULT_LOG_SERVICE_NAME),
        }
        if error:
            out["error"] = error
        return out

    def get_recent_errors(
        self,
        max_lines: int = 100,
        *,
        since_minutes: int | float | None = None,
        severity: str | None = None,
        component: str | None = None,
        text: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Return recent error lines from log files or journalctl."""
        limit = max(1, int(max_lines or 100))
        now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        since: datetime | None = None
        if since_minutes is not None:
            try:
                since_value = float(since_minutes)
            except (TypeError, ValueError):
                since_value = 0.0
            since = now_utc - timedelta(minutes=max(0.0, since_value))

        def _matches(line: str) -> bool:
            return _line_matches_recent_error_filters(
                line,
                since=since,
                severity=severity,
                component=component,
                text=text,
                now=now_utc,
            )

        payload = []
        log_paths = self._log_paths()[:10]
        for path in log_paths:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            errors = [line for line in lines if _matches(line)]
            if errors:
                payload.append({"source": "file", "path": str(path), "lines": errors[-limit:]})
        if payload:
            return {"available": True, "errors": payload}
        if log_paths:
            return {
                "available": False,
                "errors": [],
                "reason": "errors_unavailable",
                "service": str(self.context.log_service_name or DEFAULT_LOG_SERVICE_NAME),
            }

        provider_limit = limit
        if any(value is not None for value in (since, severity, component, text)):
            provider_limit = max(limit, min(limit * 5, 1000))
        lines, error = self._journal_log_lines(provider_limit)
        errors = [line for line in lines if _matches(line)]
        if errors:
            return {
                "available": True,
                "errors": [
                    {
                        "source": "journalctl",
                        "service": str(self.context.log_service_name or DEFAULT_LOG_SERVICE_NAME),
                        "lines": errors[-limit:],
                    }
                ],
            }
        out: dict[str, Any] = {
            "available": False,
            "errors": [],
            "reason": "errors_unavailable",
            "service": str(self.context.log_service_name or DEFAULT_LOG_SERVICE_NAME),
        }
        if error:
            out["error"] = error
        return out

    def get_open_incidents(self) -> list[dict[str, Any]]:
        """Return incident packages that are not marked resolved."""
        root = self.context.incidents_dir or (_project_root() / "data" / "incidents")
        if not root.exists():
            return []
        incidents: list[dict[str, Any]] = []
        for path in sorted(root.glob("*/incident.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            payload = _read_json(path, {})
            incident = dict(payload.get("incident") or {})
            if str(incident.get("status") or "open").lower() in {"closed", "resolved"}:
                continue
            incidents.append(
                {
                    **incident,
                    "path": str(path),
                    "markdown_path": str(path.with_name("incident.md")),
                    "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                }
            )
        return incidents

    def pause_trading(self, reason: str = "supervisor pause") -> dict[str, Any]:
        """Pause trading through supervisor state and the global kill switch."""
        set_kill_switch(True, user_id=self.user_id, reason=reason, data_dir=self.context.data_dir)
        state = {"mode": "paused", "reason": reason, "updated_at": _utc_now()}
        _write_json(_state_path(self.user_id, self.context.data_dir), state)
        return state

    def resume_paper_mode(self) -> dict[str, Any]:
        """Resume only paper mode; never enables live trading."""
        set_kill_switch(False, user_id=self.user_id, reason="resume_paper_mode", data_dir=self.context.data_dir)
        state = {"mode": "paper", "live_enabled": False, "updated_at": _utc_now()}
        _write_json(_state_path(self.user_id, self.context.data_dir), state)
        return state

    def run_preflight(self) -> dict[str, Any]:
        """Run readiness preflight checks."""
        report = run_preflight(config=self.context.config)
        return report.as_dict()

    def restart_algo(self) -> dict[str, Any]:
        """Invoke a configured restart callback for the supervised process."""
        if self.context.restart_callback is None:
            return {"restarted": False, "reason": "restart_callback_missing"}
        result = self.context.restart_callback()
        return {"restarted": True, "result": result}

    def run_incident_response(self) -> dict[str, Any]:
        """Run incident detection against current supervisor, health, risk, and logs."""
        health_status = self.get_health_status()
        risk_status = self.get_risk_status()
        return respond_to_incident(
            health_status=health_status,
            risk_status=risk_status,
            supervisor=self,
            log_paths=self._log_paths(),
            incidents_dir=self.context.incidents_dir,
            restart_callback=self.context.restart_callback,
        )

    def _approval_required(self, action: str) -> dict[str, Any]:
        return {
            "ok": False,
            "action": action,
            "approval_required": True,
            "message": f"{action} requires explicit operator approval and is disabled by default.",
        }

    def enable_live_trading(self, approved: bool = False) -> dict[str, Any]:
        """Approval-gated placeholder; never enables live trading without policy opt-in."""
        if not approved or not self.context.allow_approved_actions:
            return self._approval_required("enable_live_trading")
        state = {"mode": "live", "live_enabled": True, "updated_at": _utc_now()}
        _write_json(_state_path(self.user_id, self.context.data_dir), state)
        return {"ok": True, **state}

    def deploy_code(self, approved: bool = False) -> dict[str, Any]:
        """Approval-gated deployment hook."""
        if not approved or not self.context.allow_approved_actions:
            return self._approval_required("deploy_code")
        if self.context.deploy_callback is None:
            return {"ok": False, "reason": "deploy_callback_missing"}
        return {"ok": True, "result": self.context.deploy_callback()}

    def push_main_branch(self, approved: bool = False) -> dict[str, Any]:
        """Approval-gated git push for main."""
        if not approved or not self.context.allow_approved_actions:
            return self._approval_required("push_main_branch")
        result = subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=_project_root(),
            check=False,
            text=True,
            capture_output=True,
        )
        return {"ok": result.returncode == 0, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}

    def change_risk_limits(self, limits: Mapping[str, Any] | None = None, approved: bool = False) -> dict[str, Any]:
        """Approval-gated risk-limit mutation placeholder."""
        if not approved or not self.context.allow_approved_actions:
            return self._approval_required("change_risk_limits")
        return {
            "ok": False,
            "reason": "risk_limit_writes_not_configured",
            "requested_limits": dict(limits or {}),
        }


class SupervisorMCPServer:
    """Minimal MCP-style registry exposing supervisor methods as tools."""

    def __init__(self, supervisor: AlgoSupervisor) -> None:
        self.supervisor = supervisor
        self.tools: dict[str, Callable[..., Any]] = {
            "get_status": supervisor.get_status,
            "get_supervisor_summary": supervisor.get_supervisor_summary,
            "get_account_status": supervisor.get_account_status,
            "get_positions": supervisor.get_positions,
            "get_open_orders": supervisor.get_open_orders,
            "get_today_pnl": supervisor.get_today_pnl,
            "get_health_status": supervisor.get_health_status,
            "get_risk_status": supervisor.get_risk_status,
            "get_latest_premarket_report": supervisor.get_latest_premarket_report,
            "get_latest_daily_report": supervisor.get_latest_daily_report,
            "get_recent_logs": supervisor.get_recent_logs,
            "get_recent_errors": supervisor.get_recent_errors,
            "get_last_trade": supervisor.get_last_trade,
            "explain_last_trade": supervisor.explain_last_trade,
            "get_open_incidents": supervisor.get_open_incidents,
            "get_latest_reports": supervisor.get_latest_reports,
            "pause_trading": supervisor.pause_trading,
            "resume_paper_mode": supervisor.resume_paper_mode,
            "run_preflight": supervisor.run_preflight,
            "restart_algo": supervisor.restart_algo,
            "run_incident_response": supervisor.run_incident_response,
            "enable_live_trading": supervisor.enable_live_trading,
            "deploy_code": supervisor.deploy_code,
            "push_main_branch": supervisor.push_main_branch,
            "change_risk_limits": supervisor.change_risk_limits,
        }

    def call_tool(self, name: str, **kwargs: Any) -> Any:
        """Call a registered supervisor tool by name."""
        if name not in self.tools:
            raise KeyError(f"Unknown supervisor tool: {name}")
        return self.tools[name](**kwargs)

    def list_tools(self) -> list[str]:
        """List exposed tool names."""
        return sorted(self.tools)


__all__ = ["AlgoSupervisor", "SupervisorContext", "SupervisorMCPServer"]
