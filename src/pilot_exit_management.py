"""Broker-backed exit registration for bounded live pilot equity positions."""

from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.limited_live_pilot import classify_broker_positions, load_pilot_state
from src.safe_sell import submit_fractional_full_close
from src.strategy import ExitReason

log = logging.getLogger(__name__)


def _event(message: str, *args: Any) -> None:
    text = message % args if args else message
    log.info(text)
    print(text, flush=True)


def _safe_user(user_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(user_id or "default"))


def _value(obj: Any, *names: str) -> Any:
    if isinstance(obj, Mapping):
        for name in names:
            if name in obj:
                return obj.get(name)
        return None
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def _float(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _symbol(position: Any) -> str:
    return str(_value(position, "symbol", "asset_symbol") or "").strip().upper()


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value or datetime.now(timezone.utc).isoformat())


def historical_pilot_symbols(data_dir: Path | str, user_id: str, day: str) -> list[str]:
    base = Path(data_dir) / "limited_live_pilot"
    if not base.exists():
        return []
    out: list[str] = []
    for path in sorted(base.glob(f"*_{_safe_user(user_id)}.json"), reverse=True):
        state_day = path.name[:10]
        if state_day >= str(day):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        dispatch_attempts = int(_float(payload.get("broker_dispatch_attempts"), 0.0) or 0)
        has_lineage = bool(payload.get("broker_dispatch_attempted")) or dispatch_attempts > 0 or bool(payload.get("consumed_submission"))
        if not has_lineage:
            continue
        for item in payload.get("submitted_symbols") or []:
            sym = str(item or "").strip().upper()
            if sym and sym not in out:
                out.append(sym)
    return out


def pilot_state_with_historical_symbols(data_dir: Path | str, user_id: str, day: str) -> dict[str, Any]:
    state = dict(load_pilot_state(data_dir, user_id, day))
    submitted = [str(sym).strip().upper() for sym in state.get("submitted_symbols") or [] if str(sym).strip()]
    for sym in historical_pilot_symbols(data_dir, user_id, day):
        if sym not in submitted:
            submitted.append(sym)
    if submitted:
        state["submitted_symbols"] = submitted
    return state


