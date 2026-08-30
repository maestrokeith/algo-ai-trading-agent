"""Legacy compatibility exit helpers.

This module intentionally remains small and standalone for tests and simple
utility imports. The primary live exit orchestration now lives in
``src/live/exits.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _unrealized_plpc_fraction(position: Mapping[str, Any]) -> float:
    """P/L as a fraction of cost (e.g. 0.04 == +4%); 0.0 if unknown."""
    for k in ("unrealized_plpc", "unrealized_intraday_plpc"):
        raw = position.get(k)
        if raw is not None and str(raw).strip() != "":
            try:
                return float(raw)
            except (TypeError, ValueError):
                return 0.0
    ur = position.get("unrealized_pl")
    mv = position.get("market_value")
    try:
        if ur is not None and mv not in (None, 0, "", "0") and float(mv) != 0:
            return float(ur) / float(mv)
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    return 0.0


def check_partial_exit(
    position: Mapping[str, Any],
    *,
    partial_trim_trigger_pct: float | None = None,
    sell_fraction: float | None = None,
) -> float:
    """
    Return a **fraction of the position** to sell as a discretionary partial (0.0–1.0).

    Uses ``unrealized_plpc`` (Alpaca-style fractional P/L) × 100 as **pnl_pct**.

    When *partial_trim_trigger_pct* is set (from ``exit.partial_trim_trigger_pct`` / ``strategy.exits``),
    returns *sell_fraction* only if ``pnl_pct >= partial_trim_trigger_pct`` — trim only on **meaningful**
    profit (live broker row). Otherwise legacy thresholds: ``> 4`` or ``> 2`` %% → ``0.3``.
    """
    pnl_pct = _unrealized_plpc_fraction(position) * 100.0
    if partial_trim_trigger_pct is not None:
        try:
            thr = float(partial_trim_trigger_pct)
        except (TypeError, ValueError):
            thr = 0.0
        if thr <= 0 or pnl_pct < thr:
            return 0.0
        try:
            sf = 0.3 if sell_fraction is None else float(sell_fraction)
        except (TypeError, ValueError):
            sf = 0.3
        return max(0.0, min(1.0, sf))
    if pnl_pct > 4:
        return 0.3
    if pnl_pct > 2:
        return 0.3
    return 0.0
