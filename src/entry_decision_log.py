"""Structured JSON lines for entry skip / decision events (live loop)."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import pytz

_RUNTIME: dict[str, Any] = {}


def set_entry_skip_runtime_context(**kwargs: Any) -> None:
    """Replace runtime fields merged into each skip line (call at start of each user pass)."""
    _RUNTIME.clear()
    _RUNTIME.update(kwargs)


def update_entry_skip_runtime_context(**kwargs: Any) -> None:
    """Merge fields (e.g. regime, cash) once they are known."""
    _RUNTIME.update(kwargs)


def structured_skip_logs_enabled() -> bool:
    """True when skip lines should be JSON (``entries.structured_skip_logs``)."""
    cfg = _RUNTIME.get("config")
    if not isinstance(cfg, dict):
        return True
    ent = cfg.get("entries") or {}
    if not isinstance(ent, dict):
        return True
    return bool(ent.get("structured_skip_logs", True))


def _reason_slug(reason: str, reason_code: str | None) -> str:
    if reason_code:
        return str(reason_code).strip().lower().replace(" ", "_")[:120]
    r = (reason or "").strip().lower()
    if r == "open order pending" or "open order pending" in r:
        return "open_order_pending"
    if "already in positions" in r:
        return "already_in_positions"
    if "in tracked state" in r:
        return "in_tracked_state"
    if "not enough bars" in r:
        return "insufficient_history"
    if "spread" in r and ("cap" in r or ">" in r):
        return "spread_above_cap"
    if "min_trade_size" in r or ("insufficient buying power" in r and "min" in r):
        return "insufficient_buying_power_min_trade_size"
    if "buying power" in r or ("insufficient" in r and "power" in r):
        return "insufficient_buying_power"
    if "not in scoring top_n_candidates set" in r or "not in scoring allowlist" in r:
        return "not_in_scoring_top_n"
    if "not in allowed stock universe" in r:
        return "not_in_allowed_stock_universe"
    if "options not available" in r and "fallback" in r:
        return "options_stock_fallback_disabled"
    if "max positions reached" in r:
        return "max_positions_reached"
    if "defensive regime" in r or "trend longs off in defensive" in r:
        return "trend_longs_off_defensive_regime"
    if "no long" in r and ("require" in r or "sqqq" in r or "hedge" in r):
        return "requires_sqqq_position"
    if "require" in r and "sqqq" in r:
        return "requires_sqqq_position"
    if "trend-long options in bearish regime require long" in r:
        return "trend_long_options_bearish_hedge_required"
    if "underlying not allowed" in r:
        return "options_underlying_not_allowed"
    if "api" in r or "401" in r or "unauthorized" in r or "connection" in r:
        return "api_error"
    slug = re.sub(r"[^a-z0-9]+", "_", r)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return (slug[:120] if slug else "unknown")


def emit_entry_skip(
    dt: datetime,
    symbol: str,
    reason: str,
    *,
    verbose: bool,
    force: bool = False,
    signal: str | None = None,
    reason_code: str | None = None,
) -> None:
    """Emit one skip line: JSON when structured, else legacy ``SYMBOL skip — reason``."""
    sym_u = str(symbol).upper()
    if not (force or verbose or sym_u == "SQQQ"):
        return
    sig = (signal or _RUNTIME.get("signal_default") or "scan").strip()
    if not structured_skip_logs_enabled():
        print(dt.strftime("%H:%M ET"), f"{sym_u} skip — {reason}", flush=True)
        return
    et = pytz.timezone("America/New_York")
    ts = dt.astimezone(et)
    regime = _RUNTIME.get("regime")
    pos_n = _RUNTIME.get("position_count")
    cash = _RUNTIME.get("cash_available")
    payload: dict[str, Any] = {
        "symbol": sym_u,
        "signal": sig,
        "decision": "skip",
        "reason": _reason_slug(reason, reason_code),
        "regime": regime if isinstance(regime, str) else None,
        "position_count": int(pos_n) if pos_n is not None else None,
        "cash_available": float(cash) if cash is not None else None,
        "ts": ts.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    uid = _RUNTIME.get("user_id")
    if isinstance(uid, str) and uid:
        payload["user_id"] = uid
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False), flush=True)


def emit_options_trade_stock(
    dt: datetime,
    symbol: str,
    *,
    signal: str,
    detail: str | None = None,
) -> None:
    """
    Log **options not used → trade stock** when routing is ineligible or rejected before an option order.

    Structured mode: ``decision: "route"``, ``reason: "options_not_allowed_trade_stock"``, optional ``detail``.
    Legacy mode: ``HH:MM ET SYMBOL trade stock (signal): detail``.
    """
    sym_u = str(symbol).upper()
    sig = signal.strip()
    if not structured_skip_logs_enabled():
        tail = (" — " + detail) if detail else ""
        print(
            dt.strftime("%H:%M ET"),
            sym_u,
            "trade stock (%s)%s" % (sig, tail),
            flush=True,
        )
        return
    et = pytz.timezone("America/New_York")
    ts = dt.astimezone(et)
    regime = _RUNTIME.get("regime")
    pos_n = _RUNTIME.get("position_count")
    cash = _RUNTIME.get("cash_available")
    payload: dict[str, Any] = {
        "symbol": sym_u,
        "signal": sig,
        "decision": "route",
        "reason": "options_not_allowed_trade_stock",
        "regime": regime if isinstance(regime, str) else None,
        "position_count": int(pos_n) if pos_n is not None else None,
        "cash_available": float(cash) if cash is not None else None,
        "ts": ts.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if detail:
        payload["detail"] = str(detail)[:500]
    uid = _RUNTIME.get("user_id")
    if isinstance(uid, str) and uid:
        payload["user_id"] = uid
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False), flush=True)


def emit_options_fallback_to_stock(
    dt: datetime,
    symbol: str,
    *,
    signal: str,
) -> None:
    """
    Log **options → fallback to stock** when options routing ran but did not place an order.

    Structured mode: one JSON line with ``decision: "route"`` and ``reason: "options_fallback_to_stock"``.
    Legacy mode: ``HH:MM ET SYMBOL options → fallback to stock``.
    """
    sym_u = str(symbol).upper()
    sig = signal.strip()
    if not structured_skip_logs_enabled():
        print(
            dt.strftime("%H:%M ET"),
            sym_u,
            "options → fallback to stock",
            flush=True,
        )
        return
    et = pytz.timezone("America/New_York")
    ts = dt.astimezone(et)
    regime = _RUNTIME.get("regime")
    pos_n = _RUNTIME.get("position_count")
    cash = _RUNTIME.get("cash_available")
    payload: dict[str, Any] = {
        "symbol": sym_u,
        "signal": sig,
        "decision": "route",
        "reason": "options_fallback_to_stock",
        "regime": regime if isinstance(regime, str) else None,
        "position_count": int(pos_n) if pos_n is not None else None,
        "cash_available": float(cash) if cash is not None else None,
        "ts": ts.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    uid = _RUNTIME.get("user_id")
    if isinstance(uid, str) and uid:
        payload["user_id"] = uid
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False), flush=True)
