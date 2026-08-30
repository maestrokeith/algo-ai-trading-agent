"""Central broker selection."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from .base import BrokerUnavailable


log = logging.getLogger(__name__)


def broker_provider(config: Mapping[str, Any] | None, explicit: str | None = None) -> str:
    if explicit:
        return str(explicit).strip().lower()
    broker_cfg = (config or {}).get("broker")
    broker_cfg = broker_cfg if isinstance(broker_cfg, Mapping) else {}
    provider = broker_cfg.get("provider") or broker_cfg.get("firm") or "alpaca"
    return str(provider or "alpaca").strip().lower()


def get_broker(
    config: Mapping[str, Any] | None,
    *,
    provider: str | None = None,
    api_key: str | None = None,
    secret: str | None = None,
    paper: bool | None = None,
) -> Any:
    selected = broker_provider(config, provider)
    if selected == "alpaca":
        from .alpaca_client import AlpacaBroker

        return AlpacaBroker(config=dict(config or {}), api_key=api_key, secret=secret, paper=paper)
    raise BrokerUnavailable(f"Unsupported broker provider: {selected}")
