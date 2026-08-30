"""Profit re-entry price gate: strict above last exit vs pullback buffer (``entries.*`` + strategy.exits)."""
from __future__ import annotations

from typing import Any


def entries_reentry_pullback_cfg(config: dict[str, Any] | None) -> tuple[bool, float]:
    """Read ``entries.allow_reentry_on_pullback`` (alias: top-level ``reentry.allow_pullback_reentry``) and buffer."""
    ent = (config or {}).get("entries") if isinstance(config, dict) else None
    ent = ent if isinstance(ent, dict) else {}
    allow = bool(ent.get("allow_reentry_on_pullback", False))
    raw = ent.get("reentry_price_buffer_pct")
    try:
        buf = float(raw) if raw is not None and str(raw).strip() != "" else 0.0
    except (TypeError, ValueError):
        buf = 0.0
    return allow, max(0.0, buf)


def profit_reentry_price_allowed(
    current_close: float,
    exit_price: float,
    *,
    require_price_above_exit_after_profit: bool,
    allow_reentry_on_pullback: bool,
    reentry_price_buffer_pct: float,
) -> tuple[bool, str | None]:
    """
    When *allow_reentry_on_pullback* is true, allow re-entry if *current_close* is above
    ``exit_price * (1 - reentry_price_buffer_pct/100)`` (small dip below exit is OK).

    When *allow_reentry_on_pullback* is false and *require_price_above_exit_after_profit* is true,
    require *current_close* > *exit_price* (legacy strict gate).
    """
    if exit_price <= 0:
        return True, None
    if not require_price_above_exit_after_profit and not allow_reentry_on_pullback:
        return True, None
    buf = max(0.0, float(reentry_price_buffer_pct))
    if allow_reentry_on_pullback:
        floor_px = max(0.0, exit_price * (1.0 - buf / 100.0))
        if current_close <= floor_px:
            return False, (
                "re-entry after profit: close %.2f <= %.2f (exit %.2f with %.2f%% pullback buffer)"
                % (current_close, floor_px, exit_price, buf)
            )
        return True, None
    if current_close <= exit_price:
        return False, (
            "re-entry after profit requires price > exit price (%.2f <= %.2f)"
            % (current_close, exit_price)
        )
    return True, None
