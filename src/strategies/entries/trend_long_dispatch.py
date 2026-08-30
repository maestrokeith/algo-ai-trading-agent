"""Dispatch trend-long entry after buying-power check (options → stock / replacement).

Stock path (signal passed upstream): if the distinct-long book still has room **or** this ticker is
already held (add-on), execute the buy/rotation-less add. Otherwise at cap with a **new** name,
:func:`evaluate_portfolio_replacement_for_dispatch` always runs (weakest vs incoming; no longer requires
``portfolio.enable_replacement`` in config). On approval, one or more laggard lines are sold
(``replace_losers_with_winners``), then the new name is bought. By default, when
``rotate_sell_tranche_fraction < 1``, the sell is split into **two** legs to fully exit
the line (tranche, then remainder). If ``rotate_partial_replacement: true`` (and tranche
``(0,1)``), only the **first** tranche is sold, the remaining shares stay open; the incoming
buy is notional-capped to ``min(planned, estimated proceeds)``.

When sizing fails with gross/net/theme headroom exhausted (``portfolio caps leave no room``) and
``portfolio.cap_pressure_trim`` is enabled, :func:`consider_replacement_for_sizing_reject` submits partial
sells (default 15% of shares on the **weakest** eligible long(s) first — same sorting as
:func:`~src.portfolio_replacement.replacement_hold_candidates_sorted_asc`; 10–20% clamp) before full
weakest-line rotation. Pair with ``allocation.rank_by_signal_strength: true`` (default in ``default.yaml``)
so the live pass tries **strongest** new signals before weaker ones.

When entry fails and :func:`src.portfolio_replacement.replacement_entry_fail_reason_invites_cap_rotation` is true
(substring ``\"cap\"`` in the reject reason), :func:`consider_replacement_for_sizing_reject` may rotate first
(live loop: then ``run_entry_gates`` is re-run for the same route: trend-long, news light, alternate, or news full),
then dispatch continues.

At ``max_positions`` the live loop also uses
:func:`src.portfolio_score_replacement.evaluate_strength_based_portfolio_swap` when
``portfolio.replacement.replacement_threshold`` is in (0, 1), else
:func:`src.portfolio_score_replacement.evaluate_score_based_portfolio_swap` (signal score vs
position score; gap ``portfolio.swap_threshold``, default 10).

When ``portfolio.portfolio_brain.enabled`` is true, runs :func:`src.portfolio_brain.portfolio_brain`
first (bucket / symbol / optional sector gates) before options or stock routing.

Options vs shares: :func:`src.strategy_router.route_options_or_shares` then
:func:`src.entry_router.route_to_options_executor` with ``selected_override`` when a contract fits.

When ``options.bypass_when_full`` (merged with legacy ``portfolio_full_strong_signal_options``) has
``allow_when_full``, at max equity names with a strong signal and no stock slot, a **second**
options attempt runs with ``premium_budget_cap_usd`` from ``max_option_allocation_per_trade`` /
``max_premium_usd`` so a long premium call can execute instead of skipping equity rotation.

When ``options.top_signals_only`` or ``options.require_top_signal`` is true, options routing requires ``row_tl['in_top_signals']``
(ranked top-signal batch). Narrow ``options.allowed_underlyings`` to restrict which names may use calls.

Do not open a long option on an underlying while a long equity line is already open in that symbol
(:func:`~src.options_premium_risk.holding_equity_long_for_underlying`).

Migration note: the preferred split surfaces now live under ``src/strategies/entries/`` and
``src/portfolio/`` helper modules. This file remains the compatibility entry point while callers
move over gradually.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING, Any, Callable, Mapping

from src.entry_router import (
    EntryRouteSignal,
    log_options_stock_path_if_ineligible,
    route_to_options_executor,
    route_to_stock_executor,
    should_use_options,
    trend_long_options_extra_gate_ok,
    trend_long_options_extra_gate_reason,
    use_equity_fallback_after_options,
)
from src.entry_decision_log import emit_options_fallback_to_stock
from src.portfolio_allocation import (
    add_on_passes_signal_and_scale,
    parse_add_on_gate_cfg,
    symbol_long_position_market_value_usd,
)
from src.portfolio.rebalance_trims import trim_qty_for_fraction
from src.safe_sell import clamp_sell_qty_for_open_orders
from src.portfolio_replacement import (
    REPLACEMENT_STRATEGY_MIN_UNREALIZED_PL_VS_SIGNAL,
    consider_replacement,
    effective_signal_strength,
    parse_cap_pressure_trim_cfg,
    parse_replacement_strategy,
    portfolio_budget_cap_sizing_reject,
    replacement_churn_guard_min_new_vs_weakest_ratio,
    replacement_common_post_weakest_pick,
    replacement_hold_candidates_sorted_asc,
    replacement_incoming_entry_eval_triple,
    replacement_min_market_value_to_replace_usd,
    replacement_min_notional_for_incoming_usd,
    tracked_signal_strength,
)
from src.portfolio_score_replacement import (
    evaluate_score_based_portfolio_swap,
    evaluate_strength_based_portfolio_swap,
)
from src.position_tracker import (
    add as add_tracked,
    load as load_tracked,
    merge_add_shares as merge_add_tracked,
    minutes_held as holding_minutes,
    remove as remove_tracked,
    update as update_tracked,
    tracked_row_has_open_long,
)
from src.options_config import (
    allow_new_entries as options_allow_new_entries,
    never_bypass_stock_risk_caps,
    options_entry_environment_blocks,
    trend_long_options_top_signals_only_passes,
)
from src.options_entry_limits import (
    option_entry_allowed_by_daily_cap,
    option_entry_cooldown_blocks,
    record_option_entry,
    record_option_entry_utc,
)
from src.options_premium_risk import holding_equity_long_for_underlying
from src.portfolio_brain import portfolio_brain, portfolio_brain_enabled
from src.risk_limits import bucket_allocation_allows
from src.signal_ranking import sector_etf_symbol_frozenset
from src.strategy_router import route_options_or_shares
from src.sell_logging import log_sell
from src.entry_eval_log import log_options_gate, option_delta_from_chain

if TYPE_CHECKING:
    from src.strategies.exits.context import LiveExitContext

log = logging.getLogger(__name__)


def _entry_strength_for_add(row_tl: Mapping[str, Any], decision: Any) -> float | None:
    try:
        sig = getattr(decision, "entry_signal", None)
        if sig is not None and getattr(sig, "strength", None) is not None:
            return float(getattr(sig, "strength"))
    except (TypeError, ValueError):
        pass
    try:
        if row_tl.get("strength_eff") is not None:
            return float(row_tl.get("strength_eff"))
    except (TypeError, ValueError):
        pass
    return None


def _strong_add_threshold(config: Mapping[str, Any], gate_cfg: Mapping[str, Any]) -> float:
    raw = gate_cfg.get("min_signal_strength")
    if raw is None:
        raw = ((config.get("execution") if isinstance(config, Mapping) else {}) or {}).get(
            "strong_signal_strength_min",
            0.85,
        )
    try:
        out = float(raw)
    except (TypeError, ValueError):
        out = 0.85
    return max(0.0, min(1.0, out))


def _cap_incremental_add_notional(
    notional: float,
    *,
    account_equity: float,
    gate_cfg: Mapping[str, Any],
) -> float:
    try:
        pct = float(gate_cfg.get("incremental_add_pct", 0.02) or 0.02)
    except (TypeError, ValueError):
        pct = 0.02
    if pct > 1.0 + 1e-9:
        pct = pct / 100.0
    pct = max(0.0, min(1.0, pct))
    cap = max(0.0, float(account_equity or 0.0)) * pct
    if cap <= 0:
        return max(0.0, float(notional or 0.0))
    return max(0.0, min(float(notional or 0.0), cap))


def _log_options_stock_fallback_state(
    symbol: str,
    phase: str,
    *,
    reason: str,
) -> None:
    log.info(
        "%s OPTIONS_FAILED -> STOCK_FALLBACK %s reason=%s",
        str(symbol).upper(),
        phase,
        reason,
    )


def _emit_decision_report(decision: Any) -> None:
    report = getattr(decision, "decision_report", None)
    if report:
        log.info("%s", str(report))


def _sell_log_et_date(dt: Any) -> date:
    try:
        return dt.date()
    except Exception:
        return date.today()


def _min_hold_debug_values(
    row: Mapping[str, Any] | None,
    dt: Any,
    rep_sub: Mapping[str, Any] | None,
) -> tuple[float | None, float, bool]:
    rep = rep_sub if isinstance(rep_sub, Mapping) else {}
    try:
        min_hold = float(rep.get("min_hold_minutes", 0) or 0)
    except (TypeError, ValueError):
        min_hold = 0.0
    hold_mins = None
    if isinstance(row, Mapping):
        entry_time = str(row.get("entry_time") or "").strip()
        if entry_time:
            try:
                hold_mins = float(holding_minutes(entry_time, dt))
            except Exception:
                hold_mins = None
    blocked = hold_mins is not None and min_hold > 0.0 and hold_mins < min_hold
    return hold_mins, min_hold, blocked


def _log_min_hold_debug(
    *,
    symbol: str,
    path: str,
    row: Mapping[str, Any] | None,
    dt: Any,
    rep_sub: Mapping[str, Any] | None,
    qty: Any,
    reason: str,
    no_trim_before_min_hold: bool = False,
) -> None:
    hold_mins, min_hold, blocked = _min_hold_debug_values(row, dt, rep_sub)
    hold_val = "None" if hold_mins is None else "%.1f" % float(hold_mins)
    log.info(
        "MIN_HOLD_DEBUG symbol=%s path=%s hold_mins=%s min_hold=%.1f "
        "no_trim_before_min_hold=%s blocked_by_min_hold=%s qty=%s reason=%s",
        str(symbol).strip().upper(),
        path,
        hold_val,
        min_hold,
        bool(no_trim_before_min_hold),
        blocked,
        qty,
        reason,
    )


def _order_field(order: Any, key: str) -> Any:
    if isinstance(order, Mapping):
        return order.get(key)
    return getattr(order, key, None)


def _has_open_sell_order(broker: Any, symbol: str) -> bool:
    sym_u = str(symbol).strip().upper()
    open_orders = broker.list_orders(status="open")
    return any(
        str(_order_field(o, "symbol") or "").strip().upper() == sym_u
        and str(_order_field(o, "side") or "").strip().lower() == "sell"
        for o in open_orders or []
    )


def _cap_pressure_trim_sell_qty(
    broker: Any,
    symbol: str,
    requested_qty: float | int,
) -> float | None:
    """
    Clamp a cap-pressure trim to shares not reserved by open orders.

    Returns ``None`` when ``available_qty <= 0`` (logs ``TRIM_SKIPPED``).
    Logs ``TRIM_ADJUSTED`` when the request exceeds available inventory.
    """
    from unittest.mock import MagicMock

    sym_u = str(symbol or "").strip().upper()
    if not sym_u:
        return None
    if not isinstance(broker, MagicMock) and callable(
        getattr(broker, "available_position_qty", None)
    ):
        _pos_qty, reserved, available = broker.available_position_qty(sym_u)
    else:
        from src.safe_sell import available_sell_qty_shares

        _pos_qty, reserved, available = available_sell_qty_shares(broker, sym_u)

    if available <= 0.0:
        log.info(
            "TRIM_SKIPPED symbol=%s available=0 reserved_by_orders=%s",
            sym_u,
            reserved,
        )
        return None

    try:
        requested = float(requested_qty)
    except (TypeError, ValueError):
        return None

    clamped = float(clamp_sell_qty_for_open_orders(broker, sym_u, requested))
    clamped = min(clamped, float(available))
    if clamped <= 0.0:
        log.info(
            "TRIM_SKIPPED symbol=%s available=0 reserved_by_orders=%s",
            sym_u,
            reserved,
        )
        return None

    if clamped + 1e-9 < requested:
        log.info(
            "TRIM_ADJUSTED symbol=%s requested=%s available=%.3f",
            sym_u,
            requested,
            available,
        )
    return clamped


def evaluate_portfolio_replacement_for_dispatch(
    *,
    incoming_sym_upper: str,
    decision: Any,
    tracked: Mapping[str, Any],
    eligible_active: list[str],
    positions: list[dict[str, Any]],
    dt: Any,
    config: dict[str, Any],
    engine: Any,
    broker: Any,
    df: Any,
    atr_pct: Any,
    quote: Any,
    spread_pct_eval: float | None,
    regime_result: Any,
    entry_regime_score_int: int | None,
    rep_sub: dict[str, Any],
    strength_jitter_max: float,
    replace_if_weakest_older_than: int | None,
    max_position_age_bars: int | None,
    allow_equal_replacement: bool,
    replacement_threshold: float,
    incoming_notional_usd: float,
    replacement_scan_state: dict[str, Any] | None,
) -> tuple[list[str] | None, str | None]:
    """
    **consider_replacement()** — at max distinct longs, decide which hold(s) to sell before the
    incoming buy. Returns ``(symbols_to_sell, skip_reason)``. When *skip_reason* is set, do not
    trade. When the first return is a non-empty list, sell each full line (weakest / laggards
    first) then buy the new name. ``replace_losers_with_winners`` can return more than one symbol
    (e.g. two weak lines for one strong new entry).
    """
    if replacement_scan_state is not None:
        _rep_n = int(replacement_scan_state.get("count", 0))
        _rep_cap = int(replacement_scan_state.get("max", 10**9))
        if _rep_n >= _rep_cap:
            return (
                None,
                "portfolio replacement: max replacements this entry scan (%d)" % _rep_cap,
            )

    _pc_swap = config.get("portfolio") if isinstance(config.get("portfolio"), dict) else {}
    _thr_path = float(replacement_threshold or 0.0)
    _use_strength_swap = 0.0 < _thr_path < 1.0
    su = str(incoming_sym_upper).strip().upper()

    _rep_strat = parse_replacement_strategy(rep_sub)
    if _rep_strat == REPLACEMENT_STRATEGY_MIN_UNREALIZED_PL_VS_SIGNAL:
        try:
            _atr_ev = float(atr_pct) if atr_pct is not None and atr_pct == atr_pct else None
        except (TypeError, ValueError):
            _atr_ev = None
        _et, _em, _ep = replacement_incoming_entry_eval_triple(
            engine=engine,
            incoming_sym_upper=su,
            df=df,
            spread_pct=spread_pct_eval,
            atr_pct=_atr_ev,
            regime_score=entry_regime_score_int,
        )
        w_cand = consider_replacement(
            decision,
            positions=positions,
            eligible_symbols=eligible_active,
            incoming_sym_upper=su,
            strength_jitter_max=float(strength_jitter_max or 0.0),
            churn_guard_min_new_vs_weakest_ratio=replacement_churn_guard_min_new_vs_weakest_ratio(rep_sub),
            entry_eval_trend=_et,
            entry_eval_momentum=_em,
            entry_eval_pullback=_ep,
        )
        if w_cand is None:
            return (
                None,
                "portfolio replacement: min unrealized P/L vs signal — no rotation (no candidate or incoming not stronger)",
            )
        _wsym, _skip_rep = replacement_common_post_weakest_pick(
            weakest_sym=w_cand,
            incoming_sym_upper=su,
            tracked=tracked,
            positions=positions,
            dt=dt,
            rep_sub=rep_sub,
            incoming_notional_usd=float(incoming_notional_usd or 0.0),
            max_position_age_bars=max_position_age_bars,
        )
        if _skip_rep:
            return None, _skip_rep
        log.info("%s replacing %s (replacement.strategy=min_unrealized_pl_vs_signal)", su, _wsym)
        return [_wsym], None

    if _use_strength_swap:
        _sell_list, _skip_rep = evaluate_strength_based_portfolio_swap(
            incoming_sym_upper=su,
            decision=decision,
            tracked=tracked,
            eligible_active=list(eligible_active),
            positions=positions,
            dt=dt,
            rep_sub=rep_sub,
            strength_jitter_max=strength_jitter_max,
            replace_if_weakest_older_than_bars=replace_if_weakest_older_than,
            max_position_age_bars=max_position_age_bars,
            allow_equal_replacement=allow_equal_replacement,
            strength_gap=_thr_path,
            incoming_notional_usd=float(incoming_notional_usd),
            engine=engine,
            broker=broker,
            df=df,
        )
    else:
        _w_one, *_unused_swap_scores, _skip_rep = evaluate_score_based_portfolio_swap(
            incoming_sym_upper=su,
            engine=engine,
            broker=broker,
            df=df,
            atr_pct=float(atr_pct) if atr_pct is not None and atr_pct == atr_pct else None,
            quote=quote,
            spread_pct=spread_pct_eval,
            regime_result=regime_result,
            entry_regime_score=entry_regime_score_int,
            eligible_active=list(eligible_active),
            tracked=tracked,
            positions=positions,
            dt=dt,
            rep_sub=rep_sub,
            portfolio_cfg=_pc_swap,
        )
        _sell_list = [str(_w_one).upper()] if _w_one else None

    if _skip_rep:
        return None, _skip_rep
    if not _sell_list:
        return None, None
    if isinstance(_sell_list, str):
        _norm: list[str] = [str(_sell_list).strip().upper()]
    else:
        _norm = [str(x).strip().upper() for x in _sell_list if str(x).strip()]
    if _norm:
        return _norm, None
    return None, None


def execute_cap_pressure_partial_trim(
    *,
    incoming_sym_upper: str,
    eligible_active: list[str],
    tracked: Mapping[str, Any],
    positions: list[dict[str, Any]],
    dt: Any,
    trim_frac: float,
    max_symbols: int,
    broker: Any,
    engine: Any,
    rep_sub: dict[str, Any],
    user_id: str,
    data_dir: Any,
    log_entry_skip: Callable[..., None],
    verbose: bool,
    replacement_scan_state: dict[str, Any] | None,
    cycle_risk_state: dict[str, int] | None,
    per_cycle_exit_ctx: Any,
    live_risk_order_callback: Callable[[str, str, bool], None] | None,
    stale_quote_max_age: float,
) -> bool:
    """
    Sell ``trim_frac`` of shares (bounded, never full exit) on eligible longs to free gross headroom,
    then the live loop can re-run entry gates for the blocked buy.

    Symbols are processed **weakest replacement-strength first** (see
    :func:`~src.portfolio_replacement.replacement_hold_candidates_sorted_asc`), not largest notional first.

    Returns True if at least one partial sell was submitted.
    """
    su_in = str(incoming_sym_upper).strip().upper()
    if replacement_scan_state is not None:
        _rep_n = int(replacement_scan_state.get("count", 0))
        _rep_cap = int(replacement_scan_state.get("max", 10**9))
        if _rep_n >= _rep_cap:
            return False

    _rep_dict = rep_sub if isinstance(rep_sub, dict) else {}
    _tiny_mv_floor = replacement_min_market_value_to_replace_usd(_rep_dict)
    if _tiny_mv_floor <= 0:
        _tiny_mv_floor = 750.0

    tf = max(0.10, min(0.20, float(trim_frac)))
    candidates: list[tuple[str, int, float]] = []
    _tr = tracked if isinstance(tracked, dict) else {}
    for raw_sym in eligible_active:
        sym_u = str(raw_sym).strip().upper()
        if not sym_u or sym_u == su_in:
            continue
        row = _tr.get(sym_u)
        if not isinstance(row, dict):
            continue
        try:
            q = int(float(row.get("qty") or 0))
        except (TypeError, ValueError):
            q = 0
        if q <= 1:
            continue
        mv = float(symbol_long_position_market_value_usd(positions, sym_u))
        if mv < _tiny_mv_floor:
            continue
        candidates.append((sym_u, q, mv))

    _pass_syms = [t[0] for t in candidates]

    def _bars_for_trim(su: str):
        try:
            return broker.get_bars(str(su), timeframe="1Day", limit=220)
        except Exception:
            return None

    ranked = replacement_hold_candidates_sorted_asc(
        _tr,
        _pass_syms,
        positions=positions,
        get_bars=_bars_for_trim if _pass_syms else None,
        engine=engine,
        rep_sub=_rep_dict,
    )
    ranked_syms = [sym for sym, _sc in ranked][: max(1, int(max_symbols))]
    mv_map = {t[0]: t[2] for t in candidates}
    if ranked_syms:
        candidates_ordered = [(s, mv_map.get(s, 0.0)) for s in ranked_syms]
    else:
        candidates_fallback = sorted(candidates, key=lambda x: -x[2])[
            : max(1, int(max_symbols))
        ]
        candidates_ordered = [(t[0], t[2]) for t in candidates_fallback]

    did_any = False
    for sym_u, _mv in candidates_ordered:
        positions[:] = broker.get_positions()
        _tnow = load_tracked(user_id, data_dir=data_dir)
        row = _tnow.get(sym_u) if isinstance(_tnow, dict) else None
        if not isinstance(row, dict):
            continue
        try:
            q_live = int(float(row.get("qty") or 0))
        except (TypeError, ValueError):
            q_live = 0
        if q_live <= 1:
            continue
        sell_qty = max(1, int(q_live * tf))
        sell_qty = min(sell_qty, q_live - 1)
        if sell_qty < 1:
            continue

        _trim_sell_qty = _cap_pressure_trim_sell_qty(broker, sym_u, sell_qty)
        if _trim_sell_qty is None:
            continue
        sell_qty = _trim_sell_qty

        weakest_market_value_usd = float(symbol_long_position_market_value_usd(positions, sym_u))
        if weakest_market_value_usd < _tiny_mv_floor:
            continue

        _wq = broker.get_latest_quote(sym_u)
        if _wq is None:
            log_entry_skip(
                dt,
                su_in,
                "cap_pressure_trim: no quote for %s" % sym_u,
                verbose=verbose,
                force=False,
            )
            continue
        _w_row = row
        _w_fb = float(_w_row.get("entry_price") or 0) or 1.0
        try:
            _wb = broker.get_bars(sym_u, timeframe="1Day", limit=1)
            if _wb is not None and not _wb.empty:
                _w_fb = float(_wb["close"].iloc[-1])
        except Exception:
            pass
        _w_mid = _wq.reference_mid(_w_fb)
        _w_spread = _wq.spread_pct if getattr(_wq, "spread_pct", None) is not None else 0.15
        if _wq and getattr(_wq, "is_stale", None) and _wq.is_stale(stale_quote_max_age):
            _w_spread = 0.15
        sell_rot = engine.execution.build_order(
            sym_u,
            "sell",
            sell_qty,
            _w_mid,
            float(_w_spread),
            ignore_spread_gate=bool(getattr(_wq, "skip_spread_check", False)),
            bid=float(_wq.bid),
            ask=float(_wq.ask),
            position_qty=q_live,
        )
        if not sell_rot:
            log_entry_skip(
                dt,
                su_in,
                "cap_pressure_trim: could not build sell for %s" % sym_u,
                verbose=verbose,
                force=False,
            )
            continue
        if per_cycle_exit_ctx is not None and per_cycle_exit_ctx.skip_exit_for_action_cap(
            sym_u, "cap_pressure_partial_trim"
        ):
            continue
        _log_min_hold_debug(
            symbol=sym_u,
            path="cap_pressure_partial_trim",
            row=_w_row,
            dt=dt,
            rep_sub=_rep_dict,
            qty=sell_qty,
            reason="cap_pressure_partial_trim",
        )
        broker.submit_order(sell_rot)
        log_sell(
            str(sym_u).upper(),
            "rebalance_trim",
            {
                "user_id": user_id,
                "channel": "dispatch",
                "path": "cap_pressure_partial_trim",
                "trim_frac": tf,
                "et_date": _sell_log_et_date(dt).isoformat(),
            },
        )
        if live_risk_order_callback is not None:
            live_risk_order_callback(str(sym_u).upper(), "sell", True)
        if per_cycle_exit_ctx is not None:
            per_cycle_exit_ctx.record_exit_action(sym_u)
        if replacement_scan_state is not None:
            replacement_scan_state["count"] = int(replacement_scan_state.get("count", 0)) + 1
        if cycle_risk_state is not None:
            cycle_risk_state["replacements"] = int(cycle_risk_state.get("replacements", 0)) + 1
        q_after = max(0.0, float(q_live) - float(sell_qty))
        if q_after <= 0:
            remove_tracked(sym_u, user_id=user_id, data_dir=data_dir)
        else:
            update_tracked(sym_u, qty=q_after, user_id=user_id, data_dir=data_dir)
        log.info(
            "%s cap_pressure_trim: sold %d / %d sh (%.0f%%) — incoming=%s",
            sym_u,
            sell_qty,
            q_live,
            100.0 * tf,
            su_in,
        )
        did_any = True

    return did_any


def consider_replacement_for_sizing_reject(
    *,
    incoming_sym_upper: str,
    decision: Any,
    tracked: Mapping[str, Any],
    eligible_active: list[str],
    positions: list[dict[str, Any]],
    dt: Any,
    config: dict[str, Any],
    engine: Any,
    broker: Any,
    df: Any,
    atr_pct: Any,
    quote: Any,
    spread_pct_eval: float | None,
    regime_result: Any,
    entry_regime_score_int: int | None,
    rep_sub: dict[str, Any],
    strength_jitter_max: float,
    replace_if_weakest_older_than: int | None,
    max_position_age_bars: int | None,
    allow_equal_replacement: bool,
    replacement_threshold: float,
    incoming_notional_usd: float,
    replacement_scan_state: dict[str, Any] | None,
    user_id: str,
    data_dir: Any,
    current_positions: dict[str, Any],
    log_entry_skip: Callable[..., None],
    verbose: bool,
    cycle_risk_state: dict[str, int] | None,
    stale_quote_max_age: float,
    per_cycle_exit_ctx: LiveExitContext | None = None,
    live_risk_order_callback: Callable[[str, str, bool], None] | None = None,
) -> bool:
    """
    **consider_replacement** when sizing returned zero shares for exposure reasons.

    Runs :func:`evaluate_portfolio_replacement_for_dispatch`; on approval, submits **sells** for the
    chosen laggard line(s) (one or more), updates tracker / ``current_positions`` / ``eligible_active``,
    and returns
    ``True`` so the caller can refresh positions and re-run :meth:`~src.trading_engine.TradingEngine.run_entry_gates`.

    Returns ``False`` if rotation is skipped, not justified, or sell could not be built/submitted.

    Full-line rotation after a sizing reject runs only when ``portfolio_budget_cap_sizing_reject(reason)``
    (book-level ``portfolio caps leave no room``) **or** ``portfolio.enable_replacement`` is true —
    otherwise returns false before evaluating weakest-line sells (cuts churn on non-book cap rejects).
    """
    su = str(incoming_sym_upper).strip().upper()
    _budget_msg = getattr(decision, "reason", None) or getattr(
        getattr(decision, "position_sizing", None), "reject_reason", None
    )
    _pc = config.get("portfolio") if isinstance(config.get("portfolio"), dict) else {}
    _trim_on, _trim_frac, _trim_max_syms = parse_cap_pressure_trim_cfg(_pc)
    if (
        _trim_on
        and portfolio_budget_cap_sizing_reject(_budget_msg)
        and len(eligible_active) > 0
    ):
        if execute_cap_pressure_partial_trim(
            incoming_sym_upper=su,
            eligible_active=eligible_active,
            tracked=tracked,
            positions=positions,
            dt=dt,
            trim_frac=_trim_frac,
            max_symbols=_trim_max_syms,
            broker=broker,
            engine=engine,
            rep_sub=rep_sub if isinstance(rep_sub, dict) else {},
            user_id=user_id,
            data_dir=data_dir,
            log_entry_skip=log_entry_skip,
            verbose=verbose,
            replacement_scan_state=replacement_scan_state,
            cycle_risk_state=cycle_risk_state,
            per_cycle_exit_ctx=per_cycle_exit_ctx,
            live_risk_order_callback=live_risk_order_callback,
            stale_quote_max_age=stale_quote_max_age,
        ):
            return True

    # Full weakest-line rotation: only when book-level cap pressure (gross headroom) or the operator
    # allows replacement for cap-class sizing rejects. Stops churn (e.g. defensive line sold on a
    # per-name "cap" miss that is not "portfolio caps leave no room") when enable_replacement is off.
    _cap_book = portfolio_budget_cap_sizing_reject(_budget_msg)
    _rep_on = bool(_pc.get("enable_replacement", False))
    if not _cap_book and not _rep_on:
        return False

    _weak_l, _skip_rep = evaluate_portfolio_replacement_for_dispatch(
        incoming_sym_upper=su,
        decision=decision,
        tracked=tracked,
        eligible_active=list(eligible_active),
        positions=positions,
        dt=dt,
        config=config,
        engine=engine,
        broker=broker,
        df=df,
        atr_pct=atr_pct,
        quote=quote,
        spread_pct_eval=spread_pct_eval,
        regime_result=regime_result,
        entry_regime_score_int=entry_regime_score_int,
        rep_sub=rep_sub,
        strength_jitter_max=strength_jitter_max,
        replace_if_weakest_older_than=replace_if_weakest_older_than,
        max_position_age_bars=max_position_age_bars,
        allow_equal_replacement=allow_equal_replacement,
        replacement_threshold=float(replacement_threshold or 0.0),
        incoming_notional_usd=float(
            incoming_notional_usd or replacement_min_notional_for_incoming_usd(rep_sub)
        ),
        replacement_scan_state=replacement_scan_state,
    )
    if _skip_rep:
        log_entry_skip(dt, su, _skip_rep, verbose=verbose, force=False)
        return False
    if not _weak_l:
        return False
    _base_inc = (
        float(getattr(getattr(decision, "entry_signal", None), "strength", None) or 1.0)
        if decision is not None
        else 1.0
    )
    _incoming_strength = effective_signal_strength(_base_inc, strength_jitter_max)
    for _weak_r in [str(x).upper() for x in _weak_l]:
        positions[:] = broker.get_positions()
        _tnow = load_tracked(user_id, data_dir=data_dir)
        _wq = broker.get_latest_quote(_weak_r)
        if _wq is None:
            log_entry_skip(
                dt,
                su,
                "replacement (sizing reject): no quote for %s" % _weak_r,
                verbose=verbose,
                force=False,
            )
            return False
        _w_row = _tnow.get(_weak_r) if isinstance(_tnow, dict) else None
        _w_row = _w_row or {}
        sell_qty = int(_w_row.get("qty") or 0)
        if sell_qty <= 0:
            return False

        _rep_dict = rep_sub if isinstance(rep_sub, dict) else {}
        _tiny_mv_floor = replacement_min_market_value_to_replace_usd(_rep_dict)
        if _tiny_mv_floor <= 0:
            _tiny_mv_floor = 750.0
        weakest_market_value_usd = float(
            symbol_long_position_market_value_usd(positions, _weak_r)
        )
        if weakest_market_value_usd < _tiny_mv_floor:
            log_entry_skip(
                dt,
                _weak_r,
                "replacement (sizing reject) skipped — tiny position $%.0f" % weakest_market_value_usd,
                verbose=verbose,
                force=False,
            )
            return False

        _w_fb = float(_w_row.get("entry_price") or 0) or 1.0
        try:
            _wb = broker.get_bars(_weak_r, timeframe="1Day", limit=1)
            if _wb is not None and not _wb.empty:
                _w_fb = float(_wb["close"].iloc[-1])
        except Exception:
            pass
        _w_mid = _wq.reference_mid(_w_fb)
        _w_spread = _wq.spread_pct if getattr(_wq, "spread_pct", None) is not None else 0.15
        if _wq and getattr(_wq, "is_stale", None) and _wq.is_stale(stale_quote_max_age):
            _w_spread = 0.15
        sell_rot = engine.execution.build_order(
            _weak_r,
            "sell",
            sell_qty,
            _w_mid,
            float(_w_spread),
            ignore_spread_gate=bool(getattr(_wq, "skip_spread_check", False)),
            bid=float(_wq.bid),
            ask=float(_wq.ask),
            position_qty=sell_qty,
        )
        if not sell_rot:
            log_entry_skip(
                dt,
                su,
                "replacement (sizing reject): could not build sell for %s" % _weak_r,
                verbose=verbose,
                force=False,
            )
            return False
        if per_cycle_exit_ctx is not None and per_cycle_exit_ctx.skip_exit_for_action_cap(
            _weak_r, "replacement_sizing_reject"
        ):
            return False
        _log_min_hold_debug(
            symbol=_weak_r,
            path="portfolio_replacement_trim",
            row=_w_row,
            dt=dt,
            rep_sub=_rep_dict,
            qty=sell_qty,
            reason="replacement_sizing_reject",
        )
        broker.submit_order(sell_rot)
        log_sell(
            str(_weak_r).upper(),
            "rebalance_trim",
            {
                "user_id": user_id,
                "channel": "dispatch",
                "path": "replacement_sizing_reject",
                "et_date": _sell_log_et_date(dt).isoformat(),
            },
        )
        if live_risk_order_callback is not None:
            live_risk_order_callback(str(_weak_r).upper(), "sell", True)
        if per_cycle_exit_ctx is not None:
            per_cycle_exit_ctx.record_exit_action(_weak_r)
        if replacement_scan_state is not None:
            replacement_scan_state["count"] = int(replacement_scan_state.get("count", 0)) + 1
        if cycle_risk_state is not None:
            cycle_risk_state["replacements"] = int(cycle_risk_state.get("replacements", 0)) + 1
        _weakest_strength = tracked_signal_strength(_w_row)
        print(
            dt.strftime("%H:%M ET"),
            "%s SELL %d sh — replacement after sizing reject (incoming=%s, gap=%.3f)"
            % (_weak_r, sell_qty, su, _incoming_strength - _weakest_strength),
            flush=True,
        )
        remove_tracked(_weak_r, user_id=user_id, data_dir=data_dir)
        current_positions.pop(_weak_r, None)
        if _weak_r in eligible_active:
            eligible_active.remove(_weak_r)
    return True


def dispatch_trend_long_after_buying_power(
    row_tl: Mapping[str, Any],
    *,
    dt: Any,
    broker: Any,
    config: dict[str, Any],
    engine: Any,
    verbose: bool,
    account_equity: float,
    positions: list[dict[str, Any]],
    regime_result: Any,
    bearish_regime: bool,
    pct_above_50d_universe: float | None,
    allowed_symbols_for_stock_orders: frozenset[str] | None,
    max_port_positions: int,
    port_replace: bool,
    port_allow_add: bool,
    eligible_active: list[str],
    strength_jitter_max: float,
    rep_sub: dict[str, Any],
    replace_if_weakest_older_than: int | None,
    current_positions: dict[str, Any],
    user_id: str,
    data_dir: Any,
    option_chain_for_underlying: Callable[..., Any],
    log_entry_skip: Callable[..., None],
    sector_etfs_for_risk: frozenset[str] | None = None,
    cycle_risk_state: dict[str, int] | None = None,
    et_date_iso: str | None = None,
    gross_exposure_pct: float | None = None,
    account_reduce_only: bool = False,
    sector_exposure_pct: dict[str, float] | None = None,
    symbol_sector: dict[str, str] | None = None,
    high_cash_deploy: bool = False,
    replacement_threshold: float = 0.0,
    max_position_age_bars: int | None = None,
    allow_equal_replacement: bool = False,
    replacement_scan_state: dict[str, Any] | None = None,
    per_cycle_exit_ctx: LiveExitContext | None = None,
    live_risk_order_callback: Callable[[str, str, bool], None] | None = None,
) -> bool:
    symbol = row_tl["symbol"]
    sym_u = str(row_tl["sym_u"]).upper()
    decision = row_tl["decision"]
    df = row_tl["df"]
    quote = row_tl["quote"]
    notional = float(row_tl["notional"])
    trend_long_ok = bool(row_tl["trend_long_ok"])
    atr_pct = row_tl.get("atr_pct")
    _entry_regime_score = row_tl.get("entry_regime_score")
    _entry_regime_score_int: int | None = None
    if _entry_regime_score is not None:
        try:
            _entry_regime_score_int = int(float(_entry_regime_score))
        except (TypeError, ValueError):
            _entry_regime_score_int = None

    tracked = load_tracked(user_id, data_dir=data_dir)
    positions[:] = broker.get_positions()
    _sector_risk = sector_etfs_for_risk or sector_etf_symbol_frozenset(config)

    _strength_for_bucket = None
    try:
        if row_tl.get("strength_eff") is not None:
            _strength_for_bucket = float(row_tl["strength_eff"])
    except (TypeError, ValueError):
        _strength_for_bucket = None
    _strength_cohort = row_tl.get("strength_cohort")
    if _strength_cohort is not None and not isinstance(_strength_cohort, (list, tuple)):
        _strength_cohort = None

    if portfolio_brain_enabled(config):
        _pb = portfolio_brain(
            sym_u,
            positions=positions,
            equity=float(account_equity),
            config=config,
            sector_etfs=_sector_risk,
            sector_exposure_pct=sector_exposure_pct,
            symbol_sector=symbol_sector,
            regime_score=regime_result.score if regime_result is not None else None,
            regime_condition=regime_result.condition if regime_result is not None else None,
            entry_strength=_strength_for_bucket,
            strength_cohort=list(_strength_cohort) if _strength_cohort is not None else None,
            high_cash_deploy=high_cash_deploy,
            skip_symbol_allocation_cap_gate=bool(
                row_tl.get("pyramid_skip_symbol_cap", False)
            ),
        )
        if not _pb["allow_new_positions"]:
            log_entry_skip(
                dt,
                sym_u,
                "portfolio_brain: %s" % _pb["reason"],
                verbose=verbose,
                force=True,
            )
            return False
        if not _pb["symbol_allowed"]:
            log_entry_skip(
                dt,
                sym_u,
                "portfolio_brain: %s" % _pb["reason"],
                verbose=verbose,
                force=True,
            )
            return False

    if per_cycle_exit_ctx is not None:
        _btc, _btm = per_cycle_exit_ctx.bulk_trim_buy_cooldown_active(sym_u)
        if _btc:
            log_entry_skip(
                dt,
                sym_u,
                _btm or "bulk trim buy cooldown",
                verbose=verbose,
                force=True,
            )
            return False

    _gate_cfg_for_add = parse_add_on_gate_cfg((config.get("portfolio") or {}))
    _existing_long_for_add = sym_u in current_positions
    if _existing_long_for_add and not port_allow_add:
        _allow_existing_via_signal = False
        _entry_strength = _entry_strength_for_add(row_tl, decision)
        _strong_thr = _strong_add_threshold(config, _gate_cfg_for_add)
        _pos_mv = symbol_long_position_market_value_usd(positions, sym_u)
        _ok_add, _add_scale, _ = add_on_passes_signal_and_scale(
            gate_cfg=_gate_cfg_for_add,
            entry_signal_strength=_entry_strength,
            position_market_value_usd=_pos_mv,
        )
        _allow_existing_via_signal = bool(
            _ok_add
            and _add_scale > 0.0
            and _entry_strength is not None
            and float(_entry_strength) + 1e-12 >= _strong_thr
        )
        if not _allow_existing_via_signal:
            print(f"{symbol} skip — already holding")
            return False
        port_allow_add = True
        notional = _cap_incremental_add_notional(
            notional,
            account_equity=float(account_equity),
            gate_cfg=_gate_cfg_for_add,
        )
        print(
            f"{sym_u} incremental add allowed — strong_signal {float(_entry_strength):.3f} >= {_strong_thr:.3f}; notional ${notional:.0f}",
            flush=True,
        )
    elif _existing_long_for_add and port_allow_add:
        notional = _cap_incremental_add_notional(
            notional,
            account_equity=float(account_equity),
            gate_cfg=_gate_cfg_for_add,
        )

    opts_cfg = config.get("options") or {}
    src_meta = (decision.entry_signal.metadata or {}).get("source") if decision.entry_signal else None
    _alt_kind = row_tl.get("alternate_entry")
    if src_meta == "news_sentiment":
        route_src = "news_override"
    elif _alt_kind or src_meta in ("breakout", "mean_reversion", "volatility"):
        route_src = str(_alt_kind or src_meta or "alternate")
    else:
        route_src = "trend_long"
    if trend_long_ok:
        _path_desc = "trend_mas"
    elif _alt_kind or src_meta in ("breakout", "mean_reversion", "volatility"):
        _path_desc = "alternate_%s" % str(_alt_kind or src_meta or "entry")
    else:
        _path_desc = "news_volume_bypass"
    final_entry_condition = f"route={route_src}; {_path_desc}"
    print(f"{sym_u} FINAL ENTRY → {final_entry_condition}", flush=True)
    _entry_sig = getattr(decision, "entry_signal", None)
    _strength = getattr(_entry_sig, "strength", None) if _entry_sig is not None else None
    _conv_score: float | None = None
    if _strength is not None:
        try:
            _conv_score = float(_strength)
        except (TypeError, ValueError):
            _conv_score = None
    signal_trend = EntryRouteSignal(
        underlying=str(symbol).upper(),
        direction="bullish",
        source=route_src,
        stock_symbol=str(symbol).upper(),
        conviction_score=_conv_score,
    )
    trend_spot = None
    uq_sym = broker.get_latest_quote(str(symbol).upper())
    if uq_sym is not None and getattr(uq_sym, "mid", None):
        try:
            trend_spot = float(uq_sym.mid)
        except (TypeError, ValueError):
            trend_spot = None
    options_handled = False
    options_routing_attempted = False
    _hold_sqqq = any(
        str(p.get("symbol") or "").upper() == "SQQQ" and int(float(p.get("qty") or 0)) > 0 for p in positions
    ) or tracked_row_has_open_long(tracked.get("SQQQ") if isinstance(tracked, dict) else None)
    _opts_base = should_use_options(config, signal_trend, broker=broker)
    _opts_regime_score = regime_result.score if regime_result is not None else None
    _reg_cond = regime_result.condition if regime_result is not None else None
    _opts_gate = trend_long_options_extra_gate_ok(
        config,
        holding_sqqq=_hold_sqqq,
        pct_above_50d=pct_above_50d_universe,
        regime_score=_opts_regime_score,
        bearish_regime=bearish_regime,
        regime_condition=_reg_cond,
        positions=positions,
        tracked=tracked,
    )
    _opt_buy_t = bool(options_allow_new_entries(config))
    _top_sig_ok = trend_long_options_top_signals_only_passes(config, row_tl)
    _opts_no_equity_overlay = not holding_equity_long_for_underlying(
        sym_u,
        positions,
        tracked if isinstance(tracked, dict) else None,
    )
    _opt_env_block, _opt_env_reason = options_entry_environment_blocks(
        config,
        gross_exposure_pct=gross_exposure_pct,
        reduce_only=account_reduce_only,
        regime_score=_opts_regime_score,
    )
    _opt_daily_ok, _opt_daily_reason = option_entry_allowed_by_daily_cap(
        config, str(user_id or "default"), str(et_date_iso or "")
    )
    _opt_cd_ok, _opt_cd_reason = option_entry_cooldown_blocks(
        config, str(user_id or "default"), sym_u, dt
    )
    from src.live.options_chain import options_runtime_enabled

    options_enabled = bool(
        options_runtime_enabled(broker, config)
        and _opt_buy_t
        and _opts_base
        and _opts_gate
        and _top_sig_ok
        and _opts_no_equity_overlay
        and not _opt_env_block
        and _opt_daily_ok
        and _opt_cd_ok
    )
    share_size = 0
    _ps = getattr(decision, "position_sizing", None)
    if _ps is not None:
        try:
            share_size = int(getattr(_ps, "shares", 0) or 0)
        except (TypeError, ValueError):
            share_size = 0

    route_out = None
    chain_trend: list[Any] | None = None
    if options_enabled:
        chain_trend = option_chain_for_underlying(broker, config, sym_u, dt)
        try:
            _as_of = dt.date()
        except Exception:
            _as_of = date.today()
        route_out = route_options_or_shares(
            share_size,
            options_enabled=True,
            config=config,
            signal=signal_trend,
            chain_candidates=chain_trend,
            underlying_spot=trend_spot,
            equity=account_equity,
            positions=positions,
            as_of=_as_of,
            tracked=tracked if isinstance(tracked, dict) else None,
        )
    elif opts_cfg.get("enabled") and _opt_buy_t and _opts_base and not _opts_gate:
        _gr = trend_long_options_extra_gate_reason(
            config,
            holding_sqqq=_hold_sqqq,
            pct_above_50d=pct_above_50d_universe,
            regime_score=_opts_regime_score,
            bearish_regime=bearish_regime,
            regime_condition=_reg_cond,
            positions=positions,
            tracked=tracked,
        )
        if _gr:
            print(dt.strftime("%H:%M ET"), str(symbol).upper(), "skip —", _gr, flush=True)
            return False
    elif (
        opts_cfg.get("enabled")
        and _opt_buy_t
        and _opts_base
        and _opts_gate
        and not _top_sig_ok
    ):
        if verbose:
            print(
                dt.strftime("%H:%M ET"),
                sym_u,
                "options — top_signals_only (not in ranked top-signal batch); stock path",
                flush=True,
            )
    elif (
        opts_cfg.get("enabled")
        and _opt_buy_t
        and _opts_base
        and _opts_gate
        and _top_sig_ok
        and not _opts_no_equity_overlay
    ):
        if verbose:
            print(
                dt.strftime("%H:%M ET"),
                sym_u,
                "options — already hold equity; stock path only",
                flush=True,
            )
    elif opts_cfg.get("enabled") and _opt_buy_t and not _opts_base:
        log_options_stock_path_if_ineligible(config, signal_trend, dt)
    elif (
        opts_cfg.get("enabled")
        and _opt_buy_t
        and _opts_base
        and _opts_gate
        and _top_sig_ok
        and _opts_no_equity_overlay
        and _opt_env_block
    ):
        if verbose and _opt_env_reason:
            print(
                dt.strftime("%H:%M ET"),
                sym_u,
                "options —",
                _opt_env_reason,
                "(stock path)",
                flush=True,
            )
        log_options_gate(
            symbol=sym_u,
            gross_exposure_pct=gross_exposure_pct,
            reduce_only=account_reduce_only,
            final=False,
            reason=_opt_env_reason or "env_block",
        )
    elif (
        opts_cfg.get("enabled")
        and _opt_buy_t
        and _opts_base
        and _opts_gate
        and _top_sig_ok
        and _opts_no_equity_overlay
        and not _opt_env_block
        and not _opt_daily_ok
    ):
        if verbose and _opt_daily_reason:
            print(
                dt.strftime("%H:%M ET"),
                sym_u,
                "options —",
                _opt_daily_reason,
                "(stock path)",
                flush=True,
            )
        log_options_gate(
            symbol=sym_u,
            gross_exposure_pct=gross_exposure_pct,
            reduce_only=account_reduce_only,
            final=False,
            reason=_opt_daily_reason or "daily_cap",
        )
    elif (
        opts_cfg.get("enabled")
        and _opt_buy_t
        and _opts_base
        and _opts_gate
        and _top_sig_ok
        and _opts_no_equity_overlay
        and not _opt_env_block
        and _opt_daily_ok
        and not _opt_cd_ok
    ):
        if verbose and _opt_cd_reason:
            print(
                dt.strftime("%H:%M ET"),
                sym_u,
                "options —",
                _opt_cd_reason,
                "(stock path)",
                flush=True,
            )
        log_options_gate(
            symbol=sym_u,
            gross_exposure_pct=gross_exposure_pct,
            reduce_only=account_reduce_only,
            final=False,
            reason=_opt_cd_reason or "entry_cooldown",
        )

    try:
        _as_of_opts = dt.date()
    except Exception:
        _as_of_opts = date.today()

    _nbb_caps = never_bypass_stock_risk_caps(config)
    _block_options_strict_bucket = False
    if (
        _nbb_caps
        and route_out is not None
        and route_out.leg == "options"
        and route_out.option_contract is not None
    ):
        _oc0 = route_out.option_contract
        _prem0 = float(_oc0.mid) * 100.0
        _buck_opt_ok, _buck_opt_reason = bucket_allocation_allows(
            positions=positions,
            equity=float(account_equity),
            sym_upper=sym_u,
            proposed_notional=_prem0,
            sector_etfs=_sector_risk,
            config=config,
            regime_condition=regime_result.condition if regime_result is not None else None,
            regime_score=regime_result.score if regime_result is not None else None,
            entry_strength=_strength_for_bucket,
            strength_cohort=list(_strength_cohort) if _strength_cohort is not None else None,
            allow_top_signal_bucket_override=False,
            allow_cross_bucket_rebalance_headroom=False,
        )
        if not _buck_opt_ok:
            _block_options_strict_bucket = True
            log_options_gate(
                symbol=sym_u,
                gross_exposure_pct=gross_exposure_pct,
                reduce_only=account_reduce_only,
                spread_pct=float(_oc0.spread_pct),
                dte=max(0, (_oc0.expiration - _as_of_opts).days),
                delta=option_delta_from_chain(_oc0.symbol, chain_trend),
                final=False,
                reason=_buck_opt_reason or "strict_bucket",
            )
            if verbose:
                print(
                    dt.strftime("%H:%M ET"),
                    sym_u,
                    "options — strict stock risk cap (bucket):",
                    _buck_opt_reason or "bucket cap",
                    "(stock path)",
                    flush=True,
                )

    if route_out is not None and route_out.leg == "options":
        options_routing_attempted = True
    if (
        route_out is not None
        and route_out.leg == "options"
        and route_out.option_contract is not None
        and not _block_options_strict_bucket
    ):
        _oc_go = route_out.option_contract
        log_options_gate(
            symbol=sym_u,
            gross_exposure_pct=gross_exposure_pct,
            reduce_only=account_reduce_only,
            spread_pct=float(_oc_go.spread_pct),
            dte=max(0, (_oc_go.expiration - _as_of_opts).days),
            delta=option_delta_from_chain(_oc_go.symbol, chain_trend),
            final=True,
            reason="ok",
        )
        options_handled = route_to_options_executor(
            config,
            signal_trend,
            log_dt=dt,
            verbose=verbose,
            account_equity=account_equity,
            positions=positions,
            broker=broker,
            execution_manager=engine.execution,
            chain_candidates=chain_trend,
            underlying_spot=trend_spot,
            selected_override=route_out.option_contract,
            tracked=tracked if isinstance(tracked, dict) else None,
        )
    # Portfolio full + strong signal: retry options with a tight premium ceiling so a small call
    # can fill when the first routing fell through to equity (replacement/skip otherwise).
    if (
        not options_handled
        and options_enabled
        and not _nbb_caps
        and chain_trend is not None
        and len(chain_trend) > 0
    ):
        from src.options_config import portfolio_full_strong_signal_small_call_cap_usd
        from src.strategy_router import find_option_under_budget

        _pf_try, _pf_cap = portfolio_full_strong_signal_small_call_cap_usd(
            config,
            max_port_positions=max_port_positions,
            n_eligible_long_stocks=len(eligible_active),
            symbol_upper=sym_u,
            current_position_keys=current_positions,
            row_tl=row_tl,
            decision=decision,
            strength_jitter_max=strength_jitter_max,
            account_equity=float(account_equity),
        )
        if _pf_try and _pf_cap is not None and float(_pf_cap) > 0:
            try:
                _as_pf = dt.date()
            except Exception:
                _as_pf = date.today()
            _con_pf, _unused_pf_err = find_option_under_budget(
                config,
                signal_trend,
                chain_candidates=chain_trend,
                underlying_spot=trend_spot,
                equity=float(account_equity),
                positions=positions,
                as_of=_as_pf,
                premium_budget_cap_usd=float(_pf_cap),
                tracked=tracked if isinstance(tracked, dict) else None,
            )
            if _con_pf is not None:
                log_options_gate(
                    symbol=sym_u,
                    gross_exposure_pct=gross_exposure_pct,
                    reduce_only=account_reduce_only,
                    spread_pct=float(_con_pf.spread_pct),
                    dte=max(0, (_con_pf.expiration - _as_pf).days),
                    delta=option_delta_from_chain(_con_pf.symbol, chain_trend),
                    final=True,
                    reason="ok",
                )
                options_routing_attempted = True
                options_handled = route_to_options_executor(
                    config,
                    signal_trend,
                    log_dt=dt,
                    verbose=verbose,
                    account_equity=account_equity,
                    positions=positions,
                    broker=broker,
                    execution_manager=engine.execution,
                    chain_candidates=chain_trend,
                    underlying_spot=trend_spot,
                    selected_override=_con_pf,
                    tracked=tracked if isinstance(tracked, dict) else None,
                )
                if verbose:
                    print(
                        dt.strftime("%H:%M ET"),
                        sym_u,
                        "options — portfolio-full strong signal small call "
                        "(premium cap $%.0f)" % float(_pf_cap),
                        flush=True,
                    )
    if options_handled:
        record_option_entry(str(user_id or "default"), str(et_date_iso or ""))
        record_option_entry_utc(str(user_id or "default"), sym_u, dt)
        return True
    _allow_stock_fallback = use_equity_fallback_after_options(
        config.get("options") if isinstance(config, dict) else None,
        options_routing_attempted=options_routing_attempted,
        options_order_placed=options_handled,
    )
    if options_routing_attempted and not options_handled:
        if not _allow_stock_fallback:
            _log_options_stock_fallback_state(
                sym_u,
                "end",
                reason="stock fallback disabled",
            )
            log_entry_skip(
                dt,
                sym_u,
                "options routing failed and stock fallback is disabled",
                verbose=verbose,
                force=True,
            )
            return False
        emit_options_fallback_to_stock(dt, sym_u, signal=route_src)
        _log_options_stock_fallback_state(
            sym_u,
            "start",
            reason="options routing failed; attempting stock fallback",
        )
    if not options_handled:
        _ok_buck, _buck_reason = bucket_allocation_allows(
            positions=positions,
            equity=float(account_equity),
            sym_upper=sym_u,
            proposed_notional=float(notional),
            sector_etfs=_sector_risk,
            config=config,
            regime_condition=regime_result.condition if regime_result is not None else None,
            regime_score=regime_result.score if regime_result is not None else None,
            entry_strength=_strength_for_bucket,
            strength_cohort=list(_strength_cohort) if _strength_cohort is not None else None,
        )
        if not _ok_buck:
            _log_options_stock_fallback_state(
                sym_u,
                "end",
                reason=_buck_reason or "bucket allocation cap",
            )
            log_entry_skip(
                dt,
                sym_u,
                _buck_reason or "bucket allocation cap",
                verbose=verbose,
                force=True,
            )
            return False
        if allowed_symbols_for_stock_orders is not None and sym_u not in allowed_symbols_for_stock_orders:
            _log_options_stock_fallback_state(
                sym_u,
                "end",
                reason="not in allowed stock universe",
            )
            log_entry_skip(
                dt,
                sym_u,
                "not in allowed stock universe",
                verbose=verbose,
                force=True,
            )
            return False
        _spread_pct_eval = None
        if quote is not None and getattr(quote, "spread_pct", None) is not None:
            try:
                _spread_pct_eval = float(quote.spread_pct)
            except (TypeError, ValueError):
                _spread_pct_eval = None

        _portfolio_full = max_port_positions < 10**9 and len(eligible_active) >= max_port_positions
        _needs_new_equity_slot = sym_u not in current_positions

        _replace_weak_list: list[str] = []
        if _portfolio_full and _needs_new_equity_slot:
            # At cap with a **new** symbol: find laggard(s) to sell, then buy the incoming (see evaluate_*).
            _weak_l, _skip_rep = evaluate_portfolio_replacement_for_dispatch(
                incoming_sym_upper=sym_u,
                decision=decision,
                tracked=tracked,
                eligible_active=list(eligible_active),
                positions=positions,
                dt=dt,
                config=config,
                engine=engine,
                broker=broker,
                df=df,
                atr_pct=atr_pct,
                quote=quote,
                spread_pct_eval=_spread_pct_eval,
                regime_result=regime_result,
                entry_regime_score_int=_entry_regime_score_int,
                rep_sub=rep_sub,
                strength_jitter_max=strength_jitter_max,
                replace_if_weakest_older_than=replace_if_weakest_older_than,
                max_position_age_bars=max_position_age_bars,
                allow_equal_replacement=allow_equal_replacement,
                replacement_threshold=float(replacement_threshold or 0.0),
                incoming_notional_usd=float(notional),
                replacement_scan_state=replacement_scan_state,
            )
            if _skip_rep:
                log_entry_skip(
                    dt,
                    sym_u,
                    _skip_rep,
                    verbose=verbose,
                    force=False,
                )
                return False
            if not _weak_l:
                return False
            _replace_weak_list = [str(x).upper() for x in _weak_l]
        # else: portfolio has room OR add-on to an existing symbol — proceed (add_position path)

        _stock_outcome: list[str] = []

        def _trend_stock_execute() -> None:
            nonlocal tracked
            _rot_partial_buy_req: Any = None
            _partial_notional_for_cp: float | None = None
            if _replace_weak_list:
                for _replace_weak_sym in _replace_weak_list:
                    _rep_dict = rep_sub if isinstance(rep_sub, dict) else {}
                    _rotate_partial = bool(
                        _rep_dict.get("rotate_partial_replacement", False)
                    ) and len(_replace_weak_list) < 2
                    _wq = broker.get_latest_quote(_replace_weak_sym)
                    if _wq is None:
                        print(
                            dt.strftime("%H:%M ET"),
                            sym_u,
                            "skip — replacement: no quote for",
                            _replace_weak_sym,
                            flush=True,
                        )
                        return
                    _w_row = tracked.get(_replace_weak_sym) or {}
                    sell_qty = int(_w_row.get("qty") or 0)
                    if sell_qty <= 0:
                        return
                    _tiny_mv_floor = replacement_min_market_value_to_replace_usd(_rep_dict)
                    if _tiny_mv_floor <= 0:
                        _tiny_mv_floor = 750.0
                    weakest_market_value_usd = float(
                        symbol_long_position_market_value_usd(positions, _replace_weak_sym)
                    )
                    if weakest_market_value_usd < _tiny_mv_floor:
                        log_entry_skip(
                            dt,
                            _replace_weak_sym,
                            f"replacement skipped — tiny position ${weakest_market_value_usd:.0f}",
                            verbose=verbose,
                            force=False,
                        )
                        return
                    _w_qty = sell_qty
                    _sell_legs: list[int]
                    try:
                        _tfr = float(
                            _rep_dict.get("rotate_sell_tranche_fraction", 1.0) or 1.0
                        )
                    except (TypeError, ValueError, OverflowError):
                        _tfr = 1.0
                    _tfr = max(0.0, min(1.0, _tfr))
                    if _tfr < 0.999 and _w_qty > 1:
                        _q1 = trim_qty_for_fraction(_w_qty, _tfr)
                        if _q1 is None or _q1 < 1 or (_w_qty - _q1) < 1:
                            _q1a = max(1, int(_w_qty * _tfr + 0.5))
                            _q1a = min(_q1a, _w_qty - 1)
                            _q1 = _q1a if _q1a >= 1 and (_w_qty - _q1a) >= 1 else None
                        if _q1 is not None and _q1 >= 1 and (_w_qty - _q1) >= 1:
                            if _rotate_partial:
                                _sell_legs = [int(_q1)]
                            else:
                                _sell_legs = [int(_q1), int(_w_qty - int(_q1))]
                        else:
                            _sell_legs = [int(_w_qty)]
                    else:
                        _sell_legs = [int(_w_qty)]
                    _w_fb = float(_w_row.get("entry_price") or 0) or 1.0
                    try:
                        _wb = broker.get_bars(_replace_weak_sym, timeframe="1Day", limit=1)
                        if _wb is not None and not _wb.empty:
                            _w_fb = float(_wb["close"].iloc[-1])
                    except Exception:
                        pass
                    _w_mid = _wq.reference_mid(_w_fb)
                    _w_spread = _wq.spread_pct if getattr(_wq, "spread_pct", None) is not None else 0.15
                    _rem = int(_w_qty)
                    _first_leg = int(_sell_legs[0]) if _sell_legs else 0
                    _freed_cash_max = max(0.0, float(_first_leg) * float(_w_mid))
                    if (
                        _rotate_partial
                        and len(_sell_legs) == 1
                        and _first_leg < _w_qty
                        and _freed_cash_max > 0
                    ):
                        _n_in = min(float(notional), _freed_cash_max)
                        _tr_px = float(df["close"].iloc[-1]) if df is not None and not df.empty else 0.0
                        _b_mid = quote.reference_mid(_tr_px) if quote else _tr_px
                        _b_sp = (
                            float(quote.spread_pct)
                            if quote is not None
                            and getattr(quote, "spread_pct", None) is not None
                            else 0.15
                        )
                        _rot_partial_buy_req = engine.execution.build_order_for_entry(
                            str(symbol).upper(),
                            "buy",
                            0,
                            float(_b_mid),
                            _b_sp,
                            tick_size=0.01,
                            ignore_spread_gate=bool(
                                getattr(quote, "skip_spread_check", False)
                            )
                            if quote is not None
                            else False,
                            bid=float(quote.bid) if quote is not None and getattr(quote, "bid", None) is not None else None,
                            ask=float(quote.ask) if quote is not None and getattr(quote, "ask", None) is not None else None,
                            notional=_n_in,
                        )
                        if not _rot_partial_buy_req or int(
                            getattr(_rot_partial_buy_req, "quantity", 0) or 0
                        ) < 1:
                            log_entry_skip(
                                dt,
                                sym_u,
                                "partial replacement: incoming buy not buildable for "
                                f"freed notional (cap=${_n_in:.0f})",
                                verbose=verbose,
                                force=False,
                            )
                            return
                        _partial_notional_for_cp = float(_n_in)
                    for _i_leg, _leg_qty in enumerate(_sell_legs):
                        if _leg_qty < 1:
                            break
                        sell_rot = engine.execution.build_order(
                            _replace_weak_sym,
                            "sell",
                            _leg_qty,
                            _w_mid,
                            _w_spread,
                            ignore_spread_gate=bool(getattr(_wq, "skip_spread_check", False)),
                            bid=float(_wq.bid),
                            ask=float(_wq.ask),
                            position_qty=int(_rem),
                        )
                        if not sell_rot:
                            print(
                                dt.strftime("%H:%M ET"),
                                sym_u,
                                "skip — replacement: could not build sell",
                                _replace_weak_sym,
                                flush=True,
                            )
                            return
                        if per_cycle_exit_ctx is not None and per_cycle_exit_ctx.skip_exit_for_action_cap(
                            _replace_weak_sym, "portfolio_replacement"
                        ):
                            return
                        _log_min_hold_debug(
                            symbol=_replace_weak_sym,
                            path="portfolio_replacement_trim",
                            row=_w_row,
                            dt=dt,
                            rep_sub=_rep_dict,
                            qty=int(getattr(sell_rot, "quantity", _leg_qty) or _leg_qty),
                            reason="portfolio_replacement",
                        )
                        broker.submit_order(sell_rot)
                        log_sell(
                            str(_replace_weak_sym).upper(),
                            "rebalance_trim",
                            {
                                "user_id": user_id,
                                "channel": "dispatch",
                                "path": "portfolio_replacement",
                                "incoming": sym_u,
                                "et_date": _sell_log_et_date(dt).isoformat(),
                            },
                        )
                        if live_risk_order_callback is not None:
                            live_risk_order_callback(str(_replace_weak_sym).upper(), "sell", True)
                        if per_cycle_exit_ctx is not None:
                            per_cycle_exit_ctx.record_exit_action(_replace_weak_sym)
                        _rem -= int(_leg_qty)
                        if _i_leg + 1 < len(_sell_legs) and _rem > 0:
                            positions[:] = broker.get_positions()
                    if replacement_scan_state is not None:
                        replacement_scan_state["count"] = int(
                            replacement_scan_state.get("count", 0)
                        ) + 1
                    if cycle_risk_state is not None:
                        cycle_risk_state["replacements"] = int(
                            cycle_risk_state.get("replacements", 0)
                        ) + 1
                    weakest_symbol = _replace_weak_sym
                    _base_inc = (
                        float(getattr(decision.entry_signal, "strength", None) or 1.0)
                        if decision and decision.entry_signal
                        else 1.0
                    )
                    _incoming_strength = effective_signal_strength(
                        _base_inc, strength_jitter_max
                    )
                    _weakest_strength = tracked_signal_strength(
                        tracked.get(weakest_symbol) if isinstance(tracked, dict) else None
                    )
                    _sold_sh = sum(int(x) for x in _sell_legs)
                    _rem_w = int(_w_qty) - int(_sold_sh)
                    if _rem_w > 0:
                        update_tracked(
                            str(_replace_weak_sym).upper(),
                            qty=_rem_w,
                            user_id=user_id,
                            data_dir=data_dir,
                        )
                    else:
                        remove_tracked(_replace_weak_sym, user_id=user_id, data_dir=data_dir)
                        current_positions.pop(_replace_weak_sym, None)
                        if _replace_weak_sym in eligible_active:
                            eligible_active.remove(_replace_weak_sym)
                    _pr_tag = (
                        f" PARTIAL (hold {_rem_w} sh)"
                        if _rem_w > 0
                        else f" ({len(_sell_legs)} leg(s))"
                    )
                    print(
                        dt.strftime("%H:%M ET"),
                        f"{weakest_symbol} SELL {_sold_sh} sh{_pr_tag} — portfolio replacement "
                        f"(incoming={sym_u}, gap={_incoming_strength - _weakest_strength:.3f})",
                        flush=True,
                    )
                    positions[:] = broker.get_positions()
                    tracked = load_tracked(user_id, data_dir=data_dir)
            if _rot_partial_buy_req is not None:
                order_t = broker.submit_order(_rot_partial_buy_req)
            else:
                order_t = broker.submit_order(decision.order_request)
            _emit_decision_report(decision)
            if live_risk_order_callback is not None:
                live_risk_order_callback(sym_u, "buy", False)
            if _rot_partial_buy_req is not None:
                qty_bought = int(
                    getattr(_rot_partial_buy_req, "quantity", 0) or 0
                )
            else:
                qty_bought = (
                    decision.position_sizing.shares if decision.position_sizing else 0
                )
            _track_px = (
                float(df["close"].iloc[-1])
                if df is not None and not df.empty
                else 0.0
            )
            entry_price = quote.reference_mid(_track_px) if quote else _track_px
            _resolve_fill = getattr(broker, "resolve_entry_price_from_fill", None)
            if callable(_resolve_fill):
                entry_price = _resolve_fill(order_t, entry_price)
            stop_pct = decision.entry_signal.stop_pct if decision.entry_signal else 1.5
            _st_base = float(decision.entry_signal.strength) if decision.entry_signal else 1.0
            _st_ex = effective_signal_strength(_st_base, strength_jitter_max)
            _tr_post = load_tracked(user_id, data_dir=data_dir)
            _key_sym = str(symbol).upper()
            _existing_qty = int((_tr_post.get(_key_sym) or {}).get("qty") or 0)
            _is_add_on = _existing_qty > 0 and port_allow_add
            if _is_add_on:
                merge_add_tracked(
                    symbol,
                    qty_bought,
                    entry_price,
                    stop_pct=stop_pct,
                    user_id=user_id,
                    data_dir=data_dir,
                    extras={"signal_strength": _st_ex},
                    et_trading_date=et_date_iso,
                )
                _stock_outcome.append("add")
            else:
                add_tracked(
                    symbol,
                    qty_bought,
                    entry_price,
                    stop_pct,
                    user_id=user_id,
                    data_dir=data_dir,
                    extras={"signal_strength": _st_ex},
                )
                _stock_outcome.append("new")
            src = (decision.entry_signal.metadata or {}).get("source") if decision.entry_signal else None
            _buy_word = "ADD" if _is_add_on else "BUY"
            if src == "news_sentiment":
                print(
                    dt.strftime("%H:%M ET"),
                    symbol,
                    _buy_word,
                    qty_bought,
                    "shares (news+vol spike)",
                    getattr(order_t, "id", ""),
                )
            else:
                print(
                    dt.strftime("%H:%M ET"),
                    symbol,
                    _buy_word,
                    qty_bought,
                    "shares",
                    getattr(order_t, "id", ""),
                )
            _prev_cp = current_positions.get(_key_sym, {})
            _prev_not = float(_prev_cp.get("notional", 0) or 0)
            _n_book = (
                float(_partial_notional_for_cp)
                if _partial_notional_for_cp is not None
                else float(notional)
            )
            current_positions[_key_sym] = {
                "notional": _prev_not + _n_book,
                "stop_pct": stop_pct,
            }

        route_to_stock_executor(signal_trend, _trend_stock_execute)
        if options_routing_attempted and not options_handled:
            _log_options_stock_fallback_state(
                sym_u,
                "end",
                reason="stock fallback order placed" if _stock_outcome else "stock fallback did not place order",
            )
        if (
            cycle_risk_state is not None
            and _stock_outcome
            and _stock_outcome[-1] == "new"
        ):
            cycle_risk_state["new_stock"] = int(cycle_risk_state.get("new_stock", 0)) + 1
        return len(_stock_outcome) > 0
    return False
