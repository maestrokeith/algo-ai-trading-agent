"""Baseline dollar risk per v2 (before symbol-level caps)."""
from __future__ import annotations

from typing import Any


def position_size_v2(equity: float, regime_mult: float, cfg: dict[str, Any] | None = None) -> float:
    """
    ``base = equity * portfolio.base_position_pct * regime_mult``

    *regime_mult* is typically :func:`regime_long_mult_for_score`.
    """
    v2 = (cfg or {}).get("strategy_v2") or {}
    p = v2.get("portfolio") or {}
    base_pct = float(p.get("base_position_pct", 0.06))
    return max(0.0, float(equity) * base_pct * float(regime_mult))
