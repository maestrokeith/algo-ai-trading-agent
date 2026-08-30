"""
Live: full-exit long stock lines that fall **outside** the top *N* by
:func:`~src.rank_based_holding.rank_positions` (``portfolio.rank_based_holding``).
"""
from __future__ import annotations

import logging
from collections.abc import Set
from typing import Any

from src.options_premium_risk import is_option_symbol
from src.pdt_safety import block_same_day_close_for_pdt
from src.portfolio_replacement import (
    eligible_long_stock_symbols,
    max_portfolio_positions_from_config,
    replacement_weakest_min_hold_ok,
)
from src.position_state_machine import blocks_discretionary_stock_exit
from src.position_tracker import (
    bars_held as bars_held_tracker,
    holding_minutes,
    load as load_tracked,
    remove as remove_tracked,
)
from src.rank_based_holding import (
    keep_top_n_sell_rest,
    parse_rank_based_holding_cfg,
    rank_positions,
    sell_rest_worst_first,
)
from src.safe_sell import build_safe_sell_order_request
from src.strategy import ExitReason

from .exits import LiveExitContext

def execution_bypass_no_sell_after_buy_cooldown(*args, **kwargs) -> bool:
    return False

log = logging.getLogger(__name__)


def maybe_sell_non_top_n_holdings(
    ctx: LiveExitContext,
    *,
    positions: list[dict[str, Any]],
    universe_symbols: Set[str],
    bear_etf_symbols: Set[str],
    portfolio_cfg: dict[str, Any] | None,
    tracked: dict[str, Any] | None = None,
) -> None:
    """
    When ``portfolio.rank_based_holding.enabled`` is true and there are more eligible
    long **universe** stock lines than *top_n*, full-exit up to *max_sells_per_pass*
    names, **worst-ranked first**, after the usual discretionary gates.
    """
    pcfg = portfolio_cfg if isinstance(portfolio_cfg, dict) else {}
    rbc = parse_rank_based_holding_cfg(pcfg)
    if not rbc["enabled"]:
        return

    _maxp = int(max_portfolio_positions_from_config(pcfg))
    top_n = rbc["top_n"] or _maxp
    if top_n <= 0:
        return

    eligible = eligible_long_stock_symbols(
        positions,
        universe_symbols=universe_symbols,
        bear_etf_symbols=bear_etf_symbols,
    )
    if len(eligible) <= top_n:
        return

    tr = tracked if isinstance(tracked, dict) else load_tracked(ctx.user_id, data_dir=ctx.data_dir)
    rep_sub = (pcfg.get("replacement") or {}) if isinstance(pcfg.get("replacement"), dict) else {}

    def _get_bars(s: str) -> Any:
        return ctx.broker.get_bars(s, timeframe="1Day", limit=260)

    ranked = rank_positions(
        eligible,
        tr,
        list(positions),
        get_bars=_get_bars,
        engine=ctx.engine,
        rep_sub=rep_sub,
    )
    _keep, rest = keep_top_n_sell_rest(ranked, top_n=top_n)
    to_exit = sell_rest_worst_first(rest)
    max_sells = rbc["max_sells_per_pass"]
    if max_sells <= 0:
        return

    _re_sell_block, _re_sell_why = ctx.reentry_block_discretionary_sells()

    n_done = 0
    for sym in to_exit:
        if n_done >= max_sells:
            break
        su = str(sym).upper()
        if is_option_symbol(su):
            continue
        row_tr = (tr or {}).get(su)
        if not isinstance(row_tr, dict):
            continue
        qty = int(row_tr.get("qty", 0))
        if qty <= 0:
            continue
        b_row = next(
            (p for p in positions if str(p.get("symbol") or "").upper() == su),
            None,
        )
        if not isinstance(b_row, dict):
            continue
        b_qty = int(float(b_row.get("qty") or 0))
        if b_qty <= 0:
            continue
        # --- gates (discretionary full exit) ---
        blocked, psm_reason = blocks_discretionary_stock_exit(
            su, ctx.user_id, ctx.data_dir, ctx.now, ctx.config
        )
        if blocked:
            if ctx.verbose and psm_reason:
                print(
                    ctx.now.strftime("%H:%M ET"),
                    su,
                    "rank_based_holding skip —",
                    psm_reason,
                    flush=True,
                )
            continue
        if _re_sell_block:
            if ctx.verbose:
                print(
                    ctx.now.strftime("%H:%M ET"),
                    su,
                    "rank_based_holding skip — reentry balance",
                    str(_re_sell_why or ""),
                    flush=True,
                )
            continue
        _pbc, _pbc_why = ctx.post_buy_sell_cooldown_active(su, row_tr)
        if _pbc and not execution_bypass_no_sell_after_buy_cooldown(ExitReason.SIGNAL_EXIT):
            if ctx.verbose and _pbc_why:
                print(
                    ctx.now.strftime("%H:%M ET"),
                    su,
                    "rank_based_holding skip —",
                    _pbc_why,
                    flush=True,
                )
            continue
        entry_iso = str(row_tr.get("entry_time", "") or "").strip()
        hold_mins = holding_minutes(entry_iso, ctx.now) if entry_iso else None
        b_ct = int(bars_held_tracker(entry_iso, ctx.now)) if entry_iso else 0
        st = ctx.engine.strategy
        tmeth = getattr(st, "trim_deferred_for_min_hold", None)
        if tmeth is not None and bool(tmeth(minutes_held=hold_mins, bars_held=b_ct)):
            if ctx.verbose:
                print(
                    ctx.now.strftime("%H:%M ET"),
                    su,
                    "rank_based_holding skip — no_trim_before_min_hold",
                    flush=True,
                )
            continue
        w_entry = row_tr.get("entry_time")
        _ok_mh, _mh_reason = replacement_weakest_min_hold_ok(
            weakest_entry_time_iso=str(w_entry) if w_entry else None,
            now=ctx.now,
            min_hold_minutes=rep_sub.get("min_hold_minutes"),
        )
        if not _ok_mh:
            if ctx.verbose and _mh_reason:
                print(
                    ctx.now.strftime("%H:%M ET"),
                    su,
                    "rank_based_holding skip —",
                    _mh_reason,
                    flush=True,
                )
            continue
        _pdt_b, _ = block_same_day_close_for_pdt(
            config=ctx.config,
            account_equity=ctx.account_equity,
            entry_time_iso=row_tr.get("entry_time"),
            now_et=ctx.now,
        )
        if _pdt_b:
            continue
        if ctx.skip_exit_for_action_cap(su, "rank_based_holding"):
            continue
        if ctx.same_day_close_blocked(su, row_tr):
            continue
        q = ctx.broker.get_latest_quote(su)
        if not q or not hasattr(q, "reference_mid"):
            continue
        entry_price = float(row_tr.get("entry_price", 0) or 0.0)
        if entry_price <= 0.0:
            continue
        _px_fb = entry_price
        try:
            _b1 = ctx.broker.get_bars(su, timeframe="1Day", limit=1)
            if _b1 is not None and not _b1.empty:
                _px_fb = float(_b1["close"].iloc[-1])
        except Exception:
            pass
        _mid = q.reference_mid(_px_fb)
        _spread = None if q.skip_spread_check else q.spread_pct
        _spr = float(_spread) if _spread is not None else 0.0
        open_orders = ctx.broker.get_open_orders()

        def _order_field(o, name, default=None):
            if isinstance(o, dict):
                return o.get(name, default)
            return getattr(o, name, default)

        has_pending_sell = any(
            str(_order_field(o, "symbol", "")).upper() == str(su).upper()
            and str(_order_field(o, "side", "")).lower() == "sell"
            for o in open_orders
        )

        if has_pending_sell:
            print(f"SKIP {su}: existing sell order pending")
            continue

        ctx.note_decision_intent(su, "rebalance")
        sell_order = build_safe_sell_order_request(
            ctx.broker,
            ctx.engine.execution,
            su,
            float(b_qty),
            mid_price=float(_mid),
            spread_pct=float(_spr),
            ignore_spread_gate=bool(q.skip_spread_check),
            bid=float(q.bid),
            ask=float(q.ask),
            positions=[pos],
        )
        if not sell_order:
            continue
        _sq = float(sell_order.quantity)

        try:
            ctx.broker.cancel_open_orders_for_symbol(su)
            print(f"CANCEL {su}: open orders before rank_based_holding sell")
        except Exception:
            pass

        ctx.broker.submit_order(sell_order)
        ctx.record_exit_action(su)
        ctx.note_daily_risk_order(su, side="sell", full_exit=True)
        ctx.log_sell_event(
            su,
            "rebalance_trim",
            {
              "engine_reason": (
                ExitReason.SIGNAL_EXIT.value
                if hasattr(ExitReason.SIGNAL_EXIT, "value")
                else str(ExitReason.SIGNAL_EXIT)
                ),
                "variant": "rank_based_holding",
                "kept": _keep,
            },
        )
        print(
            ctx.now.strftime("%H:%M ET"),
            su,
            "SELL",
            _sq,
            "shares —",
            "rank_based_holding",
            f"(not in top {top_n})",
            flush=True,
        )
        ctx.record_engine_after_sell(
            su,
            ExitReason.SIGNAL_EXIT,
            float(_mid),
            entry_price_for_stop=entry_price,
            remaining_qty_after=0,
        )
        remove_tracked(su, user_id=ctx.user_id, data_dir=ctx.data_dir)
        ctx.notify_sqqq_tracker_removed(su)
        n_done += 1
        if ctx.verbose:
            log.info(
                "[%s] rank_based_holding: full exit %s (kept %s, ranked=%s)",
                ctx.user_id,
                su,
                _keep,
                ranked,
            )
