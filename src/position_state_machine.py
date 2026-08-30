"""Per-symbol position state for the live loop (persisted JSON).

Reduces same-day churn by tracking HOLD after buys and COOLDOWN / TRIMMED
after sells. Discretionary equity exits are skipped while a block is active;
stop-like exits (stop loss, trailing stop, option stop) still run.

Config: ``position_states`` in app YAML (see ``config/default.yaml``).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from src.strategy import ExitReason

log = logging.getLogger(__name__)

# Documented states (ENTERED / EXIT_PENDING reserved for future use)
ENTERED = "ENTERED"
HOLD = "HOLD"
TRIMMED = "TRIMMED"
EXIT_PENDING = "EXIT_PENDING"
COOLDOWN = "COOLDOWN"

_STATE_PATH_TMPL = "position_state_{user_id}.json"


def _machine_path(user_id: str, data_dir: Path | None) -> Path:
    base = data_dir or Path(__file__).resolve().parent.parent / "data"
    return base / _STATE_PATH_TMPL.format(user_id=str(user_id or "default"))


def _cfg(config: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = (dict(config or {}).get("position_states") or {}) if config is not None else {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "hold_after_buy_minutes": float(raw.get("hold_after_buy_minutes", 30)),
        "cooldown_after_sell_minutes": float(raw.get("cooldown_after_sell_minutes", 20)),
    }


def _as_utc_aware(now: datetime) -> datetime:
    """Live loop ``now`` is often naive America/New_York; ISO until values are UTC."""
    if now.tzinfo is not None:
        return now.astimezone(timezone.utc)
    try:
        import pytz

        et = pytz.timezone("America/New_York")
        return et.localize(now).astimezone(timezone.utc)
    except Exception:
        return now.replace(tzinfo=timezone.utc)


def _parse_until_utc(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        t = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if t.tzinfo is None:
            return t.replace(tzinfo=timezone.utc)
        return t.astimezone(timezone.utc)
    except Exception:
        return None


def load_machine(user_id: str, *, data_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    path = _machine_path(user_id, data_dir)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        log.warning("position_state: could not read %s", path, exc_info=True)
        return {}


def save_machine(
    data: dict[str, dict[str, Any]],
    user_id: str,
    *,
    data_dir: Path | None = None,
) -> None:
    path = _machine_path(user_id, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _prune_expired(
    data: dict[str, dict[str, Any]],
    now: datetime,
) -> dict[str, dict[str, Any]]:
    n = _as_utc_aware(now)
    out: dict[str, dict[str, Any]] = {}
    for sym, row in data.items():
        if not isinstance(row, dict):
            continue
        until = _parse_until_utc(row.get("until"))
        if until is not None and n >= until:
            continue
        out[str(sym).upper()] = row
    return out


def exit_reason_is_stop_like(reason: ExitReason | str) -> bool:
    """Exits that may proceed during HOLD / COOLDOWN (protective)."""
    v = reason.value if isinstance(reason, ExitReason) else str(reason)
    return v in frozenset(
        {
            ExitReason.STOP_LOSS.value,
            ExitReason.OPTION_STOP_LOSS.value,
            ExitReason.TRAILING_STOP.value,
        }
    )


def blocks_discretionary_stock_exit(
    symbol: str,
    user_id: str,
    data_dir: Path,
    now: datetime,
    config: Mapping[str, Any] | None,
) -> tuple[bool, str | None]:
    """
    When True, skip discretionary equity sells (trim, TP, news, risk rebalance, …).

    Stop-like reasons are filtered at the call site; this only encodes HOLD / TRIMMED / COOLDOWN timers.
    """
    cfg = _cfg(config)
    if not cfg["enabled"]:
        return False, None
    sym = str(symbol).strip().upper()
    if not sym:
        return False, None
    raw = load_machine(user_id, data_dir=data_dir)
    data = _prune_expired(raw, now)
    if len(data) != len(raw):
        save_machine(data, user_id, data_dir=data_dir)
    row = data.get(sym)
    if not isinstance(row, dict):
        return False, None
    st = str(row.get("state") or "")
    until = _parse_until_utc(row.get("until"))
    n = _as_utc_aware(now)
    if until is None or n >= until:
        return False, None
    if st == HOLD:
        return True, "position state HOLD (post-buy window)"
    if st in (COOLDOWN, TRIMMED):
        return True, "position state %s until cooldown ends" % st
    return False, None


def blocks_stock_rebuy_after_sell(
    symbol: str,
    user_id: str,
    data_dir: Path,
    now: datetime,
    config: Mapping[str, Any] | None,
) -> tuple[bool, str | None]:
    """When True, skip equity buys during the post-sell/TRIMMED cooldown window."""
    cfg = _cfg(config)
    if not cfg["enabled"]:
        return False, None
    sym = str(symbol).strip().upper()
    if not sym:
        return False, None
    raw = load_machine(user_id, data_dir=data_dir)
    data = _prune_expired(raw, now)
    if len(data) != len(raw):
        save_machine(data, user_id, data_dir=data_dir)
    row = data.get(sym)
    if not isinstance(row, dict):
        return False, None
    st = str(row.get("state") or "")
    if st not in (COOLDOWN, TRIMMED):
        return False, None
    until = _parse_until_utc(row.get("until"))
    n = _as_utc_aware(now)
    if until is None or n >= until:
        return False, None
    return True, "position state %s rebuy cooldown until %s" % (
        st,
        until.isoformat(),
    )


def record_buy_after_tracker_write(
    symbol: str,
    user_id: str,
    data_dir: Path | None,
    now: datetime,
    config: Mapping[str, Any] | None,
) -> None:
    """Call after a successful equity tracker write for a buy / add."""
    cfg = _cfg(config)
    if not cfg["enabled"]:
        return
    sym = str(symbol).strip().upper()
    if not sym:
        return
    try:
        from src.options_premium_risk import is_option_symbol

        if is_option_symbol(sym):
            return
    except Exception:
        pass
    data = _prune_expired(load_machine(user_id, data_dir=data_dir), now)
    hold_m = max(0.0, float(cfg["hold_after_buy_minutes"]))
    if hold_m <= 0:
        data.pop(sym, None)
        save_machine(data, user_id, data_dir=data_dir)
        return
    base = _as_utc_aware(now)
    until_dt = base + timedelta(minutes=hold_m)
    data[sym] = {
        "state": HOLD,
        "until": until_dt.astimezone(timezone.utc).isoformat(),
        "flat": False,
        "updated": datetime.now(timezone.utc).isoformat(),
    }
    save_machine(data, user_id, data_dir=data_dir)


def record_sell_after_exit(
    symbol: str,
    user_id: str,
    data_dir: Path,
    now: datetime,
    exit_reason: ExitReason | str,
    remaining_qty_after: int,
    config: Mapping[str, Any] | None,
) -> None:
    """Call after a submitted equity sell (see :meth:`LiveExitContext.record_engine_after_sell`)."""
    cfg = _cfg(config)
    if not cfg["enabled"]:
        return
    sym = str(symbol).strip().upper()
    if not sym:
        return
    try:
        from src.options_premium_risk import is_option_symbol

        if is_option_symbol(sym):
            return
    except Exception:
        pass
    cd_m = max(0.0, float(cfg["cooldown_after_sell_minutes"]))
    if cd_m <= 0:
        return
    data = _prune_expired(load_machine(user_id, data_dir=data_dir), now)
    base = _as_utc_aware(now)
    until = (base + timedelta(minutes=cd_m)).astimezone(timezone.utc).isoformat()
    rem = int(remaining_qty_after)
    flat = rem <= 0
    if exit_reason_is_stop_like(exit_reason):
        st = COOLDOWN
    else:
        st = TRIMMED if not flat else COOLDOWN
    data[sym] = {
        "state": st,
        "until": until,
        "flat": flat,
        "updated": datetime.now(timezone.utc).isoformat(),
    }
    save_machine(data, user_id, data_dir=data_dir)


def maybe_record_buy_from_tracker(
    symbol: str,
    user_id: str,
    *,
    data_dir: Path | None,
    config: Mapping[str, Any] | None = None,
) -> None:
    """Invoked from :mod:`position_tracker` after buy rows are saved (best-effort)."""
    try:
        from src.options_premium_risk import is_option_symbol

        if is_option_symbol(str(symbol).strip().upper()):
            return
    except Exception:
        pass
    if config is None:
        try:
            from src.config_loader import load_app_config

            config = load_app_config()
        except Exception:
            return
    d = data_dir or Path(__file__).resolve().parent.parent / "data"
    try:
        import pytz

        et = pytz.timezone("America/New_York")
        now_et = datetime.now(et)
    except Exception:
        now_et = datetime.now(timezone.utc)
    record_buy_after_tracker_write(symbol, user_id, d, now_et, config)
