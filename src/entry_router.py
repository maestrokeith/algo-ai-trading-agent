"""
Route new entries between options and existing stock execution.

Stock sizing and risk rules are unchanged. Options flow:
  options_adapter → options_selector (ranked budget scan) → options_execution (prepare + place order).
"""
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from .options_adapter import adapt_stock_signal_to_option_intent
from .options_premium_risk import (
    count_open_long_option_positions,
    holding_equity_long_for_underlying,
    sum_open_option_positions_premium,
)
from .options_config import (
    fallback_to_stock,
    max_open_option_positions_cap,
    options_conviction_entry_allowed,
    options_live_pilot_enabled,
    options_mode,
)
from .options_execution import (
    place_option_order,
    prepare_option_order_premium_only_with_lower_strike_fallback,
)
from .portfolio_allocation import effective_options_total_cap_frac
from .options_selector import OptionContractCandidate, SelectedOptionContract
from .options_position_manager import (
    entry_blocked_reason as options_entry_blocked_reason,
    record_option_entry as record_options_entry,
)
from .options_observability import (
    normalize_options_rejection_reason,
    record_options_candidate,
    record_options_contract_found,
    record_options_fill,
    record_options_order_accepted,
    record_options_order_submitted,
    record_options_rejection,
    record_options_signal,
)
from .strategy_router import find_option_under_budget
from .inverse_hedge import hedge_symbol, long_hedge_position_held
from .live.options_chain import log_options_disabled_non_paper, options_runtime_enabled

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EntryRouteSignal:
    """Semantic signal for routing — does not replace equity EntrySignal / sizing."""

    underlying: str
    direction: str  # "bullish" | "bearish" (maps to call / put per config entry_mapping)
    source: str  # "trend_long" | "bear_etf" | "news_override"
    stock_symbol: str | None = None  # equity leg if different (e.g. SQQQ vs QQQ option)
    conviction_band: str | None = None  # weak | medium | strong (optional; ``high`` normalized in gate)
    conviction_score: float | None = None  # 0–1 or 0–100 → band when ``conviction_band`` omitted
    news_score: float | None = None
    event_score: float | None = None
    relative_volume: float | None = None
    catalyst_type: str | None = None


def should_use_options(
    config: dict[str, Any],
    signal: EntryRouteSignal,
    broker: Any | None = None,
) -> bool:
    """Whether this signal may use the options executor (contract selection TBD)."""
    if broker is not None and not options_runtime_enabled(broker, config):
        return False
    opts = config.get("options") or {}
    if not bool(opts.get("enabled")):
        return False
    mode = str(opts.get("mode") or "").strip().lower()
    if mode not in ("long_premium_only", "paper_only", "live", "live_long_premium"):
        return False
    if not bool(opts.get("only_buy_options", True)):
        return False
    allowed = {str(x).upper() for x in (opts.get("allowed_underlyings") or [])}
    u = str(signal.underlying or "").upper()
    if u not in allowed:
        return False
    d = str(signal.direction or "").lower()
    if d not in ("bullish", "bearish"):
        return False
    mapping = opts.get("entry_mapping") or {}
    if d == "bullish" and not mapping.get("bullish_signal"):
        return False
    if d == "bearish" and not mapping.get("bearish_signal"):
        return False
    ok_cv, _ = options_conviction_entry_allowed(config, signal)
    return ok_cv


def _option_contract_cap(config: Mapping[str, Any] | None) -> int:
    opts = (config or {}).get("options") if isinstance(config, Mapping) else {}
    if not isinstance(opts, Mapping):
        return 1
    raw = opts.get("max_contracts_per_trade")
    if raw is None or str(raw).strip() == "":
        raw = opts.get("v1_max_contracts_per_trade")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def _live_options_pilot_pre_submit_check(
    config: Mapping[str, Any] | None,
    *,
    account_equity: float,
    positions: list[dict[str, Any]] | None,
    contracts: int,
    debit: float,
) -> tuple[bool, str | None, bool]:
    """Final live-pilot guard before order intent/submission."""
    if options_mode(dict(config or {})) not in {"live", "live_long_premium", "long_premium_only"}:
        return True, None, False
    cfg = dict(config or {})
    if not options_live_pilot_enabled(cfg):
        return False, "live_pilot_disabled", False
    max_positions = max_open_option_positions_cap(cfg)
    open_positions = count_open_long_option_positions(positions)
    if open_positions >= max_positions:
        return False, "max_option_positions", False
    max_contracts = _option_contract_cap(cfg)
    if int(contracts) > max_contracts:
        return False, "max_contracts_per_trade", False
    cap_frac = effective_options_total_cap_frac(cfg)
    existing = sum_open_option_positions_premium(positions)
    cap_usd = float(account_equity) * float(cap_frac)
    if existing > cap_usd + 1e-6:
        return False, "options_exposure_over_pilot_limit", True
    if existing + float(debit) > cap_usd + 1e-6:
        return False, "options_exposure_cap", True
    return True, None, False


