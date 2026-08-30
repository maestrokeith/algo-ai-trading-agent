"""Stock exit surface for split live exit workflows."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from datetime import datetime, time as dt_time, timedelta
from types import SimpleNamespace
from typing import Any

from src.execution import execution_bypass_no_sell_after_buy_cooldown
from src.options_premium_risk import is_option_symbol
from src.position_state_machine import blocks_discretionary_stock_exit, exit_reason_is_stop_like
from src.position_tracker import (
    bars_held,
    load as load_tracked,
    minutes_held as holding_minutes,
    remove as remove_tracked,
    update as update_tracked,
)
from src.smart_exit import (
    bump_high_price,
    load_smart_exit_state_from_row,
    process_smart_exit,
    smart_exit_state_to_json,
    smart_trailing_cfg_for_process,
)
from src.app.live_context import session_vwap_and_ema9
from src.dynamic_universe import classify_symbol, manage_dynamic_exit
from src.dynamic_universe import mark_cooldown as mark_dynamic_cooldown
from src.dynamic_universe import load_state as load_dynamic_state
from src.dynamic_universe import dynamic_reentry_cooldown_remaining_minutes
from src.news_catalyst import get_cached_news_score
from src.strategy import ExitReason, _atr, compute_overweight_trim_shares
from src.risk_limits import (
    effective_symbol_allocation_cap_pct,
    risk_enforce_position_caps_on_hold,
    risk_rebalance_on_breach,
    risk_rebalance_threshold_pct,
    symbol_allocation_breach_trim_shares,
)
from src.sell_logging import sell_log_reason_for_engine_exit
from src.safe_sell import (
    available_sell_qty_shares,
    build_safe_sell_order_request,
    maybe_submit_dust_cleanup,
    submit_fractional_full_close,
)
from src.decision_priority import exit_reason_to_intent_kind
from src.live.session_clock import minutes_since_regular_session_open_et
from src.loop_helpers import alpaca_pdt_exit_hint_line, is_alpaca_pdt_trade_denial
from src.pdt_safety import entry_opened_same_calendar_day_et
from src.strategies.exits.context import LiveExitContext, reentry_block_allows_despite_flag
from src.strategies.exits.profit_protection import (
    do_not_sell_winners_early_blocks,
    equity_unrealized_pnl_percent_points,
    exit_trim_suppressed_trend_still_strong,
    live_profit_protection_decision,
    live_time_stop_not_green_decision,
)

log = logging.getLogger(__name__)

_DYNAMIC_KILL_SWITCH_STRONG_GAIN_PCT = 5.0
_DYNAMIC_KILL_SWITCH_STRONG_REL_VOLUME = 1.3
_DYNAMIC_KILL_SWITCH_TIGHT_SPREAD_PCT = 1.0


def _build_safe_exit_sell_order(
    ctx: LiveExitContext,
    symbol: str,
    target_qty: float | int,
    *,
    exec_mid: float,
    spr_order: float,
    quote: Any,
    positions: list[Mapping[str, Any]] | None = None,
) -> Any | None:
    return build_safe_sell_order_request(
        ctx.broker,
        ctx.engine.execution,
        symbol,
        target_qty,
        mid_price=float(exec_mid),
        spread_pct=float(spr_order),
        ignore_spread_gate=bool(getattr(quote, "skip_spread_check", False)),
        bid=float(getattr(quote, "bid")),
        ask=float(getattr(quote, "ask")),
        positions=positions,
    )


def _submit_full_exit_close(
    ctx: LiveExitContext,
    symbol: str,
    *,
    reason: str,
) -> Any | None:
    return submit_fractional_full_close(
        ctx.broker,
        symbol,
        reason=reason,
        prefer_close_position=True,
    )


def _full_exit_available_qty(ctx: LiveExitContext, symbol: str, fallback: float) -> float:
    try:
        _, _, available = available_sell_qty_shares(ctx.broker, symbol)
        if available > 0.0:
            return float(available)
    except Exception:
        pass
    return float(fallback)


def _dynamic_reentry_cooldown_minutes(ctx: LiveExitContext) -> int:
    du = (ctx.config or {}).get("dynamic_universe") or {}
    try:
        return max(0, int(du.get("reentry_cooldown_minutes", 60) or 60))
    except (TypeError, ValueError):
        return 60


def _dynamic_aggressive_cfg(ctx: LiveExitContext) -> Mapping[str, Any]:
    raw = (ctx.config or {}).get("dynamic_aggressive")
    return raw if isinstance(raw, Mapping) else {}


def _is_dynamic_aggressive_position(row: Mapping[str, Any] | None) -> bool:
    if not isinstance(row, Mapping):
        return False
    return any(
        str(row.get(key) or "").strip().lower() in {"dynamic_aggressive", "dynamic_aggressive_scalp"}
        for key in ("route", "source", "entry_route", "entry_source")
    )


def _dynamic_aggressive_exit_reason(
    ctx: LiveExitContext,
    row: Mapping[str, Any],
    *,
    price: float,
    entry_price: float,
    hold_minutes: float | None,
) -> str | None:
    if not _is_dynamic_aggressive_position(row):
        return None
    cfg = _dynamic_aggressive_cfg(ctx)
    try:
        stop_loss = float(cfg.get("stop_loss_pct", 3.0) or 3.0)
    except (TypeError, ValueError):
        stop_loss = 3.0
    try:
        take_profit = float(cfg.get("take_profit_pct", 4.0) or 4.0)
    except (TypeError, ValueError):
        take_profit = 4.0
    try:
        max_hold = float(cfg.get("max_hold_minutes", 20) or 20)
    except (TypeError, ValueError):
        max_hold = 20.0
    pnl_pct = ((float(price) - float(entry_price)) / float(entry_price) * 100.0) if entry_price > 0 else 0.0
    if pnl_pct <= -abs(stop_loss) + 1e-9:
        return "stop_loss"
    if pnl_pct >= abs(take_profit) - 1e-9:
        return "take_profit"
    if hold_minutes is not None and float(hold_minutes) >= max_hold - 1e-9:
        return "max_hold"
    return None


def _submit_dynamic_aggressive_exit(
    ctx: LiveExitContext,
    symbol: str,
    *,
    pos: Mapping[str, Any],
    broker_pos: Mapping[str, Any],
    qty: float,
    reason: str,
    exec_mid: float,
    entry_price: float,
) -> bool:
    if ctx.skip_exit_for_action_cap(symbol, "dynamic_aggressive_exit"):
        return False
    if ctx.same_day_close_blocked(symbol, pos):
        return False
    available_qty = _full_exit_available_qty(ctx, symbol, qty)
    order = _submit_full_exit_close(ctx, symbol, reason=reason)
    if not order:
        return False
    ctx.record_exit_action(symbol)
    ctx.note_daily_risk_order(symbol, side="sell", full_exit=True)
    ctx.log_sell_event(
        symbol,
        "dynamic_aggressive_exit",
        {
            "engine_reason": reason,
            "qty": available_qty,
            "exit_price": float(exec_mid),
        },
    )
    log.info(
        "DYNAMIC_AGGRESSIVE_EXIT symbol=%s reason=%s qty=%.9g",
        symbol,
        reason,
        float(available_qty),
    )
    print(ctx.now.strftime("%H:%M ET"), symbol, "SELL", available_qty, "shares — dynamic_aggressive_%s" % reason, flush=True)
    exit_reason = ExitReason.STOP_LOSS if reason == "stop_loss" else ExitReason.TAKE_PROFIT if reason == "take_profit" else ExitReason.TIME_BARS
    ctx.record_engine_after_sell(
        symbol,
        exit_reason,
        float(exec_mid),
        entry_price_for_stop=entry_price if entry_price > 0 else None,
        remaining_qty_after=0,
    )
    remove_tracked(symbol, user_id=ctx.user_id, data_dir=ctx.data_dir)
    ctx.notify_sqqq_tracker_removed(symbol)
    try:
        cooldown = int(float(_dynamic_aggressive_cfg(ctx).get("cooldown_minutes", 30) or 30))
    except (TypeError, ValueError):
        cooldown = 30
    if cooldown > 0:
        mark_dynamic_cooldown(symbol, cooldown, load_dynamic_state(), remove_active=True)
    return True


def _mark_dynamic_reentry_cooldown_if_needed(
    ctx: LiveExitContext,
    symbol: str,
    *,
    original_qty: int,
    remaining_qty: int,
) -> None:
    if not _is_runtime_dynamic_momentum_symbol(ctx, symbol):
        return
    cooldown = _dynamic_reentry_cooldown_minutes(ctx)
    if cooldown <= 0:
        return
    original_qty = max(0, int(original_qty))
    remaining_qty = max(0, int(remaining_qty))
    material_reduction = remaining_qty <= 0 or remaining_qty <= max(1, original_qty // 2)
    if not material_reduction:
        return
    state = load_dynamic_state()
    mark_dynamic_cooldown(
        str(symbol).strip().upper(),
        cooldown,
        state,
        remove_active=remaining_qty <= 0,
    )
    log.info(
        "DYNAMIC_REENTRY_COOLDOWN symbol=%s remaining_minutes=%d",
        str(symbol).strip().upper(),
        cooldown,
    )


def _minutes_until_market_close_et(now: datetime) -> float | None:
    tz = getattr(now, "tzinfo", None)
    if tz is None:
        return None
    close_dt = datetime(now.year, now.month, now.day, 16, 0, 0, tzinfo=tz)
    if now >= close_dt:
        return 0.0
    return (close_dt - now).total_seconds() / 60.0


def _finite_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _runtime_dynamic_symbols(ctx: LiveExitContext) -> set[str]:
    raw = getattr(ctx.engine, "dynamic_symbols", None)
    if raw is None and hasattr(ctx.engine, "execution"):
        raw = getattr(ctx.engine.execution, "dynamic_symbols", None)
    try:
        return {str(s).strip().upper() for s in raw or [] if str(s).strip()}
    except TypeError:
        return set()


def _is_runtime_dynamic_momentum_symbol(ctx: LiveExitContext, symbol: str) -> bool:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return False
    cls_map = getattr(ctx.engine, "symbol_classifications", None)
    if isinstance(cls_map, dict):
        cls = str(cls_map.get(sym) or "").strip().upper()
        if cls:
            return cls == "DYNAMIC_ONLY"
    alloc_holdings = getattr(ctx.engine, "allocator_holdings", None)
    if alloc_holdings is None and hasattr(ctx.engine, "execution"):
        alloc_holdings = getattr(ctx.engine.execution, "allocator_holdings", None)
    return classify_symbol(
        sym,
        ctx.symbols,
        allocator_holdings=alloc_holdings or [],
        dynamic_symbols=_runtime_dynamic_symbols(ctx),
    ) == "DYNAMIC_ONLY"


def _symbol_classification(ctx: LiveExitContext, symbol: str) -> str:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return "OTHER"
    cls_map = getattr(ctx.engine, "symbol_classifications", None)
    if isinstance(cls_map, dict):
        cached = str(cls_map.get(sym) or "").strip().upper()
        if cached:
            return cached
    alloc_holdings = getattr(ctx.engine, "allocator_holdings", None)
    if alloc_holdings is None and hasattr(ctx.engine, "execution"):
        alloc_holdings = getattr(ctx.engine.execution, "allocator_holdings", None)
    return classify_symbol(
        sym,
        ctx.symbols,
        allocator_holdings=alloc_holdings or [],
        dynamic_symbols=_runtime_dynamic_symbols(ctx),
    )


def _entry_eval_momentum_still_true(
    ctx: LiveExitContext,
    symbol: str,
    df_exit_mf: Any,
    spread_pct: float | None,
    atr_pct: float | None,
) -> bool:
    if df_exit_mf is None:
        return False
    meth = getattr(getattr(ctx.engine, "strategy", None), "entry_eval_components_for_log", None)
    if meth is None:
        return False
    try:
        _trend_ok, _pullback_ok, momentum_ok, _vol_ok = meth(
            symbol,
            df_exit_mf,
            spread_pct,
            atr_pct,
        )
    except TypeError:
        try:
            _trend_ok, _pullback_ok, momentum_ok, _vol_ok = meth(
                symbol,
                df_exit_mf,
                spread_pct=spread_pct,
                atr_pct_now=atr_pct,
            )
        except Exception:
            return False
    except Exception:
        return False
    return bool(momentum_ok)


def _latest_snapshot_shows_strong_dynamic_momentum(
    ctx: LiveExitContext,
    symbol: str,
    spread_pct: float | None,
) -> bool:
    get_snapshot = getattr(ctx.broker, "get_snapshot", None)
    if get_snapshot is None:
        return False
    try:
        snap = get_snapshot(symbol)
    except Exception:
        return False
    if not isinstance(snap, Mapping):
        return False
    gain = _finite_float(snap.get("day_gain_pct"))
    volume = _finite_float(snap.get("volume"))
    if gain is None or volume is None or volume <= 0:
        return False
    avg_volume = None
    get_avg_volume = getattr(ctx.broker, "get_avg_volume", None)
    if get_avg_volume is not None:
        try:
            avg_volume = _finite_float(get_avg_volume(symbol))
        except Exception:
            avg_volume = None
    rel_volume = None
    if avg_volume is not None and avg_volume > 0:
        rel_volume = volume / avg_volume
    else:
        rel_volume = _finite_float(snap.get("relative_volume"))
    spread = _finite_float(spread_pct)
    if spread is None:
        bid = _finite_float(snap.get("bid"))
        ask = _finite_float(snap.get("ask"))
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
            if mid > 0:
                spread = abs(ask - bid) / mid * 100.0
    return (
        gain >= _DYNAMIC_KILL_SWITCH_STRONG_GAIN_PCT
        and rel_volume is not None
        and rel_volume >= _DYNAMIC_KILL_SWITCH_STRONG_REL_VOLUME
        and spread is not None
        and spread <= _DYNAMIC_KILL_SWITCH_TIGHT_SPREAD_PCT
    )


def _should_suppress_dynamic_kill_switch_partial(
    ctx: LiveExitContext,
    symbol: str,
    df_exit_mf: Any,
    spread_pct: float | None,
    atr_pct: float | None,
) -> bool:
    if not _is_runtime_dynamic_momentum_symbol(ctx, symbol):
        return False
    if _entry_eval_momentum_still_true(ctx, symbol, df_exit_mf, spread_pct, atr_pct):
        return True
    return _latest_snapshot_shows_strong_dynamic_momentum(ctx, symbol, spread_pct)


def manage_stock_position(
    ctx: LiveExitContext,
    broker_pos: dict[str, Any],
) -> None:
    """Equity / legacy short: exit rules from tracker + ctx.broker row."""
    symbol = str(broker_pos.get("symbol") or "").strip().upper()
    if not symbol or is_option_symbol(symbol):
        return
    try:
        tracked_rows = load_tracked(ctx.user_id, data_dir=ctx.data_dir)
        pos = tracked_rows.get(symbol) if isinstance(tracked_rows, dict) else None
        try:
            tracked_qty = float((pos or {}).get("qty") or 0.0) if isinstance(pos, dict) else 0.0
        except (TypeError, ValueError):
            tracked_qty = 0.0
        protected_symbols = list(ctx.symbols or [])
        active_intent_symbols = [symbol] if pos is not None and tracked_qty > 0.0 else []
        if pos is None:
            maybe_submit_dust_cleanup(
                ctx.broker,
                symbol,
                market_value=broker_pos.get("market_value"),
                config=ctx.config,
                protected_symbols=protected_symbols,
                active_intent_symbols=active_intent_symbols,
            )
            return
        qty = int(pos.get("qty", 0))
        try:
            broker_qty = abs(float(broker_pos.get("qty") or 0))
        except (TypeError, ValueError):
            broker_qty = 0.0
        position_qty_live = max(float(qty), broker_qty)
        if qty <= 0 and position_qty_live <= 0.0:
            return
        if qty <= 0 and position_qty_live > 0.0:
            maybe_submit_dust_cleanup(
                ctx.broker,
                symbol,
                market_value=broker_pos.get("market_value"),
                config=ctx.config,
                protected_symbols=protected_symbols,
                active_intent_symbols=[],
            )
            return
        side = str(pos.get("side") or "long").strip().lower()
        quote = ctx.broker.get_latest_quote(symbol)
        if not quote:
            return
        entry_price = float(pos.get("entry_price", 0))
        if entry_price <= 0:
            return

        entry_time_iso = str(pos.get("entry_time", "") or "").strip()
        bars = bars_held(entry_time_iso, ctx.now) if entry_time_iso else 0
        hold_mins = holding_minutes(entry_time_iso, ctx.now) if entry_time_iso else None

        def live_trims_blocked_by_min_hold() -> bool:
            st = ctx.engine.strategy
            meth = getattr(st, "trim_deferred_for_min_hold", None)
            if meth is None:
                return False
            return bool(meth(minutes_held=hold_mins, bars_held=bars))

        def log_min_hold_debug(path: str, submit_qty: Any, reason: Any) -> None:
            st = ctx.engine.strategy
            try:
                min_hold = float(getattr(st, "min_hold_minutes", 0) or 0)
            except (TypeError, ValueError):
                min_hold = 0.0
            no_trim = bool(getattr(st, "no_trim_before_min_hold", False))
            try:
                blocked = live_trims_blocked_by_min_hold()
            except Exception:
                blocked = False
            hold_val = "None" if hold_mins is None else "%.1f" % float(hold_mins)
            log.info(
                "MIN_HOLD_DEBUG symbol=%s path=%s hold_mins=%s min_hold=%.1f "
                "no_trim_before_min_hold=%s blocked_by_min_hold=%s qty=%s reason=%s",
                symbol,
                path,
                hold_val,
                min_hold,
                no_trim,
                blocked,
                submit_qty,
                reason,
            )

        def disc_exit_blocked() -> bool:
            blocked, reason = blocks_discretionary_stock_exit(symbol, ctx.user_id, ctx.data_dir, ctx.now, ctx.config)
            if blocked and ctx.verbose and reason:
                print(ctx.now.strftime("%H:%M ET"), symbol, "exit skip —", reason, flush=True)
            return blocked

        px_fb = entry_price
        try:
            b1 = ctx.broker.get_bars(symbol, timeframe="1Day", limit=1)
            if b1 is not None and not b1.empty:
                px_fb = float(b1["close"].iloc[-1])
        except Exception:
            pass
        exec_mid = quote.reference_mid(px_fb)
        if side != "short" and entry_price > 0.0 and float(exec_mid) > 0.0:
            try:
                update_tracked(
                    symbol,
                    user_id=ctx.user_id,
                    data_dir=ctx.data_dir,
                    min_price_since_entry=float(exec_mid),
                    max_price_since_entry=float(exec_mid),
                )
            except Exception:
                log.debug("MFE_MAE_TRACK_UPDATE_FAILED symbol=%s", symbol, exc_info=True)
        spread_ex = None if quote.skip_spread_check else quote.spread_pct
        spr_order = float(spread_ex) if spread_ex is not None else 0.0
        reentry_sell_block, reentry_sell_why = ctx.reentry_block_discretionary_sells()
        pbc, pbc_why = ctx.post_buy_sell_cooldown_active(symbol, pos)

        def post_buy_sell_cooldown_blocks_sell(reason: Any) -> bool:
            if not pbc:
                return False
            if execution_bypass_no_sell_after_buy_cooldown(reason):
                return False
            if pbc_why and ctx.verbose:
                print(ctx.now.strftime("%H:%M ET"), symbol, "exit skip —", pbc_why, flush=True)
            return True

        suppress_trim_trend = side != "short" and exit_trim_suppressed_trend_still_strong(ctx, symbol)
        pnl_pp = equity_unrealized_pnl_percent_points(broker_pos, entry_price=entry_price, mid=float(exec_mid))
        block_dnw = side != "short" and do_not_sell_winners_early_blocks(ctx, symbol, pnl_pp)
        suppress_disc_trim = suppress_trim_trend or block_dnw

        if side != "short" and live_time_stop_not_green_decision(
            config=ctx.config,
            minutes_held=hold_mins,
            pnl_percent_points=pnl_pp,
        ):
            if not ctx.skip_exit_for_action_cap(symbol, "time_stop_not_green") and not ctx.same_day_close_blocked(symbol, pos):
                q_ts = _full_exit_available_qty(ctx, symbol, position_qty_live)
                sell_ts = _submit_full_exit_close(ctx, symbol, reason="time_stop_not_green")
                if sell_ts:
                    ctx.record_exit_action(symbol)
                    ctx.note_daily_risk_order(symbol, side="sell", full_exit=True)
                    ctx.log_sell_event(
                        symbol,
                        "signal_exit",
                        {"variant": "time_stop_not_green", "qty": q_ts},
                    )
                    print(ctx.now.strftime("%H:%M ET"), symbol, "SELL", q_ts, "shares — time_stop_not_green", flush=True)
                    ctx.record_engine_after_sell(
                        symbol,
                        ExitReason.TIME_BARS,
                        float(exec_mid),
                        entry_price_for_stop=entry_price if entry_price > 0 else None,
                        remaining_qty_after=0,
                    )
                    remove_tracked(symbol, user_id=ctx.user_id, data_dir=ctx.data_dir)
                    ctx.notify_sqqq_tracker_removed(symbol)
                    return

        if side != "short":
            _pp_decision = live_profit_protection_decision(
                config=ctx.config,
                position=pos,
                entry_price=float(entry_price),
                current_price=float(exec_mid),
                qty=float(position_qty_live),
            )
            _pp_state = _pp_decision.get("state") if isinstance(_pp_decision.get("state"), Mapping) else {}
            _pp_high = _finite_float(_pp_state.get("high_price")) if isinstance(_pp_state, Mapping) else None
            if _pp_high is not None and _pp_high > float(pos.get("trail_high") or 0.0) + 1e-9:
                update_tracked(
                    symbol,
                    user_id=ctx.user_id,
                    data_dir=ctx.data_dir,
                    trail_high=float(_pp_high),
                )
            if _pp_decision.get("action") == "full_exit":
                _pp_reason = str(_pp_decision.get("reason") or "profit_protection_exit")
                _pp_exit_reason = (
                    ExitReason.TRAILING_STOP
                    if "trailing" in _pp_reason
                    else ExitReason.STOP_LOSS
                    if "breakeven" in _pp_reason
                    else ExitReason.TAKE_PROFIT
                )
                if not ctx.skip_exit_for_action_cap(symbol, _pp_reason) and not ctx.same_day_close_blocked(symbol, pos):
                    q_pp = _full_exit_available_qty(ctx, symbol, position_qty_live)
                    sell_pp = _submit_full_exit_close(ctx, symbol, reason=_pp_reason)
                    if sell_pp:
                        ctx.record_exit_action(symbol)
                        ctx.note_daily_risk_order(symbol, side="sell", full_exit=True)
                        ctx.log_sell_event(
                            symbol,
                            "take_profit" if _pp_exit_reason != ExitReason.STOP_LOSS else "stop_loss",
                            {"variant": _pp_reason, "qty": q_pp},
                        )
                        print(ctx.now.strftime("%H:%M ET"), symbol, "SELL", q_pp, "shares —", _pp_reason, flush=True)
                        ctx.record_engine_after_sell(
                            symbol,
                            _pp_exit_reason,
                            float(exec_mid),
                            entry_price_for_stop=entry_price if entry_price > 0 else None,
                            remaining_qty_after=0,
                        )
                        remove_tracked(symbol, user_id=ctx.user_id, data_dir=ctx.data_dir)
                        ctx.notify_sqqq_tracker_removed(symbol)
                        return
            elif _pp_decision.get("action") == "partial_exit" and not suppress_disc_trim:
                _pp_qty = _finite_float(_pp_decision.get("qty")) or 0.0
                if _pp_qty > 0.0 and not ctx.skip_exit_for_action_cap(symbol, "profit_protection_partial") and not ctx.same_day_close_blocked(symbol, pos):
                    sell_pp = _build_safe_exit_sell_order(
                        ctx,
                        symbol,
                        _pp_qty,
                        exec_mid=float(exec_mid),
                        spr_order=float(spr_order),
                        quote=quote,
                        positions=[broker_pos],
                    )
                    if sell_pp:
                        ctx.broker.submit_order(sell_pp)
                        q_pp = float(sell_pp.quantity)
                        rem_pp = max(0.0, float(position_qty_live) - q_pp)
                        ctx.record_exit_action(symbol)
                        ctx.note_daily_risk_order(symbol, side="sell", full_exit=rem_pp <= 0.0)
                        ctx.note_decision_intent(symbol, "take_profit")
                        ctx.log_sell_event(
                            symbol,
                            "take_profit",
                            {"variant": "profit_protection_partial_take_profit", "qty": q_pp},
                        )
                        print(ctx.now.strftime("%H:%M ET"), symbol, "SELL", q_pp, "shares — profit_protection_partial_take_profit", flush=True)
                        ctx.record_engine_after_sell(
                            symbol,
                            ExitReason.PARTIAL_TAKE_PROFIT,
                            float(exec_mid),
                            entry_price_for_stop=entry_price if entry_price > 0 else None,
                            remaining_qty_after=rem_pp,
                        )
                        if rem_pp <= 0.0:
                            remove_tracked(symbol, user_id=ctx.user_id, data_dir=ctx.data_dir)
                            ctx.notify_sqqq_tracker_removed(symbol)
                        else:
                            update_tracked(
                                symbol,
                                user_id=ctx.user_id,
                                data_dir=ctx.data_dir,
                                qty=int(rem_pp),
                                partial_taken=True,
                                trail_high=float(exec_mid),
                            )
                        return

        if side != "short" and _is_dynamic_aggressive_position(pos):
            _aggr_reason = _dynamic_aggressive_exit_reason(
                ctx,
                pos,
                price=float(exec_mid),
                entry_price=float(entry_price),
                hold_minutes=hold_mins,
            )
            if _aggr_reason is not None:
                if _submit_dynamic_aggressive_exit(
                    ctx,
                    symbol,
                    pos=pos,
                    broker_pos=broker_pos,
                    qty=position_qty_live,
                    reason=_aggr_reason,
                    exec_mid=float(exec_mid),
                    entry_price=float(entry_price),
                ):
                    return

        if (
            side != "short"
            and bool((ctx.config.get("dynamic_exits") or {}).get("enabled", False))
        ):
            _uni_dy = ctx.config.get("universe") or {}
            _paused_dy = {str(p).upper() for p in (_uni_dy.get("paused_symbols") or [])}
            _core_dy = [
                str(x)
                for x in (_uni_dy.get("symbols") or [])
                if str(x).upper() not in _paused_dy
            ]
            if _symbol_classification(ctx, symbol) == "DYNAMIC_ONLY":
                _vwap_dy, _ = session_vwap_and_ema9(ctx.broker, symbol, ctx.now)
                _atr_dy: float | None = None
                try:
                    _df_atr_dy = ctx.broker.get_bars(symbol, timeframe="1Day", limit=20)
                    if _df_atr_dy is not None and not getattr(_df_atr_dy, "empty", True) and len(_df_atr_dy) >= 14:
                        _atr_series_dy = _atr(_df_atr_dy["high"], _df_atr_dy["low"], _df_atr_dy["close"], 14)
                        if len(_atr_series_dy) and _atr_series_dy.iloc[-1] == _atr_series_dy.iloc[-1]:
                            _atr_dy = float(_atr_series_dy.iloc[-1])
                except Exception:
                    _atr_dy = None

                def _dyn_submit(sym: str, q: int, tag: str) -> bool:
                    nonlocal qty
                    if q <= 0:
                        return False
                    if disc_exit_blocked():
                        return False
                    _reason_dyn = {
                        "dynamic_tp1": ExitReason.PARTIAL_TAKE_PROFIT,
                        "dynamic_tp2_full": ExitReason.TAKE_PROFIT,
                        "dynamic_trailing_stop": ExitReason.TRAILING_STOP,
                        "dynamic_vwap_break": ExitReason.SIGNAL_EXIT,
                    }.get(tag, ExitReason.SIGNAL_EXIT)
                    if post_buy_sell_cooldown_blocks_sell(_reason_dyn):
                        return False
                    if ctx.skip_exit_for_action_cap(sym, "dynamic_exit"):
                        return False
                    if ctx.same_day_close_blocked(sym, pos):
                        return False
                    ctx.note_decision_intent(sym, "take_profit")
                    full_dynamic_exit = tag in {"dynamic_tp2_full", "dynamic_trailing_stop", "dynamic_vwap_break"}
                    if full_dynamic_exit:
                        q_d = _full_exit_available_qty(ctx, sym, position_qty_live)
                        sell_d = _submit_full_exit_close(ctx, sym, reason=tag)
                    else:
                        sell_d = _build_safe_exit_sell_order(
                            ctx,
                            sym,
                            int(q),
                            exec_mid=float(exec_mid),
                            spr_order=float(spr_order),
                            quote=quote,
                            positions=[broker_pos],
                        )
                        q_d = float(sell_d.quantity) if sell_d else 0.0
                    if not sell_d:
                        return False
                    if not full_dynamic_exit:
                        ctx.broker.submit_order(sell_d)
                    ctx.record_exit_action(sym)
                    rem_d = 0.0 if full_dynamic_exit else float(qty) - q_d
                    ctx.note_daily_risk_order(sym, side="sell", full_exit=rem_d <= 0)
                    ctx.log_sell_event(sym, "dynamic_exit", {"tag": tag, "qty": q_d})
                    print(
                        ctx.now.strftime("%H:%M ET"),
                        sym,
                        "SELL",
                        q_d,
                        "shares —",
                        tag,
                        flush=True,
                    )
                    ctx.record_engine_after_sell(
                        sym,
                        _reason_dyn,
                        float(exec_mid),
                        entry_price_for_stop=entry_price if entry_price > 0 else None,
                        remaining_qty_after=rem_d,
                    )
                    _mark_dynamic_reentry_cooldown_if_needed(
                        ctx,
                        sym,
                        original_qty=int(qty),
                        remaining_qty=rem_d,
                    )
                    if rem_d <= 0:
                        remove_tracked(sym, user_id=ctx.user_id, data_dir=ctx.data_dir)
                        ctx.notify_sqqq_tracker_removed(sym)
                    else:
                        update_tracked(
                            sym,
                            user_id=ctx.user_id,
                            data_dir=ctx.data_dir,
                            qty=rem_d,
                            partial_taken=True,
                            trail_high=float(exec_mid),
                        )
                    qty = rem_d
                    return True

                if manage_dynamic_exit(
                    symbol,
                    broker_pos,
                    float(exec_mid),
                    _vwap_dy,
                    ctx.config,
                    _dyn_submit,
                    atr=_atr_dy,
                ):
                    return

                eod_cfg = (ctx.config.get("dynamic_universe") or {})
                try:
                    flatten_before = float(eod_cfg.get("minutes_before_close_to_flatten", 10) or 10)
                except (TypeError, ValueError):
                    flatten_before = 10.0
                flatten_before = max(0.0, flatten_before)
                close_intraday = bool(eod_cfg.get("close_intraday_positions_before_close", True))
                if close_intraday and flatten_before > 0:
                    mins_to_close = _minutes_until_market_close_et(ctx.now)
                    if (
                        mins_to_close is not None
                        and mins_to_close <= flatten_before
                        and entry_opened_same_calendar_day_et(str(pos.get("entry_time") or ""), ctx.now)
                    ):
                        allow_overnight_hold = bool(eod_cfg.get("allow_overnight_dynamic_hold", False))
                        news_score, news_reason = get_cached_news_score(
                            symbol,
                            now=ctx.now,
                            max_age_seconds=1800.0,
                        )
                        news_ai_cfg = (ctx.config.get("news_ai") or {})
                        try:
                            strong_news_score = int(news_ai_cfg.get("allow_overnight_if_score_gte", 8) or 8)
                        except (TypeError, ValueError):
                            strong_news_score = 8
                        if allow_overnight_hold or news_score >= strong_news_score:
                            pass
                        else:
                            if not ctx.same_day_close_blocked(symbol, pos):
                                if not ctx.skip_exit_for_action_cap(symbol, "dynamic_eod_flatten"):
                                    q_eod = _full_exit_available_qty(ctx, symbol, position_qty_live)
                                    sell_eod = _submit_full_exit_close(ctx, symbol, reason="end_of_day_exit")
                                    if sell_eod:
                                        log.info(
                                            "DYNAMIC_EOD_FLATTEN symbol=%s reason=%s",
                                            symbol,
                                            "intraday close window",
                                        )
                                        ctx.record_exit_action(symbol)
                                        ctx.note_daily_risk_order(symbol, side="sell", full_exit=True)
                                        ctx.log_sell_event(
                                            symbol,
                                            "signal_exit",
                                            {
                                                "engine_reason": "dynamic_eod_flatten",
                                                "qty": q_eod,
                                                "exit_price": float(exec_mid),
                                            },
                                        )
                                        print(ctx.now.strftime("%H:%M ET"), symbol, "SELL", q_eod, "shares — dynamic_eod_flatten", flush=True)
                                        ctx.record_engine_after_sell(
                                            symbol,
                                            ExitReason.SIGNAL_EXIT,
                                            float(exec_mid),
                                            entry_price_for_stop=entry_price if entry_price > 0 else None,
                                            remaining_qty_after=0,
                                        )
                                        _mark_dynamic_reentry_cooldown_if_needed(
                                            ctx,
                                            symbol,
                                            original_qty=int(qty),
                                            remaining_qty=0,
                                        )
                                        remove_tracked(symbol, user_id=ctx.user_id, data_dir=ctx.data_dir)
                                        ctx.notify_sqqq_tracker_removed(symbol)
                                        return

        if side != "short" and risk_enforce_position_caps_on_hold(ctx.config) and risk_rebalance_on_breach(ctx.config):
            cap_pct_rb = effective_symbol_allocation_cap_pct(
                ctx.config,
                account_equity=float(ctx.account_equity),
                symbol_upper=symbol,
            )
            if cap_pct_rb > 0.0:
                thr_rb = risk_rebalance_threshold_pct(ctx.config)
                try:
                    mv_rb = float(broker_pos.get("market_value") or 0.0)
                except (TypeError, ValueError):
                    mv_rb = 0.0
                if mv_rb <= 0.0:
                    mv_rb = float(qty) * float(exec_mid)
                trim_rb = symbol_allocation_breach_trim_shares(
                    equity=float(ctx.account_equity),
                    position_market_value_usd=mv_rb,
                    qty=int(qty),
                    mid_price=float(exec_mid),
                    cap_pct=float(cap_pct_rb),
                    rebalance_threshold_pct=float(thr_rb),
                )
                if trim_rb > 0 and live_trims_blocked_by_min_hold():
                    if ctx.verbose:
                        print(ctx.now.strftime("%H:%M ET"), symbol, "risk cap trim skipped — no_trim_before_min_hold", flush=True)
                if trim_rb > 0 and suppress_disc_trim and not live_trims_blocked_by_min_hold():
                    if ctx.verbose:
                        if block_dnw and not suppress_trim_trend:
                            print(ctx.now.strftime("%H:%M ET"), symbol, "risk cap trim skipped — do_not_sell_winners_early (trend still strong, pnl", f"{pnl_pp:.1f}%)", flush=True)
                        else:
                            print(ctx.now.strftime("%H:%M ET"), symbol, "risk cap trim skipped — require_signal_break_for_trim (trend still strong)", flush=True)
                if trim_rb > 0 and not live_trims_blocked_by_min_hold() and not suppress_disc_trim:
                    ctx.note_decision_intent(symbol, "exposure_trim")
                if trim_rb > 0 and not live_trims_blocked_by_min_hold() and not disc_exit_blocked() and not suppress_disc_trim and not post_buy_sell_cooldown_blocks_sell(ExitReason.RISK_CAP_REBALANCE):
                    if ctx.skip_exit_for_action_cap(symbol, "risk_cap_rebalance"):
                        return
                    if ctx.same_day_close_blocked(symbol, pos):
                        return
                    sell_rb = _build_safe_exit_sell_order(
                        ctx,
                        symbol,
                        int(trim_rb),
                        exec_mid=float(exec_mid),
                        spr_order=float(spr_order),
                        quote=quote,
                        positions=[broker_pos],
                    )
                    if sell_rb:
                        q_rb = float(sell_rb.quantity)
                        log_min_hold_debug(
                            "risk_cap_rebalance",
                            q_rb,
                            ExitReason.RISK_CAP_REBALANCE.value,
                        )
                        ctx.broker.submit_order(sell_rb)
                        ctx.record_exit_action(symbol)
                        rem_rb = float(qty) - q_rb
                        ctx.note_daily_risk_order(symbol, side="sell", full_exit=rem_rb <= 0)
                        ctx.log_sell_event(symbol, "exposure_limit", {"engine_reason": ExitReason.RISK_CAP_REBALANCE.value})
                        print(ctx.now.strftime("%H:%M ET"), symbol, "SELL", q_rb, "shares —", ExitReason.RISK_CAP_REBALANCE.value, "(cap %.1f%% + thr %.1f%%)" % (float(cap_pct_rb), float(thr_rb)), flush=True)
                        ctx.record_engine_after_sell(symbol, ExitReason.RISK_CAP_REBALANCE, float(exec_mid), entry_price_for_stop=entry_price if entry_price > 0 else None, remaining_qty_after=float(qty) - q_rb)
                        _mark_dynamic_reentry_cooldown_if_needed(
                            ctx,
                            symbol,
                            original_qty=int(qty),
                            remaining_qty=rem_rb,
                        )
                        if rem_rb <= 0:
                            remove_tracked(symbol, user_id=ctx.user_id, data_dir=ctx.data_dir)
                            ctx.notify_sqqq_tracker_removed(symbol)
                        else:
                            update_tracked(symbol, user_id=ctx.user_id, data_dir=ctx.data_dir, qty=rem_rb, partial_taken=True, trail_high=float(exec_mid))
                        return

        if side != "short" and getattr(ctx.engine.strategy, "trim_on_overweight", False):
            tf_ow = float(getattr(ctx.engine.strategy, "overweight_target_fraction", 0.0) or 0.0)
            ag_ow = float(getattr(ctx.engine.strategy, "trim_aggressiveness", 0.0) or 0.0)
            if tf_ow > 0.0 and ag_ow > 0.0:
                try:
                    mv_ow = float(broker_pos.get("market_value") or 0.0)
                except (TypeError, ValueError):
                    mv_ow = 0.0
                if mv_ow <= 0.0:
                    mv_ow = float(qty) * float(exec_mid)
                trim_ow = compute_overweight_trim_shares(
                    equity=float(ctx.account_equity),
                    position_market_value_usd=mv_ow,
                    qty=int(qty),
                    mid_price=float(exec_mid),
                    target_fraction=tf_ow,
                    aggressiveness=ag_ow,
                )
                if trim_ow > 0 and live_trims_blocked_by_min_hold():
                    if ctx.verbose:
                        print(ctx.now.strftime("%H:%M ET"), symbol, "overweight trim skipped — no_trim_before_min_hold", flush=True)
                if trim_ow > 0 and suppress_disc_trim and not live_trims_blocked_by_min_hold():
                    if ctx.verbose:
                        if block_dnw and not suppress_trim_trend:
                            print(ctx.now.strftime("%H:%M ET"), symbol, "overweight trim skipped — do_not_sell_winners_early (trend still strong, pnl", f"{pnl_pp:.1f}%)", flush=True)
                        else:
                            print(ctx.now.strftime("%H:%M ET"), symbol, "overweight trim skipped — require_signal_break_for_trim (trend still strong)", flush=True)
                if trim_ow > 0 and not live_trims_blocked_by_min_hold() and not suppress_disc_trim:
                    ctx.note_decision_intent(symbol, "rebalance")
                if trim_ow > 0 and not live_trims_blocked_by_min_hold() and not disc_exit_blocked() and not suppress_disc_trim and not post_buy_sell_cooldown_blocks_sell(ExitReason.OVERWEIGHT_TRIM):
                    if ctx.skip_exit_for_action_cap(symbol, "overweight_trim"):
                        return
                    if ctx.same_day_close_blocked(symbol, pos):
                        return
                    sell_ow = _build_safe_exit_sell_order(
                        ctx,
                        symbol,
                        int(trim_ow),
                        exec_mid=float(exec_mid),
                        spr_order=float(spr_order),
                        quote=quote,
                        positions=[broker_pos],
                    )
                    if sell_ow:
                        q_ow = float(sell_ow.quantity)
                        log_min_hold_debug(
                            "overweight_trim",
                            q_ow,
                            ExitReason.OVERWEIGHT_TRIM.value,
                        )
                        ctx.broker.submit_order(sell_ow)
                        ctx.record_exit_action(symbol)
                        rem_ow = float(qty) - q_ow
                        ctx.note_daily_risk_order(symbol, side="sell", full_exit=rem_ow <= 0)
                        ctx.log_sell_event(symbol, "rebalance_trim", {"engine_reason": ExitReason.OVERWEIGHT_TRIM.value})
                        print(ctx.now.strftime("%H:%M ET"), symbol, "SELL", q_ow, "shares —", ExitReason.OVERWEIGHT_TRIM.value, "(target %.2f%% book aggr %.0f%%)" % (tf_ow * 100.0, ag_ow * 100.0), flush=True)
                        ctx.record_engine_after_sell(symbol, ExitReason.OVERWEIGHT_TRIM, float(exec_mid), entry_price_for_stop=entry_price if entry_price > 0 else None, remaining_qty_after=float(qty) - q_ow)
                        _mark_dynamic_reentry_cooldown_if_needed(
                            ctx,
                            symbol,
                            original_qty=int(qty),
                            remaining_qty=rem_ow,
                        )
                        if rem_ow <= 0:
                            remove_tracked(symbol, user_id=ctx.user_id, data_dir=ctx.data_dir)
                            ctx.notify_sqqq_tracker_removed(symbol)
                        else:
                            update_tracked(symbol, user_id=ctx.user_id, data_dir=ctx.data_dir, qty=rem_ow, partial_taken=True, trail_high=float(exec_mid))
                        return

        partial_taken = bool(pos.get("partial_taken", False))
        partial_for_exit = partial_taken
        trail_high_val = pos.get("trail_high")
        trail_high_f = float(trail_high_val) if trail_high_val is not None else None
        smart_scale_idx = int(pos.get("smart_scale_out_index") or 0)
        st_time = ctx.engine.strategy
        if (
            getattr(st_time, "time_based_trim_enabled", False)
            and side != "short"
            and hold_mins is not None
            and float(getattr(st_time, "time_based_trim_after_minutes", 0) or 0) > 0
            and float(hold_mins) >= float(getattr(st_time, "time_based_trim_after_minutes", 0) or 0)
            and not bool(pos.get("time_based_trim_done"))
        ):
            tb_min_pnl = float(getattr(st_time, "time_based_trim_min_profit_pct", 0) or 0)
            if pnl_pp >= tb_min_pnl - 1e-9:
                tbf = max(0.01, min(0.99, float(getattr(st_time, "time_based_trim_fraction", 0.15) or 0.15)))
                qty_tbt = min(max(1, int(int(qty) * tbf)), int(qty))
                if qty_tbt > 0:
                    if live_trims_blocked_by_min_hold():
                        if ctx.verbose:
                            print(ctx.now.strftime("%H:%M ET"), symbol, "time_based_trim skipped — no_trim_before_min_hold", flush=True)
                    elif suppress_disc_trim:
                        if ctx.verbose:
                            print(ctx.now.strftime("%H:%M ET"), symbol, "time_based_trim skipped — require_signal_break_for_trim / do_not_sell_winners_early", flush=True)
                    elif disc_exit_blocked():
                        pass
                    elif post_buy_sell_cooldown_blocks_sell(ExitReason.PARTIAL_TAKE_PROFIT):
                        pass
                    elif ctx.skip_exit_for_action_cap(symbol, "time_based_trim"):
                        return
                    elif ctx.same_day_close_blocked(symbol, pos):
                        return
                    else:
                        ctx.note_decision_intent(symbol, "take_profit")
                        sell_tb = _build_safe_exit_sell_order(
                            ctx,
                            symbol,
                            int(qty_tbt),
                            exec_mid=float(exec_mid),
                            spr_order=float(spr_order),
                            quote=quote,
                            positions=[broker_pos],
                        )
                        if sell_tb:
                            q_tb = float(sell_tb.quantity)
                            log_min_hold_debug("time_based_trim", q_tb, "time_based_trim")
                            ctx.broker.submit_order(sell_tb)
                            ctx.record_exit_action(symbol)
                            rem_tb = float(qty) - q_tb
                            ctx.note_daily_risk_order(symbol, side="sell", full_exit=rem_tb <= 0)
                            ctx.log_sell_event(symbol, "take_profit", {"engine_reason": "time_based_trim", "partial": True, "qty": q_tb})
                            print(ctx.now.strftime("%H:%M ET"), symbol, "SELL", q_tb, "shares — time_based_trim (fraction %.0f%%)" % (tbf * 100.0,), flush=True)
                            ctx.record_engine_after_sell(symbol, ExitReason.PARTIAL_TAKE_PROFIT, float(exec_mid), entry_price_for_stop=entry_price if entry_price > 0 else None, remaining_qty_after=float(qty) - q_tb)
                            _mark_dynamic_reentry_cooldown_if_needed(
                                ctx,
                                symbol,
                                original_qty=int(qty),
                                remaining_qty=rem_tb,
                            )
                            if rem_tb <= 0:
                                remove_tracked(symbol, user_id=ctx.user_id, data_dir=ctx.data_dir)
                                ctx.notify_sqqq_tracker_removed(symbol)
                            else:
                                update_tracked(symbol, user_id=ctx.user_id, data_dir=ctx.data_dir, qty=rem_tb, partial_taken=True, trail_high=float(exec_mid), time_based_trim_done=True)
                            return

        atr_pct_exit = None
        if symbol in ctx.symbols:
            try:
                df_ex = ctx.broker.get_bars(symbol, timeframe="1Day", limit=20)
                if not df_ex.empty and len(df_ex) >= 14:
                    atr = _atr(df_ex["high"], df_ex["low"], df_ex["close"], 14)
                    atr_pct_exit = (atr.iloc[-1] / df_ex["close"].iloc[-1]) * 100
            except Exception:
                pass

        if side != "short" and ctx.news_enabled and ctx.news_pipeline and ctx.news_rules:
            try:
                df_news = ctx.broker.get_bars(symbol, timeframe="1Day", limit=max(60, ctx.news_rules.weak_trend_ma_period + 5))
                if not df_news.empty and len(df_news) >= ctx.news_rules.weak_trend_ma_period:
                    sent = ctx.news_pipeline.sentiment_for_symbol(symbol)
                    if ctx.news_rules.should_sell(sent, df_news):
                        if reentry_sell_block:
                            if ctx.verbose:
                                print(ctx.now.strftime("%H:%M ET"), symbol, "news exit skipped — reentry balance", str(reentry_sell_why or ""), flush=True)
                            return
                        if block_dnw:
                            if ctx.verbose:
                                print(ctx.now.strftime("%H:%M ET"), symbol, "news exit skipped — do_not_sell_winners_early (trend still strong, pnl", f"{pnl_pp:.1f}%)", flush=True)
                            return
                        ctx.note_decision_intent(symbol, "rebalance")
                        mh = float(getattr(ctx.engine.strategy, "min_hold_minutes", 0) or 0)
                        skip_news_exit = mh > 0 and hold_mins is not None and hold_mins < mh
                        if skip_news_exit:
                            if ctx.verbose:
                                print(ctx.now.strftime("%H:%M ET"), symbol, "news exit skipped — min_hold", f"({hold_mins:.0f}m < {mh:.0f}m)")
                        else:
                            if disc_exit_blocked() or post_buy_sell_cooldown_blocks_sell(ExitReason.NEWS_SENTIMENT) or ctx.same_day_close_blocked(symbol, pos):
                                return
                            sell_order = _build_safe_exit_sell_order(
                                ctx,
                                symbol,
                                position_qty_live,
                                exec_mid=float(exec_mid),
                                spr_order=float(spr_order),
                                quote=quote,
                                positions=[broker_pos],
                            )
                            if sell_order:
                                if ctx.skip_exit_for_action_cap(symbol, "news_sentiment"):
                                    return
                                ctx.broker.submit_order(sell_order)
                                ctx.record_exit_action(symbol)
                                ctx.note_daily_risk_order(symbol, side="sell", full_exit=True)
                                ctx.log_sell_event(symbol, "signal_flip", {"engine_reason": ExitReason.NEWS_SENTIMENT.value, "sent": sent})
                                print(ctx.now.strftime("%H:%M ET"), symbol, "SELL", qty, "shares —", ExitReason.NEWS_SENTIMENT.value, "(sent=%.2f)" % sent)
                                ctx.record_engine_after_sell(symbol, ExitReason.NEWS_SENTIMENT, exec_mid, entry_price_for_stop=entry_price if entry_price > 0 else None, remaining_qty_after=0)
                                remove_tracked(symbol, user_id=ctx.user_id, data_dir=ctx.data_dir)
                                ctx.notify_sqqq_tracker_removed(symbol)
                            return
            except Exception as e:
                if ctx.verbose:
                    print(ctx.now.strftime("%H:%M ET"), symbol, "news exit skip —", type(e).__name__, str(e)[:50])

        if side == "short":
            if ctx.verbose:
                tp_hit, sl_hit, time_exit = ctx.engine.strategy.exit_eval_flags_for_log_short(symbol, entry_price, exec_mid, bars, 1.5, 2.0, 10, minutes_held=hold_mins)
                log.info("%s EXIT_EVAL tp_hit=%s sl_hit=%s time_exit=%s", str(symbol).strip().upper(), tp_hit, sl_hit, time_exit)
            exit_signal = ctx.engine.strategy.check_exit_short(symbol, entry_price, exec_mid, bars, 1.5, 2.0, 10, spread_ex, atr_pct_exit, minutes_held=hold_mins)
            if exit_signal:
                if ctx.skip_exit_for_action_cap(symbol, "legacy_short_cover") or ctx.same_day_close_blocked(symbol, pos):
                    return
                cover_order = ctx.engine.execution.build_order(symbol, "buy", qty, exec_mid, spr_order, ignore_spread_gate=quote.skip_spread_check, bid=float(quote.bid), ask=float(quote.ask))
                if cover_order:
                    ctx.broker.submit_order(cover_order)
                    ctx.record_exit_action(symbol)
                    ctx.note_daily_risk_order(symbol, side="buy", full_exit=True)
                    ctx.log_sell_event(symbol, sell_log_reason_for_engine_exit(exit_signal.reason.value), {"engine_reason": exit_signal.reason.value, "variant": "legacy_short_cover"})
                    print(ctx.now.strftime("%H:%M ET"), symbol, "COVER", qty, "shares (legacy short) —", exit_signal.reason.value)
                remove_tracked(symbol, user_id=ctx.user_id, data_dir=ctx.data_dir)
                ctx.notify_sqqq_tracker_removed(symbol)
            return

        if partial_taken:
            new_high = max(trail_high_f or entry_price, exec_mid)
            update_tracked(symbol, user_id=ctx.user_id, data_dir=ctx.data_dir, trail_high=new_high)
            trail_high_f = new_high
        if getattr(ctx.engine.strategy, "smart_trailing_enabled", False) and entry_price > 0:
            ret_tr = (exec_mid - entry_price) / entry_price * 100.0
            if ret_tr >= ctx.engine.strategy.smart_trailing_activate_profit_pct:
                nh_tr = max(entry_price, trail_high_f if trail_high_f is not None else entry_price, exec_mid)
                if trail_high_f is None or nh_tr > trail_high_f:
                    update_tracked(symbol, user_id=ctx.user_id, data_dir=ctx.data_dir, trail_high=nh_tr)
                    trail_high_f = nh_tr

        builtin_smart_trailing = True
        smart_on = getattr(ctx.engine.strategy, "smart_trailing_enabled", False)
        mh_st = float(getattr(ctx.engine.strategy, "smart_trailing_min_hold_minutes", 0) or 0)
        smart_trailing_deferred = smart_on and mh_st > 0 and hold_mins is not None and float(hold_mins) < mh_st
        if smart_on and not smart_trailing_deferred and side != "short":
            st_se = load_smart_exit_state_from_row(pos)
            if st_se is not None:
                st_se.entry_price = float(entry_price)
                if trail_high_f is not None:
                    bump_high_price(st_se, float(trail_high_f))
                cfg_se = smart_trailing_cfg_for_process(ctx.engine.strategy, atr_pct_exit)
                pos_se = SimpleNamespace(symbol=symbol, qty=qty)

                def smart_exit_sell(sym: str, q: int) -> bool | None:
                    nonlocal qty
                    if q <= 0:
                        return None
                    if suppress_disc_trim:
                        if ctx.verbose:
                            if block_dnw and not suppress_trim_trend:
                                print(ctx.now.strftime("%H:%M ET"), sym, "smart scale exit skipped — do_not_sell_winners_early (trend still strong, pnl", f"{pnl_pp:.1f}%)", flush=True)
                            else:
                                print(ctx.now.strftime("%H:%M ET"), sym, "smart scale exit skipped — require_signal_break_for_trim (trend still strong)", flush=True)
                        return None
                    if reentry_sell_block:
                        if ctx.verbose:
                            print(ctx.now.strftime("%H:%M ET"), sym, "smart scale exit skipped — reentry balance", str(reentry_sell_why or ""), flush=True)
                        return None
                    ctx.note_decision_intent(sym, "take_profit")
                    if disc_exit_blocked() or post_buy_sell_cooldown_blocks_sell(ExitReason.PARTIAL_TAKE_PROFIT):
                        return None
                    if ctx.skip_exit_for_action_cap(sym, "smart_trailing_scale"):
                        return False
                    if ctx.same_day_close_blocked(symbol, pos):
                        return None
                    q = min(int(q), int(qty))
                    if q <= 0:
                        return None
                    sell_order = _build_safe_exit_sell_order(
                        ctx,
                        sym,
                        q,
                        exec_mid=float(exec_mid),
                        spr_order=float(spr_order),
                        quote=quote,
                        positions=[broker_pos],
                    )
                    if sell_order:
                        q_sm = float(sell_order.quantity)
                        log_min_hold_debug("partial_take_profit", q_sm, "smart_trailing_scale")
                        ctx.broker.submit_order(sell_order)
                        ctx.record_exit_action(sym)
                        rem_sm = float(qty) - q_sm
                        ctx.note_daily_risk_order(sym, side="sell", full_exit=rem_sm <= 0)
                        ctx.log_sell_event(sym, "take_profit", {"variant": "smart_trailing_scale", "qty": q_sm})
                        print(ctx.now.strftime("%H:%M ET"), sym, "SELL", q_sm, "shares (smart scale) —", flush=True)
                        ctx.record_engine_after_sell(sym, ExitReason.PARTIAL_TAKE_PROFIT, exec_mid, entry_price_for_stop=entry_price if entry_price > 0 else None, remaining_qty_after=float(qty) - q_sm)
                        _mark_dynamic_reentry_cooldown_if_needed(
                            ctx,
                            sym,
                            original_qty=int(qty),
                            remaining_qty=rem_sm,
                        )
                        qty = rem_sm
                        if qty <= 0:
                            remove_tracked(sym, user_id=ctx.user_id, data_dir=ctx.data_dir)
                            ctx.notify_sqqq_tracker_removed(sym)
                            qty = 0
                        else:
                            update_tracked(sym, user_id=ctx.user_id, data_dir=ctx.data_dir, qty=qty, partial_taken=True, trail_high=st_se.high_price, smart_exit_state=smart_exit_state_to_json(st_se), smart_scale_out_index=len(st_se.scaled_levels))
                        return True
                    return None

                def smart_exit_sell_all(sym: str) -> bool | None:
                    nonlocal qty
                    if position_qty_live <= 0.0:
                        return None
                    ctx.note_decision_intent(sym, "stop_loss")
                    if ctx.skip_exit_for_action_cap(sym, "smart_trailing_stop"):
                        return False
                    if ctx.same_day_close_blocked(symbol, pos):
                        return None
                    q_all = _full_exit_available_qty(ctx, sym, position_qty_live)
                    sell_order = _submit_full_exit_close(ctx, sym, reason="trailing_stop")
                    if sell_order:
                        ctx.record_exit_action(sym)
                        ctx.note_daily_risk_order(sym, side="sell", full_exit=True)
                        ctx.log_sell_event(sym, "take_profit", {"variant": "smart_trailing_stop", "qty": q_all})
                        print(ctx.now.strftime("%H:%M ET"), sym, "SELL", q_all, "shares (smart trailing stop) —", flush=True)
                        ctx.record_engine_after_sell(sym, ExitReason.TRAILING_STOP, exec_mid, entry_price_for_stop=entry_price if entry_price > 0 else None, remaining_qty_after=0)
                        _mark_dynamic_reentry_cooldown_if_needed(
                            ctx,
                            sym,
                            original_qty=int(qty),
                            remaining_qty=0,
                        )
                        remove_tracked(sym, user_id=ctx.user_id, data_dir=ctx.data_dir)
                        ctx.notify_sqqq_tracker_removed(sym)
                        qty = 0
                        return True
                    return None

                res_se = process_smart_exit(pos_se, float(exec_mid), cfg_se, st_se, sell=smart_exit_sell, sell_all=smart_exit_sell_all)
                if res_se:
                    log.info("[%s] %s EXIT via %s", ctx.user_id, symbol, res_se)
                    return
                if qty <= 0:
                    return
                update_tracked(symbol, user_id=ctx.user_id, data_dir=ctx.data_dir, trail_high=float(st_se.high_price), smart_exit_state=smart_exit_state_to_json(st_se), smart_scale_out_index=len(st_se.scaled_levels))
                trail_high_f = float(st_se.high_price)
                smart_scale_idx = len(st_se.scaled_levels)
                partial_for_exit = partial_for_exit or len(st_se.scaled_levels) > 0
                builtin_smart_trailing = False

        df_exit_mf = None
        try:
            mf_slow = int(getattr(ctx.engine.strategy, "ma_slow", 50) or 50)
            mf_lim = max(130, mf_slow + 30, int(getattr(ctx.engine.strategy, "ma_fast", 10) or 10) + 30)
            df_exit_mf = ctx.broker.get_bars(symbol, timeframe="1Day", limit=mf_lim)
            if df_exit_mf is None or getattr(df_exit_mf, "empty", True):
                df_exit_mf = None
        except Exception:
            df_exit_mf = None

        exit_signal = ctx.engine.check_exit(
            symbol,
            entry_price,
            exec_mid,
            bars,
            spread_ex,
            atr_pct_exit,
            partial_taken=partial_for_exit,
            trail_high=trail_high_f,
            current_qty=qty,
            minutes_held=hold_mins,
            minutes_since_session_open_et=minutes_since_regular_session_open_et(ctx.now),
            log_exit_context=ctx.verbose,
            smart_scale_out_index=smart_scale_idx,
            builtin_smart_trailing=builtin_smart_trailing,
            ohlcv_df=df_exit_mf,
        )
        if exit_signal and block_dnw:
            r_dnw = exit_signal.reason
            if not exit_reason_is_stop_like(r_dnw) and r_dnw not in (ExitReason.KILL_SWITCH, ExitReason.KILL_SWITCH_PARTIAL):
                if ctx.verbose:
                    print(ctx.now.strftime("%H:%M ET"), symbol, "engine exit skipped — do_not_sell_winners_early (trend still strong, pnl", f"{pnl_pp:.1f}%); would have", exit_signal.reason.value, flush=True)
                exit_signal = None
        if exit_signal and suppress_trim_trend and exit_signal.reason in (ExitReason.PARTIAL_TAKE_PROFIT, ExitReason.TAKE_PROFIT):
            if ctx.verbose:
                print(ctx.now.strftime("%H:%M ET"), symbol, "engine exit skipped — require_signal_break_for_trim (trend still strong); would have", exit_signal.reason.value, flush=True)
            exit_signal = None

        suppressed_dynamic_kill_switch_partial = False
        if (
            exit_signal
            and exit_signal.reason == ExitReason.KILL_SWITCH_PARTIAL
            and _should_suppress_dynamic_kill_switch_partial(
                ctx,
                symbol,
                df_exit_mf,
                spread_ex,
                atr_pct_exit,
            )
        ):
            log.info(
                "KILL_SWITCH_PARTIAL_SUPPRESSED_DYNAMIC_MOMENTUM symbol=%s reason=strong_momentum",
                symbol,
            )
            suppressed_dynamic_kill_switch_partial = True
            exit_signal = None

        if exit_signal:
            if _is_runtime_dynamic_momentum_symbol(ctx, symbol):
                try:
                    _dyn_small_profit_min = float(
                        (ctx.config.get("dynamic_universe") or {}).get(
                            "min_profit_before_full_exit_pct", 2.0
                        )
                        or 2.0
                    )
                except (TypeError, ValueError):
                    _dyn_small_profit_min = 2.0
                try:
                    _dyn_profit_pct = ((float(exec_mid) - float(entry_price)) / float(entry_price)) * 100.0
                except Exception:
                    _dyn_profit_pct = 0.0
                try:
                    _dyn_vwap_ref = float(_vwap_dy) if "_vwap_dy" in locals() and _vwap_dy is not None else None
                except Exception:
                    _dyn_vwap_ref = None
                if (
                    _dyn_profit_pct < _dyn_small_profit_min - 1e-9
                    and not exit_reason_is_stop_like(exit_signal.reason)
                    and exit_signal.reason not in (ExitReason.KILL_SWITCH, ExitReason.KILL_SWITCH_PARTIAL)
                    and (
                        _dyn_vwap_ref is None
                        or float(exec_mid) >= float(_dyn_vwap_ref)
                    )
                ):
                    log.info(
                        "DYNAMIC_HOLD_SMALL_PROFIT symbol=%s profit_pct=%.2f min=%.2f",
                        symbol,
                        _dyn_profit_pct,
                        _dyn_small_profit_min,
                    )
                    return
            if reentry_sell_block and not reentry_block_allows_despite_flag(exit_signal.reason):
                if ctx.verbose:
                    print(ctx.now.strftime("%H:%M ET"), symbol, "engine exit skipped — reentry balance", str(reentry_sell_why or ""), flush=True)
                return
            if post_buy_sell_cooldown_blocks_sell(exit_signal.reason):
                return
            ctx.note_decision_intent(symbol, exit_reason_to_intent_kind(exit_signal.reason))
            if not exit_reason_is_stop_like(exit_signal.reason) and disc_exit_blocked():
                return
            if ctx.same_day_close_blocked(symbol, pos) or ctx.skip_exit_for_action_cap(symbol, str(exit_signal.reason.value)):
                return
            if exit_signal.reason in (ExitReason.PARTIAL_TAKE_PROFIT, ExitReason.KILL_SWITCH_PARTIAL):
                raw_q = exit_signal.metadata.get("qty_to_sell", max(1, qty // 2))
                try:
                    qty_to_sell = int(float(raw_q))
                except (TypeError, ValueError):
                    qty_to_sell = max(1, qty // 2)
                qty_to_sell = max(1, min(qty_to_sell, qty))
                sell_order = _build_safe_exit_sell_order(
                    ctx,
                    symbol,
                    qty_to_sell,
                    exec_mid=float(exec_mid),
                    spr_order=float(spr_order),
                    quote=quote,
                    positions=[broker_pos],
                )
                if sell_order:
                    sold_px = float(sell_order.quantity)
                    _partial_path = (
                        "kill_switch_partial"
                        if exit_signal.reason == ExitReason.KILL_SWITCH_PARTIAL
                        else "partial_take_profit"
                    )
                    log_min_hold_debug(_partial_path, sold_px, exit_signal.reason.value)
                    ctx.broker.submit_order(sell_order)
                    ctx.record_exit_action(symbol)
                    remaining = qty - sold_px
                    ctx.note_daily_risk_order(symbol, side="sell", full_exit=remaining <= 0)
                    ctx.log_sell_event(symbol, sell_log_reason_for_engine_exit(exit_signal.reason.value), {"engine_reason": exit_signal.reason.value, "partial": True, "qty": sold_px})
                    partial_lbl = "kill-switch partial" if exit_signal.reason == ExitReason.KILL_SWITCH_PARTIAL else "partial take-profit"
                    print(ctx.now.strftime("%H:%M ET"), symbol, "SELL", sold_px, "shares (%s) —" % partial_lbl, exit_signal.reason.value)
                    ctx.record_engine_after_sell(symbol, exit_signal.reason, exec_mid, entry_price_for_stop=entry_price if entry_price > 0 else None, remaining_qty_after=float(qty) - sold_px)
                    _mark_dynamic_reentry_cooldown_if_needed(
                        ctx,
                        symbol,
                        original_qty=int(qty),
                        remaining_qty=remaining,
                    )
                    if remaining <= 0:
                        remove_tracked(symbol, user_id=ctx.user_id, data_dir=ctx.data_dir)
                        ctx.notify_sqqq_tracker_removed(symbol)
                    else:
                        upd = {"symbol": symbol, "user_id": ctx.user_id, "data_dir": ctx.data_dir, "qty": remaining, "partial_taken": True, "trail_high": exec_mid}
                        if exit_signal.metadata.get("smart_scale_level") is not None:
                            upd["smart_scale_out_index"] = int(exit_signal.metadata["smart_scale_level"]) + 1
                        update_tracked(**upd)
            else:
                q_full = _full_exit_available_qty(ctx, symbol, position_qty_live)
                sell_order = _submit_full_exit_close(
                    ctx,
                    symbol,
                    reason=str(exit_signal.reason.value),
                )
                if sell_order:
                    ctx.record_exit_action(symbol)
                    ctx.note_daily_risk_order(symbol, side="sell", full_exit=True)
                    ctx.log_sell_event(
                        symbol,
                        sell_log_reason_for_engine_exit(exit_signal.reason.value),
                        {
                            "engine_reason": exit_signal.reason.value,
                            "qty": q_full,
                            "exit_price": float(exec_mid),
                        },
                    )
                    print(ctx.now.strftime("%H:%M ET"), symbol, "SELL", q_full, "shares —", exit_signal.reason.value)
                    ctx.record_engine_after_sell(symbol, exit_signal.reason, exec_mid, entry_price_for_stop=entry_price if entry_price > 0 else None, remaining_qty_after=0)
                    _mark_dynamic_reentry_cooldown_if_needed(
                        ctx,
                        symbol,
                        original_qty=int(qty),
                        remaining_qty=0,
                    )
                    remove_tracked(symbol, user_id=ctx.user_id, data_dir=ctx.data_dir)
                    ctx.notify_sqqq_tracker_removed(symbol)
        elif (
            not suppressed_dynamic_kill_switch_partial
            and not partial_taken
            and side != "short"
        ):
            merged_broker = dict(broker_pos)
            merged_broker["qty"] = qty
            _gross_pct_for_trim = None
            try:
                _gross_pct_for_trim = float(getattr(ctx.exposure_snapshot, "gross_pct"))
            except (TypeError, ValueError, AttributeError):
                _gross_pct_for_trim = None
            plpc_qty = ctx.engine.execution.partial_exit_sell_quantity(
                tracker_qty=qty,
                broker_position=merged_broker,
                current_gross_pct=_gross_pct_for_trim,
            )
            if plpc_qty > 0 and reentry_sell_block:
                if ctx.verbose:
                    print(ctx.now.strftime("%H:%M ET"), symbol, "partial unrealized P/L trim skipped — reentry balance", str(reentry_sell_why or ""), flush=True)
            if plpc_qty > 0 and live_trims_blocked_by_min_hold():
                if ctx.verbose:
                    print(ctx.now.strftime("%H:%M ET"), symbol, "partial unrealized P/L trim skipped — no_trim_before_min_hold", flush=True)
            if plpc_qty > 0 and suppress_disc_trim and not reentry_sell_block and not live_trims_blocked_by_min_hold():
                if ctx.verbose:
                    if block_dnw and not suppress_trim_trend:
                        print(ctx.now.strftime("%H:%M ET"), symbol, "partial unrealized P/L trim skipped — do_not_sell_winners_early (trend still strong, pnl", f"{pnl_pp:.1f}%)", flush=True)
                    else:
                        print(ctx.now.strftime("%H:%M ET"), symbol, "partial unrealized P/L trim skipped — require_signal_break_for_trim (trend still strong)", flush=True)
            if plpc_qty > 0 and not reentry_sell_block and not live_trims_blocked_by_min_hold() and not suppress_disc_trim:
                ctx.note_decision_intent(symbol, "take_profit")
            if plpc_qty > 0 and not reentry_sell_block and not live_trims_blocked_by_min_hold() and not disc_exit_blocked() and not suppress_disc_trim and not post_buy_sell_cooldown_blocks_sell(ExitReason.PARTIAL_TAKE_PROFIT):
                if ctx.skip_exit_for_action_cap(symbol, "partial_unrealized_pl") or ctx.same_day_close_blocked(symbol, pos):
                    return
                sell_plpc = _build_safe_exit_sell_order(
                    ctx,
                    symbol,
                    plpc_qty,
                    exec_mid=float(exec_mid),
                    spr_order=float(spr_order),
                    quote=quote,
                    positions=[broker_pos],
                )
                if sell_plpc:
                    q_pl = float(sell_plpc.quantity)
                    log_min_hold_debug("partial_take_profit", q_pl, "partial_unrealized_pl")
                    ctx.broker.submit_order(sell_plpc)
                    ctx.record_exit_action(symbol)
                    rem_plpc = qty - q_pl
                    ctx.note_daily_risk_order(symbol, side="sell", full_exit=rem_plpc <= 0)
                    ctx.log_sell_event(symbol, "take_profit", {"variant": "partial_unrealized_pl", "qty": q_pl})
                    print(ctx.now.strftime("%H:%M ET"), symbol, "SELL", q_pl, "shares (partial unrealized P/L) —", ExitReason.PARTIAL_TAKE_PROFIT.value, flush=True)
                    ctx.record_engine_after_sell(symbol, ExitReason.PARTIAL_TAKE_PROFIT, exec_mid, entry_price_for_stop=entry_price if entry_price > 0 else None, remaining_qty_after=float(qty) - q_pl)
                    _mark_dynamic_reentry_cooldown_if_needed(
                        ctx,
                        symbol,
                        original_qty=int(qty),
                        remaining_qty=rem_plpc,
                    )
                    if rem_plpc <= 0:
                        remove_tracked(symbol, user_id=ctx.user_id, data_dir=ctx.data_dir)
                        ctx.notify_sqqq_tracker_removed(symbol)
                    else:
                        update_tracked(symbol, user_id=ctx.user_id, data_dir=ctx.data_dir, qty=rem_plpc, partial_taken=True, trail_high=exec_mid)
    except Exception as e:
        print(ctx.now.strftime("%H:%M ET"), symbol, "exit check skip —", type(e).__name__, str(e)[:200], flush=True)
        if is_alpaca_pdt_trade_denial(e):
            print(ctx.now.strftime("%H:%M ET"), symbol, "—", alpaca_pdt_exit_hint_line(), flush=True)
        return


__all__ = ["manage_stock_position"]
