"""FastMCP server for ChatGPT monitoring of AlgoSphere."""

from __future__ import annotations

import argparse
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from src.config_loader import deep_merge, load_config
from src.risk_book_mode import apply_risk_book_mode
from src.supervisor import DEFAULT_LOG_SERVICE_NAME, AlgoSupervisor, SupervisorContext

log = logging.getLogger(__name__)


PROJECT_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class SupervisorUser:
    """Resolved user metadata for MCP supervisor routing."""

    user_id: str
    config: dict[str, Any]
    paper: bool
    api_key: str = ""
    api_secret: str = ""
    broker_error: str | None = None


class SupervisorRegistry:
    """Route MCP tool calls to the supervisor for the requested user."""

    def __init__(self, supervisors: Mapping[str, AlgoSupervisor], *, default_user_id: str) -> None:
        if not supervisors:
            raise ValueError("At least one supervisor is required")
        self._supervisors = dict(supervisors)
        self.default_user_id = default_user_id if default_user_id in supervisors else next(iter(supervisors))

    def for_user(self, user_id: str | None = None) -> AlgoSupervisor:
        """Return the supervisor for *user_id*, or the default supervisor."""
        requested = (user_id or self.default_user_id).strip() or self.default_user_id
        try:
            return self._supervisors[requested]
        except KeyError:
            available = ", ".join(sorted(self._supervisors))
            raise KeyError(f"Unknown user_id '{requested}'. Available: {available}") from None

    def list_users(self) -> list[dict[str, Any]]:
        """Return available MCP users and broker availability."""
        users: list[dict[str, Any]] = []
        for user_id, supervisor in sorted(self._supervisors.items()):
            broker_cfg = supervisor.context.config.get("broker") or {}
            users.append(
                {
                    "user_id": user_id,
                    "default": user_id == self.default_user_id,
                    "paper": bool(broker_cfg.get("paper", True)),
                    "broker_available": supervisor.context.broker is not None,
                    "broker_error": supervisor.context.broker_error,
                }
            )
        return users


def _build_broker(
    config: Mapping[str, Any],
    *,
    api_key: str | None = None,
    api_secret: str | None = None,
    paper: bool | None = None,
) -> tuple[Any | None, str | None]:
    """Create the Alpaca broker when credentials are available; otherwise stay read-only."""
    try:
        from src.brokers.alpaca_client import AlpacaBroker

        return AlpacaBroker(dict(config), api_key=api_key, secret=api_secret, paper=paper), None
    except Exception as exc:
        log.warning("MCP broker unavailable; broker-backed tools will use fallbacks: %s", exc)
        return None, f"{type(exc).__name__}: {exc}"


def _users_path(path: str | None) -> Path:
    return Path(path) if path else PROJECT_ROOT / "config" / "users.yaml"


