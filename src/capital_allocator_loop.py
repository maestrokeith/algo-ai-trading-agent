"""
Live-loop glue: **allocator-only execution** when ``portfolio.capital_allocator.enabled``.

Canonical flow is implemented in :func:`execute_capital_allocator_pass`: build candidates and
portfolio from the post-scan *signals* list (in the live loop this list is only symbols with
``entry_eval`` final true: ``decision.allowed`` from :meth:`TradingEngine.run_entry_gates`),
``allocate(...)``, optional **net-by-symbol** consolidation
(see :func:`~src.capital_allocator.consolidate_allocator_actions_net_by_symbol`), print actions, then
for each action resolve NBBO and call :func:`place_order` (plus tracker refresh on fills).

Scan rows use ``sym_u`` / ``strength_eff``; :func:`build_allocator_candidates` maps them to
``symbol`` / ``score``.

For trend-long, when both **options** and the post-scan **equity** allocator are enabled, the
live loop uses :func:`trend_long_strength_uses_equity_allocator` so that **strong** signals
(``strength_eff > execution.strong_signal_strength_min``) are dispatched per-symbol
(options → stock) and only **at-or-below** that strength are queued for stock notional here.
"""
from __future__ import annotations

import logging
import json
import math
import re
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path
from typing import Any, Protocol

from src.application.services.execution_guard import apply_cooldown
from src.brokers.alpaca_client import AlpacaBroker
from src.adaptive import adaptive_effective_max_total_exposure
from src.portfolio.allocator_actions import (
    clip_buy_actions_to_gross_headroom_dollars,
    consolidate_allocator_actions_net_by_symbol,
    gross_book_near_effective_max_for_net_reduction,
    trim_allocator_actions_for_max_buy_to_sell_ratio,
    trim_allocator_actions_for_net_sell_gte_buy,
)
from src.portfolio.allocator_caps import (
    clip_allocator_buy_notionals_to_single_order_caps,
    effective_capital_allocator_symbol_cap_soft_hard,
    effective_capital_allocator_symbol_caps_by_symbol,
)
from src.portfolio.allocator_planner import CapitalAllocator
from src.portfolio.allocator_scoring import (
    apply_allocator_defensive_drift_scores,
    reorder_allocator_candidates_diversification,
)
from src.exposure_gates import parse_equity_fraction_optional, parse_portfolio_exposure_gates
from src.exposure import ETF_SYMBOLS, SYMBOL_SECTOR, THEME_MAP
from src.dynamic_price import effective_dynamic_min_price
from src.options_premium_risk import is_option_symbol
from src.execution import OrderRequest, OrderType
from src.controlled_live_equity import (
    bounded_live_pilot_active,
    controlled_live_equity_active,
    controlled_live_limits,
)
from src.limited_live_pilot import adjust_pilot_order_size
from src.risk_limits import risk_no_recycle_above_frac, tracked_add_on_count_for_et_day
from src.sector_config import parse_sector_config
from src.trading_control import EntryBlocked, is_expected_entry_block
from src.allocation_config import parse_allocation_config
from src.portfolio_allocation import symbol_long_position_market_value_usd
from src.portfolio_replacement import effective_signal_strength, tracked_signal_strength
from src.allocation_profile import (
    allocation_target_fractions,
    clip_actions_for_allocation_profile,
    deployable_cash_after_reserve,
    dynamic_lockout_reason,
    dynamic_position_count,
    dynamic_position_value,
    dynamic_quality_decision,
    dynamic_spread_cap_pct,
    filter_allocator_candidates_for_profile,
    is_excluded_dynamic_etf,
    is_core_stock,
    is_dynamic_candidate as allocation_profile_is_dynamic_candidate,
    log_allocation_targets,
    normalize_strategy_route,
    tracked_row_is_dynamic,
)
from src.signal_ranking import (
    SIGNAL_RANKING_MODE_MVE,
    SIGNAL_RANKING_MODE_MRV,
    SIGNAL_RANKING_MODE_SIGNAL_PRIORITY,
    canonical_signal_ranking_mode,
    row_composite_score,
    row_momentum_volume_ema_score,
    row_momentum_rs_volume_score,
    row_signal_priority_score,
)
from src.position_tracker import (
    add as add_tracked,
    load as load_tracked,
    merge_add_shares as merge_add_tracked,
    minutes_since_iso,
    reconcile as reconcile_tracked,
)
from src.position_state_machine import blocks_stock_rebuy_after_sell, record_sell_after_exit
from src.news_sentiment.rules import evaluate_high_conviction_news_override
from src.trade_attribution import (
    attribution_daily_path,
    load_daily_artifact,
    record_allocator_candidate as record_trade_attribution_allocator_candidate,
    record_candidate as record_trade_attribution_candidate,
    record_order_event as record_trade_attribution_order_event,
)
from src.research_bars import capture_runtime_forward_bars
from src.universe import last_bar_volume_from_ohlcv
from src.safe_sell import is_full_exit_reason, submit_fractional_full_close

log = logging.getLogger(__name__)
logger = log  # structured allocator trace tags ([ALLOCATOR_*])
_ALLOCATOR_EMPTY_ACTION_CYCLES: dict[str, int] = {}
_ALLOCATOR_SYMBOL_BLOCK_UNTIL: dict[str, datetime] = {}
_DYNAMIC_WEAK_CATALYST_EXECUTION_COOLDOWN_UNTIL: dict[str, datetime] = {}
_LIVE_WEAK_CATALYST_EXCEPTION_DAY_COUNTS: dict[str, int] = {}
ALLOCATOR_DYNAMIC_CANDIDATE_SCORE_MULT = 1.35
_WEAK_CATALYST_EXECUTION_COOLDOWN_REASON = "weak_catalyst_dynamic_non_exceptional_live"
_DYNAMIC_DISPATCH_METADATA_KEYS = (
    "relative_volume",
    "rel_volume",
    "scanner_relative_volume",
    "scanner_rel_volume",
    "entry_relative_volume",
    "entry_rel_volume",
    "allocator_relative_volume",
    "allocator_rel_volume",
    "execution_relative_volume",
    "execution_rel_volume",
    "dispatch_relative_volume",
    "dispatch_rel_volume",
    "effective_min_rel_volume",
    "effective_min_relative_volume",
    "scanner_effective_min_rel_volume",
    "scanner_effective_min_relative_volume",
    "entry_effective_min_rel_volume",
    "entry_eval_effective_min_rel_volume",
    "entry_effective_min_relative_volume",
    "allocator_effective_min_rel_volume",
    "allocator_effective_min_relative_volume",
    "dispatch_effective_min_rel_volume",
    "dispatch_effective_min_relative_volume",
    "catalyst_fastlane_active",
    "catalyst_min_relative_volume",
    "catalyst_min_rel_volume",
    "fastlane_min_relative_volume",
    "gain_pct",
    "day_gain_pct",
    "dynamic_score",
    "scanner_score",
    "signal_score",
    "route",
    "source",
    "is_dynamic",
    "dynamic_candidate",
    "dynamic_symbol",
    "weak_catalyst_dynamic",
    "news_score",
    "catalyst_score",
    "event_score",
    "article_count",
    "catalyst_type",
    "catalyst_headline",
    "catalyst_age_minutes",
    "premarket_injected",
    "age_minutes",
    "entry_eval_route",
    "entry_eval_final",
    "decision_allowed",
    "final",
    "volume_confirmed",
    "relative_volume_confirmed",
    "price_filter_passed",
    "scanner_vwap_above",
    "scanner_price_above_vwap",
    "entry_vwap_above",
    "entry_price_above_vwap",
    "allocator_vwap_above",
    "allocator_price_above_vwap",
    "order_vwap_above",
    "order_price_above_vwap",
    "vwap_above",
    "price_above_vwap",
    "paper_current_price",
    "paper_session_vwap",
    "session_vwap",
    "vwap",
    "distance_from_vwap_pct",
    "dynamic_scan_ms",
    "dynamic_enqueue_ms",
    "dynamic_entry_eval_ms",
    "dynamic_allocator_ms",
    "first_seen_time",
    "first_seen_day_gain_pct",
    "max_day_gain_pct_seen",
    "minutes_since_first_seen",
    "minutes_since_market_open",
    "first_eligible_time",
    "first_eligible_day_gain_pct",
    "pure_momentum_override",
    "pure_momentum_allowed",
    "catalyst_rvol_override",
    "rvol_override_active",
)


def _allocator_actions_repr(actions: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for action in actions or []:
        if isinstance(action, Mapping):
            out.append(dict(action))
    return out


def _log_order_skip(
    symbol: str,
    reason: str,
    *,
    source: str = "capital_allocator",
    fields: Mapping[str, Any] | None = None,
) -> None:
    sym = str(symbol or "?").strip().upper() or "?"
    reason_clean = str(reason or "unknown")
    source_clean = str(source or "capital_allocator")
    line = "ORDER_SKIP symbol=%s reason=%s source=%s" % (
        sym,
        reason_clean,
        source_clean,
    )
    if isinstance(fields, Mapping):
        extra: list[str] = []
        for key, value in fields.items():
            key_clean = str(key or "").strip()
            if not key_clean:
                continue
            text = str(value if value is not None else "n/a").replace(" ", "_")
            extra.append("%s=%s" % (key_clean, text))
        if extra:
            line = "%s %s" % (line, " ".join(extra))
    print(line, flush=True)
    log.info(line)


def _allocator_block_key(user_id: Any, symbol: str) -> str:
    return "%s:%s" % (str(user_id or "default"), str(symbol or "").strip().upper())


def _allocator_block_ttl_min(config: Mapping[str, Any] | None, ca_cfg: Mapping[str, Any] | None) -> float:
    cfg = ca_cfg if isinstance(ca_cfg, Mapping) else {}
    raw = cfg.get("illiquid_symbol_block_ttl_min")
    if raw is None and isinstance(config, Mapping):
        raw_ca = config.get("capital_allocator")
        if isinstance(raw_ca, Mapping):
            raw = raw_ca.get("illiquid_symbol_block_ttl_min")
    try:
        ttl = float(raw if raw is not None else 15.0)
    except (TypeError, ValueError):
        ttl = 15.0
    return max(1.0, min(240.0, ttl))


def _allocator_now_utc(dt: Any) -> datetime:
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _allocator_symbol_block_active(
    *,
    user_id: Any,
    symbol: str,
    now: datetime,
) -> bool:
    until = _ALLOCATOR_SYMBOL_BLOCK_UNTIL.get(_allocator_block_key(user_id, symbol))
    if until is None:
        return False
    if until <= now:
        _ALLOCATOR_SYMBOL_BLOCK_UNTIL.pop(_allocator_block_key(user_id, symbol), None)
        return False
    return True


def _allocator_block_symbol(
    *,
    user_id: Any,
    symbol: str,
    reason: str,
    ttl_min: float,
    now: datetime,
) -> None:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return
    until = now + timedelta(minutes=float(ttl_min))
    _ALLOCATOR_SYMBOL_BLOCK_UNTIL[_allocator_block_key(user_id, sym)] = until
    log.info(
        "ALLOCATOR_SYMBOL_BLOCKED symbol=%s reason=%s ttl_min=%.0f",
        sym,
        str(reason or "unknown"),
        float(ttl_min),
    )


def _dynamic_weak_catalyst_execution_cooldown_minutes(config: Mapping[str, Any] | None) -> float:
    dyn_cfg = (config or {}).get("dynamic_universe") if isinstance(config, Mapping) else {}
    dyn_cfg = dyn_cfg if isinstance(dyn_cfg, Mapping) else {}
    raw = dyn_cfg.get("weak_catalyst_execution_cooldown_minutes", 10)
    try:
        minutes = float(raw)
    except (TypeError, ValueError):
        minutes = 10.0
    return max(0.0, min(240.0, minutes))


def _dynamic_execution_cooldown_start(
    *,
    user_id: Any,
    symbol: str,
    now: datetime,
    minutes: float,
) -> None:
    sym = str(symbol or "").strip().upper()
    if not sym or minutes <= 0:
        return
    until = now + timedelta(minutes=float(minutes))
    _DYNAMIC_WEAK_CATALYST_EXECUTION_COOLDOWN_UNTIL[_allocator_block_key(user_id, sym)] = until
    log.info(
        "DYNAMIC_EXECUTION_COOLDOWN_START symbol=%s reason=%s minutes=%.0f expires_at=%s",
        sym,
        _WEAK_CATALYST_EXECUTION_COOLDOWN_REASON,
        float(minutes),
        until.isoformat(),
    )


def _dynamic_execution_cooldown_clear(
    *,
    user_id: Any,
    symbol: str,
    reason: str,
) -> None:
    sym = str(symbol or "").strip().upper()
    key = _allocator_block_key(user_id, sym)
    if not sym or key not in _DYNAMIC_WEAK_CATALYST_EXECUTION_COOLDOWN_UNTIL:
        return
    _DYNAMIC_WEAK_CATALYST_EXECUTION_COOLDOWN_UNTIL.pop(key, None)
    log.info(
        "DYNAMIC_EXECUTION_COOLDOWN_EXPIRED symbol=%s reason=%s",
        sym,
        str(reason or "cleared"),
    )


def _dynamic_execution_cooldown_active(
    *,
    user_id: Any,
    symbol: str,
    now: datetime,
) -> bool:
    sym = str(symbol or "").strip().upper()
    key = _allocator_block_key(user_id, sym)
    until = _DYNAMIC_WEAK_CATALYST_EXECUTION_COOLDOWN_UNTIL.get(key)
    if until is None:
        return False
    if until <= now:
        _DYNAMIC_WEAK_CATALYST_EXECUTION_COOLDOWN_UNTIL.pop(key, None)
        log.info(
            "DYNAMIC_EXECUTION_COOLDOWN_EXPIRED symbol=%s reason=elapsed",
            sym,
        )
        return False
    remaining = max(0.0, (until - now).total_seconds())
    log.info(
        "DYNAMIC_EXECUTION_COOLDOWN_ACTIVE symbol=%s reason=%s seconds_remaining=%.0f expires_at=%s",
        sym,
        _WEAK_CATALYST_EXECUTION_COOLDOWN_REASON,
        remaining,
        until.isoformat(),
    )
    return True


def _log_allocator_dispatch_start(
    symbol: str,
    *,
    action: str,
    notional: float,
    source: str,
) -> None:
    sym = str(symbol or "?").strip().upper() or "?"
    action_clean = str(action or "?").strip().lower() or "?"
    source_clean = str(source or "capital_allocator")
    line = (
        "ALLOCATOR_DISPATCH_START action=%s symbol=%s notional=%.2f source=%s"
        % (action_clean, sym, float(notional or 0.0), source_clean)
    )
    print(line, flush=True)
    log.info(line)
    log.info(
        "ALLOCATOR_DISPATCH_START symbol=%s action=%s notional=%.2f source=%s",
        sym,
        action_clean,
        float(notional or 0.0),
        source_clean,
    )


def _log_allocator_order_intent(
    symbol: str,
    *,
    side: str,
    notional: float,
    qty: int,
) -> None:
    sym = str(symbol or "?").strip().upper() or "?"
    side_clean = str(side or "?").strip().lower() or "?"
    line = "ALLOCATOR_ORDER_INTENT symbol=%s side=%s qty=%d notional=%.2f" % (
        sym,
        side_clean,
        int(qty or 0),
        float(notional or 0.0),
    )
    print(line, flush=True)
    log.info(line)
    log.info(
        "ALLOCATOR_ORDER_INTENT symbol=%s side=%s notional=%.2f qty=%d",
        sym,
        side_clean,
        float(notional or 0.0),
        int(qty or 0),
    )
    order_line = "ORDER_INTENT symbol=%s side=%s qty=%d notional=%.2f source=capital_allocator" % (
        sym,
        side_clean,
        int(qty or 0),
        float(notional or 0.0),
    )
    print(order_line, flush=True)
    log.info(order_line)
    log.info(
        "ORDER_INTENT symbol=%s side=%s notional=%.2f source=capital_allocator",
        sym,
        side_clean,
        float(notional or 0.0),
    )


def _allocator_order_status(order: Any) -> str:
    return str(getattr(order, "status", "") or "")


def _allocator_order_is_shadow(order: Any) -> bool:
    status = _allocator_order_status(order).strip().lower()
    order_id = _allocator_action_order_id(order).strip().lower()
    return status == "shadow" or order_id.startswith("shadow-")


def _allocator_order_filled_qty(order: Any, fallback_qty: Any = None) -> str | None:
    raw = getattr(order, "filled_qty", None)
    if raw in (None, ""):
        raw = fallback_qty
    if raw in (None, ""):
        return None
    return str(raw)


def _allocator_order_filled_avg_price(order: Any) -> float | None:
    for attr in ("filled_avg_price", "filled_average_price"):
        raw = getattr(order, attr, None)
        if raw in (None, ""):
            continue
        try:
            out = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(out) and out > 0.0:
            return out
    return None


def _log_allocator_order_filled_if_present(
    symbol: str,
    *,
    side: str,
    order: Any,
    fallback_qty: Any = None,
) -> None:
    status = _allocator_order_status(order).lower()
    filled_avg = _allocator_order_filled_avg_price(order)
    if status not in {"filled", "partially_filled"} and filled_avg is None:
        return
    filled_qty = _allocator_order_filled_qty(order, fallback_qty=fallback_qty)
    if filled_qty is None:
        return
    line = "ORDER_FILLED symbol=%s side=%s filled_qty=%s filled_avg_price=%s order_id=%s" % (
        str(symbol or "?").strip().upper() or "?",
        str(side or "?").strip().lower() or "?",
        filled_qty,
        "n/a" if filled_avg is None else "%.6g" % float(filled_avg),
        _allocator_action_order_id(order),
    )
    print(line, flush=True)
    log.info(line)


def _log_allocator_dispatch_skipped(symbol: str, *, reason: str) -> None:
    sym = str(symbol or "?").strip().upper() or "?"
    reason_clean = str(reason or "unknown")
    line = "ALLOCATOR_DISPATCH_SKIPPED symbol=%s reason=%s" % (sym, reason_clean)
    print(line, flush=True)
    log.info(line)


def _log_allocator_dispatch_error(symbol: str, *, reason: str) -> None:
    sym = str(symbol or "?").strip().upper() or "?"
    reason_clean = str(reason or "unknown")
    line = "ALLOCATOR_DISPATCH_ERROR symbol=%s reason=%s" % (sym, reason_clean)
    print(line, flush=True)
    log.info(line)


def _log_allocator_dispatch_blocked(symbol: str, *, reason: str) -> None:
    sym = str(symbol or "?").strip().upper() or "?"
    reason_clean = str(reason or "unknown")
    line = "ALLOCATOR_DISPATCH_BLOCKED symbol=%s reason=%s" % (sym, reason_clean)
    print(line, flush=True)
    log.info(line)


def _log_allocator_dispatch_done(
    symbol: str,
    *,
    result: str,
    reason: str,
) -> None:
    sym = str(symbol or "?").strip().upper() or "?"
    result_clean = str(result or "unknown")
    reason_clean = str(reason or "unknown")
    log.info(
        "ALLOCATOR_DISPATCH_DONE symbol=%s result=%s reason=%s",
        sym,
        result_clean,
        reason_clean,
    )
    line = "ALLOCATOR_DISPATCH_END symbol=%s result=%s reason=%s" % (
        sym,
        result_clean,
        reason_clean,
    )
    print(line, flush=True)
    log.info(line)


def _dispatch_explainability_rule_class(reason: Any) -> str:
    text = str(reason or "").strip().lower().replace(" ", "_")
    if not text or text == "submitted":
        return "n/a"
    safety_markers = (
        "bad_quote",
        "unstable_quote",
        "no_quote",
        "quote",
        "spread",
        "price_below_minimum",
        "relative_volume",
        "vwap",
        "cooldown",
        "exposure",
        "cap",
        "risk",
        "pdt",
        "position",
        "size_below",
        "execution_blocked",
    )
    strategy_markers = (
        "weak_catalyst_dynamic",
        "expectancy_gate",
        "trend_reentry",
        "late_entry",
        "profile_filter",
    )
    if any(marker in text for marker in safety_markers):
        return "safety_rule"
    if any(marker in text for marker in strategy_markers):
        return "strategy_rule"
    return "strategy_rule" if "dynamic" in text else "unknown_rule"


def _bool_for_log(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "t", "1", "yes", "on"}:
            return "true"
        if text in {"false", "f", "0", "no", "off"}:
            return "false"
    return str(bool(value)).lower()


def _dispatch_explainability_value(
    action: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *keys: str,
    default: Any = "n/a",
) -> Any:
    for source in (candidate, action):
        for key in keys:
            raw = source.get(key)
            if raw is not None and str(raw).strip() != "":
                return raw
    return default


def _log_dynamic_dispatch_explainability(
    symbol: str,
    *,
    action: Mapping[str, Any],
    candidate: Mapping[str, Any],
    route: str,
    notional: Any,
    result: str,
    reason: str,
) -> None:
    sym = str(symbol or "?").strip().upper() or "?"
    source = str(action.get("source") or candidate.get("source") or "capital_allocator")
    catalyst_fastlane = _dispatch_explainability_value(
        action,
        candidate,
        "catalyst_fastlane_active",
        "catalyst_fastlane",
        "fastlane_active",
        default=False,
    )
    weak_catalyst = _dispatch_explainability_value(
        action,
        candidate,
        "weak_catalyst_dynamic",
        "is_weak_catalyst_dynamic",
        default=False,
    )
    if str(weak_catalyst).lower() in {"n/a", "none", ""}:
        weak_catalyst = _allocator_weak_catalyst_dynamic(candidate)
    entry_final = _dispatch_explainability_value(
        action,
        candidate,
        "entry_eval_final",
        "entry_final",
        "final",
        default="n/a",
    )
    log.info(
        "DYNAMIC_DISPATCH_EXPLAINABILITY symbol=%s source=%s route=%s notional=%.2f "
        "scanner_score=%s dynamic_score=%s gain_pct=%s day_gain_pct=%s relative_volume=%s "
        "news_score=%s catalyst_score=%s catalyst_fastlane_active=%s weak_catalyst_dynamic=%s "
        "entry_eval_final=%s dispatcher_result=%s dispatcher_skip_reason=%s rule_class=%s",
        sym,
        source,
        route or "n/a",
        float(notional or 0.0),
        _dispatch_explainability_value(action, candidate, "scanner_score", "score", default="n/a"),
        _dispatch_explainability_value(action, candidate, "dynamic_score", "scanner_score", "score", default="n/a"),
        _dispatch_explainability_value(action, candidate, "gain_pct", default="n/a"),
        _dispatch_explainability_value(action, candidate, "day_gain_pct", "gain_pct", default="n/a"),
        _dispatch_explainability_value(action, candidate, "relative_volume", "rel_volume", default="n/a"),
        _dispatch_explainability_value(action, candidate, "news_score", default="n/a"),
        _dispatch_explainability_value(action, candidate, "catalyst_score", default="n/a"),
        _bool_for_log(catalyst_fastlane),
        _bool_for_log(weak_catalyst),
        str(entry_final).lower() if isinstance(entry_final, bool) else str(entry_final),
        result,
        reason or "n/a",
        _dispatch_explainability_rule_class(reason),
    )


def _log_dynamic_dispatch_latency(symbol: str, candidate: Mapping[str, Any]) -> None:
    if not allocation_profile_is_dynamic_candidate(candidate):
        return
    scan_ms = _allocator_diag_float(candidate, "dynamic_scan_ms")
    if scan_ms is None:
        return
    dispatch_ms = int(time.time() * 1000)

    def delta(start_key: str, end_key: str) -> int:
        start = _allocator_diag_float(candidate, start_key)
        end = dispatch_ms if end_key == "dispatch" else _allocator_diag_float(candidate, end_key)
        if start is None or end is None:
            return -1
        return max(0, int(float(end) - float(start)))

    log.info(
        "DYNAMIC_LATENCY symbol=%s scan_to_enqueue_ms=%d enqueue_to_eval_ms=%d "
        "eval_to_allocator_ms=%d allocator_to_dispatch_ms=%d total_ms=%d",
        str(symbol or "?").strip().upper() or "?",
        delta("dynamic_scan_ms", "dynamic_enqueue_ms"),
        delta("dynamic_enqueue_ms", "dynamic_entry_eval_ms"),
        delta("dynamic_entry_eval_ms", "dynamic_allocator_ms"),
        delta("dynamic_allocator_ms", "dispatch"),
        max(0, int(dispatch_ms - float(scan_ms))),
    )


def _entry_terminal_payload(row: Mapping[str, Any] | None, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if isinstance(row, Mapping):
        for key in (
            "source",
            "route",
            "dynamic_candidate",
            "entry_eval_final",
            "notional",
            "candidate_notional_requested",
            "target_notional",
            "strength_eff",
            "score",
            "signal_score",
            "dynamic_score",
            "scanner_score",
            "news_score",
            "event_score",
            "catalyst_score",
            "relative_volume",
            "rel_volume",
            "gain_pct",
            "day_gain_pct",
            "allocation_bucket",
            "is_dynamic",
            "dynamic_symbol",
        ):
            if key in row:
                payload[key] = row.get(key)
    payload.update({key: value for key, value in extra.items() if value is not None})
    return payload


def _record_entry_terminal_outcome(
    *,
    store: Any,
    user_id: str,
    symbol: str,
    route: str | None,
    stage: str,
    reason: str,
    payload: Mapping[str, Any] | None = None,
    ts: Any = None,
) -> None:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return
    log.info(
        "ENTRY_TERMINAL_OUTCOME symbol=%s stage=%s reason=%s route=%s",
        sym,
        stage,
        reason,
        route or "n/a",
    )
    recorder = getattr(store, "record_entry_terminal_outcome", None)
    if not callable(recorder):
        return
    try:
        recorder(
            user_id=user_id,
            symbol=sym,
            route=route,
            stage=stage,
            reason=reason,
            payload=payload or {},
            ts=ts.isoformat() if hasattr(ts, "isoformat") else None,
        )
    except Exception:
        log.debug("ENTRY_TERMINAL_OUTCOME_RECORD_FAILED symbol=%s stage=%s", sym, stage, exc_info=True)


def _cfg_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on", "y"}:
            return True
        if text in {"0", "false", "no", "off", "n", ""}:
            return False
    return bool(value)


def _allocator_signal_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def _allocator_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return out if math.isfinite(out) else float(default)


def _allocator_symbol_is_etf(symbol: Any) -> bool:
    return str(symbol or "").strip().upper() in ETF_SYMBOLS


def _allocator_candidates_all_etfs(candidates: Sequence[Mapping[str, Any]] | None) -> bool:
    rows = list(candidates or [])
    return bool(rows) and all(
        _allocator_symbol_is_etf(row.get("symbol")) for row in rows if isinstance(row, Mapping)
    )


def _allocator_symbol_csv(rows: Sequence[Mapping[str, Any]] | None) -> str:
    symbols: list[str] = []
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        sym = str(row.get("symbol") or row.get("sym_u") or "").strip().upper()
        if sym and sym not in symbols:
            symbols.append(sym)
    return ",".join(symbols) or "none"


def _allocator_field_csv(
    rows: Sequence[Mapping[str, Any]] | None,
    *keys: str,
    default: str = "n/a",
) -> str:
    values: list[str] = []
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        value: Any = None
        for key in keys:
            candidate = row.get(key)
            if candidate is not None and str(candidate).strip() != "":
                value = candidate
                break
        if value is None:
            values.append(default)
            continue
        if isinstance(value, float):
            values.append(f"{value:.4f}")
        else:
            try:
                values.append(f"{float(value):.4f}")
            except (TypeError, ValueError):
                values.append(str(value))
    return ",".join(values) or default


def _allocator_candidate_is_dynamic(candidate: Mapping[str, Any]) -> bool:
    return bool(allocation_profile_is_dynamic_candidate(candidate))


def _allocator_candidate_bucket(candidate: Mapping[str, Any]) -> str:
    if _allocator_candidate_is_dynamic(candidate):
        return "dynamic"
    if is_core_stock(candidate.get("symbol")):
        return "core"
    return "other"


def _log_allocator_candidate_row(candidate: Mapping[str, Any], *, stage: str) -> None:
    sym = str(candidate.get("symbol") or candidate.get("sym_u") or "").strip().upper()
    rel_volume = _allocator_diag_field(
        candidate,
        "relative_volume",
        "rel_volume",
        default="n/a",
    )
    signal_score = _allocator_diag_field(
        candidate,
        "signal_score",
        "strength_eff",
        "score",
        default="n/a",
    )
    log.info(
        "ALLOCATOR_CANDIDATE_ROW stage=%s symbol=%s is_dynamic=%s news_score=%s catalyst_score=%s "
        "event_score=%s signal_score=%s relative_volume=%s allocation_bucket=%s",
        stage,
        sym or "?",
        str(_allocator_candidate_is_dynamic(candidate)).lower(),
        _allocator_diag_field(candidate, "news_score"),
        _allocator_diag_field(candidate, "catalyst_score"),
        _allocator_diag_field(candidate, "event_score"),
        signal_score,
        rel_volume,
        _allocator_candidate_bucket(candidate),
    )


def _log_allocator_reject_reason(
    candidate: Mapping[str, Any],
    *,
    reason: str,
    stage: str,
) -> None:
    sym = str(candidate.get("symbol") or candidate.get("sym_u") or "").strip().upper()
    log.info(
        "ALLOCATOR_REJECT_REASON symbol=%s reason=%s stage=%s score=%s catalyst_score=%s "
        "event_score=%s news_score=%s age_minutes=%s route=%s",
        sym or "?",
        reason,
        stage,
        _allocator_diag_field(candidate, "score"),
        _allocator_diag_field(candidate, "catalyst_score"),
        _allocator_diag_field(candidate, "event_score"),
        _allocator_diag_field(candidate, "news_score"),
        _allocator_diag_field(candidate, "age_minutes", "catalyst_age_minutes", default="n/a"),
        _allocator_diag_field(candidate, "route", "source", default="n/a"),
    )
    log.info(
        "ALLOCATOR_DROPPED symbol=%s reason=%s stage=%s",
        sym or "?",
        reason,
        stage,
    )


_DYNAMIC_HARD_LIQUIDITY_REASONS = {
    "no_quote",
    "no quote",
    "bad_quote",
    "bad quote",
    "invalid_quote",
    "unstable_quote",
    "unstable quote",
    "below_min_avg_volume",
    "below_min_price",
    "spread too wide",
    "spread_too_wide",
    "dynamic_spread_cap",
    "dynamic_unstable_quote",
    "dynamic_price_below_minimum",
}


def _allocator_candidate_reject_hard_liquidity(
    candidate: Mapping[str, Any],
    *,
    detail: str,
) -> None:
    sym = str(candidate.get("symbol") or candidate.get("sym_u") or "").strip().upper()
    log.info(
        "ALLOCATOR_CANDIDATE_REJECT symbol=%s reason=hard_liquidity_gate detail=%s",
        sym or "?",
        str(detail or "unknown"),
    )
    log.info(
        "ALLOCATOR_SKIP symbol=%s reason=%s",
        sym or "?",
        str(detail or "hard_liquidity_gate"),
    )


def _candidate_number(candidate: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key not in candidate:
            continue
        raw = candidate.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            return value
    return None


def _dynamic_min_price_from_config(
    config: Mapping[str, Any] | None,
    *,
    broker_is_paper: bool,
) -> float:
    return effective_dynamic_min_price(config, broker_is_paper=broker_is_paper)


def _dynamic_dispatch_price_context(
    candidate: Mapping[str, Any],
    *,
    quote_mid: float,
    config: Mapping[str, Any] | None,
    broker_is_paper: bool,
) -> dict[str, Any]:
    price_keys = (
        "paper_current_price",
        "current_price",
        "price",
        "scanner_price",
        "entry_price",
        "last_price",
    )
    observed = _candidate_number(candidate, *price_keys)
    source = "candidate"
    if observed is None or observed <= 0.0:
        observed = float(quote_mid or 0.0)
        source = "quote_mid"
    return {
        "observed_price": float(observed or 0.0),
        "min_price": _dynamic_min_price_from_config(config, broker_is_paper=broker_is_paper),
        "price_source": source,
    }


def _quote_bad_or_unstable_reason(quote: Any, *, stale_quote_max_age: float) -> str | None:
    if quote is None:
        return "no_quote"
    bid = getattr(quote, "bid", None)
    ask = getattr(quote, "ask", None)
    mid = getattr(quote, "mid", None)
    try:
        bid_f = float(bid) if bid is not None else 0.0
        ask_f = float(ask) if ask is not None else 0.0
        mid_f = float(mid) if mid is not None else 0.0
    except (TypeError, ValueError):
        return "bad_quote"
    if bid_f <= 0.0 or ask_f <= 0.0 or ask_f < bid_f:
        return "bad_quote"
    if mid_f <= 0.0:
        return "bad_quote"
    is_stale = getattr(quote, "is_stale", None)
    if callable(is_stale):
        try:
            if bool(is_stale(stale_quote_max_age)):
                return "unstable_quote"
        except Exception:
            return "unstable_quote"
    if bool(getattr(quote, "skip_spread_check", False)):
        return "unstable_quote"
    return None


def _allocator_hard_liquidity_reject_reason(
    candidate: Mapping[str, Any],
    *,
    broker: Any,
    config: Mapping[str, Any] | None,
    ca_cfg: Mapping[str, Any] | None,
    user_id: Any,
    now: datetime,
    stale_quote_max_age: float,
) -> str | None:
    if not allocation_profile_is_dynamic_candidate(candidate):
        return None
    sym = str(candidate.get("symbol") or candidate.get("sym_u") or "").strip().upper()
    if not sym:
        return None
    if _allocator_symbol_block_active(user_id=user_id, symbol=sym, now=now):
        return "blocked_after_no_quote"
    raw_reason = str(
        candidate.get("rejection_reason")
        or candidate.get("skip_reason")
        or candidate.get("hard_reject_reason")
        or ""
    ).strip()
    raw_reason_l = raw_reason.lower()
    if raw_reason_l in _DYNAMIC_HARD_LIQUIDITY_REASONS:
        return raw_reason_l.replace(" ", "_")
    du_cfg = config.get("dynamic_universe") if isinstance(config, Mapping) else {}
    du_cfg = du_cfg if isinstance(du_cfg, Mapping) else {}
    try:
        min_avg_volume = float(du_cfg.get("min_avg_volume", 0.0) or 0.0)
    except (TypeError, ValueError):
        min_avg_volume = 0.0
    avg_volume = _candidate_number(candidate, "avg_volume", "average_volume")
    if min_avg_volume > 0.0 and (avg_volume is None or avg_volume < min_avg_volume - 1e-9):
        if avg_volume is not None:
            return "below_min_avg_volume avg=%s min=%s" % (
                "%.0f" % avg_volume,
                "%.0f" % min_avg_volume,
            )
    try:
        min_price = float(du_cfg.get("min_price", 0.0) or 0.0)
    except (TypeError, ValueError):
        min_price = 0.0
    price = _candidate_number(candidate, "price", "current_price", "paper_current_price")
    if min_price > 0.0 and price is not None and price < min_price - 1e-9:
        return "below_min_price price=%.2f min=%.2f" % (price, min_price)
    should_probe_quote = any(
        key in candidate
        for key in (
            "avg_volume",
            "average_volume",
            "price",
            "current_price",
            "paper_current_price",
            "spread_pct",
        )
    )
    quote = None
    if should_probe_quote and broker is not None and hasattr(broker, "get_latest_quote"):
        try:
            quote = broker.get_latest_quote(sym)
        except Exception:
            quote = None
    if should_probe_quote:
        quote_reason = _quote_bad_or_unstable_reason(quote, stale_quote_max_age=stale_quote_max_age)
        if quote_reason is not None:
            return quote_reason
    spread = _candidate_number(candidate, "spread_pct")
    if spread is None and quote is not None and getattr(quote, "spread_pct", None) is not None:
        try:
            spread = float(quote.spread_pct)
        except (TypeError, ValueError):
            spread = None
    if should_probe_quote or "spread_pct" in candidate:
        spread_cap = dynamic_spread_cap_pct(candidate)
        if spread is None or not math.isfinite(float(spread)) or float(spread) > spread_cap + 1e-9:
            return "spread too wide spread=%s max=%.2f" % (
                "n/a" if spread is None else "%.2f%%" % float(spread),
                float(spread_cap),
            )
    return None


def _filter_allocator_hard_liquidity_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    broker: Any,
    config: Mapping[str, Any] | None,
    ca_cfg: Mapping[str, Any] | None,
    user_id: Any,
    dt: Any,
    stale_quote_max_age: float,
    event_store: Any,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    now = _allocator_now_utc(dt)
    kept: list[dict[str, Any]] = []
    rejected: dict[str, str] = {}
    for candidate in candidates or []:
        row = dict(candidate)
        sym = str(row.get("symbol") or row.get("sym_u") or "").strip().upper()
        reason = _allocator_hard_liquidity_reject_reason(
            row,
            broker=broker,
            config=config,
            ca_cfg=ca_cfg,
            user_id=user_id,
            now=now,
            stale_quote_max_age=stale_quote_max_age,
        )
        if reason is None:
            kept.append(row)
            continue
        rejected[sym] = reason
        _allocator_candidate_reject_hard_liquidity(row, detail=reason)
        if reason in {"no_quote", "bad_quote", "unstable_quote"}:
            _allocator_block_symbol(
                user_id=user_id,
                symbol=sym,
                reason=reason,
                ttl_min=_allocator_block_ttl_min(config, ca_cfg),
                now=now,
            )
        if reason == "blocked_after_no_quote":
            log.info("ALLOCATOR_SKIP symbol=%s reason=blocked_after_no_quote", sym or "?")
        _record_entry_terminal_outcome(
            store=event_store,
            user_id=str(user_id),
            symbol=sym,
            route=str(row.get("route") or row.get("source") or "allocator"),
            stage="skipped_with_reason",
            reason=reason,
            payload=_entry_terminal_payload(row, hard_liquidity_gate=True),
            ts=dt,
        )
    return kept, rejected


def _etf_fallback_block_reason(
    candidates: Sequence[Mapping[str, Any]] | None,
    ca_cfg: Mapping[str, Any] | None,
) -> str | None:
    if not _allocator_candidates_all_etfs(candidates):
        return None
    cfg = ca_cfg if isinstance(ca_cfg, Mapping) else {}
    if _cfg_bool(cfg.get("news_candidates_present"), default=False) and _cfg_bool(
        cfg.get("etf_fallback_only_when_no_news_candidates"),
        default=True,
    ):
        return "news_candidates_present"
    if not _cfg_bool(cfg.get("etf_fallback_enabled"), default=False):
        return "config_disabled"
    return None


def _etf_fallback_max_notional_fraction(raw: Any) -> float | None:
    """Parse ETF fallback max notional as percent points; ``1`` means 1%."""
    if raw is None or str(raw).strip() == "":
        return None
    s = str(raw).strip()
    if s.endswith("%"):
        s = s[:-1].strip()
    try:
        value = float(s)
    except (TypeError, ValueError):
        return None
    if value != value or value <= 0.0:
        return None
    return value / 100.0


def _etf_fallback_cash_limit(cash: float, equity: float, ca_cfg: Mapping[str, Any] | None) -> float:
    cfg = ca_cfg if isinstance(ca_cfg, Mapping) else {}
    cap_frac = _etf_fallback_max_notional_fraction(cfg.get("etf_fallback_max_notional_pct"))
    if cap_frac is None or cap_frac <= 0:
        return max(0.0, float(cash))
    return max(0.0, min(float(cash), max(0.0, float(equity)) * float(cap_frac)))


def _print_allocator_skip(symbol: str, reason: str, *, detail: str | None = None) -> None:
    sym_u = str(symbol or "").strip().upper()
    if not sym_u:
        return
    msg = f"SKIP {sym_u}: reason={reason}"
    if detail:
        msg += f" detail={detail}"
    print(msg, flush=True)


def _allocator_bulk_cooldown_state(exit_context: Any | None, symbol: str) -> tuple[bool, str, str]:
    sym_u = str(symbol or "").strip().upper()
    if not sym_u or exit_context is None:
        return False, "n/a", "n/a"
    active = False
    reason = "n/a"
    fn = getattr(exit_context, "bulk_trim_buy_cooldown_active", None)
    if callable(fn):
        try:
            active_raw, reason_raw = fn(sym_u)
            active = bool(active_raw)
            reason = str(reason_raw or "n/a")
        except Exception:
            reason = "cooldown_check_error"
    next_eligible = "n/a"
    raw_until = getattr(exit_context, "_bulk_trim_buy_block_until", None)
    if isinstance(raw_until, Mapping):
        until = raw_until.get(sym_u)
        if until is not None:
            next_eligible = str(until)
    return active, reason, next_eligible


def _allocator_daily_loss_lockout_state(
    *,
    allow_allocator_buys: bool,
    cycle_risk_state: Mapping[str, Any] | None,
) -> tuple[bool, str]:
    state = cycle_risk_state if isinstance(cycle_risk_state, Mapping) else {}
    for key in (
        "daily_loss_lockout",
        "daily_loss_locked",
        "daily_loss_lockout_active",
        "daily_loss_limit_hit",
    ):
        if key in state:
            return _cfg_bool(state.get(key), default=False), key
    if not allow_allocator_buys:
        return True, "allow_allocator_buys_false"
    return False, "not_reported"


def _allocator_high_conviction_cfg(
    config: Mapping[str, Any] | None,
    ca_cfg: Mapping[str, Any] | None,
) -> dict[str, Any]:
    trading = (config or {}).get("trading") if isinstance(config, Mapping) else {}
    trading = trading if isinstance(trading, Mapping) else {}
    dyn = trading.get("dynamic") if isinstance(trading.get("dynamic"), Mapping) else {}
    raw = dyn.get("high_conviction_news_override") if isinstance(dyn, Mapping) else {}
    cfg = raw if isinstance(raw, Mapping) else {}
    normal_ratio = _allocator_float(
        cfg.get(
            "normal_replacement_strength_ratio",
            (ca_cfg or {}).get("replacement_strength_ratio", 1.0) if isinstance(ca_cfg, Mapping) else 1.0,
        ),
        _allocator_float((ca_cfg or {}).get("replacement_strength_ratio", 1.0) if isinstance(ca_cfg, Mapping) else 1.0, 1.0),
    )
    return {
        "enabled": _cfg_bool(cfg.get("enabled"), default=False),
        "min_catalyst_score": _allocator_float(cfg.get("min_catalyst_score", 8.0), 8.0),
        "min_event_score": _allocator_float(cfg.get("min_event_score", 7.0), 7.0),
        "min_news_score": _allocator_float(cfg.get("min_news_score", 7.0), 7.0),
        "min_relative_volume": _allocator_float(cfg.get("min_relative_volume", 1.5), 1.5),
        "max_catalyst_age_minutes": _allocator_float(cfg.get("max_catalyst_age_minutes", 180.0), 180.0),
        "thresholds": cfg.get("thresholds") if isinstance(cfg.get("thresholds"), Mapping) else {},
        "replacement_strength_ratio": _allocator_float(cfg.get("replacement_strength_ratio", 1.05), 1.05),
        "normal_replacement_strength_ratio": normal_ratio,
        "require_positive_sentiment": _cfg_bool(cfg.get("require_positive_sentiment"), default=True),
    }


def _allocator_scaled_catalyst_score(value: Any) -> float:
    score = _allocator_float(value, 0.0)
    if 0.0 < score <= 1.0:
        return score * 10.0
    return score


def _allocator_candidate_high_conviction(
    candidate: Mapping[str, Any],
    hc_cfg: Mapping[str, Any],
) -> tuple[bool, str]:
    if not _cfg_bool(hc_cfg.get("enabled"), default=False):
        return False, "override_disabled"
    if not allocation_profile_is_dynamic_candidate(candidate):
        return False, "not_dynamic_candidate"
    rel_volume = max(
        _allocator_float(candidate.get("relative_volume"), 0.0),
        _allocator_float(candidate.get("rel_volume"), 0.0),
    )
    sentiment_raw = None
    for key in ("sentiment", "sentiment_score", "news_sentiment"):
        if key in candidate:
            sentiment_raw = candidate.get(key)
            break
    if isinstance(sentiment_raw, str):
        sentiment_value = -1.0 if sentiment_raw.strip().lower() in {"negative", "bearish", "bad"} else 1.0
    else:
        sentiment_value = 1.0 if sentiment_raw is None else sentiment_raw
    allowed, reason, _score, _thresholds = evaluate_high_conviction_news_override(
        {"trading": {"dynamic": {"high_conviction_news_override": hc_cfg}}},
        catalyst_type=candidate.get("catalyst_type") or "earnings_beat",
        news_score=candidate.get("news_score"),
        event_score=candidate.get("event_score"),
        catalyst_score=candidate.get("catalyst_score"),
        relative_volume=rel_volume,
        sentiment=sentiment_value,
        catalyst_age_minutes=candidate.get("catalyst_age_minutes") or candidate.get("age_minutes"),
    )
    if not allowed:
        if reason == "score_below_threshold":
            return False, "below_high_conviction_score_threshold"
        if reason == "non_positive_sentiment":
            return False, "negative_sentiment"
        return False, reason
    for key in ("volume_confirmed", "relative_volume_confirmed", "liquidity_confirmed"):
        if key in candidate and not _cfg_bool(candidate.get(key), default=False):
            return False, f"{key}_false"
    for key in ("vwap_above", "price_above_vwap", "price_filter_passed"):
        if key in candidate and not _cfg_bool(candidate.get(key), default=False):
            return False, f"{key}_false"
    return True, "high_conviction_catalyst"


def _allocator_premarket_catalyst_replay_bypasses_rvol(candidate: Mapping[str, Any]) -> bool:
    route = str(candidate.get("route") or candidate.get("source") or "").strip().lower()
    if route != "premarket_catalyst_replay":
        return False
    return (
        _allocator_float(candidate.get("catalyst_score"), 0.0) >= 0.3 - 1e-9
        and _allocator_float(candidate.get("event_score"), 0.0) >= 3.0 - 1e-9
        and _allocator_float(candidate.get("news_score"), 0.0) >= 3.0 - 1e-9
    )


def _allocator_diag_value(candidate: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in candidate and candidate.get(key) is not None and str(candidate.get(key)).strip() != "":
            return candidate.get(key)
    return None


def _allocator_diag_float(candidate: Mapping[str, Any], *keys: str) -> float | None:
    value = _allocator_diag_value(candidate, *keys)
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _allocator_diag_text(candidate: Mapping[str, Any], *keys: str) -> str:
    value = _allocator_diag_value(candidate, *keys)
    return str(value).strip() if value is not None else "n/a"


def _dynamic_dispatch_diagnostics_enabled(
    *,
    user_id: Any,
    config: Mapping[str, Any] | None,
) -> bool:
    uid = str(user_id or "").strip().lower()
    if "paper" in uid or "test" in uid:
        return True
    replay_mode = str((config or {}).get("_replay_mode") or "").strip().lower()
    if replay_mode and replay_mode != "live":
        return True
    return _cfg_bool((config or {}).get("_broker_mock"), default=False)


def _allocator_paper_execution_context(
    *,
    user_id: Any,
    config: Mapping[str, Any] | None,
) -> bool:
    uid = str(user_id or "").strip().lower()
    if "paper" in uid or "test" in uid:
        return True
    cfg = config or {}
    broker_cfg = cfg.get("broker") if isinstance(cfg, Mapping) else {}
    if isinstance(broker_cfg, Mapping) and _cfg_bool(broker_cfg.get("paper"), default=False):
        return True
    replay_mode = str(cfg.get("_replay_mode") or "").strip().lower() if isinstance(cfg, Mapping) else ""
    if replay_mode and replay_mode != "live":
        return True
    return _cfg_bool(cfg.get("_broker_mock") if isinstance(cfg, Mapping) else None, default=False)


def _paper_dynamic_churn_guard_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    root = config if isinstance(config, Mapping) else {}
    du_cfg = root.get("dynamic_universe") if isinstance(root.get("dynamic_universe"), Mapping) else {}
    cfg = du_cfg.get("paper_churn_guard") if isinstance(du_cfg.get("paper_churn_guard"), Mapping) else {}

    def _float(key: str, default: float) -> float:
        try:
            value = float(cfg.get(key, default))
        except (TypeError, ValueError, OverflowError):
            return float(default)
        return value if math.isfinite(value) else float(default)

    def _int(key: str, default: int) -> int:
        try:
            return int(float(cfg.get(key, default)))
        except (TypeError, ValueError, OverflowError):
            return int(default)

    return {
        "enabled": _cfg_bool(cfg.get("enabled"), default=True),
        "flip_window_minutes": max(1.0, _float("flip_window_minutes", 45.0)),
        "max_reversals_in_window": max(1, _int("max_reversals_in_window", 1)),
        "weak_exit_reentry_cooldown_minutes": max(
            1.0,
            _float("weak_exit_reentry_cooldown_minutes", 180.0),
        ),
        "fresh_score_delta": max(0.0, _float("fresh_score_delta", 10.0)),
        "max_symbol_realized_loss_dollars": max(
            0.0,
            _float("max_symbol_realized_loss_dollars", 250.0),
        ),
        "max_symbol_realized_loss_equity_pct": max(
            0.0,
            _float("max_symbol_realized_loss_equity_pct", 0.0025),
        ),
    }


def _paper_dynamic_parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _paper_dynamic_minutes_between(later: datetime, earlier: datetime) -> float:
    if later.tzinfo is not None and earlier.tzinfo is None:
        later = later.replace(tzinfo=None)
    elif later.tzinfo is None and earlier.tzinfo is not None:
        earlier = earlier.replace(tzinfo=None)
    return (later - earlier).total_seconds() / 60.0


def _paper_dynamic_candidate_score(candidate: Mapping[str, Any]) -> float:
    return max(
        _allocator_float(candidate.get("scanner_score"), 0.0),
        _allocator_float(candidate.get("dynamic_score"), 0.0),
        _allocator_float(candidate.get("signal_score"), 0.0),
        _allocator_float(candidate.get("score"), 0.0),
        _allocator_float(candidate.get("strength_eff"), 0.0),
    )


def _paper_dynamic_has_new_intraday_high(candidate: Mapping[str, Any]) -> bool:
    return any(
        _cfg_bool(candidate.get(key), default=False)
        for key in (
            "new_intraday_high",
            "session_new_high",
            "new_high",
            "breakout_new_high",
            "intraday_high_breakout",
            "opening_range_breakout",
            "breakout_confirmed",
            "price_breakout",
        )
    )


def _paper_dynamic_event_side(row: Mapping[str, Any]) -> str:
    side = str(row.get("action") or "").strip().lower()
    if side in {"buy", "sell"}:
        return side
    if str(row.get("exit_reason") or "").strip():
        return "sell"
    return ""


def _paper_dynamic_row_is_dynamic(row: Mapping[str, Any]) -> bool:
    if allocation_profile_is_dynamic_candidate(row):
        return True
    return normalize_strategy_route(
        row.get("route"),
        row.get("source"),
        row.get("entry_route"),
        row.get("entry_source"),
    ) == "dynamic_momentum"


def _paper_dynamic_today_rows(
    *,
    data_dir: Path | str,
    user_id: str,
    symbol: str,
    now: datetime,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    path = attribution_daily_path(data_dir=data_dir, user_id=user_id, day=now.date())
    payload = load_daily_artifact(path)
    orders = payload.get("orders") if isinstance(payload.get("orders"), list) else []
    exits = payload.get("exits") if isinstance(payload.get("exits"), list) else []

    def _matches(row: Any) -> bool:
        return (
            isinstance(row, Mapping)
            and str(row.get("symbol") or "").strip().upper() == symbol
            and _paper_dynamic_row_is_dynamic(row)
        )

    return [row for row in orders if _matches(row)], [row for row in exits if _matches(row)]


def _paper_dynamic_churn_guard_block(
    *,
    symbol: str,
    candidate: Mapping[str, Any],
    data_dir: Path | str,
    user_id: str,
    now: datetime,
    account_equity: Any,
    config: Mapping[str, Any] | None,
) -> tuple[str, dict[str, Any]] | None:
    cfg = _paper_dynamic_churn_guard_config(config)
    if not bool(cfg["enabled"]):
        return None
    orders, exits = _paper_dynamic_today_rows(
        data_dir=data_dir,
        user_id=user_id,
        symbol=symbol,
        now=now,
    )
    equity = _allocator_float(account_equity, 0.0)
    loss_threshold = max(
        float(cfg["max_symbol_realized_loss_dollars"]),
        equity * float(cfg["max_symbol_realized_loss_equity_pct"]),
    )
    realized_loss = 0.0
    for row in exits:
        pnl = _allocator_float(row.get("pnl"), 0.0)
        if pnl < 0.0:
            realized_loss += abs(float(pnl))
    if loss_threshold > 0.0 and realized_loss >= loss_threshold - 1e-9:
        log.warning(
            "SYMBOL_DAILY_LOSS_GUARD_BLOCK symbol=%s realized_loss=%.2f threshold=%.2f mode=paper route=%s",
            symbol,
            realized_loss,
            loss_threshold,
            normalize_strategy_route(candidate.get("route"), candidate.get("source")),
        )
        return (
            "symbol_daily_loss_guard",
            {
                "realized_loss": "%.2f" % realized_loss,
                "loss_threshold": "%.2f" % loss_threshold,
            },
        )

    current_score = _paper_dynamic_candidate_score(candidate)
    current_has_new_high = _paper_dynamic_has_new_intraday_high(candidate)
    latest_exit: Mapping[str, Any] | None = None
    latest_exit_ts: datetime | None = None
    for row in exits:
        reason = str(row.get("exit_reason") or "").strip().lower()
        if reason not in {"weak_exit", "stop_loss"}:
            continue
        ts = _paper_dynamic_parse_ts(row.get("timestamp"))
        if ts is None:
            continue
        if latest_exit_ts is None or _paper_dynamic_minutes_between(ts, latest_exit_ts) > 0.0:
            latest_exit = row
            latest_exit_ts = ts
    if latest_exit is not None and latest_exit_ts is not None:
        age = _paper_dynamic_minutes_between(now, latest_exit_ts)
        if 0.0 <= age <= float(cfg["weak_exit_reentry_cooldown_minutes"]):
            prior_score_raw = (
                latest_exit.get("scanner_score")
                or latest_exit.get("dynamic_score")
                or latest_exit.get("signal_score")
            )
            prior_score = (
                current_score
                if prior_score_raw is None or str(prior_score_raw).strip() == ""
                else _allocator_float(prior_score_raw, current_score)
            )
            stronger = current_score >= prior_score + float(cfg["fresh_score_delta"])
            if not stronger and not current_has_new_high:
                exit_reason = str(latest_exit.get("exit_reason") or "weak_exit")
                log.warning(
                    "REENTRY_AFTER_WEAK_EXIT_BLOCKED symbol=%s exit_reason=%s age_minutes=%.1f "
                    "current_score=%.2f prior_score=%.2f new_intraday_high=%s mode=paper",
                    symbol,
                    exit_reason,
                    age,
                    current_score,
                    prior_score,
                    str(current_has_new_high).lower(),
                )
                return (
                    "reentry_after_weak_exit",
                    {
                        "exit_reason": exit_reason,
                        "age_minutes": "%.1f" % age,
                        "current_score": "%.2f" % current_score,
                        "prior_score": "%.2f" % prior_score,
                    },
                )

    events: list[tuple[datetime, str]] = []
    for row in list(orders) + list(exits):
        ts = _paper_dynamic_parse_ts(row.get("timestamp"))
        side = _paper_dynamic_event_side(row)
        if ts is None or side not in {"buy", "sell"}:
            continue
        age = _paper_dynamic_minutes_between(now, ts)
        if 0.0 <= age <= float(cfg["flip_window_minutes"]):
            events.append((ts, side))
    events.sort(key=lambda item: item[0])
    flips = 0
    previous = ""
    for _ts, side in events:
        if previous and side != previous:
            flips += 1
        previous = side
    if flips >= int(cfg["max_reversals_in_window"]):
        log.warning(
            "REVERSAL_GUARD_BLOCK symbol=%s flips=%d window_minutes=%.1f mode=paper route=%s",
            symbol,
            flips,
            float(cfg["flip_window_minutes"]),
            normalize_strategy_route(candidate.get("route"), candidate.get("source")),
        )
        return (
            "reversal_guard",
            {
                "flips": str(int(flips)),
                "window_minutes": "%.1f" % float(cfg["flip_window_minutes"]),
            },
        )
    return None


def _allocator_dynamic_min_relative_volume(config: Mapping[str, Any] | None) -> float:
    cfg = config or {}
    du_cfg = cfg.get("dynamic_universe") if isinstance(cfg, Mapping) else {}
    du_cfg = du_cfg if isinstance(du_cfg, Mapping) else {}
    try:
        return float(du_cfg.get("min_relative_volume", du_cfg.get("min_rel_volume", 1.0)) or 1.0)
    except (TypeError, ValueError):
        return 1.0


def _allocator_dynamic_effective_min_relative_volume(
    candidate: Mapping[str, Any],
    *,
    base_min_relative_volume: float,
) -> float:
    values: list[float] = []
    for keys in (
        (
            "scanner_effective_min_rel_volume",
            "scanner_effective_min_relative_volume",
            "effective_min_rel_volume",
            "effective_min_relative_volume",
        ),
        (
            "entry_effective_min_rel_volume",
            "entry_eval_effective_min_rel_volume",
            "entry_effective_min_relative_volume",
        ),
        (
            "allocator_effective_min_rel_volume",
            "allocator_effective_min_relative_volume",
        ),
        (
            "dispatch_effective_min_rel_volume",
            "dispatch_effective_min_relative_volume",
        ),
    ):
        value = _allocator_diag_float(candidate, *keys)
        if value is not None and value >= 0.0:
            values.append(float(value))
    if _cfg_bool(candidate.get("catalyst_fastlane_active"), default=False):
        fastlane = _allocator_diag_float(
            candidate,
            "catalyst_min_relative_volume",
            "catalyst_min_rel_volume",
            "fastlane_min_relative_volume",
        )
        if fastlane is not None and fastlane >= 0.0:
            values.append(float(fastlane))
    if not values:
        return float(base_min_relative_volume)
    return max(0.0, min(float(base_min_relative_volume), min(values)))


def _allocator_dynamic_momentum_override_entry_approved(candidate: Mapping[str, Any]) -> bool:
    if not allocation_profile_is_dynamic_candidate(candidate):
        return False
    route = str(candidate.get("route") or candidate.get("source") or "").strip().lower()
    if route != "dynamic_momentum_override":
        return False
    return any(
        _cfg_bool(candidate.get(key), default=False)
        for key in ("decision_allowed", "entry_eval_final", "final")
    )


def _allocator_dynamic_rvol_upstream_approval(candidate: Mapping[str, Any]) -> tuple[bool, str]:
    """Return whether upstream dynamic/news approval should satisfy dispatch RVOL."""
    if not allocation_profile_is_dynamic_candidate(candidate):
        return False, "not_dynamic_candidate"
    entry_approved = any(
        _cfg_bool(candidate.get(key), default=False)
        for key in ("decision_allowed", "entry_eval_final", "final", "entry_final")
    )
    if not entry_approved:
        return False, "entry_not_approved"
    routes = {
        str(candidate.get(key) or "").strip().lower()
        for key in ("route", "source", "entry_eval_route", "entry_route", "entry_source")
        if str(candidate.get(key) or "").strip()
    }
    if "dynamic_momentum_override" in routes:
        return True, "dynamic_momentum_override_entry_approved"
    if routes.intersection({"dynamic_universe", "news_catalyst", "premarket_catalyst_replay"}):
        return True, "dynamic_news_entry_approved"
    if _allocator_strong_catalyst_dynamic(candidate):
        return True, "dynamic_news_entry_approved"
    return False, "dynamic_route_not_approved"


def _allocator_dynamic_catalyst_scores(candidate: Mapping[str, Any]) -> tuple[float, float, float]:
    return (
        _allocator_float(candidate.get("news_score"), 0.0),
        _allocator_scaled_catalyst_score(candidate.get("catalyst_score")) / 10.0,
        _allocator_float(candidate.get("event_score"), 0.0),
    )


def _allocator_strong_catalyst_dynamic(candidate: Mapping[str, Any]) -> bool:
    news, catalyst, event = _allocator_dynamic_catalyst_scores(candidate)
    return bool(news >= 7.0 - 1e-9 or catalyst >= 0.7 - 1e-9 or event >= 7.0 - 1e-9)


def _allocator_weak_catalyst_dynamic(candidate: Mapping[str, Any]) -> bool:
    if not allocation_profile_is_dynamic_candidate(candidate):
        return False
    route = str(candidate.get("route") or candidate.get("source") or "").strip().lower()
    if route != "dynamic_momentum_override":
        return False
    if _allocator_strong_catalyst_dynamic(candidate):
        return False
    news, catalyst, event = _allocator_dynamic_catalyst_scores(candidate)
    return bool(abs(news) <= 1e-9 and abs(catalyst) <= 1e-9 and abs(event) <= 1e-9)


def _allocator_dynamic_price_above_vwap(candidate: Mapping[str, Any]) -> bool:
    for key in (
        "price_above_vwap",
        "vwap_above",
        "scanner_price_above_vwap",
        "scanner_vwap_above",
        "entry_price_above_vwap",
        "entry_vwap_above",
        "allocator_price_above_vwap",
        "allocator_vwap_above",
        "order_price_above_vwap",
        "order_vwap_above",
    ):
        if key in candidate:
            return _cfg_bool(candidate.get(key), default=False)
    price = _allocator_diag_float(candidate, "paper_current_price", "current_price", "price")
    vwap = _allocator_diag_float(candidate, "paper_session_vwap", "session_vwap", "vwap")
    return bool(price is not None and vwap is not None and vwap > 0.0 and price >= vwap - 1e-9)


def _allocator_dynamic_recent_unstable_quote(candidate: Mapping[str, Any]) -> bool:
    return any(
        _cfg_bool(candidate.get(key), default=False)
        for key in (
            "unstable_quote",
            "quote_unstable",
            "recent_unstable_quote",
            "dynamic_unstable_quote",
            "blocked_after_unstable_quote",
        )
    )


def _allocator_weak_catalyst_reject_reason(
    candidate: Mapping[str, Any],
    *,
    relative_volume: float,
    spread_pct: float | None,
    spread_cap_pct: float | None,
) -> str | None:
    if not _allocator_weak_catalyst_dynamic(candidate):
        return None
    if _allocator_dynamic_recent_unstable_quote(candidate):
        return "unstable_quote_recent_scan"
    if not _allocator_dynamic_price_above_vwap(candidate):
        return "price_not_above_vwap"
    if (
        spread_pct is None
        or spread_cap_pct is None
        or not math.isfinite(float(spread_pct))
        or float(spread_pct) > float(spread_cap_pct) + 1e-9
    ):
        return "spread_above_cap"
    scanner_score = _allocator_diag_float(candidate, "scanner_score", "dynamic_score", "signal_score", "score")
    if scanner_score is None or scanner_score < 80.0 - 1e-9:
        if float(relative_volume) < 0.50 - 1e-9:
            return "relative_volume_below_0.50"
    return None


def _allocator_live_weak_catalyst_guard_enabled(config: Mapping[str, Any] | None) -> bool:
    cfg = config or {}
    dme = cfg.get("dynamic_momentum_entry") if isinstance(cfg, Mapping) else {}
    dme = dme if isinstance(dme, Mapping) else {}
    guard = dme.get("live_weak_catalyst_guard") if isinstance(dme.get("live_weak_catalyst_guard"), Mapping) else {}
    if "enabled" in guard:
        return _cfg_bool(guard.get("enabled"), default=True)
    if "live_weak_catalyst_guard_enabled" in dme:
        return _cfg_bool(dme.get("live_weak_catalyst_guard_enabled"), default=True)
    return True


def _allocator_live_weak_catalyst_guard_config(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    cfg = config or {}
    dme = cfg.get("dynamic_momentum_entry") if isinstance(cfg, Mapping) else {}
    dme = dme if isinstance(dme, Mapping) else {}
    guard = dme.get("live_weak_catalyst_guard") if isinstance(dme.get("live_weak_catalyst_guard"), Mapping) else {}
    return guard if isinstance(guard, Mapping) else dme


def _allocator_live_weak_catalyst_exceptional(candidate: Mapping[str, Any], config: Mapping[str, Any] | None) -> tuple[bool, dict[str, float | bool]]:
    guard = _allocator_live_weak_catalyst_guard_config(config)
    min_rvol = _allocator_float(
        guard.get("exceptional_min_relative_volume", guard.get("min_exceptional_relative_volume")),
        1.5,
    )
    min_gain = _allocator_float(
        guard.get("exceptional_min_gain_pct", guard.get("min_exceptional_gain_pct")),
        4.0,
    )
    min_score = _allocator_float(
        guard.get("exceptional_min_scanner_score", guard.get("min_exceptional_scanner_score")),
        80.0,
    )
    rel = _allocator_preserved_dynamic_relative_volume(candidate)
    if rel is None:
        rel = max(
            _allocator_float(candidate.get("relative_volume"), 0.0),
            _allocator_float(candidate.get("rel_volume"), 0.0),
        )
    gain = max(
        _allocator_float(candidate.get("gain_pct"), 0.0),
        _allocator_float(candidate.get("day_gain_pct"), 0.0),
        _allocator_float(candidate.get("gain"), 0.0),
    )
    score = _allocator_diag_float(candidate, "scanner_score", "dynamic_score", "signal_score", "score")
    score_f = float(score if score is not None else 0.0)
    aligned = _allocator_dynamic_price_above_vwap(candidate)
    exceptional = bool(rel >= min_rvol - 1e-9 and gain >= min_gain - 1e-9 and score_f >= min_score - 1e-9 and aligned)
    return exceptional, {
        "relative_volume": float(rel),
        "min_relative_volume": float(min_rvol),
        "gain_pct": float(gain),
        "min_gain_pct": float(min_gain),
        "scanner_score": float(score_f),
        "min_scanner_score": float(min_score),
        "aligned": bool(aligned),
    }


def _live_weak_catalyst_exception_experiment_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    cfg = config if isinstance(config, Mapping) else {}
    du_cfg = cfg.get("dynamic_universe") if isinstance(cfg.get("dynamic_universe"), Mapping) else {}
    raw = du_cfg.get("live_weak_catalyst_exception_experiment") if isinstance(du_cfg, Mapping) else {}
    exp = raw if isinstance(raw, Mapping) else {}
    return {
        "enabled": _cfg_bool(exp.get("enabled"), default=False),
        "min_price": _allocator_float(exp.get("min_price"), 8.0),
        "min_gain_pct": _allocator_float(exp.get("min_gain_pct"), 10.0),
        "min_relative_volume": _allocator_float(exp.get("min_relative_volume"), 0.5),
        "max_spread_pct": _allocator_float(exp.get("max_spread_pct"), 0.25),
        "require_entry_eval_pass": _cfg_bool(exp.get("require_entry_eval_pass"), default=True),
        "max_atr_pct": _allocator_float(exp.get("max_atr_pct"), 15.0),
        "max_positions_per_day": max(0, int(_allocator_float(exp.get("max_positions_per_day"), 1.0))),
        "notional_cap": _allocator_float(exp.get("notional_cap"), 300.0),
        "require_no_existing_position": _cfg_bool(exp.get("require_no_existing_position"), default=True),
    }


def _dynamic_momentum_expectancy_gate_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    cfg = config if isinstance(config, Mapping) else {}
    du_cfg = cfg.get("dynamic_universe") if isinstance(cfg.get("dynamic_universe"), Mapping) else {}
    raw = du_cfg.get("dynamic_momentum_expectancy_gate") if isinstance(du_cfg, Mapping) else {}
    gate = raw if isinstance(raw, Mapping) else {}

    def _symbol_list(key: str) -> set[str]:
        value = gate.get(key, [])
        if isinstance(value, str):
            rows = [part.strip() for part in value.split(",")]
        elif isinstance(value, Sequence):
            rows = [str(part).strip() for part in value]
        else:
            rows = []
        return {sym.upper() for sym in rows if sym}

    return {
        "enabled": _cfg_bool(gate.get("enabled"), default=False),
        "live_enabled": _cfg_bool(gate.get("live_enabled"), default=True),
        "min_expectancy_score": _allocator_float(gate.get("min_expectancy_score"), 0.0),
        "lookback_days": max(1, int(_allocator_float(gate.get("lookback_days"), 5.0))),
        "min_samples": max(1, int(_allocator_float(gate.get("min_samples"), 5.0))),
        "reduce_only_when_negative": _cfg_bool(gate.get("reduce_only_when_negative"), default=True),
        "fallback_allow_if_no_data": _cfg_bool(gate.get("fallback_allow_if_no_data"), default=True),
        "blocked_symbols": _symbol_list("blocked_symbols"),
        "reduced_symbols": _symbol_list("reduced_symbols"),
        "max_notional_when_negative": _allocator_float(gate.get("max_notional_when_negative"), 300.0),
    }


def _expectancy_report_paths(data_dir: Path | str, *, day: str | None, lookback_days: int) -> list[Path]:
    root = Path(data_dir) / "research_metrics"
    if not root.exists():
        return []
    if day:
        try:
            end = datetime.fromisoformat(str(day)).date()
            allowed = {(end - timedelta(days=idx)).isoformat() for idx in range(max(1, lookback_days))}
        except ValueError:
            allowed = set()
    else:
        allowed = set()
    paths: list[Path] = []
    for path in root.glob("*/signal_expectancy_report.json"):
        if allowed and path.parent.name not in allowed:
            continue
        paths.append(path)
    return sorted(paths, reverse=True)


def _load_recent_dynamic_expectancy(
    *,
    data_dir: Path | str,
    symbol: str,
    day: str | None,
    lookback_days: int,
) -> dict[str, Any] | None:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    symbol_count = 0
    symbol_weighted_score = 0.0
    route_count = 0
    route_weighted_score = 0.0
    latest_path: str | None = None
    for path in _expectancy_report_paths(data_dir, day=day, lookback_days=lookback_days):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        latest_path = latest_path or str(path)
        for row in payload.get("symbol_expectancy") or []:
            if not isinstance(row, Mapping):
                continue
            if str(row.get("route") or "") != "dynamic_momentum_override":
                continue
            if str(row.get("symbol") or "").strip().upper() != sym:
                continue
            count = int(_allocator_float(row.get("count"), 0.0))
            score = _allocator_float(row.get("expectancy_score"), 0.0)
            symbol_count += max(0, count)
            symbol_weighted_score += score * max(0, count)
        for row in payload.get("route_expectancy") or []:
            if not isinstance(row, Mapping) or str(row.get("route") or "") != "dynamic_momentum_override":
                continue
            count = int(_allocator_float(row.get("count"), 0.0))
            score = _allocator_float(row.get("expectancy_score"), 0.0)
            route_count += max(0, count)
            route_weighted_score += score * max(0, count)
    if symbol_count > 0:
        return {
            "scope": "symbol",
            "count": symbol_count,
            "expectancy_score": symbol_weighted_score / symbol_count,
            "source": latest_path,
        }
    if route_count > 0:
        return {
            "scope": "route",
            "count": route_count,
            "expectancy_score": route_weighted_score / route_count,
            "source": latest_path,
        }
    return None


def _is_dynamic_momentum_override_route(route: Any, candidate: Mapping[str, Any]) -> bool:
    values = [
        route,
        candidate.get("route"),
        candidate.get("entry_eval_route"),
        candidate.get("source"),
        normalize_strategy_route(route, candidate.get("source")),
    ]
    return any(str(value or "").strip() == "dynamic_momentum_override" for value in values)


def _dynamic_momentum_expectancy_gate_decision(
    *,
    symbol: str,
    route: str,
    side: str,
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | None,
    data_dir: Path | str,
    user_id: Any,
    day: str | None,
) -> dict[str, Any]:
    cfg = _dynamic_momentum_expectancy_gate_config(config)
    sym = str(symbol or "").strip().upper()
    route_norm = normalize_strategy_route(route, candidate.get("source"))
    is_dynamic_momentum_route = _is_dynamic_momentum_override_route(route, candidate)
    if is_dynamic_momentum_route:
        route_norm = "dynamic_momentum_override"
    if not cfg["enabled"]:
        return {"action": "allow", "reason": "disabled"}
    if side != "buy":
        return {"action": "allow", "reason": "not_buy"}
    if is_option_symbol(sym):
        return {"action": "allow", "reason": "option"}
    if not is_dynamic_momentum_route:
        return {"action": "allow", "reason": "route_not_dynamic_momentum_override", "route": route_norm}
    if not allocation_profile_is_dynamic_candidate(candidate):
        return {"action": "allow", "reason": "not_dynamic_candidate", "route": route_norm}
    if not _allocator_paper_execution_context(user_id=user_id, config=config) and not cfg["live_enabled"]:
        return {"action": "allow", "reason": "live_disabled", "route": route_norm}
    if sym in cfg["blocked_symbols"]:
        return {"action": "block", "reason": "configured_blocked_symbol", "route": route_norm}
    if sym in cfg["reduced_symbols"]:
        return {
            "action": "reduce",
            "reason": "configured_reduced_symbol",
            "route": route_norm,
            "expectancy_score": None,
            "sample_count": 0,
            "scope": "configured_symbol",
            "cap": float(cfg["max_notional_when_negative"]),
        }
    evidence = _load_recent_dynamic_expectancy(
        data_dir=data_dir,
        symbol=sym,
        day=day,
        lookback_days=int(cfg["lookback_days"]),
    )
    if evidence is None:
        return {
            "action": "allow" if cfg["fallback_allow_if_no_data"] else "block",
            "reason": "no_expectancy_data" if cfg["fallback_allow_if_no_data"] else "missing_expectancy_data",
            "route": route_norm,
        }
    score = float(evidence["expectancy_score"])
    count = int(evidence["count"])
    min_samples = int(cfg["min_samples"])
    min_score = float(cfg["min_expectancy_score"])
    negative_enough = score < -1e-9 if cfg["reduce_only_when_negative"] else score < min_score - 1e-9
    if count >= min_samples and score < min_score - 1e-9 and negative_enough:
        return {
            "action": "reduce",
            "reason": "negative_expectancy",
            "route": route_norm,
            "expectancy_score": score,
            "sample_count": count,
            "scope": evidence.get("scope"),
            "cap": float(cfg["max_notional_when_negative"]),
            "source": evidence.get("source"),
        }
    return {
        "action": "allow",
        "reason": "expectancy_ok_or_insufficient_samples",
        "route": route_norm,
        "expectancy_score": score,
        "sample_count": count,
        "scope": evidence.get("scope"),
        "source": evidence.get("source"),
    }


def _trend_long_reentry_protection_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    cfg = config if isinstance(config, Mapping) else {}
    tl = cfg.get("trend_long") if isinstance(cfg.get("trend_long"), Mapping) else {}
    raw = tl.get("reentry_protection") if isinstance(tl.get("reentry_protection"), Mapping) else {}
    rep = raw if isinstance(raw, Mapping) else {}
    return {
        "enabled": _cfg_bool(rep.get("enabled"), default=False),
        "live_enabled": _cfg_bool(rep.get("live_enabled"), default=False),
        "cooldown_minutes_after_stop": max(0.0, _allocator_float(rep.get("cooldown_minutes_after_stop"), 90.0)),
        "require_new_breakout": _cfg_bool(rep.get("require_new_breakout"), default=True),
        "require_new_intraday_high": _cfg_bool(rep.get("require_new_intraday_high"), default=False),
        "require_new_signal_timestamp": _cfg_bool(rep.get("require_new_signal_timestamp"), default=True),
    }


def _is_trend_long_route(route: Any, candidate: Mapping[str, Any]) -> bool:
    values = [
        route,
        candidate.get("route"),
        candidate.get("entry_route"),
        candidate.get("source"),
        candidate.get("entry_source"),
        normalize_strategy_route(route, candidate.get("source")),
    ]
    return any(str(value or "").strip() == "trend_long" for value in values)


def _trend_reentry_parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _trend_reentry_latest_stop(
    *,
    data_dir: Path | str,
    user_id: str,
    day: str | None,
    symbol: str,
    now: datetime,
) -> dict[str, Any] | None:
    if not day:
        return None
    path = attribution_daily_path(data_dir=data_dir, user_id=user_id, day=day)
    payload = load_daily_artifact(path)
    sym = str(symbol or "").strip().upper()
    latest: tuple[datetime, dict[str, Any]] | None = None
    for row in payload.get("exits", []) if isinstance(payload.get("exits"), list) else []:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("symbol") or "").strip().upper() != sym:
            continue
        if not _is_trend_long_route(row.get("entry_route") or row.get("route"), row):
            continue
        reason = str(row.get("exit_reason") or row.get("reason") or "").strip().lower()
        if "stop" not in reason:
            continue
        ts = _trend_reentry_parse_timestamp(row.get("timestamp") or row.get("exit_time"))
        if ts is None or ts > now:
            continue
        if latest is None or ts > latest[0]:
            latest = (ts, dict(row))
    if latest is None:
        return None
    out = latest[1]
    out["_stop_dt"] = latest[0]
    return out


def _trend_reentry_candidate_timestamp(candidate: Mapping[str, Any], action: Mapping[str, Any], day: str | None) -> datetime | None:
    for src in (candidate, action):
        for key in ("signal_timestamp", "entry_signal_timestamp", "timestamp", "first_seen_time", "selected_timestamp"):
            ts = _trend_reentry_parse_timestamp(src.get(key))
            if ts is not None:
                return ts
    if day:
        for src in (candidate, action):
            clock = str(src.get("signal_time") or src.get("entry_signal_time") or "").strip()
            if re.match(r"^\d{2}:\d{2}(:\d{2})?$", clock):
                parts = [int(part) for part in clock.split(":")]
                while len(parts) < 3:
                    parts.append(0)
                try:
                    return datetime.fromisoformat(f"{day}T{parts[0]:02d}:{parts[1]:02d}:{parts[2]:02d}+00:00")
                except ValueError:
                    return None
    return None


def _trend_reentry_bool(candidate: Mapping[str, Any], action: Mapping[str, Any], *keys: str) -> bool:
    for src in (candidate, action):
        for key in keys:
            if _cfg_bool(src.get(key), default=False):
                return True
    return False


def _trend_reentry_protection_decision(
    *,
    symbol: str,
    side: str,
    route: str,
    candidate: Mapping[str, Any],
    action: Mapping[str, Any],
    config: Mapping[str, Any] | None,
    data_dir: Path | str,
    user_id: str,
    day: str | None,
    now: datetime,
) -> dict[str, Any]:
    cfg = _trend_long_reentry_protection_config(config)
    sym = str(symbol or "").strip().upper()
    now_utc = _allocator_now_utc(now)
    if not cfg["enabled"]:
        return {"action": "allow", "reason": "disabled"}
    if side != "buy":
        return {"action": "allow", "reason": "not_buy"}
    if is_option_symbol(sym):
        return {"action": "allow", "reason": "option"}
    if not _is_trend_long_route(route, candidate):
        return {"action": "allow", "reason": "not_trend_long"}
    if not _allocator_paper_execution_context(user_id=user_id, config=config) and not cfg["live_enabled"]:
        return {"action": "allow", "reason": "live_disabled"}
    stop = _trend_reentry_latest_stop(data_dir=data_dir, user_id=user_id, day=day, symbol=sym, now=now_utc)
    if stop is None:
        return {"action": "allow", "reason": "no_prior_stop"}
    stop_dt = stop["_stop_dt"]
    age_min = max(0.0, (now_utc - stop_dt).total_seconds() / 60.0)
    cooldown = float(cfg["cooldown_minutes_after_stop"])
    remaining = max(0.0, cooldown - age_min)
    breakout = _trend_reentry_bool(
        candidate,
        action,
        "new_breakout",
        "fresh_breakout",
        "five_min_breakout",
        "breakout",
        "breakout_ok",
        "opening_range_breakout",
    )
    new_high = _trend_reentry_bool(
        candidate,
        action,
        "new_intraday_high",
        "fresh_intraday_high",
        "session_new_high",
        "new_high",
    )
    signal_ts = _trend_reentry_candidate_timestamp(candidate, action, day)
    new_signal = bool(signal_ts is not None and signal_ts > stop_dt)
    freshness_checks: list[bool] = []
    if cfg["require_new_breakout"]:
        freshness_checks.append(breakout)
    if cfg["require_new_intraday_high"]:
        freshness_checks.append(new_high)
    if cfg["require_new_signal_timestamp"]:
        freshness_checks.append(new_signal)
    fresh_signal = any(freshness_checks) if freshness_checks else True
    base = {
        "stop_time": stop_dt.isoformat(),
        "age_minutes": age_min,
        "cooldown_remaining_minutes": remaining,
        "breakout": breakout,
        "new_intraday_high": new_high,
        "new_signal_timestamp": signal_ts.isoformat() if signal_ts else "n/a",
        "fresh_signal": fresh_signal,
        "exit_reason": stop.get("exit_reason") or stop.get("reason") or "stop_loss",
    }
    if remaining > 1e-9:
        return {**base, "action": "block", "reason": "cooldown_active"}
    if not fresh_signal:
        return {**base, "action": "block", "reason": "fresh_signal_required"}
    return {**base, "action": "allow", "reason": "expired_with_fresh_signal"}


def _allocator_candidate_entry_eval_passed(candidate: Mapping[str, Any]) -> bool:
    return any(
        _cfg_bool(candidate.get(key), default=False)
        for key in ("decision_allowed", "entry_eval_final", "final", "entry_final")
    )


def _allocator_candidate_gain_pct(candidate: Mapping[str, Any]) -> float:
    return max(
        _allocator_float(candidate.get("gain_pct"), 0.0),
        _allocator_float(candidate.get("day_gain_pct"), 0.0),
        _allocator_float(candidate.get("gain"), 0.0),
    )


def _allocator_candidate_atr_pct(candidate: Mapping[str, Any]) -> float | None:
    value = _allocator_diag_float(
        candidate,
        "atr_pct",
        "atr_percent",
        "dynamic_atr_pct",
        "scanner_atr_pct",
        "entry_atr_pct",
        "allocator_atr_pct",
    )
    if value is not None:
        return float(value)
    price = _allocator_diag_float(candidate, "paper_current_price", "current_price", "price")
    atr = _allocator_diag_float(candidate, "atr", "average_true_range")
    if price is None or atr is None or price <= 0:
        return None
    return (float(atr) / float(price)) * 100.0


def _allocator_existing_position_qty(
    symbol: str,
    *,
    current_positions: Mapping[str, Any] | None,
    tracked: Mapping[str, Any] | None,
    positions: Sequence[Mapping[str, Any]] | None,
) -> float:
    sym = str(symbol or "").strip().upper()
    current = current_positions if isinstance(current_positions, Mapping) else {}
    for key, row in current.items():
        if str(key or "").strip().upper() != sym:
            continue
        if isinstance(row, Mapping):
            return _allocator_float(row.get("qty") or row.get("quantity"), 0.0)
        return _allocator_float(row, 0.0)
    tracked_row = (tracked or {}).get(sym) if isinstance(tracked, Mapping) else None
    if isinstance(tracked_row, Mapping):
        qty = _allocator_float(tracked_row.get("qty") or tracked_row.get("quantity"), 0.0)
        if qty:
            return qty
    for row in positions or []:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("symbol") or "").strip().upper() == sym:
            qty = _allocator_float(row.get("qty") or row.get("quantity"), 0.0)
            if qty:
                return qty
    return 0.0


def _live_weak_catalyst_exception_day_key(user_id: Any, dt: Any) -> str:
    now = _allocator_now_utc(dt)
    return "%s:%s" % (str(user_id or "default"), now.date().isoformat())


def _allocator_live_weak_catalyst_exception_experiment_decision(
    candidate: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None,
    user_id: Any,
    symbol: str,
    side: str,
    price: float | None,
    spread_pct: float | None,
    notional: float,
    current_positions: Mapping[str, Any] | None,
    tracked: Mapping[str, Any] | None,
    positions: Sequence[Mapping[str, Any]] | None,
    dt: Any,
) -> tuple[bool, str, dict[str, Any]]:
    exp = _live_weak_catalyst_exception_experiment_config(config)
    rel = _allocator_preserved_dynamic_relative_volume(candidate)
    if rel is None:
        rel = max(_allocator_float(candidate.get("relative_volume"), 0.0), _allocator_float(candidate.get("rel_volume"), 0.0))
    gain = _allocator_candidate_gain_pct(candidate)
    atr_pct = _allocator_candidate_atr_pct(candidate)
    px = float(price or _allocator_diag_float(candidate, "paper_current_price", "current_price", "price") or 0.0)
    spread = None if spread_pct is None else _allocator_float(spread_pct, math.inf)
    day_key = _live_weak_catalyst_exception_day_key(user_id, dt)
    used_today = _LIVE_WEAK_CATALYST_EXCEPTION_DAY_COUNTS.get(day_key, 0)
    qty = _allocator_existing_position_qty(
        symbol,
        current_positions=current_positions,
        tracked=tracked,
        positions=positions,
    )
    meta = {
        **exp,
        "price": px,
        "gain_pct": float(gain),
        "relative_volume": float(rel or 0.0),
        "spread_pct": spread,
        "atr_pct": atr_pct,
        "entry_eval_pass": _allocator_candidate_entry_eval_passed(candidate),
        "used_today": used_today,
        "existing_position_qty": qty,
        "notional": float(notional or 0.0),
    }
    if not exp["enabled"]:
        return False, "experiment_disabled", meta
    if side != "buy":
        return False, "not_buy", meta
    if is_option_symbol(symbol):
        return False, "option_symbol", meta
    if not allocation_profile_is_dynamic_candidate(candidate):
        return False, "not_dynamic_candidate", meta
    route = str(candidate.get("route") or candidate.get("source") or "").strip().lower()
    if route not in {"dynamic_momentum_override", "dynamic_universe", "premarket_catalyst_replay", "news_catalyst"}:
        return False, "not_dynamic_stock_route", meta
    if not _allocator_weak_catalyst_dynamic(candidate):
        return False, "not_weak_catalyst_dynamic", meta
    if exp["require_entry_eval_pass"] and not meta["entry_eval_pass"]:
        return False, "entry_eval_not_passed", meta
    if px < float(exp["min_price"]) - 1e-9:
        return False, "price_below_min", meta
    if gain < float(exp["min_gain_pct"]) - 1e-9:
        return False, "gain_below_min", meta
    if float(rel or 0.0) < float(exp["min_relative_volume"]) - 1e-9:
        return False, "relative_volume_below_min", meta
    if spread is None or not math.isfinite(float(spread)) or float(spread) > float(exp["max_spread_pct"]) + 1e-9:
        return False, "spread_above_max", meta
    if atr_pct is None:
        return False, "atr_unavailable", meta
    if float(atr_pct) > float(exp["max_atr_pct"]) + 1e-9:
        return False, "atr_above_max", meta
    if exp["require_no_existing_position"] and abs(float(qty or 0.0)) > 1e-9:
        return False, "existing_position", meta
    if used_today >= int(exp["max_positions_per_day"]):
        return False, "daily_max_positions_per_day", meta
    return True, "ok", meta


def _allocator_dynamic_breakout_confirmed(candidate: Mapping[str, Any]) -> bool:
    return any(
        _cfg_bool(candidate.get(key), default=False)
        for key in (
            "fresh_intraday_high",
            "new_intraday_high",
            "scanner_new_intraday_high",
            "five_min_breakout",
            "scanner_five_min_breakout",
            "breakout",
            "breakout_ok",
        )
    )


def _allocator_dynamic_vwap_extension_pct(candidate: Mapping[str, Any]) -> float | None:
    explicit = _allocator_diag_float(
        candidate,
        "distance_from_vwap_pct",
        "vwap_extension_pct",
        "dynamic_vwap_extension_pct",
    )
    if explicit is not None:
        return explicit
    price = _allocator_diag_float(candidate, "paper_current_price", "current_price", "price")
    vwap = _allocator_diag_float(candidate, "paper_session_vwap", "session_vwap", "vwap")
    if price is None or vwap is None or vwap <= 0.0:
        return None
    return ((float(price) - float(vwap)) / float(vwap)) * 100.0


def _allocator_dynamic_aggressive_cfg(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    cfg = config if isinstance(config, Mapping) else {}
    raw = cfg.get("dynamic_aggressive")
    return raw if isinstance(raw, Mapping) else {}


def _allocator_is_dynamic_aggressive(candidate: Mapping[str, Any] | None) -> bool:
    if not isinstance(candidate, Mapping):
        return False
    return any(
        str(candidate.get(key) or "").strip().lower() in {"dynamic_aggressive", "dynamic_aggressive_scalp"}
        for key in ("route", "source", "entry_route", "entry_source")
    )


def _allocator_dynamic_aggressive_enabled(
    config: Mapping[str, Any] | None,
    *,
    user_id: Any,
) -> bool:
    cfg = _allocator_dynamic_aggressive_cfg(config)
    if _allocator_paper_execution_context(user_id=user_id, config=config):
        return _cfg_bool(cfg.get("enabled_paper"), default=False)
    return _cfg_bool(cfg.get("enabled_live"), default=False)


def _allocator_dynamic_aggressive_active_count(
    tracked: Mapping[str, Any] | None,
    positions: Sequence[Mapping[str, Any]] | None = None,
) -> int:
    symbols: set[str] = set()
    tracked_map = tracked if isinstance(tracked, Mapping) else {}
    for sym_raw, row in tracked_map.items():
        if isinstance(row, Mapping) and _allocator_is_dynamic_aggressive(row):
            try:
                qty = float(row.get("qty") or 0.0)
            except (TypeError, ValueError):
                qty = 0.0
            if qty > 0.0:
                symbols.add(str(sym_raw or row.get("symbol") or "").strip().upper())
    for row in positions or []:
        if not isinstance(row, Mapping):
            continue
        if _allocator_is_dynamic_aggressive(row):
            sym = str(row.get("symbol") or "").strip().upper()
            if sym:
                symbols.add(sym)
    return len({sym for sym in symbols if sym})


def _allocator_open_order_symbols(broker: Any) -> set[str]:
    get_fn = getattr(broker, "get_open_orders", None)
    if not callable(get_fn):
        return set()
    try:
        rows = get_fn() or []
    except Exception:
        return set()
    out: set[str] = set()
    for row in rows:
        if isinstance(row, Mapping):
            sym = str(row.get("symbol") or "").strip().upper()
        else:
            sym = str(getattr(row, "symbol", "") or "").strip().upper()
        if sym:
            out.add(sym)
    return out


def _allocator_dynamic_aggressive_decision(
    candidate: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None,
    user_id: Any,
    symbol: str,
    spread_pct: float | None,
    current_positions: Mapping[str, Any] | None = None,
    tracked: Mapping[str, Any] | None = None,
    positions: Sequence[Mapping[str, Any]] | None = None,
    open_order_symbols: set[str] | None = None,
) -> tuple[bool, str, dict[str, float | bool]]:
    cfg = _allocator_dynamic_aggressive_cfg(config)
    if not _allocator_dynamic_aggressive_enabled(config, user_id=user_id):
        return False, "disabled", {}
    sym = str(symbol or candidate.get("symbol") or "").strip().upper()
    if not sym:
        return False, "missing_symbol", {}
    if sym in (open_order_symbols or set()):
        return False, "open_order_pending", {}
    current = current_positions if isinstance(current_positions, Mapping) else {}
    if sym in {str(k).strip().upper() for k in current.keys()}:
        return False, "position_exists", {}
    tracked_row = (tracked or {}).get(sym) if isinstance(tracked, Mapping) else None
    if isinstance(tracked_row, Mapping):
        try:
            if float(tracked_row.get("qty") or 0.0) > 0.0:
                return False, "position_exists", {}
        except (TypeError, ValueError):
            pass
    max_positions = int(_allocator_float(cfg.get("max_positions"), 1.0))
    if _allocator_dynamic_aggressive_active_count(tracked, positions) >= max(0, max_positions):
        return False, "max_positions", {}
    gain = max(
        _allocator_float(candidate.get("gain_pct"), 0.0),
        _allocator_float(candidate.get("day_gain_pct"), 0.0),
    )
    rel = _allocator_preserved_dynamic_relative_volume(candidate)
    if rel is None:
        rel = max(
            _allocator_float(candidate.get("relative_volume"), 0.0),
            _allocator_float(candidate.get("rel_volume"), 0.0),
        )
    min_gain = _allocator_float(cfg.get("min_gain_pct"), 8.0)
    max_gain = _allocator_float(cfg.get("max_gain_pct"), 25.0)
    min_rel = _allocator_float(cfg.get("min_relative_volume"), 1.5)
    above_vwap = _allocator_dynamic_price_above_vwap(candidate)
    breakout = _allocator_dynamic_breakout_confirmed(candidate)
    spread = None if spread_pct is None else _allocator_float(spread_pct, math.inf)
    max_spread = _allocator_float(cfg.get("max_spread_pct"), 2.5)
    meta = {
        "gain_pct": float(gain),
        "relative_volume": float(rel),
        "vwap_above": bool(above_vwap),
        "breakout": bool(breakout),
        "spread_pct": float(spread if spread is not None else math.nan),
        "max_spread_pct": float(max_spread),
    }
    log.info(
        "DYNAMIC_AGGRESSIVE_CANDIDATE symbol=%s gain_pct=%.2f rel_vol=%.3f vwap_above=%s",
        sym,
        float(gain),
        float(rel),
        str(bool(above_vwap)).lower(),
    )
    if gain < min_gain - 1e-9:
        return False, "gain_below_min", meta
    if gain > max_gain + 1e-9:
        return False, "gain_above_max", meta
    if rel < min_rel - 1e-9:
        return False, "relative_volume_below_min", meta
    if _cfg_bool(cfg.get("require_vwap_above"), default=True) and not above_vwap:
        return False, "price_not_above_vwap", meta
    if _cfg_bool(cfg.get("require_new_high_or_breakout"), default=True) and not breakout:
        return False, "no_new_high_or_breakout", meta
    if spread is None or not math.isfinite(float(spread)) or float(spread) > max_spread + 1e-9:
        return False, "spread_above_cap", meta
    return True, "ok", meta


def build_dynamic_aggressive_scalp_candidates(
    rows: Sequence[Mapping[str, Any]],
    *,
    config: Mapping[str, Any] | None,
    user_id: Any,
    current_positions: Mapping[str, Any] | None = None,
    tracked: Mapping[str, Any] | None = None,
    positions: Sequence[Mapping[str, Any]] | None = None,
    open_order_symbols: set[str] | None = None,
) -> list[dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    rejected = 0
    orders = 0
    for row in rows or []:
        sym = str(row.get("symbol") or row.get("sym_u") or "").strip().upper()
        if not sym:
            continue
        ok, reason, _meta = _allocator_dynamic_aggressive_decision(
            row,
            config=config,
            user_id=user_id,
            symbol=sym,
            spread_pct=_allocator_diag_float(row, "spread_pct"),
            current_positions=current_positions,
            tracked=tracked,
            positions=positions,
            open_order_symbols=open_order_symbols,
        )
        if not ok:
            rejected += 1
            log.info("DYNAMIC_AGGRESSIVE_REJECT symbol=%s reason=%s", sym, reason)
            continue
        cfg = _allocator_dynamic_aggressive_cfg(config)
        notional = clean_notional(_allocator_float(cfg.get("max_notional"), 500.0), min_notional=0.0)
        out = dict(row)
        out.update(
            {
                "symbol": sym,
                "sym_u": sym,
                "route": "dynamic_aggressive_scalp",
                "source": "dynamic_aggressive",
                "dynamic_candidate": True,
                "dynamic_symbol": True,
                "entry_eval_final": True,
                "decision_allowed": True,
                "notional": notional,
                "score": max(_allocator_float(row.get("score"), 0.0), 1.0),
            }
        )
        accepted.append(out)
        orders += 1
        log.info("DYNAMIC_AGGRESSIVE_ACCEPT symbol=%s reason=ok", sym)
    log.info(
        "DYNAMIC_AGGRESSIVE_SUMMARY candidates=%d accepted=%d rejected=%d orders=%d",
        len(rows or []),
        len(accepted),
        rejected,
        orders,
    )
    return accepted


def _allocator_weak_catalyst_late_entry_decision(
    candidate: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None,
    user_id: Any,
    spread_pct: float | None = None,
    spread_cap_pct: float | None = None,
) -> dict[str, Any]:
    if not _allocator_weak_catalyst_dynamic(candidate):
        return {"action": "allow", "reason": "not_weak_catalyst_dynamic", "factor": 1.0}
    if _allocator_paper_execution_context(user_id=user_id, config=config):
        return {"action": "allow", "reason": "paper_context", "factor": 1.0}
    cfg = config if isinstance(config, Mapping) else {}
    du_cfg = cfg.get("dynamic_universe") if isinstance(cfg.get("dynamic_universe"), Mapping) else {}
    du_cfg = du_cfg if isinstance(du_cfg, Mapping) else {}
    max_gain = _allocator_float(du_cfg.get("weak_catalyst_max_entry_gain_pct_live"), 15.0)
    reduce_floor = _allocator_float(du_cfg.get("weak_catalyst_reduce_entry_gain_pct_live"), 12.0)
    exceptional_rvol = _allocator_float(du_cfg.get("weak_catalyst_exceptional_rvol"), 3.0)
    reduction = _allocator_float(du_cfg.get("weak_catalyst_late_entry_reduction"), 0.5)
    reduction = max(0.05, min(1.0, reduction))
    gain = max(
        _allocator_float(candidate.get("gain_pct"), 0.0),
        _allocator_float(candidate.get("day_gain_pct"), 0.0),
    )
    rel = _allocator_preserved_dynamic_relative_volume(candidate)
    if rel is None:
        rel = max(
            _allocator_float(candidate.get("relative_volume"), 0.0),
            _allocator_float(candidate.get("rel_volume"), 0.0),
        )
    above_vwap = _allocator_dynamic_price_above_vwap(candidate)
    breakout = _allocator_dynamic_breakout_confirmed(candidate)
    spread_ok = True
    if spread_pct is not None and spread_cap_pct is not None:
        try:
            spread_ok = float(spread_pct) <= float(spread_cap_pct) + 1e-9
        except (TypeError, ValueError):
            spread_ok = False
    vwap_ext = _allocator_dynamic_vwap_extension_pct(candidate)
    max_vwap_ext = _allocator_float(du_cfg.get("max_entry_vwap_extension_pct"), 8.0)
    vwap_extension_ok = vwap_ext is None or float(vwap_ext) <= max_vwap_ext + 1e-9
    exceptional = bool(
        rel >= exceptional_rvol - 1e-9
        and above_vwap
        and breakout
        and spread_ok
        and vwap_extension_ok
    )
    base = {
        "gain_pct": float(gain),
        "max_gain_pct": float(max_gain),
        "reduce_floor_pct": float(reduce_floor),
        "relative_volume": float(rel),
        "exceptional_rvol": float(exceptional_rvol),
        "above_vwap": bool(above_vwap),
        "breakout": bool(breakout),
        "spread_ok": bool(spread_ok),
        "vwap_extension_pct": vwap_ext,
        "max_vwap_extension_pct": float(max_vwap_ext),
        "exceptional": bool(exceptional),
        "factor": 1.0,
    }
    if gain > max_gain + 1e-9:
        if exceptional:
            return {**base, "action": "allow", "reason": "exceptional_confirmation"}
        return {**base, "action": "block", "reason": "late_chase_protection"}
    if gain >= reduce_floor - 1e-9:
        return {**base, "action": "reduce", "reason": "late_entry_reduction", "factor": reduction}
    return {**base, "action": "allow", "reason": "within_early_entry_window"}


def _allocator_preserved_dynamic_relative_volume(candidate: Mapping[str, Any]) -> float | None:
    values = [
        _allocator_diag_float(
            candidate,
            "scanner_relative_volume",
            "scanner_rel_volume",
            "scanner_rvol",
            "relative_volume",
            "rel_volume",
        ),
        _allocator_diag_float(
            candidate,
            "entry_relative_volume",
            "entry_rel_volume",
            "entry_rvol",
            "relative_volume",
            "rel_volume",
            "scanner_relative_volume",
            "scanner_rel_volume",
        ),
        _allocator_diag_float(
            candidate,
            "allocator_relative_volume",
            "allocator_rel_volume",
            "allocated_relative_volume",
            "relative_volume",
            "rel_volume",
            "scanner_relative_volume",
            "scanner_rel_volume",
        ),
    ]
    present = [float(value) for value in values if value is not None]
    return max(present) if present else None


def _allocator_dynamic_momentum_override_bypasses_rvol_dispatch(
    candidate: Mapping[str, Any],
    *,
    user_id: Any,
    config: Mapping[str, Any] | None,
) -> bool:
    upstream_approved, _reason = _allocator_dynamic_rvol_upstream_approval(candidate)
    if not upstream_approved:
        return False
    if _allocator_paper_execution_context(user_id=user_id, config=config):
        return True
    preserved_rel = _allocator_preserved_dynamic_relative_volume(candidate)
    if preserved_rel is None:
        return False
    base_min = _allocator_dynamic_min_relative_volume(config)
    effective_min = _allocator_dynamic_effective_min_relative_volume(
        candidate,
        base_min_relative_volume=base_min,
    )
    return preserved_rel + 1e-9 >= effective_min


def _allocator_copy_dynamic_dispatch_metadata(
    row: dict[str, Any],
    candidate: Mapping[str, Any],
) -> None:
    for key in _DYNAMIC_DISPATCH_METADATA_KEYS:
        if key in row:
            continue
        value = candidate.get(key)
        if value is None or str(value).strip() == "":
            continue
        row[key] = value
    if "scanner_relative_volume" not in row:
        scanner_rel = _allocator_diag_value(candidate, "scanner_relative_volume", "scanner_rel_volume")
        if scanner_rel is None:
            scanner_rel = _allocator_diag_value(candidate, "relative_volume", "rel_volume")
        if scanner_rel is not None:
            row["scanner_relative_volume"] = scanner_rel
    if "entry_relative_volume" not in row:
        entry_rel = _allocator_diag_value(candidate, "entry_relative_volume", "entry_rel_volume")
        if entry_rel is None:
            entry_rel = _allocator_diag_value(
                candidate,
                "scanner_relative_volume",
                "scanner_rel_volume",
                "relative_volume",
                "rel_volume",
            )
        if entry_rel is not None:
            row["entry_relative_volume"] = entry_rel
    if "allocator_relative_volume" not in row:
        allocator_rel = _allocator_diag_value(candidate, "allocator_relative_volume", "allocator_rel_volume")
        if allocator_rel is None:
            allocator_rel = _allocator_diag_value(candidate, "relative_volume", "rel_volume", "scanner_relative_volume")
        if allocator_rel is not None:
            row["allocator_relative_volume"] = allocator_rel
    if "dispatch_relative_volume" not in row:
        dispatch_rel = _allocator_diag_value(
            candidate,
            "dispatch_relative_volume",
            "dispatch_rel_volume",
            "execution_relative_volume",
            "execution_rel_volume",
            "current_relative_volume",
            "current_rel_volume",
        )
        if dispatch_rel is not None:
            row["dispatch_relative_volume"] = dispatch_rel


def _allocator_dispatch_candidate_metadata(
    row: Mapping[str, Any] | None,
    candidate: Mapping[str, Any] | None,
    action: Mapping[str, Any] | None,
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for src in (candidate, row, action):
        if not isinstance(src, Mapping):
            continue
        for key, value in src.items():
            if value is None or str(value).strip() == "":
                continue
            merged[key] = value
    for src in (candidate, row, action):
        if isinstance(src, Mapping):
            _allocator_copy_dynamic_dispatch_metadata(merged, src)
    return merged


def _allocator_dynamic_dispatch_rvol_context(
    candidate: Mapping[str, Any],
    *,
    user_id: Any,
    config: Mapping[str, Any] | None,
) -> dict[str, float]:
    scanner_rel = _allocator_diag_float(
        candidate,
        "scanner_relative_volume",
        "scanner_rel_volume",
        "scanner_rvol",
        "relative_volume",
        "rel_volume",
    )
    allocator_rel = _allocator_diag_float(
        candidate,
        "allocator_relative_volume",
        "allocator_rel_volume",
        "allocated_relative_volume",
        "relative_volume",
        "rel_volume",
        "scanner_relative_volume",
        "scanner_rel_volume",
    )
    entry_rel = _allocator_diag_float(
        candidate,
        "entry_relative_volume",
        "entry_rel_volume",
        "entry_rvol",
        "scanner_relative_volume",
        "scanner_rel_volume",
        "relative_volume",
        "rel_volume",
    )
    execution_rel = _allocator_diag_float(
        candidate,
        "execution_relative_volume",
        "execution_rel_volume",
        "current_relative_volume",
        "current_rel_volume",
        "relative_volume",
        "rel_volume",
    )
    dispatch_rel = _allocator_diag_float(
        candidate,
        "dispatch_relative_volume",
        "dispatch_rel_volume",
        "execution_relative_volume",
        "execution_rel_volume",
        "current_relative_volume",
        "current_rel_volume",
    )
    if _allocator_paper_execution_context(user_id=user_id, config=config):
        observed = max(
            [value for value in (scanner_rel, entry_rel, allocator_rel, execution_rel, dispatch_rel) if value is not None],
            default=0.0,
        )
    elif _allocator_dynamic_momentum_override_entry_approved(candidate):
        preserved_rel = _allocator_preserved_dynamic_relative_volume(candidate)
        observed = preserved_rel if preserved_rel is not None else (execution_rel if execution_rel is not None else 0.0)
    else:
        observed = execution_rel if execution_rel is not None else (dispatch_rel if dispatch_rel is not None else 0.0)
    return {
        "scanner_relative_volume": float(scanner_rel if scanner_rel is not None else 0.0),
        "entry_relative_volume": float(entry_rel if entry_rel is not None else 0.0),
        "allocator_relative_volume": float(allocator_rel if allocator_rel is not None else 0.0),
        "execution_relative_volume": float(execution_rel if execution_rel is not None else 0.0),
        "dispatch_relative_volume": float(dispatch_rel if dispatch_rel is not None else 0.0),
        "observed_relative_volume": float(observed),
    }


def _allocator_dynamic_override_context(candidate: Mapping[str, Any]) -> dict[str, bool]:
    catalyst_override = _allocator_premarket_catalyst_replay_bypasses_rvol(candidate) or _cfg_bool(
        candidate.get("catalyst_rvol_override"),
        default=False,
    ) or _cfg_bool(candidate.get("rvol_override_active"), default=False)
    pure_momentum_override = _cfg_bool(candidate.get("pure_momentum_override"), default=False) or _cfg_bool(
        candidate.get("pure_momentum_allowed"),
        default=False,
    )
    return {
        "catalyst_override_active": bool(catalyst_override),
        "pure_momentum_override_active": bool(pure_momentum_override),
    }


def _allocator_dynamic_vwap_dispatch_context(
    candidate: Mapping[str, Any],
    *,
    user_id: Any,
    config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    price = _allocator_diag_float(
        candidate,
        "paper_current_price",
        "current_price",
        "price",
        "execution_price",
    )
    vwap = _allocator_diag_float(
        candidate,
        "paper_session_vwap",
        "session_vwap",
        "vwap",
    )
    distance_pct: float | None = None
    if price is not None and vwap is not None and vwap > 0.0:
        distance_pct = ((float(price) - float(vwap)) / float(vwap)) * 100.0
    scanner_vwap_above = _allocator_diag_value(
        candidate,
        "scanner_vwap_above",
        "scanner_price_above_vwap",
    )
    entry_vwap_above = _allocator_diag_value(
        candidate,
        "entry_vwap_above",
        "entry_price_above_vwap",
        "price_above_vwap",
        "vwap_above",
        "price_filter_passed",
    )
    allocator_vwap_above = _allocator_diag_value(
        candidate,
        "allocator_vwap_above",
        "allocator_price_above_vwap",
        "order_vwap_above",
        "order_price_above_vwap",
    )
    upstream_vwap_approved = any(
        key in candidate and _cfg_bool(candidate.get(key), default=False)
        for key in (
            "scanner_vwap_above",
            "scanner_price_above_vwap",
            "entry_vwap_above",
            "entry_price_above_vwap",
            "price_above_vwap",
            "vwap_above",
            "price_filter_passed",
            "allocator_vwap_above",
            "allocator_price_above_vwap",
            "order_vwap_above",
            "order_price_above_vwap",
        )
    )
    entry_approved = _allocator_dynamic_momentum_override_entry_approved(candidate)
    paper_entry_override_active = (
        _allocator_paper_execution_context(user_id=user_id, config=config)
        and entry_approved
    )
    threshold_pct = 0.0
    dispatch_below_vwap = bool(
        price is not None
        and vwap is not None
        and float(vwap) > 0.0
        and float(price) < float(vwap) - 1e-9
    )
    return {
        "price": price,
        "vwap": vwap,
        "distance_pct": distance_pct,
        "threshold_pct": threshold_pct,
        "scanner_vwap_above": None
        if scanner_vwap_above is None
        else _cfg_bool(scanner_vwap_above, default=False),
        "entry_vwap_above": None
        if entry_vwap_above is None
        else _cfg_bool(entry_vwap_above, default=False),
        "allocator_vwap_above": None
        if allocator_vwap_above is None
        else _cfg_bool(allocator_vwap_above, default=False),
        "upstream_vwap_approved": bool(upstream_vwap_approved),
        "entry_approved": bool(entry_approved),
        "paper_entry_override_active": bool(paper_entry_override_active),
        "dispatch_below_vwap": bool(dispatch_below_vwap),
    }


def _dynamic_dispatch_rvol_metadata(
    *,
    symbol: str,
    candidate: Mapping[str, Any],
    route: Any,
    source: Any,
    rel_volume: float,
    base_min_rel_volume: float,
    override_active: bool,
    dispatch_result: str,
    dispatch_reason: str,
) -> dict[str, Any]:
    route_text = _allocator_diag_text(candidate, "route") if _allocator_diag_value(candidate, "route") is not None else str(route or "n/a")
    source_text = _allocator_diag_text(candidate, "source") if _allocator_diag_value(candidate, "source") is not None else str(source or "n/a")
    scanner_effective = _allocator_diag_float(
        candidate,
        "scanner_effective_min_rel_volume",
        "scanner_effective_min_relative_volume",
        "effective_min_rel_volume",
        "effective_min_relative_volume",
    )
    entry_effective = _allocator_diag_float(
        candidate,
        "entry_effective_min_rel_volume",
        "entry_eval_effective_min_rel_volume",
        "entry_effective_min_relative_volume",
    )
    allocator_effective = _allocator_diag_float(
        candidate,
        "allocator_effective_min_rel_volume",
        "allocator_effective_min_relative_volume",
        "effective_min_rel_volume",
        "effective_min_relative_volume",
    )
    dispatch_effective = _allocator_diag_float(
        candidate,
        "dispatch_effective_min_rel_volume",
        "dispatch_effective_min_relative_volume",
    )
    effective_min = scanner_effective if scanner_effective is not None else entry_effective
    if effective_min is None:
        effective_min = float(base_min_rel_volume)
    rvol_context = _allocator_dynamic_dispatch_rvol_context(
        candidate,
        user_id="paper",
        config={"broker": {"paper": True}},
    )
    override_context = _allocator_dynamic_override_context(candidate)
    required = (
        "relative_volume",
        "news_score",
        "catalyst_score",
        "event_score",
        "effective_min_rel_volume",
    )
    aliases = {
        "relative_volume": ("relative_volume", "rel_volume"),
        "effective_min_rel_volume": (
            "scanner_effective_min_rel_volume",
            "scanner_effective_min_relative_volume",
            "effective_min_rel_volume",
            "effective_min_relative_volume",
            "entry_effective_min_rel_volume",
            "entry_eval_effective_min_rel_volume",
            "entry_effective_min_relative_volume",
        ),
    }
    missing: list[str] = []
    for field in required:
        keys = aliases.get(field, (field,))
        if _allocator_diag_value(candidate, *keys) is None:
            missing.append(field)
    return {
        "symbol": str(symbol or "").strip().upper(),
        "route": route_text,
        "source": source_text,
        "dynamic_candidate": allocation_profile_is_dynamic_candidate(candidate),
        "rel_volume": float(rel_volume),
        "scanner_relative_volume": rvol_context["scanner_relative_volume"],
        "entry_relative_volume": rvol_context["entry_relative_volume"],
        "allocator_relative_volume": rvol_context["allocator_relative_volume"],
        "execution_relative_volume": rvol_context["execution_relative_volume"],
        "dispatch_relative_volume": rvol_context["dispatch_relative_volume"],
        "base_min_rel_volume": float(base_min_rel_volume),
        "effective_min_rel_volume": float(effective_min),
        "scanner_threshold": float(scanner_effective if scanner_effective is not None else effective_min),
        "entry_threshold": float(entry_effective if entry_effective is not None else effective_min),
        "allocator_threshold": float(allocator_effective if allocator_effective is not None else effective_min),
        "dispatch_threshold": float(dispatch_effective if dispatch_effective is not None else effective_min),
        "override_active": bool(override_active),
        "catalyst_override_active": bool(override_context["catalyst_override_active"]),
        "pure_momentum_override_active": bool(override_context["pure_momentum_override_active"]),
        "news_score": _allocator_diag_float(candidate, "news_score"),
        "catalyst_score": _allocator_diag_float(candidate, "catalyst_score"),
        "event_score": _allocator_diag_float(candidate, "event_score"),
        "catalyst_type": _allocator_diag_text(candidate, "catalyst_type"),
        "catalyst_age_minutes": _allocator_diag_float(candidate, "catalyst_age_minutes", "age_minutes"),
        "scanner_effective_min_rel_volume": scanner_effective,
        "entry_eval_route": _allocator_diag_text(candidate, "entry_eval_route", "route"),
        "decision_allowed": _allocator_diag_text(candidate, "decision_allowed", "allowed"),
        "dispatch_result": str(dispatch_result or "unknown"),
        "dispatch_reason": str(dispatch_reason or "unknown"),
        "missing_fields": missing,
        "available_keys": sorted(str(key) for key in candidate.keys()),
    }


def _log_dispatch_dynamic_rvol_diagnostics(
    *,
    symbol: str,
    candidate: Mapping[str, Any],
    route: Any,
    source: Any,
    rel_volume: float,
    base_min_rel_volume: float,
    override_active: bool,
    dispatch_result: str,
    dispatch_reason: str,
) -> dict[str, Any]:
    meta = _dynamic_dispatch_rvol_metadata(
        symbol=symbol,
        candidate=candidate,
        route=route,
        source=source,
        rel_volume=rel_volume,
        base_min_rel_volume=base_min_rel_volume,
        override_active=override_active,
        dispatch_result=dispatch_result,
        dispatch_reason=dispatch_reason,
    )
    log.info(
        "DISPATCH_DYNAMIC_RVOL_CHECK symbol=%s route=%s source=%s dynamic_candidate=%s "
        "rel_volume=%.3f scanner_relative_volume=%.3f entry_relative_volume=%.3f "
        "allocator_relative_volume=%.3f execution_relative_volume=%.3f dispatch_relative_volume=%.3f "
        "base_min_rel_volume=%.3f effective_min_rel_volume=%.3f scanner_threshold=%.3f "
        "entry_threshold=%.3f allocator_threshold=%.3f dispatch_threshold=%.3f "
        "override_active=%s news_score=%s catalyst_score=%s event_score=%s catalyst_type=%s "
        "catalyst_age_minutes=%s scanner_effective_min_rel_volume=%s entry_eval_route=%s "
        "decision_allowed=%s catalyst_override_active=%s pure_momentum_override_active=%s "
        "dispatch_result=%s dispatch_reason=%s",
        meta["symbol"],
        meta["route"],
        meta["source"],
        str(bool(meta["dynamic_candidate"])).lower(),
        float(meta["rel_volume"]),
        float(meta["scanner_relative_volume"]),
        float(meta["entry_relative_volume"]),
        float(meta["allocator_relative_volume"]),
        float(meta["execution_relative_volume"]),
        float(meta["dispatch_relative_volume"]),
        float(meta["base_min_rel_volume"]),
        float(meta["effective_min_rel_volume"]),
        float(meta["scanner_threshold"]),
        float(meta["entry_threshold"]),
        float(meta["allocator_threshold"]),
        float(meta["dispatch_threshold"]),
        str(bool(meta["override_active"])).lower(),
        "n/a" if meta["news_score"] is None else f"{float(meta['news_score']):.2f}",
        "n/a" if meta["catalyst_score"] is None else f"{float(meta['catalyst_score']):.2f}",
        "n/a" if meta["event_score"] is None else f"{float(meta['event_score']):.2f}",
        meta["catalyst_type"],
        "n/a" if meta["catalyst_age_minutes"] is None else f"{float(meta['catalyst_age_minutes']):.2f}",
        "n/a"
        if meta["scanner_effective_min_rel_volume"] is None
        else f"{float(meta['scanner_effective_min_rel_volume']):.3f}",
        meta["entry_eval_route"],
        meta["decision_allowed"],
        str(bool(meta["catalyst_override_active"])).lower(),
        str(bool(meta["pure_momentum_override_active"])).lower(),
        meta["dispatch_result"],
        meta["dispatch_reason"],
    )
    if meta["missing_fields"]:
        log.info(
            "DISPATCH_DYNAMIC_METADATA_MISSING symbol=%s missing_fields=%s available_keys=%s",
            meta["symbol"],
            ",".join(meta["missing_fields"]),
            ",".join(meta["available_keys"]) or "none",
        )
    return meta


def _allocator_existing_line_value(
    portfolio: Sequence[Mapping[str, Any]],
    symbol: str,
) -> float:
    sym_u = str(symbol or "").strip().upper()
    for row in portfolio:
        if str(row.get("symbol") or "").strip().upper() != sym_u:
            continue
        return max(0.0, _allocator_float(row.get("value"), 0.0))
    return 0.0


def _apply_high_conviction_rotation_relaxation(
    candidates: Sequence[Mapping[str, Any]],
    *,
    config: Mapping[str, Any] | None,
    ca_cfg: Mapping[str, Any],
    portfolio: Sequence[Mapping[str, Any]],
    tracked: Mapping[str, Any] | None,
    equity: float,
    min_realloc_leg: float,
    allow_allocator_buys: bool,
    cycle_risk_state: Mapping[str, Any] | None,
    exit_context: Any | None,
    engine: Any | None,
) -> list[dict[str, Any]]:
    hc_cfg = _allocator_high_conviction_cfg(config, ca_cfg)
    old_ratio = _allocator_float(
        ca_cfg.get("replacement_strength_ratio", hc_cfg.get("normal_replacement_strength_ratio", 1.0)),
        _allocator_float(hc_cfg.get("normal_replacement_strength_ratio", 1.0), 1.0),
    )
    new_ratio = _allocator_float(hc_cfg.get("replacement_strength_ratio"), old_ratio)
    out: list[dict[str, Any]] = []
    targets = allocation_target_fractions(config)
    dyn_value = dynamic_position_value(portfolio, tracked)
    dyn_count = dynamic_position_count(portfolio, tracked)
    dyn_cap = max(0.0, float(equity) * float(targets.get("dynamic", 0.0) or 0.0))
    dyn_slots = max(0, 6 - int(dyn_count))
    single_dyn_cap = max(0.0, float(equity) * 0.04)
    daily_loss_active, daily_loss_source = _allocator_daily_loss_lockout_state(
        allow_allocator_buys=bool(allow_allocator_buys),
        cycle_risk_state=cycle_risk_state,
    )
    dynamic_lockout = dynamic_lockout_reason(engine, float(equity)) if engine is not None else None
    min_leg = max(0.0, float(min_realloc_leg))

    for src in candidates:
        row = dict(src)
        sym = str(row.get("symbol") or "").strip().upper()
        eligible, reason = _allocator_candidate_high_conviction(row, hc_cfg)
        if not eligible:
            out.append(row)
            continue
        reject_reason: str | None = None
        if new_ratio >= old_ratio - 1e-12:
            reject_reason = "replacement_ratio_not_lower"
        elif daily_loss_active:
            reject_reason = f"daily_loss_lockout:{daily_loss_source}"
        elif dynamic_lockout is not None:
            reject_reason = f"dynamic_lockout:{dynamic_lockout}"
        elif dyn_cap <= 1e-9 or dyn_value >= dyn_cap - 1e-9:
            reject_reason = "dynamic_sleeve_cap_exceeded"
        elif dyn_slots <= 0:
            reject_reason = "dynamic_position_limit"
        else:
            held_value = _allocator_existing_line_value(portfolio, sym)
            single_headroom = max(0.0, single_dyn_cap - held_value)
            if single_headroom < min_leg - 1e-9:
                reject_reason = "max_single_dynamic_notional"
        if reject_reason is None and exit_context is not None:
            cool_active, cool_reason, _next = _allocator_bulk_cooldown_state(exit_context, sym)
            if cool_active:
                reject_reason = f"cooldown:{cool_reason}"
        if reject_reason is not None:
            log.info(
                "HIGH_CONVICTION_ROTATION_REJECTED symbol=%s reason=%s old_ratio=%.6f new_ratio=%.6f dynamic_value=%.2f dynamic_cap=%.2f dynamic_count=%d dynamic_slots=%d",
                sym or "?",
                reject_reason,
                float(old_ratio),
                float(new_ratio),
                float(dyn_value),
                float(dyn_cap),
                int(dyn_count),
                int(dyn_slots),
            )
            out.append(row)
            continue
        row["high_conviction_rotation_relaxed"] = True
        row["replacement_strength_ratio_override"] = float(new_ratio)
        row["replacement_strength_ratio_original"] = float(old_ratio)
        row["high_conviction_rotation_reason"] = reason
        log.info(
            "HIGH_CONVICTION_ROTATION_RELAXED symbol=%s old_ratio=%.6f new_ratio=%.6f news_score=%.2f event_score=%.2f catalyst_score=%.2f rel_volume=%.2f dynamic_value=%.2f dynamic_cap=%.2f",
            sym,
            float(old_ratio),
            float(new_ratio),
            _allocator_float(row.get("news_score"), 0.0),
            _allocator_float(row.get("event_score"), 0.0),
            _allocator_scaled_catalyst_score(row.get("catalyst_score")),
            max(_allocator_float(row.get("relative_volume"), 0.0), _allocator_float(row.get("rel_volume"), 0.0)),
            float(dyn_value),
            float(dyn_cap),
        )
        out.append(row)
    return out


def _order_symbol_upper(order: Any) -> str:
    if isinstance(order, Mapping):
        return str(order.get("symbol") or "").strip().upper()
    return str(getattr(order, "symbol", "") or "").strip().upper()


def _open_order_symbols_for_broker(broker: Any) -> set[str]:
    get_fn = getattr(broker, "get_open_orders", None)
    if not callable(get_fn):
        return set()
    try:
        rows = get_fn() or []
    except Exception:
        log.warning("capital_allocator: get_open_orders failed", exc_info=True)
        return set()
    out: set[str] = set()
    try:
        iterator = iter(rows)
    except TypeError:
        return out
    for row in iterator:
        sym = _order_symbol_upper(row)
        if sym:
            out.add(sym)
    return out


def _position_held_for_orders_qty(positions: Sequence[Any], symbol: str) -> float:
    sym_u = str(symbol or "").strip().upper()
    if not sym_u:
        return 0.0
    for row in positions:
        row_sym = _position_row_symbol(row)
        if row_sym != sym_u:
            continue
        for key in ("held_for_orders", "qty_held_for_orders"):
            raw = row.get(key) if isinstance(row, Mapping) else getattr(row, key, None)
            try:
                qty = max(0.0, float(raw or 0.0))
            except (TypeError, ValueError):
                qty = 0.0
            if qty > 0.0:
                return qty
    return 0.0


def _signal_score_0_to_100(row: Mapping[str, Any] | None) -> float:
    if not isinstance(row, Mapping):
        return 0.0
    for key in ("signal_score", "score", "priority_score", "composite_score", "strength_eff"):
        raw = row.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(val):
            continue
        return val * 100.0 if 0.0 <= val <= 1.0 else val
    return 0.0


def _allocator_add_on_cooldown_state(
    *,
    symbol: str,
    signal_row: Mapping[str, Any] | None,
    tracked: Mapping[str, Any] | None,
    et_date_iso: str | None,
    now_dt: Any | None = None,
    core_rebuild: bool = False,
) -> dict[str, Any]:
    sym_u = str(symbol or "").strip().upper()
    tracked_map = tracked if isinstance(tracked, Mapping) else {}
    row = tracked_map.get(sym_u) if sym_u else None
    row = row if isinstance(row, Mapping) else {}
    try:
        qty_before = int(float(row.get("qty") or 0))
    except (TypeError, ValueError):
        qty_before = 0
    adds_today = 0
    if sym_u and et_date_iso:
        adds_today = tracked_add_on_count_for_et_day(
            tracked_map,
            sym_u,
            str(et_date_iso),
        )
    signal_score = _signal_score_0_to_100(signal_row)
    add_on_used_today = bool(qty_before > 0 and adds_today >= 1)
    blocked = bool(add_on_used_today and signal_score < 85.0 and not core_rebuild)
    dynamic_followthrough_allowed = False
    dynamic_reject_reason = ""
    if blocked and _allocator_is_dynamic_momentum_override(signal_row):
        dynamic_followthrough_allowed, dynamic_reject_reason = _dynamic_add_on_followthrough_allowed(
            signal_row=signal_row,
            tracked_row=row,
            adds_today=adds_today,
            now_dt=now_dt,
        )
        if dynamic_followthrough_allowed:
            blocked = False
            log.info(
                "DYNAMIC_ADDON_ALLOWED symbol=%s reason=dynamic_followthrough",
                sym_u or "?",
            )
        else:
            log.info(
                "DYNAMIC_ADDON_REJECT symbol=%s reason=%s",
                sym_u or "?",
                dynamic_reject_reason or "dynamic_followthrough_failed",
            )
    return {
        "symbol": sym_u,
        "qty_before": qty_before,
        "add_on_used_today": add_on_used_today,
        "adds_today": adds_today,
        "signal_score": signal_score,
        "blocked": blocked,
        "cooldown_expiry": f"after_et_date:{et_date_iso}" if blocked and et_date_iso else "n/a",
        "reason": "allocator_add_on_once_per_day" if blocked else "allowed",
        "dynamic_followthrough_allowed": dynamic_followthrough_allowed,
        "dynamic_reject_reason": dynamic_reject_reason,
    }


def _allocator_is_dynamic_momentum_override(row: Mapping[str, Any] | None) -> bool:
    if not isinstance(row, Mapping):
        return False
    for key in ("route", "source", "entry_route", "entry_source"):
        if str(row.get(key) or "").strip().lower() == "dynamic_momentum_override":
            return True
    return False


def _allocator_signal_float(row: Mapping[str, Any] | None, *keys: str) -> float | None:
    if not isinstance(row, Mapping):
        return None
    for key in keys:
        raw = row.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            return value
    return None


def _allocator_signal_bool(row: Mapping[str, Any] | None, *keys: str) -> bool:
    if not isinstance(row, Mapping):
        return False
    for key in keys:
        raw = row.get(key)
        if isinstance(raw, str):
            if raw.strip().lower() in {"1", "true", "yes", "y", "on", "pass", "passed"}:
                return True
            continue
        if bool(raw):
            return True
    return False


def _dynamic_add_on_followthrough_allowed(
    *,
    signal_row: Mapping[str, Any] | None,
    tracked_row: Mapping[str, Any],
    adds_today: int,
    now_dt: Any | None,
) -> tuple[bool, str]:
    if adds_today != 1:
        return False, "dynamic_add_limit"
    if now_dt is None:
        return False, "missing_now"
    last_add_raw = tracked_row.get("last_add_time") or tracked_row.get("last_scale_ts") or tracked_row.get("entry_time")
    if not last_add_raw:
        return False, "missing_last_add_time"
    minutes_since = minutes_since_iso(str(last_add_raw), now_dt)
    if minutes_since is None:
        return False, "invalid_last_add_time"
    if minutes_since < 45.0:
        return False, "dynamic_followthrough_cooldown"

    current_price = _allocator_signal_float(signal_row, "price", "current_price", "last_price", "mid", "mark")
    last_entry = _allocator_signal_float(tracked_row, "last_entry_price", "entry_price", "avg_entry_price")
    first_add_green = bool(current_price is not None and last_entry is not None and current_price > last_entry)
    above_vwap = _allocator_signal_bool(
        signal_row,
        "price_above_vwap",
        "vwap_above",
        "entry_eval_vwap_above",
        "entry_alignment_pass",
        "alignment_pass",
    )
    if not above_vwap:
        vwap = _allocator_signal_float(signal_row, "vwap", "session_vwap", "paper_session_vwap")
        above_vwap = bool(current_price is not None and vwap is not None and current_price > vwap)
    if not (first_add_green or above_vwap):
        return False, "not_green_or_above_vwap"
    return True, "dynamic_followthrough"


def _log_allocator_cooldown_state(prefix: str, state: Mapping[str, Any]) -> None:
    log.info(
        "%s symbol=%s cooldown_expiry=%s add_on_used_today=%s signal_score=%.1f "
        "adds_today=%s qty_before=%s reason=%s",
        prefix,
        str(state.get("symbol") or "?"),
        str(state.get("cooldown_expiry") or "n/a"),
        bool(state.get("add_on_used_today")),
        _allocator_float(state.get("signal_score"), 0.0),
        state.get("adds_today", "n/a"),
        state.get("qty_before", "n/a"),
        str(state.get("reason") or "n/a"),
    )


def trend_long_strength_uses_equity_allocator(
    *,
    strength_eff: float,
    strong_signal_strength_min: float,
    options_enabled: bool,
    options_allow_new_entries: bool,
) -> bool:
    """
    Return True if this pass should add the symbol to the **post-scan equity** allocator batch.

    When *options* are enabled and new option entries are allowed, treat
    ``strength_eff > strong_signal_strength_min`` (same key as
    ``execution.strong_signal_strength_min``) as **options** routing; otherwise **equity**
    (allocator stock notional).
    """
    if not options_enabled or not options_allow_new_entries:
        return True
    try:
        thr = float(strong_signal_strength_min)
    except (TypeError, ValueError):
        thr = 0.85
    thr = max(0.0, min(1.0, thr))
    try:
        se = float(strength_eff)
    except (TypeError, ValueError):
        se = 0.0
    return se <= thr + 1e-12


def clean_notional(x: object, min_notional: float = 1.0) -> float:
    """
    Parse *x* to a non-negative USD notional, quantize to cents with ``ROUND_DOWN``,
    and return ``0.0`` if the result is below *min_notional* or *x* is not finite / valid.
    """
    try:
        raw = float(x)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(raw) or raw < 0:
        return 0.0
    n = float(
        Decimal(str(max(0.0, raw))).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    )
    return n if n >= min_notional else 0.0


def ensure_minimum_viable_allocator_buy_notional(
    notional: float,
    *,
    ref_price: float,
) -> float:
    """
    For stock BUYs, enforce a one-share minimum at *ref_price*.

    Real-system rule:
    ``if qty == 0: qty = 1; notional = price``

    This keeps allocator clips from degenerating into ``qty == 0`` on high-priced names when the
    plan is expressed in notional dollars.
    """
    try:
        amt = float(notional)
        px = float(ref_price)
    except (TypeError, ValueError):
        return clean_notional(notional, min_notional=0.0)
    if not math.isfinite(amt) or amt <= 0.0:
        return 0.0
    if not math.isfinite(px) or px <= 0.0:
        return clean_notional(amt, min_notional=0.0)
    one_share = float(Decimal(str(px)).quantize(Decimal("0.01"), rounding=ROUND_UP))
    amt_clean = clean_notional(amt, min_notional=0.0)
    est_qty = int(amt_clean / one_share) if one_share > 0 else 0
    if est_qty <= 0:
        return one_share
    return amt_clean


def place_order(
    broker: Any,
    engine: Any,
    action: Mapping[str, Any],
    *,
    mid_price: float,
    spread_pct: float,
    ignore_spread_gate: bool = False,
    bid: float | None = None,
    ask: float | None = None,
    before_submit: Any | None = None,
    config: Mapping[str, Any] | None = None,
    data_dir: Path | str | None = None,
    user_id: str | None = None,
) -> Any | None:
    """
    Submit one allocator **market DAY notional** row (tests / direct calls).

    *action* uses ``symbol``, ``notional``, and ``action`` (``\"buy\"`` / ``\"sell\"``) — same intent as::

        api.submit_order(
            symbol=action["symbol"],
            notional=action["notional"],
            side=action["action"],
            type="market",
            time_in_force="day",
        )

    Spread is enforced first via :meth:`~src.execution.ExecutionManager.build_order_from_dict`.
    Live Alpaca uses :meth:`~src.brokers.alpaca_client.AlpacaBroker.submit_notional_market_day`.
    Test doubles and other brokers use ``broker.submit_order(req)`` with the built
    :class:`~src.execution.OrderRequest`.
    """
    sym = str(action.get("symbol") or "").strip().upper()
    side = str(action.get("action") or action.get("side") or "").strip().lower()
    if side not in ("buy", "sell"):
        return None
    n = clean_notional(action.get("notional", 0))
    if not sym or n <= 0:
        return None
    if side == "sell" and _allocator_action_is_full_exit(action):
        if callable(before_submit):
            before_submit()
        return submit_fractional_full_close(
            broker,
            sym,
            reason=_allocator_full_exit_reason(action),
            prefer_close_position=True,
        )
    spec = {
        "symbol": sym,
        "side": side,
        "notional": n,
        "route": action.get("route"),
        "source": action.get("source") or action.get("route"),
        "strategy": action.get("strategy") or action.get("route") or action.get("source"),
        "core_rebuild": action.get("core_rebuild"),
    }
    allocator_requested_notional = n
    allocator_requested_qty = None
    if mid_price and float(mid_price) > 0:
        allocator_requested_qty = int(float(n) / float(mid_price))
    pilot_metadata: dict[str, Any] = {}
    bypass_min_trade_dollars = False
    if config is not None and data_dir is not None and user_id and bounded_live_pilot_active(config):
        pilot_req = OrderRequest(
            symbol=sym,
            side=side,
            quantity=0,
            order_type=OrderType.MARKET,
            expected_price=float(mid_price),
            notional=n,
        )
        pilot_req.route = action.get("route")
        pilot_req.source = action.get("source")
        pilot_req.strategy = action.get("strategy") or action.get("route") or action.get("source")
        pilot_req.user_id = str(user_id)
        pilot_req.instrument_type = "equity"
        pilot_req.core_rebuild = action.get("core_rebuild")
        decision = adjust_pilot_order_size(
            config,
            pilot_req,
            data_dir=data_dir,
            user_id=str(user_id),
            broker=broker,
            reference_price=float(mid_price),
        )
        if not decision.allowed:
            reason = decision.reason or "limited_live_size_blocked"
            setattr(engine.execution, "last_order_build_reject_reason", reason)
            log.error(
                "LIMITED_LIVE_ORDER_BLOCKED reason=%s user_id=%s symbol=%s broker_dispatch_attempted=false",
                reason,
                user_id,
                sym,
            )
            return None
        adjusted_notional = clean_notional(getattr(pilot_req, "notional", n), min_notional=0.0)
        if bool(getattr(pilot_req, "_limited_live_sized", False)) and adjusted_notional > 0.0:
            pilot_metadata = {
                "allocator_requested_notional": getattr(pilot_req, "_allocator_requested_notional", allocator_requested_notional),
                "allocator_requested_qty": getattr(pilot_req, "_allocator_requested_qty", allocator_requested_qty),
                "bounded_pilot_applied": True,
                "final_submitted_qty": getattr(pilot_req, "_limited_live_final_quantity", getattr(pilot_req, "quantity", None)),
                "final_reference_price": getattr(pilot_req, "_limited_live_reference_price", getattr(pilot_req, "expected_price", None)),
                "final_estimated_notional": getattr(pilot_req, "_limited_live_final_notional", adjusted_notional),
            }
            n = adjusted_notional
            spec["notional"] = n
            bypass_min_trade_dollars = True
    req = engine.execution.build_order_from_dict(
        spec,
        mid_price=float(mid_price),
        spread_pct=float(spread_pct),
        ignore_spread_gate=ignore_spread_gate,
        bid=bid,
        ask=ask,
        bypass_min_trade_dollars=bypass_min_trade_dollars,
    )
    if not req:
        return None
    for key, value in pilot_metadata.items():
        setattr(req, f"_{key}", value)
    if not hasattr(req, "_allocator_requested_notional"):
        setattr(req, "_allocator_requested_notional", allocator_requested_notional)
    if not hasattr(req, "_allocator_requested_qty"):
        setattr(req, "_allocator_requested_qty", allocator_requested_qty)
    if callable(before_submit):
        before_submit()
    if isinstance(broker, AlpacaBroker):
        result = broker.submit_notional_market_day(
            {
                "symbol": sym,
                "notional": n,
                "action": side,
                "route": spec.get("route"),
                "source": spec.get("source"),
                "strategy": spec.get("strategy"),
                **pilot_metadata,
            }
        )
    else:
        result = broker.submit_order(req)
    if result is not None:
        for key, value in {
            "allocator_requested_notional": allocator_requested_notional,
            "allocator_requested_qty": allocator_requested_qty,
            "bounded_pilot_applied": bool(pilot_metadata),
            "final_submitted_qty": pilot_metadata.get("final_submitted_qty"),
            "final_reference_price": pilot_metadata.get("final_reference_price"),
            "final_estimated_notional": pilot_metadata.get("final_estimated_notional", n),
            "broker_request_type": "notional",
        }.items():
            try:
                setattr(result, f"_{key}", value)
            except Exception:
                pass
    return result


def _allocator_full_exit_reason(action: Mapping[str, Any]) -> str:
    for key in (
        "exit_reason",
        "reason",
        "route",
        "source",
        "action_reason",
        "sell_reason",
    ):
        value = action.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return "full_exit"


def _allocator_action_is_full_exit(action: Mapping[str, Any]) -> bool:
    if bool(action.get("full_exit") or action.get("reduce_to_zero")):
        return True
    for key in (
        "exit_reason",
        "reason",
        "route",
        "source",
        "action_reason",
        "sell_reason",
    ):
        if is_full_exit_reason(action.get(key)):
            return True
    return False


def dedupe_cap_alloc_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    ranking_mode: str | None = None,
) -> list[dict[str, Any]]:
    """Keep the best row per ``sym_u`` by rank score (composite or signal priority), then ``strength_eff``."""

    def _rank_primary(row_o: Mapping[str, Any]) -> float:
        m = (ranking_mode or "").strip().lower()
        if m == SIGNAL_RANKING_MODE_SIGNAL_PRIORITY:
            return row_signal_priority_score(row_o)
        if m == SIGNAL_RANKING_MODE_MRV or m in ("momentum_rs_volume", "mrv"):
            return row_momentum_rs_volume_score(row_o)
        if m == SIGNAL_RANKING_MODE_MVE or m in ("momentum_volume_ema", "mve"):
            return row_momentum_volume_ema_score(row_o)
        return row_composite_score(row_o)

    best: dict[str, dict[str, Any]] = {}
    for r in rows:
        row = dict(r)
        su = str(row.get("sym_u") or row.get("symbol") or "").strip().upper()
        if not su:
            continue
        cs = _rank_primary(row)
        se = float(row.get("strength_eff", 0.0))
        if su not in best:
            row["sym_u"] = su
            best[su] = row
            continue
        prev = best[su]
        pcs = _rank_primary(prev)
        pse = float(prev.get("strength_eff", 0.0))
        if cs > pcs or (cs == pcs and se > pse):
            row["sym_u"] = su
            best[su] = row
    return sorted(
        best.values(),
        key=lambda x: (-_rank_primary(x), -float(x.get("strength_eff", 0.0))),
    )


def compute_score(symbol: str, tracked: Mapping[str, Any] | None) -> float:
    """
    Portfolio book score for *symbol* — persisted tracker strength
    (:func:`~src.portfolio_replacement.tracked_signal_strength`), aligned with scan ``strength_eff``.
    """
    su = str(symbol or "").strip().upper()
    trow = (tracked or {}).get(su) if isinstance(tracked, dict) else None
    return float(tracked_signal_strength(trow if isinstance(trow, dict) else None))


def allocator_position_score(symbol: str, tracked: Mapping[str, Any] | None) -> float:
    """Alias for :func:`compute_score` (backward compatible name)."""
    return compute_score(symbol, tracked)


def _position_row_symbol(p: Any) -> str:
    if isinstance(p, Mapping):
        return str(p.get("symbol") or "").strip().upper()
    return str(getattr(p, "symbol", None) or "").strip().upper()


def _position_row_market_value_usd(p: Any, *, sym: str, positions: list[Any]) -> float:
    """``p.market_value`` when present on the row; else broker aggregate for *sym*."""
    raw: Any
    if isinstance(p, Mapping):
        raw = p.get("market_value")
    else:
        raw = getattr(p, "market_value", None)
    try:
        v = abs(float(raw)) if raw is not None and str(raw).strip() != "" else 0.0
    except (TypeError, ValueError):
        v = 0.0
    if v > 0:
        return v
    if positions and all(isinstance(x, Mapping) for x in positions):
        return symbol_long_position_market_value_usd(
            [x for x in positions if isinstance(x, Mapping)], sym
        )
    return 0.0


def build_allocator_portfolio(
    positions: list[Any],
    tracked: dict[str, Any],
    eligible_upper: set[str],
) -> list[dict[str, Any]]:
    """
    Portfolio rows for the allocator::

        portfolio = [
            {"symbol": p.symbol, "value": p.market_value, "score": compute_score(p.symbol)}
            for p in positions
        ]

    Rows are mapping rows from Alpaca (``symbol`` / ``market_value`` keys) or objects with the same
    attributes. One row per symbol (first qualifying row wins); *eligible_upper* and OCC options
    are skipped.
    """
    portfolio: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in positions:
        sym = _position_row_symbol(p)
        if not sym or sym in seen or sym not in eligible_upper or is_option_symbol(sym):
            continue
        value = _position_row_market_value_usd(p, sym=sym, positions=positions)
        if value <= 0:
            continue
        seen.add(sym)
        portfolio.append(
            {
                "symbol": sym,
                "value": float(value),
                "score": compute_score(sym, tracked),
                "dynamic_candidate": tracked_row_is_dynamic(
                    tracked.get(sym) if isinstance(tracked, dict) else None
                ),
            }
        )
    portfolio.sort(key=lambda r: str(r.get("symbol") or ""))
    return portfolio


def rank_allocator_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    ``rank_signals()`` for allocator: sort by ``score`` descending (same order as
    :meth:`src.capital_allocator.CapitalAllocator.allocate`). Use ``rank_allocator_candidates(s)[:3]``
    for a **top-3** view; to bias dollar tranches, enable ``concentration_bias`` in
    ``portfolio.capital_allocator`` (see :class:`~src.capital_allocator.CapitalAllocator`).
    """
    if not candidates:
        return []

    def _sc(row: dict[str, Any]) -> float:
        try:
            return float(row.get("score", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    return sorted(candidates, key=_sc, reverse=True)


def _normalized_correlation_groups(
    raw_groups: Mapping[str, Any] | None,
) -> dict[str, frozenset[str]]:
    out: dict[str, frozenset[str]] = {}
    if not isinstance(raw_groups, Mapping):
        return out
    for key, raw_syms in raw_groups.items():
        if not isinstance(raw_syms, (list, tuple, set)):
            continue
        syms = frozenset(
            str(x).strip().upper() for x in raw_syms if str(x).strip()
        )
        if syms:
            out[str(key).strip().lower()] = syms
    return out


def _allocator_filter_reject_reason(
    candidate: Mapping[str, Any],
    *,
    dyn_lockout: str | None,
    config: Mapping[str, Any] | None = None,
) -> str:
    sym = str(candidate.get("symbol") or "").strip().upper()
    if not sym:
        return "missing_symbol"
    if is_excluded_dynamic_etf(sym):
        return "etf_excluded"
    if allocation_profile_is_dynamic_candidate(candidate):
        if dyn_lockout is not None:
            return dyn_lockout
        quality = dynamic_quality_decision(candidate, config=config)
        pure_rel = quality["pure_momentum_rel_volume"]
        pure_gain = quality["pure_momentum_gain_pct"]
        log.info(
            "DYNAMIC_ALLOCATOR_INPUT symbol=%s route=%s source=%s score=%.2f gain=%s rel=%s "
            "catalyst_score=%.2f news_score=%.2f event_score=%.2f",
            sym,
            _allocator_diag_field(candidate, "route", default="n/a"),
            _allocator_diag_field(candidate, "source", default="n/a"),
            float(quality["pure_momentum_score"]),
            "n/a" if pure_gain is None else "%.3f" % float(pure_gain),
            "n/a" if pure_rel is None else "%.3f" % float(pure_rel),
            float(quality["catalyst_score"]),
            float(quality["news_score"]),
            float(quality["event_score"]),
        )
        reason = None if bool(quality["passes"]) else str(quality["reason"] or "no_catalyst")
        if reason is not None:
            if reason == "no_catalyst":
                log.info(
                    "DYNAMIC_ALLOCATOR_CATALYST_REQUIRED symbol=%s reason=no_catalyst",
                    sym,
                )
                missing_fields = [
                    name
                    for name, value in (
                        ("score", quality["pure_momentum_score"]),
                        ("rel", quality["pure_momentum_rel_volume"]),
                        ("gain", quality["pure_momentum_gain_pct"]),
                    )
                    if value is None or (name == "score" and float(value) <= 0.0)
                ]
                if missing_fields:
                    log.info(
                        "DYNAMIC_ALLOCATOR_NO_CATALYST_REJECT symbol=%s missing_fields=%s",
                        sym,
                        ",".join(missing_fields),
                    )
                log.info(
                    "DYNAMIC_ALLOCATOR_NO_CATALYST_REJECT symbol=%s score=%.2f rel=%s gain=%s required_score=%.2f",
                    sym,
                    float(quality["pure_momentum_score"]),
                    "n/a" if pure_rel is None else "%.3f" % float(pure_rel),
                    "n/a" if pure_gain is None else "%.3f" % float(pure_gain),
                    float(quality["pure_momentum_min_score"]),
                )
            return reason
        if str(quality["path"]) == "pure_momentum":
            log.info(
                "DYNAMIC_ALLOCATOR_PURE_MOMENTUM_PASS symbol=%s score=%.2f rel=%s gain=%s",
                sym,
                float(quality["pure_momentum_score"]),
                "n/a" if pure_rel is None else "%.3f" % float(pure_rel),
                "n/a" if pure_gain is None else "%.3f" % float(pure_gain),
            )
        elif str(quality["path"]) == "scanner_selected":
            log.info(
                "DYNAMIC_ALLOCATOR_CATALYST_BYPASS symbol=%s reason=scanner_selected",
                sym,
            )
            _log_dynamic_allocator_low_score_allowed_from_quality(candidate, quality)
    return "profile_filter"


def _allocator_diag_field(
    candidate: Mapping[str, Any],
    *keys: str,
    default: Any = 0.0,
) -> Any:
    for key in keys:
        raw = candidate.get(key)
        if raw is not None and str(raw).strip() != "":
            return raw
    return default


def _allocator_optional_float(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return "%.3f" % float(value)
    except (TypeError, ValueError):
        return "n/a"


def _allocator_dynamic_missing_fields(quality: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    score = quality.get("pure_momentum_score")
    if score is None:
        missing.append("score")
    else:
        try:
            if float(score) <= 0.0:
                missing.append("score")
        except (TypeError, ValueError):
            missing.append("score")
    if quality.get("pure_momentum_rel_volume") is None:
        missing.append("rel")
    if quality.get("pure_momentum_gain_pct") is None:
        missing.append("gain")
    return missing


def _log_allocator_drop_reason_debug(candidate: Mapping[str, Any], *, reason: str) -> None:
    sym = str(candidate.get("symbol") or candidate.get("sym_u") or "").strip().upper()
    log.info(
        "ALLOCATOR_DROP_REASON_DEBUG symbol=%s reason=%s route=%s source=%s is_dynamic=%s "
        "score=%s dynamic_score=%s scanner_score=%s signal_score=%s gain_pct=%s "
        "day_gain_pct=%s relative_volume=%s rel_volume=%s",
        sym or "?",
        str(reason),
        _allocator_diag_field(candidate, "route", default="n/a"),
        _allocator_diag_field(candidate, "source", default="n/a"),
        str(bool(allocation_profile_is_dynamic_candidate(candidate))).lower(),
        _allocator_diag_field(candidate, "score", default="n/a"),
        _allocator_diag_field(candidate, "dynamic_score", default="n/a"),
        _allocator_diag_field(candidate, "scanner_score", default="n/a"),
        _allocator_diag_field(candidate, "signal_score", default="n/a"),
        _allocator_diag_field(candidate, "gain_pct", default="n/a"),
        _allocator_diag_field(candidate, "day_gain_pct", default="n/a"),
        _allocator_diag_field(candidate, "relative_volume", default="n/a"),
        _allocator_diag_field(candidate, "rel_volume", default="n/a"),
    )


def _log_dynamic_allocator_input_from_quality(
    candidate: Mapping[str, Any],
    quality: Mapping[str, Any],
) -> None:
    sym = str(candidate.get("symbol") or candidate.get("sym_u") or "").strip().upper()
    log.info(
        "DYNAMIC_ALLOCATOR_INPUT symbol=%s route=%s source=%s score=%.2f gain=%s rel=%s "
        "catalyst_score=%.2f news_score=%.2f event_score=%.2f",
        sym or "?",
        _allocator_diag_field(candidate, "route", default="n/a"),
        _allocator_diag_field(candidate, "source", default="n/a"),
        float(quality["pure_momentum_score"]),
        _allocator_optional_float(quality["pure_momentum_gain_pct"]),
        _allocator_optional_float(quality["pure_momentum_rel_volume"]),
        float(quality["catalyst_score"]),
        float(quality["news_score"]),
        float(quality["event_score"]),
    )


def _log_dynamic_allocator_low_score_allowed_from_quality(
    candidate: Mapping[str, Any],
    quality: Mapping[str, Any],
) -> None:
    if float(quality["pure_momentum_score"]) >= float(quality["pure_momentum_min_score"]):
        return
    sym = str(candidate.get("symbol") or candidate.get("sym_u") or "").strip().upper()
    log.info(
        "DYNAMIC_ALLOCATOR_LOW_SCORE_ALLOWED symbol=%s score=%.2f reason=scanner_selected",
        sym or "?",
        float(quality["pure_momentum_score"]),
    )


def _log_dynamic_allocator_no_catalyst_from_quality(
    candidate: Mapping[str, Any],
    quality: Mapping[str, Any],
) -> None:
    sym = str(candidate.get("symbol") or candidate.get("sym_u") or "").strip().upper()
    missing_fields = _allocator_dynamic_missing_fields(quality)
    if missing_fields:
        log.info(
            "DYNAMIC_ALLOCATOR_NO_CATALYST_REJECT symbol=%s missing_fields=%s",
            sym or "?",
            ",".join(missing_fields),
        )
    log.info(
        "DYNAMIC_ALLOCATOR_NO_CATALYST_REJECT symbol=%s score=%.2f rel=%s gain=%s required_score=%.2f",
        sym or "?",
        float(quality["pure_momentum_score"]),
        _allocator_optional_float(quality["pure_momentum_rel_volume"]),
        _allocator_optional_float(quality["pure_momentum_gain_pct"]),
        float(quality["pure_momentum_min_score"]),
    )


def _allocator_no_catalyst_allowed_by_dynamic_quality(
    candidate: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None,
) -> bool:
    if not allocation_profile_is_dynamic_candidate(candidate):
        return False
    quality = dynamic_quality_decision(candidate, config=config)
    _log_dynamic_allocator_input_from_quality(candidate, quality)
    if bool(quality["passes"]):
        if str(quality["path"]) == "pure_momentum":
            sym = str(candidate.get("symbol") or candidate.get("sym_u") or "").strip().upper()
            log.info(
                "DYNAMIC_ALLOCATOR_PURE_MOMENTUM_PASS symbol=%s score=%.2f rel=%s gain=%s",
                sym or "?",
                float(quality["pure_momentum_score"]),
                _allocator_optional_float(quality["pure_momentum_rel_volume"]),
                _allocator_optional_float(quality["pure_momentum_gain_pct"]),
            )
        elif str(quality["path"]) == "scanner_selected":
            sym = str(candidate.get("symbol") or candidate.get("sym_u") or "").strip().upper()
            log.info(
                "DYNAMIC_ALLOCATOR_CATALYST_BYPASS symbol=%s reason=scanner_selected",
                sym or "?",
            )
            _log_dynamic_allocator_low_score_allowed_from_quality(candidate, quality)
        return True
    _log_dynamic_allocator_no_catalyst_from_quality(candidate, quality)
    sym = str(candidate.get("symbol") or candidate.get("sym_u") or "").strip().upper()
    log.info(
        "DYNAMIC_ALLOCATOR_CATALYST_REQUIRED symbol=%s reason=no_catalyst",
        sym or "?",
    )
    return False


def _log_allocator_dynamic_quality_snapshot(
    candidate: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None,
) -> None:
    if not allocation_profile_is_dynamic_candidate(candidate):
        return
    quality = dynamic_quality_decision(candidate, config=config)
    _log_dynamic_allocator_input_from_quality(candidate, quality)
    if bool(quality["passes"]):
        if str(quality["path"]) == "pure_momentum":
            sym = str(candidate.get("symbol") or candidate.get("sym_u") or "").strip().upper()
            log.info(
                "DYNAMIC_ALLOCATOR_PURE_MOMENTUM_PASS symbol=%s score=%.2f rel=%s gain=%s",
                sym or "?",
                float(quality["pure_momentum_score"]),
                _allocator_optional_float(quality["pure_momentum_rel_volume"]),
                _allocator_optional_float(quality["pure_momentum_gain_pct"]),
            )
        elif str(quality["path"]) == "scanner_selected":
            sym = str(candidate.get("symbol") or candidate.get("sym_u") or "").strip().upper()
            log.info(
                "DYNAMIC_ALLOCATOR_CATALYST_BYPASS symbol=%s reason=scanner_selected",
                sym or "?",
            )
            _log_dynamic_allocator_low_score_allowed_from_quality(candidate, quality)
        return
    if str(quality["reason"] or "no_catalyst") == "no_catalyst":
        _log_dynamic_allocator_no_catalyst_from_quality(candidate, quality)
        sym = str(candidate.get("symbol") or candidate.get("sym_u") or "").strip().upper()
        log.info(
            "DYNAMIC_ALLOCATOR_CATALYST_REQUIRED symbol=%s reason=no_catalyst",
            sym or "?",
        )


def _record_allocator_drop_reason(
    drop_reasons: dict[str, str],
    candidate: Mapping[str, Any],
    *,
    reason: str,
    config: Mapping[str, Any] | None,
) -> bool:
    sym = str(candidate.get("symbol") or candidate.get("sym_u") or "").strip().upper()
    _log_allocator_drop_reason_debug(candidate, reason=reason)
    if reason == "no_catalyst" and _allocator_no_catalyst_allowed_by_dynamic_quality(candidate, config=config):
        log.info(
            "ALLOCATOR_DROP_REASON_BYPASS symbol=%s reason=no_catalyst path=pure_momentum",
            sym or "?",
        )
        return False
    if sym:
        drop_reasons[sym] = reason
    return bool(sym)


def _final_allocator_candidate_lookup(
    *collections: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Mapping[str, Any]]:
    lookup: dict[str, Mapping[str, Any]] = {}
    for collection in collections:
        if collection is None:
            continue
        rows: Sequence[Mapping[str, Any]]
        if isinstance(collection, Mapping):
            rows = [row for row in collection.values() if isinstance(row, Mapping)]
        else:
            rows = [row for row in collection if isinstance(row, Mapping)]
        for row in rows:
            sym = str(row.get("symbol") or row.get("sym_u") or "").strip().upper()
            if sym and sym not in lookup:
                lookup[sym] = row
    return lookup


def _log_allocator_final_reject_reason_debug(
    *,
    symbol: str,
    reason: str,
    candidate: Mapping[str, Any] | None,
) -> None:
    row: Mapping[str, Any] = candidate if isinstance(candidate, Mapping) else {}
    log.info(
        "ALLOCATOR_FINAL_REJECT_REASON_DEBUG symbol=%s reason=%s candidate_present=%s "
        "route=%s source=%s is_dynamic=%s score=%s dynamic_score=%s scanner_score=%s "
        "signal_score=%s gain_pct=%s day_gain_pct=%s relative_volume=%s rel_volume=%s "
        "catalyst_score=%s news_score=%s event_score=%s",
        str(symbol or "?").strip().upper(),
        str(reason),
        str(bool(candidate)).lower(),
        _allocator_diag_field(row, "route", default="n/a"),
        _allocator_diag_field(row, "source", default="n/a"),
        str(bool(allocation_profile_is_dynamic_candidate(row))).lower(),
        _allocator_diag_field(row, "score", default="n/a"),
        _allocator_diag_field(row, "dynamic_score", default="n/a"),
        _allocator_diag_field(row, "scanner_score", default="n/a"),
        _allocator_diag_field(row, "signal_score", default="n/a"),
        _allocator_diag_field(row, "gain_pct", default="n/a"),
        _allocator_diag_field(row, "day_gain_pct", default="n/a"),
        _allocator_diag_field(row, "relative_volume", default="n/a"),
        _allocator_diag_field(row, "rel_volume", default="n/a"),
        _allocator_diag_field(row, "catalyst_score", default="n/a"),
        _allocator_diag_field(row, "news_score", default="n/a"),
        _allocator_diag_field(row, "event_score", default="n/a"),
    )


def _dynamic_override_entry_eval_final(candidate: Mapping[str, Any]) -> bool:
    route = str(candidate.get("route") or candidate.get("source") or "").strip().lower()
    if route != "dynamic_momentum_override":
        return False
    return (
        _cfg_bool(candidate.get("entry_eval_final"), default=False)
        or _cfg_bool(candidate.get("final"), default=False)
        or _cfg_bool(candidate.get("ENTRY_EVAL_PASS"), default=False)
    )


def _log_dynamic_override_no_catalyst_threshold_failure(
    *,
    symbol: str,
    quality: Mapping[str, Any],
) -> None:
    missing_fields = _allocator_dynamic_missing_fields(quality)
    if missing_fields:
        log.info(
            "DYNAMIC_OVERRIDE_NO_CATALYST_THRESHOLD_FAIL symbol=%s missing_fields=%s",
            symbol or "?",
            ",".join(missing_fields),
        )
    log.info(
        "DYNAMIC_OVERRIDE_NO_CATALYST_THRESHOLD_FAIL symbol=%s score=%.2f rel=%s gain=%s "
        "required_score=%.2f required_rel=%.3f required_gain=%.3f score_ok=%s rel_ok=%s gain_ok=%s route_ok=%s",
        symbol or "?",
        float(quality["pure_momentum_score"]),
        _allocator_optional_float(quality["pure_momentum_rel_volume"]),
        _allocator_optional_float(quality["pure_momentum_gain_pct"]),
        float(quality["pure_momentum_min_score"]),
        float(quality["pure_momentum_min_rvol"]),
        float(quality["pure_momentum_min_gain_pct"]),
        str(bool(quality["pure_momentum_score_ok"])).lower(),
        str(bool(quality["pure_momentum_rel_volume_ok"])).lower(),
        str(bool(quality["pure_momentum_gain_pct_ok"])).lower(),
        str(bool(quality["pure_momentum_route_ok"])).lower(),
    )


def _finalize_allocator_reject_reasons_for_print(
    drop_reasons: Mapping[str, str],
    *,
    candidate_lookup: Mapping[str, Mapping[str, Any]],
    config: Mapping[str, Any] | None,
) -> dict[str, str]:
    finalized: dict[str, str] = {}
    for sym_raw, reason_raw in sorted(drop_reasons.items()):
        sym = str(sym_raw).strip().upper()
        reason = str(reason_raw)
        candidate = candidate_lookup.get(sym)
        _log_allocator_final_reject_reason_debug(
            symbol=sym,
            reason=reason,
            candidate=candidate,
        )
        if reason == "no_catalyst" and isinstance(candidate, Mapping):
            route = str(
                candidate.get("route") or candidate.get("source") or ""
            ).strip().lower()
            if _dynamic_override_entry_eval_final(candidate):
                quality = dynamic_quality_decision(candidate, config=config)
                _log_dynamic_allocator_input_from_quality(candidate, quality)
                if bool(quality["passes"]):
                    if str(quality["path"]) == "pure_momentum":
                        log.info(
                            "DYNAMIC_ALLOCATOR_PURE_MOMENTUM_PASS symbol=%s score=%.2f rel=%s gain=%s",
                            sym or "?",
                            float(quality["pure_momentum_score"]),
                            _allocator_optional_float(quality["pure_momentum_rel_volume"]),
                            _allocator_optional_float(quality["pure_momentum_gain_pct"]),
                        )
                    elif str(quality["path"]) == "scanner_selected":
                        log.info(
                            "DYNAMIC_ALLOCATOR_CATALYST_BYPASS symbol=%s reason=scanner_selected",
                            sym or "?",
                        )
                        _log_dynamic_allocator_low_score_allowed_from_quality(candidate, quality)
                    log.info(
                        "ALLOCATOR_FINAL_REJECT_REASON_BYPASS symbol=%s reason=no_catalyst path=%s",
                        sym or "?",
                        str(quality["path"]),
                    )
                    continue
                _log_dynamic_override_no_catalyst_threshold_failure(
                    symbol=sym,
                    quality=quality,
                )
                _log_dynamic_allocator_no_catalyst_from_quality(candidate, quality)
            elif route in {"dynamic_momentum", "dynamic_momentum_override"}:
                log.info(
                    "DYNAMIC_OVERRIDE_NO_CATALYST_THRESHOLD_FAIL symbol=%s reason=entry_eval_not_final route=%s",
                    sym or "?",
                    route or "n/a",
                )
        if sym:
            finalized[sym] = reason
    return finalized


def _log_allocator_filter_rejections(
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
    *,
    dyn_lockout: str | None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    dropped: dict[str, str] = {}
    remaining = Counter(
        str(row.get("symbol") or "").strip().upper()
        for row in after
        if str(row.get("symbol") or "").strip()
    )
    for candidate in before:
        sym = str(candidate.get("symbol") or "").strip().upper()
        if sym and remaining[sym] > 0:
            remaining[sym] -= 1
            continue
        reason = _allocator_filter_reject_reason(candidate, dyn_lockout=dyn_lockout, config=config)
        recorded = False
        if sym:
            recorded = _record_allocator_drop_reason(dropped, candidate, reason=reason, config=config)
        if reason == "no_catalyst" and not recorded:
            continue
        log.info(
            "ALLOCATOR_FILTER_REJECT symbol=%s reason=%s score=%s catalyst_score=%s "
            "event_score=%s news_score=%s age_minutes=%s route=%s",
            sym or "?",
            reason,
            _allocator_diag_field(candidate, "score"),
            _allocator_diag_field(candidate, "catalyst_score"),
            _allocator_diag_field(candidate, "event_score"),
            _allocator_diag_field(candidate, "news_score"),
            _allocator_diag_field(candidate, "age_minutes", "catalyst_age_minutes", default="n/a"),
            _allocator_diag_field(candidate, "route", "source", default="n/a"),
        )
        _log_allocator_reject_reason(
            candidate,
            reason=reason,
            stage="profile_filter",
        )
    return dropped


def _allocator_action_order_id(order: Any) -> str:
    if order is None:
        return "n/a"
    if isinstance(order, Mapping):
        return str(order.get("id") or order.get("order_id") or "n/a")
    return str(getattr(order, "id", None) or getattr(order, "order_id", None) or "n/a")


def _allocation_value_pct(value: float, equity: float) -> float:
    if equity <= 0.0:
        return 0.0
    return max(0.0, float(value)) / float(equity) * 100.0


def _log_allocation_gap_report(
    *,
    config: Mapping[str, Any] | None,
    portfolio: Sequence[Mapping[str, Any]],
    tracked: Mapping[str, Any] | None,
    equity: float,
    cash: float,
) -> dict[str, float]:
    targets = allocation_target_fractions(config)
    eq = max(0.0, float(equity or 0.0))
    core_value = 0.0
    for row in portfolio:
        if not is_core_stock(row.get("symbol")):
            continue
        try:
            core_value += max(0.0, float(row.get("value", 0.0) or 0.0))
        except (TypeError, ValueError):
            continue
    try:
        dynamic_value = dynamic_position_value(portfolio, tracked)
    except Exception:
        dynamic_value = 0.0
    cash_pct = _allocation_value_pct(float(cash or 0.0), eq)
    payload = {
        "target_core": float(targets.get("core", 0.0) or 0.0) * 100.0,
        "actual_core": _allocation_value_pct(core_value, eq),
        "target_dynamic": float(targets.get("dynamic", 0.0) or 0.0) * 100.0,
        "actual_dynamic": _allocation_value_pct(dynamic_value, eq),
        "cash_pct": cash_pct,
        "cash_reserve": float(targets.get("cash", 0.0) or 0.0) * 100.0,
    }
    log.info(
        "ALLOCATION_TARGETS_DETAIL core_target_pct=%.2f dynamic_target_pct=%.2f cash_reserve_pct=%.2f",
        payload["target_core"],
        payload["target_dynamic"],
        payload["cash_reserve"],
    )
    log.info(
        "ALLOCATION_ACTUALS core_pct=%.2f dynamic_pct=%.2f cash_pct=%.2f",
        payload["actual_core"],
        payload["actual_dynamic"],
        payload["cash_pct"],
    )
    log.info(
        "ALLOCATION_GAP_REPORT target_core=%.2f actual_core=%.2f target_dynamic=%.2f actual_dynamic=%.2f cash_pct=%.2f",
        payload["target_core"],
        payload["actual_core"],
        payload["target_dynamic"],
        payload["actual_dynamic"],
        payload["cash_pct"],
    )
    return payload


def _correlation_group_name_for_symbol(
    symbol: str,
    groups: Mapping[str, frozenset[str]] | None,
) -> str | None:
    sym_u = str(symbol or "").strip().upper()
    if not sym_u or not groups:
        return None
    for group_name, group_syms in groups.items():
        if sym_u in group_syms:
            return str(group_name)
    return None


def select_top_candidates_with_group_cap(
    candidates: list[dict[str, Any]],
    *,
    top_n: int,
    max_per_group: int = 0,
    correlation_groups: Mapping[str, frozenset[str]] | None = None,
    portfolio: Sequence[Mapping[str, Any]] | None = None,
    equity: float | None = None,
    default_hard_cap_frac: float | None = None,
    symbol_cap_fractions: Mapping[str, tuple[float, float]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Rank by allocator score, then keep at most *top_n* names while capping configured groups.

    Returns ``(selected, skipped_reasons)`` where each skip reason is ``\"GROUP:SYMBOL\"``.
    """
    ranked = rank_allocator_candidates(candidates)
    if not ranked or top_n <= 0:
        return [], []
    held_values: dict[str, float] = {}
    if portfolio is not None:
        for row in portfolio:
            sym_p = str(row.get("symbol") or "").strip().upper()
            if not sym_p:
                continue
            try:
                held_values[sym_p] = held_values.get(sym_p, 0.0) + max(
                    0.0, float(row.get("value", 0.0) or 0.0)
                )
            except (TypeError, ValueError):
                continue
    try:
        equity_f = float(equity) if equity is not None else 0.0
    except (TypeError, ValueError):
        equity_f = 0.0
    try:
        default_hard = (
            float(default_hard_cap_frac)
            if default_hard_cap_frac is not None
            else 0.0
        )
    except (TypeError, ValueError):
        default_hard = 0.0

    def _hard_cap_dollars(sym_u: str) -> float:
        if equity_f <= 0.0:
            return 0.0
        if symbol_cap_fractions and sym_u in symbol_cap_fractions:
            try:
                return max(0.0, float(symbol_cap_fractions[sym_u][1]) * equity_f)
            except (TypeError, ValueError, IndexError):
                return 0.0
        return max(0.0, default_hard * equity_f)

    selected: list[dict[str, Any]] = []
    skipped: list[str] = []
    per_group_counts: dict[str, int] = {}
    for row in ranked:
        if len(selected) >= top_n:
            break
        sym_u = str(row.get("symbol") or "").strip().upper()
        hcap = _hard_cap_dollars(sym_u)
        held = held_values.get(sym_u, 0.0)
        if hcap > 0.0 and held >= hcap - 1e-6:
            skipped.append(f"cap:{sym_u}")
            _print_allocator_skip(
                sym_u,
                "cap reached",
                detail="already held value $%.0f >= hard cap $%.0f" % (held, hcap),
            )
            continue
        group_name = (
            _correlation_group_name_for_symbol(sym_u, correlation_groups)
            if max_per_group > 0 and correlation_groups
            else None
        )
        if group_name is not None:
            used = int(per_group_counts.get(group_name, 0))
            if used >= max_per_group:
                skipped.append(f"{group_name}:{sym_u}")
                continue
            per_group_counts[group_name] = used + 1
        selected.append(row)
    return selected, skipped


def take_top_deploy_candidates(
    candidates: list[dict[str, Any]], *, n: int
) -> list[dict[str, Any]]:
    """
    For **deploy** mode: keep the top *n* candidates by ``score`` (descending), then
    :meth:`CapitalAllocator.allocate` runs on that list.
    """
    if not candidates or n <= 0:
        return list(candidates)
    return rank_allocator_candidates(candidates)[:n]


def _allocator_symbols_list(candidates: Sequence[Mapping[str, Any]]) -> list[str]:
    return [
        str(row.get("symbol") or row.get("sym_u") or "").strip().upper()
        for row in candidates
        if str(row.get("symbol") or row.get("sym_u") or "").strip()
    ]


def _log_allocator_post_rank_reorder(
    *,
    original_ranked: Sequence[Mapping[str, Any]],
    action_attempt: Sequence[Mapping[str, Any]],
    reason: str,
) -> None:
    ranked_symbols = _allocator_symbols_list(original_ranked)
    attempt_symbols = _allocator_symbols_list(action_attempt)
    if ranked_symbols == attempt_symbols:
        return
    log.info(
        "ALLOCATOR_POST_RANK_REORDER original_ranked_symbols=%s action_attempt_symbols=%s reason=%s",
        ",".join(ranked_symbols) if ranked_symbols else "-",
        ",".join(attempt_symbols) if attempt_symbols else "-",
        str(reason or "unknown"),
    )


def _allocator_action_signature(action: Mapping[str, Any]) -> str:
    side = str(action.get("action") or "?").strip().lower() or "?"
    symbol = str(action.get("symbol") or "?").strip().upper() or "?"
    try:
        notional = float(action.get("notional", 0.0) or 0.0)
    except (TypeError, ValueError, OverflowError):
        notional = 0.0
    return f"{side}:{symbol}:{notional:.2f}"


def _allocator_action_identity(action: Mapping[str, Any]) -> str:
    side = str(action.get("action") or "?").strip().lower() or "?"
    symbol = str(action.get("symbol") or "?").strip().upper() or "?"
    return f"{side}:{symbol}"


def _allocator_action_csv(actions: Sequence[Mapping[str, Any]]) -> str:
    if not actions:
        return "none"
    return ",".join(_allocator_action_signature(row) for row in actions)


def _allocator_removed_action_csv(
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
) -> str:
    removed = [_allocator_action_signature(row) for row in _allocator_removed_actions(before, after)]
    return ",".join(removed) if removed else "none"


def _allocator_removed_actions(
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    remaining = Counter(_allocator_action_identity(row) for row in after)
    removed: list[Mapping[str, Any]] = []
    for row in before:
        sig = _allocator_action_identity(row)
        if remaining[sig] > 0:
            remaining[sig] -= 1
            continue
        removed.append(row)
    return removed


def _log_post_planner_action_trace(
    *,
    stage: str,
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
    reason: str,
) -> str | None:
    removed_actions = _allocator_removed_actions(before, after)
    removed = ",".join(_allocator_action_signature(row) for row in removed_actions) if removed_actions else "none"
    log.info(
        "POST_PLANNER_ACTION_TRACE stage=%s before_count=%d after_count=%d "
        "before_actions=%s after_actions=%s removed_actions=%s reason=%s",
        str(stage),
        len(before),
        len(after),
        _allocator_action_csv(before),
        _allocator_action_csv(after),
        removed,
        str(reason or "n/a"),
    )
    if removed_actions and str(stage) != "consolidate_net":
        skip_reason = "post_planner_%s" % (str(stage).strip() or "unknown")
        for row in removed_actions:
            symbol = str(row.get("symbol") or "?").strip().upper() or "?"
            side = str(row.get("action") or "?").strip().lower() or "?"
            try:
                notional = float(row.get("notional", 0.0) or 0.0)
            except (TypeError, ValueError, OverflowError):
                notional = 0.0
            source = str(row.get("source") or row.get("route") or "capital_allocator")
            log.info(
                "ALLOCATOR_ACTION_BLOCKED symbol=%s reason=%s action=%s notional=%.2f route=%s",
                symbol,
                skip_reason,
                side,
                notional,
                source,
            )
            _log_order_skip(symbol, skip_reason)
            _log_allocator_dispatch_done(symbol, result="skipped", reason=skip_reason)
    return str(stage) if removed != "none" else None


def _restore_allocator_rank_order(
    *,
    original_ranked: Sequence[Mapping[str, Any]],
    selected: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not selected:
        return []
    selected_by_symbol: dict[str, list[Mapping[str, Any]]] = {}
    for row in selected:
        sym = str(row.get("symbol") or row.get("sym_u") or "").strip().upper()
        if not sym:
            continue
        selected_by_symbol.setdefault(sym, []).append(row)
    out: list[dict[str, Any]] = []
    used_counts: Counter[str] = Counter()
    for ranked in original_ranked:
        sym = str(ranked.get("symbol") or ranked.get("sym_u") or "").strip().upper()
        if not sym:
            continue
        bucket = selected_by_symbol.get(sym)
        idx = used_counts[sym]
        if bucket is None or idx >= len(bucket):
            continue
        out.append(dict(bucket[idx]))
        used_counts[sym] += 1
    for row in selected:
        sym = str(row.get("symbol") or row.get("sym_u") or "").strip().upper()
        if not sym:
            continue
        idx = used_counts[sym]
        bucket = selected_by_symbol.get(sym) or []
        if idx < len(bucket) and row is bucket[idx]:
            out.append(dict(row))
            used_counts[sym] += 1
    return out


def build_allocator_candidates(
    signals: Sequence[Any],
    *,
    ranking_mode: str | None = None,
) -> list[dict[str, Any]]:
    """
    One candidate per signal (**no** ``final`` / entry-eval filter)::

        candidates = [{"symbol": s.symbol, "score": s.score} for s in signals]

    Mapping rows use ``sym_u`` / ``symbol`` and ``priority_score`` / ``composite_score`` /
    ``strength_eff`` / ``score`` (missing → 0).

    The ``score`` key is the allocator **ranking** (explicit ``score``, else ``priority_score`` when
    present, else ``composite_score``, else ``strength_eff``). When ``strength_eff`` is present on the row, it
    is also copied onto the candidate so :meth:`src.capital_allocator.CapitalAllocator.rotate_capital`
    can compare apples-to-apples with book rows (tracker / strength scale).

    Rows flagged ``dynamic_candidate`` get a modest score boost so scanner-added names can outrank
    stale core-universe fallback names when allocator capital is scarce.
    """
    candidates: list[dict[str, Any]] = []
    for s in signals:
        if isinstance(s, Mapping):
            sym_u = str(s.get("sym_u") or s.get("symbol") or "").strip().upper()
            if not sym_u:
                continue
            rm = (ranking_mode or "").strip().lower()
            if rm == SIGNAL_RANKING_MODE_MRV or rm in ("momentum_rs_volume", "mrv"):
                raw = row_momentum_rs_volume_score(dict(s))
            elif rm == SIGNAL_RANKING_MODE_MVE or rm in ("momentum_volume_ema", "mve"):
                raw = row_momentum_volume_ema_score(dict(s))
            elif "score" in s:
                raw = s["score"]
            elif (
                "priority_score" in s
                and s.get("priority_score") is not None
                and str(s.get("priority_score")).strip() != ""
            ):
                raw = s.get("priority_score")
            elif "composite_score" in s:
                raw = s.get("composite_score")
            else:
                raw = s.get("strength_eff", 0.0)
            try:
                sc = float(raw)
            except (TypeError, ValueError):
                sc = 0.0
            dynamic_candidate = _allocator_signal_bool(
                s.get("dynamic_candidate")
            ) or _allocator_signal_bool(s.get("dynamic_symbol")) or _allocator_signal_bool(
                s.get("is_dynamic")
            ) or allocation_profile_is_dynamic_candidate(s)
            if dynamic_candidate:
                sc *= ALLOCATOR_DYNAMIC_CANDIDATE_SCORE_MULT
            row: dict[str, Any] = {"symbol": sym_u, "score": sc}
            if dynamic_candidate:
                row["dynamic_candidate"] = True
                row["score_multiplier"] = ALLOCATOR_DYNAMIC_CANDIDATE_SCORE_MULT
            _se = s.get("strength_eff")
            if _se is not None and str(_se).strip() != "":
                try:
                    row["strength_eff"] = float(_se)
                except (TypeError, ValueError):
                    pass
            for key in (
                "news_score",
                "event_score",
                "catalyst_score",
                "article_count",
                "relative_volume",
                "rel_volume",
                "scanner_relative_volume",
                "scanner_rel_volume",
                "allocator_relative_volume",
                "allocator_rel_volume",
                "execution_relative_volume",
                "execution_rel_volume",
                "effective_min_rel_volume",
                "effective_min_relative_volume",
                "scanner_effective_min_rel_volume",
                "scanner_effective_min_relative_volume",
                "entry_effective_min_rel_volume",
                "entry_eval_effective_min_rel_volume",
                "entry_effective_min_relative_volume",
                "allocator_effective_min_rel_volume",
                "allocator_effective_min_relative_volume",
                "dispatch_effective_min_rel_volume",
                "dispatch_effective_min_relative_volume",
                "catalyst_fastlane_active",
                "catalyst_min_relative_volume",
                "catalyst_min_rel_volume",
                "fastlane_min_relative_volume",
                "volume_confirmed",
                "relative_volume_confirmed",
                "price_filter_passed",
                "scanner_vwap_above",
                "scanner_price_above_vwap",
                "entry_vwap_above",
                "entry_price_above_vwap",
                "allocator_vwap_above",
                "allocator_price_above_vwap",
                "order_vwap_above",
                "order_price_above_vwap",
                "entry_alignment_passed",
                "entry_alignment_ok",
                "alignment_passed",
                "alignment_ok",
                "entry_eval_final",
                "decision_allowed",
                "final",
                "scanner_selected",
                "dynamic_scanner_selected",
                "selected_by_dynamic_scanner",
                "dynamic_selected",
                "is_dynamic",
                "dynamic_symbol",
                "weak_catalyst_dynamic",
                "unstable_quote",
                "quote_unstable",
                "recent_unstable_quote",
                "dynamic_unstable_quote",
                "vwap_above",
                "price_above_vwap",
                "source",
                "route",
                "entry_eval_route",
                "age_minutes",
                "catalyst_age_minutes",
                "catalyst_type",
                "catalyst_headline",
                "premarket_injected",
                "candidate_notional_requested",
                "requested_notional",
                "notional",
                "candidate_notional_cap",
                "max_notional",
                "signal_score",
                "dynamic_score",
                "scanner_score",
                "gain_pct",
                "day_gain_pct",
                "allocation_bucket",
                "avg_volume",
                "average_volume",
                "price",
                "current_price",
                "paper_current_price",
                "paper_session_vwap",
                "session_vwap",
                "vwap",
                "distance_from_vwap_pct",
                "dynamic_scan_ms",
                "dynamic_enqueue_ms",
                "dynamic_entry_eval_ms",
                "dynamic_allocator_ms",
                "first_seen_day_gain_pct",
                "max_day_gain_pct_seen",
                "minutes_since_first_seen",
                "minutes_since_market_open",
                "first_eligible_day_gain_pct",
                "spread_pct",
                "pure_momentum_override",
                "pure_momentum_allowed",
                "catalyst_rvol_override",
                "rvol_override_active",
                "rejection_reason",
                "skip_reason",
                "hard_reject_reason",
            ):
                raw_score = s.get(key)
                if raw_score is None or str(raw_score).strip() == "":
                    continue
                if isinstance(raw_score, bool) or key == "source":
                    row[key] = raw_score
                else:
                    try:
                        row[key] = float(raw_score)
                    except (TypeError, ValueError):
                        row[key] = raw_score
            if _allocator_weak_catalyst_dynamic(row):
                row["weak_catalyst_dynamic"] = True
            candidates.append(row)
            continue
        sym_o = getattr(s, "symbol", None) or getattr(s, "sym_u", None)
        sym_u = str(sym_o or "").strip().upper()
        if not sym_u:
            continue
        try:
            sc = float(getattr(s, "score", 0.0))
        except (TypeError, ValueError):
            sc = 0.0
        dynamic_candidate = (
            _allocator_signal_bool(getattr(s, "dynamic_candidate", False))
            or _allocator_signal_bool(getattr(s, "dynamic_symbol", False))
            or _allocator_signal_bool(getattr(s, "is_dynamic", False))
        )
        if dynamic_candidate:
            sc *= ALLOCATOR_DYNAMIC_CANDIDATE_SCORE_MULT
        row2: dict[str, Any] = {"symbol": sym_u, "score": sc}
        if dynamic_candidate:
            row2["dynamic_candidate"] = True
            row2["score_multiplier"] = ALLOCATOR_DYNAMIC_CANDIDATE_SCORE_MULT
        se_o = getattr(s, "strength_eff", None)
        if se_o is not None and str(se_o).strip() != "":
            try:
                row2["strength_eff"] = float(se_o)
            except (TypeError, ValueError):
                pass
        for key in (
            "news_score",
            "event_score",
            "catalyst_score",
            "article_count",
            "relative_volume",
            "rel_volume",
            "scanner_relative_volume",
            "scanner_rel_volume",
            "allocator_relative_volume",
            "allocator_rel_volume",
            "execution_relative_volume",
            "execution_rel_volume",
            "effective_min_rel_volume",
            "effective_min_relative_volume",
            "scanner_effective_min_rel_volume",
                "scanner_effective_min_relative_volume",
                "entry_effective_min_rel_volume",
                "entry_eval_effective_min_rel_volume",
                "entry_effective_min_relative_volume",
                "allocator_effective_min_rel_volume",
                "allocator_effective_min_relative_volume",
                "dispatch_effective_min_rel_volume",
                "dispatch_effective_min_relative_volume",
                "catalyst_fastlane_active",
                "catalyst_min_relative_volume",
                "catalyst_min_rel_volume",
                "fastlane_min_relative_volume",
                "volume_confirmed",
            "relative_volume_confirmed",
            "price_filter_passed",
            "scanner_vwap_above",
            "scanner_price_above_vwap",
            "entry_vwap_above",
            "entry_price_above_vwap",
            "allocator_vwap_above",
            "allocator_price_above_vwap",
            "order_vwap_above",
            "order_price_above_vwap",
            "entry_alignment_passed",
            "entry_alignment_ok",
            "alignment_passed",
            "alignment_ok",
            "entry_eval_final",
            "decision_allowed",
            "final",
            "scanner_selected",
            "dynamic_scanner_selected",
            "selected_by_dynamic_scanner",
            "dynamic_selected",
            "is_dynamic",
            "dynamic_symbol",
            "weak_catalyst_dynamic",
            "unstable_quote",
            "quote_unstable",
            "recent_unstable_quote",
            "dynamic_unstable_quote",
            "vwap_above",
            "price_above_vwap",
            "source",
            "route",
            "entry_eval_route",
            "age_minutes",
            "catalyst_age_minutes",
            "catalyst_type",
            "catalyst_headline",
            "premarket_injected",
            "candidate_notional_requested",
            "requested_notional",
            "notional",
            "candidate_notional_cap",
            "max_notional",
            "signal_score",
            "dynamic_score",
            "scanner_score",
            "gain_pct",
            "day_gain_pct",
            "allocation_bucket",
            "avg_volume",
            "average_volume",
            "price",
            "current_price",
            "paper_current_price",
            "paper_session_vwap",
            "session_vwap",
            "vwap",
            "distance_from_vwap_pct",
            "dynamic_scan_ms",
            "dynamic_enqueue_ms",
            "dynamic_entry_eval_ms",
            "dynamic_allocator_ms",
            "first_seen_day_gain_pct",
            "max_day_gain_pct_seen",
            "minutes_since_first_seen",
            "minutes_since_market_open",
            "first_eligible_day_gain_pct",
            "spread_pct",
            "pure_momentum_override",
            "pure_momentum_allowed",
            "catalyst_rvol_override",
            "rvol_override_active",
            "rejection_reason",
            "skip_reason",
            "hard_reject_reason",
        ):
            raw_score = getattr(s, key, None)
            if raw_score is None or str(raw_score).strip() == "":
                continue
            if isinstance(raw_score, bool) or key == "source":
                row2[key] = raw_score
            else:
                try:
                    row2[key] = float(raw_score)
                except (TypeError, ValueError):
                    row2[key] = raw_score
        if _allocator_weak_catalyst_dynamic(row2):
            row2["weak_catalyst_dynamic"] = True
        candidates.append(row2)
    return candidates


def empty_alloc_equal_split_buys(
    *,
    candidates: list[dict[str, Any]],
    cash: float,
    min_realloc_leg: float,
    top_n: int = 5,
) -> list[dict[str, Any]]:
    """
    When the allocator plan is empty, return BUY actions for the top *top_n* candidates
    by ``score`` (descending), each with ``available_cash / n`` notional, where *n* is
    the number of chosen symbols (capped at *top_n*). Skips each leg that falls below
    *min_realloc_leg* after cent quantization.
    """
    if not candidates or top_n <= 0:
        return []
    try:
        _tn = int(top_n)
    except (TypeError, ValueError):
        _tn = 5
    _tn = max(1, min(20, _tn))
    try:
        cap = max(0.0, float(cash))
    except (TypeError, ValueError):
        return []
    if not math.isfinite(cap) or cap < min_realloc_leg - 1e-9:
        return []

    def _sc(row: dict[str, Any]) -> float:
        try:
            return float(row.get("score", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    ranked = sorted(candidates, key=_sc, reverse=True)
    top = ranked[:_tn]
    n = len(top)
    if n <= 0:
        return []
    per = cap / float(n)
    out: list[dict[str, Any]] = []
    for c in top:
        sym = str(c.get("symbol", "")).strip().upper()
        if not sym:
            continue
        n_amt = clean_notional(per, min_notional=min_realloc_leg)
        if n_amt > 0:
            out.append({"action": "buy", "symbol": sym, "notional": n_amt})
    return out


def empty_alloc_fixed_size_buys(
    *,
    candidates: list[dict[str, Any]],
    equity: float,
    cash: float,
    min_realloc_leg: float,
    top_n: int,
    size_pct: float,
) -> list[dict[str, Any]]:
    """
    Fixed-size fallback BUYs for the top *top_n* candidates after repeated empty allocator cycles.

    Each chosen symbol gets up to ``equity * size_pct`` notional, capped by remaining cash and
    dropped when the resulting line falls below *min_realloc_leg* after cent quantization.
    """
    if not candidates or top_n <= 0:
        return []
    try:
        cap = max(0.0, float(cash))
        eq = max(0.0, float(equity))
        sp = float(size_pct)
    except (TypeError, ValueError):
        return []
    if not math.isfinite(cap) or not math.isfinite(eq) or not math.isfinite(sp):
        return []
    sp = max(0.0, min(1.0, sp))
    if cap < min_realloc_leg - 1e-9 or eq <= 0.0 or sp <= 0.0:
        return []
    target = eq * sp
    if target < min_realloc_leg - 1e-9:
        return []
    ranked = rank_allocator_candidates(candidates)[: max(1, min(20, int(top_n)))]
    remaining = cap
    out: list[dict[str, Any]] = []
    for c in ranked:
        if remaining < min_realloc_leg - 1e-9:
            break
        sym = str(c.get("symbol", "")).strip().upper()
        if not sym:
            continue
        n_amt = clean_notional(min(target, remaining), min_notional=min_realloc_leg)
        if n_amt <= 0:
            continue
        out.append({"action": "buy", "symbol": sym, "notional": n_amt})
        remaining -= n_amt
    return out


def portfolio_rows_for_allocator(
    positions: list[Any],
    tracked: dict[str, Any],
    eligible_upper: set[str],
) -> list[dict[str, Any]]:
    """Backward-compatible alias for :func:`build_allocator_portfolio`."""
    return build_allocator_portfolio(positions, tracked, eligible_upper)


def execute_capital_allocator_pass(
    *,
    signals: list[dict[str, Any]],
    broker: Any,
    engine: Any,
    config: dict[str, Any],
    dt: Any,
    positions: list[dict[str, Any]],
    tracked: dict[str, Any],
    current_positions: dict[str, Any],
    eligible_active: list[str],
    account_equity: float,
    cash: float,
    ca_cfg: Mapping[str, Any],
    user_id: str,
    data_dir: Path | str,
    stale_quote_max_age: float,
    strength_jitter_max: float,
    et_date_iso: str | None,
    cycle_risk_state: dict[str, int] | None,
    verbose: bool,
    exit_context: AllocatorExitIntentSource | None = None,
    allow_allocator_buys: bool = True,
    gross_exposure_pct: float | None = None,
    symbol_sector: Mapping[str, str] | None = None,
    theme_map: Mapping[str, str] | None = None,
    no_recycle_block: bool = False,
    regime_score: int | None = None,
    regime_condition: str | None = None,
    entry_wave_strong_signal_count: int | None = None,
    preallocated_equal_split_buys: list[dict[str, Any]] | None = None,
) -> None:
    """
    Allocator-only execution for one pass:

    * Build ``candidates`` / ``portfolio`` (no ``final`` / entry-allowed filter on candidates).
    * ``allocate(...)`` then print allocator mode and ``"ALLOCATOR ACTIONS:"`` / *actions*.

    * When *allow_allocator_buys* is false (e.g. gross long book over the effective max vs equity),
    **buy** actions are removed after ``allocate``; **sell** actions (e.g. rotation) are kept.
    * When *gross_exposure_pct* is set and *gross long MV / equity* (0–100+ scale) implies book gross
    **fraction** ``> risk_control_gross_frac`` (default 0.95, i.e. 95%% of equity), **mode** is
    ``risk_control`` and (by default) buys are also stripped — same as above.
    * When *no_recycle_block* is true (``risk.no_recycle_above_pct`` band in the live loop), **buy**
    actions are removed after ``allocate``; **sell** actions are kept.
    * When book gross (%%) is “near” the **effective** max (``net_reduction_*``) after the net
    ``sum(sells) >= sum(buys)`` trim, aggregate buys are trimmed so
    ``sum(buys) <= net_reduction_max_buy_to_sell_ratio × sum(sells)`` (default ``0.5`` at ``0.9`` of max).
    *Regime* / *entry wave* (when passed) match :func:`src.exposure_gates.allocator_buys_disallowed_over_max_gross`.
    * **Execution:** each planned action is run with one automatic retry on exception, then the error
    is logged and the rest of the plan is still processed (partial execute).
    * When *gross_exposure_pct* is set and long gross (fraction of equity) is **below**
    ``min_gross_deployment_pct`` (default 0.85) and the book is not in risk override, **mode** is
    ``deploy`` (under-invested). In **deploy** mode, only the top *N* candidates by ``score`` are
    passed to :meth:`src.capital_allocator.CapitalAllocator.allocate` (``deploy_top_n_signals``,
    default 4, clamped 3–5), then normal notional allocation runs on that subset.
    * When the plan is still empty and ``fallback_on_empty_alloc`` is on, the loop applies
    :func:`empty_alloc_equal_split_buys`: top ``empty_alloc_top_n`` (default 5) by ``score``,
    equal split of *cash* (``cash / n``) per name, then ``apply_cooldown``.
    * When *regime_score* is 4, gross is **below** ``min_gross_deployment_pct``, and
      ``bullish_force_minimum_deploy`` (default on) is true, *force_allocate* is set: the
      ``require_net_sell_gte_buy`` post-pass is skipped so a buy-only plan is not fully trimmed
      (otherwise net notional can stay stuck under the minimum deployment band).
    """
    opts_on = bool((config.get("options") or {}).get("enabled"))
    replay_mode_diag = str((config or {}).get("_replay_mode") or "live")
    broker_mock_diag = bool((config or {}).get("_broker_mock", False))
    allow_replay_attribution = replay_mode_diag != "live" and bool(broker_mock_diag)
    market_open_diag = str((config or {}).get("_market_open", "unknown"))
    event_store = getattr(broker, "_sqlite_event_store", None)
    if opts_on:
        log.warning(
            "[%s] capital_allocator: options.enabled is true — strong trend-longs (strength > "
            "``execution.strong_signal_strength_min``) are routed to options in the main loop; "
            "this pass plans **stock** notionals for the equity batch only.",
            user_id,
        )

    elig_upper = {str(s).upper() for s in eligible_active}
    equity = float(account_equity)
    log_allocation_targets(config)
    deployable_cash = deployable_cash_after_reserve(
        cash=float(cash),
        equity=float(equity),
        config=config,
    )
    # Total long gross vs equity: available_capacity = max_gross − current_gross (same cap as exposure gates).
    _max_g_d: float | None = None
    _cur_g_d: float | None = None
    _headroom_d: float | None = None
    if gross_exposure_pct is not None and math.isfinite(equity) and equity > 0:
        try:
            _egp = float(gross_exposure_pct)
        except (TypeError, ValueError, OverflowError):
            _egp = float("nan")
        if math.isfinite(_egp):
            try:
                _egc = parse_portfolio_exposure_gates(config)
                b = float(_egc.get("max_total_exposure_frac", 0.92) or 0.92)
            except (TypeError, ValueError):
                b = 0.92
            meff = float(
                adaptive_effective_max_total_exposure(
                    dict(config) if config is not None else None,
                    base_max_total_exposure_frac=b,
                    regime_score=regime_score,
                    regime_condition=regime_condition,
                    entry_wave_strong_signal_count=entry_wave_strong_signal_count,
                )
            )
            if controlled_live_equity_active(config):
                limits = controlled_live_limits(config)
                controlled_cap = float(limits.portfolio_exposure_cap_pct) / 100.0
                if controlled_cap > 0.0:
                    meff = min(meff, controlled_cap)
            _max_g_d = max(0.0, meff * equity)
            _cur_g_d = max(0.0, _egp / 100.0 * equity)
            _headroom_d = max(0.0, _max_g_d - _cur_g_d)
            _mgipc = ca_cfg.get("max_gross_increase_per_cycle")
            if _mgipc is not None and str(_mgipc).strip() != "":
                try:
                    _incf = float(_mgipc)
                except (TypeError, ValueError, OverflowError):
                    _incf = float("nan")
                if _incf == _incf and _incf > 1.0 + 1e-9:
                    _incf = _incf / 100.0
                if _incf == _incf and _incf > 1e-12:
                    _book_cap_gross = _max_g_d
                    _cycle_ceiling = _cur_g_d + equity * max(0.0, _incf)
                    _max_g_d = min(_max_g_d, _cycle_ceiling)
                    _headroom_d = max(0.0, _max_g_d - _cur_g_d)
                    if _max_g_d + 1e-6 < _book_cap_gross:
                        log.info(
                            "[%s] capital_allocator: max_gross_increase_per_cycle caps effective "
                            "max gross to $%.0f (book cap $%.0f, +%.4f× equity vs prior gross)",
                            str(user_id),
                            _max_g_d,
                            _book_cap_gross,
                            max(0.0, _incf),
                        )

    def _risk_control_threshold_frac() -> float:
        raw = ca_cfg.get("risk_control_gross_frac", 0.95)
        try:
            t = float(raw) if raw is not None and str(raw).strip() != "" else 0.95
        except (TypeError, ValueError):
            t = 0.95
        if t > 1.0 + 1e-9:
            t = t / 100.0
        return max(0.0, min(1.0, t))

    def _min_gross_deployment_threshold_frac() -> float:
        raw = ca_cfg.get("min_gross_deployment_pct", 0.85)
        try:
            t = float(raw) if raw is not None and str(raw).strip() != "" else 0.85
        except (TypeError, ValueError):
            t = 0.85
        if t > 1.0 + 1e-9:
            t = t / 100.0
        return max(0.0, min(1.0, t))

    in_risk_control = False
    gfrac: float | None = None
    r_thr = _risk_control_threshold_frac()
    d_thr = _min_gross_deployment_threshold_frac()
    if gross_exposure_pct is not None:
        gfrac = max(0.0, float(gross_exposure_pct) / 100.0)
        in_risk_control = gfrac > r_thr + 1e-12
    rc_block = in_risk_control and bool(ca_cfg.get("risk_control_block_buys", True))
    allow_effective = (
        bool(allow_allocator_buys) and not no_recycle_block and not rc_block
    )
    if in_risk_control:
        allocator_mode = "risk_control"
    elif (
        gfrac is not None
        and d_thr > 1e-12
        and gfrac + 1e-12 < d_thr
    ):
        allocator_mode = "deploy"
    else:
        allocator_mode = "normal"
    _reg_s_i: int | None = None
    if regime_score is not None:
        try:
            _reg_s_i = int(regime_score)
        except (TypeError, ValueError):
            _reg_s_i = None
    # Under-deployed + strongest bullish: net_sell_gte_buy would strip buy-only plans — allow net adds
    # to reach min_gross_deployment (see min_gross_deployment_pct; default 0.85 == 85% of equity).
    force_allocate = bool(
        bool(ca_cfg.get("bullish_force_minimum_deploy", True))
        and not in_risk_control
        and _reg_s_i == 4
        and gfrac is not None
        and d_thr > 1e-12
        and (gfrac + 1e-12) < d_thr
    )
    if force_allocate and gross_exposure_pct is not None:
        log.info(
            "[%s] capital_allocator: force_allocate (bullish score=4, gross %.1f%% < min deploy %.0f%%) — "
            "skipping require_net_sell_gte_buy trim",
            str(user_id),
            float(gross_exposure_pct),
            d_thr * 100.0,
        )
    if gross_exposure_pct is not None:
        log.info(
            "[%s] capital_allocator: mode %s (gross %.1f%% of equity, "
            "deploy if <%.0f%%, risk_control if >%.0f%% of equity)",
            str(user_id),
            allocator_mode,
            float(gross_exposure_pct),
            d_thr * 100.0,
            r_thr * 100.0,
        )
    print("ALLOCATOR mode:", allocator_mode)

    for signal in signals:
        if isinstance(signal, Mapping):
            symbol = (
                signal.get("symbol")
                or signal.get("ticker")
                or signal.get("name")
                or signal.get("sym_u")
            )
            _sig_s = signal.get("signal")
            _sig_str = signal.get("strength")
            _sig_reas = signal.get("reason")
        else:
            symbol = (
                getattr(signal, "symbol", None)
                or getattr(signal, "ticker", None)
                or getattr(signal, "name", None)
                or getattr(signal, "sym_u", None)
            )
            _sig_s = getattr(signal, "signal", None)
            _sig_str = getattr(signal, "strength", None)
            _sig_reas = getattr(signal, "reason", None)
        symbol = str(symbol or "").strip() or "?"
        logger.info(
            "[ALLOCATOR_CANDIDATE] %s signal=%s strength=%s reason=%s "
            "allow_buys=%s cash=%.2f equity=%.2f gross=%s",
            symbol,
            _sig_s,
            _sig_str,
            _sig_reas,
            allow_allocator_buys,
            cash,
            account_equity,
            gross_exposure_pct,
        )
        if isinstance(signal, Mapping):
            record_trade_attribution_candidate(
                data_dir=data_dir,
                user_id=str(user_id),
                timestamp=dt,
                candidate=signal,
                regime_score=regime_score,
            )
    try:
        _signal_symbols = [
            str((signal or {}).get("symbol") or (signal or {}).get("sym_u") or "").strip().upper()
            for signal in signals
            if isinstance(signal, Mapping)
        ]
        _capture_diag = capture_runtime_forward_bars(
            broker=broker,
            data_dir=data_dir,
            user_id=str(user_id),
            timestamp=dt,
            symbols=_signal_symbols,
            config=config,
        )
        if isinstance(_capture_diag, Mapping) and not _capture_diag.get("skipped"):
            log.info(
                "FORWARD_BAR_CAPTURE user_id=%s symbols=%d summary=%s reason=%s",
                str(user_id),
                len(_signal_symbols),
                _capture_diag.get("summary"),
                _capture_diag.get("reason"),
            )
    except Exception:
        log.warning("FORWARD_BAR_CAPTURE_FAILED user_id=%s", str(user_id), exc_info=True)

    # --- Step 2: allocator-only (scan rows are expected to be pre-filtered in the live loop) ---
    # Build candidates (no second ``final`` filter here — *signals* = entry_eval final-true queue)
    _cfg_ac = parse_allocation_config(config if isinstance(config, dict) else None)
    _port_lb = (config.get("portfolio") if isinstance(config, dict) else {}) or {}
    _sr_lb = (
        _port_lb.get("signal_ranking")
        if isinstance(_port_lb.get("signal_ranking"), dict)
        else {}
    )
    _allocator_rank_mode = canonical_signal_ranking_mode(
        _sr_lb.get("ranking_mode"),
        allocation_rank_by_strength=bool(_cfg_ac.get("rank_by_signal_strength")),
        allocation_rank_top_k_by=str(_cfg_ac.get("rank_top_k_by") or "strength_eff"),
    )
    candidates = build_allocator_candidates(
        signals, ranking_mode=_allocator_rank_mode
    )
    _pre_liquidity_candidates = [dict(c) for c in candidates]
    candidates, _hard_liquidity_rejections = _filter_allocator_hard_liquidity_candidates(
        candidates,
        broker=broker,
        config=config if isinstance(config, Mapping) else {},
        ca_cfg=ca_cfg,
        user_id=user_id,
        dt=dt,
        stale_quote_max_age=stale_quote_max_age,
        event_store=event_store,
    )
    _ssec_ld: Mapping[str, str] = (
        symbol_sector if symbol_sector is not None else SYMBOL_SECTOR
    )
    _tmap_ld: Mapping[str, str] = theme_map if theme_map is not None else THEME_MAP
    _def_sec_ld = str(
        parse_sector_config(config).get("default_sector", "unknown")
    ).strip() or "unknown"
    candidates = apply_allocator_defensive_drift_scores(
        candidates,
        regime_score=regime_score,
        regime_condition=regime_condition,
        symbol_sector=_ssec_ld,
        theme_map=_tmap_ld,
        default_sector=_def_sec_ld,
        ca_cfg=ca_cfg,
        user_id=str(user_id),
    )
    _allocator_initial_candidates = [dict(c) for c in candidates]
    _allocator_drop_reasons: dict[str, str] = {}
    _pre_liquidity_by_symbol = {
        str(c.get("symbol") or c.get("sym_u") or "").strip().upper(): c
        for c in _pre_liquidity_candidates
        if str(c.get("symbol") or c.get("sym_u") or "").strip()
    }
    for _sym_hl, _reason_hl in _hard_liquidity_rejections.items():
        _record_allocator_drop_reason(
            _allocator_drop_reasons,
            _pre_liquidity_by_symbol.get(str(_sym_hl).strip().upper(), {"symbol": _sym_hl}),
            reason=str(_reason_hl),
            config=config if isinstance(config, Mapping) else {},
        )
    cash_pct = (float(cash) / float(equity) * 100.0) if float(equity) > 0 else 0.0
    log.info(
        "ALLOCATOR_INPUT count=%s gross=%.1f cash=%.1f",
        len(candidates),
        float(gross_exposure_pct or 0.0),
        float(cash_pct),
    )
    log.info(
        "ALLOCATOR_INPUT_SYMBOLS count=%d symbols=%s",
        len(candidates),
        _allocator_symbol_csv(candidates),
    )
    log.info(
        "ALLOCATOR_INPUT_DETAIL count=%d symbols=%s scores=%s routes=%s reasons=%s",
        len(candidates),
        _allocator_symbol_csv(candidates),
        _allocator_field_csv(candidates, "score", "strength_eff", default="0.0000"),
        _allocator_field_csv(candidates, "route", "source", default="n/a"),
        _allocator_field_csv(candidates, "reason", default="n/a"),
    )
    for _cand_input in candidates:
        _log_allocator_candidate_row(_cand_input, stage="before_filter")
        _log_allocator_dynamic_quality_snapshot(
            _cand_input,
            config=config if isinstance(config, Mapping) else {},
        )
    log.info("ALLOCATOR_STAGE_COUNT allocator_input_count=%d", len(candidates))
    if bool(ca_cfg.get("force_deploy_when_candidates_exist", False)) and candidates:
        allocator_mode = "deploy"
        force_allocate = True
        log.info(
            "[%s] capital_allocator: force_deploy_when_candidates_exist active — forcing deploy mode for %d candidate(s)",
            str(user_id),
            len(candidates),
        )
    _corr_groups = _normalized_correlation_groups(
        ca_cfg.get("correlation_groups")
        if isinstance(ca_cfg.get("correlation_groups"), Mapping)
        else None
    )
    try:
        _corr_cap = int(ca_cfg.get("correlation_max_per_group", 0) or 0)
    except (TypeError, ValueError):
        _corr_cap = 0
    _ranked_candidates_debug = rank_allocator_candidates(candidates)
    _selected_candidates_debug = list(_ranked_candidates_debug)
    _allocator_skip_reason = "not_evaluated"
    # Build portfolio (symbol, market value, tracker-aligned score)
    portfolio = build_allocator_portfolio(positions, tracked, elig_upper)
    _log_allocation_gap_report(
        config=config,
        portfolio=portfolio,
        tracked=tracked,
        equity=float(equity),
        cash=float(cash),
    )
    _cap_soft, _cap_hard = effective_capital_allocator_symbol_cap_soft_hard(
        config,
        ca_cfg,
        regime_score=regime_score,
        regime_condition=regime_condition,
        account_equity=equity,
    )
    _tier_caps = effective_capital_allocator_symbol_caps_by_symbol(
        config,
        ca_cfg,
        regime_score=regime_score,
        regime_condition=regime_condition,
        account_equity=equity,
    )

    _min_leg_cfg = float(ca_cfg.get("min_realloc_leg", 300.0) or 300.0)
    _profile_input_candidates = list(candidates)
    _profile_dyn_lockout = dynamic_lockout_reason(engine, float(equity)) if engine is not None else None
    _signal_by_symbol = {
        str(row.get("sym_u") or row.get("symbol") or "").strip().upper(): row
        for row in signals
        if isinstance(row, Mapping)
        and str(row.get("sym_u") or row.get("symbol") or "").strip()
    }
    try:
        _tracked_for_cooldown = load_tracked(str(user_id), data_dir=data_dir)
    except Exception:
        log.warning("[%s] capital_allocator: load_tracked failed for cooldown filter", str(user_id), exc_info=True)
        _tracked_for_cooldown = tracked
    candidates = filter_allocator_candidates_for_profile(
        candidates,
        config=config,
        portfolio=portfolio,
        tracked=tracked,
        equity=float(equity),
        engine=engine,
    )
    log.info("ALLOCATOR_STAGE_COUNT post_profile_filter_count=%d", len(candidates))
    _allocator_drop_reasons.update(_log_allocator_filter_rejections(
        _profile_input_candidates,
        candidates,
        dyn_lockout=_profile_dyn_lockout,
        config=config,
    ))
    _cooldown_filtered_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        sym_cd = str(candidate.get("symbol") or "").strip().upper()
        is_core_rebuild = bool(candidate.get("core_rebuild")) or str(
            candidate.get("route") or candidate.get("source") or ""
        ).strip().lower() == "core_rebuild"
        state_cd = _allocator_add_on_cooldown_state(
            symbol=sym_cd,
            signal_row=_signal_by_symbol.get(sym_cd),
            tracked=_tracked_for_cooldown,
            et_date_iso=str(et_date_iso) if et_date_iso else None,
            now_dt=dt,
            core_rebuild=is_core_rebuild,
        )
        _log_allocator_cooldown_state("ALLOCATOR_COOLDOWN_STATE", state_cd)
        if bool(state_cd.get("blocked")):
            _cooldown_reason = str(state_cd.get("reason", "allocator_add_on_once_per_day"))
            if sym_cd:
                _record_allocator_drop_reason(
                    _allocator_drop_reasons,
                    candidate,
                    reason=_cooldown_reason,
                    config=config if isinstance(config, Mapping) else {},
                )
            log.info(
                "ALLOCATOR_FILTER_REJECT symbol=%s reason=%s score=%s catalyst_score=%s "
                "event_score=%s news_score=%s age_minutes=%s route=%s",
                sym_cd or "?",
                _cooldown_reason,
                _allocator_diag_field(candidate, "score"),
                _allocator_diag_field(candidate, "catalyst_score"),
                _allocator_diag_field(candidate, "event_score"),
                _allocator_diag_field(candidate, "news_score"),
                _allocator_diag_field(candidate, "age_minutes", "catalyst_age_minutes", default="n/a"),
                _allocator_diag_field(candidate, "route", "source", default="n/a"),
            )
            _log_allocator_reject_reason(
                candidate,
                reason=_cooldown_reason,
                stage="cooldown_filter",
            )
            continue
        _cooldown_filtered_candidates.append(dict(candidate))
    candidates = _cooldown_filtered_candidates
    log.info("ALLOCATOR_STAGE_COUNT post_cooldown_filter_count=%d", len(candidates))
    _ranked_candidates_debug = rank_allocator_candidates(candidates)
    log.info("ALLOCATOR_STAGE_COUNT post_ranking_count=%d", len(_ranked_candidates_debug))
    log.info(
        "ALLOCATOR_RANKED_SYMBOLS count=%d symbols=%s",
        len(_ranked_candidates_debug),
        _allocator_symbol_csv(_ranked_candidates_debug),
    )
    _selected_candidates_debug = list(_ranked_candidates_debug)
    candidates = [dict(row) for row in _ranked_candidates_debug]
    _allocator_skipped_symbols: set[str] = set()
    _psplit = preallocated_equal_split_buys
    if _psplit is not None and len(_psplit) > 0:
        actions = [dict(x) for x in _psplit]
        if _headroom_d is not None:
            actions = clip_buy_actions_to_gross_headroom_dollars(
                actions,
                gross_headroom_dollars=float(_headroom_d),
                min_realloc_leg=float(_min_leg_cfg),
            )
        log.info(
            "[%s] capital_allocator: preallocated %d equal-split buy(s) (post-sell reallocation path)",
            str(user_id),
            len([a for a in actions if str(a.get("action", "")).lower() == "buy"]),
        )
    else:
        if bool(ca_cfg.get("prioritize_diversification", False)):
            _pre_diversification_order = list(candidates)
            _ssec: Mapping[str, str] = (
                symbol_sector if symbol_sector is not None else SYMBOL_SECTOR
            )
            _tmap: Mapping[str, str] = (
                theme_map if theme_map is not None else THEME_MAP
            )
            _default_sec = str(
                parse_sector_config(config).get("default_sector", "unknown")
            ).strip() or "unknown"
            candidates = reorder_allocator_candidates_diversification(
                candidates,
                portfolio,
                float(equity),
                ca_cfg,
                _ssec,
                _tmap,
                _default_sec,
            )
            _selected_candidates_debug = list(candidates)
            _log_allocator_post_rank_reorder(
                original_ranked=_pre_diversification_order,
                action_attempt=candidates,
                reason="prioritize_diversification",
            )

        if allocator_mode == "deploy":
            _dtn_raw = ca_cfg.get("deploy_top_n_signals", 4)
            try:
                _dtn = int(float(_dtn_raw)) if _dtn_raw is not None and str(_dtn_raw).strip() != "" else 4
            except (TypeError, ValueError):
                _dtn = 4
            _dtn = max(3, min(5, _dtn))
            _cand_before = len(candidates)
            candidates, _deploy_group_skips = select_top_candidates_with_group_cap(
                candidates,
                top_n=_dtn,
                max_per_group=_corr_cap,
                correlation_groups=_corr_groups,
                portfolio=portfolio,
                equity=float(equity),
                default_hard_cap_frac=float(_cap_hard),
                symbol_cap_fractions=_tier_caps,
            )
            _selected_candidates_debug = list(candidates)
            _log_allocator_post_rank_reorder(
                original_ranked=_ranked_candidates_debug,
                action_attempt=candidates,
                reason="deploy_selection",
            )
            for _skip in _deploy_group_skips:
                _skip_text = str(_skip)
                if ":" not in _skip_text:
                    continue
                _reason_part, _sym_part = _skip_text.split(":", 1)
                _sym_part = _sym_part.strip().upper()
                if not _sym_part:
                    continue
                _reason_final = (
                    "hard_cap_reached"
                    if _reason_part == "cap"
                    else f"correlation_group_cap:{_reason_part}"
                )
                _skipped_candidate = next(
                    (
                        c
                        for c in _ranked_candidates_debug
                        if str(c.get("symbol") or "").strip().upper() == _sym_part
                    ),
                    {"symbol": _sym_part},
                )
                _record_allocator_drop_reason(
                    _allocator_drop_reasons,
                    _skipped_candidate,
                    reason=_reason_final,
                    config=config if isinstance(config, Mapping) else {},
                )
                _log_allocator_reject_reason(
                    _skipped_candidate,
                    reason=_reason_final,
                    stage="deploy_selection",
                )
            if _cand_before:
                log.info(
                    "[%s] capital_allocator: deploy mode — using top %d of %d candidate(s) by score, then allocate",
                    str(user_id),
                    len(candidates),
                    _cand_before,
                )
                if _deploy_group_skips:
                    log.info(
                        "[%s] capital_allocator: deploy group-cap skips %s",
                        str(user_id),
                        _deploy_group_skips,
                    )

        _ctn0 = int(ca_cfg.get("concentration_top_n", 0) or 0)
        if _ctn0 > 0 and bool(ca_cfg.get("concentration_bias_enabled", False)):
            log.info(
                "[%s] capital_allocator: concentration_bias top_n=%d top_scale=%.2f rest_scale=%.2f (tranche order = final candidate list)",
                str(user_id),
                _ctn0,
                float(ca_cfg.get("concentration_top_tranche_scale", 1.0) or 1.0),
                float(ca_cfg.get("concentration_rest_tranche_scale", 1.0) or 1.0),
            )

        try:
            _ign_soft_min = float(
                ca_cfg.get("ignore_soft_caps_after_sell_minutes", 0) or 0.0
            )
        except (TypeError, ValueError):
            _ign_soft_min = 0.0
        _ign_soft_min = max(0.0, _ign_soft_min)
        _ignore_soft_caps = False
        if _ign_soft_min > 1e-12 and exit_context is not None:
            _rfn = getattr(exit_context, "recent_sell_within", None)
            if callable(_rfn):
                _ignore_soft_caps = bool(_rfn(_ign_soft_min))
        if _ignore_soft_caps:
            log.info(
                "[%s] capital_allocator: ignore soft cap band — equity sell within last %.0f min",
                str(user_id),
                _ign_soft_min,
            )
        if ca_cfg.get("symbol_caps"):
            if _tier_caps:
                log.info(
                    "[%s] capital_allocator: tiered symbol_caps (%d ticker(s)); default soft/hard %.1f%% / %.1f%% of equity for unlisted names",
                    str(user_id),
                    len(_tier_caps),
                    100.0 * _cap_soft,
                    100.0 * _cap_hard,
                )
            elif abs(_cap_soft - _cap_hard) > 1e-9:
                log.info(
                    "[%s] capital_allocator: per-name soft cap %.1f%%, hard cap %.1f%% of equity (regime-aware table)",
                    str(user_id),
                    100.0 * _cap_soft,
                    100.0 * _cap_hard,
                )
            else:
                log.info(
                    "[%s] capital_allocator: effective symbol_cap %.1f%% of equity per name (regime-aware table)",
                    str(user_id),
                    100.0 * _cap_hard,
                )
        candidates = _apply_high_conviction_rotation_relaxation(
            candidates,
            config=config,
            ca_cfg=ca_cfg,
            portfolio=portfolio,
            tracked=tracked,
            equity=float(equity),
            min_realloc_leg=float(_min_leg_cfg),
            allow_allocator_buys=bool(allow_allocator_buys),
            cycle_risk_state=cycle_risk_state,
            exit_context=exit_context,
            engine=engine,
        )
        _selected_candidates_debug = list(candidates)
        _log_allocator_post_rank_reorder(
            original_ranked=_ranked_candidates_debug,
            action_attempt=candidates,
            reason="high_conviction_rotation_relaxation",
        )
        _pre_rank_restore_order = list(candidates)
        candidates = _restore_allocator_rank_order(
            original_ranked=_ranked_candidates_debug,
            selected=candidates,
        )
        _log_allocator_post_rank_reorder(
            original_ranked=_pre_rank_restore_order,
            action_attempt=candidates,
            reason="ranked_order_enforced",
        )
        _selected_candidates_debug = list(candidates)

        def _bool_opt(value: object, default: bool = False) -> bool:
            if value is None:
                return bool(default)
            if isinstance(value, str):
                return value.strip().lower() not in ("0", "false", "no", "off", "")
            return bool(value)

        _du_cfg = config.get("dynamic_universe") if isinstance(config, Mapping) else {}
        _du_cfg = _du_cfg if isinstance(_du_cfg, Mapping) else {}
        _min_deploy_exp = _du_cfg.get("paper_dynamic_min_deploy_experiment")
        _min_deploy_exp = _min_deploy_exp if isinstance(_min_deploy_exp, Mapping) else {}
        _min_deploy_exp_enabled = _bool_opt(_min_deploy_exp.get("enabled", False))
        _min_deploy_exp_use_min_leg = _bool_opt(
            _min_deploy_exp.get("use_min_realloc_leg", True),
            True,
        )
        _broker_cfg = config.get("broker") if isinstance(config, Mapping) else {}
        _broker_cfg = _broker_cfg if isinstance(_broker_cfg, Mapping) else {}
        _broker_mode = "paper" if bool(_broker_cfg.get("paper", False)) else "live"
        allocator = CapitalAllocator(
            max_positions=int(ca_cfg["max_positions"]),
            symbol_cap=float(_cap_hard),
            symbol_cap_soft=float(_cap_soft),
            min_trade_size=float(ca_cfg["min_trade_size"]),
            min_realloc_leg=_min_leg_cfg,
            rotate_trim_fraction=float(ca_cfg["rotate_trim_fraction"]),
            soft_cap_mode=bool(ca_cfg.get("soft_cap_mode", False)),
            cap_penalty_multiplier=float(ca_cfg.get("cap_penalty_multiplier", 0.5)),
            rebalance_fund_from_weakest=bool(
                ca_cfg.get("rebalance_fund_from_weakest", False)
            ),
            rebalance_weakest_trim_fraction=float(
                ca_cfg.get("rebalance_weakest_trim_fraction", 0.30)
            ),
            replace_weakest_with_stronger=bool(
                ca_cfg.get("replace_weakest_with_stronger", True)
            ),
            sell_only_if_needed=bool(ca_cfg.get("sell_only_if_needed", True)),
            replacement_strength_ratio=float(
                ca_cfg.get("replacement_strength_ratio", 1.0)
            ),
            ignore_soft_caps=bool(_ignore_soft_caps),
            concentration_top_n=int(
                ca_cfg.get("concentration_top_n", 0) or 0
            ),
            concentration_top_tranche_scale=float(
                ca_cfg.get("concentration_top_tranche_scale", 1.0) or 1.0
            ),
            concentration_rest_tranche_scale=float(
                ca_cfg.get("concentration_rest_tranche_scale", 1.0) or 1.0
            ),
            symbol_cap_fractions=_tier_caps,
            minimum_cash_to_deploy_frac=float(
                ca_cfg.get("minimum_cash_to_deploy_pct", 0.0) or 0.0
            ),
            broker_mode=_broker_mode,
            paper_dynamic_min_deploy_experiment_enabled=_min_deploy_exp_enabled,
            paper_dynamic_min_deploy_experiment_use_min_realloc_leg=(
                _min_deploy_exp_use_min_leg
            ),
        )
        _alloc_targets = allocation_target_fractions(config)
        try:
            _alloc_dyn_value = dynamic_position_value(portfolio, tracked)
        except Exception:
            _alloc_dyn_value = 0.0
        _alloc_dyn_cap = max(
            0.0,
            float(equity) * float(_alloc_targets.get("dynamic", 0.0) or 0.0),
        )
        _cash_reserve_d = max(0.0, float(cash) - float(deployable_cash))
        if not candidates and _hard_liquidity_rejections:
            actions = []
            _allocator_skipped_symbols = set()
        else:
            actions = allocator.allocate(
                portfolio=portfolio,
                candidates=candidates,
                equity=equity,
                cash=deployable_cash,
                max_total_gross_dollars=_max_g_d,
                current_gross_dollars=_cur_g_d,
                diagnostics={
                    "allocator_mode": allocator_mode,
                    "cash_reserve": _cash_reserve_d,
                    "current_dynamic_sleeve_usage": _alloc_dyn_value,
                    "dynamic_sleeve_cap": _alloc_dyn_cap,
                },
            )
            _allocator_skipped_symbols = {
                str(sym).strip().upper()
                for sym in getattr(allocator, "last_skipped_symbols", set())
                if str(sym).strip()
            }
        if actions:
            _allocator_skip_reason = "actions_created"
        elif not candidates:
            _allocator_skip_reason = "no_candidates_after_selection"
        else:
            _allocator_skip_reason = "allocator_returned_no_actions"
    log.info(
        "ALLOCATOR_SELECTED_SYMBOLS count=%d symbols=%s",
        len(_selected_candidates_debug),
        _allocator_symbol_csv(_selected_candidates_debug),
    )
    _selected_final = Counter(
        str(row.get("symbol") or "").strip().upper()
        for row in _selected_candidates_debug
        if isinstance(row, Mapping) and str(row.get("symbol") or "").strip()
    )
    for _initial_candidate in _allocator_initial_candidates:
        _initial_sym = str(_initial_candidate.get("symbol") or "").strip().upper()
        if _initial_sym and _selected_final[_initial_sym] > 0:
            _selected_final[_initial_sym] -= 1
            continue
        _final_reason = _allocator_drop_reasons.get(_initial_sym)
        _already_logged_reject = bool(_final_reason)
        if not _final_reason:
            _final_reason = "not_selected_after_allocator_selection"
        if not _already_logged_reject:
            _log_allocator_reject_reason(
                _initial_candidate,
                reason=_final_reason,
                stage="final_selection",
            )
        _log_allocator_drop_reason_debug(_initial_candidate, reason=str(_final_reason))
        _record_entry_terminal_outcome(
            store=event_store,
            user_id=str(user_id),
            symbol=_initial_sym,
            route=str(_initial_candidate.get("route") or _initial_candidate.get("source") or "allocator"),
            stage="allocator_filtered",
            reason=str(_final_reason),
            payload=_entry_terminal_payload(
                _initial_candidate,
                profile_rule="final_selection",
            ),
            ts=dt,
        )
        log.info(
            "ALLOCATOR_DROPPED symbol=%s reason=%s",
            _initial_sym or "?",
            _final_reason,
        )
    _candidate_by_symbol = {
        str(c.get("symbol") or "").strip().upper(): c
        for c in candidates
        if str(c.get("symbol") or "").strip()
    }
    _post_planner_actions_seen = bool(actions)
    _last_post_planner_removal_stage = "none"
    _trace_before = [dict(a) for a in actions]
    _removed_stage = _log_post_planner_action_trace(
        stage="planner_output",
        before=[],
        after=_trace_before,
        reason="planner_returned_actions" if _trace_before else _allocator_skip_reason,
    )
    if _removed_stage is not None:
        _last_post_planner_removal_stage = _removed_stage

    def _annotate_allocator_actions(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for src in rows or []:
            row = dict(src)
            sym_a = str(row.get("symbol") or "").strip().upper()
            cand_a = _candidate_by_symbol.get(sym_a, {}) or _signal_by_symbol.get(sym_a, {})
            if str(row.get("action") or "").strip().lower() == "buy" and cand_a:
                for key in ("source", "route", "reason", "core_rebuild", "dynamic_candidate", "signal_score"):
                    if key in cand_a and key not in row:
                        row[key] = cand_a[key]
                if allocation_profile_is_dynamic_candidate(cand_a):
                    _allocator_copy_dynamic_dispatch_metadata(row, cand_a)
            out.append(row)
        return out

    _trace_before = [dict(a) for a in actions]
    actions = _annotate_allocator_actions(actions)
    _post_planner_actions_seen = _post_planner_actions_seen or bool(actions)
    _removed_stage = _log_post_planner_action_trace(
        stage="annotate_initial",
        before=_trace_before,
        after=actions,
        reason="copy_candidate_metadata",
    )
    if _removed_stage is not None:
        _last_post_planner_removal_stage = _removed_stage
    _action_by_symbol = {
        str(a.get("symbol") or "").strip().upper(): a
        for a in actions
        if str(a.get("symbol") or "").strip()
    }
    for _cand_decision in _selected_candidates_debug:
        if not isinstance(_cand_decision, Mapping):
            continue
        _sym_decision = str(_cand_decision.get("symbol") or "").strip().upper()
        if not _sym_decision:
            continue
        _act_decision = _action_by_symbol.get(_sym_decision)
        if _act_decision is not None:
            log.info(
                "ALLOCATOR_DECISION symbol=%s action=%s notional=%.2f reason=%s",
                _sym_decision,
                str(_act_decision.get("action") or "").strip().lower() or "?",
                float(_act_decision.get("notional", 0.0) or 0.0),
                "action_created",
            )
        else:
            _decision_reason = (
                f"actions_removed_by_post_planner_filter last_removal_stage={_last_post_planner_removal_stage}"
                if _post_planner_actions_seen or _last_post_planner_removal_stage != "none"
                else str(_allocator_skip_reason or "allocator_returned_no_actions")
            )
            log.info(
                "ALLOCATOR_DECISION symbol=%s action=none notional=0.00 reason=%s",
                _sym_decision,
                _decision_reason,
            )
            log.info(
                "ALLOCATOR_SKIP symbol=%s reason=%s",
                _sym_decision,
                _decision_reason,
            )
    _selected_rank_by_symbol = {
        str(c.get("symbol") or "").strip().upper(): idx
        for idx, c in enumerate(_selected_candidates_debug, start=1)
        if isinstance(c, Mapping) and str(c.get("symbol") or "").strip()
    }
    for _cand_attr in _selected_candidates_debug:
        if not isinstance(_cand_attr, Mapping):
            continue
        _sym_attr = str(_cand_attr.get("symbol") or "").strip().upper()
        if not _sym_attr:
            continue
        _act_attr = _action_by_symbol.get(_sym_attr)
        record_trade_attribution_allocator_candidate(
            data_dir=data_dir,
            user_id=str(user_id),
            timestamp=dt,
            candidate=_cand_attr,
            selected_rank=_selected_rank_by_symbol.get(_sym_attr),
            action_created=_act_attr is not None,
            no_action_reason=None if _act_attr is not None else _allocator_skip_reason,
            target_notional=_cand_attr.get("target_notional") or _cand_attr.get("candidate_notional_requested"),
            final_notional=(_act_attr or {}).get("notional"),
        )
        if _act_attr is None:
            _record_entry_terminal_outcome(
                store=event_store,
                user_id=str(user_id),
                symbol=_sym_attr,
                route=str(_cand_attr.get("route") or _cand_attr.get("source") or "allocator"),
                stage="allocator_no_action",
                reason=str(_allocator_skip_reason or "allocator_returned_no_actions"),
                payload=_entry_terminal_payload(
                    _cand_attr,
                    minimum_cash_to_deploy=ca_cfg.get("minimum_cash_to_deploy"),
                    minimum_cash_to_deploy_pct=ca_cfg.get("minimum_cash_to_deploy_pct"),
                    min_order_notional=ca_cfg.get("min_order_notional"),
                    min_realloc_leg=ca_cfg.get("min_realloc_leg"),
                    available_cash=deployable_cash,
                    cash=cash,
                    gross_headroom=_headroom_d,
                    allow_allocator_buys=allow_allocator_buys,
                    no_recycle_block=no_recycle_block,
                ),
                ts=dt,
            )
    try:
        _selected_capture_symbols = [
            str((row or {}).get("symbol") or (row or {}).get("sym_u") or "").strip().upper()
            for row in _selected_candidates_debug
            if isinstance(row, Mapping)
        ]
        _selected_capture_diag = capture_runtime_forward_bars(
            broker=broker,
            data_dir=data_dir,
            user_id=str(user_id),
            timestamp=dt,
            symbols=_selected_capture_symbols,
            config=config,
        )
        if isinstance(_selected_capture_diag, Mapping) and not _selected_capture_diag.get("skipped"):
            log.info(
                "FORWARD_BAR_CAPTURE_ALLOCATOR_SELECTED user_id=%s symbols=%d summary=%s reason=%s",
                str(user_id),
                len(_selected_capture_symbols),
                _selected_capture_diag.get("summary"),
                _selected_capture_diag.get("reason"),
            )
    except Exception:
        log.warning("FORWARD_BAR_CAPTURE_ALLOCATOR_SELECTED_FAILED user_id=%s", str(user_id), exc_info=True)
    for _created in actions:
        log.info(
            "ALLOCATOR_ACTION_CREATED symbol=%s action=%s notional=%.2f route=%s",
            str(_created.get("symbol") or "").strip().upper() or "?",
            str(_created.get("action") or "").strip().lower() or "?",
            float(_created.get("notional", 0.0) or 0.0),
            str(_created.get("route") or _created.get("source") or "n/a"),
        )
        _created_sym = str(_created.get("symbol") or "").strip().upper()
        _record_entry_terminal_outcome(
            store=event_store,
            user_id=str(user_id),
            symbol=_created_sym,
            route=str(_created.get("route") or _created.get("source") or "allocator"),
            stage="allocator_action_created",
            reason="action_created",
            payload=_entry_terminal_payload(
                _candidate_by_symbol.get(_created_sym, {}),
                action=str(_created.get("action") or ""),
                notional=_created.get("notional"),
            ),
            ts=dt,
        )

    if not actions:
        print("ALLOCATOR ACTIONS:", actions)
    if not allow_effective and actions:
        _buys = [a for a in actions if str(a.get("action", "")).lower() == "buy"]
        if _buys:
            for _a in _buys:
                _sym_drop = str(_a.get("symbol", "")).upper()
                _print_allocator_skip(_sym_drop, "cap reached")
                log.info(
                    "ALLOCATOR_ACTION_BLOCKED symbol=%s reason=allocator_buy_lockout action=buy notional=%.2f",
                    _sym_drop,
                    float(_a.get("notional", 0.0) or 0.0),
                )
            if not allow_allocator_buys:
                log.warning(
                    "[%s] capital_allocator: gross over max eff — dropping %d buy(s), keeping sells",
                    str(user_id),
                    len(_buys),
                )
            if rc_block:
                log.warning(
                    "[%s] capital_allocator: risk_control (gross over %.0f%% of equity) — dropping %d buy(s), keeping sells",
                    str(user_id),
                    r_thr * 100.0,
                    len(_buys),
                )
            if no_recycle_block:
                _nrf = risk_no_recycle_above_frac(config)
                _pct = float(_nrf) * 100.0 if _nrf is not None else 0.0
                log.warning(
                    "[%s] capital_allocator: no_recycle band (gross > %.0f%% of equity) — dropping %d buy(s), keeping sells",
                    str(user_id),
                    _pct,
                    len(_buys),
                )
        _trace_before = [dict(a) for a in actions]
        actions = [a for a in actions if str(a.get("action", "")).lower() != "buy"]
        _removed_stage = _log_post_planner_action_trace(
            stage="no_recycle_drop_buys",
            before=_trace_before,
            after=actions,
            reason="allow_effective_false",
        )
        if _removed_stage is not None:
            _last_post_planner_removal_stage = _removed_stage
    else:
        _log_post_planner_action_trace(
            stage="no_recycle_drop_buys",
            before=actions,
            after=actions,
            reason="not_applicable",
        )

    _pre_cooldown_actions = [dict(a) for a in actions]
    actions = apply_cooldown(actions, portfolio, exit_context=exit_context)
    _removed_stage = _log_post_planner_action_trace(
        stage="cooldown",
        before=_pre_cooldown_actions,
        after=actions,
        reason="apply_cooldown",
    )
    if _removed_stage is not None:
        _last_post_planner_removal_stage = _removed_stage
    if len(actions) < len(_pre_cooldown_actions):
        _kept = {
            (
                str(a.get("action", "")).lower(),
                str(a.get("symbol", "")).upper(),
                float(a.get("notional", 0) or 0),
            )
            for a in actions
        }
        for _a in _pre_cooldown_actions:
            _key = (
                str(_a.get("action", "")).lower(),
                str(_a.get("symbol", "")).upper(),
                float(_a.get("notional", 0) or 0),
            )
            if _key[0] == "buy" and _key not in _kept:
                _print_allocator_skip(_key[1], "cooldown")
                log.info(
                    "ALLOCATOR_ACTION_BLOCKED symbol=%s reason=cooldown action=buy notional=%.2f",
                    _key[1],
                    float(_key[2]),
                )

    def _allocator_nsum(ac: list[Mapping[str, Any]], side: str) -> float:
        t = 0.0
        for x in ac:
            if str(x.get("action", "")).lower() != side:
                continue
            try:
                t += max(0.0, float(x.get("notional", 0) or 0))
            except (TypeError, ValueError):
                pass
        return t

    if (
        bool(ca_cfg.get("require_net_sell_gte_buy", True))
        and actions
        and not force_allocate
    ):
        _b0, _s0 = _allocator_nsum(actions, "buy"), _allocator_nsum(
            actions, "sell"
        )
        _trace_before = [dict(a) for a in actions]
        actions = trim_allocator_actions_for_net_sell_gte_buy(
            actions, min_realloc_leg=float(_min_leg_cfg)
        )
        _removed_stage = _log_post_planner_action_trace(
            stage="net_sell_gte_buy",
            before=_trace_before,
            after=actions,
            reason="require_net_sell_gte_buy",
        )
        if _removed_stage is not None:
            _last_post_planner_removal_stage = _removed_stage
        _b1 = _allocator_nsum(actions, "buy")
        if _b1 < _b0 - 1e-6:
            log.info(
                "[%s] capital_allocator: net_sell_gte_buy trim (buys $%.0f→$%.0f, sells $%.0f)",
                str(user_id),
                _b0,
                _b1,
                _s0,
            )
    else:
        _log_post_planner_action_trace(
            stage="net_sell_gte_buy",
            before=actions,
            after=actions,
            reason="not_applicable",
        )
    _nrr_raw = ca_cfg.get("net_reduction_max_buy_to_sell_ratio", 0.5)
    try:
        _nrr = (
            float(_nrr_raw)
            if _nrr_raw is not None and str(_nrr_raw).strip() != ""
            else 0.5
        )
    except (TypeError, ValueError):
        _nrr = 0.5
    _nrr = max(0.0, min(1.0, _nrr))
    _nrel_raw = ca_cfg.get("net_reduction_near_cap_relative_to_max", 0.9)
    try:
        _nrel = (
            float(_nrel_raw)
            if _nrel_raw is not None and str(_nrel_raw).strip() != ""
            else 0.9
        )
    except (TypeError, ValueError):
        _nrel = 0.9
    _nrel = max(0.0, min(1.0, _nrel))
    if (
        1e-9 < _nrr < 1.0 - 1e-9
        and _nrel > 1e-12
        and actions
        and gross_exposure_pct is not None
        and gross_book_near_effective_max_for_net_reduction(
            float(gross_exposure_pct),
            config,
            relative_to_max_frac=_nrel,
            regime_score=regime_score,
            regime_condition=regime_condition,
            entry_wave_strong_signal_count=entry_wave_strong_signal_count,
        )
    ):
        _b2, _s2 = _allocator_nsum(actions, "buy"), _allocator_nsum(
            actions, "sell"
        )
        _trace_before = [dict(a) for a in actions]
        actions = trim_allocator_actions_for_max_buy_to_sell_ratio(
            actions,
            min_realloc_leg=float(_min_leg_cfg),
            max_buy_to_sell_ratio=_nrr,
        )
        _removed_stage = _log_post_planner_action_trace(
            stage="max_buy_to_sell_ratio",
            before=_trace_before,
            after=actions,
            reason="near_cap_net_reduction",
        )
        if _removed_stage is not None:
            _last_post_planner_removal_stage = _removed_stage
        _b3 = _allocator_nsum(actions, "buy")
        if _b3 < _b2 - 1e-6:
            log.info(
                "[%s] capital_allocator: near_cap net reduction trim (buys $%.0f→$%.0f, "
                "sells $%.0f, max_buy/sell=%.2f, near_cap>=%.0f%% of eff max)",
                str(user_id),
                _b2,
                _b3,
                _s2,
                _nrr,
                _nrel * 100.0,
            )
    else:
        _log_post_planner_action_trace(
            stage="max_buy_to_sell_ratio",
            before=actions,
            after=actions,
            reason="not_applicable",
        )
    if (
        not actions
        and bool(ca_cfg.get("fallback_on_empty_alloc", True))
        and allow_effective
        and candidates
    ):
        _eatn_raw = ca_cfg.get("empty_alloc_top_n", 5)
        try:
            _eatn = (
                int(float(_eatn_raw))
                if _eatn_raw is not None and str(_eatn_raw).strip() != ""
                else 5
            )
        except (TypeError, ValueError):
            _eatn = 5
        _eatn = max(1, min(20, _eatn))
        _fb_source_candidates = [
            c
            for c in candidates
            if str(c.get("symbol", "")).strip().upper()
            not in _allocator_skipped_symbols
        ]
        if len(_fb_source_candidates) < len(candidates):
            log.info(
                "[%s] capital_allocator: equal-split fallback excluding allocator-skipped symbols %s",
                str(user_id),
                sorted(_allocator_skipped_symbols),
            )
        _fb_candidates_eq, _fb_group_skips_eq = select_top_candidates_with_group_cap(
            _fb_source_candidates,
            top_n=_eatn,
            max_per_group=_corr_cap
            if bool(ca_cfg.get("fallback_enforce_diversity", False))
            else 0,
            correlation_groups=_corr_groups
            if bool(ca_cfg.get("fallback_enforce_diversity", False))
            else None,
            portfolio=portfolio,
            equity=float(equity),
            default_hard_cap_frac=float(_cap_hard),
            symbol_cap_fractions=_tier_caps,
        )
        _selected_candidates_debug = list(_fb_candidates_eq)
        _fb_block_reason = _etf_fallback_block_reason(_fb_candidates_eq, ca_cfg)
        if _fb_block_reason is not None:
            log.info("ETF_FALLBACK_BLOCKED reason=%s", _fb_block_reason)
            for _etf_row in _fb_candidates_eq:
                _etf_sym = str(_etf_row.get("symbol") or "").strip().upper()
                if _etf_sym:
                    log.info("ALLOCATOR_SKIP_ETF symbol=%s reason=etf_excluded", _etf_sym)
            _fb_candidates_eq = []
        _fb_cash = float(deployable_cash)
        if _fb_candidates_eq and _allocator_candidates_all_etfs(_fb_candidates_eq):
            _fb_cash = _etf_fallback_cash_limit(float(deployable_cash), float(equity), ca_cfg)
        _trace_before = [dict(a) for a in actions]
        _fb = empty_alloc_equal_split_buys(
            candidates=_fb_candidates_eq,
            cash=float(_fb_cash),
            min_realloc_leg=float(_min_leg_cfg),
            top_n=_eatn,
        )
        if _fb:
            _syms = ",".join(str(x.get("symbol", "")) for x in _fb)
            _tot = sum(float(x.get("notional", 0) or 0) for x in _fb)
            log.warning(
                "[%s] capital_allocator: allocator plan empty — equal-split BUY %d name(s) $%.0f total (%s)",
                str(user_id),
                len(_fb),
                _tot,
                _syms,
            )
            if _fb_group_skips_eq:
                log.info(
                    "[%s] capital_allocator: equal-split fallback group-cap skips %s",
                    str(user_id),
                    _fb_group_skips_eq,
                )
            actions = apply_cooldown(_fb, portfolio, exit_context=exit_context)
            _post_planner_actions_seen = _post_planner_actions_seen or bool(actions) or bool(_fb)
            _removed_stage = _log_post_planner_action_trace(
                stage="idle_no_trade_cycle_branch",
                before=_trace_before,
                after=actions,
                reason="equal_split_fallback",
            )
            if _removed_stage is not None:
                _last_post_planner_removal_stage = _removed_stage
            if not actions:
                for _a in _fb:
                    if str(_a.get("action", "")).lower() == "buy":
                        _print_allocator_skip(str(_a.get("symbol", "")).upper(), "cooldown")
                log.info(
                    "[%s] capital_allocator: equal-split buys removed by apply_cooldown (exit/cooldown)",
                    str(user_id),
                )
            else:
                _allocator_skip_reason = "equal_split_fallback"
    _allow_no_trade_cycles = _cfg_bool(ca_cfg.get("allow_no_trade_cycles"), default=False)
    if (
        not _allow_no_trade_cycles
        and _cfg_bool(ca_cfg.get("force_minimum_trade_single_candidate", True), default=True)
        and not actions
        and len(candidates) == 1
        and allow_effective
        and candidates
    ):
        _fc_block_reason = _etf_fallback_block_reason(candidates, ca_cfg)
        if _fc_block_reason is not None:
            log.info("ETF_FALLBACK_BLOCKED reason=%s", _fc_block_reason)
            for _etf_row in candidates:
                _etf_sym = str(_etf_row.get("symbol") or "").strip().upper()
                if _etf_sym:
                    log.info("ALLOCATOR_SKIP_ETF symbol=%s reason=etf_excluded", _etf_sym)
        else:
            _fc_sym = str(candidates[0].get("symbol", "")).strip().upper()
            if _fc_sym:
                try:
                    _fc_cash = max(0.0, float(deployable_cash))
                except (TypeError, ValueError):
                    _fc_cash = 0.0
                if _allocator_symbol_is_etf(_fc_sym):
                    _fc_cash = _etf_fallback_cash_limit(_fc_cash, float(equity), ca_cfg)
                if _fc_cash >= float(_min_leg_cfg) - 1e-9:
                    _row_fc = next(
                        (
                            r
                            for r in signals
                            if str(r.get("sym_u", "")).upper() == _fc_sym
                        ),
                        None,
                    )
                    _df_fc = _row_fc.get("df") if _row_fc else None
                    _q_fc = broker.get_latest_quote(_fc_sym)
                    if _q_fc:
                        _px_fb = 0.0
                        try:
                            if _df_fc is not None and not getattr(_df_fc, "empty", True):
                                _px_fb = float(_df_fc["close"].iloc[-1])
                        except Exception:
                            pass
                        _mid_fc = _q_fc.reference_mid(
                            _px_fb if _px_fb > 0 else float(getattr(_q_fc, "mid", 0) or 0)
                        )
                        try:
                            _mid_fc = float(_mid_fc)
                        except (TypeError, ValueError):
                            _mid_fc = 0.0
                        if _mid_fc > 0:
                            _nom_fc = max(_mid_fc, float(_min_leg_cfg))
                            _nom_fc = min(_nom_fc, _fc_cash)
                            _nom_fc = clean_notional(
                                _nom_fc, min_notional=float(_min_leg_cfg)
                            )
                            if _nom_fc > 0 and _nom_fc <= _fc_cash + 1e-6:
                                log.warning(
                                    "[%s] capital_allocator: forcing minimum BUY "
                                    "(single candidate, allocator empty) %s $%.2f",
                                    str(user_id),
                                    _fc_sym,
                                    _nom_fc,
                                )
                                print("FORCING MINIMUM TRADE (single candidate fix)")
                                _forced = [
                                    {
                                        "action": "buy",
                                        "symbol": _fc_sym,
                                        "notional": _nom_fc,
                                        "qty": 1,
                                    }
                                ]
                                _trace_before = [dict(a) for a in actions]
                                actions = apply_cooldown(
                                    _forced, portfolio, exit_context=exit_context
                                )
                                _post_planner_actions_seen = _post_planner_actions_seen or bool(actions) or bool(_forced)
                                _removed_stage = _log_post_planner_action_trace(
                                    stage="idle_no_trade_cycle_branch",
                                    before=_trace_before,
                                    after=actions,
                                    reason="single_candidate_minimum_trade",
                                )
                                if _removed_stage is not None:
                                    _last_post_planner_removal_stage = _removed_stage
                                if actions:
                                    _allocator_skip_reason = (
                                        "single_candidate_minimum_trade"
                                    )
                                else:
                                    _print_allocator_skip(_fc_sym, "cooldown")
                                    log.info(
                                        "[%s] capital_allocator: single-candidate "
                                        "minimum trade removed by apply_cooldown",
                                        str(user_id),
                                    )
    _selected_must_execute = _cfg_bool(
        ca_cfg.get("selected_must_execute"),
        default=False,
    )
    if (
        _selected_must_execute
        and not _allow_no_trade_cycles
        and not actions
        and allow_effective
        and _selected_candidates_debug
    ):
        _force_candidates = [
            str(c.get("symbol", "")).strip().upper()
            for c in _selected_candidates_debug
            if str(c.get("symbol", "")).strip()
        ]
        _force_candidates = list(dict.fromkeys(_force_candidates))
        for _fc_sym in _force_candidates:
            try:
                _fc_cash = max(0.0, float(deployable_cash))
            except (TypeError, ValueError):
                _fc_cash = 0.0
            _force_headroom: float | None = None
            _force_max_gross = parse_equity_fraction_optional(
                ca_cfg.get("idle_fallback_max_gross_pct")
            )
            if (
                _force_max_gross is not None
                and gross_exposure_pct is not None
                and math.isfinite(float(equity))
                and float(equity) > 0
            ):
                try:
                    _force_gfrac = max(0.0, float(gross_exposure_pct) / 100.0)
                except (TypeError, ValueError, OverflowError):
                    _force_gfrac = float("nan")
                if math.isfinite(_force_gfrac):
                    if _force_gfrac + 1e-12 >= float(_force_max_gross):
                        _print_allocator_skip(
                            _fc_sym,
                            "cap reached",
                            detail="gross %.1f%% >= selected fallback cap %.1f%%"
                            % (_force_gfrac * 100.0, float(_force_max_gross) * 100.0),
                        )
                        logger.info(
                            "[ALLOCATOR_SKIP] %s reason=gross cap for selected_must_execute",
                            _fc_sym,
                        )
                        break
                    _force_headroom = max(
                        0.0,
                        (float(_force_max_gross) - _force_gfrac) * float(equity),
                    )
            if _fc_cash < float(_min_leg_cfg) - 1e-9:
                _print_allocator_skip(
                    _fc_sym,
                    "size = 0",
                    detail="cash $%.2f < min_realloc_leg %.0f" % (_fc_cash, _min_leg_cfg),
                )
                break
            _row_fc = next(
                (
                    r
                    for r in signals
                    if str(r.get("sym_u", "")).upper() == _fc_sym
                ),
                None,
            )
            _df_fc = _row_fc.get("df") if _row_fc else None
            _q_fc = broker.get_latest_quote(_fc_sym)
            if not _q_fc:
                _print_allocator_skip(_fc_sym, "size = 0", detail="no quote")
                logger.info("[ALLOCATOR_SKIP] %s reason=no quote", _fc_sym)
                continue
            _px_fb = 0.0
            try:
                if _df_fc is not None and not getattr(_df_fc, "empty", True):
                    _px_fb = float(_df_fc["close"].iloc[-1])
            except Exception:
                pass
            _mid_fc = _q_fc.reference_mid(
                _px_fb if _px_fb > 0 else float(getattr(_q_fc, "mid", 0) or 0)
            )
            try:
                _mid_fc = float(_mid_fc)
            except (TypeError, ValueError):
                _mid_fc = 0.0
            if _mid_fc <= 0:
                _print_allocator_skip(_fc_sym, "size = 0", detail="invalid quote mid")
                logger.info("[ALLOCATOR_SKIP] %s reason=invalid quote mid", _fc_sym)
                continue
            _nom_fc = max(_mid_fc, float(_min_leg_cfg))
            _nom_fc = min(_nom_fc, _fc_cash)
            if _force_headroom is not None:
                _nom_fc = min(_nom_fc, _force_headroom)
            _nom_fc = clean_notional(_nom_fc, min_notional=float(_min_leg_cfg))
            if _nom_fc <= 0 or _nom_fc > _fc_cash + 1e-6:
                _print_allocator_skip(_fc_sym, "size = 0")
                logger.info("[ALLOCATOR_SKIP] %s reason=notional <= 0 or exceeds cash", _fc_sym)
                continue
            log.warning(
                "[%s] capital_allocator: selected-must-execute fallback — forcing minimum BUY %s $%.2f",
                str(user_id),
                _fc_sym,
                _nom_fc,
            )
            print(
                "FORCING MINIMUM TRADE (selected must execute)",
                flush=True,
            )
            _forced = [
                {
                    "action": "buy",
                    "symbol": _fc_sym,
                    "notional": _nom_fc,
                    "qty": 1,
                }
            ]
            _trace_before = [dict(a) for a in actions]
            actions = apply_cooldown(_forced, portfolio, exit_context=exit_context)
            _post_planner_actions_seen = _post_planner_actions_seen or bool(actions) or bool(_forced)
            _removed_stage = _log_post_planner_action_trace(
                stage="idle_no_trade_cycle_branch",
                before=_trace_before,
                after=actions,
                reason="selected_must_execute_force_buy",
            )
            if _removed_stage is not None:
                _last_post_planner_removal_stage = _removed_stage
            if actions:
                _allocator_skip_reason = "selected_must_execute_force_buy"
                break
            _print_allocator_skip(_fc_sym, "cooldown")
    _trace_before = [dict(a) for a in actions]
    actions = clip_actions_for_allocation_profile(
        actions,
        candidates=candidates,
        portfolio=portfolio,
        tracked=tracked,
        equity=float(equity),
        config=config,
        min_realloc_leg=float(_min_leg_cfg),
    )
    _removed_stage = _log_post_planner_action_trace(
        stage="allocation_profile_clip",
        before=_trace_before,
        after=actions,
        reason="clip_actions_for_allocation_profile",
    )
    if _removed_stage is not None:
        _last_post_planner_removal_stage = _removed_stage
    _msop = ca_cfg.get("max_single_order_notional_pct")
    _mson = ca_cfg.get("max_single_order_notional")
    _trace_before = [dict(a) for a in actions]
    if (_msop is not None and str(_msop).strip() != "") or (
        _mson is not None and str(_mson).strip() != ""
    ):
        if actions:
            actions = clip_allocator_buy_notionals_to_single_order_caps(
                actions,
                account_equity=float(account_equity or 0.0),
                max_single_order_notional_pct=float(_msop)
                if _msop is not None and str(_msop).strip() != ""
                else None,
                max_single_order_notional=float(_mson)
                if _mson is not None and str(_mson).strip() != ""
                else None,
            )
    _removed_stage = _log_post_planner_action_trace(
        stage="single_order_caps",
        before=_trace_before,
        after=actions,
        reason="clip_allocator_buy_notionals_to_single_order_caps"
        if ((_msop is not None and str(_msop).strip() != "") or (_mson is not None and str(_mson).strip() != ""))
        else "not_applicable",
    )
    if _removed_stage is not None:
        _last_post_planner_removal_stage = _removed_stage
    _cn_raw = ca_cfg.get("consolidate_net_before_submit", True)
    if isinstance(_cn_raw, str):
        _consolidate_net = str(_cn_raw).strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
            "",
        )
    else:
        _consolidate_net = bool(_cn_raw) if _cn_raw is not None else True
    _trace_before = [dict(a) for a in actions]
    if _consolidate_net and actions:
        _mn_raw = ca_cfg.get("min_net_notional")
        if _mn_raw is None or str(_mn_raw).strip() == "":
            try:
                _min_net = float(ca_cfg.get("min_realloc_leg", 500) or 500)
            except (TypeError, ValueError):
                _min_net = 500.0
        else:
            try:
                _min_net = float(_mn_raw)
            except (TypeError, ValueError):
                _min_net = 500.0
        _min_net = max(0.0, _min_net)
        _n_before = len(actions)
        actions = consolidate_allocator_actions_net_by_symbol(
            actions,
            min_abs_net_notional=_min_net,
        )
        actions = _annotate_allocator_actions(actions)
        _removed_stage = _log_post_planner_action_trace(
            stage="consolidate_net",
            before=_trace_before,
            after=actions,
            reason="consolidate_allocator_actions_net_by_symbol",
        )
        if _removed_stage is not None:
            _last_post_planner_removal_stage = _removed_stage
        if len(actions) != _n_before:
            log.info(
                "[%s] capital_allocator: net-by-symbol consolidation %d → %d row(s) (min_net $%.0f)",
                str(user_id),
                _n_before,
                len(actions),
                _min_net,
            )
    else:
        _log_post_planner_action_trace(
            stage="consolidate_net",
            before=_trace_before,
            after=actions,
            reason="not_applicable",
        )
    _idle_trigger_raw = ca_cfg.get("if_no_actions_cycles", 0)
    try:
        _idle_trigger = (
            int(float(_idle_trigger_raw))
            if _idle_trigger_raw is not None and str(_idle_trigger_raw).strip() != ""
            else 0
        )
    except (TypeError, ValueError):
        _idle_trigger = 0
    _idle_trigger = max(0, _idle_trigger)
    _idle_key = str(user_id)
    _empty_candidate_pass = not actions and allow_effective and bool(candidates)
    _idle_count = _ALLOCATOR_EMPTY_ACTION_CYCLES.get(_idle_key, 0)
    if _empty_candidate_pass:
        _idle_count += 1
        _ALLOCATOR_EMPTY_ACTION_CYCLES[_idle_key] = _idle_count
    else:
        _ALLOCATOR_EMPTY_ACTION_CYCLES[_idle_key] = 0
    if _empty_candidate_pass and _allow_no_trade_cycles:
        print(
            "[live_bot] capital_allocator: no trade cycle allowed — nothing worth buying right now",
            flush=True,
        )
        _allocator_skip_reason = (
            "actions_removed_by_post_planner_filter"
            if _post_planner_actions_seen
            else "no_trade_cycle_allowed"
        )
        _log_post_planner_action_trace(
            stage="idle_no_trade_cycle_branch",
            before=[],
            after=[],
            reason=(
                f"actions_removed_by_post_planner_filter last_removal_stage={_last_post_planner_removal_stage}"
                if _post_planner_actions_seen
                else "no_trade_cycle_allowed"
            ),
        )
        _dyn_targets = allocation_target_fractions(config)
        try:
            _dyn_value = dynamic_position_value(portfolio, tracked)
        except Exception:
            _dyn_value = 0.0
        try:
            _dyn_count = dynamic_position_count(portfolio, tracked)
        except Exception:
            _dyn_count = 0
        _dyn_cap_value = max(0.0, float(equity) * float(_dyn_targets.get("dynamic", 0.0) or 0.0))
        _dyn_slots_remaining = max(0, 6 - int(_dyn_count))
        _dyn_lockout_reason = dynamic_lockout_reason(engine, float(equity)) if engine is not None else None
        _daily_loss_active, _daily_loss_source = _allocator_daily_loss_lockout_state(
            allow_allocator_buys=bool(allow_allocator_buys),
            cycle_risk_state=cycle_risk_state,
        )
        _lockout_state_diag = (
            f"allow_effective={bool(allow_effective)};"
            f"allow_buys={bool(allow_allocator_buys)};"
            f"no_recycle={bool(no_recycle_block)};"
            f"risk_control={bool(rc_block)};"
            f"daily_loss={bool(_daily_loss_active)};"
            f"dynamic_lockout={_dyn_lockout_reason or 'none'}"
        )
        for _cand_diag in _selected_candidates_debug:
            _sym_diag = str(_cand_diag.get("symbol") or "").strip().upper()
            if not _sym_diag:
                continue
            _cool_active, _cool_reason, _next_eligible = _allocator_bulk_cooldown_state(
                exit_context,
                _sym_diag,
            )
            _gate_skip_reason = (
                f"actions_removed_by_post_planner_filter last_removal_stage={_last_post_planner_removal_stage}"
                if _allocator_skip_reason == "actions_removed_by_post_planner_filter"
                else _allocator_skip_reason
            )
            log.info(
                "TRADE_CYCLE_GATE symbol=%s replay_mode=%s broker_mock=%s market_open=%s "
                "trade_cycle_allowed=%s allow_buys=%s cooldown_active=%s lockout_state=%s "
                "skip_reason=%s",
                _sym_diag,
                replay_mode_diag,
                bool(broker_mock_diag),
                market_open_diag,
                bool(_allow_no_trade_cycles),
                bool(allow_effective and allow_allocator_buys),
                bool(_cool_active),
                _lockout_state_diag,
                _gate_skip_reason,
            )
            if replay_mode_diag != "live":
                log.info(
                    "ENTRY_PIPELINE_STAGE symbol=%s stage=post_planner result=skipped reason=%s",
                    _sym_diag,
                    _gate_skip_reason,
                )
                log.info(
                    "OPTION_PIPELINE_STAGE symbol=%s stage=post_planner result=skipped "
                    "reason=entry_pipeline_not_reached:%s",
                    _sym_diag,
                    _gate_skip_reason,
                )
            log.info(
                "ALLOCATOR_SKIP_REASON symbol=%s reason=%s last_removal_stage=%s trade_cycle_allowed=%s cooldown_active=%s cooldown_reason=%s next_eligible_entry_time=%s allocator_lockout_allow_effective=%s allocator_lockout_allow_buys=%s allocator_lockout_no_recycle=%s allocator_lockout_risk_control=%s daily_loss_lockout_active=%s daily_loss_lockout_source=%s dynamic_lockout_reason=%s dynamic_position_count=%d dynamic_position_slots_remaining=%d dynamic_position_value=%.2f dynamic_position_cap=%.2f",
                _sym_diag,
                _allocator_skip_reason,
                _last_post_planner_removal_stage,
                bool(_allow_no_trade_cycles),
                bool(_cool_active),
                _cool_reason,
                _next_eligible,
                bool(allow_effective),
                bool(allow_allocator_buys),
                bool(no_recycle_block),
                bool(rc_block),
                bool(_daily_loss_active),
                _daily_loss_source,
                _dyn_lockout_reason or "none",
                int(_dyn_count),
                int(_dyn_slots_remaining),
                float(_dyn_value),
                float(_dyn_cap_value),
            )
    if (
        _empty_candidate_pass
        and not _allow_no_trade_cycles
        and _idle_trigger > 0
        and _idle_count >= _idle_trigger
    ):
        _fb_topn_raw = ca_cfg.get("fallback_pick_top_n", 0)
        _fb_size_pct = ca_cfg.get("fallback_size_pct")
        _idle_max_gross = parse_equity_fraction_optional(
            ca_cfg.get("idle_fallback_max_gross_pct")
        )
        try:
            _fb_topn = (
                int(float(_fb_topn_raw))
                if _fb_topn_raw is not None and str(_fb_topn_raw).strip() != ""
                else 0
            )
        except (TypeError, ValueError):
            _fb_topn = 0
        try:
            _fb_size = (
                float(_fb_size_pct)
                if _fb_size_pct is not None and str(_fb_size_pct).strip() != ""
                else 0.0
            )
        except (TypeError, ValueError):
            _fb_size = 0.0
        _fb_topn = max(0, min(20, _fb_topn))
        _fb_size = max(0.0, min(1.0, _fb_size))
        _idle_cap_blocks = False
        _idle_cap_headroom: float | None = None
        if (
            _idle_max_gross is not None
            and gross_exposure_pct is not None
            and math.isfinite(float(equity))
            and float(equity) > 0
        ):
            try:
                _idle_gfrac = max(0.0, float(gross_exposure_pct) / 100.0)
            except (TypeError, ValueError, OverflowError):
                _idle_gfrac = float("nan")
            if math.isfinite(_idle_gfrac):
                if _idle_gfrac + 1e-12 >= float(_idle_max_gross):
                    print(
                        "[live_bot] capital_allocator: idle fallback skipped — gross exposure too high",
                        flush=True,
                    )
                    log.info(
                        "[%s] capital_allocator: idle fallback skipped — gross %.1f%% >= cap %.1f%%",
                        str(user_id),
                        _idle_gfrac * 100.0,
                        float(_idle_max_gross) * 100.0,
                    )
                    _allocator_skip_reason = "idle_fallback_gross_cap"
                    _idle_cap_blocks = True
                else:
                    _idle_cap_headroom = max(
                        0.0,
                        (float(_idle_max_gross) - _idle_gfrac) * float(equity),
                    )
        if _fb_topn > 0 and _fb_size > 1e-12 and not _idle_cap_blocks:
            _fb_candidates, _fb_group_skips = select_top_candidates_with_group_cap(
                candidates,
                top_n=_fb_topn,
                max_per_group=_corr_cap
                if bool(ca_cfg.get("fallback_enforce_diversity", False))
                else 0,
                correlation_groups=_corr_groups
                if bool(ca_cfg.get("fallback_enforce_diversity", False))
                else None,
                portfolio=portfolio,
                equity=float(equity),
                default_hard_cap_frac=float(_cap_hard),
                symbol_cap_fractions=_tier_caps,
            )
            _selected_candidates_debug = list(_fb_candidates)
            _fb_idle = empty_alloc_fixed_size_buys(
                candidates=_fb_candidates,
                equity=float(equity),
                cash=float(deployable_cash),
                min_realloc_leg=float(_min_leg_cfg),
                top_n=_fb_topn,
                size_pct=_fb_size,
            )
            if _fb_idle and _idle_cap_headroom is not None:
                _fb_before = sum(float(x.get("notional", 0) or 0) for x in _fb_idle)
                _fb_idle = clip_buy_actions_to_gross_headroom_dollars(
                    _fb_idle,
                    gross_headroom_dollars=_idle_cap_headroom,
                    min_realloc_leg=float(_min_leg_cfg),
                )
                _fb_after = sum(float(x.get("notional", 0) or 0) for x in _fb_idle)
                if _fb_after + 1e-6 < _fb_before:
                    log.info(
                        "[%s] capital_allocator: idle fallback gross cap clipped buys $%.0f → $%.0f (cap %.1f%%)",
                        str(user_id),
                        _fb_before,
                        _fb_after,
                        float(_idle_max_gross) * 100.0,
                    )
                if not _fb_idle:
                    _allocator_skip_reason = "idle_fallback_gross_cap"
            if _fb_idle:
                _syms = ",".join(str(x.get("symbol", "")) for x in _fb_idle)
                _tot = sum(float(x.get("notional", 0) or 0) for x in _fb_idle)
                log.warning(
                    "[%s] capital_allocator: idle fallback after %d empty cycle(s) — fixed-size BUY %d name(s) $%.0f total (%s)",
                    str(user_id),
                    _idle_count,
                    len(_fb_idle),
                    _tot,
                    _syms,
                )
                if _fb_group_skips:
                    log.info(
                        "[%s] capital_allocator: idle fallback group-cap skips %s",
                        str(user_id),
                        _fb_group_skips,
                    )
                actions = apply_cooldown(_fb_idle, portfolio, exit_context=exit_context)
                if actions:
                    _ALLOCATOR_EMPTY_ACTION_CYCLES[_idle_key] = 0
                    _allocator_skip_reason = "idle_fallback"
                else:
                    for _a in _fb_idle:
                        if str(_a.get("action", "")).lower() == "buy":
                            _print_allocator_skip(str(_a.get("symbol", "")).upper(), "cooldown")
                    log.info(
                        "[%s] capital_allocator: idle fallback buys removed by apply_cooldown",
                        str(user_id),
                    )
                    _allocator_skip_reason = "idle_fallback_removed_by_cooldown"
    if actions:
        _trace_before = [dict(a) for a in actions]
        _open_order_symbols = _open_order_symbols_for_broker(broker)
        _filtered_actions: list[dict[str, Any]] = []
        _removed_sell = False
        for _a in actions:
            _side = str(_a.get("action", "")).strip().lower()
            _sym = str(_a.get("symbol", "")).strip().upper()
            if _side == "sell" and _sym:
                _held_qty = _position_held_for_orders_qty(positions, _sym)
                if _sym in _open_order_symbols or _held_qty > 0.0:
                    _removed_sell = True
                    detail = (
                        "open order exists"
                        if _sym in _open_order_symbols
                        else "held_for_orders %.4g > 0" % _held_qty
                    )
                    _print_allocator_skip(_sym, "open order", detail=detail)
                    log.info(
                        "[%s] capital_allocator: skip SELL %s — %s",
                        str(user_id),
                        _sym,
                        detail,
                    )
                    continue
            _filtered_actions.append(dict(_a))
        actions = _filtered_actions
        _removed_stage = _log_post_planner_action_trace(
            stage="open_order_sell_filter",
            before=_trace_before,
            after=actions,
            reason="skip_sells_with_open_orders_or_held_for_orders",
        )
        if _removed_stage is not None:
            _last_post_planner_removal_stage = _removed_stage
        if (
            _removed_sell
            and bool(ca_cfg.get("require_net_sell_gte_buy", True))
            and actions
            and not force_allocate
        ):
            _b_before = _allocator_nsum(actions, "buy")
            actions = trim_allocator_actions_for_net_sell_gte_buy(
                actions,
                min_realloc_leg=float(_min_leg_cfg),
            )
            _b_after = _allocator_nsum(actions, "buy")
            if _b_after < _b_before - 1e-6:
                log.info(
                    "[%s] capital_allocator: open-order sell skip triggered net trim (buys $%.0f→$%.0f)",
                    str(user_id),
                    _b_before,
                    _b_after,
                )
    if not actions and _selected_candidates_debug:
        _terminal_stage = (
            "allocator_filtered"
            if _post_planner_actions_seen or _last_post_planner_removal_stage != "none"
            else "allocator_no_action"
        )
        _terminal_reason = (
            f"actions_removed_by_post_planner_filter last_removal_stage={_last_post_planner_removal_stage}"
            if _terminal_stage == "allocator_filtered"
            else str(_allocator_skip_reason or "allocator_returned_no_actions")
        )
        for _cand_final in _selected_candidates_debug:
            if not isinstance(_cand_final, Mapping):
                continue
            _sym_final = str(_cand_final.get("symbol") or "").strip().upper()
            if not _sym_final:
                continue
            _record_entry_terminal_outcome(
                store=event_store,
                user_id=str(user_id),
                symbol=_sym_final,
                route=str(_cand_final.get("route") or _cand_final.get("source") or "allocator"),
                stage=_terminal_stage,
                reason=_terminal_reason,
                payload=_entry_terminal_payload(
                    _cand_final,
                    last_removal_stage=_last_post_planner_removal_stage,
                    allocator_skip_reason=_allocator_skip_reason,
                    minimum_cash_to_deploy=ca_cfg.get("minimum_cash_to_deploy"),
                    minimum_cash_to_deploy_pct=ca_cfg.get("minimum_cash_to_deploy_pct"),
                    min_order_notional=ca_cfg.get("min_order_notional"),
                    min_realloc_leg=ca_cfg.get("min_realloc_leg"),
                    available_cash=deployable_cash,
                    cash=cash,
                    gross_headroom=_headroom_d,
                    allow_allocator_buys=allow_allocator_buys,
                    no_recycle_block=no_recycle_block,
                ),
                ts=dt,
            )
    _final_reject_candidate_lookup = _final_allocator_candidate_lookup(
        _pre_liquidity_candidates,
        _allocator_initial_candidates,
        _profile_input_candidates,
        candidates,
        _ranked_candidates_debug,
        _selected_candidates_debug,
        _signal_by_symbol,
    )
    _allocator_drop_reasons = _finalize_allocator_reject_reasons_for_print(
        _allocator_drop_reasons,
        candidate_lookup=_final_reject_candidate_lookup,
        config=config if isinstance(config, Mapping) else {},
    )
    print("ALLOCATOR DEBUG:")
    print("candidates:", len(signals))
    print(
        "ranked:",
        [str(x.get("symbol", "")).upper() for x in _ranked_candidates_debug[:20]],
    )
    print(
        "selected:",
        [str(x.get("symbol", "")).upper() for x in _selected_candidates_debug[:20]],
    )
    print(
        "reject_reasons:",
        [
            f"{sym}:{reason}"
            for sym, reason in sorted(_allocator_drop_reasons.items())
        ],
    )
    print("reason skipped:", _allocator_skip_reason)
    print("ALLOCATOR ACTIONS:", actions)
    log.info(
        "ALLOCATOR_ACTIONS count=%d actions=%s",
        len(actions or []),
        _allocator_actions_repr(actions),
    )

    if actions:
        _min_leg = _min_leg_cfg
        mq = getattr(engine, "market_quality", None)
        _candidate_by_symbol = {
            str(c.get("symbol") or "").strip().upper(): c
            for c in candidates
            if str(c.get("symbol") or "").strip()
        }

        def _refresh_from_broker() -> None:
            fresh = broker.get_positions()
            positions.clear()
            positions.extend(fresh)
            for p in positions:
                sym_r = str(p.get("symbol") or "").strip().upper()
                if not sym_r:
                    continue
                qty_raw = int(float(p.get("qty") or 0))
                avg_px = float(p.get("avg_price") or p.get("avg_entry_price") or 0) or 0.0
                if avg_px <= 0 and qty_raw != 0:
                    avg_px = abs(float(p.get("cost_basis") or 0) / qty_raw)
                reconcile_tracked(sym_r, qty_raw, avg_px, user_id=user_id, data_dir=data_dir)

        uid = str(user_id)
        dd: Path | str = data_dir

        def _execute_allocator_action(a: Mapping[str, Any]) -> None:
            sym = str(a.get("symbol") or "").strip().upper()
            side = str(a.get("action") or "").strip().lower()
            amt = clean_notional(a.get("notional", 0))
            route = str(a.get("route") or a.get("source") or "n/a")
            _log_allocator_dispatch_start(
                sym or "?",
                action=side or "?",
                notional=float(amt or 0.0),
                source=str(a.get("source") or route or "capital_allocator"),
            )

            def _action_blocked(
                reason: str,
                *,
                stage: str = "order_builder_rejected",
                order_skip_fields: Mapping[str, Any] | None = None,
            ) -> None:
                record_trade_attribution_order_event(
                    data_dir=dd,
                    user_id=uid,
                    timestamp=dt,
                    symbol=sym or "?",
                    action=side or "?",
                    route=route,
                    source=str(a.get("source") or "") or None,
                    notional=amt,
                    order_build_status="not_built",
                    reject_reason=reason,
                    submit_attempt=False,
                    submitted=False,
                    allow_replay_attribution=allow_replay_attribution,
                )
                _record_entry_terminal_outcome(
                    store=event_store,
                    user_id=uid,
                    symbol=sym or "?",
                    route=route,
                    stage=stage,
                    reason=reason,
                    payload=_entry_terminal_payload(
                        _candidate_by_symbol.get(sym, {}),
                        action=side,
                        notional=amt,
                    ),
                    ts=dt,
                )
                log.info(
                    "ALLOCATOR_ACTION_BLOCKED symbol=%s reason=%s action=%s notional=%.2f route=%s",
                    sym or "?",
                    reason,
                    side or "?",
                    float(amt or 0.0),
                    route,
                )
                log.info(
                    "ALLOCATOR_SKIP symbol=%s reason=%s",
                    sym or "?",
                    reason,
                )
                _log_dynamic_dispatch_explainability(
                    sym or "?",
                    action=a,
                    candidate=_candidate_by_symbol.get(sym, {}),
                    route=route,
                    notional=amt,
                    result="skipped",
                    reason=reason,
                )
                _log_order_skip(sym or "?", reason, fields=order_skip_fields)
                _log_allocator_dispatch_skipped(sym or "?", reason=reason)
                _log_allocator_dispatch_done(
                    sym or "?",
                    result="skipped",
                    reason=reason,
                )

            def _post_check_exit(reason: str) -> None:
                log.info(
                    "ALLOCATOR_ACTION_POST_CHECK_EXIT symbol=%s reason=%s action=%s notional=%.2f route=%s",
                    sym or "?",
                    reason,
                    side or "?",
                    float(amt or 0.0),
                    route,
                )

            def _action_exception(error: Exception) -> None:
                _error_reason = f"{type(error).__name__}: {str(error)[:200]}"
                log.exception(
                    "ALLOCATOR_ACTION_EXCEPTION symbol=%s action=%s notional=%.2f route=%s error=%s",
                    sym or "?",
                    side or "?",
                    float(amt or 0.0),
                    route,
                    _error_reason,
                )
                _log_allocator_dispatch_error(sym or "?", reason=_error_reason)
                _record_entry_terminal_outcome(
                    store=event_store,
                    user_id=uid,
                    symbol=sym or "?",
                    route=route,
                    stage="broker_rejected",
                    reason=_error_reason,
                    payload=_entry_terminal_payload(
                        _candidate_by_symbol.get(sym, {}),
                        action=side,
                        notional=amt,
                    ),
                    ts=dt,
                )

            def _action_entry_blocked(error: EntryBlocked) -> None:
                reason = str(error) or "ENTRY_BLOCKED"
                log.info(
                    "ENTRY_BLOCKED_MODE symbol=%s action=%s notional=%.2f route=%s reason=%s",
                    sym or "?",
                    side or "?",
                    float(amt or 0.0),
                    route,
                    reason,
                )
                record_trade_attribution_order_event(
                    data_dir=dd,
                    user_id=uid,
                    timestamp=dt,
                    symbol=sym or "?",
                    action=side or "?",
                    route=route,
                    source=str(a.get("source") or "") or None,
                    notional=amt,
                    order_build_status="blocked",
                    reject_reason=reason,
                    submit_attempt=True,
                    submitted=False,
                    allow_replay_attribution=allow_replay_attribution,
                )
                _record_entry_terminal_outcome(
                    store=event_store,
                    user_id=uid,
                    symbol=sym or "?",
                    route=route,
                    stage="entry_blocked_mode" if is_expected_entry_block(reason) else "entry_blocked",
                    reason=reason,
                    payload=_entry_terminal_payload(
                        _candidate_by_symbol.get(sym, {}),
                        action=side,
                        notional=amt,
                    ),
                    ts=dt,
                )
                _log_allocator_dispatch_blocked(sym or "?", reason=reason)
                _log_allocator_dispatch_done(sym or "?", result="blocked", reason=reason)

            if not sym or side not in ("buy", "sell"):
                logger.info(
                    "[ALLOCATOR_SKIP] %s reason=invalid action row",
                    sym or "?",
                )
                _action_blocked("invalid_action")
                return
            if amt <= 0:
                logger.info("[ALLOCATOR_SKIP] %s reason=notional <= 0", sym)
                _print_allocator_skip(sym, "size = 0")
                _action_blocked("notional_nonpositive")
                return
            if abs(amt) < _min_leg:
                _print_allocator_skip(
                    sym,
                    "size = 0",
                    detail="notional $%.2f < min_realloc_leg %.0f" % (amt, _min_leg),
                )
                log.info(
                    "ALLOCATOR_REJECT %s reason=%s",
                    sym,
                    "notional below min_realloc_leg",
                )
                log.info(
                    "[%s] capital_allocator: skip %s %s notional $%.2f (< min_realloc_leg %.0f)",
                    uid,
                    side,
                    sym,
                    amt,
                    _min_leg,
                )
                logger.info(
                    "[ALLOCATOR_SKIP] %s reason=notional below min_realloc_leg",
                    sym,
                )
                _action_blocked("notional_below_min_realloc_leg")
                return

            if side == "buy" and exit_context is not None:
                _bcool, _bcool_why = exit_context.bulk_trim_buy_cooldown_active(sym)
                if _bcool:
                    logger.info(
                        "[ALLOCATOR_SKIP] %s reason=bulk trim buy cooldown",
                        sym,
                    )
                    _print_allocator_skip(
                        sym,
                        "cooldown",
                        detail=_bcool_why or "bulk trim buy cooldown",
                    )
                    log.info(
                        "ALLOCATOR_REJECT %s reason=%s",
                        sym,
                        _bcool_why or "bulk trim buy cooldown",
                    )
                    log.warning(
                        "[%s] capital_allocator: skip BUY %s $%.2f — %s",
                        uid,
                        sym,
                        amt,
                        _bcool_why or "bulk trim buy cooldown",
                    )
                    _action_blocked("bulk_trim_buy_cooldown", stage="risk_rejected")
                    return
                _blocked, _why = exit_context.allocator_buy_blocked_by_priority(sym)
                if _blocked:
                    logger.info(
                        "[ALLOCATOR_SKIP] %s reason=allocator buy blocked by priority (%s)",
                        sym,
                        _why or "exit intent",
                    )
                    _print_allocator_skip(
                        sym,
                        "cooldown",
                        detail=_why or "exit intent outranks new_entry",
                    )
                    log.info(
                        "ALLOCATOR_REJECT %s reason=%s",
                        sym,
                        _why or "exit intent outranks new_entry",
                    )
                    log.warning(
                        "[%s] capital_allocator: skip BUY %s $%.2f — %s",
                        uid,
                        sym,
                        amt,
                        _why or "exit intent outranks new_entry",
                    )
                    _action_blocked("allocator_buy_blocked_by_priority", stage="risk_rejected")
                    return

            if side == "buy" and not is_option_symbol(sym):
                _now_state = dt if isinstance(dt, datetime) else datetime.now(timezone.utc)
                _rebuy_blocked, _rebuy_why = blocks_stock_rebuy_after_sell(
                    sym,
                    uid,
                    Path(dd),
                    _now_state,
                    config,
                )
                if _rebuy_blocked:
                    logger.info(
                        "[ALLOCATOR_SKIP] %s reason=post-sell rebuy cooldown",
                        sym,
                    )
                    _print_allocator_skip(
                        sym,
                        "cooldown",
                        detail=_rebuy_why or "post-sell rebuy cooldown",
                    )
                    log.info(
                        "ALLOCATOR_REJECT %s reason=%s",
                        sym,
                        _rebuy_why or "post-sell rebuy cooldown",
                    )
                    _action_blocked("post_sell_rebuy_cooldown", stage="risk_rejected")
                    return

                _tracked_before = load_tracked(uid, data_dir=dd)
                _row_before = (
                    _tracked_before.get(sym)
                    if isinstance(_tracked_before, dict)
                    else None
                )
                try:
                    _qty_before_for_gate = int(float((_row_before or {}).get("qty") or 0))
                except (TypeError, ValueError):
                    _qty_before_for_gate = 0
                _is_core_rebuild_action = bool(a.get("core_rebuild")) or route == "core_rebuild"
                _execution_cd_state = _allocator_add_on_cooldown_state(
                    symbol=sym,
                    signal_row=next(
                        (r for r in signals if str(r.get("sym_u", "")).upper() == sym),
                        None,
                    ),
                    tracked=_tracked_before,
                    et_date_iso=str(et_date_iso) if et_date_iso else None,
                    now_dt=dt,
                    core_rebuild=_is_core_rebuild_action,
                )
                _log_allocator_cooldown_state("EXECUTION_COOLDOWN_STATE", _execution_cd_state)
                if _qty_before_for_gate > 0 and et_date_iso and not _is_core_rebuild_action:
                    if bool(_execution_cd_state.get("blocked")):
                        _sig_score = _allocator_float(_execution_cd_state.get("signal_score"), 0.0)
                        logger.info(
                            "[ALLOCATOR_SKIP] %s reason=add-on daily cap",
                            sym,
                        )
                        _print_allocator_skip(
                            sym,
                            "cooldown",
                            detail=(
                                "allocator add-on already used today; signal_score %.1f < 85"
                                % _sig_score
                            ),
                        )
                        log.info(
                            "ALLOCATOR_REJECT %s reason=allocator add-on once per day",
                            sym,
                        )
                        _action_blocked("allocator_add_on_once_per_day", stage="risk_rejected")
                        return

            row_tl = next((r for r in signals if str(r.get("sym_u", "")).upper() == sym), None)
            df = row_tl.get("df") if row_tl else None
            _cand_tl = _candidate_by_symbol.get(sym, {})
            _row_or_cand = _allocator_dispatch_candidate_metadata(row_tl, _cand_tl, a)
            _dynamic_aggressive_action = bool(
                _allocator_is_dynamic_aggressive(_row_or_cand)
                or _allocator_is_dynamic_aggressive(a)
            )
            if side == "buy":
                _log_dynamic_dispatch_latency(sym, _row_or_cand)
            _weak_catalyst_dynamic_action = bool(_allocator_weak_catalyst_dynamic(_row_or_cand))
            _live_weak_catalyst_exceptional_action = False
            _live_weak_catalyst_exception_experiment_action = False
            _live_weak_catalyst_exception_cap = 0.0
            _is_live_buy = bool(
                side == "buy"
                and not _allocator_paper_execution_context(user_id=uid, config=config)
            )
            _is_live_dynamic_buy = bool(
                _is_live_buy
                and allocation_profile_is_dynamic_candidate(_row_or_cand)
            )
            _trend_reentry_decision = _trend_reentry_protection_decision(
                symbol=sym,
                side=side,
                route=route,
                candidate=_row_or_cand,
                action=a,
                config=config if isinstance(config, Mapping) else {},
                data_dir=dd,
                user_id=uid,
                day=str(et_date_iso) if et_date_iso else None,
                now=dt if isinstance(dt, datetime) else datetime.now(timezone.utc),
            )
            if str(_trend_reentry_decision.get("reason") or "") == "expired_with_fresh_signal":
                log.info(
                    "TREND_REENTRY_PROTECTION_EXPIRED symbol=%s stop_time=%s age_minutes=%.1f fresh_signal=%s breakout=%s new_intraday_high=%s new_signal_timestamp=%s",
                    sym,
                    str(_trend_reentry_decision.get("stop_time") or "n/a"),
                    float(_trend_reentry_decision.get("age_minutes") or 0.0),
                    str(bool(_trend_reentry_decision.get("fresh_signal"))).lower(),
                    str(bool(_trend_reentry_decision.get("breakout"))).lower(),
                    str(bool(_trend_reentry_decision.get("new_intraday_high"))).lower(),
                    str(_trend_reentry_decision.get("new_signal_timestamp") or "n/a"),
                )
            if str(_trend_reentry_decision.get("reason") or "") not in {"disabled", "not_buy", "option", "not_trend_long", "live_disabled", "no_prior_stop"}:
                log.info(
                    "TREND_REENTRY_PROTECTION_CHECK symbol=%s action=%s route=%s decision=%s reason=%s stop_time=%s cooldown_remaining_minutes=%.1f breakout=%s new_intraday_high=%s new_signal_timestamp=%s",
                    sym,
                    side,
                    route,
                    str(_trend_reentry_decision.get("action") or "allow"),
                    str(_trend_reentry_decision.get("reason") or "unknown"),
                    str(_trend_reentry_decision.get("stop_time") or "n/a"),
                    float(_trend_reentry_decision.get("cooldown_remaining_minutes") or 0.0),
                    str(bool(_trend_reentry_decision.get("breakout"))).lower(),
                    str(bool(_trend_reentry_decision.get("new_intraday_high"))).lower(),
                    str(_trend_reentry_decision.get("new_signal_timestamp") or "n/a"),
                )
            if str(_trend_reentry_decision.get("action") or "") == "block":
                log.info(
                    "TREND_REENTRY_PROTECTION_BLOCK symbol=%s reason=%s stop_time=%s cooldown_remaining_minutes=%.1f fresh_signal=%s",
                    sym,
                    str(_trend_reentry_decision.get("reason") or "unknown"),
                    str(_trend_reentry_decision.get("stop_time") or "n/a"),
                    float(_trend_reentry_decision.get("cooldown_remaining_minutes") or 0.0),
                    str(bool(_trend_reentry_decision.get("fresh_signal"))).lower(),
                )
                _action_blocked(
                    "trend_reentry_protection",
                    stage="risk_rejected",
                    order_skip_fields={
                        "route": route,
                        "gate_reason": str(_trend_reentry_decision.get("reason") or "unknown"),
                        "cooldown_remaining_minutes": "%.1f" % float(_trend_reentry_decision.get("cooldown_remaining_minutes") or 0.0),
                    },
                )
                return
            if str(_trend_reentry_decision.get("action") or "") == "allow" and str(_trend_reentry_decision.get("reason") or "") == "expired_with_fresh_signal":
                log.info(
                    "TREND_REENTRY_PROTECTION_ALLOW symbol=%s reason=expired_with_fresh_signal stop_time=%s age_minutes=%.1f",
                    sym,
                    str(_trend_reentry_decision.get("stop_time") or "n/a"),
                    float(_trend_reentry_decision.get("age_minutes") or 0.0),
                )
            _expectancy_gate_decision = _dynamic_momentum_expectancy_gate_decision(
                symbol=sym,
                route=route,
                side=side,
                candidate=_row_or_cand,
                config=config if isinstance(config, Mapping) else {},
                data_dir=dd,
                user_id=uid,
                day=str(et_date_iso) if et_date_iso else None,
            )
            if str(_expectancy_gate_decision.get("action") or "") == "block":
                log.info(
                    "DYNAMIC_EXPECTANCY_GATE_BLOCK symbol=%s route=%s reason=%s scope=%s sample_count=%s expectancy_score=%s",
                    sym,
                    str(_expectancy_gate_decision.get("route") or route),
                    str(_expectancy_gate_decision.get("reason") or "unknown"),
                    str(_expectancy_gate_decision.get("scope") or "n/a"),
                    str(_expectancy_gate_decision.get("sample_count") or "n/a"),
                    str(_expectancy_gate_decision.get("expectancy_score") or "n/a"),
                )
                _action_blocked(
                    "dynamic_expectancy_gate_block",
                    stage="risk_rejected",
                    order_skip_fields={
                        "route": str(_expectancy_gate_decision.get("route") or route),
                        "gate_reason": str(_expectancy_gate_decision.get("reason") or "unknown"),
                    },
                )
                return
            if _is_live_buy:
                _dispatch_now = _allocator_now_utc(dt)
                if not _weak_catalyst_dynamic_action:
                    _dynamic_execution_cooldown_clear(
                        user_id=uid,
                        symbol=sym,
                        reason=(
                            "strong_catalyst"
                            if _allocator_strong_catalyst_dynamic(_row_or_cand)
                            else "no_longer_weak_catalyst_dynamic"
                        ),
                    )
                elif _dynamic_execution_cooldown_active(
                    user_id=uid,
                    symbol=sym,
                    now=_dispatch_now,
                ):
                    _action_blocked(
                        "weak_catalyst_execution_cooldown",
                        order_skip_fields={
                            "route": route,
                            "is_dynamic": "true",
                            "cooldown_reason": _WEAK_CATALYST_EXECUTION_COOLDOWN_REASON,
                        },
                    )
                    return
            if (
                side == "buy"
                and allocation_profile_is_dynamic_candidate(_row_or_cand)
                and _allocator_paper_execution_context(user_id=uid, config=config)
            ):
                _churn_block = _paper_dynamic_churn_guard_block(
                    symbol=sym,
                    candidate=_row_or_cand,
                    data_dir=dd,
                    user_id=uid,
                    now=dt if isinstance(dt, datetime) else datetime.now(timezone.utc),
                    account_equity=equity,
                    config=config if isinstance(config, Mapping) else {},
                )
                if _churn_block is not None:
                    _churn_reason, _churn_fields = _churn_block
                    log.warning(
                        "CHURN_GUARD_BLOCK symbol=%s reason=%s mode=paper route=%s source=%s details=%s",
                        sym,
                        _churn_reason,
                        route,
                        str(a.get("source") or _row_or_cand.get("source") or "n/a"),
                        _churn_fields,
                    )
                    _action_blocked(
                        _churn_reason,
                        stage="risk_rejected",
                        order_skip_fields={
                            "route": route,
                            "is_dynamic": "true",
                            **_churn_fields,
                        },
                    )
                    return
            quote = broker.get_latest_quote(sym)
            if not quote:
                logger.info("[ALLOCATOR_SKIP] %s reason=no quote", sym)
                _print_allocator_skip(sym, "size = 0", detail="no quote")
                log.info("ALLOCATOR_REJECT %s reason=%s", sym, "no quote")
                log.warning("[%s] capital_allocator: no quote for %s — skip %s", uid, sym, side)
                if side == "buy" and allocation_profile_is_dynamic_candidate(_row_or_cand):
                    _allocator_block_symbol(
                        user_id=uid,
                        symbol=sym,
                        reason="no_quote",
                        ttl_min=_allocator_block_ttl_min(config, ca_cfg),
                        now=_allocator_now_utc(dt),
                    )
                _action_blocked("no_quote")
                return
            _px_fb = 0.0
            try:
                if df is not None and not getattr(df, "empty", True):
                    _px_fb = float(df["close"].iloc[-1])
            except Exception:
                pass
            mid = quote.reference_mid(_px_fb if _px_fb > 0 else float(getattr(quote, "mid", 0) or 0))
            if quote and getattr(quote, "is_stale", None) and quote.is_stale(stale_quote_max_age):
                spread_pct = 0.15
            else:
                spread_pct = float(quote.spread_pct) if quote.spread_pct is not None else 0.15
            _skip_spread = bool(getattr(quote, "skip_spread_check", False))
            _ignore_lv = False
            if mq is not None and df is not None and not getattr(df, "empty", True):
                try:
                    _ignore_lv = bool(mq.should_ignore_spread_for_low_volume(last_bar_volume_from_ohlcv(df)))
                except Exception:
                    _ignore_lv = False
            if side == "buy" and allocation_profile_is_dynamic_candidate(_row_or_cand):
                _quote_hard_reason = _quote_bad_or_unstable_reason(
                    quote,
                    stale_quote_max_age=stale_quote_max_age,
                )
                if _quote_hard_reason in {"bad_quote", "unstable_quote"}:
                    logger.info("[ALLOCATOR_SKIP] %s reason=dynamic %s", sym, _quote_hard_reason)
                    log.info("ALLOCATOR_REJECT %s reason=%s", sym, "dynamic_%s" % _quote_hard_reason)
                    _allocator_block_symbol(
                        user_id=uid,
                        symbol=sym,
                        reason=_quote_hard_reason,
                        ttl_min=_allocator_block_ttl_min(config, ca_cfg),
                        now=_allocator_now_utc(dt),
                    )
                    _action_blocked("dynamic_%s" % _quote_hard_reason)
                    return
                if _skip_spread:
                    logger.info("[ALLOCATOR_SKIP] %s reason=dynamic unstable quote", sym)
                    log.info("ALLOCATOR_REJECT %s reason=%s", sym, "dynamic unstable quote")
                    _allocator_block_symbol(
                        user_id=uid,
                        symbol=sym,
                        reason="unstable_quote",
                        ttl_min=_allocator_block_ttl_min(config, ca_cfg),
                        now=_allocator_now_utc(dt),
                    )
                    _action_blocked("dynamic_unstable_quote")
                    return
                _price_ctx = _dynamic_dispatch_price_context(
                    _row_or_cand,
                    quote_mid=float(mid or 0.0),
                    config=config if isinstance(config, Mapping) else {},
                    broker_is_paper=_allocator_paper_execution_context(user_id=uid, config=config),
                )
                _observed_dynamic_price = float(_price_ctx["observed_price"])
                _dynamic_min_price = float(_price_ctx["min_price"])
                if _observed_dynamic_price < _dynamic_min_price - 1e-9:
                    _price_skip_fields = {
                        "observed_price": "%.4f" % _observed_dynamic_price,
                        "min_price": "%.4f" % _dynamic_min_price,
                        "price_source": _price_ctx.get("price_source"),
                        "route": route,
                        "is_dynamic": str(bool(allocation_profile_is_dynamic_candidate(_row_or_cand))).lower(),
                        "scanner_score": _allocator_diag_field(_row_or_cand, "scanner_score", "dynamic_score", "score", default="n/a"),
                        "news_score": _allocator_diag_field(_row_or_cand, "news_score", default="n/a"),
                        "catalyst_score": _allocator_diag_field(_row_or_cand, "catalyst_score", default="n/a"),
                    }
                    logger.info(
                        "[ALLOCATOR_SKIP] %s reason=dynamic price below minimum observed_price=%.4f min_price=%.4f price_source=%s",
                        sym,
                        _observed_dynamic_price,
                        _dynamic_min_price,
                        _price_ctx.get("price_source"),
                    )
                    log.info(
                        "ALLOCATOR_REJECT %s reason=%s observed_price=%.4f min_price=%.4f price_source=%s route=%s",
                        sym,
                        "dynamic price below minimum",
                        _observed_dynamic_price,
                        _dynamic_min_price,
                        _price_ctx.get("price_source"),
                        route,
                    )
                    _action_blocked(
                        "dynamic_price_below_minimum",
                        order_skip_fields=_price_skip_fields,
                    )
                    return
                if _dynamic_aggressive_action:
                    _ok_aggr, _reason_aggr, _meta_aggr = _allocator_dynamic_aggressive_decision(
                        _row_or_cand,
                        config=config if isinstance(config, Mapping) else {},
                        user_id=uid,
                        symbol=sym,
                        spread_pct=spread_pct,
                        current_positions=current_positions,
                        tracked=tracked if isinstance(tracked, Mapping) else {},
                        positions=positions,
                        open_order_symbols=_allocator_open_order_symbols(broker),
                    )
                    if not _ok_aggr:
                        log.info(
                            "DYNAMIC_AGGRESSIVE_REJECT symbol=%s reason=%s",
                            sym,
                            _reason_aggr,
                        )
                        _action_blocked("dynamic_aggressive_%s" % _reason_aggr)
                        return
                    log.info("DYNAMIC_AGGRESSIVE_ACCEPT symbol=%s reason=%s", sym, _reason_aggr)
                _dyn_cap = dynamic_spread_cap_pct(_row_or_cand)
                if spread_pct is None or not math.isfinite(float(spread_pct)) or float(spread_pct) > _dyn_cap + 1e-9:
                    if _weak_catalyst_dynamic_action:
                        log.info(
                            "DYNAMIC_WEAK_CATALYST_REJECT symbol=%s reason=spread_above_cap spread_pct=%s cap=%.3f",
                            sym,
                            "n/a" if spread_pct is None else "%.3f" % float(spread_pct),
                            float(_dyn_cap),
                        )
                    logger.info("[ALLOCATOR_SKIP] %s reason=dynamic spread cap", sym)
                    log.info(
                        "ALLOCATOR_REJECT %s reason=dynamic spread %.3f%% > %.2f%%",
                        sym,
                        float(spread_pct or 0.0),
                        float(_dyn_cap),
                    )
                    _action_blocked("dynamic_spread_cap")
                    return
                if _weak_catalyst_dynamic_action:
                    _weak_rel = _allocator_preserved_dynamic_relative_volume(_row_or_cand)
                    if _weak_rel is None:
                        _weak_rel = max(
                            _allocator_float(_row_or_cand.get("relative_volume"), 0.0),
                            _allocator_float(_row_or_cand.get("rel_volume"), 0.0),
                        )
                    log.info(
                        "DYNAMIC_WEAK_CATALYST_CLASSIFIED symbol=%s news_score=%.2f catalyst_score=%.2f event_score=%.2f scanner_score=%s relative_volume=%.3f",
                        sym,
                        _allocator_float(_row_or_cand.get("news_score"), 0.0),
                        _allocator_float(_row_or_cand.get("catalyst_score"), 0.0),
                        _allocator_float(_row_or_cand.get("event_score"), 0.0),
                        _allocator_diag_field(_row_or_cand, "scanner_score", "dynamic_score", "signal_score", "score", default="n/a"),
                        float(_weak_rel or 0.0),
                    )
                    _weak_reject_reason = _allocator_weak_catalyst_reject_reason(
                        _row_or_cand,
                        relative_volume=float(_weak_rel or 0.0),
                        spread_pct=float(spread_pct),
                        spread_cap_pct=float(_dyn_cap),
                    )
                    if _weak_reject_reason is not None:
                        log.info(
                            "DYNAMIC_WEAK_CATALYST_REJECT symbol=%s reason=%s relative_volume=%.3f scanner_score=%s vwap_above=%s unstable_quote_recent_scan=%s",
                            sym,
                            _weak_reject_reason,
                            float(_weak_rel or 0.0),
                            _allocator_diag_field(_row_or_cand, "scanner_score", "dynamic_score", "signal_score", "score", default="n/a"),
                            str(_allocator_dynamic_price_above_vwap(_row_or_cand)).lower(),
                            str(_allocator_dynamic_recent_unstable_quote(_row_or_cand)).lower(),
                        )
                        _action_blocked("dynamic_weak_catalyst_%s" % _weak_reject_reason)
                        return
                    if (
                        not _allocator_paper_execution_context(user_id=uid, config=config)
                        and _allocator_live_weak_catalyst_guard_enabled(config if isinstance(config, Mapping) else {})
                    ):
                        _live_exceptional, _live_guard_meta = _allocator_live_weak_catalyst_exceptional(
                            _row_or_cand,
                            config if isinstance(config, Mapping) else {},
                        )
                        if not _live_exceptional:
                            _exp_ok, _exp_reason, _exp_meta = _allocator_live_weak_catalyst_exception_experiment_decision(
                                _row_or_cand,
                                config=config if isinstance(config, Mapping) else {},
                                user_id=uid,
                                symbol=sym,
                                side=side,
                                price=float(mid or 0.0),
                                spread_pct=spread_pct,
                                notional=float(amt or 0.0),
                                current_positions=current_positions,
                                tracked=tracked if isinstance(tracked, Mapping) else {},
                                positions=positions,
                                dt=dt,
                            )
                            log.info(
                                "LIVE_WEAK_CATALYST_EXCEPTION_CHECK symbol=%s enabled=%s price=%.4f min_price=%.4f "
                                "gain_pct=%.3f min_gain_pct=%.3f relative_volume=%.3f min_relative_volume=%.3f "
                                "spread_pct=%s max_spread_pct=%.3f atr_pct=%s max_atr_pct=%.3f "
                                "entry_eval_pass=%s used_today=%d max_positions_per_day=%d existing_position_qty=%.6f result=%s reason=%s",
                                sym,
                                str(bool(_exp_meta.get("enabled"))).lower(),
                                float(_exp_meta.get("price") or 0.0),
                                float(_exp_meta.get("min_price") or 0.0),
                                float(_exp_meta.get("gain_pct") or 0.0),
                                float(_exp_meta.get("min_gain_pct") or 0.0),
                                float(_exp_meta.get("relative_volume") or 0.0),
                                float(_exp_meta.get("min_relative_volume") or 0.0),
                                "n/a" if _exp_meta.get("spread_pct") is None else "%.3f" % float(_exp_meta.get("spread_pct") or 0.0),
                                float(_exp_meta.get("max_spread_pct") or 0.0),
                                "n/a" if _exp_meta.get("atr_pct") is None else "%.3f" % float(_exp_meta.get("atr_pct") or 0.0),
                                float(_exp_meta.get("max_atr_pct") or 0.0),
                                str(bool(_exp_meta.get("entry_eval_pass"))).lower(),
                                int(_exp_meta.get("used_today") or 0),
                                int(_exp_meta.get("max_positions_per_day") or 0),
                                float(_exp_meta.get("existing_position_qty") or 0.0),
                                "allow" if _exp_ok else "reject",
                                _exp_reason,
                            )
                            if _exp_ok:
                                _live_weak_catalyst_exception_experiment_action = True
                                _live_weak_catalyst_exception_cap = float(_exp_meta.get("notional_cap") or 300.0)
                                _LIVE_WEAK_CATALYST_EXCEPTION_DAY_COUNTS[
                                    _live_weak_catalyst_exception_day_key(uid, dt)
                                ] = int(_exp_meta.get("used_today") or 0) + 1
                                log.info(
                                    "LIVE_WEAK_CATALYST_EXCEPTION_ALLOW symbol=%s reason=ok notional=%.2f cap=%.2f",
                                    sym,
                                    float(amt or 0.0),
                                    float(_live_weak_catalyst_exception_cap),
                                )
                            else:
                                log.info(
                                    "LIVE_WEAK_CATALYST_EXCEPTION_REJECT symbol=%s reason=%s",
                                    sym,
                                    _exp_reason,
                                )
                            if _exp_ok:
                                _live_weak_catalyst_exceptional_action = True
                            else:
                                log.info(
                                    "WEAK_CATALYST_DYNAMIC_BLOCKED symbol=%s reason=non_exceptional_zero_news_live "
                                    "relative_volume=%.3f required_relative_volume=%.3f gain_pct=%.3f "
                                    "required_gain_pct=%.3f scanner_score=%.2f required_scanner_score=%.2f aligned=%s",
                                    sym,
                                    float(_live_guard_meta["relative_volume"]),
                                    float(_live_guard_meta["min_relative_volume"]),
                                    float(_live_guard_meta["gain_pct"]),
                                    float(_live_guard_meta["min_gain_pct"]),
                                    float(_live_guard_meta["scanner_score"]),
                                    float(_live_guard_meta["min_scanner_score"]),
                                    str(bool(_live_guard_meta["aligned"])).lower(),
                                )
                                _dynamic_execution_cooldown_start(
                                    user_id=uid,
                                    symbol=sym,
                                    now=_allocator_now_utc(dt),
                                    minutes=_dynamic_weak_catalyst_execution_cooldown_minutes(
                                        config if isinstance(config, Mapping) else {}
                                    ),
                                )
                                _action_blocked("weak_catalyst_dynamic_non_exceptional_live")
                                return
                        else:
                            _live_weak_catalyst_exceptional_action = True
                _rvol_context = _allocator_dynamic_dispatch_rvol_context(
                    _row_or_cand,
                    user_id=uid,
                    config=config if isinstance(config, Mapping) else {},
                )
                _rel = float(_rvol_context["observed_relative_volume"])
                _du_cfg = config.get("dynamic_universe") if isinstance(config, dict) else {}
                _du_cfg = _du_cfg if isinstance(_du_cfg, Mapping) else {}
                try:
                    _min_rel = float(
                        _du_cfg.get("min_relative_volume", _du_cfg.get("min_rel_volume", 1.0))
                        or 1.0
                    )
                except (TypeError, ValueError):
                    _min_rel = 1.0
                _effective_min_rel = _allocator_dynamic_effective_min_relative_volume(
                    _row_or_cand,
                    base_min_relative_volume=float(_min_rel),
                )
                _rvol_bypass = _allocator_premarket_catalyst_replay_bypasses_rvol(_row_or_cand)
                _rvol_entry_override_bypass = (
                    _allocator_dynamic_momentum_override_bypasses_rvol_dispatch(
                        _row_or_cand,
                        user_id=uid,
                        config=config if isinstance(config, Mapping) else {},
                    )
                )
                _rvol_bypass = bool(_rvol_bypass or _rvol_entry_override_bypass)
                _upstream_rvol_approved, _upstream_rvol_reason = _allocator_dynamic_rvol_upstream_approval(
                    _row_or_cand
                )
                _entry_override_reason = (
                    f"paper_{_upstream_rvol_reason}"
                    if _allocator_paper_execution_context(
                        user_id=uid,
                        config=config if isinstance(config, Mapping) else {},
                    )
                    else f"live_{_upstream_rvol_reason}"
                    )
                _dispatch_rvol_result = "allowed"
                _dispatch_rvol_reason = (
                    _entry_override_reason
                    if _rvol_entry_override_bypass and _rel < _effective_min_rel - 1e-9
                    else "ok"
                )
                if (
                    _rvol_entry_override_bypass
                    and float(_rvol_context["execution_relative_volume"]) < _effective_min_rel - 1e-9
                ):
                    _dispatch_rvol_reason = _entry_override_reason
                if _rel < _effective_min_rel - 1e-9 and not _rvol_bypass:
                    _dispatch_rvol_result = "skipped"
                    _dispatch_rvol_reason = "dynamic_relative_volume"
                _dispatch_diag_enabled = _dynamic_dispatch_diagnostics_enabled(
                    user_id=uid,
                    config=config if isinstance(config, Mapping) else {},
                )
                _dispatch_diag_meta: dict[str, Any] | None = None
                if _dispatch_diag_enabled:
                    _dispatch_diag_meta = _log_dispatch_dynamic_rvol_diagnostics(
                        symbol=sym,
                        candidate=_row_or_cand,
                        route=route,
                        source=a.get("source"),
                        rel_volume=float(_rel),
                        base_min_rel_volume=float(_effective_min_rel),
                        override_active=bool(_rvol_bypass),
                        dispatch_result=_dispatch_rvol_result,
                        dispatch_reason=_dispatch_rvol_reason,
                    )
                _route_for_rvol_log = str(
                    _row_or_cand.get("route")
                    or _row_or_cand.get("source")
                    or route
                    or "n/a"
                )
                log.info(
                    "DISPATCH_DYNAMIC_RVOL_DECISION symbol=%s route=%s source=%s "
                    "dynamic_candidate=%s observed_rvol=%.3f required_rvol=%.3f "
                    "scanner_relative_volume=%.3f entry_relative_volume=%.3f "
                    "allocator_relative_volume=%.3f execution_relative_volume=%.3f "
                    "dispatch_relative_volume=%.3f scanner_threshold=%.3f entry_threshold=%.3f "
                    "allocator_threshold=%.3f dispatch_threshold=%.3f threshold_used=%.3f "
                    "rejected_component=%s upstream_approved=%s upstream_reason=%s "
                    "catalyst_override_active=%s pure_momentum_override_active=%s "
                    "override_active=%s skip_or_allow_reason=%s",
                    sym,
                    _route_for_rvol_log,
                    str(a.get("source") or _row_or_cand.get("source") or "n/a"),
                    str(bool(allocation_profile_is_dynamic_candidate(_row_or_cand))).lower(),
                    float(_rel),
                    float(_effective_min_rel),
                    float(_rvol_context["scanner_relative_volume"]),
                    float(_rvol_context["entry_relative_volume"]),
                    float(_rvol_context["allocator_relative_volume"]),
                    float(_rvol_context["execution_relative_volume"]),
                    float(_rvol_context["dispatch_relative_volume"]),
                    float(_effective_min_rel),
                    float(_effective_min_rel),
                    float(_effective_min_rel),
                    float(_effective_min_rel),
                    float(_effective_min_rel),
                    "dispatch" if _dispatch_rvol_result == "skipped" else "none",
                    str(bool(_upstream_rvol_approved)).lower(),
                    _upstream_rvol_reason,
                    str(bool(_allocator_dynamic_override_context(_row_or_cand)["catalyst_override_active"])).lower(),
                    str(bool(_allocator_dynamic_override_context(_row_or_cand)["pure_momentum_override_active"])).lower(),
                    str(bool(_rvol_bypass)).lower(),
                    _dispatch_rvol_reason,
                )
                log.info(
                    "ALLOCATOR_DYNAMIC_RVOL_CHECK symbol=%s route=%s relative_volume=%.3f "
                    "required_relative_volume=%.3f catalyst_score=%.2f event_score=%.2f "
                    "news_score=%.2f bypass=%s",
                    sym,
                    _route_for_rvol_log,
                    float(_rel),
                    float(_effective_min_rel),
                    _allocator_float(_row_or_cand.get("catalyst_score"), 0.0),
                    _allocator_float(_row_or_cand.get("event_score"), 0.0),
                    _allocator_float(_row_or_cand.get("news_score"), 0.0),
                    str(_rvol_bypass).lower(),
                )
                if _dispatch_rvol_result == "skipped":
                    logger.info("[ALLOCATOR_SKIP] %s reason=dynamic relative volume", sym)
                    log.info(
                        "ALLOCATOR_REJECT %s reason=dynamic relative volume relative_volume=%.3f required_relative_volume=%.3f route=%s catalyst_score=%.2f event_score=%.2f news_score=%.2f",
                        sym,
                        float(_rel),
                        float(_effective_min_rel),
                        _route_for_rvol_log,
                        _allocator_float(_row_or_cand.get("catalyst_score"), 0.0),
                        _allocator_float(_row_or_cand.get("event_score"), 0.0),
                        _allocator_float(_row_or_cand.get("news_score"), 0.0),
                    )
                    if _dispatch_diag_enabled:
                        log.info(
                            "DISPATCH_DYNAMIC_RVOL_SKIP_DETAIL symbol=%s threshold_used=%.3f "
                            "base_min_rel_volume=%.3f rel_volume=%.3f scanner_relative_volume=%.3f "
                            "entry_relative_volume=%.3f allocator_relative_volume=%.3f "
                            "execution_relative_volume=%.3f dispatch_relative_volume=%.3f "
                            "catalyst_override_active=%s pure_momentum_override_active=%s "
                            "override_active=%s override_reason=%s missing_fields=%s "
                            "rejected_component=dispatch dispatch_reason=dynamic_relative_volume",
                            sym,
                            float((_dispatch_diag_meta or {}).get("effective_min_rel_volume") or _min_rel),
                            float(_min_rel),
                            float(_rel),
                            float(_rvol_context["scanner_relative_volume"]),
                            float(_rvol_context["entry_relative_volume"]),
                            float(_rvol_context["allocator_relative_volume"]),
                            float(_rvol_context["execution_relative_volume"]),
                            float(_rvol_context["dispatch_relative_volume"]),
                            str(bool(_allocator_dynamic_override_context(_row_or_cand)["catalyst_override_active"])).lower(),
                            str(bool(_allocator_dynamic_override_context(_row_or_cand)["pure_momentum_override_active"])).lower(),
                            str(bool(_rvol_bypass)).lower(),
                            _entry_override_reason if _rvol_entry_override_bypass else "premarket_catalyst_replay_bypass"
                            if _rvol_bypass
                            else "not_applied",
                            ",".join((_dispatch_diag_meta or {}).get("missing_fields") or []) or "none",
                        )
                    log.info(
                        "ORDER_SKIP symbol=%s reason=dynamic_relative_volume source=capital_allocator "
                        "route=%s scanner_relative_volume=%.3f entry_relative_volume=%.3f "
                        "allocator_relative_volume=%.3f execution_relative_volume=%.3f "
                        "dispatch_relative_volume=%.3f threshold_used=%.3f "
                        "catalyst_override_active=%s pure_momentum_override_active=%s "
                        "override_active=%s skip_or_allow_reason=dynamic_relative_volume",
                        sym,
                        _route_for_rvol_log,
                        float(_rvol_context["scanner_relative_volume"]),
                        float(_rvol_context["entry_relative_volume"]),
                        float(_rvol_context["allocator_relative_volume"]),
                        float(_rvol_context["execution_relative_volume"]),
                        float(_rvol_context["dispatch_relative_volume"]),
                        float(_effective_min_rel),
                        str(bool(_allocator_dynamic_override_context(_row_or_cand)["catalyst_override_active"])).lower(),
                        str(bool(_allocator_dynamic_override_context(_row_or_cand)["pure_momentum_override_active"])).lower(),
                        str(bool(_rvol_bypass)).lower(),
                    )
                    log.info(
                        "ALLOCATOR_DISPATCH_SKIPPED symbol=%s reason=dynamic_relative_volume "
                        "route=%s source=%s scanner_relative_volume=%.3f entry_relative_volume=%.3f "
                        "allocator_relative_volume=%.3f execution_relative_volume=%.3f "
                        "dispatch_relative_volume=%.3f threshold_used=%.3f "
                        "catalyst_override_active=%s pure_momentum_override_active=%s "
                        "override_active=%s skip_or_allow_reason=dynamic_relative_volume",
                        sym,
                        _route_for_rvol_log,
                        str(a.get("source") or _row_or_cand.get("source") or "n/a"),
                        float(_rvol_context["scanner_relative_volume"]),
                        float(_rvol_context["entry_relative_volume"]),
                        float(_rvol_context["allocator_relative_volume"]),
                        float(_rvol_context["execution_relative_volume"]),
                        float(_rvol_context["dispatch_relative_volume"]),
                        float(_effective_min_rel),
                        str(bool(_allocator_dynamic_override_context(_row_or_cand)["catalyst_override_active"])).lower(),
                        str(bool(_allocator_dynamic_override_context(_row_or_cand)["pure_momentum_override_active"])).lower(),
                        str(bool(_rvol_bypass)).lower(),
                    )
                    _action_blocked("dynamic_relative_volume")
                    return
                _px_vwap = _row_or_cand.get("paper_current_price")
                _vw = _row_or_cand.get("paper_session_vwap")
                if _px_vwap is not None and _vw is not None and str(_vw).strip() != "":
                    _vwap_context = _allocator_dynamic_vwap_dispatch_context(
                        _row_or_cand,
                        user_id=uid,
                        config=config if isinstance(config, Mapping) else {},
                    )
                    _vwap_route_for_log = str(
                        _row_or_cand.get("route")
                        or _row_or_cand.get("source")
                        or route
                        or "n/a"
                    )
                    _vwap_distance = _vwap_context.get("distance_pct")
                    _vwap_override_context = _allocator_dynamic_override_context(_row_or_cand)
                    _vwap_flag_text = lambda value: "n/a" if value is None else str(bool(value)).lower()
                    _vwap_skip_or_allow = (
                        "paper_dynamic_entry_vwap_approved"
                        if _vwap_context.get("paper_entry_override_active")
                        and _vwap_context.get("dispatch_below_vwap")
                        else "ok"
                    )
                    log.info(
                        "DISPATCH_DYNAMIC_VWAP_CHECK symbol=%s route=%s source=%s "
                        "dynamic_candidate=%s price=%s vwap=%s distance_from_vwap_pct=%s "
                        "threshold_pct=%.3f scanner_vwap_above=%s entry_vwap_above=%s "
                        "allocator_vwap_above=%s upstream_vwap_approved=%s entry_approved=%s "
                        "catalyst_override_active=%s pure_momentum_override_active=%s "
                        "override_active=%s dispatch_result=%s skip_or_allow_reason=%s",
                        sym,
                        _vwap_route_for_log,
                        str(a.get("source") or _row_or_cand.get("source") or "n/a"),
                        str(bool(allocation_profile_is_dynamic_candidate(_row_or_cand))).lower(),
                        "n/a"
                        if _vwap_context.get("price") is None
                        else f"{float(_vwap_context['price']):.4f}",
                        "n/a"
                        if _vwap_context.get("vwap") is None
                        else f"{float(_vwap_context['vwap']):.4f}",
                        "n/a" if _vwap_distance is None else f"{float(_vwap_distance):.3f}",
                        float(_vwap_context["threshold_pct"]),
                        _vwap_flag_text(_vwap_context["scanner_vwap_above"]),
                        _vwap_flag_text(_vwap_context["entry_vwap_above"]),
                        _vwap_flag_text(_vwap_context["allocator_vwap_above"]),
                        str(bool(_vwap_context["upstream_vwap_approved"])).lower(),
                        str(bool(_vwap_context["entry_approved"])).lower(),
                        str(bool(_vwap_override_context["catalyst_override_active"])).lower(),
                        str(bool(_vwap_override_context["pure_momentum_override_active"])).lower(),
                        str(bool(_vwap_context["paper_entry_override_active"])).lower(),
                        "allowed"
                        if not _vwap_context.get("dispatch_below_vwap")
                        or _vwap_context.get("paper_entry_override_active")
                        else "skipped",
                        _vwap_skip_or_allow,
                    )
                    try:
                        if (
                            float(_vw) > 0.0
                            and float(_px_vwap) < float(_vw) - 1e-9
                            and not _vwap_context.get("paper_entry_override_active")
                        ):
                            logger.info("[ALLOCATOR_SKIP] %s reason=dynamic vwap", sym)
                            log.info(
                                "ALLOCATOR_REJECT %s reason=dynamic vwap price=%.4f vwap=%.4f "
                                "distance_from_vwap_pct=%s threshold_pct=%.3f route=%s source=%s",
                                sym,
                                float(_px_vwap),
                                float(_vw),
                                "n/a" if _vwap_distance is None else f"{float(_vwap_distance):.3f}",
                                float(_vwap_context["threshold_pct"]),
                                _vwap_route_for_log,
                                str(a.get("source") or _row_or_cand.get("source") or "n/a"),
                            )
                            log.info(
                                "ORDER_SKIP symbol=%s reason=dynamic_vwap source=capital_allocator "
                                "route=%s price=%.4f vwap=%.4f distance_from_vwap_pct=%s "
                                "threshold_pct=%.3f upstream_vwap_approved=%s entry_approved=%s "
                                "override_active=%s skip_or_allow_reason=dynamic_vwap",
                                sym,
                                _vwap_route_for_log,
                                float(_px_vwap),
                                float(_vw),
                                "n/a" if _vwap_distance is None else f"{float(_vwap_distance):.3f}",
                                float(_vwap_context["threshold_pct"]),
                                str(bool(_vwap_context["upstream_vwap_approved"])).lower(),
                                str(bool(_vwap_context["entry_approved"])).lower(),
                                str(bool(_vwap_context["paper_entry_override_active"])).lower(),
                            )
                            log.info(
                                "ALLOCATOR_DISPATCH_SKIPPED symbol=%s reason=dynamic_vwap route=%s "
                                "source=%s price=%.4f vwap=%.4f distance_from_vwap_pct=%s "
                                "threshold_pct=%.3f upstream_vwap_approved=%s entry_approved=%s "
                                "override_active=%s skip_or_allow_reason=dynamic_vwap",
                                sym,
                                _vwap_route_for_log,
                                str(a.get("source") or _row_or_cand.get("source") or "n/a"),
                                float(_px_vwap),
                                float(_vw),
                                "n/a" if _vwap_distance is None else f"{float(_vwap_distance):.3f}",
                                float(_vwap_context["threshold_pct"]),
                                str(bool(_vwap_context["upstream_vwap_approved"])).lower(),
                                str(bool(_vwap_context["entry_approved"])).lower(),
                                str(bool(_vwap_context["paper_entry_override_active"])).lower(),
                            )
                            _action_blocked("dynamic_vwap")
                            return
                    except (TypeError, ValueError):
                        logger.info("[ALLOCATOR_SKIP] %s reason=dynamic vwap", sym)
                        log.info(
                            "ALLOCATOR_REJECT %s reason=dynamic vwap invalid_vwap_context price=%s vwap=%s route=%s source=%s",
                            sym,
                            str(_px_vwap),
                            str(_vw),
                            _vwap_route_for_log,
                            str(a.get("source") or _row_or_cand.get("source") or "n/a"),
                        )
                        _action_blocked("dynamic_vwap")
                        return
            if side == "buy" and not is_option_symbol(sym) and not _dynamic_aggressive_action:
                amt = ensure_minimum_viable_allocator_buy_notional(
                    float(amt),
                    ref_price=float(mid),
                )
            if side == "buy" and _dynamic_aggressive_action:
                _aggr_cfg = _allocator_dynamic_aggressive_cfg(config if isinstance(config, Mapping) else {})
                _aggr_cap = _allocator_float(_aggr_cfg.get("max_notional"), 500.0)
                _old_aggr_amt = float(amt or 0.0)
                amt = clean_notional(min(_old_aggr_amt, _aggr_cap), min_notional=0.0)
                log.info(
                    "DYNAMIC_AGGRESSIVE_SIZE symbol=%s notional=%.2f cap=%.2f",
                    sym,
                    float(amt),
                    float(_aggr_cap),
                )
                log.info(
                    "DYNAMIC_AGGRESSIVE_ORDER_INTENT symbol=%s notional=%.2f",
                    sym,
                    float(amt),
                )
            if side == "buy" and _weak_catalyst_dynamic_action:
                _late_cap = dynamic_spread_cap_pct(_row_or_cand)
                _late_decision = _allocator_weak_catalyst_late_entry_decision(
                    _row_or_cand,
                    config=config if isinstance(config, Mapping) else {},
                    user_id=uid,
                    spread_pct=spread_pct,
                    spread_cap_pct=_late_cap,
                )
                _late_action = str(_late_decision.get("action") or "allow")
                _late_gain = float(_late_decision.get("gain_pct") or 0.0)
                _late_vwap_ext = _late_decision.get("vwap_extension_pct")
                _late_vwap_ext_s = (
                    "n/a"
                    if _late_vwap_ext is None
                    else "%.3f" % float(_late_vwap_ext)
                )
                if _late_action == "block":
                    log.info(
                        "DYNAMIC_LATE_ENTRY_RISK symbol=%s gain_pct=%.2f first_seen_gain_pct=%s "
                        "minutes_since_first_seen=%s",
                        sym,
                        _late_gain,
                        _allocator_diag_field(_row_or_cand, "first_seen_day_gain_pct", default="n/a"),
                        _allocator_diag_field(_row_or_cand, "minutes_since_first_seen", default="n/a"),
                    )
                    log.info(
                        "WEAK_CATALYST_DYNAMIC_LATE_BLOCKED symbol=%s gain_pct=%.2f max=%.2f "
                        "reason=late_chase_protection relative_volume=%.3f vwap_above=%s "
                        "breakout=%s spread_ok=%s vwap_extension_pct=%s threshold_pct=%.3f",
                        sym,
                        _late_gain,
                        float(_late_decision.get("max_gain_pct") or 15.0),
                        float(_late_decision.get("relative_volume") or 0.0),
                        str(bool(_late_decision.get("above_vwap"))).lower(),
                        str(bool(_late_decision.get("breakout"))).lower(),
                        str(bool(_late_decision.get("spread_ok"))).lower(),
                        _late_vwap_ext_s,
                        float(_late_decision.get("max_vwap_extension_pct") or 8.0),
                    )
                    _action_blocked(
                        "weak_catalyst_dynamic_late_entry",
                        order_skip_fields={
                            "gain_pct": "%.2f" % _late_gain,
                            "max_gain_pct": "%.2f" % float(_late_decision.get("max_gain_pct") or 15.0),
                            "reason": "late_chase_protection",
                        },
                    )
                    return
                if _late_action == "reduce" and not _live_weak_catalyst_exception_experiment_action:
                    log.info(
                        "DYNAMIC_LATE_ENTRY_RISK symbol=%s gain_pct=%.2f first_seen_gain_pct=%s "
                        "minutes_since_first_seen=%s",
                        sym,
                        _late_gain,
                        _allocator_diag_field(_row_or_cand, "first_seen_day_gain_pct", default="n/a"),
                        _allocator_diag_field(_row_or_cand, "minutes_since_first_seen", default="n/a"),
                    )
                    _late_old_amt = float(amt or 0.0)
                    amt = clean_notional(
                        _late_old_amt * float(_late_decision.get("factor") or 0.5),
                        min_notional=0.0,
                    )
                    log.info(
                        "WEAK_CATALYST_DYNAMIC_LATE_REDUCED symbol=%s gain_pct=%.2f factor=%.2f "
                        "original_notional=%.2f reduced_notional=%.2f vwap_extension_pct=%s threshold_pct=%.3f",
                        sym,
                        _late_gain,
                        float(_late_decision.get("factor") or 0.5),
                        _late_old_amt,
                        float(amt),
                        _late_vwap_ext_s,
                        float(_late_decision.get("max_vwap_extension_pct") or 8.0),
                    )
                elif str(_late_decision.get("reason") or "") == "exceptional_confirmation":
                    log.info(
                        "WEAK_CATALYST_DYNAMIC_LATE_ALLOWED symbol=%s reason=exceptional_confirmation "
                        "gain_pct=%.2f relative_volume=%.3f vwap_above=%s breakout=%s vwap_extension_pct=%s",
                        sym,
                        _late_gain,
                        float(_late_decision.get("relative_volume") or 0.0),
                        str(bool(_late_decision.get("above_vwap"))).lower(),
                        str(bool(_late_decision.get("breakout"))).lower(),
                        _late_vwap_ext_s,
                    )
            if side == "buy" and _live_weak_catalyst_exception_experiment_action:
                _exp_old_amt = float(amt or 0.0)
                amt = clean_notional(
                    min(_exp_old_amt, float(_live_weak_catalyst_exception_cap or 300.0)),
                    min_notional=0.0,
                )
                log.info(
                    "LIVE_WEAK_CATALYST_EXCEPTION_CAP symbol=%s original_notional=%.2f capped_notional=%.2f cap=%.2f",
                    sym,
                    _exp_old_amt,
                    float(amt),
                    float(_live_weak_catalyst_exception_cap or 300.0),
                )
            if side == "buy" and _weak_catalyst_dynamic_action:
                _weak_old_amt = float(amt or 0.0)
                _weak_new_amt = (
                    _weak_old_amt
                    if _live_weak_catalyst_exception_experiment_action
                    else clean_notional(
                        min(_weak_old_amt * 0.5, 600.0),
                        min_notional=0.0,
                    )
                )
                if _weak_new_amt <= 0.0 or _weak_new_amt < float(_min_leg) - 1e-9:
                    log.info(
                        "DYNAMIC_WEAK_CATALYST_REJECT symbol=%s reason=size_below_min_after_reduction original_notional=%.2f reduced_notional=%.2f min_notional=%.2f",
                        sym,
                        _weak_old_amt,
                        _weak_new_amt,
                        float(_min_leg),
                    )
                    _action_blocked("dynamic_weak_catalyst_size_below_min")
                    return
                if _weak_new_amt < _weak_old_amt - 1e-9:
                    amt = _weak_new_amt
                    if _live_weak_catalyst_exceptional_action:
                        log.info(
                            "WEAK_CATALYST_DYNAMIC_REDUCED symbol=%s reason=exceptional_zero_news_live original_notional=%.2f reduced_notional=%.2f",
                            sym,
                            _weak_old_amt,
                            float(amt),
                        )
                    log.info(
                        "DYNAMIC_WEAK_CATALYST_SIZE_REDUCED symbol=%s original_notional=%.2f reduced_notional=%.2f cap_notional=600.00",
                        sym,
                        _weak_old_amt,
                        float(amt),
                    )
            if str(_expectancy_gate_decision.get("action") or "") == "reduce":
                _gate_old_amt = float(amt or 0.0)
                _gate_cap = float(_expectancy_gate_decision.get("cap") or 300.0)
                amt = clean_notional(min(_gate_old_amt, _gate_cap), min_notional=0.0)
                log.info(
                    "DYNAMIC_EXPECTANCY_GATE_REDUCE symbol=%s route=%s scope=%s sample_count=%s "
                    "expectancy_score=%s original_notional=%.2f capped_notional=%.2f cap=%.2f reason=%s",
                    sym,
                    str(_expectancy_gate_decision.get("route") or route),
                    str(_expectancy_gate_decision.get("scope") or "n/a"),
                    str(_expectancy_gate_decision.get("sample_count") or "n/a"),
                    (
                        "n/a"
                        if _expectancy_gate_decision.get("expectancy_score") is None
                        else "%.4f" % float(_expectancy_gate_decision.get("expectancy_score") or 0.0)
                    ),
                    _gate_old_amt,
                    float(amt),
                    _gate_cap,
                    str(_expectancy_gate_decision.get("reason") or "negative_expectancy"),
                )
            _price_dbg = float(mid) if mid is not None else 0.0
            _min_trade_notional_dbg = float(_min_leg)
            _qty_dbg = int(float(amt) / _price_dbg) if _price_dbg > 0 else 0
            _passes_dbg = _qty_dbg > 0 and float(amt) >= _min_trade_notional_dbg
            print(f"ALLOCATOR ORDER CHECK {sym}:")
            print("  proposed notional:", float(amt))
            print("  price:", float(_price_dbg))
            print("  qty:", int(_qty_dbg))
            print("  min_notional:", float(_min_trade_notional_dbg))
            print("  passes?", bool(_passes_dbg))
            _log_allocator_order_intent(
                sym,
                side=side,
                notional=float(amt),
                qty=int(_qty_dbg),
            )
            _record_entry_terminal_outcome(
                store=event_store,
                user_id=uid,
                symbol=sym,
                route=route,
                stage="allocator_order_intent",
                reason="order_intent",
                payload=_entry_terminal_payload(
                    _candidate_by_symbol.get(sym, {}),
                    action=side,
                    notional=amt,
                    qty=int(_qty_dbg),
                ),
                ts=dt,
            )

            try:
                if side == "buy":
                    logger.info(
                        "[ALLOCATOR_BUY] %s notional=%.2f cash=%.2f equity=%.2f",
                        sym,
                        float(amt),
                        float(cash),
                        float(account_equity),
                    )

                def _submit_attempt() -> None:
                    record_trade_attribution_order_event(
                        data_dir=dd,
                        user_id=uid,
                        timestamp=dt,
                        symbol=sym,
                        action=side,
                        route=route,
                        source=str(a.get("source") or "") or None,
                        notional=amt,
                        order_build_status="built",
                        reject_reason=None,
                        submit_attempt=True,
                        submitted=False,
                        allow_replay_attribution=allow_replay_attribution,
                    )
                    log.info(
                        "ALLOCATOR_ACTION_SUBMIT_ATTEMPT symbol=%s action=%s notional=%.2f route=%s",
                        sym,
                        side,
                        float(amt),
                        route,
                    )

                order_t = place_order(
                    broker,
                    engine,
                    {
                        "symbol": sym,
                        "notional": amt,
                        "action": side,
                        "route": route,
                        "source": a.get("source"),
                        "core_rebuild": a.get("core_rebuild"),
                    },
                    mid_price=float(mid),
                    spread_pct=float(spread_pct),
                    ignore_spread_gate=_skip_spread or _ignore_lv,
                    bid=float(quote.bid) if quote.bid is not None else None,
                    ask=float(quote.ask) if quote.ask is not None else None,
                    before_submit=_submit_attempt,
                    config=config,
                    data_dir=dd,
                    user_id=uid,
                )
            except EntryBlocked as exc:
                _action_entry_blocked(exc)
                _post_check_exit("entry_blocked_mode" if is_expected_entry_block(str(exc)) else "entry_blocked")
                return
            except Exception as exc:
                _action_exception(exc)
                _post_check_exit("exception_before_submit")
                raise
            if order_t is None:
                _build_reject_reason = getattr(engine.execution, "last_order_build_reject_reason", None)
                record_trade_attribution_order_event(
                    data_dir=dd,
                    user_id=uid,
                    timestamp=dt,
                    symbol=sym,
                    action=side,
                    route=route,
                    source=str(a.get("source") or "") or None,
                    notional=amt,
                    order_build_status="rejected",
                    reject_reason=str(_build_reject_reason or "order_build_or_execution_blocked"),
                    submit_attempt=False,
                    submitted=False,
                    allow_replay_attribution=allow_replay_attribution,
                )
                logger.info("[ALLOCATOR_SKIP] %s reason=execution blocked (place_order)", sym)
                _print_allocator_skip(sym, "size = 0", detail="execution blocked")
                log.info("ALLOCATOR_REJECT %s reason=%s", sym, "execution blocked")
                log.warning("[%s] capital_allocator: execution blocked %s %s (spread?)", uid, side, sym)
                _action_blocked("execution_blocked")
                _post_check_exit("order_build_or_execution_blocked")
                return
            _shadow_order = _allocator_order_is_shadow(order_t)
            def _explicit_meta(obj: Any, name: str, default: Any = None) -> Any:
                if name not in getattr(obj, "__dict__", {}):
                    return default
                return getattr(obj, name, default)

            def _explicit_meta_float(obj: Any, name: str, default: float) -> float:
                raw = _explicit_meta(obj, name, None)
                if raw is None:
                    return float(default)
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    return float(default)
                return value if math.isfinite(value) else float(default)

            _allocator_requested_notional = _explicit_meta_float(order_t, "_allocator_requested_notional", float(amt))
            _allocator_requested_qty = _explicit_meta(order_t, "_allocator_requested_qty", int(_qty_dbg))
            _bounded_pilot_applied = bool(_explicit_meta(order_t, "_bounded_pilot_applied", False))
            _final_submitted_qty = _explicit_meta(order_t, "_final_submitted_qty", None)
            _final_reference_price = _explicit_meta(order_t, "_final_reference_price", None)
            _final_estimated_notional = _explicit_meta_float(order_t, "_final_estimated_notional", float(amt))
            _broker_returned_qty = _explicit_meta(order_t, "qty", None)
            _broker_returned_notional = _explicit_meta(order_t, "notional", None)
            if _shadow_order:
                log.info(
                    "SHADOW_ALLOCATOR_ACTION symbol=%s action=%s notional=%.2f order_id=%s route=%s hypothetical=true broker_dispatch_attempted=false execution_allowed=false",
                    sym,
                    side,
                    float(amt),
                    _allocator_action_order_id(order_t),
                    route,
                )
                print(
                    "SHADOW_ORDER_INTENT symbol=%s side=%s notional=%.2f source=capital_allocator order_id=%s status=%s hypothetical=true broker_dispatch_attempted=false execution_allowed=false"
                    % (
                        sym,
                        side,
                        float(amt),
                        _allocator_action_order_id(order_t),
                        _allocator_order_status(order_t) or "shadow",
                    ),
                    flush=True,
                )
                log.info(
                    "SHADOW_ORDER_INTENT symbol=%s side=%s notional=%.2f source=capital_allocator order_id=%s status=%s hypothetical=true broker_dispatch_attempted=false execution_allowed=false",
                    sym,
                    side,
                    float(amt),
                    _allocator_action_order_id(order_t),
                    _allocator_order_status(order_t) or "shadow",
                )
            else:
                log.info(
                    "ALLOCATOR_ACTION_SUBMITTED symbol=%s action=%s notional=%.2f order_id=%s route=%s allocator_requested_notional=%.2f final_estimated_notional=%.2f bounded_pilot_applied=%s",
                    sym,
                    side,
                    _final_estimated_notional,
                    _allocator_action_order_id(order_t),
                    route,
                    _allocator_requested_notional,
                    _final_estimated_notional,
                    str(_bounded_pilot_applied).lower(),
                )
                print(
                    "ORDER_SUBMITTED symbol=%s side=%s notional=%.2f source=capital_allocator order_id=%s status=%s allocator_requested_notional=%.2f allocator_requested_qty=%s bounded_pilot_applied=%s final_submitted_qty=%s final_reference_price=%s final_estimated_notional=%.2f"
                    % (
                        sym,
                        side,
                        _final_estimated_notional,
                        _allocator_action_order_id(order_t),
                        _allocator_order_status(order_t) or "n/a",
                        _allocator_requested_notional,
                        str(_allocator_requested_qty),
                        str(_bounded_pilot_applied).lower(),
                        str(_final_submitted_qty if _final_submitted_qty is not None else "n/a"),
                        str(_final_reference_price if _final_reference_price is not None else "n/a"),
                        _final_estimated_notional,
                    ),
                    flush=True,
                )
                log.info(
                    "ORDER_SUBMITTED symbol=%s side=%s notional=%.2f source=capital_allocator order_id=%s status=%s allocator_requested_notional=%.2f allocator_requested_qty=%s bounded_pilot_applied=%s final_submitted_qty=%s final_reference_price=%s final_estimated_notional=%.2f broker_returned_qty=%s broker_returned_notional=%s",
                    sym,
                    side,
                    _final_estimated_notional,
                    _allocator_action_order_id(order_t),
                    _allocator_order_status(order_t) or "n/a",
                    _allocator_requested_notional,
                    str(_allocator_requested_qty),
                    str(_bounded_pilot_applied).lower(),
                    str(_final_submitted_qty if _final_submitted_qty is not None else "n/a"),
                    str(_final_reference_price if _final_reference_price is not None else "n/a"),
                    _final_estimated_notional,
                    str(_broker_returned_qty if _broker_returned_qty is not None else "n/a"),
                    str(_broker_returned_notional if _broker_returned_notional is not None else "n/a"),
                )
            log.info(
                "ORDER_STATUS symbol=%s side=%s order_id=%s status=%s source=capital_allocator route=%s",
                sym,
                side,
                _allocator_action_order_id(order_t),
                _allocator_order_status(order_t) or "n/a",
                route,
            )
            _log_dynamic_dispatch_explainability(
                sym,
                action=a,
                candidate=_candidate_by_symbol.get(sym, {}),
                route=route,
                notional=amt if _shadow_order else _final_estimated_notional,
                result="shadow_blocked" if _shadow_order else "submitted",
                reason="shadow_broker_dispatch_blocked" if _shadow_order else "submitted",
            )
            if not _shadow_order:
                _log_allocator_order_filled_if_present(
                    sym,
                    side=side,
                    order=order_t,
                    fallback_qty=int(_qty_dbg),
                )
            _log_allocator_dispatch_done(
                sym,
                result="shadow_blocked" if _shadow_order else "submitted",
                reason="shadow_broker_dispatch_blocked" if _shadow_order else "submitted",
            )
            _record_entry_terminal_outcome(
                store=event_store,
                user_id=uid,
                symbol=sym,
                route=route,
                stage="shadow_order_intent" if _shadow_order else "submitted",
                reason="shadow_broker_dispatch_blocked" if _shadow_order else "submitted",
                payload=_entry_terminal_payload(
                    _candidate_by_symbol.get(sym, {}),
                    action=side,
                    notional=amt,
                    order_id=_allocator_action_order_id(order_t),
                    hypothetical=_shadow_order,
                    broker_dispatch_attempted=not _shadow_order,
                    execution_allowed=not _shadow_order,
                ),
                ts=dt,
            )
            record_trade_attribution_order_event(
                data_dir=dd,
                user_id=uid,
                timestamp=dt,
                symbol=sym,
                action=side,
                route=route,
                source=str(a.get("source") or "") or None,
                notional=amt,
                order_build_status="built",
                reject_reason=None,
                submit_attempt=True,
                submitted=not _shadow_order,
                order_id=_allocator_action_order_id(order_t),
                qty=int(_qty_dbg) if _shadow_order else _final_submitted_qty,
                status=_allocator_order_status(order_t) or "n/a",
                filled_qty=None if _shadow_order else _allocator_order_filled_qty(order_t, fallback_qty=int(_qty_dbg)),
                filled_avg_price=None if _shadow_order else _allocator_order_filled_avg_price(order_t),
                dynamic_candidate=allocation_profile_is_dynamic_candidate(_row_or_cand),
                news_score=_row_or_cand.get("news_score"),
                event_score=_row_or_cand.get("event_score"),
                catalyst_score=_row_or_cand.get("catalyst_score"),
                catalyst_type=_row_or_cand.get("catalyst_type"),
                relative_volume=_row_or_cand.get("relative_volume", _row_or_cand.get("rel_volume")),
                gain_pct=_row_or_cand.get("gain_pct", _row_or_cand.get("day_gain_pct")),
                environment="shadow" if _shadow_order else None,
                hypothetical=True if _shadow_order else None,
                broker_dispatch_attempted=False if _shadow_order else None,
                execution_allowed=False if _shadow_order else None,
                allow_replay_attribution=allow_replay_attribution,
                allocator_requested_notional=_allocator_requested_notional,
                allocator_requested_qty=_allocator_requested_qty,
                bounded_pilot_applied=_bounded_pilot_applied,
                final_submitted_qty=_final_submitted_qty,
                final_reference_price=_final_reference_price,
                final_estimated_notional=_final_estimated_notional,
                broker_request_type=getattr(order_t, "_broker_request_type", "notional" if _final_estimated_notional else "qty"),
                broker_returned_qty=_broker_returned_qty,
                broker_returned_notional=_broker_returned_notional,
            )

            _qty_before = 0
            if side == "buy":
                _tb = load_tracked(uid, data_dir=dd)
                _qty_before = int(float((_tb.get(sym) or {}).get("qty") or 0))
            if verbose:
                print(
                    dt.strftime("%H:%M ET") if hasattr(dt, "strftime") else "",
                    "[%s] capital_allocator: %s %s notional $%.2f" % (uid, side.upper(), sym, amt),
                    flush=True,
                )
            _refresh_from_broker()
            tr = load_tracked(uid, data_dir=dd)
            tracked.clear()
            tracked.update(tr)
            current_positions.clear()
            current_positions.update(
                {
                    str(p["symbol"]).upper(): {
                        "notional": p["market_value"],
                        "stop_pct": tracked.get(str(p["symbol"]).upper(), {}).get("stop_pct", 1.5),
                    }
                    for p in positions
                }
            )

            if side == "sell" and not is_option_symbol(sym):
                _remaining_qty = 0
                for _p in positions:
                    if str(_p.get("symbol") or "").upper() == sym:
                        try:
                            _remaining_qty = int(float(_p.get("qty") or 0))
                        except (TypeError, ValueError):
                            _remaining_qty = 0
                        break
                _now_state = dt if isinstance(dt, datetime) else datetime.now(timezone.utc)
                record_sell_after_exit(
                    sym,
                    uid,
                    Path(dd),
                    _now_state,
                    "allocator_trim",
                    _remaining_qty,
                    config,
                )

            if side == "buy" and row_tl is not None:
                decision = row_tl.get("decision")
                _track_px = float(df["close"].iloc[-1]) if df is not None and not getattr(df, "empty", True) else float(mid)
                entry_price = quote.reference_mid(_track_px) if quote else _track_px
                _resolve_fill = getattr(broker, "resolve_entry_price_from_fill", None)
                if callable(_resolve_fill):
                    entry_price = float(_resolve_fill(order_t, entry_price))
                stop_pct = (
                    float(decision.entry_signal.stop_pct)
                    if decision is not None and decision.entry_signal
                    else float(getattr(engine.strategy, "stop_loss_pct", 1.5) or 1.5)
                )
                _st_base = (
                    float(decision.entry_signal.strength)
                    if decision is not None and decision.entry_signal
                    else float(row_tl.get("strength_eff", 1.0))
                )
                _st_ex = effective_signal_strength(_st_base, float(strength_jitter_max))
                _broker_qty = 0
                for _p in positions:
                    if str(_p.get("symbol") or "").upper() == sym:
                        _broker_qty = int(float(_p.get("qty") or 0))
                        break
                qty_delta = max(0, _broker_qty - _qty_before)
                if qty_delta < 1:
                    logger.info(
                        "[ALLOCATOR_SKIP] %s reason=no brokerage qty delta after buy",
                        sym,
                    )
                    return
                if _qty_before > 0:
                    merge_add_tracked(
                        sym,
                        qty_delta,
                        entry_price,
                        stop_pct=stop_pct,
                        user_id=uid,
                        data_dir=dd,
                        extras={
                            "signal_strength": _st_ex,
                            "dynamic_candidate": bool(row_tl.get("dynamic_candidate", False)),
                            "source": row_tl.get("source"),
                        },
                        et_trading_date=et_date_iso,
                    )
                else:
                    add_tracked(
                        sym,
                        qty_delta,
                        entry_price,
                        stop_pct,
                        user_id=uid,
                        data_dir=dd,
                        extras={
                            "signal_strength": _st_ex,
                            "dynamic_candidate": bool(row_tl.get("dynamic_candidate", False)),
                            "source": row_tl.get("source"),
                        },
                    )
                if cycle_risk_state is not None and _qty_before <= 0:
                    cycle_risk_state["new_stock"] = int(cycle_risk_state.get("new_stock", 0)) + 1

        # Execute ONLY allocator output (quote + module place_order + book/tracker refresh)
        n_actions = len(actions)
        for i, action in enumerate(actions):
            _action_sym_diag = str(action.get("symbol") or "").strip().upper() or "?"
            _action_side_diag = str(action.get("action") or "").strip().lower() or "?"
            _lockout_state_diag = (
                f"allow_effective={bool(allow_effective)};"
                f"allow_buys={bool(allow_allocator_buys)};"
                f"no_recycle={bool(no_recycle_block)};"
                f"risk_control={bool(rc_block)}"
            )
            log.info(
                "TRADE_CYCLE_GATE symbol=%s replay_mode=%s broker_mock=%s market_open=%s "
                "trade_cycle_allowed=%s allow_buys=%s cooldown_active=%s lockout_state=%s "
                "skip_reason=%s",
                _action_sym_diag,
                replay_mode_diag,
                bool(broker_mock_diag),
                market_open_diag,
                True,
                bool(allow_effective and allow_allocator_buys),
                False,
                _lockout_state_diag,
                "none",
            )
            if replay_mode_diag != "live":
                log.info(
                    "ENTRY_PIPELINE_STAGE symbol=%s stage=allocator_execute result=skipped "
                    "reason=offline_allocator_replay_uses_preselected_allocator_actions",
                    _action_sym_diag,
                )
                log.info(
                    "OPTION_PIPELINE_STAGE symbol=%s stage=allocator_execute result=skipped "
                    "reason=offline_allocator_replay_does_not_run_options_selector",
                    _action_sym_diag,
                )
            for attempt in (1, 2):
                try:
                    _execute_allocator_action(action)
                    break
                except EntryBlocked as e:
                    _sym_exc = str(action.get("symbol") or "?").strip().upper()
                    log.info(
                        "ALLOCATOR_ACTION_BLOCKED symbol=%s reason=%s attempt=%d",
                        _sym_exc,
                        str(e) or "ENTRY_BLOCKED",
                        attempt,
                    )
                    break
                except Exception as e:
                    _sym_exc = str(action.get("symbol") or "?").strip().upper()
                    _side_exc = str(action.get("action") or "?").strip().lower()
                    _notional_exc = float(action.get("notional", 0) or 0)
                    log.exception(
                        "ALLOCATOR_ACTION_EXCEPTION symbol=%s action=%s notional=%.2f error=%s attempt=%d",
                        _sym_exc,
                        _side_exc,
                        _notional_exc,
                        f"{type(e).__name__}: {str(e)[:200]}",
                        attempt,
                    )
                    if attempt == 1:
                        log.warning(
                            "[%s] capital_allocator: action %d/%d %s $%.0f — %s: %s; retrying once",
                            uid,
                            i + 1,
                            n_actions,
                            str(action.get("action", "")).lower(),
                            float(action.get("notional", 0) or 0),
                            type(e).__name__,
                            str(e)[:80],
                        )
                        logger.info(
                            "[ALLOCATOR_SKIP] %s reason=exception %s; retrying once",
                            str(action.get("symbol") or "?").strip().upper(),
                            type(e).__name__,
                        )
                        continue
                    log.error(
                        "[%s] capital_allocator: action %d/%d failed after retry — %s: %s; skipping",
                        uid,
                        i + 1,
                        n_actions,
                        type(e).__name__,
                        str(e)[:200],
                    )
                    _log_order_skip(
                        _sym_exc,
                        "exception_after_retry_%s" % type(e).__name__,
                    )
                    _log_allocator_dispatch_done(
                        _sym_exc,
                        result="skipped",
                        reason="exception_after_retry_%s" % type(e).__name__,
                    )
                    break
