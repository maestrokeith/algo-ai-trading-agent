# Alpaca MCP

Existing repository MCP support lives in `mcp_server.py` and exposes Algo supervisor/status tooling. It can build an Alpaca broker when credentials are available, while degrading safely when credentials are missing.

This change adds `src/alpaca/mcp_adapter.py` as an agent-facing abstraction. It intentionally exposes market-context style access and does not expose unrestricted order placement.

Desired path:

```text
Agent -> Alpaca MCP / market tools -> TradeProposal -> deterministic policy -> controlled execution
```

Disallowed path:

```text
LLM -> place_order()
```

External Alpaca MCP tooling is not assumed to be installed. Until configured, the adapter returns `alpaca_mcp_not_configured`.
