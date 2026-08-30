"""Config and policy parsing surface for the capital allocator split."""

from __future__ import annotations

from src.portfolio.allocator_planner import (
    parse_capital_allocator_cfg,
    parse_defensive_drift_cfg,
)

__all__ = [
    "parse_capital_allocator_cfg",
    "parse_defensive_drift_cfg",
]