def use_equity_fallback_after_options(
    opts: dict[str, Any] | None,
    *,
    options_routing_attempted: bool,
    options_order_placed: bool,
) -> bool:
    """
    Whether to run the stock leg after the options path did not place an order.

    If routing was not attempted (options off, gates, or ineligible signal), return
    ``True`` so equity logic runs as today. If routing was attempted but
    :func:`route_to_options_executor` returned without an order, consult
    ``options.allow_fallback_to_shares`` / ``options.fallback_to_stock`` (default ``True``).
    """
    if options_order_placed:
        return False
    if not options_routing_attempted:
        return True
    o = opts if isinstance(opts, dict) else {}
    if str(o.get("mode") or "").strip().lower() == "paper_only":
        return False
    return fallback_to_stock({"options": o} if o else {})


def _options_int_opt(opts: dict[str, Any], key: str) -> int | None:
    raw = opts.get(key)
    if raw is None:
        return None
    if str(raw).strip() == "":
        return None
    return int(raw)


def _options_min_regime_for_long(opts: dict[str, Any]) -> int | None:
    """If set, bullish / trend-long options require ``regime_score >=`` this (0–5)."""
    return _options_int_opt(opts, "min_regime_for_long")


def _options_min_bullish_regime(opts: dict[str, Any]) -> int | None:
    """If set, trend-long options require a non-defensive regime (see :func:`_trend_long_regime_allows_options`)."""
    return _options_int_opt(opts, "min_bullish_regime")


def _trend_long_regime_allows_options(
    regime_condition: str | None,
    regime_score: int | None,
) -> bool:
    """
    True when trend-long option routing may proceed under ``min_bullish_regime``:

    * ``regime_condition`` **bullish** or **neutral** (``market_regime`` scorer), or
    * label missing: treat ``regime_score >= 2`` as bullish/neutral band (same scorer thresholds).

    **defensive** (and score ``< 2`` when unlabeled) → False.
    """
    c = str(regime_condition or "").strip().lower()
    if c in ("bullish", "neutral"):
        return True
    if c == "defensive":
        return False
    if regime_score is None:
        return False
    return int(regime_score) >= 2


def allow_long_options_trades() -> bool:
    """Non-bearish path: trend-long options are not blocked by the bearish inverse-hedge gate."""
    return True


def trend_long_options_extra_gate_ok(
    config: dict[str, Any],
    *,
    holding_sqqq: bool,
    pct_above_50d: float | None,
    regime_score: int | None = None,
    bearish_regime: bool = False,
    regime_condition: str | None = None,
    positions: Sequence[Mapping[str, Any]] | None = None,
    tracked: Mapping[str, Any] | None = None,
) -> bool:
    """Extra gate for trend-long option routing when enabled in config.

    When ``options.min_regime_for_long`` is set, ``regime_score`` must be >= that
    value (floor for long/call routing).

    When ``options.min_bullish_regime`` is set, regime must be **bullish** or **neutral**
    (``regime_condition`` from :class:`~src.market_regime.MarketRegimeScorer`, or
    ``regime_score >= 2`` when the label is unavailable). Defensive blocks.

    When ``options.trend_long_options_require_sqqq_and_strong_trend`` is true
    (legacy name; no longer uses breadth): in a **bearish** context (breadth
    ``bearish_regime`` or ``regime_condition == bearish``), require the configured
    inverse hedge (see :func:`long_hedge_position_held`). Otherwise
    :func:`allow_long_options_trades` applies (gate passes).

    When *positions* and *tracked* are provided, hedge detection uses them; else
    *holding_sqqq* is used (tests / callers without portfolio state).
    """
    _ = pct_above_50d  # legacy breadth arg; retained for call-site compatibility
    o = config.get("options") or {}
    need_long = _options_min_regime_for_long(o)
    if need_long is not None and (regime_score is None or int(regime_score) < need_long):
        return False
    if _options_min_bullish_regime(o) is not None and not _trend_long_regime_allows_options(
        regime_condition, regime_score
    ):
        return False
    if not bool(o.get("trend_long_options_require_sqqq_and_strong_trend", False)):
        return True
    bearish = bool(bearish_regime) or str(regime_condition or "").strip().lower() == "bearish"
    if bearish:
        if positions is not None and tracked is not None and (positions or tracked):
            return long_hedge_position_held(config, positions, tracked)
        return bool(holding_sqqq)
    return allow_long_options_trades()


def trend_long_options_extra_gate_reason(
    config: dict[str, Any],
    *,
    holding_sqqq: bool,
    pct_above_50d: float | None,
    regime_score: int | None = None,
    bearish_regime: bool = False,
    regime_condition: str | None = None,
    positions: Sequence[Mapping[str, Any]] | None = None,
    tracked: Mapping[str, Any] | None = None,
) -> str | None:
    """Human reason when an extra gate fails (else None)."""
    _ = pct_above_50d
    o = config.get("options") or {}
    need_long = _options_min_regime_for_long(o)
    if need_long is not None and (regime_score is None or int(regime_score) < need_long):
        return "long options require regime score >= %d" % need_long
    if _options_min_bullish_regime(o) is not None and not _trend_long_regime_allows_options(
        regime_condition, regime_score
    ):
        return "trend-long options require bullish or neutral regime"
    if not bool(o.get("trend_long_options_require_sqqq_and_strong_trend", False)):
        return None
    bearish = bool(bearish_regime) or str(regime_condition or "").strip().lower() == "bearish"
    if not bearish:
        return None
    if positions is not None and tracked is not None and (positions or tracked):
        if long_hedge_position_held(config, positions, tracked):
            return None
        h = hedge_symbol(config)
        return "trend-long options in bearish regime require long %s position" % h
    if not holding_sqqq:
        return "trend-long options in bearish regime require long SQQQ position"
    return None


