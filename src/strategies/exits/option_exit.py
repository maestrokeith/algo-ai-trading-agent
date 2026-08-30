"""Option exit surface for split live exit workflows."""

from __future__ import annotations

import pytz

from src.app.live_context import session_vwap_and_ema9
from src.brokers.alpaca_client import QuoteInfo
from src.options_exit import evaluate_long_option_exit
from src.options_premium_risk import is_option_position
from src.options_selector import parse_occ_equity_option_symbol
from src.options_position_manager import record_option_exit
from src.pdt_safety import entry_opened_same_calendar_day_et
from src.position_tracker import (
    bars_held,
    load as load_tracked,
    remove as remove_tracked,
    update as update_tracked,
)
from src.strategy import ExitReason
from src.strategies.exits.context import LiveExitContext
from src.loop_helpers import alpaca_pdt_exit_hint_line, is_alpaca_pdt_trade_denial


def manage_option_position(
    ctx: LiveExitContext,
    position: dict[str, object],
) -> None:
    """Long OCC option: rules + orders (ctx.broker row)."""
    if not is_option_position(position):
        return
    symbol = str(position.get("symbol") or "").strip().upper()
    try:
        live_tracked = load_tracked(ctx.user_id, data_dir=ctx.data_dir)
        pos = live_tracked.get(symbol)
        untracked = pos is None
        if pos is None:
            pos = ctx.synthetic_option_tracker_pos(position)

        qty_opt = abs(int(float(position.get("qty") or 0)))
        if qty_opt <= 0:
            return
        side_o = str(position.get("side") or "long").strip().lower()
        if side_o not in ("long", ""):
            if ctx.verbose:
                print(ctx.now.strftime("%H:%M ET"), symbol, "option exit skip — not a long position", flush=True)
            return

        oq = None
        get_quote = getattr(ctx.broker, "get_option_latest_quote", None)
        if callable(get_quote):
            oq = get_quote(symbol)
        if oq is None:
            mv_o = float(position.get("market_value") or 0)
            if qty_opt > 0 and mv_o > 0:
                mid_est = mv_o / (qty_opt * 100.0)
                oq = QuoteInfo(
                    bid=max(mid_est * 0.995, 1e-4),
                    ask=max(mid_est * 1.005, 1e-4),
                    mid=mid_est,
                    spread_pct=1.0,
                    timestamp=None,
                    last=None,
                    skip_spread_check=False,
                )
        if oq is None:
            if ctx.verbose:
                print(ctx.now.strftime("%H:%M ET"), symbol, "option exit skip — no quote or market value", flush=True)
            return

        ex_opt = (ctx.config.get("options") or {}).get("exits") or {}
        entry_px = float(position.get("avg_entry_price") or 0.0)
        if entry_px <= 0 and qty_opt > 0:
            cb_abs = abs(float(position.get("cost_basis") or 0.0))
            if cb_abs > 0:
                entry_px = cb_abs / (qty_opt * 100.0)
        current_px = float(position.get("current_price") or 0.0)
        if current_px <= 0:
            current_px = float(oq.mid)
        pnl_simple: float | None = None
        if entry_px > 1e-8 and current_px > 0:
            pnl_simple = (current_px - entry_px) / entry_px * 100.0
        if pnl_simple is not None:
            print(ctx.now.strftime("%H:%M ET"), symbol, "option exit eval → pnl=%.2f%%" % pnl_simple, flush=True)

        mode = str((ctx.config.get("options") or {}).get("mode") or "").strip().lower()
        eod_minutes_raw = ex_opt.get("exit_before_market_close_min", 5)
        try:
            eod_minutes = max(0, int(float(eod_minutes_raw)))
        except (TypeError, ValueError):
            eod_minutes = 5
        et_now = ctx.now.astimezone(pytz.timezone("America/New_York"))

        def _max_exit_spread_pct() -> float:
            raw = ex_opt.get("max_exit_spread_pct")
            if raw is None or str(raw).strip() == "":
                raw = ex_opt.get("max_bid_ask_spread_pct")
            if raw is None or str(raw).strip() == "":
                raw = (ctx.config.get("options") or {}).get("max_bid_ask_spread_pct", 8.0)
            try:
                v = float(raw)
            except (TypeError, ValueError):
                return 8.0
            if v <= 0:
                return 8.0
            if v < 1.0:
                return v * 100.0
            return v

        def _record_full_exit(exit_reason: ExitReason, exit_price: float, sell_qty: int) -> None:
            entry_price_opt = float(pos.get("entry_price") or 0.0)
            realized_pl_opt = None
            if entry_price_opt > 0:
                realized_pl_opt = (float(exit_price) - entry_price_opt) * float(sell_qty) * 100.0
            record_option_exit(
                symbol,
                user_id=ctx.user_id,
                data_dir=ctx.data_dir,
                exit_reason=exit_reason.value,
                exit_price=float(exit_price),
                realized_pl=realized_pl_opt,
                now=ctx.now,
            )

        if float(oq.spread_pct) > _max_exit_spread_pct() + 1e-9:
            if not ctx.same_day_close_blocked(symbol, pos):
                max_spr = float(ctx.engine.execution.max_spread_pct_for_symbol(symbol) or 1.0)
                raw_spread = max(float(oq.spread_pct), 0.01)
                spr = min(raw_spread, max(max_spr * 0.999, 0.01))
                print(
                    ctx.now.strftime("%H:%M ET"),
                    "OPTIONS_EXIT_SIGNAL",
                    "symbol=%s reason=spread_too_wide spread=%.2f max=%.2f"
                    % (symbol, float(oq.spread_pct), _max_exit_spread_pct()),
                    flush=True,
                )
                sell_opt = ctx.engine.execution.build_order(
                    symbol,
                    "sell",
                    qty_opt,
                    float(oq.mid),
                    spr,
                    tick_size=0.01,
                    bid=float(oq.bid),
                    ask=float(oq.ask),
                    position_qty=qty_opt,
                )
                if sell_opt:
                    if ctx.skip_exit_for_action_cap(symbol, "option_spread_too_wide"):
                        return
                    ctx.broker.submit_order(sell_opt)
                    ctx.record_exit_action(symbol)
                    ctx.note_daily_risk_order(symbol, side="sell", full_exit=True)
                    ctx.log_sell_event(symbol, "stop_loss", {"variant": "option_spread_too_wide", "pnl_pct": pnl_simple})
                    print(
                        ctx.now.strftime("%H:%M ET"),
                        symbol,
                        "SELL",
                        qty_opt,
                        "contracts — spread too wide exit",
                        flush=True,
                    )
                    ctx.record_engine_after_sell(
                        symbol,
                        ExitReason.OPTION_SPREAD_TOO_WIDE,
                        float(oq.mid),
                        remaining_qty_after=0,
                    )
                    _record_full_exit(ExitReason.OPTION_SPREAD_TOO_WIDE, float(oq.mid), qty_opt)
                    remove_tracked(symbol, user_id=ctx.user_id, data_dir=ctx.data_dir)
                    return

        if et_now.hour == 15 and et_now.minute >= max(0, 60 - eod_minutes):
            if not ctx.same_day_close_blocked(symbol, pos):
                max_spr = float(ctx.engine.execution.max_spread_pct_for_symbol(symbol) or 1.0)
                raw_spread = max(float(oq.spread_pct), 0.01)
                spr = min(raw_spread, max(max_spr * 0.999, 0.01))
                sell_opt = ctx.engine.execution.build_order(
                    symbol,
                    "sell",
                    qty_opt,
                    float(oq.mid),
                    spr,
                    tick_size=0.01,
                    bid=float(oq.bid),
                    ask=float(oq.ask),
                    position_qty=qty_opt,
                )
                if sell_opt:
                    if ctx.skip_exit_for_action_cap(symbol, "option_end_of_day"):
                        return
                    ctx.broker.submit_order(sell_opt)
                    ctx.record_exit_action(symbol)
                    ctx.note_daily_risk_order(symbol, side="sell", full_exit=True)
                    ctx.log_sell_event(symbol, "take_profit", {"variant": "option_end_of_day", "pnl_pct": pnl_simple})
                    print(
                        ctx.now.strftime("%H:%M ET"),
                        symbol,
                        "SELL",
                        qty_opt,
                        "contracts — end_of_day_exit before market close",
                        flush=True,
                    )
                    ctx.record_engine_after_sell(
                        symbol,
                        ExitReason.OPTION_END_OF_DAY,
                        float(oq.mid),
                        remaining_qty_after=0,
                    )
                    _record_full_exit(ExitReason.OPTION_END_OF_DAY, float(oq.mid), qty_opt)
                    remove_tracked(symbol, user_id=ctx.user_id, data_dir=ctx.data_dir)
                    return

        thresh_raw = ex_opt.get("simple_price_exit_gain_pct")
        try:
            simple_exit = float(thresh_raw) if thresh_raw is not None and str(thresh_raw).strip() != "" else None
        except (TypeError, ValueError):
            simple_exit = None
        if simple_exit is not None and pnl_simple is not None and pnl_simple >= float(simple_exit):
            if not ctx.same_day_close_blocked(symbol, pos):
                max_spr = float(ctx.engine.execution.max_spread_pct_for_symbol(symbol) or 1.0)
                raw_spread = max(float(oq.spread_pct), 0.01)
                spr = min(raw_spread, max(max_spr * 0.999, 0.01))
                sell_opt = ctx.engine.execution.build_order(
                    symbol,
                    "sell",
                    qty_opt,
                    float(oq.mid),
                    spr,
                    tick_size=0.01,
                    bid=float(oq.bid),
                    ask=float(oq.ask),
                    position_qty=qty_opt,
                )
                if sell_opt:
                    if ctx.skip_exit_for_action_cap(symbol, "option_simple_price_exit"):
                        return
                    ctx.broker.submit_order(sell_opt)
                    ctx.record_exit_action(symbol)
                    ctx.note_daily_risk_order(symbol, side="sell", full_exit=True)
                    ctx.log_sell_event(symbol, "take_profit", {"variant": "option_simple_price_exit", "pnl_pct": pnl_simple})
                    print(
                        ctx.now.strftime("%H:%M ET"),
                        symbol,
                        "SELL",
                        qty_opt,
                        "contracts — simple_price_exit (pnl %.1f%% >= %.0f%%)" % (pnl_simple, float(simple_exit)),
                        flush=True,
                    )
                    ctx.record_engine_after_sell(symbol, ExitReason.OPTION_PROFIT_TAKE, float(oq.mid), remaining_qty_after=0)
                    _record_full_exit(ExitReason.OPTION_PROFIT_TAKE, float(oq.mid), qty_opt)
                    remove_tracked(symbol, user_id=ctx.user_id, data_dir=ctx.data_dir)
                    return
                if ctx.verbose:
                    print(ctx.now.strftime("%H:%M ET"), symbol, "option simple exit — could not build order (spread gate?)", flush=True)

        entry_time_iso_o = pos.get("entry_time", "")
        days_h_o = bars_held(entry_time_iso_o, ctx.now)
        upl_o = position.get("unrealized_pl")
        upl_f_o = float(upl_o) if upl_o is not None else None
        cb_o = float(position.get("cost_basis") or 0)
        u_close_o = None
        u_ma_o = None
        parsed_underlying = parse_occ_equity_option_symbol(symbol)
        if parsed_underlying and (
            bool(ex_opt.get("exit_if_underlying_breaks_signal", False))
            or bool(ex_opt.get("exit_if_underlying_loses_vwap", False))
            or mode == "paper_only"
        ):
            und_o = parsed_underlying[0]
            try:
                if mode == "paper_only" or bool(ex_opt.get("exit_if_underlying_loses_vwap", False)):
                    u_ma_o, _ema9 = session_vwap_and_ema9(ctx.broker, und_o, ctx.now)
                    get_uq = getattr(ctx.broker, "get_latest_quote", None)
                    if callable(get_uq):
                        try:
                            uq = get_uq(und_o)
                            if uq is not None and getattr(uq, "mid", None):
                                u_close_o = float(uq.mid)
                        except Exception:
                            pass
                    if u_close_o is None:
                        df_u = ctx.broker.get_bars(und_o, timeframe="1Day", limit=2)
                        if not df_u.empty:
                            u_close_o = float(df_u["close"].iloc[-1])
                else:
                    ma_period = int(ex_opt.get("underlying_ma_period", 50) or 50)
                    df_u = ctx.broker.get_bars(und_o, timeframe="1Day", limit=max(55, ma_period + 5))
                    if not df_u.empty and len(df_u) >= ma_period:
                        u_close_o = float(df_u["close"].iloc[-1])
                        u_ma_o = float(df_u["close"].rolling(ma_period).mean().iloc[-1])
            except Exception:
                pass

        mv_o_raw = position.get("market_value")
        mv_o = float(mv_o_raw) if mv_o_raw is not None else None
        dec_o = evaluate_long_option_exit(
            ctx.config,
            occ_symbol=symbol,
            days_held=days_h_o,
            unrealized_pl=upl_f_o,
            cost_basis=cb_o,
            underlying_close=u_close_o,
            underlying_ma=u_ma_o,
            market_value=mv_o,
            open_contracts=qty_opt,
            tracker_position=pos,
        )

        sell_contracts = dec_o.contracts_to_sell
        remove_after = dec_o.remove_tracker_after
        mark_tier1 = dec_o.mark_option_profit_tier1_done
        if untracked and sell_contracts is not None and int(sell_contracts) < qty_opt:
            sell_contracts = None
            remove_after = True
            mark_tier1 = False
            if ctx.verbose:
                print(ctx.now.strftime("%H:%M ET"), symbol, "option exit — full close (untracked row; partial tier needs tracker)", flush=True)

        if dec_o.should_exit and dec_o.reason is not None:
            if ctx.same_day_close_blocked(symbol, pos):
                if dec_o.persist_option_pnl_peak_pct is not None:
                    update_tracked(
                        symbol,
                        user_id=ctx.user_id,
                        data_dir=ctx.data_dir,
                        option_pnl_peak_pct=dec_o.persist_option_pnl_peak_pct,
                    )
                return
            max_spr = float(ctx.engine.execution.max_spread_pct_for_symbol(symbol) or 1.0)
            raw_spread = max(float(oq.spread_pct), 0.01)
            spr = min(raw_spread, max(max_spr * 0.999, 0.01))
            sell_qty = qty_opt if sell_contracts is None else max(1, min(int(sell_contracts), qty_opt))
            sell_opt = ctx.engine.execution.build_order(
                symbol,
                "sell",
                sell_qty,
                float(oq.mid),
                spr,
                tick_size=0.01,
                bid=float(oq.bid),
                ask=float(oq.ask),
                position_qty=qty_opt,
            )
            if sell_opt:
                if ctx.skip_exit_for_action_cap(symbol, "option_exit"):
                    return
                ctx.broker.submit_order(sell_opt)
                ctx.record_exit_action(symbol)
                ctx.note_daily_risk_order(symbol, side="sell", full_exit=bool(remove_after or sell_qty >= qty_opt))
                log_reason = dec_o.reason.value if dec_o.reason is not None else None
                ctx.log_sell_event(symbol, dec_o.reason and dec_o.reason.value or "signal_flip", {
                    "variant": "option_evaluate_exit",
                    "engine_reason": log_reason,
                })
                print(
                    ctx.now.strftime("%H:%M ET"),
                    symbol,
                    "SELL",
                    sell_qty,
                    "contracts —",
                    dec_o.reason.value,
                    "(%s)" % dec_o.message,
                    flush=True,
                )
                entry_price_opt = float(pos.get("entry_price") or 0.0)
                ctx.record_engine_after_sell(
                    symbol,
                    dec_o.reason,
                    float(oq.mid),
                    entry_price_for_stop=entry_price_opt if entry_price_opt > 0 else None,
                    remaining_qty_after=0 if (remove_after or sell_qty >= qty_opt) else max(0, int(qty_opt) - int(sell_qty)),
                )
                if remove_after or sell_qty >= qty_opt:
                    _record_full_exit(dec_o.reason, float(oq.mid), sell_qty)
                    remove_tracked(symbol, user_id=ctx.user_id, data_dir=ctx.data_dir)
                else:
                    rem = max(0, qty_opt - sell_qty)
                    update_tracked(
                        symbol,
                        rem,
                        user_id=ctx.user_id,
                        data_dir=ctx.data_dir,
                        option_profit_tier1_done=True if mark_tier1 else None,
                    )
            elif ctx.verbose:
                print(ctx.now.strftime("%H:%M ET"), symbol, "option exit — could not build order (spread gate?)", flush=True)

        if dec_o.persist_option_pnl_peak_pct is not None:
            update_tracked(
                symbol,
                user_id=ctx.user_id,
                data_dir=ctx.data_dir,
                option_pnl_peak_pct=dec_o.persist_option_pnl_peak_pct,
            )
        elif ctx.verbose and dec_o.pnl_pct is not None and not (dec_o.should_exit and dec_o.reason is not None):
            print(ctx.now.strftime("%H:%M ET"), symbol, "option hold — P/L %.1f%%" % dec_o.pnl_pct, flush=True)
    except Exception as e:
        print(ctx.now.strftime("%H:%M ET"), symbol, "option exit skip —", type(e).__name__, str(e)[:200], flush=True)
        if is_alpaca_pdt_trade_denial(e):
            print(ctx.now.strftime("%H:%M ET"), symbol, "—", alpaca_pdt_exit_hint_line(), flush=True)


__all__ = ["manage_option_position"]
