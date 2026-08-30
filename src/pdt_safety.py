"""
Pre-trade checks to avoid same-day round trips when equity is below the PDT threshold.

Brokers typically treat closing a margin position opened the same session as a day trade.
When ``compliance.avoid_same_day_close_below_pdt_equity`` is true, ``margin_account`` is
true, equity ``< pdt_min_equity``, and the tracked ``entry_time`` falls on the same NY
calendar day as *now*, closing sells (and short covers) are skipped until a later day.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def entry_opened_same_calendar_day_et(
    entry_time_iso: str | None,
    now_et: datetime,
    *,
    entry_time_uncertain: bool = False,
) -> bool:
    """
    True if *entry_time_iso* parses to the same America/New_York calendar date as *now_et*.

    If *entry_time_uncertain* is True (e.g. broker sync without fill time), returns False
    so we do not defer exits for long-held positions incorrectly.
    """
    if entry_time_uncertain:
        return False
    if not entry_time_iso or not str(entry_time_iso).strip():
        return False
    try:
        t = datetime.fromisoformat(str(entry_time_iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    tz = now_et.tzinfo
    if tz is None:
        return False
    t_et = t.astimezone(tz)
    return t_et.date() == now_et.date()


def block_same_day_close_for_pdt(
    *,
    config: dict[str, Any],
    account_equity: float,
    entry_time_iso: str | None,
    now_et: datetime,
    entry_time_uncertain: bool = False,
) -> tuple[bool, str | None]:
    """
    Return (True, reason) if a closing order should be deferred to avoid a same-day round trip
    on a margin account below the PDT equity threshold.
    """
    comp = config.get("compliance") or {}
    if not bool(comp.get("avoid_same_day_close_below_pdt_equity", True)):
        return False, None
    if not bool(comp.get("margin_account", True)):
        return False, None
    pdt_min = float(comp.get("pdt_min_equity", 25_000))
    if account_equity >= pdt_min:
        return False, None
    if not entry_opened_same_calendar_day_et(
        entry_time_iso,
        now_et,
        entry_time_uncertain=entry_time_uncertain,
    ):
        return False, None
    return (
        True,
        "same-day close deferred — equity $%.0f < $%.0f (margin); open next session to exit"
        % (account_equity, pdt_min),
    )