def _options_ineligible_reason(config: dict[str, Any], signal: EntryRouteSignal) -> str:
    """Human reason when options.enabled but should_use_options is false (for logging)."""
    opts = config.get("options") or {}
    mode = str(opts.get("mode") or "").strip().lower()
    if mode not in ("long_premium_only", "paper_only", "live", "live_long_premium"):
        return "options.mode is %r (need long_premium_only, paper_only, live, or live_long_premium)" % (opts.get("mode"),)
    allowed = {str(x).upper() for x in (opts.get("allowed_underlyings") or [])}
    u = str(signal.underlying or "").upper()
    if not allowed:
        return "underlying not allowed for options"
    if u not in allowed:
        return "underlying not allowed for options"
    d = str(signal.direction or "").lower()
    if d not in ("bullish", "bearish"):
        return "direction %r not bullish/bearish" % (signal.direction,)
    mapping = opts.get("entry_mapping") or {}
    if d == "bullish" and not mapping.get("bullish_signal"):
        return "entry_mapping.bullish_signal missing"
    if d == "bearish" and not mapping.get("bearish_signal"):
        return "entry_mapping.bearish_signal missing"
    ok_cv, cv_reason = options_conviction_entry_allowed(config, signal)
    if not ok_cv:
        return cv_reason or "options conviction_required not met"
    return "options routing not eligible"


def _emit_options_trade_stock(
    log_dt: Any | None,
    signal: EntryRouteSignal,
    detail: str,
) -> None:
    """Structured ``route`` line (trade stock) or legacy one-liner; not an entry *skip*."""
    from datetime import datetime as _dt

    from src import entry_decision_log as _edl

    sym = str(signal.stock_symbol or signal.underlying or "OPTIONS").strip().upper()
    sig = str(signal.source or "trend_long").strip()
    if log_dt is not None and isinstance(log_dt, _dt):
        _edl.emit_options_trade_stock(log_dt, sym, signal=sig, detail=detail)
        return
    try:
        ts = log_dt.strftime("%H:%M ET") if log_dt is not None else ""
    except Exception:
        ts = ""
    tail = (" — " + detail) if detail else ""
    line = "%s trade stock (%s)%s" % (sym, sig, tail)
    if ts:
        print(ts, line, flush=True)
    else:
        print(line, flush=True)


def log_options_stock_path_if_ineligible(
    config: dict[str, Any],
    signal: EntryRouteSignal,
    log_dt: Any | None,
) -> None:
    """If options are on but this signal uses stock only, log route-to-stock (not a skip)."""
    opts = config.get("options") or {}
    if not bool(opts.get("enabled")):
        return
    if should_use_options(config, signal):
        return
    _emit_options_trade_stock(
        log_dt,
        signal,
        _options_ineligible_reason(config, signal),
    )


def _options_skip_msg(log_dt: Any | None, underlying: str, reason: str) -> None:
    from datetime import datetime as _dt

    from src import entry_decision_log as _edl

    label = str(underlying or "OPTIONS").upper()
    if log_dt is not None and isinstance(log_dt, _dt) and _edl.structured_skip_logs_enabled():
        _edl.emit_entry_skip(
            log_dt,
            label,
            reason,
            verbose=True,
            force=True,
            signal="options",
        )
        return
    try:
        ts = log_dt.strftime("%H:%M ET") if log_dt is not None else ""
    except Exception:
        ts = ""
    if ts:
        print(ts, f"{label} skip — {reason}", flush=True)
    else:
        print(f"{label} skip — {reason}", flush=True)


def _options_info(log_dt: Any | None, line: str) -> None:
    try:
        ts = log_dt.strftime("%H:%M ET") if log_dt is not None else ""
    except Exception:
        ts = ""
    if ts:
        print(ts, "OPTIONS —", line, flush=True)
    else:
        print("OPTIONS —", line, flush=True)


def _options_entry_log_lines(log_dt: Any | None, lines: list[str]) -> None:
    """Print a small block (timestamp + continuation lines) for option entry observability."""
    try:
        ts = log_dt.strftime("%H:%M ET") if log_dt is not None else ""
    except Exception:
        ts = ""
    for i, line in enumerate(lines):
        if i == 0:
            if ts:
                print(ts, line, flush=True)
            else:
                print(line, flush=True)
        elif ts:
            print(ts, " ", line, flush=True)
        else:
            print(" ", line, flush=True)


