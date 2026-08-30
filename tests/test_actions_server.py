from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import HTTPException

import mcp_server
from scripts.actions_server import ActionQuery, create_actions_app
from src.trade_attribution import record_exit, record_order_event


class RecordingSupervisor:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_health_status(self) -> dict[str, Any]:
        self.calls.append(("get_health_status", {}))
        return {"available": True, "ok": True, "user_id": self.user_id}

    def get_status(self) -> dict[str, Any]:
        self.calls.append(("get_status", {}))
        return {"available": True, "ok": True, "user_id": self.user_id}

    def get_supervisor_summary(self, *, max_lines: int, since_minutes: int | None) -> dict[str, Any]:
        args = {"max_lines": max_lines, "since_minutes": since_minutes}
        self.calls.append(("get_supervisor_summary", args))
        return {"available": True, "user_id": self.user_id, **args}

    def get_recent_errors(
        self,
        *,
        max_lines: int,
        since_minutes: int | None,
        severity: str | None,
        component: str | None,
        text: str | None,
    ) -> dict[str, Any]:
        args = {
            "max_lines": max_lines,
            "since_minutes": since_minutes,
            "severity": severity,
            "component": component,
            "text": text,
        }
        self.calls.append(("get_recent_errors", args))
        return {"available": True, "user_id": self.user_id, "errors": [], **args}

    def get_recent_logs(
        self,
        *,
        max_lines: int,
        since_minutes: int | None,
        severity: str | None,
        component: str | None,
        text: str | None,
    ) -> dict[str, Any]:
        args = {
            "max_lines": max_lines,
            "since_minutes": since_minutes,
            "severity": severity,
            "component": component,
            "text": text,
        }
        self.calls.append(("get_recent_logs", args))
        return {"available": True, "user_id": self.user_id, "logs": [], **args}


def _route(app: Any, path: str) -> Any:
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return route
    raise AssertionError(f"Missing route: {path}")


def _call(app: Any, path: str, query: ActionQuery | None = None) -> dict[str, Any]:
    return asyncio.run(_route(app, path).endpoint(query or ActionQuery()))


def _app() -> tuple[Any, RecordingSupervisor, RecordingSupervisor]:
    first = RecordingSupervisor("first")
    second = RecordingSupervisor("second")
    registry = mcp_server.SupervisorRegistry({"first": first, "second": second}, default_user_id="first")
    return create_actions_app(registry=registry), first, second


def test_actions_health_status_summary_errors_and_logs_endpoints() -> None:
    app, first, second = _app()

    assert _call(app, "/health") == {"available": True, "ok": True, "user_id": "first"}
    assert _call(app, "/status", ActionQuery(user_id="second"))["user_id"] == "second"

    summary = _call(
        app,
        "/summary",
        ActionQuery(user_id="second", since_minutes=15, max_lines=7),
    )
    assert summary == {"available": True, "user_id": "second", "max_lines": 7, "since_minutes": 15}

    query = ActionQuery(
        user_id="second",
        since_minutes=30,
        severity="ERROR",
        component="broker",
        text="order",
        max_lines=5,
    )
    errors = _call(app, "/errors", query)
    logs = _call(app, "/logs", query)

    expected_filter_args = {
        "max_lines": 5,
        "since_minutes": 30,
        "severity": "ERROR",
        "component": "broker",
        "text": "order",
    }
    assert errors == {"available": True, "user_id": "second", "errors": [], **expected_filter_args}
    assert logs == {"available": True, "user_id": "second", "logs": [], **expected_filter_args}
    assert first.calls == [("get_health_status", {})]
    assert second.calls == [
        ("get_status", {}),
        ("get_supervisor_summary", {"max_lines": 7, "since_minutes": 15}),
        ("get_recent_errors", expected_filter_args),
        ("get_recent_logs", expected_filter_args),
    ]


def test_actions_defaults_and_unknown_user_are_json() -> None:
    app, _first, _second = _app()

    summary = _call(app, "/summary")
    errors = _call(app, "/errors")
    logs = _call(app, "/logs")

    assert summary["max_lines"] == 100
    assert summary["since_minutes"] == 60
    assert errors["max_lines"] == 100
    assert errors["since_minutes"] is None
    assert logs["max_lines"] == 100
    assert logs["since_minutes"] is None

    with pytest.raises(HTTPException) as excinfo:
        _call(app, "/status", ActionQuery(user_id="missing"))
    assert excinfo.value.status_code == 404
    assert "Unknown user_id" in excinfo.value.detail


def test_actions_openapi_docs_only_include_read_only_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    configured_url = "https://b40d-2605-a601-8130-9800-00-3.ngrok-free.app"
    monkeypatch.setenv("ALGOSPHERE_ACTIONS_SERVER_URL", configured_url)
    app, _first, _second = _app()

    schema = app.openapi()
    assert schema["info"]["title"] == "AlgoSphere Read-Only Actions"
    assert schema["servers"][0]["url"] == configured_url
    assert schema["servers"][0]["description"] == "Public Actions endpoint"
    assert set(schema["paths"]) >= {"/health", "/status", "/summary", "/errors", "/logs"}
    assert _route(app, "/docs") is not None

    forbidden_terms = (
        "run_preflight",
        "run_incident_response",
        "restart",
        "pause",
        "resume",
        "buy",
        "sell",
        "trading",
    )
    schema_text = str(schema).lower()
    for term in forbidden_terms:
        assert term not in schema_text


def test_actions_churn_endpoint_reads_local_artifacts(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 6, 6, 15, 30, tzinfo=timezone.utc)
    record_order_event(
        data_dir=tmp_path,
        user_id="second",
        timestamp=now,
        symbol="AAPL",
        action="buy",
        order_build_status="built",
        submitted=True,
    )
    record_exit(
        data_dir=tmp_path,
        user_id="second",
        timestamp=now,
        symbol="AAPL",
        exit_reason="signal_flip",
        pnl=-5.0,
        pnl_pct=-0.7,
        hold_minutes=9,
        entry_route="core_rebuild",
    )
    replay_dir = tmp_path / "replay_market_session"
    replay_dir.mkdir(parents=True)
    (replay_dir / "2026-06-06_second.json").write_text(
        '{"mock_orders": [{"symbol": "AAPL", "side": "sell"}]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("ALGOSPHERE_ACTIONS_DATA_DIR", str(tmp_path))
    app, _first, _second = _app()

    churn = _call(app, "/churn", ActionQuery(user_id="second", day="2026-06-06"))

    assert churn["user_id"] == "second"
    assert churn["same_day_reversals"]["symbols"] == ["AAPL"]
    assert churn["weak_exits"]["by_route"] == {"core_rebuild": 1}
