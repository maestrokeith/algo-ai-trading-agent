"""Shadow-live options simulator.

This mode uses live market data and the live options selection path, but it never submits
orders to the broker. It records intended entries/exits, simulates realistic fills, and keeps
a separate shadow ledger for validation and reporting.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.brokers.alpaca_client import QuoteInfo
from src.entry_router import EntryRouteSignal
from src.entry_router import find_option_under_budget, holding_equity_long_for_underlying
from src.execution import ExecutionManager
from src.options_exit import evaluate_long_option_exit
from src.app.live_context import session_vwap_and_ema9
from src.options_selector import (
    OptionContractCandidate,
    SelectedOptionContract,
    parse_occ_equity_option_symbol,
)
from src.options_position_manager import is_option_position
from src.live.options_chain import log_options_disabled_non_paper, option_chain_for_underlying, options_runtime_enabled
from src.options_execution import prepare_option_order_premium_only_with_lower_strike_fallback
from src.options_config import options_conviction_entry_allowed

log = logging.getLogger(__name__)

_DEFAULT_DAILY_LOSS_PCT = 1.0
_DEFAULT_STOP_LOSS_COUNT = 2
_DEFAULT_MAX_STALE_SECONDS = 120.0


@dataclass(frozen=True)
class ShadowOptionEntryResult:
    intended: bool
    filled: bool
    reason: str | None
    reason_codes: tuple[str, ...]
    direction: str | None = None
    right: str | None = None
    symbol: str | None = None
    fill_price: float | None = None
    intended_limit_price: float | None = None
    slippage_bps: float | None = None


@dataclass(frozen=True)
class ShadowReportSummary:
    total_intents: int
    total_fills: int
    open_positions: int
    closed_positions: int
    win_rate: float
    realized_pl: float
    unrealized_pl: float
    total_pl: float
    avg_slippage_bps: float
    avg_hold_minutes: float
    best_symbol: str | None
    worst_symbol: str | None
    exit_reason_breakdown: dict[str, int]


def shadow_live_options_active(config: Mapping[str, Any] | None) -> bool:
    opts = (config or {}).get("options")
    if not isinstance(opts, Mapping):
        return False
    return bool(opts.get("enabled")) and str(opts.get("mode") or "").strip().lower() == "shadow_live"


def shadow_state_path(user_id: str = "default", *, data_dir: Path | None = None) -> Path:
    root = data_dir or (Path(__file__).resolve().parents[2] / "data")
    return root / f"options_shadow_{user_id}.json"


def _utc_now_iso(now: datetime | None = None) -> str:
    n = now or datetime.now(timezone.utc)
    if getattr(n, "tzinfo", None) is None:
        n = n.replace(tzinfo=timezone.utc)
    return n.astimezone(timezone.utc).isoformat()


def _trade_date_key(now: datetime | None = None) -> str:
    n = now or datetime.now(timezone.utc)
    if getattr(n, "tzinfo", None) is None:
        n = n.replace(tzinfo=timezone.utc)
    return n.astimezone(timezone.utc).date().isoformat()


def _load_state(user_id: str = "default", *, data_dir: Path | None = None) -> dict[str, Any]:
    path = shadow_state_path(user_id, data_dir=data_dir)
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


def _save_state(state: Mapping[str, Any], user_id: str = "default", *, data_dir: Path | None = None) -> None:
    path = shadow_state_path(user_id, data_dir=data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=2, default=str)


def _append_history(state: dict[str, Any], record: Mapping[str, Any]) -> None:
    hist = state.setdefault("history", [])
    if not isinstance(hist, list):
        hist = []
        state["history"] = hist
    hist.append(dict(record))


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


def _slippage_bps(fill_price: float | None, intended_price: float | None, *, side: str) -> float | None:
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


def _daily_row(state: dict[str, Any], now: datetime) -> dict[str, Any]:
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
            "block_new_entries": False,
            "block_reasons": [],
            "last_updated_at": _utc_now_iso(now),
        },
    )
    return row


def _today_history(state: Mapping[str, Any], now: datetime) -> list[dict[str, Any]]:
    hist = state.get("history") or []
    if not isinstance(hist, list):
        return []
    today = _trade_date_key(now)
    out: list[dict[str, Any]] = []
    for row in hist:
        if not isinstance(row, dict):
            continue
        ts = str(row.get("ts") or row.get("exit_time") or row.get("entry_time") or "")
        if ts.startswith(today):
            out.append(row)
    return out


def _count_stop_losses(history: Sequence[Mapping[str, Any]]) -> int:
    c = 0
    for row in history:
        reason = str(row.get("exit_reason") or row.get("reason") or "").strip().lower()
        if reason in {"option_stop_loss", "stop_loss"}:
            c += 1
    return c


def _quote_info_from_market_value(position: Mapping[str, Any], quote: QuoteInfo | None) -> QuoteInfo | None:
    if quote is not None:
        return quote
    mv = _as_float(position.get("market_value"), 0.0) or 0.0
    qty = abs(_as_int(position.get("qty"), 0) or 0)
    if mv > 0 and qty > 0:
        mid = mv / (qty * 100.0)
        return QuoteInfo(
            bid=max(mid * 0.995, 1e-4),
            ask=max(mid * 1.005, 1e-4),
            mid=mid,
            spread_pct=1.0,
            timestamp=None,
            last=None,
            skip_spread_check=False,
        )
    return None


def _shadow_position_record(
    *,
    symbol: str,
    qty: int,
    entry_fill_price: float,
    intended_limit_price: float,
    bid: float | None,
    ask: float | None,
    mid: float | None,
    quote_spread_pct: float | None,
    delta: float | None,
    iv: float | None,
    entry_reason: str,
    source: str,
    now: datetime,
) -> dict[str, Any]:
    parsed = parse_occ_equity_option_symbol(symbol)
    underlying = parsed[0] if parsed else None
    right = parsed[2] if parsed else None
    strike = parsed[3] if parsed else None
    premium_paid = float(entry_fill_price) * float(qty) * 100.0
    current_value = premium_paid
    return {
        "symbol": symbol,
        "underlying": underlying,
        "right": right,
        "strike": strike,
        "status": "open",
        "qty": int(qty),
        "contracts": int(qty),
        "entry_time": _utc_now_iso(now),
        "entry_reason": entry_reason,
        "source": source,
        "intended_limit_price": float(intended_limit_price),
        "entry_fill_price": float(entry_fill_price),
        "premium_paid": float(premium_paid),
        "cost_basis": float(-premium_paid),
        "current_price": float(entry_fill_price),
        "current_value": float(current_value),
        "unrealized_pl": 0.0,
        "realized_pl": 0.0,
        "dte": None,
        "delta": delta,
        "iv": iv,
        "quote_bid": bid,
        "quote_ask": ask,
        "quote_mid": mid,
        "quote_spread_pct": quote_spread_pct,
        "quote_age_seconds": None,
        "quote_stale": False,
        "last_seen_at": _utc_now_iso(now),
        "open_paper": True,
    }


def _entry_block_reason(
    config: Mapping[str, Any],
    *,
    state: Mapping[str, Any],
    underlying: str,
    direction: str,
    now: datetime,
    account_equity: float,
) -> tuple[bool, list[str], str | None]:
    reasons: list[str] = []
    opts = (config.get("options") or {}) if isinstance(config, Mapping) else {}
    max_open_raw = opts.get("max_option_positions", 3)
    try:
        max_open = max(1, int(float(max_open_raw)))
    except (TypeError, ValueError):
        max_open = 3
    open_positions = state.get("positions") or {}
    if isinstance(open_positions, dict) and len([v for v in open_positions.values() if str((v or {}).get("status") or "").lower() == "open"]) >= max_open:
        reasons.append(f"max open option positions ({max_open})")

    open_underlying_right: set[str] = set()
    if isinstance(open_positions, dict):
        for rec in open_positions.values():
            if not isinstance(rec, dict):
                continue
            if str(rec.get("status") or "").lower() != "open":
                continue
            u = str(rec.get("underlying") or "").strip().upper()
            r = str(rec.get("right") or "").strip().lower()
            if u and r:
                open_underlying_right.add(f"{u}|{r}")
    right = "call" if str(direction).lower() == "bullish" else "put" if str(direction).lower() == "bearish" else None
    if underlying and right and f"{underlying}|{right}" in open_underlying_right:
        reasons.append(f"duplicate open contract for {underlying}|{right}")

    daily_row = _daily_row(dict(state), now)
    daily_realized = _as_float(daily_row.get("realized_pl"), 0.0) or 0.0
    daily_unrealized = _as_float(daily_row.get("unrealized_pl"), 0.0) or 0.0
    if account_equity > 0:
        limit_pct_raw = opts.get("max_daily_loss_pct", _DEFAULT_DAILY_LOSS_PCT)
        try:
            limit_pct = abs(float(limit_pct_raw))
        except (TypeError, ValueError):
            limit_pct = _DEFAULT_DAILY_LOSS_PCT
        total = daily_realized + daily_unrealized
        if total <= -account_equity * (limit_pct / 100.0) + 1e-9:
            reasons.append(
                "daily options P&L %.2f <= -%.2f%% of equity" % (total, limit_pct)
            )

    today_hist = _today_history(state, now)
    stop_losses = _count_stop_losses(today_hist)
    stop_limit_raw = (opts.get("exits") or {}).get("stop_loss_exit_limit_per_day", _DEFAULT_STOP_LOSS_COUNT)
    try:
        stop_limit = max(1, int(float(stop_limit_raw)))
    except (TypeError, ValueError):
        stop_limit = _DEFAULT_STOP_LOSS_COUNT
    if stop_losses >= stop_limit:
        reasons.append(f"daily stop-loss exits {stop_losses} >= {stop_limit}")

    stale_reasons: list[str] = []
    for rec in open_positions.values() if isinstance(open_positions, dict) else []:
        if not isinstance(rec, dict) or str(rec.get("status") or "").lower() != "open":
            continue
        if bool(rec.get("quote_stale")):
            stale_reasons.append(str(rec.get("symbol") or "unknown") + ":stale_quote")
    if stale_reasons:
        reasons.append("option data stale/missing: " + ", ".join(stale_reasons[:6]))

    return bool(reasons), reasons, "; ".join(reasons) if reasons else None


def _log_intended_order(
    *,
    symbol: str,
    side: str,
    intended_limit_price: float | None,
    bid: float | None,
    ask: float | None,
    mid: float | None,
    expected_premium: float | None,
    risk_result: str,
    entry_reason: str | None,
    skipped_reason: str | None,
    filled: bool | None = None,
    fill_price: float | None = None,
    slippage_bps: float | None = None,
) -> None:
    log.info(
        "OPTIONS_SHADOW_ORDER_INTENDED symbol=%s side=%s intended_limit_price=%s current_bid=%s current_ask=%s current_mid=%s expected_premium=%s risk_check=%s entry_reason=%s skipped_reason=%s",
        symbol,
        side,
        "n/a" if intended_limit_price is None else f"{float(intended_limit_price):.4f}",
        "n/a" if bid is None else f"{float(bid):.4f}",
        "n/a" if ask is None else f"{float(ask):.4f}",
        "n/a" if mid is None else f"{float(mid):.4f}",
        "n/a" if expected_premium is None else f"{float(expected_premium):.2f}",
        risk_result,
        entry_reason or "n/a",
        skipped_reason or "n/a",
    )
    if filled is not None:
        log.info(
            "OPTIONS_SHADOW_ORDER_RESULT symbol=%s side=%s filled=%s fill_price=%s realistic_slippage_bps=%s",
            symbol,
            side,
            str(bool(filled)).lower(),
            "n/a" if fill_price is None else f"{float(fill_price):.4f}",
            "n/a" if slippage_bps is None else f"{float(slippage_bps):.2f}",
        )


def _simulate_limit_fill(side: str, limit_price: float | None, bid: float | None, ask: float | None) -> tuple[bool, float | None, float | None]:
    if limit_price is None:
        return False, None, None
    try:
        limit = float(limit_price)
    except (TypeError, ValueError):
        return False, None, None
    if side == "sell":
        if bid is None:
            return False, None, None
        try:
            b = float(bid)
        except (TypeError, ValueError):
            return False, None, None
        if b >= limit:
            return True, b, _slippage_bps(b, limit, side="sell")
        return False, None, None
    if ask is None:
        return False, None, None
    try:
        a = float(ask)
    except (TypeError, ValueError):
        return False, None, None
    if a <= limit:
        return True, a, _slippage_bps(a, limit, side="buy")
    return False, None, None


def attempt_shadow_option_entry(
    config: Mapping[str, Any],
    *,
    broker: Any,
    execution_manager: ExecutionManager,
    symbol: str,
    dt: datetime,
    current_price: float,
    session_vwap: float | None,
    account_equity: float,
    positions: list[dict[str, Any]] | None,
    source: str,
    conviction_score: float | None = None,
    news_score: float | None = None,
    event_score: float | None = None,
    relative_volume: float | None = None,
    tracked: Mapping[str, Any] | None = None,
    chain_candidates: Sequence[Any] | None = None,
    user_id: str = "default",
    data_dir: Path | None = None,
) -> ShadowOptionEntryResult:
    if not options_runtime_enabled(broker, dict(config or {})):
        log_options_disabled_non_paper()
        return ShadowOptionEntryResult(False, False, "options disabled outside paper mode", ("non_paper_mode",), None, None)
    if not shadow_live_options_active(config):
        return ShadowOptionEntryResult(False, False, "options.mode is not shadow_live", ("mode_off",), None, None)
    if session_vwap is None or session_vwap <= 0:
        return ShadowOptionEntryResult(False, False, "session VWAP unavailable", ("no_vwap",), None, None)

    direction = "bullish" if float(current_price) >= float(session_vwap) else "bearish"
    right = "call" if direction == "bullish" else "put"
    reason_codes = ["shadow_live", "live_data", right]
    boosted = max([float(v) for v in (conviction_score, news_score, event_score) if v is not None], default=0.0)
    if boosted > 0:
        reason_codes.append("boosted")
    else:
        reason_codes.append("no_boost")

    sig = EntryRouteSignal(
        underlying=str(symbol or "").strip().upper(),
        direction=direction,
        source=str(source or "dynamic"),
        stock_symbol=str(symbol or "").strip().upper(),
        conviction_score=boosted if boosted > 0 else conviction_score,
            news_score=news_score,
            event_score=event_score,
            relative_volume=relative_volume,
    )
    state = _load_state(user_id, data_dir=data_dir)
    opts = (config.get("options") or {}) if isinstance(config, Mapping) else {}
    allowed = {str(x).upper() for x in (opts.get("allowed_underlyings") or [])}
    u = str(sig.underlying or "").strip().upper()
    if not allowed or u not in allowed:
        reason = "underlying not allowed for options"
        _log_intended_order(
            symbol=u,
            side="buy",
            intended_limit_price=None,
            bid=None,
            ask=None,
            mid=None,
            expected_premium=None,
            risk_result="blocked",
            entry_reason=f"source={sig.source}; direction={sig.direction}",
            skipped_reason=reason,
        )
        return ShadowOptionEntryResult(False, False, reason, tuple(reason_codes + ["underlying_not_allowed"]), direction, right, u)

    ok_cv, cv_reason = options_conviction_entry_allowed(dict(config), sig)
    if not ok_cv:
        reason = cv_reason or "options conviction_required not met"
        _log_intended_order(
            symbol=u,
            side="buy",
            intended_limit_price=None,
            bid=None,
            ask=None,
            mid=None,
            expected_premium=None,
            risk_result="blocked",
            entry_reason=f"source={sig.source}; direction={sig.direction}",
            skipped_reason=reason,
        )
        return ShadowOptionEntryResult(False, False, reason, tuple(reason_codes + ["conviction_block"]), direction, right, u)

    blocked, blocked_reasons, blocked_reason = _entry_block_reason(
        dict(config),
        state=state,
        underlying=u,
        direction=sig.direction,
        now=dt,
        account_equity=float(account_equity),
    )
    if blocked:
        reason = blocked_reason or "options kill switch / duplicate contract"
        _log_intended_order(
            symbol=u,
            side="buy",
            intended_limit_price=None,
            bid=None,
            ask=None,
            mid=None,
            expected_premium=None,
            risk_result="blocked",
            entry_reason=f"source={sig.source}; direction={sig.direction}",
            skipped_reason=reason,
        )
        return ShadowOptionEntryResult(False, False, reason, tuple(reason_codes + blocked_reasons), direction, right, u)

    if holding_equity_long_for_underlying(u, positions, tracked):
        reason = "holding equity; skip option overlay"
        _log_intended_order(
            symbol=u,
            side="buy",
            intended_limit_price=None,
            bid=None,
            ask=None,
            mid=None,
            expected_premium=None,
            risk_result="blocked",
            entry_reason=f"source={sig.source}; direction={sig.direction}",
            skipped_reason=reason,
        )
        return ShadowOptionEntryResult(False, False, reason, tuple(reason_codes + ["holding_equity"]), direction, right, u)

    cands = list(chain_candidates) if chain_candidates is not None else option_chain_for_underlying(broker, dict(config or {}), u, dt)
    if not cands:
        reason = "no option chain rows"
        _log_intended_order(
            symbol=u,
            side="buy",
            intended_limit_price=None,
            bid=None,
            ask=None,
            mid=None,
            expected_premium=None,
            risk_result="blocked",
            entry_reason=f"source={sig.source}; direction={sig.direction}",
            skipped_reason=reason,
        )
        return ShadowOptionEntryResult(False, False, reason, tuple(reason_codes + ["no_chain"]), direction, right, u)

    selected, sel_err = find_option_under_budget(
        dict(config or {}),
        sig,
        chain_candidates=cands,
        underlying_spot=current_price,
        equity=float(account_equity),
        positions=positions,
        as_of=dt.date(),
        tracked=tracked,
    )
    if selected is None:
        reason = sel_err or "(no ranked candidate matched budget/spread/liquidity)"
        _log_intended_order(
            symbol=u,
            side="buy",
            intended_limit_price=None,
            bid=None,
            ask=None,
            mid=None,
            expected_premium=None,
            risk_result="blocked",
            entry_reason=f"source={sig.source}; direction={sig.direction}",
            skipped_reason=reason,
        )
        return ShadowOptionEntryResult(False, False, reason, tuple(reason_codes + ["no_selected"]), direction, right, u)

    prepared, contract_for_order, prep_err = prepare_option_order_premium_only_with_lower_strike_fallback(
        dict(config or {}),
        equity=float(account_equity),
        positions=positions,
        chain_candidates=cands,
        selected_atm=selected,
        intent_underlying=u,
        intent_right=right,
        as_of=dt.date(),
    )
    if prepared is None:
        reason = prep_err or "(prepare_option_order_premium_only_with_lower_strike_fallback returned None)"
        _log_intended_order(
            symbol=u,
            side="buy",
            intended_limit_price=None,
            bid=None,
            ask=None,
            mid=None,
            expected_premium=None,
            risk_result="blocked",
            entry_reason=f"source={sig.source}; direction={sig.direction}",
            skipped_reason=reason,
        )
        return ShadowOptionEntryResult(False, False, reason, tuple(reason_codes + ["no_prepare"]), direction, right, u)

    chosen = contract_for_order or selected
    req = execution_manager.build_order_for_entry(
        prepared.occ_symbol,
        "buy",
        prepared.contracts,
        prepared.mid,
        prepared.spread_pct,
        bid=prepared.bid,
        ask=prepared.ask,
    )
    if req is None:
        reason = "execution could not build order (spread gate)"
        _log_intended_order(
            symbol=prepared.occ_symbol,
            side="buy",
            intended_limit_price=None,
            bid=prepared.bid,
            ask=prepared.ask,
            mid=prepared.mid,
            expected_premium=float(prepared.contracts) * float(prepared.mid) * 100.0,
            risk_result="blocked",
            entry_reason=f"source={sig.source}; direction={sig.direction}; conviction={sig.conviction_score if sig.conviction_score is not None else 'n/a'}; news={sig.news_score if sig.news_score is not None else 'n/a'}; event={sig.event_score if sig.event_score is not None else 'n/a'}; rel_vol={sig.relative_volume if sig.relative_volume is not None else 'n/a'}; catalyst={sig.catalyst_type or 'none'}",
            skipped_reason=reason,
        )
        return ShadowOptionEntryResult(False, False, reason, tuple(reason_codes + ["build_order_failed"]), direction, right, prepared.occ_symbol)

    intended_limit_price = float(getattr(req, "limit_price", None) or prepared.mid)
    expected_premium = float(prepared.contracts) * float(prepared.mid) * 100.0
    filled, fill_price, slippage_bps = _simulate_limit_fill("buy", intended_limit_price, prepared.bid, prepared.ask)
    entry_reason = (
        f"source={sig.source}; direction={sig.direction}; "
        f"conviction={sig.conviction_score if sig.conviction_score is not None else 'n/a'}; "
        f"news={sig.news_score if sig.news_score is not None else 'n/a'}; "
        f"event={sig.event_score if sig.event_score is not None else 'n/a'}; "
        f"rel_vol={sig.relative_volume if sig.relative_volume is not None else 'n/a'}; "
        f"catalyst={sig.catalyst_type or 'none'}"
    )
    _log_intended_order(
        symbol=prepared.occ_symbol,
        side="buy",
        intended_limit_price=intended_limit_price,
        bid=prepared.bid,
        ask=prepared.ask,
        mid=prepared.mid,
        expected_premium=expected_premium,
        risk_result="pass",
        entry_reason=entry_reason,
        skipped_reason=None if filled else "limit not marketable",
        filled=filled,
        fill_price=fill_price,
        slippage_bps=slippage_bps,
    )

    state = _load_state(user_id, data_dir=data_dir)
    pos_map = state.setdefault("positions", {})
    if not isinstance(pos_map, dict):
        pos_map = {}
        state["positions"] = pos_map
    if filled:
        rec = _shadow_position_record(
            symbol=prepared.occ_symbol,
            qty=int(prepared.contracts),
            entry_fill_price=float(fill_price or intended_limit_price),
            intended_limit_price=intended_limit_price,
            bid=prepared.bid,
            ask=prepared.ask,
            mid=prepared.mid,
            quote_spread_pct=prepared.spread_pct,
            delta=getattr(chosen, "delta", None),
            iv=getattr(chosen, "iv", None),
            entry_reason=entry_reason,
            source=str(source or "dynamic"),
            now=dt,
        )
        pos_map[prepared.occ_symbol] = rec
        _append_history(
            state,
            {
                "ts": _utc_now_iso(dt),
                "action": "entry",
                "symbol": prepared.occ_symbol,
                "side": "buy",
                "status": "filled",
                "reason": entry_reason,
                "intended_limit_price": intended_limit_price,
                "fill_price": float(fill_price or intended_limit_price),
                "slippage_bps": slippage_bps,
                "premium_paid": rec["premium_paid"],
                "expected_premium": expected_premium,
                "filled": True,
            },
        )
        state["meta"] = {"updated_at": _utc_now_iso(dt), "user_id": user_id, "mode": "shadow_live"}
        _save_state(state, user_id, data_dir=data_dir)
    else:
        _append_history(
            state,
            {
                "ts": _utc_now_iso(dt),
                "action": "entry",
                "symbol": prepared.occ_symbol,
                "side": "buy",
                "status": "unfilled",
                "reason": entry_reason,
                "intended_limit_price": intended_limit_price,
                "fill_price": None,
                "slippage_bps": None,
                "premium_paid": expected_premium,
                "expected_premium": expected_premium,
                "filled": False,
            },
        )
        state["meta"] = {"updated_at": _utc_now_iso(dt), "user_id": user_id, "mode": "shadow_live"}
        _save_state(state, user_id, data_dir=data_dir)

    return ShadowOptionEntryResult(
        True,
        filled,
        None if filled else "limit not marketable",
        tuple(reason_codes),
        direction,
        right,
        prepared.occ_symbol,
        fill_price=fill_price,
        intended_limit_price=intended_limit_price,
        slippage_bps=slippage_bps,
    )


def manage_shadow_option_positions(
    *,
    broker: Any,
    config: Mapping[str, Any],
    user_id: str,
    data_dir: Path | None,
    now: datetime,
    execution_manager: ExecutionManager,
) -> None:
    if not shadow_live_options_active(config):
        return
    if not options_runtime_enabled(broker, dict(config or {})):
        log_options_disabled_non_paper()
        return
    state = _load_state(user_id, data_dir=data_dir)
    positions = state.get("positions") or {}
    if not isinstance(positions, dict) or not positions:
        return
    any_change = False
    for symbol, pos in list(positions.items()):
        if not isinstance(pos, dict) or str(pos.get("status") or "").lower() != "open":
            continue
        if not is_option_position(pos):
            continue
        oq = None
        get_quote = getattr(broker, "get_option_latest_quote", None)
        if callable(get_quote):
            try:
                oq = get_quote(symbol)
            except Exception:
                oq = None
        oq = _quote_info_from_market_value(pos, oq)
        if oq is None:
            log.info("OPTIONS_SHADOW_EXIT_SIGNAL symbol=%s reason=no_quote", symbol)
            continue
        entry_px = _as_float(pos.get("entry_fill_price") or pos.get("entry_price"), 0.0) or 0.0
        cost_basis = _as_float(pos.get("cost_basis"), 0.0)
        if cost_basis is None or abs(cost_basis) < 1e-8:
            premium_paid = _as_float(pos.get("premium_paid"), 0.0) or 0.0
            cost_basis = -premium_paid if premium_paid else 0.0
        current_px = float(oq.mid) if oq is not None and float(oq.mid) > 0 else (_as_float(pos.get("current_price"), 0.0) or 0.0)
        current_value = float(current_px) * float(abs(_as_int(pos.get("qty"), 0) or 0)) * 100.0
        unrealized_pl = current_value + float(cost_basis) if float(cost_basis) < 0 else current_value - float(cost_basis)
        pos_for_exit = dict(pos)
        pos_for_exit.update(
            {
                "current_price": current_px,
                "current_value": current_value,
                "unrealized_pl": unrealized_pl,
            }
        )
        und = str(pos.get("underlying") or "").strip().upper()
        u_close = None
        u_ma = None
        if und:
            try:
                u_ma, _ema9 = session_vwap_and_ema9(broker, und, now)
                get_uq = getattr(broker, "get_latest_quote", None)
                if callable(get_uq):
                    uq = get_uq(und)
                    if uq is not None and getattr(uq, "mid", None):
                        u_close = float(uq.mid)
            except Exception:
                pass
        dec = evaluate_long_option_exit(
            dict(config),
            occ_symbol=symbol,
            days_held=max(0, (now.date() - datetime.fromisoformat(str(pos.get("entry_time") or now.isoformat())).date()).days),
            unrealized_pl=unrealized_pl,
            cost_basis=float(cost_basis or 0.0),
            underlying_close=u_close,
            underlying_ma=u_ma,
            market_value=current_value,
            open_contracts=int(pos.get("qty") or 0),
            tracker_position=pos,
        )
        if not dec.should_exit or dec.reason is None:
            log.info(
                "OPTIONS_SHADOW_POSITION_UPDATE symbol=%s current_value=%.2f unrealized_pl=%.2f pnl_pct=%s",
                symbol,
                float(current_value),
                float(unrealized_pl),
                "n/a" if dec.pnl_pct is None else f"{float(dec.pnl_pct):.2f}",
            )
            continue
        sell_qty = int(dec.contracts_to_sell or int(pos.get("qty") or 0))
        sell_qty = max(1, min(sell_qty, int(pos.get("qty") or 0)))
        req = execution_manager.build_order(
            symbol,
            "sell",
            sell_qty,
            float(oq.mid),
            float(oq.spread_pct),
            tick_size=0.01,
            bid=float(oq.bid),
            ask=float(oq.ask),
            position_qty=int(pos.get("qty") or 0),
        )
        intended_limit_price = float(getattr(req, "limit_price", None) or oq.mid)
        filled, fill_price, slippage_bps = _simulate_limit_fill("sell", intended_limit_price, oq.bid, oq.ask)
        log.info(
            "OPTIONS_SHADOW_EXIT_SIGNAL symbol=%s reason=%s message=%s intended_limit_price=%s bid=%s ask=%s mid=%s",
            symbol,
            dec.reason.value,
            dec.message,
            f"{intended_limit_price:.4f}",
            f"{float(oq.bid):.4f}",
            f"{float(oq.ask):.4f}",
            f"{float(oq.mid):.4f}",
        )
        if not filled:
            log.info("OPTIONS_SHADOW_EXIT_FILLED symbol=%s filled=false reason=limit_not_marketable", symbol)
            continue
        realized_pl = (float(fill_price) - float(entry_px)) * float(sell_qty) * 100.0
        pos["status"] = "closed"
        pos["exit_reason"] = dec.reason.value
        pos["exit_time"] = _utc_now_iso(now)
        pos["exit_price"] = float(fill_price)
        pos["realized_pl"] = float(realized_pl)
        pos["current_value"] = 0.0
        pos["unrealized_pl"] = 0.0
        pos["exit_intended_limit_price"] = intended_limit_price
        pos["exit_fill_price"] = float(fill_price)
        pos["exit_slippage_bps"] = slippage_bps
        _append_history(
            state,
            {
                "ts": _utc_now_iso(now),
                "action": "exit",
                "symbol": symbol,
                "side": "sell",
                "status": "filled",
                "reason": dec.reason.value,
                "message": dec.message,
                "intended_limit_price": intended_limit_price,
                "fill_price": float(fill_price),
                "slippage_bps": slippage_bps,
                "realized_pl": float(realized_pl),
                "filled": True,
            },
        )
        positions[symbol] = pos
        any_change = True
        log.info(
            "OPTIONS_SHADOW_EXIT_FILLED symbol=%s exit_reason=%s fill_price=%.4f realized_pl=%.2f",
            symbol,
            dec.reason.value,
            float(fill_price),
            float(realized_pl),
        )
    if any_change:
        state["meta"] = {"updated_at": _utc_now_iso(now), "user_id": user_id, "mode": "shadow_live"}
        _save_state(state, user_id, data_dir=data_dir)


def summarize_shadow_report(*, user_id: str, data_dir: Path | None = None, now: datetime | None = None) -> ShadowReportSummary:
    n = now or datetime.now(timezone.utc)
    if getattr(n, "tzinfo", None) is None:
        n = n.replace(tzinfo=timezone.utc)
    state = _load_state(user_id, data_dir=data_dir)
    hist = state.get("history") or []
    if not isinstance(hist, list):
        hist = []
    positions = state.get("positions") or {}
    if not isinstance(positions, dict):
        positions = {}
    intents = [row for row in hist if isinstance(row, dict) and str(row.get("action") or "").lower() == "entry"]
    fills = [row for row in intents if bool(row.get("filled"))]
    closed = [row for row in hist if isinstance(row, dict) and str(row.get("action") or "").lower() == "exit" and bool(row.get("filled"))]
    open_positions = [row for row in positions.values() if isinstance(row, dict) and str(row.get("status") or "").lower() == "open"]
    realized = sum(float(row.get("realized_pl") or 0.0) for row in closed)
    unrealized = 0.0
    for row in open_positions:
        unrealized += float(row.get("unrealized_pl") or 0.0)
    total = realized + unrealized
    slippages = [float(row.get("slippage_bps")) for row in hist if isinstance(row, dict) and row.get("slippage_bps") is not None]
    avg_slip = sum(slippages) / len(slippages) if slippages else 0.0
    hold_mins: list[float] = []
    symbol_pnl: dict[str, float] = {}
    exit_breakdown: Counter[str] = Counter()
    for row in closed:
        reason = str(row.get("reason") or row.get("exit_reason") or "unknown")
        exit_breakdown[reason] += 1
        symbol = str(row.get("symbol") or "").upper()
        symbol_pnl[symbol] = symbol_pnl.get(symbol, 0.0) + float(row.get("realized_pl") or 0.0)
        ts = str(row.get("ts") or "")
        et = str(row.get("entry_time") or "")
        if ts and et:
            try:
                start = datetime.fromisoformat(et)
                end = datetime.fromisoformat(ts)
                hold_mins.append(max(0.0, (end - start).total_seconds() / 60.0))
            except Exception:
                pass
    wins = [row for row in closed if float(row.get("realized_pl") or 0.0) > 0]
    losses = [row for row in closed if float(row.get("realized_pl") or 0.0) < 0]
    win_rate = (len(wins) / len(closed) * 100.0) if closed else 0.0
    best_symbol = None
    worst_symbol = None
    if symbol_pnl:
        best_symbol = max(symbol_pnl.items(), key=lambda kv: kv[1])[0]
        worst_symbol = min(symbol_pnl.items(), key=lambda kv: kv[1])[0]
    return ShadowReportSummary(
        total_intents=len(intents),
        total_fills=len(fills),
        open_positions=len(open_positions),
        closed_positions=len(closed),
        win_rate=win_rate,
        realized_pl=realized,
        unrealized_pl=unrealized,
        total_pl=total,
        avg_slippage_bps=avg_slip,
        avg_hold_minutes=(sum(hold_mins) / len(hold_mins)) if hold_mins else 0.0,
        best_symbol=best_symbol,
        worst_symbol=worst_symbol,
        exit_reason_breakdown=dict(exit_breakdown),
    )
