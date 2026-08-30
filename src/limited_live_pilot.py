"""Bounded live-equity pilot guardrails."""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import fcntl
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
DEFAULT_ALLOWED_STRATEGIES = ("trend_long",)


@dataclass(frozen=True)
class PilotLimits:
    enabled: bool
    allowed_strategies: tuple[str, ...]
    max_trades_per_day: int
    max_entry_submissions_per_day: int
    max_entry_fills_per_day: int
    max_open_positions: int
    max_notional_per_trade: float
    max_total_deployed_notional: float
    max_daily_loss_usd: float
    allow_short_selling: bool
    allow_add_to_existing: bool
    allow_replacements: bool
    allow_reallocation: bool
    allow_overnight: bool
    eod_flatten_required: bool
    preexisting_position_allowlist: tuple[str, ...]


@dataclass(frozen=True)
class PilotDecision:
    allowed: bool
    reason: str | None
    reserved: bool = False
    state_path: Path | None = None


def trading_day_et(now: datetime | None = None) -> str:
    value = now or datetime.now(ET)
    if value.tzinfo is None:
        value = value.replace(tzinfo=ET)
    return value.astimezone(ET).date().isoformat()


def live_pilot_config(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    tc = (config or {}).get("trading_control") if isinstance(config, Mapping) else {}
    if not isinstance(tc, Mapping):
        return {}
    raw = tc.get("live_pilot")
    return raw if isinstance(raw, Mapping) else {}


def _bool(raw: Any, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def _float(raw: Any, default: float) -> float:
    try:
        out = float(raw)
    except (TypeError, ValueError):
        return default
    return out if out == out else default


def _int(raw: Any, default: int) -> int:
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _symbols(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple, set)):
        return ()
    out: list[str] = []
    for item in raw:
        symbol = str(item or "").strip().upper()
        if symbol and symbol not in out:
            out.append(symbol)
    return tuple(out)


def pilot_limits(config: Mapping[str, Any] | None) -> PilotLimits:
    raw = live_pilot_config(config)
    strategies = raw.get("allowed_strategies")
    if isinstance(strategies, (list, tuple, set)):
        allowed = tuple(str(item).strip() for item in strategies if str(item).strip())
    else:
        allowed = DEFAULT_ALLOWED_STRATEGIES
    return PilotLimits(
        enabled=_bool(raw.get("enabled"), False),
        allowed_strategies=allowed or DEFAULT_ALLOWED_STRATEGIES,
        max_trades_per_day=max(0, _int(raw.get("max_trades_per_day"), 1)),
        max_entry_submissions_per_day=max(0, _int(raw.get("max_entry_submissions_per_day"), 1)),
        max_entry_fills_per_day=max(0, _int(raw.get("max_entry_fills_per_day"), 1)),
        max_open_positions=max(0, _int(raw.get("max_open_positions"), 1)),
        max_notional_per_trade=max(0.0, min(100.0, _float(raw.get("max_notional_per_trade"), 100.0))),
        max_total_deployed_notional=max(0.0, min(100.0, _float(raw.get("max_total_deployed_notional"), 100.0))),
        max_daily_loss_usd=max(0.0, min(25.0, _float(raw.get("max_daily_loss_usd"), 25.0))),
        allow_short_selling=_bool(raw.get("allow_short_selling"), False),
        allow_add_to_existing=_bool(raw.get("allow_add_to_existing"), False),
        allow_replacements=_bool(raw.get("allow_replacements"), False),
        allow_reallocation=_bool(raw.get("allow_reallocation"), False),
        allow_overnight=_bool(raw.get("allow_overnight"), False),
        eod_flatten_required=_bool(raw.get("eod_flatten_required"), True),
        preexisting_position_allowlist=_symbols(raw.get("preexisting_position_allowlist")),
    )


def preexisting_position_allowlist(config: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Return configured manual holdings that the bounded pilot must not touch."""

    return pilot_limits(config).preexisting_position_allowlist


def is_preexisting_position_symbol(config: Mapping[str, Any] | None, symbol: Any) -> bool:
    sym = str(symbol or "").strip().upper()
    return bool(sym and sym in set(preexisting_position_allowlist(config)))


def _position_value(position: Any, *names: str) -> Any:
    if isinstance(position, Mapping):
        for name in names:
            if name in position:
                return position.get(name)
        return None
    for name in names:
        value = getattr(position, name, None)
        if value is not None:
            return value
    return None


def _position_symbol(position: Any) -> str:
    return str(_position_value(position, "symbol", "asset_symbol") or "").strip().upper()


def _position_notional(position: Any) -> float:
    raw = _position_value(position, "market_value", "notional", "current_value")
    value = _float(raw, 0.0)
    if value:
        return abs(value)
    qty = abs(_float(_position_value(position, "qty", "quantity"), 0.0))
    price = _float(_position_value(position, "current_price", "price", "market_price"), 0.0)
    return qty * price if qty > 0 and price > 0 else 0.0


def classify_broker_positions(
    config: Mapping[str, Any] | None,
    positions: Any,
    *,
    pilot_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Separate account-wide manual holdings from bounded-pilot exposure."""

    if positions is None or isinstance(positions, str):
        return {
            "broker_positions_total": "unknown",
            "preexisting_allowed_positions": 0,
            "pilot_managed_positions": 0,
            "unknown_positions": "unknown",
            "preexisting_allowed_notional": 0.0,
            "pilot_deployed_notional": _float((pilot_state or {}).get("deployed_notional"), 0.0),
            "total_broker_notional": "unknown",
            "position_classifications": [],
        }
    rows = list(positions)
    allowlisted = set(preexisting_position_allowlist(config))
    submitted = {str(item or "").strip().upper() for item in (pilot_state or {}).get("submitted_symbols", []) if item}
    classifications: list[dict[str, Any]] = []
    preexisting_count = 0
    pilot_count = 0
    unknown_count = 0
    preexisting_notional = 0.0
    pilot_notional_from_broker = 0.0
    total_notional = 0.0
    for row in rows:
        symbol = _position_symbol(row)
        notional = _position_notional(row)
        total_notional += notional
        if symbol in allowlisted:
            classification = "PREEXISTING_ALLOWED"
            preexisting_count += 1
            preexisting_notional += notional
        elif symbol in submitted:
            classification = "PILOT_MANAGED"
            pilot_count += 1
            pilot_notional_from_broker += notional
        else:
            classification = "UNKNOWN"
            unknown_count += 1
        classifications.append({"symbol": symbol, "classification": classification, "notional": round(notional, 4)})
    pilot_deployed = _float((pilot_state or {}).get("deployed_notional"), 0.0) or pilot_notional_from_broker
    return {
        "broker_positions_total": len(rows),
        "preexisting_allowed_positions": preexisting_count,
        "pilot_managed_positions": pilot_count,
        "unknown_positions": unknown_count,
        "preexisting_allowed_notional": round(preexisting_notional, 4),
        "pilot_deployed_notional": round(pilot_deployed, 4),
        "total_broker_notional": round(total_notional, 4),
        "position_classifications": classifications,
    }


def pilot_state_path(data_dir: Path | str, user_id: str, day: str | None = None) -> Path:
    safe_user = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(user_id or "default"))
    return Path(data_dir) / "limited_live_pilot" / f"{day or trading_day_et()}_{safe_user}.json"


def empty_pilot_state(user_id: str, day: str | None = None, *, environment: str = "live") -> dict[str, Any]:
    trading_date = day or trading_day_et()
    return {
        "schema_version": 2,
        "user_id": str(user_id or "default"),
        "environment": environment,
        "trading_date": trading_date,
        "created_at": None,
        "updated_at": None,
        "order_intents": 0,
        "entry_submissions": 0,
        "broker_dispatch_attempts": 0,
        "active_submission_reservations": 0,
        "released_reservations": 0,
        "accepted_entry_orders": 0,
        "entry_fills": 0,
        "open_positions": 0,
        "deployed_notional": 0.0,
        "realized_plus_unrealized_pnl": 0.0,
        "submitted_symbols": [],
        "entry_locked": False,
        "entry_lock_reason": None,
        "lock_reasons": [],
    }


def _state_record_trading_date(state: Mapping[str, Any], fallback: str | None = None) -> str | None:
    raw = state.get("trading_date") or state.get("date") or state.get("trading_date_et")
    if raw:
        return str(raw).strip()
    return fallback


def load_pilot_state(data_dir: Path | str, user_id: str, day: str | None = None) -> dict[str, Any]:
    requested_day = day or trading_day_et()
    path = pilot_state_path(data_dir, user_id, day)
    if not path.exists():
        return empty_pilot_state(user_id, requested_day)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = empty_pilot_state(user_id, requested_day)
        state.update({"entry_locked": True, "lock_reasons": ["pilot_state_unreadable"]})
        return state
    if not isinstance(payload, dict):
        state = empty_pilot_state(user_id, requested_day)
        state.update({"entry_locked": True, "lock_reasons": ["pilot_state_malformed"]})
        return state
    record_day = _state_record_trading_date(payload)
    if record_day != requested_day:
        state = empty_pilot_state(user_id, requested_day)
        state.update(
            {
                "prior_or_ambiguous_state_ignored": True,
                "ignored_state_record_trading_date": record_day,
                "ignored_state_path": str(path),
                "ignored_state_reason": "missing_trading_date" if not record_day else "different_trading_date",
                "historical_entry_submissions": _state_count(payload, "entry_submissions"),
                "historical_broker_dispatch_attempts": _state_count(payload, "broker_dispatch_attempts"),
                "historical_active_submission_reservations": _state_count(payload, "active_submission_reservations"),
                "historical_entry_locked": bool(payload.get("entry_locked")),
            }
        )
        return state
    state = empty_pilot_state(str(payload.get("user_id") or user_id), requested_day, environment=str(payload.get("environment") or "live"))
    state.update(payload)
    return state


def _write_pilot_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(dict(state), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _order_route(order: Any) -> str:
    for name in ("route", "strategy", "strategy_route", "source"):
        raw = getattr(order, name, None)
        if raw:
            return str(raw).strip()
    return ""


def _is_option_order(order: Any) -> bool:
    if getattr(order, "contract_symbol", None) or getattr(order, "option_symbol", None):
        return True
    symbol = str(getattr(order, "symbol", "") or "").strip().upper()
    return len(symbol) >= 15 and any(ch in symbol for ch in ("C", "P")) and symbol[-8:].isdigit()


def _order_notional(order: Any) -> float:
    raw = getattr(order, "notional", None)
    if raw not in (None, ""):
        return _float(raw, 0.0)
    qty = _float(getattr(order, "quantity", None), 0.0)
    price = _float(getattr(order, "expected_price", None), 0.0) or _float(getattr(order, "limit_price", None), 0.0)
    return qty * price if qty > 0 and price > 0 else 0.0


def _trading_mode(config: Mapping[str, Any] | None) -> str:
    tc = (config or {}).get("trading_control") if isinstance(config, Mapping) else {}
    if not isinstance(tc, Mapping):
        return ""
    return str(tc.get("mode") or "").strip().lower()


def _decimal_floor(value: float, quantum: str) -> float:
    return float(Decimal(str(max(0.0, value))).quantize(Decimal(quantum), rounding=ROUND_DOWN))


def _reference_price(order: Any, fallback_price: Any = None) -> float:
    for raw in (
        fallback_price,
        getattr(order, "expected_price", None),
        getattr(order, "reference_price", None),
        getattr(order, "limit_price", None),
        getattr(order, "mid_price", None),
        getattr(order, "price", None),
    ):
        try:
            px = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(px) and px > 0.0:
            return px
    return 0.0


def _asset_fractionable(broker: Any | None, symbol: str) -> bool:
    if broker is None:
        return True
    for name in ("is_asset_fractionable", "asset_fractionable"):
        fn = getattr(broker, name, None)
        if callable(fn):
            try:
                return bool(fn(symbol))
            except Exception:
                log.exception("LIMITED_LIVE_FRACTIONABLE_LOOKUP_FAILED symbol=%s", symbol)
                return False
    raw = getattr(broker, "fractionable", None)
    if isinstance(raw, Mapping):
        return bool(raw.get(symbol, True))
    return True


def adjust_pilot_order_size(
    config: Mapping[str, Any] | None,
    order: Any,
    *,
    data_dir: Path | str,
    user_id: str,
    broker: Any | None = None,
    reference_price: float | None = None,
    day: str | None = None,
    state: Mapping[str, Any] | None = None,
) -> PilotDecision:
    """Cap bounded live pilot equity buys before the broker-boundary validation."""

    limits = pilot_limits(config)
    path = pilot_state_path(data_dir, user_id, day)
    if _trading_mode(config) != "live" or not limits.enabled:
        return PilotDecision(True, None, state_path=path)
    symbol = str(getattr(order, "symbol", "") or "").strip().upper()
    side = str(getattr(order, "side", "") or "").strip().lower()
    route = _order_route(order)
    if symbol and symbol in set(limits.preexisting_position_allowlist):
        return PilotDecision(True, None, state_path=path)
    if side != "buy" or route not in set(limits.allowed_strategies) or _is_option_order(order):
        return PilotDecision(True, None, state_path=path)
    px = _reference_price(order, reference_price)
    if px <= 0.0:
        return PilotDecision(False, "invalid_reference_price", state_path=path)
    state = dict(state or load_pilot_state(data_dir, user_id, day))
    remaining = max(0.0, limits.max_total_deployed_notional - _float(state.get("deployed_notional"), 0.0))
    original_notional = _order_notional(order)
    if original_notional <= 0.0:
        return PilotDecision(False, "missing_order_notional", state_path=path)
    capped = min(original_notional, remaining, limits.max_notional_per_trade)
    if capped <= 0.0:
        return PilotDecision(False, "deployed_notional_above_cap", state_path=path)
    fractionable = _asset_fractionable(broker, symbol)
    if not fractionable:
        if px > capped + 1e-9:
            return PilotDecision(False, "minimum_share_cost_exceeds_cap", state_path=path)
        qty = math.floor(capped / px)
        final_qty = float(qty)
    else:
        final_qty = _decimal_floor(capped / px, "0.000001")
    final_notional = _decimal_floor(final_qty * px, "0.01")
    if final_qty <= 0.0 or final_notional <= 0.0:
        return PilotDecision(False, "final_pilot_notional_invalid", state_path=path)
    if final_notional > limits.max_notional_per_trade + 1e-9:
        return PilotDecision(False, "order_notional_above_cap", state_path=path)
    if _float(state.get("deployed_notional"), 0.0) + final_notional > limits.max_total_deployed_notional + 1e-9:
        return PilotDecision(False, "deployed_notional_above_cap", state_path=path)
    setattr(order, "_allocator_requested_notional", original_notional)
    setattr(order, "_allocator_requested_qty", _float(getattr(order, "quantity", None), 0.0))
    setattr(order, "_limited_live_original_notional", original_notional)
    setattr(order, "_limited_live_capped_notional", capped)
    setattr(order, "quantity", final_qty)
    setattr(order, "notional", final_notional)
    setattr(order, "expected_price", px)
    setattr(order, "_limited_live_sized", True)
    setattr(order, "_limited_live_final_quantity", final_qty)
    setattr(order, "_limited_live_final_notional", final_notional)
    setattr(order, "_limited_live_reference_price", px)
    if abs(final_notional - original_notional) > 1e-9 or abs(final_notional - capped) > 1e-9:
        log.info(
            "LIMITED_LIVE_SIZE_ADJUSTED original_notional=%.2f capped_notional=%.2f final_quantity=%.6f reference_price=%.4f final_notional=%.2f",
            original_notional,
            capped,
            final_qty,
            px,
            final_notional,
        )
    return PilotDecision(True, None, state_path=path)


def _state_count(state: Mapping[str, Any], key: str) -> int:
    return max(0, _int(state.get(key), 0))


def validate_pilot_order(
    config: Mapping[str, Any] | None,
    order: Any,
    *,
    data_dir: Path | str,
    user_id: str,
    day: str | None = None,
    state: Mapping[str, Any] | None = None,
) -> PilotDecision:
    limits = pilot_limits(config)
    path = pilot_state_path(data_dir, user_id, day)
    state = dict(state or load_pilot_state(data_dir, user_id, day))
    if not limits.enabled:
        return PilotDecision(False, "live_pilot_disabled", state_path=path)
    symbol = str(getattr(order, "symbol", "") or "").strip().upper()
    if symbol and symbol in set(limits.preexisting_position_allowlist):
        return PilotDecision(False, "preexisting_position_symbol", state_path=path)
    side = str(getattr(order, "side", "") or "").strip().lower()
    if _is_option_order(order):
        return PilotDecision(False, "options_blocked_live_pilot", state_path=path)
    if side != "buy":
        return PilotDecision(True, None, state_path=path)
    if getattr(order, "short", False) or side in {"sell_short", "short"}:
        return PilotDecision(False, "short_selling_blocked", state_path=path)
    route = _order_route(order)
    if not route:
        return PilotDecision(False, "metadata_route_missing", state_path=path)
    if route not in set(limits.allowed_strategies):
        return PilotDecision(False, "strategy_not_allowed", state_path=path)
    if bool(state.get("entry_locked")):
        return PilotDecision(False, "pilot_entry_locked", state_path=path)
    if _state_count(state, "active_submission_reservations") > 0:
        return PilotDecision(False, "active_submission_reservation", state_path=path)
    if _state_count(state, "broker_dispatch_attempts") >= limits.max_entry_submissions_per_day:
        return PilotDecision(False, "daily_submission_limit", state_path=path)
    if _state_count(state, "accepted_entry_orders") >= limits.max_trades_per_day:
        return PilotDecision(False, "daily_trade_limit", state_path=path)
    if _state_count(state, "entry_fills") >= limits.max_entry_fills_per_day:
        return PilotDecision(False, "daily_fill_limit", state_path=path)
    if _state_count(state, "open_positions") >= limits.max_open_positions:
        return PilotDecision(False, "open_position_limit", state_path=path)
    if symbol and symbol in {str(item).upper() for item in state.get("submitted_symbols", []) if item}:
        return PilotDecision(False, "same_symbol_reentry_blocked", state_path=path)
    notional = _order_notional(order)
    if notional <= 0:
        return PilotDecision(False, "missing_order_notional", state_path=path)
    if notional > limits.max_notional_per_trade + 1e-6:
        return PilotDecision(False, "order_notional_above_cap", state_path=path)
    deployed = _float(state.get("deployed_notional"), 0.0)
    if deployed + notional > limits.max_total_deployed_notional + 1e-6:
        return PilotDecision(False, "deployed_notional_above_cap", state_path=path)
    pnl = _float(state.get("realized_plus_unrealized_pnl"), 0.0)
    if pnl <= -abs(limits.max_daily_loss_usd):
        return PilotDecision(False, "daily_loss_lock", state_path=path)
    if bool(getattr(order, "replacement", False)) and not limits.allow_replacements:
        return PilotDecision(False, "replacement_blocked", state_path=path)
    if bool(getattr(order, "add_to_existing", False)) and not limits.allow_add_to_existing:
        return PilotDecision(False, "add_to_existing_blocked", state_path=path)
    if bool(getattr(order, "reallocation", False)) and not limits.allow_reallocation:
        return PilotDecision(False, "reallocation_blocked", state_path=path)
    return PilotDecision(True, None, state_path=path)


def reserve_pilot_submission(
    config: Mapping[str, Any] | None,
    order: Any,
    *,
    data_dir: Path | str,
    user_id: str,
    day: str | None = None,
) -> PilotDecision:
    """Atomically consume the one bounded-pilot broker dispatch attempt.

    This function must be called immediately before invoking a broker SDK method.
    It records a genuine dispatch attempt, not a mere order intent.
    """

    trading_date = day or trading_day_et()
    path = pilot_state_path(data_dir, user_id, trading_date)
    lock = path.with_suffix(path.suffix + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle: Any | None = None
    try:
        handle = lock.open("w", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(str(os.getpid()))
        handle.flush()
        state = load_pilot_state(data_dir, user_id, trading_date)
        decision = validate_pilot_order(config, order, data_dir=data_dir, user_id=user_id, day=trading_date, state=state)
        if not decision.allowed:
            return decision
        if str(getattr(order, "side", "") or "").strip().lower() != "buy":
            return PilotDecision(True, None, reserved=False, state_path=path)
        symbol = str(getattr(order, "symbol", "") or "").strip().upper()
        submitted = list(state.get("submitted_symbols") or [])
        if symbol:
            submitted.append(symbol)
        now_iso = datetime.now(ET).isoformat()
        reservation_id = f"pilot-{trading_date}-{os.getpid()}-{threading.get_ident()}"
        state.update(
            {
                "schema_version": 2,
                "user_id": str(user_id),
                "environment": "live",
                "trading_date": trading_date,
                "created_at": state.get("created_at") or now_iso,
                "updated_at": now_iso,
                "entry_submissions": _state_count(state, "entry_submissions") + 1,
                "broker_dispatch_attempts": _state_count(state, "broker_dispatch_attempts") + 1,
                "active_submission_reservations": _state_count(state, "active_submission_reservations") + 1,
                "submitted_symbols": submitted,
                "entry_locked": True,
                "entry_lock_reason": "broker_dispatch_attempt_reserved",
                "lock_reasons": sorted(set(list(state.get("lock_reasons") or []) + ["broker_dispatch_attempt_reserved"])),
                "reservation_id": reservation_id,
                "reservation_status": "active",
                "reservation_created_at": now_iso,
                "broker_dispatch_attempted": True,
                "broker_dispatch_timestamp": now_iso,
                "consumed_submission": True,
                "last_reservation_id": reservation_id,
                "last_submission_reserved_at": now_iso,
            }
        )
        _write_pilot_state(path, state)
        setattr(order, "_limited_live_reserved", True)
        setattr(order, "_limited_live_reservation_id", reservation_id)
        log.info(
            "LIMITED_LIVE_SUBMISSION_RESERVED date=%s user_id=%s symbol=%s route=%s source=%s strategy=%s reservation_id=%s broker_dispatch_attempted=true execution_allowed=true daily_reservation_count=%d daily_dispatch_attempt_count=%d daily_submission_count=%d daily_accepted_count=%d daily_fill_count=%d reason=broker_dispatch_attempt_reserved",
            trading_date,
            user_id,
            symbol or "?",
            _order_route(order) or "unknown",
            getattr(order, "source", None) or _order_route(order) or "unknown",
            getattr(order, "strategy", None) or _order_route(order) or "unknown",
            reservation_id,
            _state_count(state, "active_submission_reservations"),
            _state_count(state, "broker_dispatch_attempts"),
            _state_count(state, "entry_submissions"),
            _state_count(state, "accepted_entry_orders"),
            _state_count(state, "entry_fills"),
        )
        return PilotDecision(True, None, reserved=True, state_path=path)
    finally:
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def finalize_pilot_submission_reservation(
    data_dir: Path | str,
    user_id: str,
    *,
    day: str | None = None,
    order: Any | None = None,
    reason: str = "broker_dispatch_completed",
) -> Path:
    """Clear the active reservation marker after the broker dispatch path returns.

    The consumed dispatch attempt remains recorded. Releasing this marker only
    distinguishes an in-flight reservation from a completed broker attempt.
    """

    trading_date = day or trading_day_et()
    path = pilot_state_path(data_dir, user_id, trading_date)
    state = load_pilot_state(data_dir, user_id, trading_date)
    active = _state_count(state, "active_submission_reservations")
    if active > 0:
        state["active_submission_reservations"] = active - 1
        state["released_reservations"] = _state_count(state, "released_reservations") + 1
        state["reservation_status"] = "released"
        state["updated_at"] = datetime.now(ET).isoformat()
        state["last_submission_released_at"] = state["updated_at"]
        _write_pilot_state(path, state)
        log.info(
            "LIMITED_LIVE_SUBMISSION_RELEASED date=%s user_id=%s symbol=%s route=%s source=%s strategy=%s reservation_id=%s broker_dispatch_attempted=true execution_allowed=true daily_reservation_count=%d daily_dispatch_attempt_count=%d daily_submission_count=%d daily_accepted_count=%d daily_fill_count=%d reason=%s",
            trading_date,
            user_id,
            getattr(order, "symbol", None) or "?",
            _order_route(order) if order is not None else "unknown",
            getattr(order, "source", None) if order is not None else "unknown",
            getattr(order, "strategy", None) if order is not None else "unknown",
            getattr(order, "_limited_live_reservation_id", None) if order is not None else state.get("last_reservation_id"),
            _state_count(state, "active_submission_reservations"),
            _state_count(state, "broker_dispatch_attempts"),
            _state_count(state, "entry_submissions"),
            _state_count(state, "accepted_entry_orders"),
            _state_count(state, "entry_fills"),
            reason,
        )
    return path