def _options_post_chain_skip(
    log_dt: Any | None,
    underlying: str,
    phase: str,
    reason: str,
    *,
    detail: str | None = None,
) -> None:
    """Log skip after chain fetch / try line: phase + exact reason, optional contract/context detail."""
    msg = "post-chain [%s]: %s" % (phase, reason)
    if detail:
        msg = "%s | %s" % (msg, detail)
    _options_skip_msg(log_dt, underlying, msg)


def _signal_symbol(signal: EntryRouteSignal) -> str:
    return str(signal.stock_symbol or signal.underlying or "OPTIONS").strip().upper()


def _log_option_signal(signal: EntryRouteSignal, *, eligible: bool, reason: str = "") -> None:
    record_options_signal(symbol=_signal_symbol(signal), eligible=eligible, reason=reason or "ok")
    log.info(
        "OPTION_SIGNAL symbol=%s underlying=%s direction=%s source=%s eligible=%s reason=%s conviction=%s news_score=%s event_score=%s relative_volume=%s catalyst_type=%s",
        _signal_symbol(signal),
        str(signal.underlying or "").strip().upper(),
        str(signal.direction or "").strip().lower(),
        str(signal.source or "").strip(),
        str(bool(eligible)).lower(),
        str(reason or "ok").replace(" ", "_"),
        "n/a" if signal.conviction_score is None else "%.6g" % float(signal.conviction_score),
        "n/a" if signal.news_score is None else "%.6g" % float(signal.news_score),
        "n/a" if signal.event_score is None else "%.6g" % float(signal.event_score),
        "n/a" if signal.relative_volume is None else "%.6g" % float(signal.relative_volume),
        str(signal.catalyst_type or "none"),
    )


def _log_option_entry_blocked(signal: EntryRouteSignal, *, stage: str, reason: str, detail: str = "") -> None:
    normalized = normalize_options_rejection_reason(reason)
    record_options_rejection(symbol=_signal_symbol(signal), stage=stage, reason=reason)
    log.info(
        "OPTION_ENTRY_BLOCKED symbol=%s underlying=%s direction=%s stage=%s reason=%s normalized_reason=%s detail=%s",
        _signal_symbol(signal),
        str(signal.underlying or "").strip().upper(),
        str(signal.direction or "").strip().lower(),
        str(stage or "unknown"),
        str(reason or "unknown").replace(" ", "_"),
        normalized,
        str(detail or "n/a").replace(" ", "_"),
    )
    log.info(
        "OPTIONS_REJECTION symbol=%s reason=%s stage=%s raw_reason=%s",
        _signal_symbol(signal),
        normalized,
        str(stage or "unknown"),
        str(reason or "unknown").replace(" ", "_"),
    )

