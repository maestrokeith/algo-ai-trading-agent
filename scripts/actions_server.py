#!/usr/bin/env python3
"""Read-only FastAPI Actions wrapper for ChatGPT Custom GPT Actions."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mcp_server import SupervisorRegistry, build_supervisor_registry
from src.profitability_attribution import load_trade_churn_analysis


DEFAULT_ACTIONS_SERVER_URL = "https://example.ngrok-free.app"


@dataclass(frozen=True)
class ActionQuery:
    """Common optional query parameters accepted by read-only Actions routes."""

    user_id: str | None = None
    since_minutes: int | None = None
    severity: str | None = None
    component: str | None = None
    text: str | None = None
    max_lines: int | None = None
    day: str | None = None


def action_query(
    user_id: str | None = Query(default=None, description="Optional configured AlgoSphere user id."),
    since_minutes: int | None = Query(default=None, ge=0, description="Only include log lines this many minutes old."),
    severity: str | None = Query(default=None, description="Minimum log severity such as WARNING or ERROR."),
    component: str | None = Query(default=None, description="Log component filter."),
    text: str | None = Query(default=None, description="Case-insensitive text filter for log lines."),
    max_lines: int | None = Query(default=None, ge=1, le=1000, description="Maximum lines per log source."),
    day: str | None = Query(default=None, description="Artifact date YYYY-MM-DD."),
) -> ActionQuery:
    """Return normalized read-only Action query parameters."""
    return ActionQuery(
        user_id=user_id,
        since_minutes=since_minutes,
        severity=severity,
        component=component,
        text=text,
        max_lines=max_lines,
        day=day,
    )


def _supervisor(registry: SupervisorRegistry, user_id: str | None) -> Any:
    try:
        return registry.for_user(user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc


def build_actions_registry(
    *,
    config_path: str | None = None,
    users_path: str | None = None,
    user_id: str | None = None,
    connect_broker: bool | None = None,
) -> SupervisorRegistry:
    """Build the supervisor registry for read-only Actions endpoints."""
    should_connect_broker = (
        os.getenv("ALGOSPHERE_ACTIONS_CONNECT_BROKER", "").lower() in {"1", "true", "yes"}
        if connect_broker is None
        else connect_broker
    )
    return build_supervisor_registry(
        config_path=config_path or os.getenv("ALGOSPHERE_ACTIONS_CONFIG"),
        users_path=users_path or os.getenv("ALGOSPHERE_ACTIONS_USERS"),
        user_id=user_id or os.getenv("ALGOSPHERE_ACTIONS_USER_ID"),
        connect_broker=should_connect_broker,
    )


def create_actions_app(registry: SupervisorRegistry | None = None) -> FastAPI:
    """Create the read-only FastAPI app used by ChatGPT Actions."""
    registry = registry or build_actions_registry()
    app = FastAPI(
        title="AlgoSphere Read-Only Actions",
        version="0.1.0",
        description="Read-only HTTP wrapper around AlgoSphere supervisor status, summary, logs, and errors.",
        servers=[
            {
                "url": os.getenv("ALGOSPHERE_ACTIONS_SERVER_URL", DEFAULT_ACTIONS_SERVER_URL),
                "description": "Public Actions endpoint",
            }
        ],
    )

    @app.get("/health", tags=["actions"])
    async def health(query: ActionQuery = Depends(action_query)) -> dict[str, Any]:
        """Return runtime health for the selected supervisor user."""
        return _supervisor(registry, query.user_id).get_health_status()

    @app.get("/status", tags=["actions"])
    async def status(query: ActionQuery = Depends(action_query)) -> dict[str, Any]:
        """Return compact read-only bot status."""
        return _supervisor(registry, query.user_id).get_status()

    @app.get("/summary", tags=["actions"])
    async def summary(query: ActionQuery = Depends(action_query)) -> dict[str, Any]:
        """Return a consolidated read-only supervisor summary."""
        return _supervisor(registry, query.user_id).get_supervisor_summary(
            max_lines=query.max_lines or 100,
            since_minutes=60 if query.since_minutes is None else query.since_minutes,
        )

    @app.get("/errors", tags=["actions"])
    async def errors(query: ActionQuery = Depends(action_query)) -> dict[str, Any]:
        """Return recent operational error log lines."""
        return _supervisor(registry, query.user_id).get_recent_errors(
            max_lines=query.max_lines or 100,
            since_minutes=query.since_minutes,
            severity=query.severity,
            component=query.component,
            text=query.text,
        )

    @app.get("/logs", tags=["actions"])
    async def logs(query: ActionQuery = Depends(action_query)) -> dict[str, Any]:
        """Return recent log lines."""
        return _supervisor(registry, query.user_id).get_recent_logs(
            max_lines=query.max_lines or 100,
            since_minutes=query.since_minutes,
            severity=query.severity,
            component=query.component,
            text=query.text,
        )

    @app.get("/churn", tags=["actions"])
    async def churn(query: ActionQuery = Depends(action_query)) -> dict[str, Any]:
        """Return daily churn analysis from local artifacts."""
        user_id = query.user_id or getattr(registry, "default_user_id", "default")
        data_dir = Path(os.getenv("ALGOSPHERE_ACTIONS_DATA_DIR", PROJECT_ROOT / "data"))
        return load_trade_churn_analysis(
            data_dir=data_dir,
            user_id=user_id,
            day=query.day or date.today().isoformat(),
        )

    return app


app = create_actions_app()


def main() -> int:
    """Run the read-only Actions API server."""
    parser = argparse.ArgumentParser(description="Run the AlgoSphere read-only Actions server")
    parser.add_argument("--host", default=os.getenv("ACTIONS_HOST", "0.0.0.0"), help="Bind host")
    parser.add_argument("--port", type=int, default=int(os.getenv("ACTIONS_PORT", "8010")), help="Bind port")
    parser.add_argument("--config", default=None, help="Optional YAML config path")
    parser.add_argument("--users", default=None, help="Optional users.yaml path")
    parser.add_argument("--user-id", default=None, help="Default supervisor user id")
    parser.add_argument(
        "--connect-broker",
        action="store_true",
        help="Enable broker read APIs when credentials are configured",
    )
    args = parser.parse_args()

    registry = build_actions_registry(
        config_path=args.config,
        users_path=args.users,
        user_id=args.user_id,
        connect_broker=args.connect_broker,
    )
    uvicorn.run(create_actions_app(registry=registry), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
