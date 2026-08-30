"""Persistent options position tracking and kill-switch state."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.options_exit import compute_option_pnl_pct
from src.options_daily_limit import ET, build_options_daily_limit_usage
from src.options_premium_risk import is_option_position
from src.options_selector import parse_occ_equity_option_symbol

log = logging.getLogger(__name__)

_DEFAULT_STALE_SECONDS = 120.0
_DEFAULT_DAILY_LOSS_PCT = 1.0
_DEFAULT_STOP_LOSS_COUNT = 2


def _data_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data"


def options_state_path(user_id: str = "default", *, data_dir: Path | None = None) -> Path:
    return (data_dir or _data_dir()) / f"options_positions_{user_id}.json"


def _utc_now_iso(now: datetime | None = None) -> str:
    n = now or datetime.now(timezone.utc)
    if getattr(n, "tzinfo", None) is None:
        n = n.replace(tzinfo=timezone.utc)
    return n.astimezone(timezone.utc).isoformat()


def _trade_date_key(now: datetime | None = None) -> str:
    n = now or datetime.now(timezone.utc)
    if getattr(n, "tzinfo", None) is None:
        n = n.replace(tzinfo=timezone.utc)
    return n.astimezone(ET).date().isoformat()


def _load_state(user_id: str = "default", *, data_dir: Path | None = None) -> dict[str, Any]:
    path = options_state_path(user_id, data_dir=data_dir)
    if not path.exists():
        return {"meta": {}, "positions": {}, "history": [], "daily": {}}
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"meta": {}, "positions": {}, "history": [], "daily": {}}
        data.setdefault("meta", {})
        data.setdefault("positions", {})
        data.setdefault("history", [])
        data.setdefault("daily", {})
        return data
    except Exception:
        return {"meta": {}, "positions": {}, "history": [], "daily": {}}


def _save_state(
    state: Mapping[str, Any],
    user_id: str = "default",
    *,
    data_dir: Path | None = None,
) -> None:
    path = options_state_path(user_id, data_dir=data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=2, default=str)


def _as_float(raw: Any, default: float | None = None) -> float | None:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    if v != v:
        return default
    return v


def _as_int(raw: Any, default: int | None = None) -> int | None:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _stale_seconds(config: Mapping[str, Any] | None) -> float:
    opts = (config or {}).get("options")
    if isinstance(opts, Mapping):
        raw = opts.get("data_stale_max_age_seconds")
        v = _as_float(raw, _DEFAULT_STALE_SECONDS)
        if v is not None and v > 0:
            return float(v)
    return _DEFAULT_STALE_SECONDS


def _daily_loss_limit_pct(config: Mapping[str, Any] | None) -> float:
    opts = (config or {}).get("options")
    if isinstance(opts, Mapping):
        raw = opts.get("max_daily_loss_pct")
        v = _as_float(raw, _DEFAULT_DAILY_LOSS_PCT)
        if v is not None and v > 0:
            return float(v)
    return _DEFAULT_DAILY_LOSS_PCT


def _daily_loss_limit_dollars(config: Mapping[str, Any] | None) -> float:
    opts = (config or {}).get("options")
    if isinstance(opts, Mapping):
        for key in ("max_daily_options_loss_dollars", "max_daily_loss_dollars"):
            raw = opts.get(key)
            v = _as_float(raw, 0.0)
            if v is not None and v > 0:
                return float(v)
    return 0.0


def _daily_contract_limit(config: Mapping[str, Any] | None) -> int:
    opts = (config or {}).get("options")
    if isinstance(opts, Mapping):
        for key in ("max_option_contracts_per_day", "max_contracts_per_day"):
            raw = opts.get(key)
            v = _as_int(raw, 0)
            if v is not None and v > 0:
                return int(v)
    return 0


def _stop_loss_exit_limit(config: Mapping[str, Any] | None) -> int:
    opts = (config or {}).get("options")
    if isinstance(opts, Mapping):
        exits = opts.get("exits")
        if isinstance(exits, Mapping):
            raw = exits.get("stop_loss_exit_limit_per_day")
            v = _as_int(raw, _DEFAULT_STOP_LOSS_COUNT)
            if v is not None and v > 0:
                return int(v)
    return _DEFAULT_STOP_LOSS_COUNT


def _option_position_key(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def _underlying_right(symbol: str) -> tuple[str | None, str | None, float | None]:
    parsed = parse_occ_equity_option_symbol(str(symbol or "").strip().upper())
    if parsed is None:
        return None, None, None
    underlying, _exp, right, _strike = parsed
    return underlying, right, _strike


def _position_quote_age_seconds(quote: Any | None, now: datetime) -> float | None:
    if quote is None:
        return None
    ts = getattr(quote, "timestamp", None)
    if ts is None:
        return None
    try:
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return max(0.0, (now.astimezone(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return None


def _refresh_execution_manager(execution_manager: Any | None, snapshot: "OptionsPositionSnapshot") -> None:
    if execution_manager is None:
        return
    try:
        execution_manager.options_entry_blocked = bool(snapshot.block_new_entries)
        execution_manager.options_entry_block_reason = snapshot.block_reason
        execution_manager.options_entry_block_reasons = list(snapshot.block_reasons)
        execution_manager.options_kill_switch_on = bool(snapshot.kill_switch_on)
        execution_manager.options_kill_switch_reasons = list(snapshot.kill_switch_reasons)
        execution_manager.options_data_stale = bool(snapshot.data_stale)
        execution_manager.options_data_stale_reasons = list(snapshot.data_stale_reasons)
        execution_manager.options_open_underlying_right = set(snapshot.open_underlying_right)
        execution_manager.options_open_contracts = set(snapshot.open_contracts)
    except Exception:
        log.debug("options manager execution-manager refresh failed", exc_info=True)


@dataclass(frozen=True)
class OptionsPositionSnapshot:
    user_id: str
    updated_at: datetime
    open_count: int
    open_contracts: tuple[str, ...]
    open_underlying_right: tuple[str, ...]
    daily_realized_pl: float
    daily_unrealized_pl: float
    daily_total_pl: float
    daily_equity: float | None
    kill_switch_on: bool
    kill_switch_reasons: tuple[str, ...]
    block_new_entries: bool
    block_reasons: tuple[str, ...]
    block_reason: str | None
    data_stale: bool
    data_stale_reasons: tuple[str, ...]
    stop_loss_exits_today: int
    positions: tuple[dict[str, Any], ...] = ()


def _serialize_position_record(
    *,
    symbol: str,
    broker_row: Mapping[str, Any] | None,
    quote: Any | None,
    now: datetime,
    stale_limit_seconds: float,
    existing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    underlying, right, strike = _underlying_right(symbol)
    qty = abs(int(float((broker_row or {}).get("qty") or 0)))
    avg_entry = _as_float((broker_row or {}).get("avg_entry_price")) or 0.0
    cost_basis = _as_float((broker_row or {}).get("cost_basis")) or 0.0
    market_value = _as_float((broker_row or {}).get("market_value")) or 0.0
    unrealized_pl = _as_float((broker_row or {}).get("unrealized_pl"))
    if unrealized_pl is None and cost_basis:
        unrealized_pl = compute_option_pnl_pct(
            unrealized_pl=None,
            cost_basis=cost_basis,
            market_value=market_value if market_value > 0 else None,
        )
        if unrealized_pl is not None:
            unrealized_pl = unrealized_pl / 100.0 * abs(cost_basis)
    if unrealized_pl is None:
        if market_value > 0 and cost_basis:
            unrealized_pl = market_value + cost_basis if cost_basis < 0 else market_value - cost_basis
        else:
            unrealized_pl = 0.0
    premium_paid = abs(cost_basis) if abs(cost_basis) > 0 else (avg_entry * qty * 100.0 if avg_entry > 0 else 0.0)
    current_price = _as_float((broker_row or {}).get("current_price"))
    if current_price is None and quote is not None:
        current_price = _as_float(getattr(quote, "mid", None))
    if current_price is None and qty > 0 and market_value > 0:
        current_price = market_value / (qty * 100.0)
    current_value = (current_price or 0.0) * qty * 100.0 if current_price is not None else market_value
    dte = None
    if underlying is not None:
        parsed = parse_occ_equity_option_symbol(symbol)
        if parsed is not None:
            _u, exp, _r, _s = parsed
            dte = max(0, (exp - now.date()).days)
    quote_stale = False
    quote_age_seconds = None
    if quote is None:
        quote_stale = True
    else:
        quote_age_seconds = _position_quote_age_seconds(quote, now)
        if quote_age_seconds is None:
            quote_stale = True
        else:
            quote_stale = quote_age_seconds > float(stale_limit_seconds)
    rec = dict(existing or {})
    rec.update(
        {
            "symbol": symbol,
            "underlying": underlying,
            "right": right,
            "strike": strike,
            "status": "open",
            "qty": qty,
            "entry_time": rec.get("entry_time") or _utc_now_iso(now),
            "entry_price": avg_entry or _as_float(rec.get("entry_price")) or 0.0,
            "premium_paid": premium_paid,
            "current_price": current_price or 0.0,
            "current_value": current_value,
            "unrealized_pl": unrealized_pl,
            "realized_pl": _as_float(rec.get("realized_pl")) or 0.0,
            "dte": dte,
            "delta": _as_float(rec.get("delta")),
            "iv": _as_float(rec.get("iv")),
            "exit_reason": rec.get("exit_reason"),
            "exit_time": rec.get("exit_time"),
            "exit_price": _as_float(rec.get("exit_price")),
            "quote_bid": _as_float(getattr(quote, "bid", None)) if quote is not None else _as_float(rec.get("quote_bid")),
            "quote_ask": _as_float(getattr(quote, "ask", None)) if quote is not None else _as_float(rec.get("quote_ask")),
            "quote_spread_pct": _as_float(getattr(quote, "spread_pct", None)) if quote is not None else _as_float(rec.get("quote_spread_pct")),
            "quote_age_seconds": quote_age_seconds if quote_age_seconds is not None else _as_float(rec.get("quote_age_seconds")),
            "quote_stale": bool(quote_stale),
            "last_seen_at": _utc_now_iso(now),
        }
    )
    return rec


def _lookup_delta_from_chain(
    broker: Any,
    *,
    symbol: str,
    now: datetime,
    existing_delta: float | None = None,
) -> float | None:
    if existing_delta is not None:
        return existing_delta
    parsed = parse_occ_equity_option_symbol(symbol)
    if parsed is None:
        return None
    underlying, exp, right, strike = parsed
    get_chain = getattr(broker, "get_option_chain_candidates", None)
    if not callable(get_chain):
        return None
    try:
        chain = get_chain(
            underlying,
            expiration_date_gte=exp,
            expiration_date_lte=exp,
        )
    except Exception:
        return None
    for c in chain or []:
        if str(getattr(c, "symbol", "")).strip().upper() != str(symbol).strip().upper():
            continue
        raw = getattr(c, "delta", None)
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    return None


def _append_history_entry(state: dict[str, Any], record: Mapping[str, Any]) -> None:
    hist = state.setdefault("history", [])
    if not isinstance(hist, list):
        hist = []
        state["history"] = hist
    hist.append(dict(record))


def _slippage_bps(fill_price: float | None, intended_price: float | None, *, side: str = "buy") -> float | None:
    if fill_price is None or intended_price is None:
        return None
    try:
        fill = float(fill_price)
        intended = float(intended_price)
    except (TypeError, ValueError):
        return None
    if fill <= 0 or intended <= 0:
        return None
    if side == "sell":
        return (intended - fill) / intended * 10_000.0
    return (fill - intended) / intended * 10_000.0


def _daily_state(state: dict[str, Any], now: datetime) -> dict[str, Any]:
    daily = state.setdefault("daily", {})
    if not isinstance(daily, dict):
        daily = {}
        state["daily"] = daily
    key = _trade_date_key(now)
    row = daily.setdefault(
        key,
        {
            "realized_pl": 0.0,
            "unrealized_pl": 0.0,
            "stop_loss_exits": 0,
            "contracts_opened": 0,
            "kill_switch_on": False,
            "kill_switch_reasons": [],
            "block_new_entries": False,
            "block_reasons": [],
            "last_updated_at": _utc_now_iso(now),
        },
    )
    return row


def _count_today_stop_losses(history: list[dict[str, Any]], now: datetime) -> int:
    today = _trade_date_key(now)
    c = 0
    for row in history:
        if not isinstance(row, dict):
            continue
        et = str(row.get("exit_time") or "")
        if not et.startswith(today):
            continue
        reason = str(row.get("exit_reason") or "").strip().lower()
        if reason in {"option_stop_loss", "stop_loss"}:
            c += 1
    return c


def sync_options_positions(
    broker: Any,
    config: Mapping[str, Any] | None,
    *,
    user_id: str = "default",
    data_dir: Path | None = None,
    now: datetime | None = None,
    execution_manager: Any | None = None,
    log_updates: bool = True,
) -> OptionsPositionSnapshot:
    """
    Refresh open OCC positions from broker state, persist current metrics, and derive kill-switch flags.
    """
    now_dt = now or datetime.now(timezone.utc)
    if getattr(now_dt, "tzinfo", None) is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    state = _load_state(user_id, data_dir=data_dir)
    positions_map = state.setdefault("positions", {})
    if not isinstance(positions_map, dict):
        positions_map = {}
        state["positions"] = positions_map
    history = state.setdefault("history", [])
    if not isinstance(history, list):
        history = []
        state["history"] = history
    open_positions: list[dict[str, Any]] = []
    seen: set[str] = set()
    stale_reasons: list[str] = []
    open_underlying_right: list[str] = []
    open_contracts: list[str] = []
    daily_realized = 0.0
    daily_unrealized = 0.0
    daily_equity: float | None = None
    try:
        daily_equity = float(broker.get_equity())
    except Exception:
        daily_equity = None

    broker_positions = []
    try:
        broker_positions = [p for p in broker.get_positions() if is_option_position(p)]
    except Exception:
        broker_positions = []

    for row in broker_positions:
        sym = _option_position_key(row.get("symbol"))
        if not sym:
            continue
        seen.add(sym)
        quote = None
        quote_missing = False
        try:
            from src.live.options_chain import options_runtime_enabled

            if options_runtime_enabled(broker, dict(config or {})):
                get_quote = getattr(broker, "get_option_latest_quote", None)
                if callable(get_quote):
                    quote = get_quote(sym)
        except Exception:
            quote = None
        if quote is None:
            quote_missing = True
        stale_limit = _stale_seconds(config)
        quote_age_seconds = _position_quote_age_seconds(quote, now_dt) if quote is not None else None
        quote_stale = quote_missing or quote_age_seconds is None or quote_age_seconds > stale_limit
        if quote_stale:
            stale_reasons.append(f"{sym}:missing_quote" if quote_missing else f"{sym}:stale_quote")
        existing = positions_map.get(sym, {}) if isinstance(positions_map, dict) else {}
        rec = _serialize_position_record(
            symbol=sym,
            broker_row=row,
            quote=quote,
            now=now_dt,
            stale_limit_seconds=stale_limit,
            existing=existing if isinstance(existing, dict) else {},
        )
        if rec.get("delta") is None:
            # Preserve a previously known delta if present.
            if isinstance(existing, dict) and existing.get("delta") is not None:
                rec["delta"] = _as_float(existing.get("delta"))
            else:
                rec["delta"] = _lookup_delta_from_chain(
                    broker,
                    symbol=sym,
                    now=now_dt,
                    existing_delta=None,
                )
        if rec.get("iv") is None and isinstance(existing, dict) and existing.get("iv") is not None:
            rec["iv"] = _as_float(existing.get("iv"))
        if not existing or str(existing.get("status") or "").lower() != "open":
            log.info(
                "OPTIONS_POSITION_OPENED symbol=%s underlying=%s right=%s qty=%s premium_paid=%.2f current_value=%.2f unrealized_pl=%.2f dte=%s delta=%s iv=%s",
                sym,
                rec.get("underlying") or "",
                rec.get("right") or "",
                rec.get("qty", 0),
                float(rec.get("premium_paid") or 0.0),
                float(rec.get("current_value") or 0.0),
                float(rec.get("unrealized_pl") or 0.0),
                "n/a" if rec.get("dte") is None else int(rec.get("dte") or 0),
                "n/a" if rec.get("delta") is None else f"{float(rec.get('delta')):.3f}",
                "n/a" if rec.get("iv") is None else f"{float(rec.get('iv')):.3f}",
            )
        else:
            log.info(
                "OPTIONS_POSITION_UPDATE symbol=%s current_value=%.2f unrealized_pl=%.2f quote_stale=%s quote_age_seconds=%s",
                sym,
                float(rec.get("current_value") or 0.0),
                float(rec.get("unrealized_pl") or 0.0),
                bool(rec.get("quote_stale")),
                "n/a" if quote_age_seconds is None else f"{float(quote_age_seconds):.1f}",
            )
        positions_map[sym] = rec
        open_positions.append(dict(rec))
        if rec.get("underlying") and rec.get("right"):
            open_underlying_right.append(f"{rec.get('underlying')}|{rec.get('right')}")
        open_contracts.append(sym)
        daily_unrealized += float(rec.get("unrealized_pl") or 0.0)

    # Close records that no longer appear at the broker and were previously marked open.
    for sym, rec in list(positions_map.items()):
        if sym in seen:
            continue
        if not isinstance(rec, dict):
            continue
        if str(rec.get("status") or "").lower() != "open":
            continue
        rec = dict(rec)
        rec["status"] = "closed"
        rec.setdefault("exit_reason", "broker_position_missing")
        rec.setdefault("exit_time", _utc_now_iso(now_dt))
        rec.setdefault("exit_price", rec.get("current_price") or 0.0)
        rec.setdefault("realized_pl", rec.get("realized_pl") or 0.0)
        positions_map[sym] = rec
        _append_history_entry(state, rec)
        positions_map.pop(sym, None)
        log.info(
            "OPTIONS_EXIT_FILLED symbol=%s reason=%s exit_price=%.2f realized_pl=%.2f",
            sym,
            rec.get("exit_reason") or "broker_position_missing",
            float(rec.get("exit_price") or 0.0),
            float(rec.get("realized_pl") or 0.0),
        )

    # Daily aggregates from history and open marks.
    day_row = _daily_state(state, now_dt)
    history_today = [
        row
        for row in history
        if isinstance(row, dict) and str(row.get("exit_time") or "").startswith(_trade_date_key(now_dt))
    ]
    daily_realized = sum(float(row.get("realized_pl") or 0.0) for row in history_today)
    daily_stop_losses = _count_today_stop_losses(history_today, now_dt)
    day_row["realized_pl"] = float(daily_realized)
    day_row["unrealized_pl"] = float(daily_unrealized)
    day_row["stop_loss_exits"] = int(daily_stop_losses)
    day_row["kill_switch_on"] = False
    day_row["kill_switch_reasons"] = []
    day_row["block_new_entries"] = False
    day_row["block_reasons"] = []
    day_row["last_updated_at"] = _utc_now_iso(now_dt)

    daily_total = float(daily_realized + daily_unrealized)
    daily_loss_limit_pct = _daily_loss_limit_pct(config)
    block_reasons: list[str] = []
    kill_reasons: list[str] = []
    daily_loss_limit_dollars = _daily_loss_limit_dollars(config)
    if daily_loss_limit_dollars > 0 and daily_total <= -daily_loss_limit_dollars + 1e-9:
        reason = "daily options P&L %.2f <= -%.2f dollars" % (daily_total, daily_loss_limit_dollars)
        block_reasons.append(reason)
        kill_reasons.append("daily_loss_dollars")
        log.info("OPTIONS_DAILY_RISK_BLOCK reason=daily_loss_dollars total=%.2f limit=%.2f", daily_total, daily_loss_limit_dollars)
    if daily_equity is not None and daily_equity > 0:
        if daily_total <= -abs(daily_equity) * (daily_loss_limit_pct / 100.0) + 1e-9:
            reason = "daily options P&L %.2f <= -%.2f%% of equity" % (daily_total, daily_loss_limit_pct)
            block_reasons.append(reason)
            kill_reasons.append("daily_loss")
            log.info("OPTIONS_DAILY_RISK_BLOCK reason=daily_loss_percent total=%.2f equity=%.2f limit_pct=%.2f", daily_total, daily_equity, daily_loss_limit_pct)
    contract_limit = _daily_contract_limit(config)
    usage = build_options_daily_limit_usage(
        root=data_dir or _data_dir(),
        user_id=user_id,
        environment="live" if not bool(getattr(broker, "paper", True)) else "paper",
        limit=contract_limit,
        trading_date=_trade_date_key(now_dt),
        now=now_dt,
    )
    entries_opened = int(usage.counted)
    day_row["entries"] = entries_opened
    if contract_limit > 0 and entries_opened >= contract_limit:
        reason = "daily option entries %d >= %d" % (entries_opened, contract_limit)
        block_reasons.append(reason)
        kill_reasons.append("entry_count")
        log.info("OPTIONS_DAILY_RISK_BLOCK reason=max_entries_per_day entries_opened=%d limit=%d", entries_opened, contract_limit)
    stop_limit = _stop_loss_exit_limit(config)
    if daily_stop_losses >= stop_limit:
        reason = "daily stop-loss exits %d >= %d" % (daily_stop_losses, stop_limit)
        block_reasons.append(reason)
        kill_reasons.append("stop_loss_count")
    if stale_reasons:
        block_reasons.append("option data stale/missing: " + ", ".join(stale_reasons[:6]))
        kill_reasons.append("stale_data")
    kill_switch_on = bool(kill_reasons)
    block_new_entries = bool(block_reasons)
    block_reason = "; ".join(block_reasons) if block_reasons else None
    if kill_switch_on:
        day_row["kill_switch_on"] = True
        day_row["kill_switch_reasons"] = list(kill_reasons)
        log.info(
            "OPTIONS_KILL_SWITCH_ON reasons=%s daily_realized=%.2f daily_unrealized=%.2f total=%.2f equity=%s stop_losses=%d",
            ",".join(kill_reasons),
            float(daily_realized),
            float(daily_unrealized),
            float(daily_total),
            "n/a" if daily_equity is None else f"{daily_equity:.2f}",
            int(daily_stop_losses),
        )
    if block_new_entries:
        day_row["block_new_entries"] = True
        day_row["block_reasons"] = list(block_reasons)
        log.info(
            "OPTIONS_ENTRY_BLOCKED reasons=%s",
            block_reason or "unknown",
        )
    state["meta"] = {
        "updated_at": _utc_now_iso(now_dt),
        "user_id": user_id,
    }
    _save_state(state, user_id, data_dir=data_dir)
    snapshot = OptionsPositionSnapshot(
        user_id=user_id,
        updated_at=now_dt,
        open_count=len(open_positions),
        open_contracts=tuple(sorted(open_contracts)),
        open_underlying_right=tuple(sorted(set(open_underlying_right))),
        daily_realized_pl=float(daily_realized),
        daily_unrealized_pl=float(daily_unrealized),
        daily_total_pl=float(daily_total),
        daily_equity=daily_equity,
        kill_switch_on=kill_switch_on,
        kill_switch_reasons=tuple(kill_reasons),
        block_new_entries=block_new_entries,
        block_reasons=tuple(block_reasons),
        block_reason=block_reason,
        data_stale=bool(stale_reasons),
        data_stale_reasons=tuple(stale_reasons),
        stop_loss_exits_today=int(daily_stop_losses),
        positions=tuple(open_positions),
    )
    _refresh_execution_manager(execution_manager, snapshot)
    return snapshot


def record_option_entry(
    symbol: str,
    *,
    user_id: str = "default",
    data_dir: Path | None = None,
    entry_reason: str | None = None,
    intended_limit_price: float | None = None,
    entry_fill_price: float | None = None,
    quantity: int | None = None,
    contracts: int | None = None,
    premium_paid: float | None = None,
    quote_spread_pct: float | None = None,
    delta: float | None = None,
    iv: float | None = None,
    order_id: str | None = None,
    order_status: str | None = None,
    now: datetime | None = None,
) -> None:
    """
    Persist an option entry marker so later analytics can measure entry slippage and reasons.
    """
    now_dt = now or datetime.now(timezone.utc)
    if getattr(now_dt, "tzinfo", None) is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    state = _load_state(user_id, data_dir=data_dir)
    positions_map = state.setdefault("positions", {})
    if not isinstance(positions_map, dict):
        positions_map = {}
        state["positions"] = positions_map
    key = _option_position_key(symbol)
    rec = dict(positions_map.get(key) or {})
    rec["symbol"] = key
    underlying, right, strike = _underlying_right(key)
    if underlying is not None:
        rec["underlying"] = underlying
    if right is not None:
        rec["right"] = right
    if strike is not None:
        rec["strike"] = strike
    rec["status"] = "open"
    rec.setdefault("entry_time", _utc_now_iso(now_dt))
    if entry_reason is not None:
        rec["entry_reason"] = str(entry_reason)
    if intended_limit_price is not None:
        rec["intended_limit_price"] = float(intended_limit_price)
    if entry_fill_price is not None:
        rec["entry_fill_price"] = float(entry_fill_price)
    if premium_paid is not None:
        rec["premium_paid"] = float(premium_paid)
    if quantity is not None:
        rec["qty"] = int(quantity)
    if contracts is not None:
        rec["contracts"] = int(contracts)
    if quote_spread_pct is not None:
        rec["entry_quote_spread_pct"] = float(quote_spread_pct)
    if delta is not None:
        rec["delta"] = float(delta)
    if iv is not None:
        rec["iv"] = float(iv)
    if order_id is not None:
        rec["entry_order_id"] = str(order_id)
    if order_status is not None:
        rec["entry_order_status"] = str(order_status)
    rec["entry_slippage_bps"] = _slippage_bps(
        entry_fill_price,
        intended_limit_price,
        side="buy",
    )
    if rec.get("entry_fill_price") is None and rec.get("intended_limit_price") is not None:
        rec["entry_fill_price"] = float(rec.get("intended_limit_price") or 0.0)
    positions_map[key] = rec
    day_row = _daily_state(state, now_dt)
    day_row["contracts_opened"] = int(day_row.get("contracts_opened") or 0) + int(
        contracts or quantity or 0
    )
    day_row["last_updated_at"] = _utc_now_iso(now_dt)
    state["meta"] = {
        "updated_at": _utc_now_iso(now_dt),
        "user_id": user_id,
    }
    _save_state(state, user_id, data_dir=data_dir)


def entry_blocked_reason(
    execution_manager: Any | None,
    *,
    underlying: str | None,
    direction: str | None,
) -> tuple[bool, list[str], str | None]:
    """
    Read the live snapshot flags that were applied to the execution manager by
    :func:`sync_options_positions`.
    """
    reasons: list[str] = []
    if execution_manager is None:
        return False, reasons, None
    if bool(getattr(execution_manager, "options_entry_blocked", False)):
        raw = getattr(execution_manager, "options_entry_block_reasons", None) or []
        reasons.extend(str(r) for r in raw if str(r).strip())
    open_underlying_right = set(getattr(execution_manager, "options_open_underlying_right", set()) or set())
    u = str(underlying or "").strip().upper()
    d = str(direction or "").strip().lower()
    right = "call" if d == "bullish" else "put" if d == "bearish" else None
    if u and right and f"{u}|{right}" in open_underlying_right:
        reasons.append(f"duplicate open contract for {u}|{right}")
    block = bool(reasons)
    return block, reasons, ("; ".join(reasons) if reasons else None)


def record_option_exit(
    symbol: str,
    *,
    user_id: str = "default",
    data_dir: Path | None = None,
    exit_reason: str,
    exit_price: float,
    realized_pl: float | None = None,
    now: datetime | None = None,
) -> None:
    """
    Persist a closed option record and append it to history.
    """
    key = _option_position_key(symbol)
    log.info(
        "OPTIONS_EXIT_SUBMITTED symbol=%s reason=%s",
        key,
        str(exit_reason or ""),
    )
    now_dt = now or datetime.now(timezone.utc)
    if getattr(now_dt, "tzinfo", None) is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    state = _load_state(user_id, data_dir=data_dir)
    positions_map = state.setdefault("positions", {})
    if not isinstance(positions_map, dict):
        positions_map = {}
        state["positions"] = positions_map
    rec = dict(positions_map.get(key) or {})
    if not rec:
        rec = {"symbol": key}
    rec["symbol"] = key
    rec["status"] = "closed"
    rec["exit_reason"] = str(exit_reason or "")
    rec["exit_time"] = _utc_now_iso(now_dt)
    rec["exit_price"] = float(exit_price)
    if realized_pl is not None:
        rec["realized_pl"] = float(realized_pl)
    rec["current_value"] = 0.0
    rec["quote_stale"] = False
    _append_history_entry(state, rec)
    positions_map.pop(key, None)
    day_row = _daily_state(state, now_dt)
    if str(exit_reason or "").strip().lower() in {"option_stop_loss", "stop_loss"}:
        day_row["stop_loss_exits"] = int(day_row.get("stop_loss_exits") or 0) + 1
    day_row["last_updated_at"] = _utc_now_iso(now_dt)
    state["meta"] = {
        "updated_at": _utc_now_iso(now_dt),
        "user_id": user_id,
    }
    _save_state(state, user_id, data_dir=data_dir)
    log.info(
        "OPTIONS_EXIT_FILLED symbol=%s reason=%s exit_price=%.2f realized_pl=%.2f",
        key,
        exit_reason,
        float(exit_price),
        float(realized_pl or 0.0),
    )
    log.info(
        "OPTIONS_POSITION_CLOSED symbol=%s reason=%s exit_price=%.2f realized_pl=%.2f",
        key,
        exit_reason,
        float(exit_price),
        float(realized_pl or 0.0),
    )
