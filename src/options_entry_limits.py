"""
Per-session counters for option **entry** limits (daily cap + post-entry cooldown).

State is in-process only (resets on loop restart). Pair with broker-side controls for production.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

_OPTION_DAILY: dict[str, tuple[str, int]] = {}
_LAST_OPTION_ENTRY: dict[tuple[str, str], datetime] = {}


def _uid_key(user_id: str) -> str:
    return str(user_id or "default").strip() or "default"


def option_entries_used_today(user_id: str, et_date_iso: str) -> int:
    """How many option entries recorded for this ET calendar day (same loop process)."""
    k = _uid_key(user_id)
    d, n = _OPTION_DAILY.get(k, ("", 0))
    if d != et_date_iso:
        return 0
    return int(n)


def record_option_entry(user_id: str, et_date_iso: str) -> None:
    """Bump daily option-entry count after a successful option buy."""
    k = _uid_key(user_id)
    d, n = _OPTION_DAILY.get(k, ("", 0))
    if d != et_date_iso:
        n = 0
    _OPTION_DAILY[k] = (et_date_iso, n + 1)


def record_option_entry_utc(
    user_id: str,
    underlying_upper: str,
    when: datetime | None,
) -> None:
    """Remember last option entry time per user + underlying (cooldown after entry)."""
    k = (_uid_key(user_id), str(underlying_upper or "").strip().upper())
    if not k[1]:
        return
    w = when or datetime.now(timezone.utc)
    if w.tzinfo is None:
        w = w.replace(tzinfo=timezone.utc)
    _LAST_OPTION_ENTRY[k] = w


def option_entry_allowed_by_daily_cap(
    config: dict[str, Any] | None,
    user_id: str,
    et_date_iso: str,
) -> tuple[bool, str | None]:
    """``(ok, reason)`` vs ``options.max_option_trades_per_day``."""
    o = (config or {}).get("options") or {}
    raw = o.get("max_option_trades_per_day")
    if raw is None or str(raw).strip() == "":
        return True, None
    try:
        cap = max(0, int(raw))
    except (TypeError, ValueError):
        return True, None
    if cap <= 0:
        return True, None
    if option_entries_used_today(user_id, et_date_iso) >= cap:
        return False, "max option entries per day (%d) reached" % cap
    return True, None


def option_entry_cooldown_blocks(
    config: dict[str, Any] | None,
    user_id: str,
    underlying_upper: str,
    now_dt: Any,
) -> tuple[bool, str | None]:
    """``(blocked, reason)`` when ``cooldown_minutes_after_entry`` has not elapsed since last entry."""
    o = (config or {}).get("options") or {}
    raw_cd = o.get("cooldown_minutes_after_entry")
    if raw_cd is None or str(raw_cd).strip() == "":
        return False, None
    try:
        need_min = float(raw_cd)
    except (TypeError, ValueError):
        return False, None
    if need_min <= 0:
        return False, None
    if now_dt is None or not isinstance(now_dt, datetime):
        return False, None
    sym = str(underlying_upper or "").strip().upper()
    if not sym:
        return False, None
    prev = _LAST_OPTION_ENTRY.get((_uid_key(user_id), sym))
    if prev is None:
        return False, None
    now = now_dt
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    delta_min = (now - prev).total_seconds() / 60.0
    if delta_min < need_min:
        return True, "option entry cooldown %.0f/%.0f min after last entry on %s" % (
            delta_min,
            need_min,
            sym,
        )
    return False, None
