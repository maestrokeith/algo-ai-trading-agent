"""
Persisted re-entry rules for inverse ETF stock entries (SQQQ v1).

Used when ``universe.bear_etfs.sqqq_reentry.enabled`` is true: cooldown after a
full exit, optional fresh QQQ breakdown vs the exit reference close, and an
optional cap on one initial stock entry per NY session day (controlled scaling
adds are separate and do not use this path).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from .position_tracker import minutes_since_iso

logger = logging.getLogger(__name__)

SQQQ_UPPER = "SQQQ"


def _state_path(user_id: str, data_dir: Path) -> Path:
    return data_dir / f"inverse_reentry_{user_id}.json"


def load_state(user_id: str, data_dir: Path) -> dict[str, Any]:
    path = _state_path(user_id, data_dir)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.warning("inverse_reentry: could not load %s", path, exc_info=True)
        return {}


def save_state(user_id: str, data_dir: Path, state: dict[str, Any]) -> None:
    path = _state_path(user_id, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def _sqqq_block(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg.get("sqqq_reentry") or {}


def record_sqqq_full_exit(
    user_id: str,
    data_dir: Path,
    now_dt: datetime,
    qqq_close_at_exit: float | None,
) -> None:
    """Call when SQQQ is removed from the position tracker (full flat)."""
    state = load_state(user_id, data_dir)
    sq = state.get(SQQQ_UPPER)
    if not isinstance(sq, dict):
        sq = {}
    sq["last_full_exit_at"] = now_dt.isoformat()
    sq["qqq_close_at_full_exit"] = float(qqq_close_at_exit) if qqq_close_at_exit is not None else None
    state[SQQQ_UPPER] = sq
    save_state(user_id, data_dir, state)


def record_sqqq_initial_stock_entry(
    user_id: str,
    data_dir: Path,
    et_date_str: str,
) -> None:
    """Call after a successful *initial* SQQQ stock buy (not a scaling add)."""
    state = load_state(user_id, data_dir)
    sq = state.get(SQQQ_UPPER)
    if not isinstance(sq, dict):
        sq = {}
    day = str(et_date_str).strip()
    prev_day = sq.get("initial_stock_entry_et_date")
    if prev_day == day:
        sq["initial_stock_entries_same_day_count"] = int(sq.get("initial_stock_entries_same_day_count") or 0) + 1
    else:
        sq["initial_stock_entry_et_date"] = day
        sq["initial_stock_entries_same_day_count"] = 1
    state[SQQQ_UPPER] = sq
    save_state(user_id, data_dir, state)


def check_sqqq_stock_reentry_allowed(
    bear_etfs_cfg: dict[str, Any],
    user_id: str,
    data_dir: Path,
    now_dt: datetime,
    qqq_close: float | None,
    *,
    et_date_str: str,
) -> tuple[bool, str | None]:
    """
    Return (allowed, reason_if_blocked) for a *new* SQQQ stock entry from flat.

    ``et_date_str`` is calendar date in America/New_York (YYYY-MM-DD), same as loop ``dt``.
    """
    sr = _sqqq_block(bear_etfs_cfg)
    if not bool(sr.get("enabled", False)):
        return True, None

    state = load_state(user_id, data_dir)
    sq = state.get(SQQQ_UPPER)
    if not isinstance(sq, dict):
        sq = {}

    cooldown = int(sr.get("full_exit_cooldown_minutes", 0) or 0)
    if cooldown > 0:
        last_exit = sq.get("last_full_exit_at")
        mins = minutes_since_iso(str(last_exit) if last_exit else None, now_dt)
        if mins is not None and mins < float(cooldown):
            return False, "SQQQ re-entry cooldown %.0f/%d min after full exit" % (mins, cooldown)

    if bool(sr.get("require_qqq_close_below_exit_reference", False)):
        ref = sq.get("qqq_close_at_full_exit")
        if ref is not None and qqq_close is not None:
            ref_f = float(ref)
            if float(qqq_close) >= ref_f:
                return False, "SQQQ re-entry blocked — QQQ %.2f not below exit reference %.2f" % (
                    float(qqq_close),
                    ref_f,
                )

    max_init = sr.get("max_initial_stock_entries_per_et_day")
    if max_init is not None and str(max_init).strip() != "":
        cap = int(max_init)
        if cap >= 1:
            last_init_day = sq.get("initial_stock_entry_et_date")
            count_today = int(sq.get("initial_stock_entries_same_day_count") or 0)
            if last_init_day == et_date_str and count_today >= cap:
                return False, (
                    "SQQQ re-entry blocked — %d initial stock entr%s per NY day (cap %d)"
                    % (count_today, "y" if count_today == 1 else "ies", cap)
                )

    return True, None
