"""Shared dynamic universe price safety helpers."""
from __future__ import annotations

from typing import Any, Mapping


def _dynamic_universe_cfg(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    nested = config.get("dynamic_universe")
    if isinstance(nested, Mapping):
        return nested
    return config


def effective_dynamic_min_price(
    config: Mapping[str, Any] | None,
    *,
    broker_is_paper: bool | None = None,
) -> float:
    """Return the dynamic minimum price enforced by scanner and execution."""
    du_cfg = _dynamic_universe_cfg(config)
    try:
        configured = float(du_cfg.get("min_price", 5.0) or 5.0)
    except (TypeError, ValueError):
        configured = 5.0
    configured = max(0.0, configured)
    if broker_is_paper is None:
        if "broker_is_paper" in du_cfg:
            broker_is_paper = bool(du_cfg.get("broker_is_paper"))
        elif "_broker_is_paper" in du_cfg:
            broker_is_paper = bool(du_cfg.get("_broker_is_paper"))
        else:
            broker_cfg = config.get("broker") if isinstance(config, Mapping) else {}
            broker_is_paper = (
                bool(broker_cfg.get("paper"))
                if isinstance(broker_cfg, Mapping)
                else True
            )
    if broker_is_paper:
        return configured
    return max(5.0, configured)
