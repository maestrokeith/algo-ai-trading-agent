# AlgoSphere MCP Setup

AlgoSphere exposes a FastMCP server so ChatGPT can monitor account state, risk, reports, logs, and incidents without direct shell access.

## Install

```bash
pip install -r requirements.txt
```

Broker-backed tools require Alpaca credentials in the environment:

```bash
export APCA_API_KEY_ID=...
export APCA_API_SECRET_KEY=...
```

For live account reads, use `ALPACA_LIVE_API_KEY_ID` and `ALPACA_LIVE_API_SECRET_KEY` with a config that sets live mode. Do not store API keys in YAML.

## Start Locally

Default stdio MCP server:

```bash
PYTHONPATH=. python mcp_server.py
```

Start without connecting to Alpaca at boot:

```bash
PYTHONPATH=. python mcp_server.py --no-broker
```

Use a specific config and user label:

```bash
PYTHONPATH=. python mcp_server.py --config config/default.yaml --user-id live_bot
```

Validated read-only startup for the live bot supervisor:

```bash
PYTHONPATH=. python mcp_server.py --no-broker --config config/default.yaml --user-id live_bot
```

Start through the compatibility runner:

```bash
PYTHONPATH=. python scripts/run_mcp_supervisor.py serve --transport stdio
PYTHONPATH=. python scripts/run_mcp_supervisor.py serve --transport stdio --user-id live_bot
```

The JSON tool runner mode is still available for local smoke checks:

```bash
PYTHONPATH=. python scripts/run_mcp_supervisor.py list_tools --no-broker
PYTHONPATH=. python scripts/run_mcp_supervisor.py get_health_status --user-id live_bot --no-broker
PYTHONPATH=. python scripts/run_mcp_supervisor.py --config config/default.yaml get_health_status
PYTHONPATH=. python scripts/run_mcp_supervisor.py --config config/default.yaml get_recent_logs
PYTHONPATH=. python scripts/run_mcp_supervisor.py --config config/default.yaml get_recent_errors
```

When `config/users.yaml` exists and `--user-id` is omitted, `mcp_server.py` loads all configured users. Each MCP tool accepts an optional `user_id` argument; omitted means the registry default user.

If Alpaca credentials are missing or broker startup fails, the server still starts. Broker-backed tools return `available: false`, `reason: broker_missing`, and a broker error, while logs, reports, incidents, preflight, and tracker-backed positions remain available.

## Log Access

`get_recent_logs` first tails local `*.log` files from `logs/` and `data/logs/`. When file logs are unavailable, it falls back to:

```bash
journalctl -u algo.service -n 100 --no-pager --output=short-iso
```

`get_recent_errors` searches recent file or journal logs only for operational failure markers:

- `ERROR`
- `Traceback`
- `Exception`
- `TypeError`
- `ValueError`
- `order rejected`
- `insufficient qty`
- `broker failure`
- `failed health check`
- `service crash`

Normal scanner accounting such as `DYNAMIC_SCAN_BATCH ... rejected=50` is not an error.

The systemd service name defaults to `algo.service`. Override it either in config:

```yaml
mcp:
  log_service_name: algo.service
```

or with the environment:

```bash
export ALGOSPHERE_MCP_LOG_SERVICE=algo.service
```

## Tools

Monitoring tools:

- `list_users`
- `get_health_status`
- `get_account_status`
- `get_positions`
- `get_open_orders`
- `get_today_pnl`
- `get_risk_status`
- `get_latest_premarket_report`
- `get_latest_daily_report`
- `get_latest_reports`
- `get_recent_logs`
- `get_recent_errors`
- `get_last_trade`
- `explain_last_trade`
- `get_open_incidents`

Safe actions:

- `run_preflight`
- `run_incident_response`
- `pause_trading`
- `resume_paper_mode`
- `restart_algo`

Approval-required actions:

- `enable_live_trading`
- `deploy_code`
- `push_main_branch`
- `change_risk_limits`

Approval-required actions return `approval_required: true` by default. They only run when the caller passes approval and the server process is explicitly started with:

```bash
export ALGOSPHERE_MCP_ALLOW_APPROVED_ACTIONS=true
```

Leave this unset for normal ChatGPT monitoring.

## Remote Exposure

Preferred production pattern:

1. Run `mcp_server.py` on the trading host over stdio for local clients.
2. If remote access is required, put it behind a private tunnel or reverse proxy that terminates TLS.
3. Require strong authentication at the proxy layer.
4. Restrict source IPs to trusted networks.
5. Keep approval-required actions disabled unless an operator is supervising the session.

FastMCP transport can be selected with:

```bash
PYTHONPATH=. python mcp_server.py --transport streamable-http
```

Only expose HTTP transports behind authenticated TLS. Do not bind an unauthenticated MCP endpoint to the public internet.

## Authentication Guidance

MCP itself should not be treated as the auth boundary for this trading system. Use:

- OS account isolation for stdio mode.
- TLS for all remote traffic.
- Reverse proxy authentication such as OAuth, SSO, or mTLS.
- Separate read-only and operator profiles.
- Short-lived credentials for remote tunnels.
- Alpaca API keys stored only in environment variables or a secret manager.

For normal monitoring, give ChatGPT access to read tools and safe paper-mode actions only.
