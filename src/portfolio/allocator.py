"""
Post-scan **capital allocation** and **ranked** trend-long dispatch helpers.

Used by :mod:`scripts.run_alpaca_loop` so the loop can stay “fetch / decide / act”
without inlining dedupe, allocator pass, and ranking flush. When options and allocator are
both on, :func:`src.capital_allocator_loop.trend_long_strength_uses_equity_allocator` filters
**strong** trend-longs to the per-symbol path so this batch remains **equity** notionals.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

log = logging.getLogger(__name__)

from src.allocation_config import effective_ranked_signals_cap, parse_allocation_config
from src.alpha_config import alpha_cap_ranked_take
from src.capital_allocator_loop import (
    _entry_terminal_payload,
    _record_entry_terminal_outcome,
    build_allocator_candidates,
    dedupe_cap_alloc_rows,
    empty_alloc_equal_split_buys,
    execute_capital_allocator_pass,
)
from src.exposure_gates import (
    allocator_buys_disallowed_over_max_gross,
    allocator_buys_refused_when_gross_above_threshold,
)
from src.risk_limits import risk_no_recycle_blocks_allocator_buys
from src.portfolio_allocation import scaled_buying_power_for_lane
from src.signal_ranking import (
    SIGNAL_RANKING_MODE_COMPOSITE,
    SIGNAL_RANKING_MODE_SIGNAL_PRIORITY,
    SIGNAL_RANKING_MODE_STRENGTH,
    SIGNAL_RANKING_MODE_TIER,
    canonical_signal_ranking_mode,
    rank_trend_long_candidate_rows,
    sector_etf_symbol_frozenset,
)
from src.signal_selection import get_valid_signals, rank_all_by_mode, select_top_signals
from src.winner_allocation import mark_top_signal_symbols_in_chosen


def _allocator_queue_symbols(rows: list[dict[str, Any]] | None) -> str:
    symbols: list[str] = []
    for row in rows or []:
        if isinstance(row, dict):
            sym = row.get("sym_u") or row.get("symbol") or row.get("ticker")
        else:
            sym = (
                getattr(row, "sym_u", None)
                or getattr(row, "symbol", None)
                or getattr(row, "ticker", None)
            )
        sym_u = str(sym or "").strip().upper()
        if sym_u:
            symbols.append(sym_u)
    return ",".join(dict.fromkeys(symbols))


def run_post_scan_capital_allocator(
    cap_alloc_candidates: list[dict[str, Any]],
    *,
    broker: Any,
    engine: Any,
    config: dict[str, Any],
    dt: datetime,
    positions: list[dict[str, Any]],
    tracked: dict[str, Any],
    current_positions: dict[str, Any],
    eligible_active: list[str],
    account_equity: float,
    available_cash: float,
    ca_cfg: dict[str, Any],
    user_id: str,
    data_dir: Any,
    stale_quote_max_age: float,
    strength_jitter_max: float,
    et_date_iso: str | None,
    cycle_risk_state: dict[str, int] | None,
    verbose: bool,
    exit_context: Any,
    reg_score_bp: int | None,
    reg_cond_bp: str | None,
    entry_full_invest_flag: bool,
    gross_exposure_pct: float | None = None,
    entry_wave_strong_signal_count: int | None = None,
    symbol_sector: dict[str, str] | None = None,
    theme_map: dict[str, str] | None = None,
    locked_buying_power: float | None = None,
) -> float:
    """
    Deduplicate queued rows, run :func:`execute_capital_allocator_pass`, then refresh
    available cash (broker BP × effective min-cash / full-invest rules). Returns
    *available_cash* unchanged if the list is empty or the plan/execute phase raises
    (dedupe, gate, or :func:`execute_capital_allocator_pass` — logged, no partial portfolio update).

    When *gross_exposure_pct* is set and ``portfolio.exposure_gates`` is on, **buy** actions
    are suppressed if the book is over the same effective max gross (%% of equity) as
    :func:`allocator_buys_disallowed_over_max_gross`.

    The same *gross_exposure_pct* is passed into the allocator so **risk override** can switch
    to ``mode: risk_control`` when gross (as a fraction of equity) is above
    ``capital_allocator.risk_control_gross_frac`` (default 0.95), optionally blocking buys.
    When gross is **below** ``min_gross_deployment_pct`` (default 0.85) and not in risk override,
    mode is ``deploy`` (under-invested). In deploy mode, the loop keeps only the top
    ``deploy_top_n_signals`` (3–5) candidates by score before :func:`execute_capital_allocator_pass`
    calls :meth:`src.capital_allocator.CapitalAllocator.allocate` (notional allocation).
    If the plan is still empty, :func:`src.capital_allocator_loop.empty_alloc_equal_split_buys` may
    apply (top ``empty_alloc_top_n`` by score, equal split of cash). With ``regime_score==4`` and
    gross **below** ``min_gross_deployment_pct``, *force_allocate* skips the ``require_net_sell_gte_buy``
    trim so buy-only plans are not fully stripped.
    If ``risk.no_recycle_above_pct`` is set and current gross (%% of equity) is **strictly above** that
    **fraction** (e.g. ``0.94``), allocator buys are disabled (no recycling while over the band);
    *symbol_sector* / *theme_map* are still passed for diversification when buys are allowed.

    When *locked_buying_power* is set (live loop: one snapshot after the symbol scan), it overrides
    *available_cash* for ``allocate()`` / fallback equal-split sizing so trims and per-symbol BP
    mutations inside the scan do not change the planned deployable cash mid-cycle.
    """
    queued_count = len(cap_alloc_candidates or [])
    queued_symbols = _allocator_queue_symbols(cap_alloc_candidates)
    log.info(
        "ALLOCATOR_PASS_START queued=%d",
        queued_count,
    )
    log.info(
        "ALLOCATOR_QUEUE_CONTENTS symbols=%s",
        queued_symbols,
    )
    if not cap_alloc_candidates:
        log.info("ALLOCATOR_PASS_SKIP reason=no_candidates queued=0")
        return float(available_cash)
    try:
        _alloc_ap = parse_allocation_config(config)
        _port0 = (config or {}).get("portfolio") or {}
        _sr0 = _port0.get("signal_ranking") if isinstance(_port0.get("signal_ranking"), dict) else {}
        _rank_mode0 = canonical_signal_ranking_mode(
            _sr0.get("ranking_mode"),
            allocation_rank_by_strength=bool(_alloc_ap.get("rank_by_signal_strength")),
            allocation_rank_top_k_by=str(_alloc_ap.get("rank_top_k_by") or "strength_eff"),
        )
        _ca_rows = dedupe_cap_alloc_rows(cap_alloc_candidates, ranking_mode=_rank_mode0)
        log.info(
            "ALLOCATOR_PASS_AFTER_DEDUPE queued=%d symbols=%s",
            len(_ca_rows),
            _allocator_queue_symbols(_ca_rows),
        )
        if not _ca_rows:
            log.info(
                "ALLOCATOR_PASS_SKIP reason=dedupe_removed_all queued=%d",
                queued_count,
            )
            return float(available_cash)
        _mt = effective_ranked_signals_cap(config)
        if _mt > 0 and len(_ca_rows) > _mt:
            _port = (config or {}).get("portfolio") or {}
            _sr = _port.get("signal_ranking") or {}
            _rmode = canonical_signal_ranking_mode(
                _sr.get("ranking_mode"),
                allocation_rank_by_strength=bool(_alloc_ap.get("rank_by_signal_strength")),
                allocation_rank_top_k_by=str(_alloc_ap.get("rank_top_k_by") or "strength_eff"),
            )
            _sef = sector_etf_symbol_frozenset(config)
            _pre_n = len(_ca_rows)
            _ca_rows, _dropped_ca = rank_trend_long_candidate_rows(
                _ca_rows,
                max_take=_mt,
                sector_etfs=_sef,
                ranking_mode=_rmode,
            )
            _row_by_symbol = {
                str(row.get("symbol") or row.get("sym_u") or "").strip().upper(): row
                for row in cap_alloc_candidates
                if str(row.get("symbol") or row.get("sym_u") or "").strip()
            }
            _event_store = getattr(broker, "_sqlite_event_store", None)
            for _d in _dropped_ca:
                _d_sym = str(_d or "").strip().upper()
                _d_row = _row_by_symbol.get(_d_sym, {})
                log.info(
                    "[%s] capital_allocator: not in top %d (signal rank) — skipped %s (had %d candidates)",
                    user_id,
                    _mt,
                    _d,
                    _pre_n,
                )
                _record_entry_terminal_outcome(
                    store=_event_store,
                    user_id=str(user_id),
                    symbol=_d_sym,
                    route=str(_d_row.get("route") or _d_row.get("source") or "allocator"),
                    stage="allocator_filtered",
                    reason="not_in_ranked_signal_cap",
                    payload=_entry_terminal_payload(
                        _d_row,
                        ranked_signal_cap=_mt,
                        pre_filter_candidate_count=_pre_n,
                        ranking_mode=_rmode,
                    ),
                    ts=dt,
                )
            log.info(
                "ALLOCATOR_PASS_AFTER_DEDUPE queued=%d symbols=%s",
                len(_ca_rows),
                _allocator_queue_symbols(_ca_rows),
            )
            if not _ca_rows:
                log.info(
                    "ALLOCATOR_PASS_SKIP reason=ranked_signal_cap_removed_all queued=%d",
                    queued_count,
                )
                return float(available_cash)
        _allow_buys = True
        if gross_exposure_pct is not None:
            _over = allocator_buys_disallowed_over_max_gross(
                float(gross_exposure_pct),
                config,
                regime_score=reg_score_bp,
                regime_condition=reg_cond_bp,
                entry_wave_strong_signal_count=entry_wave_strong_signal_count,
            )
            _refuse_thr = allocator_buys_refused_when_gross_above_threshold(
                float(gross_exposure_pct),
                config,
                ca_cfg,
            )
            _allow_buys = not _over and not _refuse_thr
        _no_recycle = False
        if gross_exposure_pct is not None and risk_no_recycle_blocks_allocator_buys(
            float(gross_exposure_pct), config
        ):
            _no_recycle = True
        _cash_plan = (
            float(locked_buying_power)
            if locked_buying_power is not None
            else float(available_cash)
        )
        execute_capital_allocator_pass(
            signals=_ca_rows,
            broker=broker,
            engine=engine,
            config=config,
            dt=dt,
            positions=positions,
            tracked=tracked,
            current_positions=current_positions,
            eligible_active=eligible_active,
            account_equity=float(account_equity),
            cash=float(_cash_plan),
            ca_cfg=ca_cfg,
            user_id=user_id,
            data_dir=data_dir,
            stale_quote_max_age=stale_quote_max_age,
            strength_jitter_max=strength_jitter_max,
            et_date_iso=et_date_iso,
            cycle_risk_state=cycle_risk_state,
            verbose=verbose,
            exit_context=exit_context,
            allow_allocator_buys=_allow_buys,
            gross_exposure_pct=gross_exposure_pct,
            symbol_sector=symbol_sector,
            theme_map=theme_map,
            no_recycle_block=_no_recycle,
            regime_score=reg_score_bp,
            regime_condition=reg_cond_bp,
            entry_wave_strong_signal_count=entry_wave_strong_signal_count,
        )
        return float(
            scaled_buying_power_for_lane(
                buying_power=broker.get_buying_power(),
                equity=float(account_equity),
                config=config,
                regime_score=reg_score_bp,
                regime_condition=reg_cond_bp,
                full_invest=bool(entry_full_invest_flag),
                lane="stocks",
            )
        )
    except Exception as e:
        log.info(
            "ALLOCATOR_PASS_SKIP reason=plan_execute_exception queued=%d",
            queued_count,
        )
        log.exception(
            "[%s] capital_allocator: plan/execute failed — %s: %s",
            user_id,
            type(e).__name__,
            str(e)[:200],
        )
        print(
            dt.strftime("%H:%M ET"),
            "[%s] capital_allocator ERROR: %s: %s"
            % (user_id, type(e).__name__, str(e)[:120]),
            flush=True,
        )
        return float(available_cash)


def run_post_sell_reallocation(
    had_equity_sell: bool,
    remainder_cash: float,
    cap_alloc_candidates: list[dict[str, Any]],
    *,
    broker: Any,
    engine: Any,
    config: dict[str, Any],
    dt: datetime,
    positions: list[dict[str, Any]],
    tracked: dict[str, Any],
    current_positions: dict[str, Any],
    eligible_active: list[str],
    account_equity: float,
    ca_cfg: dict[str, Any],
    user_id: str,
    data_dir: Any,
    stale_quote_max_age: float,
    strength_jitter_max: float,
    et_date_iso: str | None,
    cycle_risk_state: dict[str, int] | None,
    verbose: bool,
    exit_context: Any,
    reg_score_bp: int | None,
    reg_cond_bp: str | None,
    entry_full_invest_flag: bool,
    gross_exposure_pct: float | None = None,
    entry_wave_strong_signal_count: int | None = None,
    symbol_sector: dict[str, str] | None = None,
    theme_map: dict[str, str] | None = None,
) -> float:
    """
    After a stock **sell** in the same loop iteration, redeploy *remainder* cash across the top
    ranked allocator signals with :func:`src.capital_allocator_loop.empty_alloc_equal_split_buys`
    (``remainder / n`` per name). Requires ``portfolio.capital_allocator.post_sell_reallocation.enabled``;
    by default *had_equity_sell* must be true (``require_equity_sell``).

    Uses the same rank/dedupe rules as :func:`run_post_scan_capital_allocator`. Merges
    ``ca_cfg`` with ``require_net_sell_gte_buy: false`` so a buy-only plan is not stripped.
    """
    pca = ((config or {}).get("portfolio") or {}).get("capital_allocator")
    pca = pca if isinstance(pca, dict) else {}
    psr = pca.get("post_sell_reallocation")
    psr = psr if isinstance(psr, dict) else {}
    if not bool(psr.get("enabled", False)):
        return float(remainder_cash)
    if bool(psr.get("require_equity_sell", True)) and not had_equity_sell:
        return float(remainder_cash)
    if not cap_alloc_candidates:
        return float(remainder_cash)
    try:
        rmg = max(0.0, float(remainder_cash))
    except (TypeError, ValueError):
        rmg = 0.0
    try:
        mfc = float(psr.get("min_freed_cash", 1.0) or 0.0)
    except (TypeError, ValueError):
        mfc = 1.0
    if mfc < 0:
        mfc = 0.0
    if rmg < mfc - 1e-9:
        return float(remainder_cash)
    if bool(ca_cfg.get("single_pass_per_cycle", True)):
        log.info(
            "[%s] post_sell_reallocation: skipped — single_pass_per_cycle (no second allocator execute)",
            user_id,
        )
        try:
            return float(
                scaled_buying_power_for_lane(
                    buying_power=broker.get_buying_power(),
                    equity=float(account_equity),
                    config=config,
                    regime_score=reg_score_bp,
                    regime_condition=reg_cond_bp,
                    full_invest=bool(entry_full_invest_flag),
                    lane="stocks",
                )
            )
        except Exception:
            return float(remainder_cash)
    try:
        _split_n = int(float(psr.get("split_n", 5) or 5))
    except (TypeError, ValueError):
        _split_n = 5
    _split_n = max(1, min(20, _split_n))
    _alloc_psr = parse_allocation_config(config)
    _port_psr = (config or {}).get("portfolio") or {}
    _sr_psr = (
        _port_psr.get("signal_ranking")
        if isinstance(_port_psr.get("signal_ranking"), dict)
        else {}
    )
    _rank_mode_psr = canonical_signal_ranking_mode(
        _sr_psr.get("ranking_mode"),
        allocation_rank_by_strength=bool(_alloc_psr.get("rank_by_signal_strength")),
        allocation_rank_top_k_by=str(_alloc_psr.get("rank_top_k_by") or "strength_eff"),
    )
    _ca_rows = dedupe_cap_alloc_rows(cap_alloc_candidates, ranking_mode=_rank_mode_psr)
    _mt = effective_ranked_signals_cap(config)
    _rank_mode_for_allocator = _rank_mode_psr
    if _mt > 0 and len(_ca_rows) > _mt:
        _port = (config or {}).get("portfolio") or {}
        _sr = _port.get("signal_ranking") or {}
        _rmode = canonical_signal_ranking_mode(
            _sr.get("ranking_mode"),
            allocation_rank_by_strength=bool(_alloc_psr.get("rank_by_signal_strength")),
            allocation_rank_top_k_by=str(_alloc_psr.get("rank_top_k_by") or "strength_eff"),
        )
        _rank_mode_for_allocator = _rmode
        _sef = sector_etf_symbol_frozenset(config)
        _ca_rows, _dropped_psr = rank_trend_long_candidate_rows(
            _ca_rows,
            max_take=_mt,
            sector_etfs=_sef,
            ranking_mode=_rmode,
        )
        for _d in _dropped_psr:
            log.info(
                "[%s] post_sell_realloc: not in top %d by rank — skipped %s",
                user_id,
                _mt,
                _d,
            )
    cands = build_allocator_candidates(_ca_rows, ranking_mode=_rank_mode_for_allocator)
    try:
        mleg = float(
            (ca_cfg or {}).get("min_realloc_leg", 300.0) or 300.0
        )
    except (TypeError, ValueError):
        mleg = 300.0
    actions = empty_alloc_equal_split_buys(
        candidates=cands,
        cash=float(rmg),
        min_realloc_leg=float(mleg),
        top_n=_split_n,
    )
    if not actions:
        return float(remainder_cash)
    _allow_buys = True
    if gross_exposure_pct is not None:
        _over = allocator_buys_disallowed_over_max_gross(
            float(gross_exposure_pct),
            config,
            regime_score=reg_score_bp,
            regime_condition=reg_cond_bp,
            entry_wave_strong_signal_count=entry_wave_strong_signal_count,
        )
        _refuse_thr = allocator_buys_refused_when_gross_above_threshold(
            float(gross_exposure_pct),
            config,
            ca_cfg,
        )
        _allow_buys = not _over and not _refuse_thr
    _no_recycle = False
    if gross_exposure_pct is not None and risk_no_recycle_blocks_allocator_buys(
        float(gross_exposure_pct), config
    ):
        _no_recycle = True
    ca2 = {**ca_cfg, "require_net_sell_gte_buy": False}
    log.info(
        "[%s] post_sell_realloc: %d name(s) × ~$%.0f (cash $%.0f, equity sell this pass: %s)",
        user_id,
        len(actions),
        float(rmg) / max(1, len(actions)),
        rmg,
        str(had_equity_sell),
    )
    try:
        execute_capital_allocator_pass(
            signals=_ca_rows,
            broker=broker,
            engine=engine,
            config=config,
            dt=dt,
            positions=positions,
            tracked=tracked,
            current_positions=current_positions,
            eligible_active=eligible_active,
            account_equity=float(account_equity),
            cash=float(rmg),
            ca_cfg=ca2,
            user_id=user_id,
            data_dir=data_dir,
            stale_quote_max_age=stale_quote_max_age,
            strength_jitter_max=strength_jitter_max,
            et_date_iso=et_date_iso,
            cycle_risk_state=cycle_risk_state,
            verbose=verbose,
            exit_context=exit_context,
            allow_allocator_buys=_allow_buys,
            gross_exposure_pct=gross_exposure_pct,
            symbol_sector=symbol_sector,
            theme_map=theme_map,
            no_recycle_block=_no_recycle,
            regime_score=reg_score_bp,
            regime_condition=reg_cond_bp,
            entry_wave_strong_signal_count=entry_wave_strong_signal_count,
            preallocated_equal_split_buys=actions,
        )
    except Exception as e:
        log.exception(
            "[%s] post_sell_realloc failed — %s: %s",
            user_id,
            type(e).__name__,
            str(e)[:200],
        )
        return float(remainder_cash)
    return float(
        scaled_buying_power_for_lane(
            buying_power=broker.get_buying_power(),
            equity=float(account_equity),
            config=config,
            regime_score=reg_score_bp,
            regime_condition=reg_cond_bp,
            full_invest=bool(entry_full_invest_flag),
            lane="stocks",
        )
    )


def flush_ranked_trend_long_entry_queue(
    ranked_entry_queue: list[dict[str, Any]],
    *,
    max_take: int,
    sector_etfs: frozenset[str],
    ranking_mode: str,
    log_entry_skip: Callable[..., None],
    dt: datetime,
    symbol_for_skip: str,
    verbose: bool,
    dispatch_row: Callable[[dict[str, Any]], bool],
    winner_allocation_enabled: bool = False,
    winner_top_n: int = 0,
    winner_size_multiplier: float = 1.0,
    trim_weakest_for_blocked_top: Callable[[str], bool] | None = None,
    config: dict[str, Any] | None = None,
) -> None:
    """
    Rank the collected queue, log skips for dropped symbols, then call *dispatch_row*
    for each chosen row (adds *strength_cohort* for rotate/BP when applicable).

    Pipeline: ``get_valid_signals`` → full rank (same order as
    :func:`src.signal_ranking.rank_trend_long_candidate_rows`) → ``[:eff_take]`` where
    ``eff_take = min(max_take, alpha.select_top_k, …)`` from :func:`src.alpha_config.alpha_cap_ranked_take`.

    *dispatch_row* returns ``True`` when an entry was placed (options or stock). When the **rank-1**
    row returns ``False`` and *trim_weakest_for_blocked_top* is set, we trim one weakest eligible
    line for gross/BP headroom (same weakest-first ordering as cap-pressure trim) and retry that row once.
    """
    if not ranked_entry_queue:
        return
    _eff_take = alpha_cap_ranked_take(config, max_take)
    _valid = get_valid_signals(ranked_entry_queue)
    if not _valid:
        return
    _ranked_full = rank_all_by_mode(
        _valid,
        sector_etfs=sector_etfs,
        ranking_mode=ranking_mode,
    )
    _chosen_tl = select_top_signals(_ranked_full, _eff_take)
    _dropped_tl = [str(r["sym_u"]).upper() for r in _ranked_full[_eff_take:]]
    if (
        winner_allocation_enabled
        and int(winner_top_n) > 0
        and float(winner_size_multiplier) > 1.0
        and _chosen_tl
    ):
        mark_top_signal_symbols_in_chosen(
            _chosen_tl,
            top_n=int(winner_top_n),
            size_multiplier=float(winner_size_multiplier),
            sym_key="sym_u",
        )
    if ranking_mode == SIGNAL_RANKING_MODE_STRENGTH:
        _rank_skip_msg = (
            "signal rank: not in top %d by strength_eff (allocation)" % _eff_take
        )
    elif ranking_mode == SIGNAL_RANKING_MODE_SIGNAL_PRIORITY:
        _rank_skip_msg = (
            "signal rank: not in top %d by signal priority (trend+momentum+volatility+RS)"
            % _eff_take
        )
    elif ranking_mode == SIGNAL_RANKING_MODE_COMPOSITE:
        _rank_skip_msg = (
            "signal rank: not in top %d by weighted entry composite" % _eff_take
        )
    else:
        _rank_skip_msg = (
            "signal rank: not in top %d (SPY/QQQ > NVDA/MSFT > sector ETFs > others; then strength)"
            % _eff_take
        )
    for _ds in _dropped_tl:
        log_entry_skip(
            dt,
            _ds,
            _rank_skip_msg,
            verbose=verbose,
            force=False,
        )
    _cohort_eff: list[float] = []
    for _r in _chosen_tl:
        if _r.get("strength_eff") is not None:
            try:
                _cohort_eff.append(float(_r["strength_eff"]))
            except (TypeError, ValueError):
                pass
    for _rank_i, row_tl in enumerate(_chosen_tl):
        if _cohort_eff:
            row_tl["strength_cohort"] = _cohort_eff
        try:
            _disp_ok = dispatch_row(row_tl)
            if (
                _rank_i == 0
                and _disp_ok is False
                and trim_weakest_for_blocked_top is not None
            ):
                _top_sym = str(row_tl.get("sym_u", symbol_for_skip)).strip().upper()
                if _top_sym and trim_weakest_for_blocked_top(_top_sym):
                    dispatch_row(row_tl)
        except Exception as _rank_exc:
            log_entry_skip(
                dt,
                str(row_tl.get("sym_u", symbol_for_skip)),
                "%s: %s" % (type(_rank_exc).__name__, str(_rank_exc)[:80]),
                verbose=verbose,
                force=False,
            )
