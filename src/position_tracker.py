"""Track open positions (entry price, time) for exit logic. Persisted to JSON.

Each user's positions are stored in a separate file:
``data/positions_{user_id}.json``.  When no ``user_id`` is supplied the
module falls back to ``"default"``.

On first run, if a legacy ``data/positions_tracked.json`` exists it is
automatically migrated to ``data/positions_default.json``.
"""
from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from collections.abc import Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def tracked_row_has_open_long(row: Mapping[str, Any] | None) -> bool:
    """
    True when a persisted tracker row represents an open long.

    Prefer ``qty`` > 0 when present; otherwise treat ``notional`` > 0 as an open
    stake (for rows sized in dollars without an integer share count).
    """
    if not isinstance(row, dict):
        return False
    try:
        q = int(float(row.get("qty") or 0))
        if q > 0:
            return True
    except (TypeError, ValueError):
        pass
    try:
        return float(row.get("notional") or 0) > 0
    except (TypeError, ValueError):
        return False



def holding_minutes(entry_time, now=None) -> float:
    """
    Return minutes between entry_time and now.
    entry_time can be str/datetime.
    now can be str/datetime.
    """
    try:
        if entry_time is None:
            return 999999.0

        if isinstance(entry_time, datetime):
            entry_dt = entry_time
        else:
            entry_dt = datetime.fromisoformat(str(entry_time).replace("Z", "+00:00"))

        if now is None:
            now_dt = datetime.now(timezone.utc)
        elif isinstance(now, datetime):
            now_dt = now
        else:
            now_dt = datetime.fromisoformat(str(now).replace("Z", "+00:00"))

        if entry_dt.tzinfo is None:
            entry_dt = entry_dt.replace(tzinfo=timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)

        return max(0.0, (now_dt - entry_dt).total_seconds() / 60.0)

    except Exception:
        return 999999.0


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _data_dir() -> Path:
    """Return the default ``data/`` directory (project root / data)."""
    return Path(__file__).resolve().parent.parent / "data"


def _user_path(user_id: str = "default", data_dir: Path | None = None) -> Path:
    """Return the JSON path for *user_id*: ``<data_dir>/positions_<user_id>.json``."""
    d = data_dir or _data_dir()
    return d / f"positions_{user_id}.json"


_LEGACY_FILENAME = "positions_tracked.json"


