from __future__ import annotations

import sys
import types

import mcp_server
from src.supervisor import AlgoSupervisor, SupervisorContext


class FakeFastMCP:
    def __init__(self, name: str) -> None:
        self.name = name
        self.tools = {}
        self.transport = None

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator

    def run(self, *, transport: str = "stdio") -> None:
        self.transport = transport


def test_create_mcp_app_registers_required_tools(monkeypatch, tmp_path) -> None:
    fastmcp_module = types.ModuleType("mcp.server.fastmcp")
    fastmcp_module.FastMCP = FakeFastMCP
    server_module = types.ModuleType("mcp.server")
    mcp_module = types.ModuleType("mcp")
    monkeypatch.setitem(sys.modules, "mcp", mcp_module)
    monkeypatch.setitem(sys.modules, "mcp.server", server_module)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_module)

    supervisor = AlgoSupervisor(SupervisorContext(config={"broker": {"paper": True}}, data_dir=tmp_path))
    app = mcp_server.create_mcp_app(supervisor)

    expected = {
        "list_users",
        "get_status",
        "get_supervisor_summary",
        "get_health_status",
        "get_account_status",
        "get_positions",
        "get_open_orders",
        "get_today_pnl",
        "get_risk_status",
        "get_latest_premarket_report",
        "get_latest_daily_report",
        "get_latest_reports",
        "get_recent_logs",
        "get_recent_errors",
        "get_last_trade",
        "explain_last_trade",
        "get_open_incidents",
        "run_preflight",
        "run_incident_response",
        "pause_trading",
        "resume_paper_mode",
        "restart_algo",
        "enable_live_trading",
        "deploy_code",
        "push_main_branch",
        "change_risk_limits",
    }
    assert set(app.tools) == expected
    assert app.tools["get_status"]()["available"] is True
    assert app.tools["get_supervisor_summary"]()["available"] is True
    assert app.tools["get_positions"]() == []
    assert app.tools["enable_live_trading"](approved=True)["approval_required"] is True


def test_registry_loads_users_without_credentials(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "default.yaml"
    config_path.write_text(
        """
broker:
  paper: true
strategy:
  enabled: true
portfolio_risk:
  max_daily_loss_pct: 2
""",
        encoding="utf-8",
    )
    users_path = tmp_path / "users.yaml"
    users_path.write_text(
        """
users:
  - id: alice
    alpaca_key_env: ALICE_KEY
    alpaca_secret_env: ALICE_SECRET
    paper: true
  - id: bob
    alpaca_key_env: BOB_KEY
    alpaca_secret_env: BOB_SECRET
    paper: false
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("ALICE_KEY", raising=False)
    monkeypatch.delenv("ALICE_SECRET", raising=False)
    monkeypatch.delenv("BOB_KEY", raising=False)
    monkeypatch.delenv("BOB_SECRET", raising=False)

    registry = mcp_server.build_supervisor_registry(
        config_path=str(config_path),
        users_path=str(users_path),
        connect_broker=True,
    )

    users = registry.list_users()
    assert [row["user_id"] for row in users] == ["alice", "bob"]
    assert users[0]["broker_available"] is False
    assert "ALICE_KEY" in users[0]["broker_error"]
    assert registry.for_user("bob").get_account_status()["reason"] == "broker_missing"


def test_registry_sets_log_service_from_config_and_env(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "default.yaml"
    config_path.write_text(
        """
broker:
  paper: true
mcp:
  log_service_name: configured.service
""",
        encoding="utf-8",
    )

    monkeypatch.delenv("ALGOSPHERE_MCP_LOG_SERVICE", raising=False)
    registry = mcp_server.build_supervisor_registry(
        config_path=str(config_path),
        users_path=str(tmp_path / "missing-users.yaml"),
        connect_broker=False,
    )
    assert registry.for_user().context.log_service_name == "configured.service"

    monkeypatch.setenv("ALGOSPHERE_MCP_LOG_SERVICE", "env.service")
    registry = mcp_server.build_supervisor_registry(
        config_path=str(config_path),
        users_path=str(tmp_path / "missing-users.yaml"),
        connect_broker=False,
    )
    assert registry.for_user().context.log_service_name == "env.service"


def test_default_single_user_fallback_is_quiet(tmp_path, monkeypatch, caplog) -> None:
    config_path = tmp_path / "default.yaml"
    config_path.write_text("broker:\n  paper: true\n", encoding="utf-8")
    users_path = tmp_path / "users.yaml"
    users_path.write_text("users: []\n", encoding="utf-8")

    mcp_server.build_supervisor_registry(
        config_path=str(config_path),
        users_path=str(users_path),
        user_id="default",
        connect_broker=False,
    )

    assert "using single-user fallback" not in caplog.text


def test_mcp_tools_route_by_user_id(monkeypatch, tmp_path) -> None:
    fastmcp_module = types.ModuleType("mcp.server.fastmcp")
    fastmcp_module.FastMCP = FakeFastMCP
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_module)

    first = AlgoSupervisor(SupervisorContext(config={"broker": {"paper": True}}, data_dir=tmp_path), user_id="first")
    second = AlgoSupervisor(SupervisorContext(config={"broker": {"paper": False}}, data_dir=tmp_path), user_id="second")
    registry = mcp_server.SupervisorRegistry({"first": first, "second": second}, default_user_id="first")
    app = mcp_server.create_mcp_app(registry=registry)

    assert app.tools["get_account_status"]()["user_id"] == "first"
    assert app.tools["get_account_status"](user_id="second")["user_id"] == "second"
