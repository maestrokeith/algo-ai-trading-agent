"""
Pre-execution filters on allocator *plans* (action rows), before order submission.

Cooldown / priority that previously ran only at submit time can be applied here so
the printed plan matches what will execute. Pass :class:`~src.live.exits.LiveExitContext`
as *exit_context* when available.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

log = logging.getLogger(__name__)

Plan = list[dict[str, Any]]
Book = list[Mapping[str, Any]] | None


def apply_cooldown(
    plan: list[Mapping[str, Any]] | None,
    portfolio: Book = None,
    *,
    exit_context: Any | None = None,
) -> Plan:
    """
    Filter *plan* (buy/sell notional actions) for symbols blocked by **cooldown** (and related
    exit-priority rules on buys). *portfolio* is the allocator book; reserved for book-aware
    rules. When *exit_context* is omitted, actions are returned unchanged (aside from shallow copy).

    Typical wiring::

        plan = allocator.allocate(portfolio=portfolio, candidates=candidates, equity=equity, cash=cash)
        plan = apply_cooldown(plan, portfolio, exit_context=exit_context)
    """
    if not plan:
        return []
    filtered: Plan = []
    for act in plan:
        row = dict(act)
        side = str(row.get("action", "")).lower()
        sym = str(row.get("symbol", "")).strip().upper()
        if not sym or side not in ("buy", "sell"):
            filtered.append(row)
            continue
        if side == "buy" and exit_context is not None:
            b_cool, b_why = exit_context.bulk_trim_buy_cooldown_active(sym)
            if b_cool:
                log.info(
                    "execution_guard: remove BUY %s from plan — %s",
                    sym,
                    b_why or "bulk trim buy cooldown",
                )
                continue
            blocked, p_why = exit_context.allocator_buy_blocked_by_priority(sym)
            if blocked:
                log.info(
                    "execution_guard: remove BUY %s from plan — %s",
                    sym,
                    p_why or "exit intent outranks new entry",
                )
                continue
        # *portfolio* reserved for book-based rules (e.g. re-entry) — exit_context drives cooldown today.
        filtered.append(row)
    return filtered