def _load_supervisor_users(
    base_config: Mapping[str, Any],
    *,
    users_path: str | None = None,
    selected_user_id: str | None = None,
    connect_broker: bool = True,
) -> list[SupervisorUser]:
    """Load user contexts for MCP without failing startup when credentials are absent."""
    path = _users_path(users_path)
    selected = (selected_user_id or "").strip()
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entries = raw.get("users") or []
        users: list[SupervisorUser] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            user_id = str(entry.get("id") or "").strip()
            if not user_id or (selected and user_id != selected):
                continue
            config = deep_merge(dict(base_config), dict(entry.get("overrides") or {}))
            broker_cfg = config.get("broker")
            if not isinstance(broker_cfg, dict):
                broker_cfg = {}
                config["broker"] = broker_cfg
            paper = bool(entry.get("paper", broker_cfg.get("paper", True)))
            broker_cfg["paper"] = paper
            apply_risk_book_mode(config)
            key_env = str(entry.get("alpaca_key_env") or "")
            secret_env = str(entry.get("alpaca_secret_env") or "")
            api_key = os.getenv(key_env, "") if key_env else ""
            api_secret = os.getenv(secret_env, "") if secret_env else ""
            broker_error = None
            if connect_broker and (not api_key or not api_secret):
                missing = [name for name, value in ((key_env, api_key), (secret_env, api_secret)) if name and not value]
                broker_error = f"missing_env:{','.join(missing)}" if missing else "missing_credentials"
            users.append(
                SupervisorUser(
                    user_id=user_id,
                    config=config,
                    paper=paper,
                    api_key=api_key,
                    api_secret=api_secret,
                    broker_error=broker_error,
                )
            )
        if users:
            return users
        if selected and selected != "default":
            log.warning("Requested MCP user_id %s was not found in %s; using single-user fallback", selected, path)

    config = dict(base_config)
    paper = bool((config.get("broker") or {}).get("paper", True))
    if paper:
        api_key = os.getenv("APCA_API_KEY_ID", "")
        api_secret = os.getenv("APCA_API_SECRET_KEY", "")
        missing_names = [name for name, value in (("APCA_API_KEY_ID", api_key), ("APCA_API_SECRET_KEY", api_secret)) if not value]
    else:
        api_key = os.getenv("ALPACA_LIVE_API_KEY_ID", "")
        api_secret = os.getenv("ALPACA_LIVE_API_SECRET_KEY", "")
        missing_names = [
            name for name, value in (("ALPACA_LIVE_API_KEY_ID", api_key), ("ALPACA_LIVE_API_SECRET_KEY", api_secret)) if not value
        ]
    broker_error = f"missing_env:{','.join(missing_names)}" if connect_broker and missing_names else None
    return [
        SupervisorUser(
            user_id=selected or "default",
            config=config,
            paper=paper,
            api_key=api_key,
            api_secret=api_secret,
            broker_error=broker_error,
        )
    ]


def build_supervisor(
    *,
    config_path: str | None = None,
    users_path: str | None = None,
    user_id: str = "default",
    connect_broker: bool = True,
) -> AlgoSupervisor:
    """Build the supervisor used by MCP tools."""
    registry = build_supervisor_registry(
        config_path=config_path,
        users_path=users_path,
        user_id=user_id,
        connect_broker=connect_broker,
    )
    return registry.for_user(user_id)


def build_supervisor_registry(
    *,
    config_path: str | None = None,
    users_path: str | None = None,
    user_id: str | None = None,
    connect_broker: bool = True,
) -> SupervisorRegistry:
    """Build all configured MCP supervisors."""
    base_config = load_config(config_path)
    users = _load_supervisor_users(
        base_config,
        users_path=users_path,
        selected_user_id=user_id,
        connect_broker=connect_broker,
    )
    supervisors: dict[str, AlgoSupervisor] = {}
    process_start_ts = time.time()
    for user in users:
        broker = None
        broker_error = user.broker_error
        if connect_broker and broker_error is None:
            broker, broker_error = _build_broker(
                user.config,
                api_key=user.api_key or None,
                api_secret=user.api_secret or None,
                paper=user.paper,
            )
        mcp_cfg = user.config.get("mcp") if isinstance(user.config.get("mcp"), Mapping) else {}
        log_service_name = (
            os.getenv("ALGOSPHERE_MCP_LOG_SERVICE", "").strip()
            or str(mcp_cfg.get("log_service_name") or mcp_cfg.get("service_name") or "").strip()
            or DEFAULT_LOG_SERVICE_NAME
        )
        context = SupervisorContext(
            config=user.config,
            broker=broker,
            reports_dir=PROJECT_ROOT / "reports",
            data_dir=PROJECT_ROOT / "data",
            logs_dir=PROJECT_ROOT / "logs",
            incidents_dir=PROJECT_ROOT / "data" / "incidents",
            log_service_name=log_service_name,
            broker_error=broker_error,
            process_start_ts=process_start_ts,
            allow_approved_actions=os.getenv("ALGOSPHERE_MCP_ALLOW_APPROVED_ACTIONS", "").lower()
            in {"1", "true", "yes"},
        )
        supervisors[user.user_id] = AlgoSupervisor(context, user_id=user.user_id)
    return SupervisorRegistry(supervisors, default_user_id=user_id or users[0].user_id)


