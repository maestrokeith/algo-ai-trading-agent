"""Inverse hedge symbol and position checks (no strategy_v2 / entry_router imports)."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from src.position_tracker import tracked_row_has_open_long


def hedge_symbol(cfg: dict[str, Any] | None = None) -> str:
    """Configured inverse ticker (default ``SQQQ``)."""
    v2 = (cfg or {}).get("strategy_v2") or {}
    h = v2.get("hedging") or {}
    return str(h.get("symbol", "SQQQ")).upper()


def long_hedge_position_held(
    cfg: dict[str, Any] | None,
    positions: Sequence[Mapping[str, Any]],
    tracked: Mapping[str, Any],
) -> bool:
    """True if broker shows positive qty or tracker row has open long (qty or notional)."""
    sym = hedge_symbol(cfg)
    for p in positions:
        if str(p.get("symbol") or "").upper() == sym and float(p.get("qty") or 0) > 0:
            return True
    row = tracked.get(sym) if isinstance(tracked, dict) else None
    return tracked_row_has_open_long(row if isinstance(row, dict) else None)
