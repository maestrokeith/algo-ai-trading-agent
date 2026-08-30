"""Structured logging for equity/option sell events (live + dispatch).

Use :func:`log_sell` with one of the canonical ``reason`` values so logs are
grep-friendly and dashboards can aggregate churn without parsing free text.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

log = logging.getLogger(__name__)

SELL_LOG_REASONS: frozenset[str] = frozenset(
    {
        "take_profit",
        "stop_loss",
        "rebalance_trim",
        "exposure_limit",
        "signal_flip",
        "time_exit",
    }
)


def log_sell(symbol: str, reason: str, context: Mapping[str, Any] | None = None) -> None:
    """Log one sell decision with a **canonical** reason and optional JSON-safe context.

    ``reason`` must be one of: take_profit, stop_loss, rebalance_trim, exposure_limit,
    signal_flip, time_exit. Unknown values are coerced to ``signal_flip`` and warned.
    """
    sym_u = str(symbol or "").strip().upper()
    r = str(reason or "").strip().lower()
    if r not in SELL_LOG_REASONS:
        log.warning(
            "log_sell: unknown reason %r for symbol=%s — coercing to signal_flip",
            reason,
            sym_u or "?",
        )
        r = "signal_flip"
    payload: dict[str, Any] = {"symbol": sym_u, "reason": r}
    if context:
        for k, v in dict(context).items():
            payload[str(k)] = v
    try:
        line = json.dumps(payload, default=str, separators=(",", ":"))
    except TypeError:
        line = json.dumps({"symbol": sym_u, "reason": r, "context_error": True}, default=str)
    log.info("[sell] %s", line)


def sell_log_reason_for_engine_exit(exit_value: str | None) -> str:
    """Map :class:`~src.strategy.ExitReason` ``.value`` (or equivalent string) to a ``log_sell`` reason."""
    v = str(exit_value or "").strip().lower()
    if v in ("stop_loss", "option_stop_loss", "option_spread_too_wide"):
        return "stop_loss"
    if v in (
        "tp",
        "take_profit",
        "partial_take_profit",
        "trail",
        "trailing_stop",
        "option_profit_take",
        "option_profit_take_partial",
        "option_pnl_trail",
    ):
        return "take_profit"
    if v in ("time_bars", "option_max_hold_days"):
        return "time_exit"
    if v in (
        "signal_exit",
        "news_sentiment",
        "option_underlying_break_signal",
        "kill_switch",
        "kill_switch_partial",
        "momentum_fade",
    ):
        return "signal_flip"
    if v in ("risk_cap_rebalance",):
        return "exposure_limit"
    if v in ("overweight_trim",):
        return "rebalance_trim"
    return "signal_flip"