def create_mcp_app(supervisor: AlgoSupervisor | None = None, registry: SupervisorRegistry | None = None) -> Any:
    """Create and register the FastMCP application."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("Install MCP support with `pip install -r requirements.txt`.") from exc

    registry = registry or (
        SupervisorRegistry({supervisor.user_id: supervisor}, default_user_id=supervisor.user_id)
        if supervisor
        else build_supervisor_registry()
    )
    app = FastMCP("algosphere")

    @app.tool()
    def list_users() -> list[dict[str, Any]]:
        """Return configured supervisor users and broker availability."""
        return registry.list_users()

    @app.tool()
    def get_status(user_id: str | None = None) -> dict[str, Any]:
        """Return compact read-only bot status."""
        return registry.for_user(user_id).get_status()

    @app.tool()
    def get_supervisor_summary(
        max_lines: int = 100,
        user_id: str | None = None,
        since_minutes: int | None = 60,
    ) -> dict[str, Any]:
        """Return a consolidated read-only supervisor summary."""
        return registry.for_user(user_id).get_supervisor_summary(
            max_lines=max_lines,
            since_minutes=since_minutes,
        )

    @app.tool()
    def get_health_status(user_id: str | None = None) -> dict[str, Any]:
        """Return runtime and broker health."""
        return registry.for_user(user_id).get_health_status()

    @app.tool()
    def get_account_status(user_id: str | None = None) -> dict[str, Any]:
        """Return Alpaca account status when broker credentials are configured."""
        return registry.for_user(user_id).get_account_status()

    @app.tool()
    def get_positions(user_id: str | None = None) -> list[dict[str, Any]]:
        """Return current broker positions or persisted tracker positions."""
        return registry.for_user(user_id).get_positions()

    @app.tool()
    def get_open_orders(user_id: str | None = None) -> list[dict[str, Any]]:
        """Return open broker orders."""
        return registry.for_user(user_id).get_open_orders()

    @app.tool()
    def get_today_pnl(user_id: str | None = None) -> dict[str, Any]:
        """Return today's realized and account-level P/L."""
        return registry.for_user(user_id).get_today_pnl()

    @app.tool()
    def get_risk_status(user_id: str | None = None) -> dict[str, Any]:
        """Return current global risk status."""
        return registry.for_user(user_id).get_risk_status()

    @app.tool()
    def get_latest_premarket_report(user_id: str | None = None) -> dict[str, Any]:
        """Return the latest pre-market report."""
        return registry.for_user(user_id).get_latest_premarket_report()

    @app.tool()
    def get_latest_daily_report(user_id: str | None = None) -> dict[str, Any]:
        """Return the latest daily report."""
        return registry.for_user(user_id).get_latest_daily_report()

    @app.tool()
    def get_latest_reports(limit: int = 5, user_id: str | None = None) -> list[dict[str, Any]]:
        """Return latest report artifacts."""
        return registry.for_user(user_id).get_latest_reports(limit=limit)

    @app.tool()
    def get_recent_logs(
        max_lines: int = 100,
        user_id: str | None = None,
        since_minutes: int | None = None,
        component: str | None = None,
        severity: str | None = None,
        text: str | None = None,
    ) -> dict[str, Any]:
        """Return recent log tails."""
        return registry.for_user(user_id).get_recent_logs(
            max_lines=max_lines,
            since_minutes=since_minutes,
            component=component,
            severity=severity,
            text=text,
        )

    @app.tool()
    def get_recent_errors(
        max_lines: int = 100,
        user_id: str | None = None,
        since_minutes: int | None = None,
        severity: str | None = None,
        component: str | None = None,
        text: str | None = None,
    ) -> dict[str, Any]:
        """Return recent error log lines."""
        return registry.for_user(user_id).get_recent_errors(
            max_lines=max_lines,
            since_minutes=since_minutes,
            severity=severity,
            component=component,
            text=text,
        )

    @app.tool()
    def get_last_trade(user_id: str | None = None) -> dict[str, Any]:
        """Return the latest trade row."""
        return registry.for_user(user_id).get_last_trade()

    @app.tool()
    def explain_last_trade(user_id: str | None = None) -> dict[str, Any]:
        """Explain the latest realized trade."""
        return registry.for_user(user_id).explain_last_trade()

    @app.tool()
    def get_open_incidents(user_id: str | None = None) -> list[dict[str, Any]]:
        """Return open incident packages."""
        return registry.for_user(user_id).get_open_incidents()

    @app.tool()
    def run_preflight(user_id: str | None = None) -> dict[str, Any]:
        """Run operational preflight checks."""
        return registry.for_user(user_id).run_preflight()

    @app.tool()
    def run_incident_response(user_id: str | None = None) -> dict[str, Any]:
        """Run incident detection and package any actionable incident."""
        return registry.for_user(user_id).run_incident_response()

    @app.tool()
    def pause_trading(reason: str = "MCP operator pause", user_id: str | None = None) -> dict[str, Any]:
        """Pause trading through the supervisor state and kill switch."""
        return registry.for_user(user_id).pause_trading(reason=reason)

    @app.tool()
    def resume_paper_mode(user_id: str | None = None) -> dict[str, Any]:
        """Resume paper mode only."""
        return registry.for_user(user_id).resume_paper_mode()

    @app.tool()
    def restart_algo(user_id: str | None = None) -> dict[str, Any]:
        """Restart the supervised process when a restart callback is configured."""
        return registry.for_user(user_id).restart_algo()

    @app.tool()
    def enable_live_trading(approved: bool = False, user_id: str | None = None) -> dict[str, Any]:
        """Approval-required action: enable live trading."""
        return registry.for_user(user_id).enable_live_trading(approved=approved)

    @app.tool()
    def deploy_code(approved: bool = False, user_id: str | None = None) -> dict[str, Any]:
        """Approval-required action: deploy code."""
        return registry.for_user(user_id).deploy_code(approved=approved)

    @app.tool()
    def push_main_branch(approved: bool = False, user_id: str | None = None) -> dict[str, Any]:
        """Approval-required action: push main branch."""
        return registry.for_user(user_id).push_main_branch(approved=approved)

    @app.tool()
    def change_risk_limits(
        limits: dict[str, Any] | None = None,
        approved: bool = False,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Approval-required action: change risk limits."""
        return registry.for_user(user_id).change_risk_limits(limits=limits, approved=approved)

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the AlgoSphere FastMCP server")
    parser.add_argument("--config", default=None, help="Optional YAML config path")
    parser.add_argument("--users", default=None, help="Optional users.yaml path")
    parser.add_argument("--user-id", default=None, help="Default supervisor user id; omit to load all configured users")
    parser.add_argument("--no-broker", action="store_true", help="Do not initialize Alpaca broker at startup")
    parser.add_argument(
        "--transport",
        default=os.getenv("ALGOSPHERE_MCP_TRANSPORT", "stdio"),
        choices=("stdio", "sse", "streamable-http"),
        help="FastMCP transport",
    )
    args = parser.parse_args()

    logging.basicConfig(level=os.getenv("ALGOSPHERE_MCP_LOG_LEVEL", "INFO"))
    registry = build_supervisor_registry(
        config_path=args.config,
        users_path=args.users,
        user_id=args.user_id,
        connect_broker=not args.no_broker,
    )
    app = create_mcp_app(registry=registry)
    app.run(transport=args.transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
