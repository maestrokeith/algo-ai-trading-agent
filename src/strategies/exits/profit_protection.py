"""Profit-protection helpers for split live exit workflows."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.strategies.exits.context import LiveExitContext


def equity_long_trend_structure_still_strong(
    ctx: LiveExitContext,
    symbol: str,
) -> bool:
    """
    True when daily bar structure is still a strong long trend
    (see :meth:`~src.strategy.TrendFollowingStrategy.long_trend_structure_still_strong`).

    Used for ``exits.do_not_sell_winners_early`` and by
    :func:`exit_trim_suppressed_trend_still_strong` when
    ``require_signal_break_for_trim`` is on.
    """
    st = ctx.engine.strategy
    if str(symbol or "").upper() == "SQQQ":
        return False
    try:
        ma_slow = int(getattr(st, "ma_slow", 200))
        cap = max(260, ma_slow + 2)
        df = ctx.broker.get_bars(str(symbol), timeframe="1Day", limit=cap)
    except Exception:
        return False
    if df is None or getattr(df, "empty", True) or len(df) < ma_slow:
        return False
    return bool(st.long_trend_structure_still_strong(symbol, df))


def equity_unrealized_pnl_percent_points(
    broker_pos: Mapping[str, Any],
    *,
    entry_price: float,
    mid: float,
) -> float:
    """
    Unrealized P/L as **percent points** (e.g. 2.0 = 2%).

    Prefer broker ``unrealized_plpc`` (fraction, e.g. 0.02) when present; else
    return vs *entry* from *mid* when *entry_price* > 0.
    """
    try:
        raw = broker_pos.get("unrealized_plpc")
        if raw is not None and str(raw).strip() != "":
            v = float(raw)
            if abs(v) < 1.0 + 1e-5:
                return v * 100.0
            return v
    except (TypeError, ValueError):
        pass
    if entry_price > 0.0 and mid == mid and mid > 0.0:
        return (float(mid) - float(entry_price)) / float(entry_price) * 100.0
    return 0.0


def exit_trim_suppressed_trend_still_strong(
    ctx: LiveExitContext,
    symbol: str,
) -> bool:
    """
    When ``exits.require_signal_break_for_trim`` is true, return True to **skip**
    profit/risk trims while the daily MA trend filter still passes.
    """
    st = ctx.engine.strategy
    if not getattr(st, "require_signal_break_for_trim", False):
        return False
    return equity_long_trend_structure_still_strong(ctx, symbol)


def do_not_sell_winners_early_blocks(
    ctx: LiveExitContext,
    symbol: str,
    pnl_percent_points: float,
) -> bool:
    """
    When ``exits.do_not_sell_winners_early`` is enabled, *pnl* above
    ``min_pnl_pct`` and trend still structurally long → block discretionary sells.
    """
    st = ctx.engine.strategy
    if not getattr(st, "do_not_sell_winners_early_enabled", False):
        return False
    try:
        min_pnl = float(getattr(st, "do_not_sell_winners_early_min_pnl_pct", 2.0) or 0.0)
    except (TypeError, ValueError):
        min_pnl = 2.0
    if pnl_percent_points <= min_pnl + 1e-9:
        return False
    return bool(equity_long_trend_structure_still_strong(ctx, symbol))


def live_profit_protection_cfg(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return conservative live profit protection defaults."""

    raw = ((config or {}).get("live_risk_protection") or {}).get("profit_protection")
    configured = isinstance(raw, Mapping)
    raw = raw if configured else {}

    def f(key: str, default: float) -> float:
        try:
            return float(raw.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    return {
        "enabled": bool(raw.get("enabled", True)) if configured else False,
        "breakeven_trigger_pct": f("breakeven_trigger_pct", 0.5),
        "trailing_trigger_pct": f("trailing_trigger_pct", 1.0),
        "trailing_stop_pct": f("trailing_stop_pct", 0.5),
        "partial_take_profit_pct": f("partial_take_profit_pct", 1.5),
        "partial_take_profit_fraction": max(0.01, min(0.99, f("partial_take_profit_fraction", 0.5))),
    }


def live_profit_protection_decision(
    *,
    config: Mapping[str, Any] | None,
    position: Mapping[str, Any],
    entry_price: float,
    current_price: float,
    qty: float,
) -> dict[str, Any]:
    """Evaluate breakeven, trailing, and partial-take-profit protections."""

    cfg = live_profit_protection_cfg(config)
    if not bool(cfg["enabled"]) or entry_price <= 0.0 or current_price <= 0.0 or qty <= 0.0:
        return {"action": "hold", "reason": "disabled_or_invalid", "state": {}}

    def safe_float(value: Any, default: float = 0.0) -> float:
        try:
            out = float(value)
        except (TypeError, ValueError):
            return default
        return out if out == out else default

    pnl_pct = (float(current_price) - float(entry_price)) / float(entry_price) * 100.0
    state = position.get("live_profit_protection") if isinstance(position.get("live_profit_protection"), Mapping) else {}
    high = max(
        float(current_price),
        safe_float(position.get("trail_high")),
        safe_float(state.get("high_price")),
    )
    high_pnl_pct = (float(high) - float(entry_price)) / float(entry_price) * 100.0
    breakeven_armed = bool(state.get("breakeven_armed")) or high_pnl_pct >= float(cfg["breakeven_trigger_pct"]) - 1e-9
    trailing_armed = bool(state.get("trailing_armed")) or high_pnl_pct >= float(cfg["trailing_trigger_pct"]) - 1e-9
    new_state = {
        "breakeven_armed": breakeven_armed,
        "trailing_armed": trailing_armed,
        "high_price": high,
    }
    if breakeven_armed and current_price <= entry_price + 1e-9:
        return {"action": "full_exit", "reason": "breakeven_stop", "qty": float(qty), "state": new_state}
    if trailing_armed:
        stop_price = high * (1.0 - float(cfg["trailing_stop_pct"]) / 100.0)
        if current_price <= stop_price + 1e-9:
            return {
                "action": "full_exit",
                "reason": "profit_trailing_stop",
                "qty": float(qty),
                "stop_price": stop_price,
                "state": new_state,
            }
    partial_taken = bool(position.get("partial_taken")) or bool(state.get("partial_take_profit_done"))
    if not partial_taken and pnl_pct >= float(cfg["partial_take_profit_pct"]) - 1e-9:
        sell_qty = max(0.0, float(qty) * float(cfg["partial_take_profit_fraction"]))
        new_state["partial_take_profit_done"] = True
        return {"action": "partial_exit", "reason": "profit_protection_partial_take_profit", "qty": sell_qty, "state": new_state}
    return {"action": "hold", "reason": "ok", "state": new_state}


def live_time_stop_not_green_decision(
    *,
    config: Mapping[str, Any] | None,
    minutes_held: float | None,
    pnl_percent_points: float,
) -> bool:
    raw = ((config or {}).get("live_risk_protection") or {}).get("profit_protection")
    raw = raw if isinstance(raw, Mapping) else {}
    try:
        threshold = float(raw.get("time_stop_not_green_minutes", 15) or 15)
    except (TypeError, ValueError):
        threshold = 15.0
    return bool(
        threshold > 0.0
        and minutes_held is not None
        and float(minutes_held) >= threshold - 1e-9
        and float(pnl_percent_points) <= 0.0
    )


__all__ = [
    "do_not_sell_winners_early_blocks",
    "equity_long_trend_structure_still_strong",
    "equity_unrealized_pnl_percent_points",
    "exit_trim_suppressed_trend_still_strong",
    "live_profit_protection_cfg",
    "live_profit_protection_decision",
    "live_time_stop_not_green_decision",
]
