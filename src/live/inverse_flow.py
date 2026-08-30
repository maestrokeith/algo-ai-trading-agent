"""Bear ETF / SQQQ inverse entry path and controlled scaling (live loop)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.bear_etf_gates import daily_fresh_cross_below_ma
from src.entry_router import (
    EntryRouteSignal,
    log_options_stock_path_if_ineligible,
    route_to_options_executor,
    route_to_stock_executor,
    should_use_options,
    use_equity_fallback_after_options,
)
from src.entry_decision_log import emit_options_fallback_to_stock
from src.strategies.entries.trend_long_signal import (
    _log_options_stock_fallback_state,
)
from src.exposure import ETF_SYMBOLS, INVERSE_ETFS, SYMBOL_SECTOR, THEME_MAP
from src.inverse_reentry import (
    check_sqqq_stock_reentry_allowed,
    record_sqqq_initial_stock_entry,
)
from src.live.loop_log import log_entry_skip, log_inverse_state_line, quote_skip_spread_check
from src.live.options_chain import option_chain_for_underlying
from src.options_config import allow_new_entries as options_allow_new_entries
from src.portfolio_allocation import scaled_buying_power_for_lane
from src.position_tracker import (
    add as add_tracked,
    get_tracked_entry_info,
    load as load_tracked,
    merge_add_shares as merge_add_tracked,
    minutes_since_iso,
)
from src.regime_entry_policy import policy_blocks_sqqq_entry, severe_breakdown_ok
from src.sqqq_entry_gates import score_2_skip_fresh_cross_below_ma, score_2_sqqq_optional_filters_pass
from src.trading_engine import TradingEngine
from src.universe import last_bar_volume_from_ohlcv


@dataclass
class BearInverseContext:
    now: Any
    verbose: bool
    broker: Any
    engine: TradingEngine
    config: dict[str, Any]
    user_id: str
    data_dir: Path
    account_equity: float
    exposure_snapshot: Any
    allowed_symbols_for_stock_orders: frozenset[str] | set[str] | None
    open_order_symbols: set[str]
    available_cash: float
    stale_quote_max_age: float
    regime_entry_policy: Any
    regime_result: Any
    bear_inv_regime_mult: float | None
    bearish_regime: bool
    # Gross long MV / equity > ``portfolio.exposure_gates.overexposed_reduce_only_gross_frac`` (default 1.0)
    reduce_only: bool = False


def run_bear_inverse_flow(
    ctx: BearInverseContext,
    positions: list[dict[str, Any]],
    tracked: dict[str, Any],
    current_positions: dict[str, Any],
) -> set[str]:
    if ctx.reduce_only:
        print(
            ctx.now.strftime("%H:%M ET"),
            "— over_exposed_mode: reduce_only — skip new bear / inverse longs (gross %.1f%% of equity)"
            % (float(ctx.exposure_snapshot.gross_pct),),
            flush=True,
        )
        return set()

    # Bear ETFs: long only when bearish + breakdown (e.g. QQQ below 50D MA)
    bear_etfs_cfg = ctx.config.get("universe", {}).get("bear_etfs", {})
    bear_etf_all_raw = list(bear_etfs_cfg.get("symbols") or [])
    bear_etf_universe_set = {str(s).upper() for s in bear_etf_all_raw}
    bear_etf_symbols = list(bear_etf_all_raw)
    if not ctx.bearish_regime and "SQQQ" in bear_etf_universe_set:
        log_entry_skip(
            ctx.now,
            "SQQQ",
            "not bearish regime (breadth filter)",
            verbose=ctx.verbose,
            force=True,
        )
    breakdown_cfg = bear_etfs_cfg.get("breakdown", {}) or {}
    ref_symbol = breakdown_cfg.get("reference_symbol") or "QQQ"
    breakdown_ma_period = int(breakdown_cfg.get("ma_period") or 50)
    require_fresh_cross_below_ma = bool(
        breakdown_cfg.get("require_fresh_cross_below_ma", False)
    )
    prefer_sqqq_qqq = bool(bear_etfs_cfg.get("prefer_sqqq_when_breakdown_reference_is_qqq", True))
    if prefer_sqqq_qqq and str(ref_symbol).upper() == "QQQ":
        sqqq_only = [s for s in bear_etf_symbols if str(s).upper() == "SQQQ"]
        if sqqq_only:
            bear_etf_symbols = sqqq_only
        elif ctx.verbose:
            print(ctx.now.strftime("%H:%M ET"), "— bear ETFs: QQQ breakdown but SQQQ not in bear_etfs.symbols; using full list")
    breakdown_detected = False
    ref_close: float | None = None
    ref_ma: float | None = None
    ref_fresh_cross: bool | None = None
    if ctx.bearish_regime and bear_etf_symbols and ref_symbol:
        try:
            ref_bars = ctx.broker.get_bars(ref_symbol, timeframe="1Day", limit=breakdown_ma_period + 10)
            if not ref_bars.empty and len(ref_bars) >= breakdown_ma_period:
                ref_close = float(ref_bars["close"].iloc[-1])
                ref_ma = float(ref_bars["close"].rolling(breakdown_ma_period).mean().iloc[-1])
                ref_fresh_cross = daily_fresh_cross_below_ma(ref_bars, breakdown_ma_period)
                below_ma = ref_close < ref_ma
                if below_ma and (not require_fresh_cross_below_ma or ref_fresh_cross is True):
                    breakdown_detected = True
                    if ctx.verbose:
                        _fx = "fresh cross" if ref_fresh_cross is True else "below MA"
                        print(
                            ctx.now.strftime("%H:%M ET"),
                            "— breakdown: %s %s %dD MA (%.2f vs %.2f)"
                            % (ref_symbol, _fx, breakdown_ma_period, ref_close, ref_ma),
                        )
                elif below_ma and require_fresh_cross_below_ma and ctx.verbose:
                    print(
                        ctx.now.strftime("%H:%M ET"),
                        "— breakdown: %s below %dD MA but no fresh cross (prev bar not at/above MA)"
                        % (ref_symbol, breakdown_ma_period),
                    )
        except Exception as e:
            if ctx.verbose:
                print(ctx.now.strftime("%H:%M ET"), "— breakdown check skip:", type(e).__name__, str(e)[:40])
    
    # SQQQ entry signal: QQQ close < QQQ 50D MA (always QQQ/50 for SQQQ; independent of breakdown.reference_symbol)
    qqq_below_ma50 = False
    qqq_price: float | None = None
    qqq_ma50: float | None = None
    qqq_fresh_cross_ma50: bool | None = None
    if ctx.bearish_regime:
        try:
            if (
                str(ref_symbol).upper() == "QQQ"
                and breakdown_ma_period == 50
                and ref_close is not None
                and ref_ma is not None
            ):
                qqq_price, qqq_ma50 = ref_close, ref_ma
                qqq_fresh_cross_ma50 = ref_fresh_cross
            else:
                qb = ctx.broker.get_bars("QQQ", timeframe="1Day", limit=60)
                if not qb.empty and len(qb) >= 50:
                    qqq_price = float(qb["close"].iloc[-1])
                    qqq_ma50 = float(qb["close"].rolling(50).mean().iloc[-1])
                    qqq_fresh_cross_ma50 = daily_fresh_cross_below_ma(qb, 50)
            if qqq_price is not None and qqq_ma50 is not None:
                qqq_below_ma50 = qqq_price < qqq_ma50
                if qqq_below_ma50 and ctx.verbose:
                    print(
                        ctx.now.strftime("%H:%M ET"),
                        "— SQQQ gate: QQQ %.2f < 50D MA %.2f" % (qqq_price, qqq_ma50),
                    )
        except Exception as e:
            if ctx.verbose:
                print(ctx.now.strftime("%H:%M ET"), "— QQQ MA50 check skip:", type(e).__name__, str(e)[:40])
    
    if ctx.bearish_regime and bear_etf_symbols:
        # Inverse entries: SQQQ when QQQ < MA50; other bear symbols use configured breakdown ref/MA
        max_bear_etf_positions = int(bear_etfs_cfg.get("max_positions") or 2)
        bear_etf_stop_pct = float(bear_etfs_cfg.get("stop_pct") or 2.0)
        max_bear_etf_pct = float(bear_etfs_cfg.get("max_exposure_pct_equity") or 10)
        max_bear_etf_notional = ctx.account_equity * (max_bear_etf_pct / 100.0)
        current_bear_etf_notional = sum(
            abs(float(p.get("market_value") or 0))
            for p in positions
            if str(p.get("symbol") or "").upper() in bear_etf_universe_set
        )
        tracked_upper = {str(k).upper() for k in tracked}
        pos_bear_syms = {str(p.get("symbol") or "").upper() for p in positions}
        current_bear_etf = len(
            {s for s in bear_etf_universe_set if s in pos_bear_syms or s in tracked_upper}
        )
        for symbol in bear_etf_symbols:
            sym_u = str(symbol).upper()
            if current_bear_etf >= max_bear_etf_positions:
                log_entry_skip(
                    ctx.now,
                    sym_u,
                    "max inverse ETF positions (%d)" % max_bear_etf_positions,
                    verbose=ctx.verbose,
                    force=True,
                )
                break
            if sym_u == "SQQQ":
                _ep_mr = (ctx.config.get("market_regime") or {}).get("entry_policy") or {}
                _severe_ok_sqqq = severe_breakdown_ok(
                    qqq_price=qqq_price,
                    qqq_ma50=qqq_ma50,
                    min_pct_below_ma=float(
                        _ep_mr.get("severe_breakdown_min_pct_below_ma", 1.0)
                    ),
                    require_fresh_cross=bool(
                        _ep_mr.get("severe_breakdown_require_fresh_cross", True)
                    ),
                    qqq_fresh_cross_ma50=qqq_fresh_cross_ma50,
                )
                _pol_block, _pol_reason = policy_blocks_sqqq_entry(
                    ctx.regime_entry_policy,
                    severe_ok=_severe_ok_sqqq,
                )
                if _pol_block:
                    log_entry_skip(
                        ctx.now,
                        sym_u,
                        _pol_reason or "SQQQ blocked by regime entry policy",
                        verbose=ctx.verbose,
                        force=True,
                    )
                    continue
                if not qqq_below_ma50:
                    if qqq_price is None or qqq_ma50 is None:
                        log_entry_skip(
                            ctx.now,
                            sym_u,
                            "QQQ vs 50D MA unavailable (insufficient data or API)",
                            verbose=ctx.verbose,
                            force=True,
                        )
                    else:
                        log_entry_skip(
                            ctx.now,
                            sym_u,
                            "QQQ not below 50D MA (QQQ %.2f >= MA %.2f)" % (qqq_price, qqq_ma50),
                            verbose=ctx.verbose,
                            force=True,
                        )
                    continue
                if require_fresh_cross_below_ma and not score_2_skip_fresh_cross_below_ma(
                    ctx.config, ctx.regime_entry_policy.score
                ):
                    if qqq_fresh_cross_ma50 is not True:
                        log_entry_skip(
                            ctx.now,
                            sym_u,
                            "no fresh QQQ cross below 50D MA (need prior close >= MA, current < MA)"
                            if qqq_fresh_cross_ma50 is False
                            else "fresh cross below MA unavailable (insufficient QQQ history)",
                            verbose=ctx.verbose,
                            force=True,
                        )
                        continue
                if ctx.regime_entry_policy.score == 2:
                    _qqq_last_ex = qqq_price
                    try:
                        _lq_ex = ctx.broker.get_latest_quote("QQQ")
                        if _lq_ex is not None and getattr(_lq_ex, "mid", None):
                            _qqq_last_ex = float(_lq_ex.mid)
                    except Exception:
                        pass
                    _s2_ok, _s2_msg = score_2_sqqq_optional_filters_pass(
                        ctx.config,
                        ctx.broker,
                        now_et=ctx.now,
                        qqq_last=_qqq_last_ex,
                    )
                    if not _s2_ok:
                        log_entry_skip(
                            ctx.now,
                            sym_u,
                            _s2_msg or "score 2 SQQQ optional filters",
                            verbose=ctx.verbose,
                            force=True,
                        )
                        continue
                    if _s2_msg:
                        print(
                            ctx.now.strftime("%H:%M ET"),
                            sym_u,
                            "score 2 filter OK —",
                            _s2_msg,
                            flush=True,
                        )
            elif not breakdown_detected:
                log_entry_skip(
                    ctx.now,
                    sym_u,
                    "no breakdown (%s not below %dD MA)" % (ref_symbol, breakdown_ma_period),
                    verbose=ctx.verbose,
                    force=True,
                )
                continue
            try:
                # Bearish options (e.g. QQQ puts) run before stock blockers so we can add hedges
                # while already long SQQQ; stock buy stays blocked below when already held/tracked.
                opts_cfg = ctx.config.get("options") or {}
                und = "QQQ" if sym_u == "SQQQ" else ("SPY" if sym_u == "SPXS" else sym_u)
                signal_bear = EntryRouteSignal(
                    underlying=und,
                    direction="bearish",
                    source="bear_etf",
                    stock_symbol=sym_u,
                )
                u_spot = None
                uq_und = ctx.broker.get_latest_quote(und)
                if uq_und is not None and getattr(uq_und, "mid", None):
                    try:
                        u_spot = float(uq_und.mid)
                    except (TypeError, ValueError):
                        u_spot = None
                options_handled = False
                options_routing_attempted = False
                _opt_buy = bool(options_allow_new_entries(ctx.config))
                if (
                    opts_cfg.get("enabled")
                    and _opt_buy
                    and should_use_options(ctx.config, signal_bear, broker=ctx.broker)
                ):
                    chain_bear = option_chain_for_underlying(ctx.broker, ctx.config, und, ctx.now)
                    options_routing_attempted = True
                    options_handled = route_to_options_executor(
                        ctx.config,
                        signal_bear,
                        log_dt=ctx.now,
                        verbose=ctx.verbose,
                        account_equity=ctx.account_equity,
                        positions=positions,
                        broker=ctx.broker,
                        execution_manager=ctx.engine.execution,
                        chain_candidates=chain_bear,
                        underlying_spot=u_spot,
                        tracked=tracked,
                    )
                elif opts_cfg.get("enabled") and _opt_buy:
                    log_options_stock_path_if_ineligible(ctx.config, signal_bear, ctx.now)
                if options_handled:
                    continue
                _allow_stock_fallback = use_equity_fallback_after_options(
                    ctx.config.get("options") if isinstance(ctx.config, dict) else None,
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
                            ctx.now,
                            sym_u,
                            "options routing failed and stock fallback is disabled",
                            verbose=ctx.verbose,
                            force=True,
                        )
                        continue
                    emit_options_fallback_to_stock(
                        ctx.now,
                        sym_u,
                        signal="bear_etf",
                    )
                    _log_options_stock_fallback_state(
                        sym_u,
                        "start",
                        reason="options routing failed; attempting stock fallback",
                    )

                reasons_block = []
                if sym_u in current_positions:
                    reasons_block.append("already in positions")
                if sym_u in ctx.open_order_symbols:
                    reasons_block.append("open buy/sell order")
                if sym_u in tracked:
                    reasons_block.append("in tracked state")
                if reasons_block:
                    _log_options_stock_fallback_state(
                        sym_u,
                        "end",
                        reason="; ".join(reasons_block),
                    )
                    log_entry_skip(
                        ctx.now,
                        sym_u,
                        "; ".join(reasons_block),
                        verbose=ctx.verbose,
                        force=True,
                    )
                    continue
    
                if sym_u == "SQQQ" and (bear_etfs_cfg.get("sqqq_reentry") or {}).get("enabled", False):
                    _sq_ok, _sq_reason = check_sqqq_stock_reentry_allowed(
                        bear_etfs_cfg,
                        ctx.user_id,
                        ctx.data_dir,
                        ctx.now,
                        qqq_price,
                        et_date_str=ctx.now.strftime("%Y-%m-%d"),
                    )
                    if not _sq_ok:
                        log_entry_skip(
                            ctx.now,
                            sym_u,
                            _sq_reason or "SQQQ re-entry blocked",
                            verbose=ctx.verbose,
                            force=True,
                        )
                        continue
    
                df = ctx.broker.get_bars(symbol, timeframe="1Day", limit=60)
                if df.empty or len(df) < 20:
                    log_entry_skip(
                        ctx.now,
                        sym_u,
                        "not enough daily bars (need 20, got %d)" % (0 if df.empty else len(df)),
                        verbose=ctx.verbose,
                        force=True,
                    )
                    continue
                close = float(df["close"].iloc[-1])
                quote = ctx.broker.get_latest_quote(symbol)
                _skip_nbbo_spread_b = quote_skip_spread_check(quote)
                if quote and getattr(quote, "is_stale", None) and quote.is_stale(ctx.stale_quote_max_age):
                    spread_pct = 0.15
                else:
                    spread_pct = quote.spread_pct if quote else 0.15
                spread_cap = ctx.engine.market_quality._max_spread_for_symbol(symbol)
                _ignore_spread_lv = ctx.engine.market_quality.should_ignore_spread_for_low_volume(
                    last_bar_volume_from_ohlcv(df)
                )
                if (
                    not _skip_nbbo_spread_b
                    and spread_pct is not None
                    and spread_pct > spread_cap
                    and not _ignore_spread_lv
                ):
                    log_entry_skip(
                        ctx.now,
                        sym_u,
                        "spread %.3f%% > cap %.3f%%" % (spread_pct, spread_cap),
                        verbose=ctx.verbose,
                        force=True,
                    )
                    continue
                _bp_probe = ctx.engine.execution.min_buying_power_for_equity_entry_probe(close)
                if _bp_probe > ctx.available_cash:
                    log_entry_skip(
                        ctx.now,
                        sym_u,
                        "insufficient buying power (min ~$%.2f > $%.2f available)"
                        % (_bp_probe, ctx.available_cash),
                        verbose=ctx.verbose,
                        force=True,
                    )
                    continue
                _bear_reg_sizer = (
                    ctx.bear_inv_regime_mult if ctx.bear_inv_regime_mult is not None else 1.0
                )
                if sym_u == "SQQQ":
                    _bear_reg_sizer *= float(ctx.regime_entry_policy.sqqq_notional_fraction)
                sizing = ctx.engine.sizer.size_position(
                    ctx.account_equity,
                    close,
                    bear_etf_stop_pct,
                    symbol,
                    current_positions,
                    ctx.exposure_snapshot.sector_pct,
                    symbol_sector=SYMBOL_SECTOR,
                    regime_size_multiplier=_bear_reg_sizer,
                    regime_score=ctx.regime_result.score if ctx.regime_result is not None else None,
                    regime_condition=(
                        ctx.regime_result.condition
                        if ctx.regime_result is not None
                        else None
                    ),
                    conviction="medium",
                    strategy_winrate=0.50,
                    current_gross_exposure_pct=ctx.exposure_snapshot.gross_pct,
                    current_net_exposure_pct=ctx.exposure_snapshot.net_pct,
                    current_theme_exposure_pct=ctx.exposure_snapshot.theme_pct.get(
                        THEME_MAP.get(sym_u, sym_u), 0.0
                    ),
                    is_etf=sym_u in ETF_SYMBOLS,
                    is_inverse_etf=sym_u in INVERSE_ETFS,
                    ohlcv_df=df,
                    theme_key=THEME_MAP.get(sym_u, sym_u),
                )
                if not sizing or sizing.shares <= 0:
                    rr = getattr(sizing, "reject_reason", None) if sizing else None
                    log_entry_skip(
                        ctx.now,
                        sym_u,
                        "position size rejected (%s)" % (rr or "zero shares"),
                        verbose=ctx.verbose,
                        force=True,
                    )
                    continue
                if current_bear_etf_notional + sizing.notional > max_bear_etf_notional:
                    log_entry_skip(
                        ctx.now,
                        sym_u,
                        "inverse ETF exposure cap (%.0f%% equity ≈ $%.0f max, current $%.0f + new $%.0f)"
                        % (
                            max_bear_etf_pct,
                            max_bear_etf_notional,
                            current_bear_etf_notional,
                            sizing.notional,
                        ),
                        verbose=ctx.verbose,
                        force=True,
                    )
                    continue
                if (
                    ctx.allowed_symbols_for_stock_orders is not None
                    and sym_u not in ctx.allowed_symbols_for_stock_orders
                ):
                    log_entry_skip(
                        ctx.now,
                        sym_u,
                        "not in allowed stock universe",
                        verbose=ctx.verbose,
                        force=True,
                    )
                    continue
                buy_order = ctx.engine.execution.build_order_for_entry(
                    symbol,
                    "buy",
                    sizing.shares,
                    quote.reference_mid(close) if quote else close,
                    spread_pct or 0.15,
                    ignore_spread_gate=_ignore_spread_lv or _skip_nbbo_spread_b,
                    bid=float(quote.bid) if quote else None,
                    ask=float(quote.ask) if quote else None,
                )
                if not buy_order:
                    log_entry_skip(ctx.now, sym_u, "execution could not build order", verbose=ctx.verbose, force=True)
                    continue
    
                def _bear_stock_execute() -> None:
                    _ord_bear = ctx.broker.submit_order(buy_order)
                    _entry_px_bear = ctx.broker.resolve_entry_price_from_fill(_ord_bear, float(close))
                    current_bear_etf_notional += sizing.notional
                    _pyramid_extras = None
                    _cs_en_b = bool(
                        (bear_etfs_cfg.get("controlled_scaling") or {}).get("enabled", False)
                    )
                    _csym_b = str(
                        (bear_etfs_cfg.get("controlled_scaling") or {}).get("symbol") or "SQQQ"
                    ).upper()
                    if _cs_en_b and sym_u == _csym_b:
                        _pyramid_extras = {
                            "scale_count": 1,
                            "last_entry_price": float(close),
                        }
                    add_tracked(
                        symbol,
                        sizing.shares,
                        _entry_px_bear,
                        bear_etf_stop_pct,
                        side="long",
                        user_id=ctx.user_id,
                        data_dir=ctx.data_dir,
                        extras=_pyramid_extras,
                    )
                    if sym_u == "SQQQ" and (bear_etfs_cfg.get("sqqq_reentry") or {}).get("enabled", False):
                        record_sqqq_initial_stock_entry(
                            ctx.user_id,
                            ctx.data_dir,
                            ctx.now.strftime("%Y-%m-%d"),
                        )
                    current_bear_etf += 1
                    current_positions[sym_u] = {"notional": sizing.notional, "stop_pct": bear_etf_stop_pct}
                    if _cs_en_b and sym_u == _csym_b:
                        _cs_steps = list(
                            (bear_etfs_cfg.get("controlled_scaling") or {}).get("steps") or []
                        )
                        _fc = float(_entry_px_bear)
                        log_inverse_state_line(
                            ctx.now,
                            sym_u,
                            shares=int(sizing.shares),
                            scale_count=1,
                            num_scale_steps=len(_cs_steps),
                            avg_entry=_fc,
                            last_entry=_fc,
                            unrealized_pnl=None,
                        )
                    else:
                        label_local = "QQQ < MA50" if sym_u == "SQQQ" else "breakdown"
                        print(
                            ctx.now.strftime("%H:%M ET"),
                            symbol,
                            "BUY (bear ETF, %s)" % label_local,
                            sizing.shares,
                            "shares",
                        )
    
                route_to_stock_executor(signal_bear, _bear_stock_execute)
            except Exception as e:
                log_entry_skip(
                    ctx.now,
                    sym_u,
                    "%s: %s" % (type(e).__name__, str(e)[:80]),
                    verbose=ctx.verbose,
                    force=True,
                )
                continue
    
        # SQQQ controlled scaling (QQQ vs 50D MA only; step index + scale_count)
        scaling_cfg = bear_etfs_cfg.get("controlled_scaling") or {}
        sqqq_sym = str(scaling_cfg.get("symbol") or "SQQQ").upper()
    
        if bool(scaling_cfg.get("enabled", False)) and sqqq_sym in bear_etf_universe_set:
            ref_sym_cfg = str(scaling_cfg.get("reference_symbol") or "QQQ").upper()
            ref_ma_cfg = int(scaling_cfg.get("ma_period") or 50)
            cooldown_minutes = int(scaling_cfg.get("cooldown_minutes") or 15)
            require_price_above_last_entry = bool(
                scaling_cfg.get("require_price_above_last_entry", True)
            )
            steps = list(scaling_cfg.get("steps") or [])
    
            if (
                qqq_price is None
                or qqq_ma50 is None
                or qqq_ma50 <= 0
                or ref_sym_cfg != "QQQ"
                or ref_ma_cfg != 50
            ):
                log_entry_skip(
                    ctx.now,
                    sqqq_sym,
                    "scaling skipped — QQQ/50D reference unavailable",
                    verbose=ctx.verbose,
                    force=True,
                )
            else:
                dist_pct = (qqq_ma50 - qqq_price) / qqq_ma50 * 100.0
                active_step_idx = -1
                for i, step in enumerate(steps):
                    if not isinstance(step, dict):
                        continue
                    trig = float(step.get("reference_ma_distance_pct_min") or 0.0)
                    if dist_pct >= trig:
                        active_step_idx = i
    
                tracked_row = get_tracked_entry_info(
                    ctx.data_dir / f"positions_{ctx.user_id}.json", sqqq_sym
                )
                holds_sqqq = any(
                    str(p.get("symbol") or "").upper() == sqqq_sym
                    and abs(int(float(p.get("qty") or 0))) > 0
                    for p in positions
                )
                # Pyramid state lives in the persisted tracker for the full open position,
                # not in the in-memory dict for this loop tick.
                scale_count = (
                    int(tracked_row.get("scale_count") or 1) if holds_sqqq else 0
                )
                last_entry_price = tracked_row.get("last_entry_price")
                last_scale_ts = tracked_row.get("last_scale_ts")
    
                mins_since_last = minutes_since_iso(
                    str(last_scale_ts) if last_scale_ts else None, ctx.now
                )
                cooldown_ok = mins_since_last is None or mins_since_last >= cooldown_minutes
    
                if not holds_sqqq:
                    log_entry_skip(
                        ctx.now,
                        sqqq_sym,
                        "scaling skipped — no existing SQQQ position",
                        verbose=ctx.verbose,
                        force=True,
                    )
                elif active_step_idx < 0:
                    log_entry_skip(
                        ctx.now,
                        sqqq_sym,
                        "scaling skipped — QQQ only %.2f%% below MA50" % dist_pct,
                        verbose=ctx.verbose,
                        force=True,
                    )
                elif scale_count >= len(steps):
                    log_entry_skip(
                        ctx.now,
                        sqqq_sym,
                        "scaling skipped — already at max scale steps (%d)" % len(steps),
                        verbose=ctx.verbose,
                        force=True,
                    )
                elif scale_count > active_step_idx:
                    log_entry_skip(
                        ctx.now,
                        sqqq_sym,
                        "scaling skipped — current scale_count %d already matches trend step %d"
                        % (scale_count, active_step_idx + 1),
                        verbose=ctx.verbose,
                        force=True,
                    )
                elif sqqq_sym in ctx.open_order_symbols:
                    log_entry_skip(
                        ctx.now,
                        sqqq_sym,
                        "scaling skipped — open order pending",
                        verbose=ctx.verbose,
                        force=True,
                    )
                elif current_bear_etf_notional >= max_bear_etf_notional - 1e-6:
                    log_entry_skip(
                        ctx.now,
                        sqqq_sym,
                        "scaling skipped — inverse exposure at cap (~$%.0f)"
                        % max_bear_etf_notional,
                        verbose=ctx.verbose,
                        force=True,
                    )
                elif not cooldown_ok:
                    log_entry_skip(
                        ctx.now,
                        sqqq_sym,
                        "scaling skipped — cooldown %.1f/%d min"
                        % (mins_since_last or 0.0, cooldown_minutes),
                        verbose=ctx.verbose,
                        force=True,
                    )
                else:
                    try:
                        df_s = ctx.broker.get_bars(sqqq_sym, timeframe="1Day", limit=60)
                        if df_s.empty or len(df_s) < 20:
                            log_entry_skip(
                                ctx.now,
                                sqqq_sym,
                                "scaling skipped — not enough daily bars",
                                verbose=ctx.verbose,
                                force=True,
                            )
                        else:
                            close_s = float(df_s["close"].iloc[-1])
    
                            if (
                                require_price_above_last_entry
                                and last_entry_price is not None
                                and close_s <= float(last_entry_price)
                            ):
                                log_entry_skip(
                                    ctx.now,
                                    sqqq_sym,
                                    "scaling skipped — SQQQ %.2f <= last entry %.2f"
                                    % (close_s, float(last_entry_price)),
                                    verbose=ctx.verbose,
                                    force=True,
                                )
                            else:
                                quote_s = ctx.broker.get_latest_quote(sqqq_sym)
                                _skip_nbbo_spread_s = quote_skip_spread_check(quote_s)
                                if quote_s and getattr(quote_s, "is_stale", None) and quote_s.is_stale(ctx.stale_quote_max_age):
                                    spread_pct_s = 0.15
                                else:
                                    spread_pct_s = quote_s.spread_pct if quote_s else 0.15
    
                                spread_cap_s = ctx.engine.market_quality._max_spread_for_symbol(sqqq_sym)
                                _ignore_spread_lv_s = ctx.engine.market_quality.should_ignore_spread_for_low_volume(
                                    last_bar_volume_from_ohlcv(df_s)
                                )
                                if (
                                    not _skip_nbbo_spread_s
                                    and spread_pct_s is not None
                                    and spread_pct_s > spread_cap_s
                                    and not _ignore_spread_lv_s
                                ):
                                    log_entry_skip(
                                        ctx.now,
                                        sqqq_sym,
                                        "scaling skipped — spread %.3f%% > cap %.3f%%"
                                        % (spread_pct_s, spread_cap_s),
                                        verbose=ctx.verbose,
                                        force=True,
                                    )
                                else:
                                    sizing_s = ctx.engine.sizer.size_position(
                                        ctx.account_equity,
                                        close_s,
                                        bear_etf_stop_pct,
                                        sqqq_sym,
                                        current_positions,
                                        ctx.exposure_snapshot.sector_pct,
                                        symbol_sector=SYMBOL_SECTOR,
                                        regime_size_multiplier=ctx.bear_inv_regime_mult,
                                        regime_score=ctx.regime_result.score if ctx.regime_result is not None else None,
                                        regime_condition=(
                                            ctx.regime_result.condition
                                            if ctx.regime_result is not None
                                            else None
                                        ),
                                        conviction="medium",
                                        strategy_winrate=0.50,
                                        current_gross_exposure_pct=ctx.exposure_snapshot.gross_pct,
                                        current_net_exposure_pct=ctx.exposure_snapshot.net_pct,
                                        current_theme_exposure_pct=ctx.exposure_snapshot.theme_pct.get(
                                            THEME_MAP.get(sqqq_sym, sqqq_sym), 0.0
                                        ),
                                        is_etf=sqqq_sym in ETF_SYMBOLS,
                                        is_inverse_etf=sqqq_sym in INVERSE_ETFS,
                                        ohlcv_df=df_s,
                                        theme_key=THEME_MAP.get(sqqq_sym, sqqq_sym),
                                    )
    
                                    if not sizing_s or sizing_s.shares <= 0:
                                        rr = getattr(sizing_s, "reject_reason", None) if sizing_s else None
                                        log_entry_skip(
                                            ctx.now,
                                            sqqq_sym,
                                            "scaling skipped — sizing rejected (%s)" % (rr or "zero shares"),
                                            verbose=ctx.verbose,
                                            force=True,
                                        )
                                    else:
                                        step_cfg = steps[scale_count]
                                        size_mult = float(step_cfg.get("size_multiplier") or 0.0)
                                        add_sh = max(1, int(sizing_s.shares * size_mult))
                                        add_notional = add_sh * close_s
                                        room = max_bear_etf_notional - current_bear_etf_notional
    
                                        if add_notional > room and close_s > 0:
                                            add_sh = max(0, int(room / close_s))
                                            add_notional = add_sh * close_s
    
                                        buying_power_s = scaled_buying_power_for_lane(
                                            buying_power=ctx.broker.get_buying_power(),
                                            equity=float(ctx.account_equity),
                                            config=ctx.config,
                                            regime_score=(
                                                ctx.regime_result.score
                                                if ctx.regime_result is not None
                                                else None
                                            ),
                                            regime_condition=(
                                                ctx.regime_result.condition
                                                if ctx.regime_result is not None
                                                else None
                                            ),
                                            full_invest=False,
                                            lane="stocks",
                                        )
    
                                        if add_sh <= 0:
                                            log_entry_skip(
                                                ctx.now,
                                                sqqq_sym,
                                                "scaling skipped — clipped to 0 shares (room $%.0f)" % room,
                                                verbose=ctx.verbose,
                                                force=True,
                                            )
                                        elif add_notional > buying_power_s:
                                            log_entry_skip(
                                                ctx.now,
                                                sqqq_sym,
                                                "scaling skipped — buying power (need $%.0f, have $%.0f)"
                                                % (add_notional, buying_power_s),
                                                verbose=ctx.verbose,
                                                force=True,
                                            )
                                        else:
                                            if (
                                                ctx.allowed_symbols_for_stock_orders is not None
                                                and sqqq_sym
                                                not in ctx.allowed_symbols_for_stock_orders
                                            ):
                                                log_entry_skip(
                                                    ctx.now,
                                                    sqqq_sym,
                                                    "not in allowed stock universe",
                                                    verbose=ctx.verbose,
                                                    force=True,
                                                )
                                            else:
                                                buy_order_s = ctx.engine.execution.build_order_for_entry(
                                                    sqqq_sym,
                                                    "buy",
                                                    add_sh,
                                                    quote_s.reference_mid(close_s) if quote_s else close_s,
                                                    spread_pct_s or 0.15,
                                                    ignore_spread_gate=_ignore_spread_lv_s or _skip_nbbo_spread_s,
                                                    bid=float(quote_s.bid) if quote_s else None,
                                                    ask=float(quote_s.ask) if quote_s else None,
                                                )
                                                if not buy_order_s:
                                                    log_entry_skip(
                                                        ctx.now,
                                                        sqqq_sym,
                                                        "scaling skipped — could not build order",
                                                        verbose=ctx.verbose,
                                                        force=True,
                                                    )
                                                else:
                                                    _ord_scale = ctx.broker.submit_order(buy_order_s)
                                                    _fill_scale = ctx.broker.resolve_entry_price_from_fill(
                                                        _ord_scale, float(close_s)
                                                    )
                                                    current_bear_etf_notional += add_notional
    
                                                    prev = current_positions.get(sqqq_sym, {})
                                                    merge_add_tracked(
                                                        sqqq_sym,
                                                        add_sh,
                                                        _fill_scale,
                                                        bear_etf_stop_pct,
                                                        user_id=ctx.user_id,
                                                        data_dir=ctx.data_dir,
                                                        extras={
                                                            "scale_count": scale_count + 1,
                                                            "last_entry_price": _fill_scale,
                                                            "last_scale_ts": ctx.now.isoformat(),
                                                        },
                                                        et_trading_date=ctx.now.strftime("%Y-%m-%d"),
                                                    )
                                                    current_positions[sqqq_sym] = {
                                                        "notional": float(prev.get("notional", 0)) + add_notional,
                                                        "stop_pct": bear_etf_stop_pct,
                                                    }
    
                                                    _tr_post = load_tracked(ctx.user_id, data_dir=ctx.data_dir)
                                                    _r_sc = _tr_post.get(sqqq_sym) or {}
                                                    _qty_after = int(_r_sc.get("qty") or 0)
                                                    _avg_sc = _r_sc.get("entry_price")
                                                    _avg_sc_f = float(_avg_sc) if _avg_sc is not None else None
                                                    _last_sc = _r_sc.get("last_entry_price")
                                                    _last_sc_f = (
                                                        float(_last_sc)
                                                        if _last_sc is not None
                                                        else float(_fill_scale)
                                                    )
                                                    log_inverse_state_line(
                                                        ctx.now,
                                                        sqqq_sym,
                                                        shares=_qty_after,
                                                        scale_count=scale_count + 1,
                                                        num_scale_steps=len(steps),
                                                        avg_entry=_avg_sc_f,
                                                        last_entry=_last_sc_f,
                                                        unrealized_pnl=None,
                                                    )
                    except Exception as e:
                        log_entry_skip(
                            ctx.now,
                            sqqq_sym,
                            "scaling skipped — %s: %s" % (type(e).__name__, str(e)[:60]),
                            verbose=ctx.verbose,
                            force=True,
                        )
    return bear_etf_universe_set