def _migrate_legacy(data_dir: Path | None = None) -> None:
    """If the legacy ``positions_tracked.json`` exists, copy it to
    ``positions_default.json`` and rename the old file to a ``.bak``
    so it is preserved but no longer used.
    """
    d = data_dir or _data_dir()
    legacy = d / _LEGACY_FILENAME
    target = _user_path("default", d)
    if legacy.exists() and not target.exists():
        shutil.copy2(legacy, target)
        backup = legacy.with_suffix(".json.bak")
        legacy.rename(backup)
        logger.info(
            "Migrated legacy %s → %s (backup at %s)",
            legacy.name, target.name, backup.name,
        )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def load(
    user_id: str = "default",
    *,
    data_dir: Path | None = None,
    base_path: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Load tracked positions for *user_id*.

    ``base_path`` is accepted for backward compatibility but ``user_id``
    is the preferred interface.
    """
    if base_path is not None:
        path = base_path
    else:
        _migrate_legacy(data_dir)
        path = _user_path(user_id, data_dir)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save(
    data: dict[str, dict[str, Any]],
    user_id: str = "default",
    *,
    data_dir: Path | None = None,
    base_path: Path | None = None,
) -> None:
    """Persist *data* for *user_id*."""
    path = base_path if base_path is not None else _user_path(user_id, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def add(
    symbol: str,
    qty: int,
    entry_price: float,
    stop_pct: float,
    partial_taken: bool = False,
    trail_high: float | None = None,
    side: str = "long",
    user_id: str = "default",
    *,
    data_dir: Path | None = None,
    base_path: Path | None = None,
    extras: dict[str, Any] | None = None,
) -> None:
    """Add a new tracked position for *symbol*.

    Optional *extras* are merged into the row (e.g. ``scale_count``, ``last_entry_price``)
    for pyramiding state that must survive process restarts.
    """
    data = load(user_id, data_dir=data_dir, base_path=base_path)
    row: dict[str, Any] = {
        "qty": qty,
        "entry_price": entry_price,
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "stop_pct": stop_pct,
        "partial_taken": partial_taken,
        "trail_high": float(trail_high) if trail_high is not None else None,
        "side": (side or "long").strip().lower(),
        "last_buy_price": float(entry_price),
    }
    if extras:
        for ek, ev in extras.items():
            row[ek] = ev
    data[symbol.upper()] = row
    save(data, user_id, data_dir=data_dir, base_path=base_path)
    try:
        from src.position_state_machine import maybe_record_buy_from_tracker

        maybe_record_buy_from_tracker(symbol, user_id, data_dir=data_dir)
    except Exception:
        logger.debug("position_state maybe_record_buy failed", exc_info=True)


def merge_add_shares(
    symbol: str,
    add_qty: int,
    fill_price: float,
    stop_pct: float | None = None,
    user_id: str = "default",
    *,
    data_dir: Path | None = None,
    base_path: Path | None = None,
    extras: dict[str, Any] | None = None,
    et_trading_date: str | None = None,
) -> None:
    """Increase qty for an open position; weight-average entry_price.

    If not tracked, behaves like :func:`add`.
    """
    if add_qty <= 0:
        return
    data = load(user_id, data_dir=data_dir, base_path=base_path)
    key = symbol.upper()
    if key not in data:
        add(symbol, add_qty, fill_price, stop_pct if stop_pct is not None else 2.0,
            user_id=user_id, data_dir=data_dir, base_path=base_path)
        if extras:
            data = load(user_id, data_dir=data_dir, base_path=base_path)
            if key in data:
                for ek, ev in extras.items():
                    data[key][ek] = ev
                save(data, user_id, data_dir=data_dir, base_path=base_path)
        return
    old = data[key]
    oq = int(old.get("qty", 0))
    oe = float(old.get("entry_price", 0) or 0)
    new_q = oq + add_qty
    if new_q <= 0:
        return
    if oq > 0 and oe > 0:
        new_e = (oe * oq + fill_price * add_qty) / new_q
    else:
        new_e = fill_price
    data[key]["qty"] = new_q
    data[key]["entry_price"] = new_e
    data[key]["last_buy_price"] = float(fill_price)
    if stop_pct is not None:
        data[key]["stop_pct"] = stop_pct
    if extras:
        for ek, ev in extras.items():
            data[key][ek] = ev
    data[key]["last_add_time"] = datetime.now(timezone.utc).isoformat()
    if et_trading_date is not None and str(et_trading_date).strip() != "":
        d = str(et_trading_date).strip()
        prev_d = str(data[key].get("adds_et_date") or "")
        if prev_d != d:
            data[key]["adds_et_date"] = d
            data[key]["adds_et_date_count"] = 1
        else:
            data[key]["adds_et_date_count"] = int(data[key].get("adds_et_date_count") or 0) + 1
    save(data, user_id, data_dir=data_dir, base_path=base_path)
    try:
        from src.position_state_machine import maybe_record_buy_from_tracker

        maybe_record_buy_from_tracker(symbol, user_id, data_dir=data_dir)
    except Exception:
        logger.debug("position_state maybe_record_buy failed", exc_info=True)


def update(
    symbol: str,
    qty: int | None = None,
    partial_taken: bool | None = None,
    trail_high: float | None = None,
    min_price_since_entry: float | None = None,
    max_price_since_entry: float | None = None,
    option_profit_tier1_done: bool | None = None,
    option_pnl_peak_pct: float | None = None,
    smart_scale_out_index: int | None = None,
    smart_exit_state: Mapping[str, Any] | None = None,
    time_based_trim_done: bool | None = None,
    user_id: str = "default",
    *,
    data_dir: Path | None = None,
    base_path: Path | None = None,
) -> None:
    """Update one or more fields for an existing position."""
    data = load(user_id, data_dir=data_dir, base_path=base_path)
    key = symbol.upper()
    if key not in data:
        return
    if qty is not None:
        data[key]["qty"] = qty
    if partial_taken is not None:
        data[key]["partial_taken"] = partial_taken
    if trail_high is not None:
        data[key]["trail_high"] = trail_high
    if min_price_since_entry is not None:
        current = data[key].get("min_price_since_entry")
        try:
            data[key]["min_price_since_entry"] = min(float(current), float(min_price_since_entry)) if current is not None else float(min_price_since_entry)
        except (TypeError, ValueError):
            data[key]["min_price_since_entry"] = float(min_price_since_entry)
    if max_price_since_entry is not None:
        current = data[key].get("max_price_since_entry")
        try:
            data[key]["max_price_since_entry"] = max(float(current), float(max_price_since_entry)) if current is not None else float(max_price_since_entry)
        except (TypeError, ValueError):
            data[key]["max_price_since_entry"] = float(max_price_since_entry)
    if option_profit_tier1_done is not None:
        data[key]["option_profit_tier1_done"] = bool(option_profit_tier1_done)
    if option_pnl_peak_pct is not None:
        data[key]["option_pnl_peak_pct"] = float(option_pnl_peak_pct)
    if smart_scale_out_index is not None:
        data[key]["smart_scale_out_index"] = int(smart_scale_out_index)
    if smart_exit_state is not None:
        data[key]["smart_exit_state"] = dict(smart_exit_state)
    if time_based_trim_done is not None:
        data[key]["time_based_trim_done"] = bool(time_based_trim_done)
    save(data, user_id, data_dir=data_dir, base_path=base_path)


def remove(
    symbol: str,
    user_id: str = "default",
    *,
    data_dir: Path | None = None,
    base_path: Path | None = None,
) -> None:
    """Remove *symbol* from tracked positions."""
    data = load(user_id, data_dir=data_dir, base_path=base_path)
    data.pop(symbol.upper(), None)
    save(data, user_id, data_dir=data_dir, base_path=base_path)


def reconcile(
    symbol: str,
    qty: int,
    avg_price: float,
    *,
    user_id: str = "default",
    data_dir: Path | None = None,
    base_path: Path | None = None,
    default_stop_pct: float = 1.5,
) -> None:
    """Align one tracker row with broker-reported open position.

    *qty* is the broker signed position size (negative for shorts). *avg_price* is
    average entry per share/contract (positive). Tracker stores absolute quantity
    and ``side`` ``long`` or ``short``.

    If the symbol is not yet tracked, inserts a baseline row (restart / manual trade
    sync). If ``abs(qty)`` is zero, removes the row. When *avg_price* is missing
    (<= 0) for an existing row, ``entry_price`` is left unchanged.
    """
    key = str(symbol or "").strip().upper()
    if not key:
        return
    qty_raw = int(qty)
    qty_abs = abs(qty_raw)
    if qty_abs <= 0:
        remove(key, user_id=user_id, data_dir=data_dir, base_path=base_path)
        return

    side = "short" if qty_raw < 0 else "long"
    entry_in = float(avg_price) if avg_price is not None else 0.0

    data = load(user_id, data_dir=data_dir, base_path=base_path)
    if key not in data:
        if entry_in <= 0:
            return
        add(
            key,
            qty_abs,
            entry_in,
            default_stop_pct,
            side=side,
            user_id=user_id,
            data_dir=data_dir,
            base_path=base_path,
            extras={
                "scale_count": 1,
                "last_entry_price": float(entry_in),
                "entry_time_uncertain": True,
                "signal_strength": 1.0,
            },
        )
        return

    row = data[key]
    row["qty"] = qty_abs
    row["side"] = side
    if entry_in > 0:
        row["entry_price"] = entry_in
        lb = row.get("last_buy_price")
        if lb is None or float(lb) <= 0:
            row["last_buy_price"] = float(entry_in)
    save(data, user_id, data_dir=data_dir, base_path=base_path)


def clear_all(
    user_id: str = "default",
    *,
    data_dir: Path | None = None,
    base_path: Path | None = None,
) -> None:
    """Clear all tracked positions (e.g. after resetting paper account)."""
    save({}, user_id, data_dir=data_dir, base_path=base_path)


# ---------------------------------------------------------------------------
# Time helpers (stateless — no user_id needed)
# ---------------------------------------------------------------------------

def bars_held(entry_time_iso: str, now: datetime | None = None) -> int:
    """Days held (for daily bars)."""
    try:
        s = entry_time_iso.replace("Z", "+00:00")
        t = datetime.fromisoformat(s)
    except Exception:
        t = datetime.now(timezone.utc)
    now = now or datetime.now(timezone.utc)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0, (now - t).days)


def minutes_held(entry_time_iso: str, now: datetime | None = None) -> float:
    """Wall-clock minutes since entry (for strategy.exits.min_hold_minutes)."""
    if not entry_time_iso or not str(entry_time_iso).strip():
        return 0.0
    try:
        s = str(entry_time_iso).replace("Z", "+00:00")
        t = datetime.fromisoformat(s)
    except Exception:
        return 0.0
    now = now or datetime.now(timezone.utc)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(0.0, (now - t).total_seconds() / 60.0)


def add_on_pullback_ok(
    close: float,
    last_entry_price: Any,
    max_price_ratio: float | None,
) -> bool:
    """
    For add-on buys: return True when no gate, or when ``close < last_entry_price * max_price_ratio``.

    *max_price_ratio* in ``(0, 1)`` enables the gate (e.g. ``0.98`` → need a 2%+ dip vs last entry).
    ``None``, values ``<= 0``, or ``>= 1`` disable the check (always allow). Missing or non-positive
    *last_entry_price* also allows (insufficient tracker data).
    """
    if max_price_ratio is None or float(max_price_ratio) <= 0 or float(max_price_ratio) >= 1:
        return True
    if last_entry_price is None or str(last_entry_price).strip() == "":
        return True
    try:
        le = float(last_entry_price)
    except (TypeError, ValueError):
        return True
    if le <= 0:
        return True
    return float(close) < le * float(max_price_ratio)


def add_on_pullback_or_momentum_ok(
    close: float,
    last_entry_price: Any,
    max_price_ratio: float | None,
    *,
    allow_momentum_bypass: bool,
    trend_long_ok: bool,
    news_buy: bool,
) -> bool:
    """
    True when an add-on may proceed: usual pullback vs ``last_entry_price``, **or** when
    *allow_momentum_bypass* is set and the name still passes the trend prefilter or news buy path.

    Used by the live loop so adds are not limited to dip-only when momentum is still aligned.
    """
    if add_on_pullback_ok(close, last_entry_price, max_price_ratio):
        return True
    if allow_momentum_bypass and (bool(trend_long_ok) or bool(news_buy)):
        return True
    return False


def last_entry_within(
    symbol: str,
    window_minutes: float,
    *,
    tracked: dict[str, Any],
    now_dt: datetime,
) -> bool:
    """
    True when *symbol* is tracked with an open long (``qty`` or ``notional``) and the latest of ``entry_time``,
    ``last_scale_ts``, or ``last_add_time`` is fewer than *window_minutes* before *now_dt*.

    Used to skip a symbol for a cooldown after a fill. When *window_minutes* <= 0, returns False.
    ``merge_add_shares`` sets ``last_add_time`` on each add-on buy.
    """
    if window_minutes <= 0:
        return False
    key = str(symbol or "").strip().upper()
    row = tracked.get(key)
    if not isinstance(row, dict):
        return False
    if not tracked_row_has_open_long(row):
        return False
    stamps: list[datetime] = []
    for fld in ("last_add_time", "last_scale_ts", "entry_time"):
        raw = row.get(fld)
        if not raw:
            continue
        try:
            t = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            stamps.append(t)
        except Exception:
            continue
    if not stamps:
        return False
    last_t = max(stamps)
    if last_t.tzinfo is not None:
        if now_dt.tzinfo is not None:
            last_t = last_t.astimezone(now_dt.tzinfo).replace(tzinfo=None)
        else:
            last_t = last_t.replace(tzinfo=None)
    n = now_dt.replace(tzinfo=None)
    age_min = (n - last_t).total_seconds() / 60.0
    return age_min < float(window_minutes)


def last_tracker_fill_age_minutes(
    symbol: str,
    *,
    tracked: dict[str, Any],
    now_dt: datetime,
) -> float | None:
    """
    Minutes since the latest of ``entry_time``, ``last_scale_ts``, or ``last_add_time`` for a
    tracked row with an open long (``qty`` or ``notional``). Returns ``None`` if not tracked, no timestamps, or parse fails.
    """
    key = str(symbol or "").strip().upper()
    row = tracked.get(key)
    if not isinstance(row, dict):
        return None
    if not tracked_row_has_open_long(row):
        return None
    stamps: list[datetime] = []
    for fld in ("last_add_time", "last_scale_ts", "entry_time"):
        raw = row.get(fld)
        if not raw:
            continue
        try:
            t = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            stamps.append(t)
        except Exception:
            continue
    if not stamps:
        return None
    last_t = max(stamps)
    if last_t.tzinfo is not None:
        if now_dt.tzinfo is not None:
            last_t = last_t.astimezone(now_dt.tzinfo).replace(tzinfo=None)
        else:
            last_t = last_t.replace(tzinfo=None)
    n = now_dt.replace(tzinfo=None)
    return (n - last_t).total_seconds() / 60.0


def minutes_since_iso(ts: str | None, now_dt: datetime) -> float | None:
    """
    Minutes from an ISO timestamp string to ``now_dt``.

    If ``ts`` has a timezone, it is converted to ``now_dt``'s zone (if any) before
    subtracting as naive datetimes, matching wall-clock comparison in the loop.
    Returns None if ``ts`` is missing or unparseable.
    """
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if t.tzinfo is not None:
            if now_dt.tzinfo is not None:
                t = t.astimezone(now_dt.tzinfo).replace(tzinfo=None)
            else:
                t = t.replace(tzinfo=None)
            n = now_dt.replace(tzinfo=None)
        else:
            n = now_dt.replace(tzinfo=None)
        return (n - t).total_seconds() / 60.0
    except Exception:
        return None


def get_tracked_entry_info(tracker_path: Path | str | None, symbol: str) -> dict[str, Any]:
    """Return one symbol's row from the tracker file, or {} if missing / invalid."""
    path = Path(tracker_path) if tracker_path is not None else _user_path("default")
    data = load(base_path=path)
    key = str(symbol).upper()
    row = data.get(key) or data.get(symbol) or {}
    return row if isinstance(row, dict) else {}
