# ChatGPT Integration

This guide connects ChatGPT to AlgoSphere through the FastMCP server in `mcp_server.py`.

## Connect ChatGPT

Use a local MCP client configuration that starts the server over stdio:

```json
{
  "mcpServers": {
    "algosphere": {
      "command": "python",
      "args": ["mcp_server.py"],
      "env": {
        "PYTHONPATH": "."
      }
    }
  }
}
```

For the read-only live-bot supervisor profile, start the server with:

```bash
PYTHONPATH=. python mcp_server.py --no-broker --config config/default.yaml --user-id live_bot
```

When the client runs outside `/opt/algosphere/algo-ai-trading-agent`, use absolute paths:

```json
{
  "mcpServers": {
    "algosphere": {
      "command": "python",
      "args": ["/opt/algosphere/algo-ai-trading-agent/mcp_server.py"],
      "env": {
        "PYTHONPATH": "/opt/algosphere/algo-ai-trading-agent"
      }
    }
  }
}
```

For remote ChatGPT access, expose the MCP server only through an authenticated TLS proxy or private tunnel. Keep `ALGOSPHERE_MCP_ALLOW_APPROVED_ACTIONS` unset unless an operator intentionally enables approval-gated operations.

When `config/users.yaml` is present, the MCP server loads all configured users by default. Call `list_users` first, then pass `user_id` to account, position, risk, log, incident, and action tools when you need a specific account. Starting with `--user-id live_bot` narrows the default to that account.

## What ChatGPT Can Monitor

The MCP server connects ChatGPT to:

- Supervisor: user discovery, health, account, positions, open orders, P/L, risk, pause/resume, restart hook, and preflight.
- Reports: latest pre-market and daily report content from `reports/` and `docs/`.
- Health monitor: broker connectivity and supervisor health checks.
- Incident response: open incident packages and incident detection workflow.
- Logs: recent log tails and filtered error lines.

`get_recent_errors` reports only operational failure markers: `ERROR`, `Traceback`, `Exception`, `TypeError`, `ValueError`, `order rejected`, `insufficient qty`, `broker failure`, `failed health check`, and `service crash`. Normal scanner rejection counts such as `DYNAMIC_SCAN_BATCH ... rejected=50` are intentionally ignored.

## Authentication Guidance

Use stdio mode for local ChatGPT clients whenever possible. For remote use:

- Put MCP behind TLS.
- Require SSO, OAuth, mTLS, or equivalent proxy authentication.
- Restrict inbound IPs.
- Keep broker keys in environment variables only.
- Use paper credentials for routine monitoring.
- Do not expose approval-required actions to unattended sessions.

## Example Prompts

```text
Check AlgoSphere health, risk status, open orders, and today's P/L. Summarize anything that needs operator attention.
```

```text
List users, then read the latest pre-market report and tell me whether paper_bot is ready to trade paper mode today.
```

```text
Show recent errors and open incidents for user_id paper_bot. If there is an operational issue, run preflight and explain the likely cause.
```

```text
Pause trading for maintenance, then confirm risk status and open orders.
```

```text
Explain the last realized trade and compare it with the latest daily report.
```

```text
Do not deploy or push. Check whether the account, positions, risk status, and logs look safe for continued paper trading.
```

## Operator Notes

`pause_trading` writes supervisor state and enables the global kill switch for the selected user. `resume_paper_mode` clears that kill switch and explicitly resumes paper mode only.

`enable_live_trading`, `deploy_code`, `push_main_branch`, and `change_risk_limits` are approval-required actions. By default they return an approval-required response and perform no mutation.

Local smoke checks:

```bash
PYTHONPATH=. python scripts/run_mcp_supervisor.py --config config/default.yaml get_health_status
PYTHONPATH=. python scripts/run_mcp_supervisor.py --config config/default.yaml get_recent_logs
PYTHONPATH=. python scripts/run_mcp_supervisor.py --config config/default.yaml get_recent_errors
```