def route_to_options_executor(
    config: dict[str, Any],
    signal: EntryRouteSignal,
    *,
    log_dt: Any | None = None,
    verbose: bool = False,
    account_equity: float | None = None,
    positions: list[dict[str, Any]] | None = None,
    broker: Any | None = None,
    execution_manager: Any | None = None,
    chain_candidates: Sequence[OptionContractCandidate] | None = None,
    underlying_spot: float | None = None,
    selected_override: SelectedOptionContract | None = None,
    tracked: Mapping[str, Any] | None = None,
) -> bool:
    """
    Attempt options execution. Returns True if an option order was placed (skip stock).

    Pipeline: adapt signal → ranked contract scan (budget + spread + liquidity) → prepare → place order.
    Pass `chain_candidates` from the broker (e.g. Alpaca `get_option_chain_candidates`); empty list skips selection.
    When ``selected_override`` is set, skips re-selection (caller already ran :func:`src.strategy_router.find_option_under_budget`).
    Without `broker` / `execution_manager`, preparation may succeed but nothing is sent.
    """
    record_options_candidate(
        symbol=_signal_symbol(signal),
        underlying=str(signal.underlying or "").strip().upper(),
        direction=str(signal.direction or "").strip().lower(),
        source=str(signal.source or "").strip(),
        stage="route_start",
    )
    if not options_runtime_enabled(broker, config):
        opts_block = config.get("options") if isinstance(config, Mapping) else {}
        mode_block = str((opts_block or {}).get("mode") or "").strip().lower()
        broker_live = bool(broker is not None and not bool(getattr(broker, "paper", True)))
        if (
            broker_live
            and bool((opts_block or {}).get("enabled"))
            and mode_block in {"live", "live_long_premium", "long_premium_only"}
            and not options_live_pilot_enabled(config)
        ):
            log.info("OPTIONS_LIVE_BLOCKED reason=live_pilot_disabled")
        _log_option_signal(signal, eligible=False, reason="runtime_disabled")
        _log_option_entry_blocked(signal, stage="runtime", reason="options_runtime_disabled")
        log_options_disabled_non_paper()
        return False
    opts = config.get("options") or {}
    _signal_eligible = should_use_options(config, signal, broker=broker)
    _log_option_signal(
        signal,
        eligible=_signal_eligible,
        reason="ok" if _signal_eligible else _options_ineligible_reason(config, signal),
    )
    if bool(opts.get("enabled")) and str(opts.get("mode") or "").strip().lower() in (
        "long_premium_only",
        "paper_only",
        "live",
        "live_long_premium",
    ):
        allowed = {str(x).upper() for x in (opts.get("allowed_underlyings") or [])}
        u = str(signal.underlying or "").strip().upper()
        if not allowed or u not in allowed:
            _log_option_entry_blocked(signal, stage="eligibility", reason="underlying_not_allowed")
            _emit_options_trade_stock(
                log_dt,
                signal,
                "underlying not allowed for options",
            )
            return False
        ok_cv, cv_reason = options_conviction_entry_allowed(config, signal)
        if not ok_cv:
            _log_option_entry_blocked(
                signal,
                stage="eligibility",
                reason=cv_reason or "options_conviction_required_not_met",
            )
            _emit_options_trade_stock(
                log_dt,
                signal,
                cv_reason or "options conviction_required not met",
            )
            return False

    blocked, blocked_reasons, blocked_reason = options_entry_blocked_reason(
        execution_manager,
        underlying=signal.underlying,
        direction=signal.direction,
    )
    if blocked:
        reason_text = blocked_reason or "options kill switch / duplicate contract"
        _log_option_entry_blocked(
            signal,
            stage="entry_guard",
            reason=reason_text,
            detail=",".join(blocked_reasons),
        )
        _options_entry_log_lines(
            log_dt,
            [
                "OPTIONS_ENTRY_BLOCKED symbol=%s underlying=%s direction=%s reasons=%s"
                % (
                    str(signal.stock_symbol or signal.underlying or "").strip().upper(),
                    str(signal.underlying or "").strip().upper(),
                    str(signal.direction or "").strip().lower(),
                    ",".join(blocked_reasons),
                )
            ],
        )
        _emit_options_trade_stock(log_dt, signal, reason_text)
        return False

    u_hold = str(signal.underlying or "").strip().upper()
    if u_hold and holding_equity_long_for_underlying(u_hold, positions, tracked):
        _log_option_entry_blocked(signal, stage="position_guard", reason="holding_equity_skip_option_overlay")
        _emit_options_trade_stock(
            log_dt,
            signal,
            "holding equity; skip option overlay",
        )
        return False

    intent, adapt_err = adapt_stock_signal_to_option_intent(
        config,
        underlying=signal.underlying,
        direction=signal.direction,
        source=signal.source,
        stock_symbol=signal.stock_symbol,
    )
    if intent is None:
        _log_option_entry_blocked(signal, stage="intent", reason=adapt_err or "could_not_build_option_intent")
        _emit_options_trade_stock(
            log_dt,
            signal,
            adapt_err or "could not build option intent",
        )
        return False

    n_chain = len(chain_candidates) if chain_candidates is not None else 0
    u_up = str(signal.underlying or intent.underlying or "").upper()
    _options_entry_log_lines(
        log_dt,
        [
            "evaluating options for %s (%s | chain=%d)"
            % (u_up, str(signal.direction or "").lower(), n_chain),
        ],
    )

    if account_equity is None or positions is None:
        _log_option_entry_blocked(
            signal,
            stage="select",
            reason="missing_account_equity_or_positions",
            detail="chain_n=%d" % n_chain,
        )
        _options_post_chain_skip(
            log_dt,
            signal.underlying,
            "select",
            "missing account_equity or positions for ranked selection and premium caps",
            detail="chain_n=%d want=%s %s" % (n_chain, intent.underlying, intent.right),
        )
        return False

    _as_of = log_dt.date() if log_dt is not None else date.today()
    if selected_override is not None:
        selected = selected_override
        sel_err = None
    else:
        selected, sel_err = find_option_under_budget(
            config,
            signal,
            chain_candidates=chain_candidates,
            underlying_spot=underlying_spot,
            equity=float(account_equity) if account_equity is not None else None,
            positions=positions,
            as_of=_as_of,
            tracked=tracked,
        )
    if selected is None:
        reject_reason = sel_err if sel_err is not None else "no_ranked_candidate_matched_budget_spread_liquidity"
        log.info(
            "OPTIONS_ALLOCATOR_REJECT symbol=%s reason=%s",
            _signal_symbol(signal),
            reject_reason,
        )
        if "exposure cap" in str(reject_reason).lower():
            log.warning(
                "OPTIONS_KILL_SWITCH triggered symbol=%s reason=%s",
                _signal_symbol(signal),
                reject_reason,
            )
        spot_s = (
            "%.6g" % float(underlying_spot)
            if underlying_spot is not None and float(underlying_spot) > 0
            else repr(underlying_spot)
        )
        _log_option_entry_blocked(
            signal,
            stage="select",
            reason=sel_err if sel_err is not None else "no_ranked_candidate_matched_budget_spread_liquidity",
            detail="chain_n=%d spot=%s" % (n_chain, spot_s),
        )
        _options_post_chain_skip(
            log_dt,
            signal.underlying,
            "select",
            sel_err if sel_err is not None else "(no ranked candidate matched budget/spread/liquidity)",
            detail="chain_n=%d spot=%s want=%s %s" % (n_chain, spot_s, intent.underlying, intent.right),
        )
        return False

    dte = (selected.expiration - _as_of).days
    dte_s = "%d DTE" % dte
    prem_1 = float(selected.mid) * 100.0
    record_options_contract_found(_signal_symbol(signal))
    log.info(
        "OPTIONS_CONTRACT_SELECTION symbol=%s underlying=%s contract=%s result=accepted expiry=%s strike=%.4g delta=%s bid=%.4g ask=%.4g spread_pct=%.4g rejection_reason=none",
        _signal_symbol(signal),
        str(signal.underlying or "").strip().upper(),
        selected.symbol,
        selected.expiration.isoformat(),
        float(selected.strike),
        "n/a",
        float(selected.bid),
        float(selected.ask),
        float(selected.spread_pct),
    )
    _options_entry_log_lines(
        log_dt,
        [
            "found candidate %s" % selected.right,
            "expiry = %s (%s)" % (selected.expiration.isoformat(), dte_s),
            "strike = %.4g" % float(selected.strike),
            "delta = n/a",
            "spread = %.4g%% (bid %.4g / ask %.4g)" % (selected.spread_pct, selected.bid, selected.ask),
            "mid = %.6g  (~$%.2f / contract @ mid)" % (selected.mid, prem_1),
        ],
    )

    cands = list(chain_candidates) if chain_candidates is not None else []
    prepared, contract_for_order, prep_err = prepare_option_order_premium_only_with_lower_strike_fallback(
        config,
        equity=float(account_equity),
        positions=positions,
        chain_candidates=cands,
        selected_atm=selected,
        intent_underlying=intent.underlying,
        intent_right=intent.right,
        as_of=_as_of,
    )
    if prepared is None:
        reject_reason = prep_err if prep_err is not None else "prepare_option_order_returned_none_with_no_reason"
        log.info(
            "OPTIONS_ALLOCATOR_REJECT symbol=%s reason=%s",
            _signal_symbol(signal),
            reject_reason,
        )
        if "exposure cap" in str(reject_reason).lower():
            log.warning(
                "OPTIONS_KILL_SWITCH triggered symbol=%s reason=%s",
                _signal_symbol(signal),
                reject_reason,
            )
        _log_option_entry_blocked(
            signal,
            stage="prepare",
            reason=reject_reason,
            detail="picked=%s" % selected.symbol,
        )
        _options_post_chain_skip(
            log_dt,
            signal.underlying,
            "prepare",
            prep_err
            if prep_err is not None
            else "(prepare_option_order_premium_only_with_lower_strike_fallback returned None with no reason)",
            detail="picked=%s strike=%s exp=%s mid=%.6g spread_pct=%.4g"
            % (
                selected.symbol,
                selected.strike,
                selected.expiration.isoformat(),
                selected.mid,
                selected.spread_pct,
            ),
        )
        return False

    if (
        contract_for_order is not None
        and contract_for_order.symbol != selected.symbol
    ):
        prem_alt = float(contract_for_order.mid) * 100.0
        _options_entry_log_lines(
            log_dt,
            [
                "ATM premium over budget; stepped to strike %.4g (%s, mid %.6g ~$%.2f/contract)"
                % (
                    float(contract_for_order.strike),
                    contract_for_order.symbol,
                    contract_for_order.mid,
                    prem_alt,
                ),
            ],
        )

    debit = float(prepared.mid) * float(prepared.contracts) * 100.0
    pilot_ok, pilot_reason, pilot_kill = _live_options_pilot_pre_submit_check(
        config,
        account_equity=float(account_equity),
        positions=positions,
        contracts=int(prepared.contracts),
        debit=float(debit),
    )
    if not pilot_ok:
        log.info(
            "OPTIONS_ALLOCATOR_REJECT symbol=%s reason=%s",
            _signal_symbol(signal),
            pilot_reason or "live_pilot_rejected",
        )
        if pilot_kill:
            log.warning(
                "OPTIONS_KILL_SWITCH triggered symbol=%s reason=%s",
                _signal_symbol(signal),
                pilot_reason or "options_exposure_cap",
            )
        _log_option_entry_blocked(
            signal,
            stage="pilot",
            reason=pilot_reason or "live_pilot_rejected",
            detail="%s x%d debit=%.2f" % (prepared.occ_symbol, prepared.contracts, debit),
        )
        _options_post_chain_skip(
            log_dt,
            signal.underlying,
            "pilot",
            pilot_reason or "live pilot rejected option order",
            detail="%s x%d debit=%.2f" % (prepared.occ_symbol, prepared.contracts, debit),
        )
        return False
    log.info(
        "OPTIONS_ALLOCATOR_ACCEPT symbol=%s contract=%s qty=%d debit=%.2f",
        _signal_symbol(signal),
        prepared.occ_symbol,
        int(prepared.contracts),
        float(debit),
    )
    log.info(
        "OPTIONS_ORDER_INTENT symbol=%s contract=%s side=buy qty=%d mid=%.6g debit=%.2f spread_pct=%.4g source=%s",
        _signal_symbol(signal),
        prepared.occ_symbol,
        int(prepared.contracts),
        float(prepared.mid),
        float(debit),
        float(prepared.spread_pct),
        str(signal.source or ""),
    )
    log.info(
        "OPTION_ORDER_INTENT symbol=%s contract=%s side=buy qty=%d mid=%.6g debit=%.2f spread_pct=%.4g source=%s",
        _signal_symbol(signal),
        prepared.occ_symbol,
        int(prepared.contracts),
        float(prepared.mid),
        float(debit),
        float(prepared.spread_pct),
        str(signal.source or ""),
    )
    log.info(
        "ORDER_INTENT symbol=%s side=buy qty=%d notional=%.2f source=%s",
        prepared.occ_symbol,
        int(prepared.contracts),
        float(debit),
        str(signal.source or "options"),
    )
    _options_entry_log_lines(
        log_dt,
        [
            "premium = $%.2f (%d contracts × mid %.6g × 100)"
            % (debit, prepared.contracts, prepared.mid),
            "submitting order — BUY %s × %d (limit/market per execution)"
            % (prepared.occ_symbol, prepared.contracts),
        ],
    )

    if broker is not None and execution_manager is not None:
        order, intended_limit_price, place_err = place_option_order(broker, execution_manager, prepared)
        if place_err:
            record_options_rejection(symbol=_signal_symbol(signal), stage="place", reason=place_err)
            log.info(
                "OPTIONS_FUNNEL underlying=%s underlyings_seen=%d chains_loaded=%d contracts_examined=%d contracts_rejected_quote=%d contracts_rejected_spread=%d contracts_rejected_volume=%d contracts_rejected_open_interest=%d contracts_rejected_delta=%d contracts_rejected_dte=%d contracts_selected=%d orders_submitted=%d orders_filled=%d orders_rejected=%d",
                _signal_symbol(signal),
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                1,
                0,
                0,
                1,
            )
            _log_option_entry_blocked(
                signal,
                stage="place",
                reason=place_err,
                detail="%s x%d" % (prepared.occ_symbol, prepared.contracts),
            )
            _options_post_chain_skip(
                log_dt,
                signal.underlying,
                "place",
                place_err if place_err is not None else "(place_option_order returned no error string)",
                detail="%s x%d @ mid=%.6g" % (prepared.occ_symbol, prepared.contracts, prepared.mid),
            )
            return False
        record_options_order_submitted(_signal_symbol(signal))
        log.info(
            "OPTIONS_ORDER_SUBMITTED symbol=%s contract=%s side=buy qty=%d order_id=%s status=%s limit_price=%s",
            _signal_symbol(signal),
            prepared.occ_symbol,
            int(prepared.contracts),
            str(getattr(order, "id", "") or ""),
            str(getattr(order, "status", "") or ""),
            "n/a" if intended_limit_price is None else "%.6g" % float(intended_limit_price),
        )
        log.info(
            "OPTION_ORDER_SUBMITTED symbol=%s contract=%s side=buy qty=%d order_id=%s status=%s limit_price=%s",
            _signal_symbol(signal),
            prepared.occ_symbol,
            int(prepared.contracts),
            str(getattr(order, "id", "") or ""),
            str(getattr(order, "status", "") or ""),
            "n/a" if intended_limit_price is None else "%.6g" % float(intended_limit_price),
        )
        _filled_qty = getattr(order, "filled_qty", None)
        _filled_avg = getattr(order, "filled_avg_price", None) or getattr(order, "filled_average_price", None)
        _order_status = str(getattr(order, "status", "") or "").lower()
        if _order_status == "rejected":
            record_options_rejection(symbol=_signal_symbol(signal), stage="broker", reason="order_submission_failed")
            log.info(
                "OPTIONS_ORDER_REJECTED symbol=%s contract=%s side=buy qty=%d order_id=%s reason=broker_rejected",
                _signal_symbol(signal),
                prepared.occ_symbol,
                int(prepared.contracts),
                str(getattr(order, "id", "") or ""),
            )
        log.info(
            "OPTIONS_FUNNEL underlying=%s underlyings_seen=%d chains_loaded=%d contracts_examined=%d contracts_rejected_quote=%d contracts_rejected_spread=%d contracts_rejected_volume=%d contracts_rejected_open_interest=%d contracts_rejected_delta=%d contracts_rejected_dte=%d contracts_selected=%d orders_submitted=%d orders_filled=%d orders_rejected=%d",
            _signal_symbol(signal),
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            1,
            1,
            1 if _order_status == "filled" or _filled_avg not in (None, "") else 0,
            1 if _order_status == "rejected" else 0,
        )
        if _order_status == "rejected":
            _log_option_entry_blocked(
                signal,
                stage="broker",
                reason="order_submission_failed",
                detail="%s x%d order_id=%s" % (
                    prepared.occ_symbol,
                    prepared.contracts,
                    str(getattr(order, "id", "") or ""),
                ),
            )
            return False
        record_options_order_accepted(_signal_symbol(signal))
        log.info(
            "ORDER_SUBMITTED symbol=%s side=buy order_id=%s status=%s qty=%d notional=%.2f source=%s",
            prepared.occ_symbol,
            str(getattr(order, "id", "") or ""),
            str(getattr(order, "status", "") or ""),
            int(prepared.contracts),
            float(debit),
            str(signal.source or "options"),
        )
        if str(getattr(order, "status", "") or "").lower() in {"filled", "partially_filled"} or _filled_avg not in (None, ""):
            record_options_fill(_signal_symbol(signal))
            log.info(
                "ORDER_FILLED symbol=%s side=buy filled_qty=%s filled_avg_price=%s order_id=%s",
                prepared.occ_symbol,
                str(_filled_qty if _filled_qty not in (None, "") else prepared.contracts),
                "n/a" if _filled_avg in (None, "") else str(_filled_avg),
                str(getattr(order, "id", "") or ""),
            )
        try:
            ts = log_dt.strftime("%H:%M ET") if log_dt is not None else ""
        except Exception:
            ts = ""
        oid = getattr(order, "id", "") if order is not None else ""
        if ts:
            print(
                ts,
                prepared.occ_symbol,
                "BUY",
                prepared.contracts,
                "contracts (options)",
                oid,
                flush=True,
            )
        else:
            print(
                prepared.occ_symbol,
                "BUY",
                prepared.contracts,
                "contracts (options)",
                oid,
                flush=True,
            )
        entry_fill = None
        if hasattr(broker, "resolve_entry_price_from_fill"):
            try:
                entry_fill = float(broker.resolve_entry_price_from_fill(order, fallback=float(intended_limit_price or prepared.mid)))
            except Exception:
                entry_fill = float(intended_limit_price or prepared.mid)
        else:
            try:
                entry_fill = float(getattr(order, "filled_avg_price", None) or getattr(order, "limit_price", None) or intended_limit_price or prepared.mid)
            except Exception:
                entry_fill = float(intended_limit_price or prepared.mid)
        entry_reason = (
            f"source={signal.source}; direction={signal.direction}; "
            f"conviction={signal.conviction_score if signal.conviction_score is not None else 'n/a'}; "
            f"news={signal.news_score if signal.news_score is not None else 'n/a'}; "
            f"event={signal.event_score if signal.event_score is not None else 'n/a'}; "
            f"rel_vol={signal.relative_volume if signal.relative_volume is not None else 'n/a'}; "
            f"catalyst={signal.catalyst_type or 'none'}"
        )
        try:
            opt_user_id = str(
                getattr(broker, "_sqlite_user_id", None)
                or getattr(execution_manager, "_sqlite_user_id", None)
                or "default"
            )
            record_options_entry(
                prepared.occ_symbol,
                user_id=opt_user_id,
                data_dir=getattr(execution_manager, "_options_data_dir", None),
                entry_reason=entry_reason,
                intended_limit_price=float(intended_limit_price or prepared.mid),
                entry_fill_price=entry_fill,
                quantity=int(prepared.contracts),
                contracts=int(prepared.contracts),
                premium_paid=float(prepared.contracts) * float(prepared.mid) * 100.0,
                quote_spread_pct=float(prepared.spread_pct),
                order_id=str(getattr(order, "id", "") or ""),
                order_status=str(getattr(order, "status", "") or ""),
            )
            log.info(
                "OPTION_POSITION_OPENED symbol=%s contract=%s qty=%d entry_fill_price=%.6g order_id=%s status=%s user_id=%s",
                _signal_symbol(signal),
                prepared.occ_symbol,
                int(prepared.contracts),
                float(entry_fill),
                str(getattr(order, "id", "") or ""),
                str(getattr(order, "status", "") or ""),
                opt_user_id,
            )
            log.info(
                "OPTIONS_POSITION_OPENED symbol=%s contract=%s qty=%d entry_fill_price=%.6g order_id=%s status=%s user_id=%s",
                _signal_symbol(signal),
                prepared.occ_symbol,
                int(prepared.contracts),
                float(entry_fill),
                str(getattr(order, "id", "") or ""),
                str(getattr(order, "status", "") or ""),
                opt_user_id,
            )
        except Exception as exc:
            log.warning(
                "OPTION_ENTRY_BLOCKED symbol=%s underlying=%s direction=%s stage=position_tracking reason=record_option_entry_exception detail=%s",
                _signal_symbol(signal),
                str(signal.underlying or "").strip().upper(),
                str(signal.direction or "").strip().lower(),
                type(exc).__name__,
            )
            pass
        return True

    _log_option_entry_blocked(
        signal,
        stage="submit",
        reason="broker_or_execution_manager_missing_after_prepare",
        detail="%s x%d" % (prepared.occ_symbol, prepared.contracts),
    )
    _options_post_chain_skip(
        log_dt,
        signal.underlying,
        "submit",
        "broker or execution_manager was None after prepare",
        detail="%s x%d" % (prepared.occ_symbol, prepared.contracts),
    )
    return False


def route_to_stock_executor(signal: EntryRouteSignal, execute_stock: Callable[[], None]) -> None:
    """Unchanged stock submission / tracking (caller supplies closure)."""
    execute_stock()
