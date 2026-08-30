"""Numeric priorities for conflicting live-loop decisions (exit vs allocator add).

Lower number = higher priority. Used to suppress ``capital_allocator`` BUY actions when the
exit pass already signaled a trim or protective exit for the same symbol in the same iteration.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from src.strategy import ExitReason

log = logging.getLogger(__name__)

DEFAULT_DECISION_PRIORITY: dict[str, int] = {
    "stop_loss": 1,
    "take_profit": 2,
    "exposure_trim": 3,
    "rebalance": 4,
    "new_entry": 5,
}


def parse_decision_priority(config: Mapping[str, Any] | None) -> dict[str, int]:
    """Merge ``config['decision_priority']`` onto defaults; invalid values fall back to defaults."""
    out = dict(DEFAULT_DECISION_PRIORITY)
    if not isinstance(config, Mapping):
        return out
    raw = config.get("decision_priority")
    if not isinstance(raw, Mapping):
        return out
    for k, v in DEFAULT_DECISION_PRIORITY.items():
        if k not in raw:
            continue
        try:
            iv = int(raw[k])
        except (TypeError, ValueError):
            log.warning("decision_priority.%s invalid (%r) — using default %s", k, raw.get(k), v)
            continue
        out[k] = iv
    return out


def rank_for_kind(table: Mapping[str, int], kind: str) -> int:
    """Return configured rank for *kind*; unknown kinds sort last (99)."""
    try:
        return int(table.get(str(kind).strip(), 99))
    except (TypeError, ValueError):
        return 99


def exit_reason_to_intent_kind(reason: ExitReason) -> str:
    """Map engine :class:`ExitReason` to a ``decision_priority`` bucket."""
    r = reason
    if r in (
        ExitReason.STOP_LOSS,
        ExitReason.TRAILING_STOP,
        ExitReason.OPTION_STOP_LOSS,
        ExitReason.OPTION_SPREAD_TOO_WIDE,
        ExitReason.KILL_SWITCH,
    ):
        return "stop_loss"
    if r in (
        ExitReason.TAKE_PROFIT,
        ExitReason.PARTIAL_TAKE_PROFIT,
        ExitReason.KILL_SWITCH_PARTIAL,
        ExitReason.OPTION_PROFIT_TAKE,
        ExitReason.OPTION_PROFIT_TAKE_PARTIAL,
        ExitReason.OPTION_PNL_TRAIL,
    ):
        return "take_profit"
    if r in (ExitReason.RISK_CAP_REBALANCE,):
        return "exposure_trim"
    if r in (ExitReason.OVERWEIGHT_TRIM,):
        return "rebalance"
    # Discretionary / hygiene exits — block adds like other non-entry trims
    if r in (
        ExitReason.TIME_BARS,
        ExitReason.SIGNAL_EXIT,
        ExitReason.NEWS_SENTIMENT,
        ExitReason.OPTION_MAX_HOLD,
        ExitReason.OPTION_UNDERLYING_BREAK,
        ExitReason.MOMENTUM_FADE,
    ):
        return "rebalance"
    return "rebalance"