def _latest_lifecycle_lineage(data_dir: Path | str, user_id: str, symbol: str) -> dict[str, Any]:
    base = Path(data_dir) / "trade_attribution" / "daily"
    if not base.exists():
        return {}
    sym = str(symbol or "").strip().upper()
    best: dict[str, Any] = {}
    for path in sorted(base.glob(f"*_{_safe_user(user_id)}.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        rows = payload.get("orders")
        if not isinstance(rows, Sequence):
            continue
        for row in reversed(list(rows)):
            if not isinstance(row, Mapping) or str(row.get("symbol") or "").strip().upper() != sym:
                continue
            if str(row.get("action") or row.get("side") or "").strip().lower() not in {"buy", "long"}:
                continue
            status = str(row.get("status") or "").lower()
            filled_qty = _float(row.get("filled_qty"), 0.0) or 0.0
            if filled_qty <= 0.0 and "filled" not in status and not row.get("submitted"):
                continue
            best = dict(row)
            break
        if best:
            break
    return best


def exit_status_path(data_dir: Path | str, user_id: str, day: str) -> Path:
    return Path(data_dir) / "position_management" / f"{day}_{_safe_user(user_id)}.json"


def load_exit_status(data_dir: Path | str, user_id: str, day: str) -> dict[str, Any]:
    path = exit_status_path(data_dir, user_id, day)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "schema_version": 1,
            "user_id": str(user_id or "default"),
            "trading_date": str(day),
            "positions": {},
            "cycles": [],
        }
    return payload if isinstance(payload, dict) else {"positions": {}, "cycles": []}


def record_exit_status(data_dir: Path | str, user_id: str, day: str, symbol: str, record: Mapping[str, Any]) -> Path:
    path = exit_status_path(data_dir, user_id, day)
    payload = load_exit_status(data_dir, user_id, day)
    payload["schema_version"] = 1
    payload["user_id"] = str(user_id or "default")
    payload["trading_date"] = str(day)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    positions = payload.setdefault("positions", {})
    if not isinstance(positions, dict):
        positions = {}
        payload["positions"] = positions
    positions[str(symbol or "").strip().upper()] = dict(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def broker_pilot_position_report(
    *,
    config: Mapping[str, Any],
    positions: Sequence[Any],
    data_dir: Path | str,
    user_id: str,
    day: str,
) -> dict[str, Any]:
    state = pilot_state_with_historical_symbols(data_dir, user_id, day)
    return classify_broker_positions(config, positions, pilot_state=state)


def classification_map(report: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in report.get("position_classifications") or []:
        if isinstance(row, Mapping):
            sym = str(row.get("symbol") or "").strip().upper()
            cls = str(row.get("classification") or "").strip().upper()
            if sym and cls:
                out[sym] = cls
    return out


def pilot_position_metadata(data_dir: Path | str, user_id: str, symbol: str, broker_position: Any) -> dict[str, Any]:
    row = _latest_lifecycle_lineage(data_dir, user_id, symbol)
    qty = _float(_value(broker_position, "qty", "quantity"), 0.0) or 0.0
    entry_price = (
        _float(row.get("filled_avg_price"))
        or _float(row.get("avg_entry_price"))
        or _float(_value(broker_position, "avg_entry_price", "average_entry_price"))
        or _float(_value(broker_position, "current_price", "market_price", "price"))
    )
    return {
        "symbol": str(symbol or "").strip().upper(),
        "qty": abs(qty),
        "side": "long" if qty >= 0 else "short",
        "entry_price": entry_price,
        "broker_order_id": row.get("broker_order_id") or row.get("order_id"),
        "route": row.get("route") or "trend_long",
        "source": row.get("source") or row.get("route") or "trend_long",
        "strategy": row.get("strategy") or row.get("route") or "trend_long",
        "opened_at": row.get("filled_at") or row.get("event_timestamp_utc") or row.get("timestamp"),
        "recovered_from_broker": bool(row.get("recovered") or row.get("event_origin") == "broker_reconciliation"),
        "lineage_found": bool(row),
    }


def evaluate_pilot_position(ctx: Any, broker_position: Any, *, classification: str) -> dict[str, Any]:
    symbol = _symbol(broker_position)
    day = ctx.now.date().isoformat() if isinstance(ctx.now, datetime) else str(ctx.now)[:10]
    meta = pilot_position_metadata(ctx.data_dir, ctx.user_id, symbol, broker_position)
    qty = float(meta.get("qty") or 0.0)
    entry_price = _float(meta.get("entry_price"))
    if not symbol or qty <= 0.0:
        return {"symbol": symbol, "evaluated": False, "reason": "invalid_position_quantity"}
    _event(
        "POSITION_MANAGER_POSITION_LOADED user_id=%s symbol=%s classification=%s qty=%.9g strategy=%s broker_order_id=%s recovered_from_broker=%s",
        ctx.user_id,
        symbol,
        classification,
        qty,
        meta.get("strategy") or "n/a",
        meta.get("broker_order_id") or "n/a",
        str(bool(meta.get("recovered_from_broker"))).lower(),
    )
    if not entry_price or entry_price <= 0.0:
        rec = {
            **meta,
            "classification": classification,
            "last_exit_eval_at": _iso(ctx.now),
            "exit_manager_healthy": False,
            "exit_health_reason": "entry_price_unavailable",
        }
        record_exit_status(ctx.data_dir, ctx.user_id, day, symbol, rec)
        text = (
            "POSITION_MANAGER_EVAL user_id=%s symbol=%s classification=%s qty=%.9g entry_price=n/a exit_allowed=false exit_reason=entry_price_unavailable",
            ctx.user_id,
            symbol,
            classification,
            qty,
        )
        log.warning(text[0], *text[1:])
        print(text[0] % text[1:], flush=True)
        return rec
    quote = ctx.broker.get_latest_quote(symbol)
    current_price = None
    if quote is not None:
        current_price = _float(getattr(quote, "reference_mid", lambda fallback: fallback)(entry_price))
    if current_price is None:
        current_price = _float(_value(broker_position, "current_price", "market_price", "price"), entry_price)
    pnl = (float(current_price) - float(entry_price)) * qty
    pnl_pct = ((float(current_price) - float(entry_price)) / float(entry_price)) * 100.0
    status = load_exit_status(ctx.data_dir, ctx.user_id, day)
    prior = ((status.get("positions") or {}).get(symbol) or {}) if isinstance(status, Mapping) else {}
    high_water = max(
        float(entry_price),
        float(current_price),
        _float(prior.get("high_water_mark"), entry_price) or float(entry_price),
        _float(_value(broker_position, "highest_price"), entry_price) or float(entry_price),
    )
    stop_pct = _float(getattr(ctx.engine.strategy, "stop_loss_pct", None))
    take_profit_pct = _float(getattr(ctx.engine.strategy, "take_profit_pct", None))
    trailing_enabled = bool(getattr(ctx.engine.strategy, "use_trailing_stop", False))
    trailing_pct = _float(getattr(ctx.engine.strategy, "trailing_stop_pct", None))
    stop_hit = bool(stop_pct is not None and pnl_pct <= -abs(stop_pct))
    trailing_hit = bool(trailing_enabled and trailing_pct is not None and current_price <= high_water * (1.0 - trailing_pct / 100.0))
    take_profit_hit = bool(take_profit_pct is not None and pnl_pct >= abs(take_profit_pct))
    _event(
        "POSITION_MANAGER_EVAL user_id=%s symbol=%s classification=%s qty=%.9g entry_price=%.6f current_price=%.6f unrealized_pnl=%.4f unrealized_pnl_pct=%.4f strategy=%s broker_order_id=%s recovered_from_broker=%s stop_evaluated=true trailing_evaluated=%s take_profit_evaluated=true exit_allowed=true exit_reason=none",
        ctx.user_id,
        symbol,
        classification,
        qty,
        float(entry_price),
        float(current_price),
        pnl,
        pnl_pct,
        meta.get("strategy") or "trend_long",
        meta.get("broker_order_id") or "n/a",
        str(bool(meta.get("recovered_from_broker"))).lower(),
        str(trailing_enabled).lower(),
    )
    _event("STOP_EVALUATED user_id=%s symbol=%s stop_pct=%s hit=%s pnl_pct=%.4f", ctx.user_id, symbol, stop_pct, str(stop_hit).lower(), pnl_pct)
    _event("TRAIL_EVALUATED user_id=%s symbol=%s enabled=%s trailing_pct=%s high_water_mark=%.6f hit=%s", ctx.user_id, symbol, str(trailing_enabled).lower(), trailing_pct, high_water, str(trailing_hit).lower())
    _event("TAKE_PROFIT_EVALUATED user_id=%s symbol=%s take_profit_pct=%s hit=%s pnl_pct=%.4f", ctx.user_id, symbol, take_profit_pct, str(take_profit_hit).lower(), pnl_pct)
    mins_to_close = None
    if isinstance(ctx.now, datetime):
        close_dt = ctx.now.replace(hour=16, minute=0, second=0, microsecond=0)
        mins_to_close = max(0.0, (close_dt - ctx.now).total_seconds() / 60.0)
    _event(
        "EOD_POSITION_EVALUATED user_id=%s symbol=%s classification=%s eod_flatten_required=%s minutes_to_close=%s",
        ctx.user_id,
        symbol,
        classification,
        str(bool(((ctx.config.get("trading_control") or {}).get("live_pilot") or {}).get("eod_flatten_required", True))).lower(),
        "n/a" if mins_to_close is None else f"{mins_to_close:.1f}",
    )
    bars = 0
    opened_at = str(meta.get("opened_at") or "")
    if opened_at and isinstance(ctx.now, datetime):
        try:
            opened = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
            bars = max(0, (ctx.now.date() - opened.astimezone(ctx.now.tzinfo or timezone.utc).date()).days)
        except Exception:
            bars = 0
    exit_signal = ctx.engine.check_exit(
        symbol,
        float(entry_price),
        float(current_price),
        bars,
        None,
        None,
        partial_taken=False,
        trail_high=high_water,
        current_qty=max(1, int(math.ceil(qty))),
        minutes_held=None,
        log_exit_context=False,
    )
    exit_reason = str(exit_signal.reason.value) if exit_signal is not None else None
    action_submitted = False
    if exit_signal is not None:
        _event("EXIT_DECISION user_id=%s symbol=%s classification=%s exit_reason=%s action_required=true", ctx.user_id, symbol, classification, exit_reason)
        if exit_signal.reason in {ExitReason.STOP_LOSS, ExitReason.TRAILING_STOP, ExitReason.TAKE_PROFIT, ExitReason.TIME_BARS, ExitReason.KILL_SWITCH, ExitReason.SIGNAL_EXIT}:
            if not ctx.skip_exit_for_action_cap(symbol, exit_reason or "pilot_exit") and not ctx.same_day_close_blocked(symbol, {"entry_time": opened_at}):
                order = submit_fractional_full_close(ctx.broker, symbol, reason=exit_reason or "pilot_exit", prefer_close_position=True)
                if order:
                    action_submitted = True
                    ctx.record_exit_action(symbol)
                    ctx.note_daily_risk_order(symbol, side="sell", full_exit=True)
                    ctx.log_sell_event(symbol, "stop_loss" if exit_signal.reason == ExitReason.STOP_LOSS else "take_profit", {"engine_reason": exit_reason, "qty": qty, "exit_price": float(current_price)})
                    _event("EOD_FLATTEN_ACTION user_id=%s symbol=%s classification=%s reason=%s order_id=%s", ctx.user_id, symbol, classification, exit_reason, getattr(order, "id", None) or "n/a")
    else:
        _event("EXIT_DECISION user_id=%s symbol=%s classification=%s exit_reason=none action_required=false", ctx.user_id, symbol, classification)
    rec = {
        **meta,
        "classification": classification,
        "last_exit_eval_at": _iso(ctx.now),
        "current_price": float(current_price),
        "entry_price": float(entry_price),
        "unrealized_pnl": pnl,
        "unrealized_pnl_pct": pnl_pct,
        "high_water_mark": high_water,
        "stop_evaluated": True,
        "stop_hit": stop_hit,
        "trailing_evaluated": trailing_enabled,
        "trailing_hit": trailing_hit,
        "take_profit_evaluated": True,
        "take_profit_hit": take_profit_hit,
        "eod_flatten_registration": True,
        "exit_manager_healthy": True,
        "exit_reason": exit_reason,
        "exit_action_submitted": action_submitted,
    }
    record_exit_status(ctx.data_dir, ctx.user_id, day, symbol, rec)
    return rec


def position_management_status_report(
    *,
    config: Mapping[str, Any],
    data_dir: Path | str,
    user_id: str,
    day: str,
    positions: Sequence[Any],
) -> dict[str, Any]:
    report = broker_pilot_position_report(config=config, positions=positions, data_dir=data_dir, user_id=user_id, day=day)
    classes = classification_map(report)
    managed = sorted(sym for sym, cls in classes.items() if cls == "PILOT_MANAGED")
    protected = sorted(sym for sym, cls in classes.items() if cls == "PREEXISTING_ALLOWED")
    status = load_exit_status(data_dir, user_id, day)
    rows = status.get("positions") if isinstance(status.get("positions"), Mapping) else {}
    registered = sorted(sym for sym in managed if isinstance(rows.get(sym), Mapping) and rows[sym].get("last_exit_eval_at"))
    missing = sorted(sym for sym in managed if sym not in registered)
    last_by_symbol = {sym: (rows.get(sym) or {}).get("last_exit_eval_at") for sym in registered}
    return {
        "managed_positions_registered_for_exit": registered,
        "managed_positions_missing_exit_registration": missing,
        "last_exit_cycle_at": max([v for v in last_by_symbol.values() if v] or [None]),
        "last_exit_eval_by_symbol": last_by_symbol,
        "last_iwm_exit_eval_at": last_by_symbol.get("IWM"),
        "exit_manager_healthy": not missing,
        "eod_flatten_registration": all(bool((rows.get(sym) or {}).get("eod_flatten_registration")) for sym in managed) if managed else True,
        "protected_preexisting_positions": protected,
        "exit_management_status": "healthy" if not missing else "stale_or_missing",
    }


def position_management_status_main(argv: list[str] | None = None) -> int:
    import argparse

    from src.config_loader import load_config
    from src.trading_control import resolve_trading_mode
    from src.user_manager import UserManager

    parser = argparse.ArgumentParser(description="Read-only bounded pilot position-management status")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--user", default="live_bot")
    parser.add_argument("--date", required=True)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = args.project_root
    base = load_config(root / "config" / "default.yaml")
    mgr = UserManager(base, users_path=root / "config" / "users.yaml", selected_user_id=args.user)
    ctx = mgr.get_user(args.user)
    broker = mgr.get_broker(args.user)
    positions = list(broker.get_positions() or [])
    report = position_management_status_report(
        config=ctx.config,
        data_dir=getattr(ctx, "data_dir", None) or (root / "data"),
        user_id=args.user,
        day=args.date,
        positions=positions,
    )
    report.update(
        {
            "configured_mode": str(((ctx.config.get("trading_control") or {}).get("mode")) or "missing"),
            "effective_mode": resolve_trading_mode(ctx.config, paper=ctx.paper, live_operation=not bool(ctx.paper)).mode,
            "broker_positions_total": len(positions),
            "position_classifications": broker_pilot_position_report(
                config=ctx.config,
                positions=positions,
                data_dir=getattr(ctx, "data_dir", None) or (root / "data"),
                user_id=args.user,
                day=args.date,
            ).get("position_classifications", []),
        }
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for key, value in report.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, sort_keys=True)
            print(f"{key}: {value}")
    return 0 if report.get("exit_manager_healthy") else 1
