"""Alpaca MCP abstraction for agents.

This adapter intentionally exposes market-data style methods only. Order
placement remains routed through deterministic policy and the existing broker.
"""

from __future__ import annotations

from typing import Any, Protocol


class AlpacaMCPClient(Protocol):
    def get_market_context(self, symbol: str) -> dict[str, Any]:
        ...


class DisabledAlpacaMCPClient:
    def get_market_context(self, symbol: str) -> dict[str, Any]:
        return {"symbol": symbol.upper(), "available": False, "reason": "alpaca_mcp_not_configured"}


def alpaca_mcp_from_config(config: dict[str, Any] | None) -> AlpacaMCPClient:
    cfg = ((config or {}).get("agents") or {}).get("alpaca_mcp") or {}
    if not bool(cfg.get("enabled", False)):
        return DisabledAlpacaMCPClient()
    return DisabledAlpacaMCPClient()
