"""
Run the trading engine in a loop until market close (no user interaction).

Checks for entry signals every N minutes during regular session; stops when
market closes or daily loss limit / safe mode is hit. Each wake: per user,
``fetch_account()`` (equity + positions + tracker sync), ``manage_positions()``
(open-row exits + tracker cleanup), ``evaluate_entries()`` (gated entry scan);
then ``sleep(exit_interval_sec)``.

Multi-user support: when ``config/users.yaml`` exists, iterates over all
configured users each cycle.  Each user has their own broker, engine,
tracker, and risk state.  Errors in one user never crash others.

Open-position exits (equity + OCC options) live in ``src/live/exits.py``;
session-open clock helper in ``src/live/session_clock.py``.

CLI: --live or --paper to override config (single-user only).
     --user <id> to run only one user (multi-user mode).
     ``-v`` / ``LOGLEVEL=DEBUG``: enables DEBUG logging (loop tick + next exit/entry cadence).
"""
import argparse
import json
import logging
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from datetime import date, datetime
from typing import Any, Callable, Iterable, Mapping, Sequence
import pytz
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from src.options_readiness import build_options_readiness, format_startup_options_config
from src.options_observability import (
    emit_options_cycle_summary,
    record_options_candidate,
    record_options_rejection,
    reset_options_cycle_stats,
)
from src.research_bars import capture_runtime_forward_bars
from src.runtime_progress import record_runtime_event

log = logging.getLogger(__name__)

_PROCESS_START_TS = time.time()
STARTUP_NO_NEW_ENTRIES_SECONDS = 300
_DYNAMIC_TIMING_STATE: dict[str, dict[str, Any]] = {}


def _log_core_skip_reason(symbol, reason, core_symbols) -> bool:
    sym_u = str(symbol or "").strip().upper()
    core = {str(s or "").strip().upper() for s in core_symbols or [] if str(s or "").strip()}
    if not sym_u or sym_u not in core:
        return False
    reason_text = str(reason or "unknown").strip() or "unknown"
    log.info("CORE_SKIP_REASON symbol=%s reason=%s", sym_u, reason_text)
    return True


def _as_pct(value, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float(default)
    if out <= 1.0:
        out *= 100.0
    return max(0.0, float(out))


def _core_rebuild_cfg(config) -> dict:
    cfg = config if isinstance(config, dict) else {}
    allocation = cfg.get("allocation") if isinstance(cfg.get("allocation"), dict) else {}
    raw = allocation.get("core_rebuild") if isinstance(allocation.get("core_rebuild"), dict) else {}
    churn_raw = raw.get("churn_guard") if isinstance(raw.get("churn_guard"), dict) else {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "underweight_threshold_pct": _as_pct(raw.get("underweight_threshold_pct", 10), 10.0),
        "max_rebuild_notional_pct": _as_pct(raw.get("max_rebuild_notional_pct", 2), 2.0),
        "max_symbols_per_cycle": max(0, int(float(raw.get("max_symbols_per_cycle", 2) or 2))),
        "require_non_bearish_regime": bool(raw.get("require_non_bearish_regime", True)),
        "require_spread_ok": bool(raw.get("require_spread_ok", True)),
        "allow_when_below_mas": bool(raw.get("allow_when_below_mas", True)),
        "allow_core_rebuild_bypass": bool(raw.get("allow_core_rebuild_bypass", False)),
        "min_cash_reserve_pct": _as_pct(raw.get("min_cash_reserve_pct", 10), 10.0),
        "min_avg_volume": max(0.0, float(raw.get("min_avg_volume", 0.0) or 0.0)),
        "allow_same_day_rebuild_after_sell": bool(raw.get("allow_same_day_rebuild_after_sell", False)),
        "churn_guard_enabled": bool(churn_raw.get("enabled", True)),
        "churn_guard_max_hold_minutes": max(
            0.0,
            float(churn_raw.get("max_hold_minutes", 30) or 30),
        ),
        "churn_guard_cooldown_minutes": max(
            0.0,
            float(churn_raw.get("cooldown_minutes", 180) or 180),
        ),
        "churn_guard_lookback_days": max(
            1,
            int(float(churn_raw.get("lookback_days", 3) or 3)),
        ),
    }


def _allow_core_rebuild_buys(config) -> bool:
    cfg = config if isinstance(config, dict) else {}
    allocation = cfg.get("allocation") if isinstance(cfg.get("allocation"), dict) else {}
    return bool(allocation.get("allow_core_rebuild_buys", False))


def _position_value_by_symbol(positions) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in positions or []:
        if isinstance(row, dict):
            sym = str(row.get("symbol") or "").strip().upper()
            value = row.get("market_value", row.get("value", 0.0))
        else:
            sym = str(getattr(row, "symbol", "") or "").strip().upper()
            value = getattr(row, "market_value", getattr(row, "value", 0.0))
        if not sym:
            continue
        try:
            out[sym] = out.get(sym, 0.0) + max(0.0, float(value or 0.0))
        except (TypeError, ValueError):
            continue
    return out


def _spread_value(quote) -> float | None:
    if quote is None:
        return None
    try:
        value = float(getattr(quote, "spread_pct", None))
    except (TypeError, ValueError):
        return None
    return value if value == value else None


def _broker_open_order_symbols(broker: Any) -> set[str]:
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


def _dynamic_timing_ms() -> int:
    return int(time.time() * 1000)


def _dynamic_timing_float(raw: Any, default: float = 0.0) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _minutes_since_market_open(now: datetime | None) -> float:
    if not isinstance(now, datetime):
        return 0.0
    et = pytz.timezone("America/New_York")
    now_et = now.astimezone(et) if now.tzinfo is not None else et.localize(now)
    open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    return max(0.0, (now_et - open_et).total_seconds() / 60.0)


def _dynamic_timing_mark(symbol: str, stage: str) -> None:
    sym = str(symbol or "").strip().upper()
    stage_clean = str(stage or "").strip()
    if not sym or not stage_clean:
        return
    ms = _dynamic_timing_ms()
    state = _DYNAMIC_TIMING_STATE.setdefault(sym, {})
    state[f"{stage_clean}_ms"] = ms
    state[f"dynamic_{stage_clean}_ms"] = ms


def _log_dynamic_latency(symbol: str) -> None:
    sym = str(symbol or "").strip().upper()
    state = _DYNAMIC_TIMING_STATE.get(sym) or {}

    def delta(start: str, end: str) -> int:
        a = state.get(f"{start}_ms")
        b = state.get(f"{end}_ms")
        if not isinstance(a, int) or not isinstance(b, int):
            return -1
        return max(0, b - a)

    scan_ms = state.get("scan_ms")
    latest = max(
        [value for key, value in state.items() if key.endswith("_ms") and isinstance(value, int)]
        or [scan_ms if isinstance(scan_ms, int) else _dynamic_timing_ms()]
    )
    total = -1 if not isinstance(scan_ms, int) else max(0, latest - scan_ms)
    log.info(
        "DYNAMIC_LATENCY symbol=%s scan_to_enqueue_ms=%d enqueue_to_eval_ms=%d "
        "eval_to_allocator_ms=%d allocator_to_dispatch_ms=%d total_ms=%d",
        sym,
        delta("scan", "enqueue"),
        delta("enqueue", "eval"),
        delta("eval", "allocator"),
        delta("allocator", "dispatch"),
        total,
    )


def _dynamic_timing_observe_scan_candidate(
    *,
    symbol: str,
    gain_pct: Any,
    price: Any,
    rel_volume: Any,
    vwap_above: Any,
    config: Mapping[str, Any] | None,
    is_live: bool,
    now: datetime | None,
    eligible: bool,
    eligible_reason: str,
) -> None:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return
    gain = _dynamic_timing_float(gain_pct)
    state = _DYNAMIC_TIMING_STATE.setdefault(sym, {})
    state["scan_ms"] = _dynamic_timing_ms()
    state["dynamic_scan_ms"] = state["scan_ms"]
    if "first_seen_ms" not in state:
        state["first_seen_ms"] = state["scan_ms"]
        state["first_seen_gain_pct"] = gain
        state["first_seen_time"] = now.isoformat() if isinstance(now, datetime) else ""
        log.info(
            "DYNAMIC_FIRST_SEEN symbol=%s gain_pct=%.2f price=%.4f rel_vol=%.3f vwap_above=%s",
            sym,
            gain,
            _dynamic_timing_float(price),
            _dynamic_timing_float(rel_volume),
            str(bool(vwap_above)).lower(),
        )
    state["max_day_gain_pct_seen"] = max(
        _dynamic_timing_float(state.get("max_day_gain_pct_seen"), gain),
        gain,
    )
    if eligible and "first_eligible_ms" not in state:
        state["first_eligible_ms"] = state["scan_ms"]
        state["first_eligible_day_gain_pct"] = gain
        state["first_eligible_time"] = now.isoformat() if isinstance(now, datetime) else ""
        log.info(
            "DYNAMIC_FIRST_ELIGIBLE symbol=%s gain_pct=%.2f reason=%s",
            sym,
            gain,
            str(eligible_reason or "scanner_selected"),
        )
    if not eligible:
        log.info(
            "DYNAMIC_EARLY_MISSED_REASON symbol=%s reason=%s",
            sym,
            str(eligible_reason or "not_eligible"),
        )
    du_cfg = (config or {}).get("dynamic_universe") if isinstance(config, Mapping) else {}
    du_cfg = du_cfg if isinstance(du_cfg, Mapping) else {}
    watch_enabled = bool(du_cfg.get("early_watch_enabled_live", True)) and bool(is_live)
    watch_min = _dynamic_timing_float(du_cfg.get("early_watch_gain_min_pct"), 8.0)
    watch_max = _dynamic_timing_float(du_cfg.get("early_watch_gain_max_pct"), 12.0)
    if watch_enabled and watch_min <= gain <= watch_max and not eligible and not state.get("early_watch_active"):
        state["early_watch_active"] = True
        log.info(
            "DYNAMIC_EARLY_WATCH symbol=%s gain_pct=%.2f reason=watch_for_alignment",
            sym,
            gain,
        )
    elif watch_enabled and state.get("early_watch_active") and eligible:
        state["early_watch_active"] = False
        log.info(
            "DYNAMIC_EARLY_WATCH_PROMOTED symbol=%s gain_pct=%.2f reason=alignment_confirmed",
            sym,
            gain,
        )
    elif watch_enabled and state.get("early_watch_active") and gain > watch_max:
        state["early_watch_active"] = False
        log.info(
            "DYNAMIC_EARLY_WATCH_EXPIRED symbol=%s reason=too_extended_or_stale",
            sym,
        )


def _dynamic_timing_metadata(symbol: str, *, now: datetime | None = None) -> dict[str, Any]:
    sym = str(symbol or "").strip().upper()
    state = _DYNAMIC_TIMING_STATE.get(sym) or {}
    first_seen_ms = state.get("first_seen_ms")
    current_ms = _dynamic_timing_ms()
    minutes_since_first = 0.0
    if isinstance(first_seen_ms, int):
        minutes_since_first = max(0.0, (current_ms - first_seen_ms) / 60000.0)
    return {
        "dynamic_scan_ms": state.get("dynamic_scan_ms") or state.get("scan_ms"),
        "dynamic_enqueue_ms": state.get("dynamic_enqueue_ms") or state.get("enqueue_ms"),
        "dynamic_entry_eval_ms": state.get("dynamic_eval_ms") or state.get("eval_ms"),
        "dynamic_allocator_ms": state.get("dynamic_allocator_ms") or state.get("allocator_ms"),
        "first_seen_time": state.get("first_seen_time"),
        "first_seen_day_gain_pct": state.get("first_seen_gain_pct"),
        "max_day_gain_pct_seen": state.get("max_day_gain_pct_seen"),
        "minutes_since_first_seen": minutes_since_first,
        "minutes_since_market_open": _minutes_since_market_open(now),
        "first_eligible_time": state.get("first_eligible_time"),
        "first_eligible_day_gain_pct": state.get("first_eligible_day_gain_pct"),
    }


def build_core_rebuild_candidates(
    *,
    config,
    core_symbols,
    dynamic_symbols,
    existing_candidates,
    positions,
    equity: float,
    cash: float,
    broker,
    open_order_symbols,
    cooldown_symbols=None,
    max_positions: int | None = None,
    regime_score=None,
    regime_condition=None,
    spread_cap_fn=None,
    user_id: str | None = None,
    data_dir: Path | str | None = None,
    now: datetime | None = None,
    entry_eval_final_symbols=None,
    entry_eval_exception_symbols=None,
) -> list[dict[str, object]]:
    cfg = _core_rebuild_cfg(config)
    core = [str(s or "").strip().upper() for s in core_symbols or [] if str(s or "").strip()]
    dynamic = {str(s or "").strip().upper() for s in dynamic_symbols or [] if str(s or "").strip()}
    core_set = set(core)
    existing_syms = {
        str((row.get("sym_u") or row.get("symbol")) if isinstance(row, dict) else getattr(row, "symbol", "")).strip().upper()
        for row in existing_candidates or []
    }
    open_orders = {str(s or "").strip().upper() for s in open_order_symbols or [] if str(s or "").strip()}
    cooldown = {str(s or "").strip().upper() for s in cooldown_symbols or [] if str(s or "").strip()}
    entry_final_required = entry_eval_final_symbols is not None
    entry_final = {
        str(s or "").strip().upper()
        for s in entry_eval_final_symbols or []
        if str(s or "").strip()
    }
    entry_exceptions = {
        str(s or "").strip().upper()
        for s in entry_eval_exception_symbols or []
        if str(s or "").strip()
    }
    eq = max(0.0, float(equity or 0.0))
    cash_value = max(0.0, float(cash or 0.0))
    targets = allocation_target_fractions(config if isinstance(config, dict) else None)
    pos_values = _position_value_by_symbol(positions)
    core_value = sum(value for sym, value in pos_values.items() if sym in core_set)
    actual_core = (core_value / eq * 100.0) if eq > 0.0 else 0.0
    target_core = float(targets.get("core", 0.0) or 0.0) * 100.0
    gap = target_core - actual_core
    added: list[dict[str, object]] = []
    churn_symbols: dict[str, dict[str, Any]] = {}
    sold_today: set[str] = set()
    now_dt = now if isinstance(now, datetime) else datetime.now()
    if bool(cfg["churn_guard_enabled"]) and user_id is not None and data_dir is not None:
        try:
            churn_symbols = recent_core_rebuild_churn_symbols(
                data_dir=data_dir,
                user_id=str(user_id),
                now=now_dt,
                max_hold_minutes=float(cfg["churn_guard_max_hold_minutes"]),
                cooldown_minutes=float(cfg["churn_guard_cooldown_minutes"]),
                lookback_days=int(cfg["churn_guard_lookback_days"]),
            )
        except Exception:
            churn_symbols = {}
            log.warning("CORE_REBUILD_CHURN_GUARD_ERROR user_id=%s", user_id, exc_info=True)
    if (
        not bool(cfg["allow_same_day_rebuild_after_sell"])
        and user_id is not None
        and data_dir is not None
    ):
        try:
            sold_today = symbols_sold_on_day(
                data_dir=data_dir,
                user_id=str(user_id),
                day=now_dt.date(),
            )
        except Exception:
            sold_today = set()
            log.warning("CORE_REBUILD_SOLD_TODAY_CHECK_ERROR user_id=%s", user_id, exc_info=True)

    def _skip(sym: str, reason: str) -> None:
        log.info("CORE_REBUILD_SKIP symbol=%s reason=%s", sym, reason)

    def _reject(sym: str, reason: str) -> None:
        log.info("CORE_REBUILD_REJECT symbol=%s reason=%s", sym, reason)

    if not cfg["enabled"]:
        log.info(
            "CORE_REBUILD_SUMMARY target_core=%.2f actual_core=%.2f added=0",
            target_core,
            actual_core,
        )
        return []
    if not bool(cfg["allow_when_below_mas"]):
        for sym in core:
            _skip(sym, "below_mas_not_allowed")
        log.info(
            "CORE_REBUILD_SUMMARY target_core=%.2f actual_core=%.2f added=0",
            target_core,
            actual_core,
        )
        return []
    if gap < float(cfg["underweight_threshold_pct"]):
        for sym in core:
            _skip(sym, "core_near_target")
        log.info(
            "CORE_REBUILD_SUMMARY target_core=%.2f actual_core=%.2f added=0",
            target_core,
            actual_core,
        )
        return []
    if bool(cfg["require_non_bearish_regime"]):
        cond = str(regime_condition or "").strip().lower()
        try:
            score_i = int(float(regime_score)) if regime_score is not None else None
        except (TypeError, ValueError):
            score_i = None
        if score_i is not None and score_i <= 1 or cond in {"bearish", "severe_bearish", "defensive"}:
            for sym in core:
                _skip(sym, "bearish_regime")
            log.info(
                "CORE_REBUILD_SUMMARY target_core=%.2f actual_core=%.2f added=0",
                target_core,
                actual_core,
            )
            return []

    reserve = eq * float(cfg["min_cash_reserve_pct"]) / 100.0
    deployable = max(0.0, cash_value - reserve)
    per_symbol_cap = eq * float(cfg["max_rebuild_notional_pct"]) / 100.0
    if deployable <= 1e-9 or per_symbol_cap <= 1e-9:
        for sym in core:
            _skip(sym, "cash_reserve")
        log.info(
            "CORE_REBUILD_SUMMARY target_core=%.2f actual_core=%.2f added=0",
            target_core,
            actual_core,
        )
        return []

    held_count = len([sym for sym, value in pos_values.items() if value > 0.0])
    max_adds = int(cfg["max_symbols_per_cycle"])
    for sym in core:
        if len(added) >= max_adds:
            _skip(sym, "max_symbols_per_cycle")
            continue
        if sym in dynamic or sym not in core_set:
            _skip(sym, "not_core")
            continue
        if sym in existing_syms:
            _skip(sym, "already_candidate")
            continue
        held_value = pos_values.get(sym, 0.0)
        if max_positions is not None and held_value <= 0.0 and held_count + len(added) >= int(max_positions):
            _skip(sym, "max_positions")
            continue
        if held_value >= per_symbol_cap - 1e-9:
            _skip(sym, "already_overweight")
            continue
        if sym in open_orders:
            _skip(sym, "open_order")
            continue
        if sym in cooldown:
            _skip(sym, "cooldown")
            continue
        if sym in sold_today:
            _skip(sym, "sold_today")
            continue
        if user_id is not None and data_dir is not None:
            try:
                recent_exit_blocked, recent_exit_reason = blocks_stock_rebuy_after_sell(
                    sym,
                    str(user_id),
                    Path(data_dir),
                    now_dt,
                    config if isinstance(config, Mapping) else None,
                )
            except Exception:
                recent_exit_blocked = False
                recent_exit_reason = None
                log.warning("CORE_REBUILD_RECENT_EXIT_CHECK_ERROR symbol=%s user_id=%s", sym, user_id, exc_info=True)
            if recent_exit_blocked:
                log.info(
                    "CORE_REBUILD_SKIP symbol=%s reason=recent_exit detail=%s",
                    sym,
                    str(recent_exit_reason or "post-sell rebuy cooldown"),
                )
                continue
        if sym in churn_symbols:
            churn = churn_symbols.get(sym, {})
            log.info(
                "CORE_REBUILD_SKIP symbol=%s reason=recent_core_rebuild_churn hold_minutes=%.2f age_minutes=%.2f cooldown_minutes=%.2f exit_reason=%s",
                sym,
                float(churn.get("hold_minutes", 0.0) or 0.0),
                float(churn.get("age_minutes", 0.0) or 0.0),
                float(churn.get("cooldown_minutes", 0.0) or 0.0),
                str(churn.get("exit_reason") or "unknown"),
            )
            continue
        if sym in entry_exceptions:
            _reject(sym, "entry_eval_exception")
            continue
        if (
            entry_final_required
            and sym not in entry_final
            and not bool(cfg["allow_core_rebuild_bypass"])
        ):
            _reject(sym, "entry_eval_not_final")
            continue
        quote = None
        if broker is not None and hasattr(broker, "get_latest_quote"):
            try:
                quote = broker.get_latest_quote(sym)
            except Exception:
                quote = None
        spread = _spread_value(quote)
        spread_cap = 3.5
        if callable(spread_cap_fn):
            try:
                spread_cap = float(spread_cap_fn(sym))
            except (TypeError, ValueError):
                spread_cap = 3.5
        if bool(cfg["require_spread_ok"]) and (spread is None or spread > spread_cap):
            _skip(sym, "spread")
            continue
        avg_volume = None
        if broker is not None and hasattr(broker, "get_avg_volume"):
            try:
                avg_volume = float(broker.get_avg_volume(sym))
            except Exception:
                avg_volume = None
        min_avg_volume = float(cfg["min_avg_volume"])
        if avg_volume is None or avg_volume < min_avg_volume:
            _skip(sym, "liquidity")
            continue
        notional = min(deployable, per_symbol_cap, max(0.0, per_symbol_cap - held_value))
        if notional <= 1e-9:
            _skip(sym, "cash_reserve")
            continue
        row = {
            "symbol": sym,
            "sym_u": sym,
            "score": 0.25,
            "strength_eff": 0.25,
            "composite_score": 0.25,
            "priority_score": 0.25,
            "source": "core_rebuild",
            "route": "core_rebuild",
            "reason": "allocation_underweight",
            "notional": float(notional),
            "candidate_notional_cap": float(notional),
            "core_rebuild": True,
            "entry_eval_final": False,
        }
        added.append(row)
        deployable -= float(notional)
        log.info(
            "CORE_REBUILD_CANDIDATE symbol=%s reason=allocation_underweight target_core=%.2f actual_core=%.2f notional=%.2f spread=%s",
            sym,
            target_core,
            actual_core,
            float(notional),
            "n/a" if spread is None else "%.3f" % float(spread),
        )
        log.info("CORE_REBUILD_SELECTED symbol=%s", sym)
    log.info(
        "CORE_REBUILD_SUMMARY target_core=%.2f actual_core=%.2f added=%d",
        target_core,
        actual_core,
        len(added),
    )
    return added


from src.app.live_context import (
    PROJECT_ROOT,
    allocator_skip_due_cooldown as _allocator_skip_due_cooldown,
    breakout_module_cfg as _breakout_module_cfg,
    calc_small_position_size as _calc_small_position_size,
    max_breakout_exposure as _max_breakout_exposure,
    normalize_broker_positions as _normalize_broker_positions,
    quote_skip_spread_check as _quote_skip_spread_check,
    session_vwap_and_ema9 as _session_vwap_and_ema9,
)
from src.app.live_logging import (
    log_entry_skip as _log_entry_skip,
    log_inverse_state_line as _log_inverse_state_line,
)
from src.config_loader import load_app_config
from src.trading_engine import TradingEngine
from src.brokers.alpaca_client import AlpacaBroker
from src.alternate_entry_signals import (
    alternate_entry_signal_strength,
    evaluate_alternate_entries,
)
from src.ai_catalyst import score_ai_catalyst
from src.strategy import _atr, trend_long_scan_ma_filter_ok
from src.universe import (
    MarketCalendar,
    SessionType,
    last_bar_volume_from_ohlcv,
    liquid_spread_relief_parse,
    symbol_in_liquid_spread_relief_set,
)
from src.position_tracker import (
    load as load_tracked,
    add as add_tracked,
    add_on_pullback_or_momentum_ok,
    last_entry_within,
    last_tracker_fill_age_minutes,
    reconcile as reconcile_tracked,
    remove as remove_tracked,
    tracked_row_has_open_long,
    update as update_tracked,
    bars_held,
    minutes_held as holding_minutes,
)
from src.strategy import EntrySignal
from src.market_regime import MarketRegimeScorer
from src.day_type_regime import compute_day_type, fetch_vix_context
from src.news_sentiment import NewsSentimentPipeline, NewsRuleEngine, volume_spike_ratio
from src.news_sentiment.newsapi_client import newsapi_key_from_config
from src.news_sentiment.rules import normalize_news_override_mode
from src.news_catalyst import (
    fetch_recent_news_catalysts,
    get_cached_news_catalyst,
    get_cached_news_score,
    load_premarket_artifacts,
    news_dynamic_starter_notional_usd,
    news_early_entry_passes,
    news_pipeline_summary,
    news_refresh_phase_for_et,
)
from src.premarket_intelligence import (
    PREMARKET_ENGINE_VERSION,
    log_premarket_startup_config,
    next_premarket_job,
    resolve_premarket_config,
    run_due_premarket_jobs,
    run_premarket_scheduler_startup_catchup,
)
from src.premarket_readiness import check_premarket_readiness, premarket_runtime_ready
from src.entry_router import (
    trend_long_options_extra_gate_ok,
    trend_long_options_extra_gate_reason,
)
from src.dynamic_universe import (
    _dynamic_scan_settings,
    classify_symbol,
    compute_dynamic_entry_signals,
    compute_intraday_momentum_score,
    dynamic_entry_guard_failure_reason,
    dynamic_entry_guard_passes,
    dynamic_entry_spread_override_cap,
    dynamic_entry_vwap_extension_pct,
    dynamic_adaptive_volume_min_relative_volume,
    dynamic_momentum_entry_passes,
    dynamic_regime_strength_threshold_multiplier,
    dynamic_reentry_cooldown_active,
    dynamic_scan_cfg_with_entry_alignment,
    dynamic_scan_candidates_to_dicts,
    entry_target_dollars_for_symbol,
    five_min_breakout_from_bars,
    high_momentum_bypass_ok,
    is_dynamic_symbol,
    pick_top_n_momentum_symbols,
    persist_allocator_candidate_bar_snapshot,
    session_vwap_from_bars,
    scan_candidates_batch,
)
from src.dynamic_entry_adaptive import (
    load_recent_dynamic_metrics as _load_recent_dynamic_metrics,
    render_adaptive_config as _render_dynamic_adaptive_config,
    resolve_adaptive_sensitivity as _resolve_dynamic_adaptive_sensitivity,
)
from src.portfolio_intelligence import portfolio_intelligence_blocks_entry
from src.user_manager import UserManager, resolve_selected_user_id
from src.loop_lock import LoopLockError, UserLoopLock, acquire_user_loop_locks
from src.safe_sell import build_safe_sell_order_request, submit_fractional_full_close
from src.loop_helpers import (
    UserLoopContext,
    alpaca_pdt_exit_hint_line,
    emergency_prepare_symbol,
    entry_scan_allowed_et,
    init_user_contexts,
    is_alpaca_pdt_trade_denial,
    log_startup_summary,
    effective_per_symbol_buy_cooldown_min,
    reduce_only_mode_exit_interval_minutes,
    resolve_dynamic_momentum_intervals,
    resolve_live_loop_intervals,
)
from src.pdt_safety import entry_opened_same_calendar_day_et
from src.strategies.exits.context import LiveExitContext
from src.strategies.exits.option_exit import manage_option_position
from src.strategies.exits.stock_exit import manage_stock_position
from src.pilot_exit_management import (
    broker_pilot_position_report,
    classification_map as pilot_exit_classification_map,
    evaluate_pilot_position,
)
from src.controlled_live_equity import emit_controlled_live_equity_startup
from src.live.inverse_flow import BearInverseContext, run_bear_inverse_flow
from src.live.options_chain import (
    broker_mode_is_paper as _broker_mode_is_paper,
    log_options_disabled_non_paper as _log_options_disabled_non_paper,
    log_options_disabled_non_paper_once as _log_options_disabled_non_paper_once,
    options_runtime_enabled as _options_runtime_enabled,
    reset_options_non_paper_log_flags as _reset_options_non_paper_log_flags,
    option_chain_for_underlying as _option_chain_for_underlying,
)
from src.live.options_paper import (
    attempt_paper_option_entry as _attempt_paper_option_entry,
    live_pilot_options_active as _live_pilot_options_active,
    paper_only_options_active as _paper_only_options_active,
)
from src.live.options_shadow import (
    attempt_shadow_option_entry as _attempt_shadow_option_entry,
    manage_shadow_option_positions as _manage_shadow_option_positions,
    shadow_live_options_active as _shadow_live_options_active,
)
from src.live.options_scanner import (
    options_scan_only_active as _options_scan_only_active,
    scan_dynamic_candidates_option_chains as _scan_dynamic_candidates_option_chains,
)
from src.options_position_manager import (
    OptionsPositionSnapshot as _OptionsPositionSnapshot,
    sync_options_positions as _sync_options_positions,
)
from src.live.session_clock import minutes_since_regular_session_open_et
from src.brokers.alpaca_client import QuoteInfo
from src.options_exit import evaluate_long_option_exit
from src.options_premium_risk import is_option_position, is_option_symbol
from src.options_selector import parse_occ_equity_option_symbol
from src.regime_entry_policy import compute_regime_entry_policy
from src.regime_neutral_probe import apply_neutral_probe_size_floor
from src.portfolio_allocation import (
    add_on_passes_signal_and_scale,
    cash_pct_of_equity,
    is_high_cash_deploy,
    scaled_buying_power_for_lane,
    min_cash_target_frac,
    parse_add_on_gate_cfg,
    parse_rebalance_sell_triggers,
    parse_pyramid_into_winners_cfg,
    portfolio_rebalance_each_cycle,
    portfolio_rebalance_tolerance_pct,
    rebalance_signal_deterioration_min_gap,
    symbol_long_position_market_value_usd,
    symbol_long_unrealized_pl_pct,
    symbol_position_has_headroom_below_cap,
)
from src.risk_limits import (
    add_on_allowed_for_daily_cap,
    add_on_allowed_for_min_minutes,
    effective_hold_for_risk,
    effective_symbol_allocation_cap_pct,
    gross_exposure_tier,
    parse_risk_emergency_cancel_all_open_orders,
    parse_risk_emergency_deleverage,
    risk_enforce_position_caps_on_hold,
    risk_max_adds_per_symbol_per_day,
    risk_max_new_positions_per_cycle,
    risk_min_minutes_between_adds,
    risk_rebalance_on_breach,
    risk_rebalance_threshold_pct,
    symbol_allocation_breach_trim_shares,
)
from src.portfolio_risk import note_live_order_for_daily_risk
from src.live_risk_protection import (
    build_live_risk_guard_state,
    record_guard_summary,
    sleeve_for_route,
)
from src.entry_quality import (
    evaluate_entry_quality,
    sector_confirmation_symbol,
)
from src.sell_logging import log_sell
from src.portfolio_replacement import (
    allowed_symbols_for_stock_orders_set,
    effective_signal_strength,
    eligible_long_stock_symbols,
    replacement_strength_ok,
    replacement_weakest_min_hold_ok,
    new_symbol_blocked_at_position_cap_only_replacement,
    trend_long_blocked_by_portfolio_cap,
    max_replacements_per_entry_cycle,
    max_portfolio_positions_from_config,
)
from src.portfolio.rebalance_cash import (
    emergency_bulk_trim_notional_usd,
    rfc_uses_largest_exposure_notional_trim,
    trim_fraction_by_gross_leverage,
)
from src.portfolio.rebalance_planner import (
    get_top_n_positions,
    parse_rebalance_free_capital_cfg,
    plan_bulk_notional_trims_for_free_capital,
    plan_emergency_deleverage_portfolio_pct_trims,
    plan_full_exit_weakest_for_gross_delever,
    plan_full_exit_weakest_when_stronger,
    plan_proportional_gross_delever_notional_trims,
    plan_weakest_gross_unwind_phase1,
    plan_weakest_trim_for_free_capital,
)
from src.portfolio.rebalance_trims import effective_allow_add_after_capital_trim
from src.allocation_config import (
    effective_ranked_signals_cap,
    low_regime_stock_entry_top_n,
    parse_allocation_config,
)
from src.alpha_config import (
    alpha_rank_candidates,
    alpha_signal_ranking_mode_override,
    effective_composite_weights,
)
from src.signal_ranking import (
    SIGNAL_RANKING_MODE_STRENGTH,
    SIGNAL_RANKING_MODE_TIER,
    apply_recent_add_rank_penalty,
    canonical_signal_ranking_mode,
    parse_recent_add_priority_cfg,
    rank_trend_long_candidate_rows,
    row_signal_priority_score,
    sector_etf_symbol_frozenset,
    symbol_signal_priority_tier,
    trend_long_composite_rank,
)
from src.exposure_gates import (
    block_new_entries_total_exposure,
    is_reduce_only_overexposed,
    parse_portfolio_exposure_gates,
    parse_strong_signal_cap_relief,
    portfolio_loop_mode,
)
from src.portfolio.allocator_config import parse_capital_allocator_cfg
from src.capital_allocator_loop import (
    build_dynamic_aggressive_scalp_candidates,
    trend_long_strength_uses_equity_allocator,
)
from src.adaptive import (
    adaptive_bump_streak,
    adaptive_effective_max_total_exposure,
    cap_relax_factor_effective,
)
from src.portfolio.allocator import (
    flush_ranked_trend_long_entry_queue,
    run_post_scan_capital_allocator,
    run_post_sell_reallocation,
)
from src.winner_allocation import (
    apply_winner_size_multiplier_to_trend_row,
    parse_winner_allocation_config,
)
from src.portfolio.rebalance import (
    rfc_effective_spread_pct,
    rfc_fallback_open_mid_from_bars,
    rfc_position_qty_floor_for_sell,
    rfc_reference_mid_for_quote,
)
from src.portfolio.replacement import preflight_replacement_gates_on_dispatch
from src.allocation_profile import allocation_target_fractions
from src.portfolio_replacement import replacement_entry_fail_reason_invites_cap_rotation
from src.portfolio.cap_pressure import (
    consider_replacement_for_sizing_reject,
    execute_cap_pressure_partial_trim,
)
from src.position_state_machine import blocks_stock_rebuy_after_sell
from src.strategies.entries.trend_long_dispatch import (
    dispatch_trend_long_after_buying_power,
)
from src.strategy_v2.hedge import trend_long_hedge_requirement_ok
from src.entry_eval_log import (
    infer_spread_position_cooldown_ok,
    log_entry_eval,
    log_execution_block,
    trend_scan_route_label,
)
from src.trade_attribution import (
    recent_core_rebuild_churn_symbols,
    record_candidate as record_trade_attribution_candidate,
    record_rejected_one_rule as record_trade_attribution_rejected_one_rule,
    symbols_sold_on_day,
)
from src.position_scoring import (
    COOLDOWN_BYPASS_MIN_SIGNAL_SCORE,
    position_dict_for_signal_score,
    score_position,
)
from src.exposure import (
    SYMBOL_SECTOR,
    compute_exposures,
    ETF_SYMBOLS,
    INVERSE_ETFS,
    THEME_MAP,
)
from src.sqlite_store import get_sqlite_event_store


def _append_capital_allocator_candidate(
    candidates: list[dict[str, Any]],
    row: dict[str, Any],
    *,
    symbol: str,
    route: str,
    reason: str,
    score: float,
    allocator_on: bool = True,
    final: bool = True,
    stage: str = "allocator_queue",
    emit_log: bool = True,
) -> None:
    """Append one final=True stock candidate to the post-scan capital allocator queue."""
    sym_clean = str(symbol or row.get("sym_u") or row.get("symbol") or "").strip().upper()
    route_clean = str(route or row.get("route") or row.get("source") or "n/a")
    reason_clean = str(reason or "ok")
    log.info(
        "ALLOCATOR_APPEND_TRACE symbol=%s route=%s reason=%s score=%.4f allocator_on=%s final=%s stage=%s",
        sym_clean or "?",
        route_clean,
        reason_clean,
        float(score or 0.0),
        str(bool(allocator_on)).lower(),
        str(bool(final)).lower(),
        str(stage or "allocator_queue"),
    )
    _persist_allocator_candidate_bars_from_row(
        row,
        symbol=sym_clean,
        route=route_clean,
        source="allocator_selected" if str(stage or "") == "allocator_queue" else "entry_eval_pass",
        stage=str(stage or "allocator_queue"),
    )
    if emit_log:
        _allocator_entry_eval_followup(
            symbol=symbol,
            route=route,
            final=final,
            reason=reason,
            allocator_on=allocator_on,
            stage=stage,
            action="enqueue",
            candidates=candidates,
            row=row,
            score=score,
        )
        return
    candidates.append(row)
    log.info(
        "ENTRY_TO_ALLOCATOR_FOLLOWUP_END symbol=%s route=%s result=append stage=%s",
        sym_clean or "?",
        route_clean,
        str(stage or "allocator_queue"),
    )


def _persist_allocator_candidate_bars_from_row(
    row: Mapping[str, Any] | None,
    *,
    symbol: str,
    route: str,
    source: str,
    stage: str,
) -> None:
    """Best-effort research capture using only bars already attached to allocator rows."""
    if not isinstance(row, Mapping):
        return
    bars = row.get("bars_1m")
    timeframe = "1Min"
    if bars is None:
        bars = row.get("df")
        timeframe = str(row.get("timeframe") or "ohlcv")
    if bars is None:
        bars = row.get("ohlcv_df")
        timeframe = str(row.get("timeframe") or "ohlcv")
    try:
        persist_allocator_candidate_bar_snapshot(
            symbol=symbol,
            user_id=str(row.get("user_id") or row.get("user") or "default"),
            bars=bars,
            timeframe=timeframe,
            project_root=PROJECT_ROOT,
            now=row.get("captured_at") if isinstance(row.get("captured_at"), datetime) else None,
            source=source,
            route=route or str(row.get("route") or row.get("source") or stage),
        )
    except Exception:
        log.debug("allocator candidate bar snapshot write failed symbol=%s stage=%s", symbol, stage, exc_info=True)


def _append_entry_eval_allocator_candidate_now(
    candidates: list[dict[str, Any]],
    row: dict[str, Any],
    *,
    symbol: str,
    route: str,
    reason: str,
    score: float,
    allocator_on: bool = True,
    final: bool = True,
    stage: str = "entry_eval",
) -> bool:
    """Immediately append a final=True entry-eval candidate to the allocator queue."""
    if not bool(allocator_on) or not bool(final):
        return False
    sym_clean = str(symbol or row.get("sym_u") or row.get("symbol") or "").strip().upper()
    route_clean = str(route or row.get("route") or row.get("source") or "n/a")
    stage_clean = str(stage or "entry_eval")
    log.info(
        "ENTRY_TO_ALLOCATOR_FOLLOWUP_START symbol=%s route=%s action=append_now stage=%s",
        sym_clean or "?",
        route_clean,
        stage_clean,
    )
    _dynamic_timing_mark(sym_clean, "allocator")
    _log_dynamic_latency(sym_clean)
    _append_capital_allocator_candidate(
        candidates,
        row,
        symbol=sym_clean,
        route=route_clean,
        reason=reason,
        score=score,
        allocator_on=allocator_on,
        final=final,
        stage=stage_clean,
        emit_log=True,
    )
    return True


def _entry_eval_allocator_score(
    *,
    route: str | None,
    final: bool,
    trend: bool | None = None,
    pullback: bool | None = None,
    momentum: bool | None = None,
    volatility: bool | None = None,
    existing_score: Any = 0.0,
) -> float:
    """Nonzero allocator score for final=True trend_long candidates."""
    try:
        score = float(existing_score if existing_score is not None else 0.0)
    except (TypeError, ValueError):
        score = 0.0
    if not bool(final):
        return max(0.0, score)
    route_clean = str(route or "").strip().lower()
    if route_clean != "trend_long":
        return max(0.0, score)
    score = max(score, 1.0)
    if trend is True:
        score += 0.15
    if pullback is True:
        score += 0.10
    if momentum is True:
        score += 0.15
    if volatility is True:
        score += 0.05
    return score


def _market_vwap_feature_result(
    broker: Any,
    *,
    symbol: str = "SPY",
    start: Any = None,
    end: Any = None,
    timeframe: str = "1Min",
    limit: int = 390,
) -> dict[str, Any]:
    """Return complete market-VWAP features, using unavailable defaults on failure."""

    result: dict[str, Any] = {
        "data_available": False,
        "market_price": None,
        "market_vwap": None,
        "distance_pct": None,
        "slope": None,
        "state": "unavailable",
        "confirmed": False,
        "score_fraction": 0.0,
    }
    try:
        bars = broker.get_bars(
            str(symbol or "SPY").strip().upper() or "SPY",
            timeframe=timeframe,
            start=start,
            end=end,
            limit=limit,
        )
    except Exception:
        return result
    if bars is None or getattr(bars, "empty", True) or "close" not in getattr(bars, "columns", []):
        return result
    vwap = session_vwap_from_bars(bars)
    try:
        px = float(bars["close"].iloc[-1])
    except Exception:
        px = None
    if px is None or vwap is None or float(vwap) <= 0.0:
        return result
    distance_pct = (float(px) - float(vwap)) / float(vwap) * 100.0
    slope = None
    try:
        if len(bars) >= 6:
            slope = float(bars["close"].iloc[-1]) - float(bars["close"].iloc[-6])
    except Exception:
        slope = None
    confirmed = bool(float(px) >= float(vwap) - 1e-9)
    recovering = bool(not confirmed and slope is not None and float(slope) > 0.0 and distance_pct >= -0.25)
    result.update(
        {
            "data_available": True,
            "market_price": float(px),
            "market_vwap": float(vwap),
            "distance_pct": float(distance_pct),
            "slope": slope,
            "state": "confirmed" if confirmed else ("recovering" if recovering else "deteriorating"),
            "confirmed": confirmed,
            "score_fraction": 1.0 if confirmed else (0.5 if recovering else 0.0),
        }
    )
    return result


def _session_feature_result(now: Any) -> dict[str, Any]:
    """Return complete regular-session timing features for entry-quality metadata."""

    result: dict[str, Any] = {
        "session_open": None,
        "session_close": None,
        "session_duration": None,
        "session_elapsed": None,
        "session_elapsed_minutes": None,
        "session_remaining_minutes": None,
        "session_available": False,
    }
    if not isinstance(now, datetime):
        return result
    try:
        et_tz = pytz.timezone("America/New_York")
        now_et = now.astimezone(et_tz) if now.tzinfo is not None else et_tz.localize(now)
        session_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        session_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        session_duration = max(0.0, (session_close - session_open).total_seconds())
        session_elapsed = max(0.0, min(session_duration, (now_et - session_open).total_seconds()))
        session_remaining = max(0.0, session_duration - session_elapsed)
    except Exception:
        return result
    result.update(
        {
            "session_open": session_open,
            "session_close": session_close,
            "session_duration": session_duration,
            "session_elapsed": session_elapsed,
            "session_elapsed_minutes": session_elapsed / 60.0,
            "session_remaining_minutes": session_remaining / 60.0,
            "session_available": True,
        }
    )
    return result


def _positive_finite_price(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or out <= 0.0:
        return None
    return out


def _latest_valid_close_from_bars(bars: Any) -> float | None:
    if bars is None or getattr(bars, "empty", True):
        return None
    try:
        columns = getattr(bars, "columns", [])
        if "close" not in columns:
            return None
        return _positive_finite_price(bars["close"].iloc[-1])
    except Exception:
        return None


def _latest_bar_timestamp_from_bars(bars: Any) -> str | None:
    if bars is None or getattr(bars, "empty", True):
        return None
    try:
        index = getattr(bars, "index", None)
        if index is not None and len(index) > 0:
            value = index[-1]
            if value is not None:
                return str(value)
    except Exception:
        pass
    try:
        columns = getattr(bars, "columns", [])
        for col in ("timestamp", "datetime", "time", "t"):
            if col in columns:
                value = bars[col].iloc[-1]
                if value is not None:
                    return str(value)
    except Exception:
        return None
    return None


def _quote_age_seconds(quote: Any) -> float | None:
    if quote is None:
        return None
    for attr in ("age_seconds", "quote_age_seconds", "age"):
        try:
            value = float(getattr(quote, attr, None))
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value) and value >= 0.0:
            return value
    return None


def _quote_reference_price(quote: Any, *, stale_quote_max_age: float | None = None) -> tuple[float | None, str | None]:
    if quote is None:
        return None, None
    try:
        if (
            stale_quote_max_age is not None
            and getattr(quote, "is_stale", None)
            and quote.is_stale(stale_quote_max_age)
        ):
            return None, "quote_stale"
    except Exception:
        return None, "quote_stale"
    bid = _positive_finite_price(getattr(quote, "bid", None))
    ask = _positive_finite_price(getattr(quote, "ask", None))
    if bid is not None and ask is not None:
        if bid > ask:
            return None, "quote_crossed"
        midpoint = _positive_finite_price(getattr(quote, "mid", None))
        if midpoint is None:
            midpoint = (bid + ask) / 2.0
        return midpoint, "quote"
    midpoint = _positive_finite_price(getattr(quote, "mid", None))
    if midpoint is not None:
        return midpoint, "quote"
    last = _positive_finite_price(
        getattr(quote, "last", getattr(quote, "last_price", getattr(quote, "price", None)))
    )
    if last is not None:
        return last, "quote"
    return None, None


def _entry_evaluation_context(
    *,
    symbol: Any,
    route: Any = None,
    quote: Any = None,
    bars: Any = None,
    signal_price: Any = None,
    scanner_price: Any = None,
    current_price: Any = None,
    now: Any = None,
    stale_quote_max_age: float | None = None,
    catalyst_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build complete shared entry-evaluation context with explicit unavailable states."""

    session = _session_feature_result(now)
    context: dict[str, Any] = {
        "symbol": str(symbol or "").strip().upper(),
        "route": str(route or "").strip().lower(),
        "reference_price": None,
        "reference_price_available": False,
        "reference_price_source": "unavailable",
        "reference_price_unavailable_reason": "no_valid_reference_price",
        "reference_price_attempted_sources": [],
        "reference_price_diagnostics": {},
        "session_open": session["session_open"],
        "session_close": session["session_close"],
        "session_duration": session["session_duration"],
        "session_elapsed": session["session_elapsed"],
        "session_elapsed_minutes": session["session_elapsed_minutes"],
        "session_remaining_minutes": session["session_remaining_minutes"],
        "session_available": bool(session["session_available"]),
        "symbol_vwap": None,
        "market_price": None,
        "market_vwap": None,
        "market_vwap_available": False,
        "market_vwap_state": "unavailable",
        "market_vwap_distance_pct": None,
        "market_vwap_slope": None,
        "bars_available": bool(bars is not None and not getattr(bars, "empty", True)),
        "catalyst_metadata": dict(catalyst_metadata or {}),
    }
    quote_price, quote_reason = _quote_reference_price(
        quote,
        stale_quote_max_age=stale_quote_max_age,
    )
    latest_close = _latest_valid_close_from_bars(bars)
    source_diagnostics = {
        "quote_age_seconds": _quote_age_seconds(quote),
        "bar_timestamp": _latest_bar_timestamp_from_bars(bars),
        "quote_unavailable_reason": quote_reason,
    }
    context["reference_price_diagnostics"] = source_diagnostics
    if quote_reason in {"quote_stale", "quote_crossed"}:
        context["reference_price_unavailable_reason"] = quote_reason
        context["reference_price_attempted_sources"] = [
            {"source": "quote", "available": False, "reason": quote_reason},
        ]
        return context
    candidates: list[tuple[str, float | None]] = [
        ("quote", quote_price),
        ("latest_intraday_close", latest_close),
        ("signal", _positive_finite_price(signal_price)),
        ("scanner", _positive_finite_price(scanner_price)),
        ("current_price", _positive_finite_price(current_price)),
    ]
    attempted: list[dict[str, Any]] = []
    for source, price in candidates:
        attempted.append(
            {
                "source": source,
                "available": price is not None,
                "value": None if price is None else float(price),
            }
        )
        if price is None:
            continue
        context.update(
            {
                "reference_price": float(price),
                "reference_price_available": True,
                "reference_price_source": source,
                "reference_price_unavailable_reason": None,
                "reference_price_attempted_sources": attempted,
            }
        )
        return context
    context["reference_price_attempted_sources"] = attempted
    if quote_reason:
        context["reference_price_unavailable_reason"] = quote_reason
    return context


def _entry_evaluation_runtime_error_reason(exc: BaseException) -> str:
    """Stable reason code for programming/runtime failures during symbol entry evaluation."""

    return "entry_evaluation_runtime_error:%s" % type(exc).__name__


def _entry_quality_metadata(decision: Any | None) -> dict[str, Any]:
    features = getattr(decision, "features", None) if decision is not None else None
    if not isinstance(features, Mapping):
        return {}
    keys = (
        "aggressive_dynamic_mode",
        "aggressive_dynamic_score",
        "aggressive_dynamic_threshold",
        "aggressive_fast_lane",
        "fast_lane_trigger",
        "bypassed_noncritical_rules",
        "score_before_override",
        "score_after_override",
        "size_multiplier",
        "price_tier",
        "aggressive_dynamic_reason",
    )
    return {key: features.get(key) for key in keys if key in features}


def _allocator_entry_eval_followup(
    *,
    symbol: str,
    route: str,
    final: bool,
    reason: str,
    allocator_on: bool,
    stage: str,
    action: str,
    candidates: list[dict[str, Any]] | None = None,
    row: dict[str, Any] | None = None,
    score: float | None = None,
    skip_reason: str | None = None,
) -> bool:
    """
    Emit the single allocator follow-up required after ENTRY_EVAL final=T.

    Returns True when a follow-up was emitted. Enqueue action also appends the provided row.
    """
    if not bool(allocator_on) or not bool(final):
        return False
    action_clean = str(action or "").strip().lower()
    route_clean = str(route or "n/a")
    reason_clean = str(reason or "ok")
    stage_clean = str(stage or "unknown")
    sym_clean = str(symbol or "").strip().upper()
    if action_clean == "enqueue":
        if candidates is None or row is None:
            action_clean = "skip"
            skip_reason = skip_reason or "missing_candidate_details"
        else:
            score_value = 0.0
            try:
                score_value = float(score if score is not None else 0.0)
            except (TypeError, ValueError):
                score_value = 0.0
            log.info(
                "ALLOCATOR_ENQUEUE symbol=%s route=%s reason=%s score=%.4f allocator_on=%s final=%s stage=%s",
                sym_clean,
                route_clean,
                reason_clean,
                score_value,
                str(bool(allocator_on)).lower(),
                str(bool(final)).lower(),
                stage_clean,
            )
            _dynamic_timing_mark(sym_clean, "allocator")
            _log_dynamic_latency(sym_clean)
            candidates.append(row)
            log.info(
                "ENTRY_TO_ALLOCATOR_FOLLOWUP_END symbol=%s route=%s result=enqueue stage=%s",
                sym_clean,
                route_clean,
                stage_clean,
            )
            return True
    skip_clean = str(skip_reason or reason_clean or "unknown")
    log.info(
        "ENTRY_TO_ALLOCATOR_FOLLOWUP_SKIPPED symbol=%s reason=%s route=%s stage=%s",
        sym_clean,
        skip_clean,
        route_clean,
        stage_clean,
    )
    log.info(
        "ALLOCATOR_APPEND_SKIPPED symbol=%s route=%s reason=%s allocator_on=%s final=%s stage=%s",
        sym_clean,
        route_clean,
        skip_clean,
        str(bool(allocator_on)).lower(),
        str(bool(final)).lower(),
        stage_clean,
    )
    log.info(
        "ENTRY_TO_ALLOCATOR_FOLLOWUP_END symbol=%s route=%s result=skipped reason=%s stage=%s",
        sym_clean,
        route_clean,
        skip_clean,
        stage_clean,
    )
    log.info(
        "ALLOCATOR_ENQUEUE_SKIP symbol=%s route=%s reason=%s allocator_on=%s final=%s stage=%s",
        sym_clean,
        route_clean,
        skip_clean,
        str(bool(allocator_on)).lower(),
        str(bool(final)).lower(),
        stage_clean,
    )
    return True


def _log_allocator_enqueue_skip_symbol(
    symbol: str,
    reason: str,
    *,
    route: str = "n/a",
    allocator_on: bool = True,
    final: bool = True,
    stage: str = "allocator_queue",
) -> None:
    _allocator_entry_eval_followup(
        symbol=symbol,
        route=route,
        final=final,
        reason=reason,
        allocator_on=allocator_on,
        stage=stage,
        action="skip",
        skip_reason=reason,
    )


def _allocator_queue_symbols(rows: Sequence[Any]) -> str:
    symbols: list[str] = []
    for row in rows or []:
        if isinstance(row, Mapping):
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


def _log_allocator_queue_summary(rows: Sequence[Any]) -> None:
    symbols = _allocator_queue_symbols(rows)
    log.info(
        "ALLOCATOR_QUEUE_SUMMARY queued=%d symbols=%s",
        len(rows or []),
        symbols,
    )
    log.info(
        "ALLOCATOR_QUEUE_CONTENTS symbols=%s",
        symbols,
    )


def _log_allocator_queue_state(
    stage: str,
    rows: Sequence[Any],
    *,
    allocator_on: bool = True,
) -> None:
    log.info(
        "ALLOCATOR_QUEUE_STATE stage=%s allocator_on=%s pending_count=%d symbols=%s",
        str(stage or "unknown"),
        str(bool(allocator_on)).lower(),
        len(rows or []),
        _allocator_queue_symbols(rows),
    )
    log.info(
        "ALLOCATOR_QUEUE_STATE phase=%s pending_count=%d allocator_on=%s symbols=%s",
        str(stage or "unknown"),
        len(rows or []),
        str(bool(allocator_on)).lower(),
        _allocator_queue_symbols(rows),
    )


def _log_allocator_drain_entry(
    rows: Sequence[Any],
    *,
    allocator_on: bool,
) -> None:
    log.info(
        "ALLOCATOR_DRAIN_ENTRY pending_count=%d allocator_on=%s symbols=%s",
        len(rows or []),
        str(bool(allocator_on)).lower(),
        _allocator_queue_symbols(rows),
    )


def _log_allocator_drain_start(rows: Sequence[Any]) -> None:
    log.info(
        "ALLOCATOR_DRAIN_START count=%d symbols=%s",
        len(rows or []),
        _allocator_queue_symbols(rows),
    )


def _log_allocator_drain_done(*, actions: int, rows: Sequence[Any]) -> None:
    log.info(
        "ALLOCATOR_DRAIN_DONE actions=%d pending_count=%d symbols=%s",
        int(actions),
        len(rows or []),
        _allocator_queue_symbols(rows),
    )


def _log_allocator_drain_exit(
    rows: Sequence[Any],
    *,
    reason: str,
) -> None:
    log.info(
        "ALLOCATOR_DRAIN_EXIT pending_count=%d reason=%s symbols=%s",
        len(rows or []),
        str(reason or "unknown"),
        _allocator_queue_symbols(rows),
    )


def _log_allocator_drain_skipped(reason: str, rows: Sequence[Any]) -> None:
    log.info(
        "ALLOCATOR_DRAIN_SKIPPED reason=%s pending_count=%d symbols=%s",
        str(reason or "unknown"),
        len(rows or []),
        _allocator_queue_symbols(rows),
    )


def _log_live_signal_scan_end(
    *,
    user_id: Any,
    pass_index: int,
    checked_count: int,
    rows: Sequence[Any],
    allocator_on: bool,
) -> None:
    log.info(
        "LIVE_SIGNAL_SCAN_END user=%s pass=%d checked=%d queued=%d allocator_on=%s symbols=%s",
        str(user_id),
        int(pass_index),
        int(checked_count),
        len(rows or []),
        str(bool(allocator_on)).lower(),
        _allocator_queue_symbols(rows),
    )


def _log_allocator_drain_fatal(
    reason: str,
    rows: Sequence[Any],
    *,
    allocator_on: bool,
    stage: str,
) -> bool:
    if not bool(allocator_on) or not rows:
        return False
    log.error(
        "ALLOCATOR_DRAIN_FATAL reason=%s pending_count=%d allocator_on=%s stage=%s symbols=%s",
        str(reason or "unknown"),
        len(rows or []),
        str(bool(allocator_on)).lower(),
        str(stage or "unknown"),
        _allocator_queue_symbols(rows),
    )
    return True


def _log_allocator_pass_skip(reason: str, rows: Sequence[Any]) -> None:
    log.info(
        "ALLOCATOR_PASS_SKIP reason=%s queued=%d",
        str(reason or "unknown"),
        len(rows or []),
    )


def _log_allocator_skip_for_rows(
    reason: str,
    rows: Sequence[Any],
    *,
    stage: str = "allocator_drain",
) -> None:
    reason_clean = str(reason or "unknown")
    stage_clean = str(stage or "allocator_drain")
    if rows:
        log.info(
            "ALLOCATOR_SKIP reason=%s pending_count=%d symbols=%s stage=%s",
            reason_clean,
            len(rows or []),
            _allocator_queue_symbols(rows),
            stage_clean,
        )
    for row in rows or []:
        if isinstance(row, Mapping):
            sym = row.get("sym_u") or row.get("symbol") or row.get("ticker")
            route = row.get("route") or row.get("source") or "allocator"
        else:
            sym = (
                getattr(row, "sym_u", None)
                or getattr(row, "symbol", None)
                or getattr(row, "ticker", None)
            )
            route = getattr(row, "route", None) or getattr(row, "source", None) or "allocator"
        sym_u = str(sym or "").strip().upper()
        if not sym_u:
            continue
        log.info(
            "ALLOCATOR_SKIP symbol=%s reason=%s route=%s stage=%s",
            sym_u,
            reason_clean,
            str(route or "allocator"),
            stage_clean,
        )


def _run_live_capital_allocator_pass(
    cap_alloc_candidates: list[dict[str, Any]],
    **kwargs: Any,
) -> float:
    _log_allocator_drain_start(cap_alloc_candidates)
    log.info(
        "ALLOCATOR_PASS_START queued=%d",
        len(cap_alloc_candidates or []),
    )
    available_cash = run_post_scan_capital_allocator(cap_alloc_candidates, **kwargs)
    _log_allocator_drain_done(
        actions=len(cap_alloc_candidates or []),
        rows=cap_alloc_candidates,
    )
    return available_cash


def _final_true_stock_candidate_can_enter_allocator(decision: Any, df: Any) -> bool:
    return (
        decision is not None
        and bool(getattr(decision, "allowed", False))
        and df is not None
        and not getattr(df, "empty", True)
    )


def _restrict_low_regime_new_stock_entries(
    rows: list[dict[str, Any]],
    *,
    config: dict[str, Any],
    sector_etfs: frozenset[str],
    ranking_mode: str,
    log_drop: Callable[[str, str], None] | None = None,
) -> list[dict[str, Any]]:
    if not rows:
        return rows
    passthrough: list[dict[str, Any]] = []
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        cap = low_regime_stock_entry_top_n(config, regime_score=row.get("entry_regime_score"))
        if cap <= 0 or bool(row.get("is_add_flow")):
            passthrough.append(row)
            continue
        grouped.setdefault(int(cap), []).append(row)
    chosen: list[dict[str, Any]] = []
    for cap, bucket in grouped.items():
        kept, dropped = rank_trend_long_candidate_rows(
            bucket,
            max_take=cap,
            sector_etfs=sector_etfs,
            ranking_mode=ranking_mode,
        )
        chosen.extend(kept)
        if log_drop is not None:
            for sym in dropped:
                log_drop(str(sym).strip().upper(), f"low regime top-{cap} filter (regime <= 3)")
    return passthrough + chosen


def _record_entry_terminal_outcome_live(
    *,
    store: Any,
    user_id: str,
    symbol: str,
    route: str | None,
    stage: str,
    reason: str,
    payload: dict[str, object] | None = None,
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
    stage_clean = str(stage or "").strip().lower()
    if stage_clean == "submitted":
        terminal_state = "order_submitted"
    elif stage_clean in {"allocator_filtered", "allocator_rejected"}:
        terminal_state = "allocator_rejected"
    elif stage_clean == "dispatch_rejected":
        terminal_state = "dispatch_rejected"
    elif stage_clean in {"allocator_order_intent", "allocator_action_created", "allocator_input"}:
        terminal_state = "entry_eval_completed"
    else:
        terminal_state = _dynamic_pipeline_terminal_state(reason)
    _log_dynamic_pipeline_terminal(
        sym,
        terminal_state=terminal_state,
        terminal_reason=reason,
        entry_eval_started=True,
        entry_eval_completed=terminal_state
        in {"entry_eval_completed", "allocator_rejected", "dispatch_rejected", "order_submitted"},
    )
    recorder = getattr(store, "record_entry_terminal_outcome", None)
    if not callable(recorder):
        return
    try:
        recorder(
            user_id=str(user_id),
            symbol=sym,
            route=route,
            stage=stage,
            reason=reason,
            payload=payload or {},
            ts=ts.isoformat() if hasattr(ts, "isoformat") else None,
        )
    except Exception:
        log.debug(
            "ENTRY_TERMINAL_OUTCOME_RECORD_FAILED symbol=%s stage=%s",
            sym,
            stage,
            exc_info=True,
        )


def _entry_allocator_symbol(row: Mapping[str, Any] | Any) -> str:
    if isinstance(row, Mapping):
        raw = row.get("sym_u") or row.get("symbol") or row.get("ticker")
    else:
        raw = (
            getattr(row, "sym_u", None)
            or getattr(row, "symbol", None)
            or getattr(row, "ticker", None)
        )
    return str(raw or "").strip().upper()


def _entry_allocator_route(row: Mapping[str, Any] | Any) -> str:
    if isinstance(row, Mapping):
        raw = row.get("route") or row.get("source") or "allocator"
    else:
        raw = getattr(row, "route", None) or getattr(row, "source", None) or "allocator"
    return str(raw or "allocator")


def _record_entry_allocator_stage_for_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    stage: str,
    reason: str,
    store: Any,
    user_id: str,
    ts: Any = None,
    symbols_out: set[str] | None = None,
    payload_extra: Mapping[str, Any] | None = None,
) -> None:
    """Persist allocator handoff stage for every queued entry-eval final row."""
    for row in rows or []:
        if not isinstance(row, Mapping) or not bool(row.get("entry_eval_final")):
            continue
        sym = _entry_allocator_symbol(row)
        if not sym:
            continue
        if symbols_out is not None:
            symbols_out.add(sym)
        payload = {
            "dynamic_candidate": bool(
                row.get("dynamic_candidate") or row.get("dynamic_symbol")
            ),
            "is_dynamic": bool(row.get("is_dynamic") or row.get("dynamic_candidate") or row.get("dynamic_symbol")),
            "entry_eval_final": True,
            "notional": row.get("notional"),
            "score": row.get("score"),
            "strength_eff": row.get("strength_eff"),
            "source": row.get("source"),
            "route": row.get("route"),
            "relative_volume": row.get("relative_volume"),
            "rel_volume": row.get("rel_volume"),
            "gain_pct": row.get("gain_pct"),
            "day_gain_pct": row.get("day_gain_pct"),
            "dynamic_score": row.get("dynamic_score"),
            "scanner_score": row.get("scanner_score"),
            "signal_score": row.get("signal_score"),
            "catalyst_score": row.get("catalyst_score"),
            "news_score": row.get("news_score"),
            "event_score": row.get("event_score"),
        }
        if payload_extra:
            payload.update(dict(payload_extra))
        _record_entry_terminal_outcome_live(
            store=store,
            user_id=str(user_id),
            symbol=sym,
            route=_entry_allocator_route(row),
            stage=stage,
            reason=reason,
            payload=payload,
            ts=ts,
        )


def _log_entry_allocator_reconcile(
    *,
    final_true: Mapping[str, Mapping[str, Any]],
    appended: set[str],
    allocator_input: set[str],
    submitted: set[str] | None = None,
    skipped: set[str] | None = None,
) -> set[str]:
    submitted_set = set(submitted or set())
    skipped_set = set(skipped or set())
    accounted = set(appended) | set(allocator_input) | submitted_set | skipped_set
    missing = sorted(sym for sym in final_true if sym and sym not in accounted)
    log.info(
        "ENTRY_ALLOCATOR_RECONCILE final_true=%d appended=%d input=%d submitted=%d missing=%d",
        len(final_true),
        len(appended),
        len(allocator_input),
        len(submitted_set),
        len(missing),
    )
    for sym in missing:
        row = final_true.get(sym) or {}
        reason = str(row.get("reason") or "no_allocator_terminal_stage")
        log.info(
            "ENTRY_ALLOCATOR_MISSING symbol=%s route=%s reason=%s",
            sym,
            str(row.get("route") or "n/a"),
            reason,
        )
    return set(missing)


def _log_allocator_dynamic_candidate(
    symbol: str,
    *,
    reason: str,
    strength_eff: float | None = None,
    source: str | None = None,
    news_score: float | None = None,
    relative_volume: float | None = None,
) -> None:
    log.info(
        "ALLOCATOR_DYNAMIC_CANDIDATES symbol=%s source=%s reason=%s strength_eff=%s news_score=%s rel_volume=%s",
        str(symbol or "").strip().upper(),
        source or "unknown",
        reason,
        "n/a" if strength_eff is None else f"{float(strength_eff):.3f}",
        "n/a" if news_score is None else f"{float(news_score):.2f}",
        "n/a" if relative_volume is None else f"{float(relative_volume):.2f}",
    )


def _log_allocator_dynamic_skipped(
    symbol: str,
    *,
    reason: str,
    spread: float | None = None,
    spread_cap: float | None = None,
    vwap_above: bool | None = None,
    strength_eff: float | None = None,
    source: str | None = None,
    news_score: float | None = None,
    relative_volume: float | None = None,
) -> None:
    log.info(
        "ALLOCATOR_DYNAMIC_SKIPPED symbol=%s source=%s reason=%s spread=%s spread_cap=%s vwap_above=%s strength_eff=%s news_score=%s rel_volume=%s",
        str(symbol or "").strip().upper(),
        source or "unknown",
        reason,
        "n/a" if spread is None else f"{float(spread):.3f}",
        "n/a" if spread_cap is None else f"{float(spread_cap):.3f}",
        "n/a" if vwap_above is None else str(bool(vwap_above)),
        "n/a" if strength_eff is None else f"{float(strength_eff):.3f}",
        "n/a" if news_score is None else f"{float(news_score):.2f}",
        "n/a" if relative_volume is None else f"{float(relative_volume):.2f}",
    )


def _log_allocator_dynamic_selected(
    symbol: str,
    *,
    reason: str,
    strength_eff: float | None = None,
    source: str | None = None,
    news_score: float | None = None,
    relative_volume: float | None = None,
) -> None:
    log.info(
        "ALLOCATOR_DYNAMIC_SELECTED symbol=%s source=%s reason=%s strength_eff=%s news_score=%s rel_volume=%s",
        str(symbol or "").strip().upper(),
        source or "unknown",
        reason,
        "n/a" if strength_eff is None else f"{float(strength_eff):.3f}",
        "n/a" if news_score is None else f"{float(news_score):.2f}",
        "n/a" if relative_volume is None else f"{float(relative_volume):.2f}",
    )
from src.sector_config import parse_sector_config
from src.options_config import (
    allow_new_entries as options_allow_new_entries,
    options_entry_environment_blocks,
    options_live_pilot_enabled,
    options_mode,
    trend_long_options_top_signals_only_passes,
)
from src.scoring_prefilter import compute_scoring_allowed_symbols, should_apply_scoring_gate
from scripts.generate_daily_report import generate_report
from src.alpaca_loop_notify import (
    HeartbeatUserSnapshot,
    heartbeat_interval_seconds,
    notify_alpaca_loop_health_alert,
    notify_alpaca_loop_heartbeat,
    notify_alpaca_loop_started,
    notify_alpaca_loop_stopped,
)
from src.health_monitor import evaluate_runtime_health, failed_health_checks
from src.daily_report_notify import deliver_daily_report
from src.daily_trading_report import collect_daily_trading_report_data
from src.catalyst_outcomes import append_catalyst_outcomes_json, record_catalyst_outcomes_from_trades
from src.news_sentiment.rules import evaluate_high_conviction_news_override
from src.trade_postmortem import write_daily_postmortem_report
from src.execution import (
    execution_rebalance_deferred_because_incoming_strong,
    parse_no_sell_within_min_of_buy,
)
from src.market.sector_strength import SECTOR_ETFS, build_sector_snapshot, get_top_sectors
from src.strategies.breakout_detector import (
    breakout_score,
    build_breakout_snapshot,
    find_breakouts,
    infer_symbol_sector,
)
from src.strategies.breakout_exit import evaluate_breakout_exit

log = logging.getLogger(__name__)


def _finite_float_or_none(value: Any) -> float | None:
    """Return a finite float or ``None`` for optional live metrics."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _dynamic_momentum_entry_effective_cfg(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """
    Live entry config for scanner-added names.

    The scanner overlays ``dynamic_momentum_override`` thresholds before selecting names.
    Keep the entry gate no stricter for the same tape fields, otherwise scan-qualified names can
    be rejected only because ``dynamic_momentum_entry`` carries older defaults.
    """
    cfg = config if isinstance(config, Mapping) else {}
    entry = cfg.get("dynamic_momentum_entry")
    override = cfg.get("dynamic_momentum_override")
    dynamic_entry = cfg.get("dynamic_entry") if isinstance(cfg.get("dynamic_entry"), Mapping) else {}
    out = dict(entry if isinstance(entry, Mapping) else {})
    adaptive = dynamic_entry.get("adaptive_sensitivity") if isinstance(dynamic_entry.get("adaptive_sensitivity"), Mapping) else None
    if adaptive is not None and "adaptive_sensitivity" not in out:
        out["adaptive_sensitivity"] = dict(adaptive)
    flexible = dynamic_entry.get("flexible_entries") if isinstance(dynamic_entry.get("flexible_entries"), Mapping) else None
    if flexible is not None and "flexible_entries" not in out:
        out["flexible_entries"] = dict(flexible)
    aggressive = dynamic_entry.get("aggressive_mode") if isinstance(dynamic_entry.get("aggressive_mode"), Mapping) else None
    if aggressive is not None and "aggressive_mode" not in out:
        out["aggressive_mode"] = dict(aggressive)
    if not isinstance(override, Mapping) or not bool(override.get("enabled")):
        return out
    for key in ("min_day_gain_pct", "min_relative_volume"):
        raw = override.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            ov_value = float(raw)
        except (TypeError, ValueError):
            continue
        current = out.get(key)
        if current is None or str(current).strip() == "":
            out[key] = ov_value
            continue
        try:
            out[key] = min(float(current), ov_value)
        except (TypeError, ValueError):
            out[key] = ov_value
    if "require_above_vwap" in override:
        out["require_above_vwap"] = bool(override.get("require_above_vwap"))
    if "allow_without_ema_pullback" in override:
        out["allow_without_ema_pullback"] = bool(override.get("allow_without_ema_pullback"))
    if "allow_without_pullback" in override:
        out["allow_without_pullback"] = bool(override.get("allow_without_pullback"))
    return out


def _dynamic_ema_bypass_enabled(
    config: dict[str, Any],
    *,
    is_dynamic_candidate: bool,
    entry_route: str | None,
) -> bool:
    dmo = config.get("dynamic_momentum_override") if isinstance(config, dict) else {}
    return (
        bool(is_dynamic_candidate)
        and str(entry_route or "") == "momentum_breakout"
        and isinstance(dmo, dict)
        and bool(dmo.get("enabled"))
        and bool(dmo.get("allow_without_ema_pullback"))
    )


def _is_close_not_above_20ema_reject(reason: Any) -> bool:
    text = str(reason or "").strip().lower()
    return (
        text.startswith("trend filter:")
        and "close" in text
        and "not above 20 ema" in text
    )


def _config_without_price_above_20ema(config: dict[str, Any]) -> dict[str, Any]:
    out = dict(config or {})
    filters = dict(out.get("filters") or {})
    filters["require_price_above_20ema"] = False
    out["filters"] = filters
    return out


def _run_entry_gates_dynamic_ema_bypass(
    engine: Any,
    *,
    config: dict[str, Any],
    is_dynamic_candidate: bool,
    entry_route: str | None,
    run_kwargs: dict[str, Any],
) -> Any:
    decision = engine.run_entry_gates(**run_kwargs)
    if (
        getattr(decision, "allowed", False)
        or not _dynamic_ema_bypass_enabled(
            config,
            is_dynamic_candidate=is_dynamic_candidate,
            entry_route=entry_route,
        )
        or not _is_close_not_above_20ema_reject(getattr(decision, "reason", None))
    ):
        return decision

    symbol = str(run_kwargs.get("symbol") or "").strip().upper()
    original_reason = str(getattr(decision, "reason", "") or "")
    log.info("DYNAMIC_EMA_BYPASS symbol=%s reason=%s", symbol, original_reason)
    original_config = getattr(engine, "config", None)
    try:
        engine.config = _config_without_price_above_20ema(
            original_config if isinstance(original_config, dict) else config
        )
        return engine.run_entry_gates(**run_kwargs)
    finally:
        if original_config is not None:
            engine.config = original_config


def _startup_no_new_entries_active(now_ts: float | None = None) -> bool:
    ts = time.time() if now_ts is None else float(now_ts)
    return (ts - _PROCESS_START_TS) < STARTUP_NO_NEW_ENTRIES_SECONDS


def _entry_startup_warmup_decision(
    *,
    process_warmup_active: bool,
    session: SessionType,
    account_loaded: bool,
    positions_loaded: bool,
    premarket_required: bool,
    premarket_loaded: bool,
    local_state_loaded: bool,
) -> tuple[bool, str]:
    """Return (warmup_active, reason) for entry-lane startup warmup."""
    if not process_warmup_active:
        return False, "expired"
    core_state_ready = bool(account_loaded) and bool(positions_loaded) and bool(local_state_loaded)
    if session != SessionType.REGULAR:
        if bool(premarket_required) and not bool(premarket_loaded):
            return True, "missing_state_or_premarket"
        return True, "process_start"
    state_ready = core_state_ready
    if state_ready:
        return False, "intraday_restart"
    return True, "missing_state_or_premarket"


def _current_git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(PROJECT_ROOT),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"


def _newsapi_startup_status(config: dict[str, Any]) -> tuple[str, bool]:
    ns_cfg = (config or {}).get("news_sentiment") or {}
    env_name = str(ns_cfg.get("newsapi_key_env") or "NEWSAPI_KEY")
    return env_name, bool(newsapi_key_from_config(config))


def _log_options_startup_config(config: Mapping[str, Any] | None, broker: Any | None = None) -> None:
    opts = (config or {}).get("options") if isinstance(config, Mapping) else {}
    opts_cfg = opts if isinstance(opts, Mapping) else {}
    def _fmt_pct(raw: Any, default: float = 0.0) -> str:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = float(default)
        pct = value * 100.0 if value <= 1.0 else value
        if abs(pct - round(pct)) < 1e-9:
            return f"{int(round(pct))}%"
        return f"{pct:.2f}%"

    live_pilot_active = bool(options_live_pilot_enabled(dict(config or {})))
    msg = (
        "OPTIONS_CONFIG enabled=%s mode=%s paper_only_active=%s live_pilot_active=%s"
        % (
            str(bool(opts_cfg.get("enabled", False))).lower(),
            str(opts_cfg.get("mode") or "unset").strip().lower() or "unset",
            str(bool(_paper_only_options_active(config))).lower(),
            str(live_pilot_active).lower(),
        )
    )
    log.info(msg)
    print(msg, flush=True)
    pilot_enabled = bool(options_live_pilot_enabled(dict(config or {})))
    pilot_lines = [
        "OPTIONS_LIVE_PILOT enabled=%s" % str(pilot_enabled).lower(),
        "OPTIONS_LIVE_PILOT exposure_limit=%s"
        % _fmt_pct(opts_cfg.get("total_exposure_limit", opts_cfg.get("max_total_options_exposure_pct")), 0.0),
        "OPTIONS_LIVE_PILOT max_positions=%s"
        % str(opts_cfg.get("max_option_positions", opts_cfg.get("max_positions", "unset"))),
        "OPTIONS_LIVE_PILOT max_contracts=%s"
        % str(opts_cfg.get("max_contracts_per_trade", opts_cfg.get("v1_max_contracts_per_trade", "unset"))),
    ]
    for line in pilot_lines:
        log.info(line)
        print(line, flush=True)
    readiness = build_options_readiness(
        dict(config or {}),
        environment="paper" if bool(getattr(broker, "paper", True)) else "live",
        user_id="live_bot",
        root=PROJECT_ROOT,
        broker=broker,
    )
    readiness_line = format_startup_options_config(readiness)
    log.info(readiness_line)
    print(readiness_line, flush=True)


def _strong_news_dynamic_persistence_map(
    premarket_artifacts: Mapping[str, Mapping[str, Any]] | None,
    dynamic_scan_accepted: Sequence[Any] | None,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for sym, data in (premarket_artifacts or {}).items():
        sym_u = str(sym or "").strip().upper()
        if not sym_u or not isinstance(data, Mapping):
            continue
        if not _premarket_artifact_has_confirmed_metadata(data):
            continue
        try:
            news_score = int(float(data.get("news_score") or 0))
        except (TypeError, ValueError):
            news_score = 0
        try:
            age_minutes = float(data.get("age_minutes") if data.get("age_minutes") is not None else data.get("catalyst_age_minutes") or 0.0)
        except (TypeError, ValueError):
            age_minutes = None
        if news_score >= 7 and age_minutes is not None and age_minutes <= 300.0:
            out[sym_u] = {
                "news_score": news_score,
                "age_minutes": age_minutes,
                "headline": str(data.get("headline") or ""),
                "catalyst_type": str(data.get("catalyst_type") or data.get("source") or ""),
            }
    for row in dynamic_scan_accepted or []:
        sym_u = str(getattr(row, "symbol", "") or "").strip().upper()
        if not sym_u:
            continue
        try:
            news_score = int(float(getattr(row, "news_score", 0) or 0))
        except (TypeError, ValueError):
            news_score = 0
        try:
            age_minutes = float(getattr(row, "catalyst_age_minutes", None))
        except (TypeError, ValueError):
            age_minutes = None
        if news_score >= 7 and age_minutes is not None and age_minutes <= 300.0 and sym_u not in out:
            out[sym_u] = {
                "news_score": news_score,
                "age_minutes": age_minutes,
                "headline": str(getattr(row, "news_headline", "") or ""),
                "catalyst_type": str(getattr(row, "catalyst_type", "") or ""),
            }
    return out


def _premarket_artifact_has_confirmed_metadata(row: Mapping[str, Any] | None) -> bool:
    """True when an artifact row contains metadata, not just a symbol membership."""
    if not isinstance(row, Mapping) or not row:
        return False

    def _float(key: str) -> float:
        try:
            value = float(row.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return value if math.isfinite(value) else 0.0

    def _int(key: str) -> int:
        try:
            return int(float(row.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    headline = str(row.get("headline") or row.get("catalyst_headline") or "").strip()
    catalyst_type = str(row.get("catalyst_type") or "").strip()
    return bool(
        _float("news_score") > 0.0
        or _float("event_score") > 0.0
        or _float("catalyst_score") > 0.0
        or _int("article_count") > 0
        or headline
        or catalyst_type
    )


def _dynamic_scan_runtime_score_maps(
    dynamic_scan_accepted: Sequence[Any] | None,
    strong_dynamic_persistent_map: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[dict[str, int], dict[str, str], dict[str, str], dict[str, float], dict[str, float]]:
    news_scores = {
        str(row.symbol).upper(): int(getattr(row, "news_score", 0) or 0)
        for row in dynamic_scan_accepted or []
    }
    headlines = {
        str(row.symbol).upper(): str(getattr(row, "news_headline", "") or "")
        for row in dynamic_scan_accepted or []
        if getattr(row, "news_headline", None)
    }
    catalyst_types = {
        str(row.symbol).upper(): str(getattr(row, "catalyst_type", "") or "")
        for row in dynamic_scan_accepted or []
        if getattr(row, "catalyst_type", None)
    }
    event_scores: dict[str, float] = {}
    catalyst_scores: dict[str, float] = {}
    for row in dynamic_scan_accepted or []:
        sym = str(getattr(row, "symbol", "") or "").strip().upper()
        if not sym:
            continue
        try:
            event_score = float(getattr(row, "event_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            event_score = 0.0
        try:
            catalyst_score = float(getattr(row, "catalyst_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            catalyst_score = 0.0
        if event_score > 0.0:
            event_scores[sym] = event_score
        if catalyst_score > 0.0:
            catalyst_scores[sym] = catalyst_score
    for sym, meta in (strong_dynamic_persistent_map or {}).items():
        sym_u = str(sym or "").strip().upper()
        if not sym_u:
            continue
        news_scores.setdefault(sym_u, int(meta.get("news_score") or 0))
        if meta.get("headline"):
            headlines.setdefault(sym_u, str(meta.get("headline") or ""))
        if meta.get("catalyst_type"):
            catalyst_types.setdefault(sym_u, str(meta.get("catalyst_type") or ""))
    return news_scores, headlines, catalyst_types, event_scores, catalyst_scores


def _premarket_injection_top_n(config: Mapping[str, Any] | None) -> int:
    cfg = config if isinstance(config, Mapping) else {}
    dyn = cfg.get("dynamic_universe") if isinstance(cfg.get("dynamic_universe"), Mapping) else {}
    pre = cfg.get("premarket_intelligence") if isinstance(cfg.get("premarket_intelligence"), Mapping) else {}
    for container, key in (
        (dyn, "premarket_candidate_injection_top_n"),
        (dyn, "premarket_injection_top_n"),
        (pre, "candidate_injection_top_n"),
        (pre, "inject_top_n"),
    ):
        raw = container.get(key) if isinstance(container, Mapping) else None
        if raw is None or str(raw).strip() == "":
            continue
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            continue
    return 5


def _premarket_artifact_rank_rows(
    *,
    project_root: Path,
    artifact_summary: Mapping[str, Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    artifacts = {
        str(sym or "").strip().upper(): dict(data)
        for sym, data in (artifact_summary or {}).items()
        if str(sym or "").strip() and isinstance(data, Mapping)
    }
    if not artifacts:
        return []

    def _num(row: Mapping[str, Any], key: str) -> float:
        try:
            return float(row.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    ranked_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    rankings_path = project_root / "data" / "premarket" / "latest_rankings.json"
    try:
        payload = json.loads(rankings_path.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    raw_rankings = payload.get("rankings") if isinstance(payload, Mapping) else None
    if isinstance(raw_rankings, list):
        for idx, raw in enumerate(raw_rankings, start=1):
            if not isinstance(raw, Mapping):
                continue
            sym = str(raw.get("symbol") or raw.get("ticker") or "").strip().upper()
            if not sym or sym in seen or sym not in artifacts:
                continue
            artifact_row = dict(artifacts[sym])
            artifact_news_score = _num(artifact_row, "news_score")
            artifact_event_score = _num(artifact_row, "event_score")
            artifact_catalyst_score = _num(artifact_row, "catalyst_score")
            ranking_score = _num(raw, "score")
            ranking_event_score = _num(raw, "event_score")
            ranking_news_score = _num(raw, "news_score")
            ranking_catalyst_score = _num(raw, "catalyst_score")
            row = dict(artifact_row)
            row.update({k: v for k, v in raw.items() if v is not None})
            row["symbol"] = sym
            row["rank"] = int(raw.get("rank") or idx)
            row["ranking_score"] = ranking_score
            row["news_score"] = max(artifact_news_score, ranking_news_score)
            row["event_score"] = max(artifact_event_score, ranking_event_score)
            if row["event_score"] <= 0.0:
                row["event_score"] = ranking_score
            row["catalyst_score"] = max(
                artifact_catalyst_score,
                ranking_catalyst_score,
                row["event_score"] / 10.0,
                row["news_score"] / 10.0,
            )
            row["score"] = max(_num(row, "score"), row["event_score"], row["news_score"], row["catalyst_score"] * 10.0)
            log.info(
                "PREMARKET_RANKING_SCORE_TRACE symbol=%s rank=%d ranking_score=%.2f "
                "artifact_news_score=%.2f artifact_event_score=%.2f artifact_catalyst_score=%.2f "
                "merged_news_score=%.2f merged_event_score=%.2f merged_catalyst_score=%.2f "
                "catalyst_type=%s source=%s",
                sym,
                int(row["rank"]),
                float(ranking_score),
                float(artifact_news_score),
                float(artifact_event_score),
                float(artifact_catalyst_score),
                float(row["news_score"]),
                float(row["event_score"]),
                float(row["catalyst_score"]),
                str(row.get("catalyst_type") or "unknown"),
                str(row.get("source") or "unknown"),
            )
            seen.add(sym)
            ranked_rows.append(row)

    for sym, data in artifacts.items():
        if sym in seen:
            continue
        row = dict(data)
        row["symbol"] = sym
        row["rank"] = len(ranked_rows) + 1
        row["ranking_score"] = _num(row, "score")
        log.info(
            "PREMARKET_RANKING_SCORE_TRACE symbol=%s rank=%d ranking_score=%.2f "
            "artifact_news_score=%.2f artifact_event_score=%.2f artifact_catalyst_score=%.2f "
            "merged_news_score=%.2f merged_event_score=%.2f merged_catalyst_score=%.2f "
            "catalyst_type=%s source=%s",
            sym,
            int(row["rank"]),
            float(row["ranking_score"]),
            _num(row, "news_score"),
            _num(row, "event_score"),
            _num(row, "catalyst_score"),
            _num(row, "news_score"),
            _num(row, "event_score"),
            _num(row, "catalyst_score"),
            str(row.get("catalyst_type") or "unknown"),
            str(row.get("source") or "unknown"),
        )
        ranked_rows.append(row)

    def _score(row: Mapping[str, Any], key: str) -> float:
        try:
            return float(row.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    ranked_rows.sort(
        key=lambda row: (
            int(row.get("rank") or 999_999),
            -max(_score(row, "catalyst_score") * 10.0, _score(row, "event_score"), _score(row, "news_score"), _score(row, "score")),
            str(row.get("symbol") or ""),
        )
    )
    return ranked_rows


def _inject_premarket_ranked_candidates(
    *,
    config: Mapping[str, Any] | None,
    project_root: Path,
    now: datetime,
    artifact_summary: Mapping[str, Mapping[str, Any]] | None,
    existing_symbols: Iterable[str],
    dynamic_symbols: Sequence[str],
    paused_symbols: set[str] | frozenset[str],
) -> list[dict[str, Any]]:
    top_n = _premarket_injection_top_n(config)
    existing = {str(sym or "").strip().upper() for sym in existing_symbols if str(sym or "").strip()}
    existing_dynamic = {str(sym or "").strip().upper() for sym in dynamic_symbols if str(sym or "").strip()}
    paused = {str(sym or "").strip().upper() for sym in paused_symbols if str(sym or "").strip()}
    if top_n <= 0:
        log.info("PREMARKET_CANDIDATE_INJECTION_SUMMARY injected=0 skipped_existing=0 skipped_stale=0")
        return []

    readiness = check_premarket_readiness(project_root, now=now)
    if not readiness.fresh:
        skipped = max(int(readiness.catalyst_ranked_symbols), len(artifact_summary or {}))
        log.info(
            "PREMARKET_CANDIDATE_INJECTION_SUMMARY injected=0 skipped_existing=0 skipped_stale=%d",
            skipped,
        )
        return []

    injected: list[dict[str, Any]] = []
    skipped_existing = 0
    for raw in _premarket_artifact_rank_rows(project_root=project_root, artifact_summary=artifact_summary):
        if len(injected) >= top_n:
            break
        sym = str(raw.get("symbol") or "").strip().upper()
        if not sym or sym in paused:
            continue
        if sym in existing_dynamic:
            skipped_existing += 1
            continue
        try:
            news_score = float(raw.get("news_score", 0.0) or raw.get("score", 0.0) or 0.0)
        except (TypeError, ValueError):
            news_score = 0.0
        try:
            event_score = float(raw.get("event_score", 0.0) or raw.get("score", 0.0) or 0.0)
        except (TypeError, ValueError):
            event_score = 0.0
        try:
            catalyst_score = float(raw.get("catalyst_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            catalyst_score = 0.0
        try:
            article_count = int(float(raw.get("article_count", 0) or 0))
        except (TypeError, ValueError):
            article_count = 0
        headline = str(raw.get("headline") or raw.get("catalyst_headline") or "")
        catalyst_type = str(raw.get("catalyst_type") or raw.get("rank_source") or raw.get("source") or "")
        if not _premarket_artifact_has_confirmed_metadata(raw):
            log.info(
                "CATALYST_METADATA_MISSING symbol=%s reason=missing_or_zero_metadata",
                sym,
            )
        if catalyst_score <= 0.0:
            catalyst_score = max(news_score, event_score) / 10.0
        if max(news_score, event_score, catalyst_score) <= 0.0:
            log.info(
                "PREMARKET_CANDIDATE_SCORE_TRACE symbol=%s action=skip reason=zero_scores "
                "ranking_score=%.2f news_score=%.2f event_score=%.2f catalyst_score=%.2f "
                "source=%s catalyst_type=%s",
                sym,
                float(raw.get("ranking_score", raw.get("score", 0.0)) or 0.0),
                float(news_score),
                float(event_score),
                float(catalyst_score),
                str(raw.get("source") or raw.get("artifact_kind") or "premarket"),
                str(raw.get("catalyst_type") or raw.get("rank_source") or raw.get("source") or ""),
            )
            continue
        rank = int(raw.get("rank") or (len(injected) + 1))
        row = {
            "symbol": sym,
            "sym_u": sym,
            "rank": rank,
            "score": float(raw.get("score", max(news_score, event_score, catalyst_score * 10.0)) or 0.0),
            "news_score": news_score,
            "event_score": event_score,
            "catalyst_score": catalyst_score,
            "article_count": article_count,
            "headline": headline,
            "catalyst_headline": headline,
            "catalyst_type": catalyst_type,
            "source": str(raw.get("source") or raw.get("artifact_kind") or "premarket"),
            "catalyst_age_minutes": raw.get("age_minutes") if raw.get("age_minutes") is not None else raw.get("catalyst_age_minutes"),
            "dynamic_candidate": True,
            "dynamic_symbol": True,
            "premarket_injected": True,
            "already_in_universe": sym in existing,
        }
        injected.append(row)
        log.info(
            "PREMARKET_CANDIDATE_SCORE_TRACE symbol=%s action=inject rank=%d ranking_score=%.2f "
            "final_news_score=%.2f final_event_score=%.2f final_catalyst_score=%.2f "
            "already_in_universe=%s source=%s catalyst_type=%s headline=%s",
            sym,
            rank,
            float(raw.get("ranking_score", raw.get("score", 0.0)) or 0.0),
            float(news_score),
            float(event_score),
            float(catalyst_score),
            str(bool(row["already_in_universe"])).lower(),
            row["source"] or "unknown",
            row["catalyst_type"] or "unknown",
            str(row["headline"] or "")[:180],
        )
        log.info(
            "PREMARKET_CANDIDATE_INJECTED symbol=%s rank=%d score=%.2f news_score=%.2f catalyst_score=%.2f event_score=%.2f source=%s catalyst_type=%s",
            sym,
            rank,
            float(row["score"]),
            float(news_score),
            float(catalyst_score),
            float(event_score),
            row["source"] or "unknown",
            row["catalyst_type"] or "unknown",
        )
    log.info(
        "PREMARKET_CANDIDATE_INJECTION_SUMMARY injected=%d skipped_existing=%d skipped_stale=0",
        len(injected),
        skipped_existing,
    )
    return injected


def _dynamic_fastlane_window_active(now: datetime) -> bool:
    """True for the market-open dynamic fast lane: 09:30 ET <= now < 10:00 ET."""
    ny = pytz.timezone("America/New_York")
    local = now.astimezone(ny) if now.tzinfo is not None else ny.localize(now)
    mins = local.hour * 60 + local.minute + (local.second / 60.0)
    return (9 * 60 + 30) <= mins < (10 * 60)


def _market_open_accelerated_window_active(now: datetime) -> bool:
    """True during the 09:30-10:00 ET high-cadence live restart window."""
    return _dynamic_fastlane_window_active(now)


def _dynamic_scan_open_protected(
    now: datetime,
    *,
    enabled: bool,
    configured_delay_minutes: float,
) -> bool:
    """Open protection based on wall-clock market-open time."""
    if not enabled or configured_delay_minutes <= 0:
        return False
    return minutes_since_regular_session_open_et(now) < float(configured_delay_minutes)


def _market_session_entry_cadence_seconds(
    now: datetime,
    *,
    default_dynamic_seconds: float,
    default_core_seconds: float,
) -> tuple[float, float]:
    """Return dynamic/core entry cadences for the ET regular session."""
    ny = pytz.timezone("America/New_York")
    local = now.astimezone(ny) if now.tzinfo is not None else ny.localize(now)
    mins = local.hour * 60 + local.minute + (local.second / 60.0)
    if (9 * 60 + 30) <= mins < (10 * 60):
        return 60.0, 180.0
    if (10 * 60) <= mins < (15 * 60 + 30):
        return 180.0, 600.0
    if (15 * 60 + 30) <= mins < (16 * 60):
        return 120.0, 300.0
    return float(default_dynamic_seconds), float(default_core_seconds)


def _parse_live_loop_hhmm(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except (TypeError, ValueError):
        return None
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return float(hour * 60 + minute)


def _live_loop_fast_poll_sleep_seconds(
    config: Mapping[str, Any] | None,
    now: datetime,
    *,
    default_sleep_seconds: float,
) -> tuple[float, str, str | None]:
    """Return loop sleep seconds plus reason/window for configured ET fast-poll windows."""
    cfg = config if isinstance(config, Mapping) else {}
    ll_cfg = cfg.get("live_loop")
    windows = (ll_cfg or {}).get("fast_poll_windows") if isinstance(ll_cfg, Mapping) else None
    ny = pytz.timezone("America/New_York")
    local = now.astimezone(ny) if now.tzinfo is not None else ny.localize(now)
    mins = local.hour * 60 + local.minute + (local.second / 60.0)
    for row in windows if isinstance(windows, list) else []:
        if not isinstance(row, Mapping):
            continue
        start_min = _parse_live_loop_hhmm(row.get("start"))
        end_min = _parse_live_loop_hhmm(row.get("end"))
        if start_min is None or end_min is None:
            continue
        in_window = (
            start_min <= mins < end_min
            if end_min > start_min
            else mins >= start_min or mins < end_min
        )
        if not in_window:
            continue
        try:
            sleep_seconds = float(row.get("sleep_seconds"))
        except (TypeError, ValueError):
            continue
        if sleep_seconds <= 0:
            continue
        return sleep_seconds, "fast_poll_window", f"{row.get('start')}-{row.get('end')}"
    return float(default_sleep_seconds), "normal_poll", None


def _live_loop_sleep_config_seconds(
    config: Mapping[str, Any] | None,
    key: str,
    fallback: float,
) -> float:
    cfg = config if isinstance(config, Mapping) else {}
    ll_cfg = cfg.get("live_loop")
    raw = (ll_cfg or {}).get(key) if isinstance(ll_cfg, Mapping) else None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(fallback)
    return value if value > 0 else float(fallback)


def _live_loop_options_mode_label(config: Mapping[str, Any] | None) -> str:
    opts = (config or {}).get("options") if isinstance(config, Mapping) else None
    if not isinstance(opts, Mapping) or not bool(opts.get("enabled")):
        return "disabled"
    mode = str(opts.get("mode") or "").strip().lower()
    return "paper_only" if mode == "paper_only" else "live"


def _live_loop_poll_sleep_seconds(
    config: Mapping[str, Any] | None,
    *,
    mode: str,
    options_mode: str,
    default_sleep_seconds: float,
) -> tuple[float, str]:
    """Return configured live-loop sleep seconds and log reason for broker mode."""
    mode_l = str(mode or "live").strip().lower()
    options_mode_l = str(options_mode or "disabled").strip().lower() or "disabled"
    if mode_l == "paper" and options_mode_l == "paper_only":
        return (
            _live_loop_sleep_config_seconds(config, "loop_sleep_seconds_paper_options", 5.0),
            "paper_options_fast",
        )
    if mode_l == "paper":
        return (
            _live_loop_sleep_config_seconds(config, "loop_sleep_seconds_paper", 15.0),
            "paper_fast",
        )
    return (
        _live_loop_sleep_config_seconds(config, "loop_sleep_seconds_live", default_sleep_seconds),
        "normal",
    )


def _live_loop_poll_context(user_contexts: Sequence[Any]) -> tuple[str, str]:
    """Return effective loop polling mode/options label for all active users."""
    any_paper = False
    any_paper_options = False
    any_live_options = False
    for ctx in user_contexts or []:
        cfg = getattr(ctx, "config", None)
        paper = bool(getattr(ctx, "paper", False))
        options_mode = _live_loop_options_mode_label(cfg if isinstance(cfg, Mapping) else None)
        if paper:
            any_paper = True
            if options_mode == "paper_only":
                any_paper_options = True
        elif options_mode != "disabled":
            any_live_options = True
    if any_paper_options:
        return "paper", "paper_only"
    if any_paper:
        return "paper", "disabled"
    return "live", "live" if any_live_options else "disabled"


def _dynamic_daily_history_requirement(
    config: Mapping[str, Any] | None,
    *,
    symbol: str,
    ma_slow_period: int,
    min_history_bars: int,
    is_dynamic_candidate: bool,
    broker_is_paper: bool,
    candidate_type: str | None = None,
) -> tuple[int, int, bool]:
    """
    Daily-bar requirement for the live entry guard.

    Returns ``(need, default_need, experiment_active)``. Generic dynamic
    candidates use ``dynamic_universe.min_history_bars`` when configured.
    Live DYNAMIC_ONLY scanner candidates may use ``live_min_history_bars``;
    core/scoring symbols still use the default slow-history requirement.
    The paper experiment can still explicitly lower paper-only history for
    controlled research.
    """
    sym_u = str(symbol or "").strip().upper()
    try:
        min_hist = int(min_history_bars)
    except (TypeError, ValueError):
        min_hist = 0
    try:
        ma_slow = int(ma_slow_period)
    except (TypeError, ValueError):
        ma_slow = 200
    ma_slow = max(1, ma_slow)
    if sym_u == "SQQQ":
        return max(1, min_hist), max(1, min_hist), False

    default_need = max(200, ma_slow)
    if not is_dynamic_candidate:
        return default_need, default_need, False

    dyn_cfg = (config or {}).get("dynamic_universe")
    dyn_cfg = dyn_cfg if isinstance(dyn_cfg, Mapping) else {}
    try:
        dynamic_need = int(dyn_cfg.get("min_history_bars", 180) or 180)
    except (TypeError, ValueError):
        dynamic_need = 180
    dynamic_need = max(1, dynamic_need)
    candidate_type_norm = str(candidate_type or "").strip().upper()
    if not broker_is_paper:
        if candidate_type_norm == "DYNAMIC_ONLY":
            try:
                live_dynamic_need = int(dyn_cfg.get("live_min_history_bars", dynamic_need) or dynamic_need)
            except (TypeError, ValueError):
                live_dynamic_need = dynamic_need
            return max(1, live_dynamic_need), default_need, False
        return dynamic_need, default_need, False

    exp_cfg = dyn_cfg.get("paper_min_history_bars_experiment")
    exp_cfg = exp_cfg if isinstance(exp_cfg, Mapping) else {}
    if not bool(exp_cfg.get("enabled", False)):
        return dynamic_need, default_need, False

    try:
        configured_min = int(exp_cfg.get("min_bars", 50) or 50)
    except (TypeError, ValueError):
        configured_min = 50
    configured_min = max(1, configured_min)
    return configured_min, default_need, True


def _log_live_loop_sleep(seconds: float, *, mode: str, options_mode: str, reason: str) -> None:
    log.info(
        "LIVE_LOOP_SLEEP seconds=%d mode=%s options_mode=%s reason=%s",
        int(seconds),
        str(mode or "live"),
        str(options_mode or "disabled"),
        str(reason or "normal"),
    )


def _entry_scan_order_for_session(
    symbols: Sequence[Any],
    *,
    dynamic_symbols: Sequence[Any],
    early_session: bool,
) -> list[Any]:
    """During the open, evaluate dynamic/news symbols before ETFs/core symbols."""
    ordered = list(symbols or [])
    if not early_session:
        return ordered
    dynamic_set = {
        str(sym or "").strip().upper()
        for sym in dynamic_symbols or []
        if str(sym or "").strip()
    }
    if not dynamic_set:
        return ordered
    dyn_rows = [sym for sym in ordered if str(sym or "").strip().upper() in dynamic_set]
    non_dyn_rows = [sym for sym in ordered if str(sym or "").strip().upper() not in dynamic_set]
    etf_rows = [sym for sym in non_dyn_rows if str(sym or "").strip().upper() in ETF_SYMBOLS]
    other_rows = [sym for sym in non_dyn_rows if str(sym or "").strip().upper() not in ETF_SYMBOLS]
    return list(dict.fromkeys(dyn_rows + other_rows + etf_rows))


def _log_dynamic_selected_entry_trace(
    symbol: str,
    *,
    in_universe: bool,
    will_evaluate: bool,
    reason: str,
    in_dynamic_set: bool | None = None,
    in_effective_universe: bool | None = None,
    in_scoring_top_n: bool | None = None,
    scoring_allowed: bool | None = None,
    dynamic_bypass_applied: bool | None = None,
    route_candidate: str | None = None,
    selected_count: int | None = None,
    rank: int | None = None,
) -> None:
    sym_u = str(symbol or "").strip().upper() or "?"
    reason_clean = str(reason or "unknown")
    log.info(
        "DYNAMIC_SELECTED_ENTRY_TRACE symbol=%s in_universe=%s will_evaluate=%s reason=%s "
        "in_dynamic_set=%s in_effective_universe=%s in_scoring_top_n=%s scoring_allowed=%s "
        "dynamic_bypass_applied=%s route_candidate=%s selected_count=%s rank=%s",
        sym_u,
        str(bool(in_universe)).lower(),
        str(bool(will_evaluate)).lower(),
        reason_clean,
        "unknown" if in_dynamic_set is None else str(bool(in_dynamic_set)).lower(),
        "unknown" if in_effective_universe is None else str(bool(in_effective_universe)).lower(),
        "unknown" if in_scoring_top_n is None else str(bool(in_scoring_top_n)).lower(),
        "unknown" if scoring_allowed is None else str(bool(scoring_allowed)).lower(),
        "unknown" if dynamic_bypass_applied is None else str(bool(dynamic_bypass_applied)).lower(),
        str(route_candidate or "unknown"),
        "unknown" if selected_count is None else str(int(selected_count)),
        "unknown" if rank is None else str(int(rank)),
    )
    if not bool(will_evaluate):
        log.info(
            "DYNAMIC_SELECTED_ENTRY_SKIPPED symbol=%s reason=%s",
            sym_u,
            reason_clean,
        )


def _log_dynamic_selected_entry_drop(
    symbol: str,
    *,
    stage: str,
    reason: str,
    detail: str | None = None,
) -> None:
    sym_u = str(symbol or "").strip().upper() or "?"
    stage_clean = str(stage or "pre_entry").strip() or "pre_entry"
    reason_clean = str(reason or "unknown").strip() or "unknown"
    detail_clean = str(detail or "").strip()
    log.info(
        "DYNAMIC_SELECTED_ENTRY_DROP symbol=%s stage=%s reason=%s detail=%s",
        sym_u,
        stage_clean,
        reason_clean,
        detail_clean or "n/a",
    )


def _dynamic_pipeline_terminal_state(reason: str) -> str:
    clean = str(reason or "").strip().lower().replace(" ", "_").replace("-", "_")
    if "short_history" in clean or "not_enough_bars" in clean:
        return "short_history"
    if "spread" in clean:
        return "spread_too_wide"
    if "unstable_quote" in clean or "quote" in clean:
        return "unstable_quote"
    if "cooldown" in clean:
        return "cooldown"
    if "vwap" in clean:
        return "dynamic_vwap_extension"
    if "market_closed" in clean or "market_not_open" in clean:
        return "market_closed"
    if "reduce_only" in clean or "account_state" in clean or "buying_power" in clean or "exposure" in clean:
        return "account_state_changed"
    if "weak_catalyst" in clean or "no_catalyst" in clean:
        return "weak_catalyst_filter"
    if "allocator" in clean:
        return "allocator_rejected"
    if "dispatch" in clean or "dynamic_relative_volume" in clean or "dynamic_price_below_minimum" in clean:
        return "dispatch_rejected"
    return "entry_eval_rejected"


def _log_dynamic_pipeline_terminal(
    symbol: str,
    *,
    terminal_state: str,
    terminal_reason: str,
    entry_eval_started: bool = False,
    entry_eval_completed: bool = False,
    selected_timestamp: str = "unknown",
) -> None:
    sym_u = str(symbol or "").strip().upper() or "?"
    log.info(
        "DYNAMIC_PIPELINE_TERMINAL symbol=%s selected_timestamp=%s entry_eval_started=%s "
        "entry_eval_completed=%s terminal_state=%s terminal_reason=%s",
        sym_u,
        str(selected_timestamp or "unknown"),
        str(bool(entry_eval_started)).lower(),
        str(bool(entry_eval_completed)).lower(),
        str(terminal_state or "unknown"),
        str(terminal_reason or "unknown"),
    )


def _log_dynamic_entry_candidate_enqueued(symbol: str, *, source: str = "scanner_selected") -> None:
    sym_u = str(symbol or "").strip().upper() or "?"
    _dynamic_timing_mark(sym_u, "enqueue")
    _log_dynamic_latency(sym_u)
    line = "DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=%s source=%s" % (
        sym_u,
        str(source or "scanner_selected"),
    )
    log.info(line)
    print(line, flush=True)


def _log_dynamic_entry_candidate_skipped(symbol: str, *, reason: str) -> None:
    sym_u = str(symbol or "").strip().upper() or "?"
    line = "DYNAMIC_ENTRY_CANDIDATE_SKIPPED symbol=%s reason=%s" % (
        sym_u,
        str(reason or "unknown"),
    )
    log.info(line)
    print(line, flush=True)
    _log_dynamic_pipeline_terminal(
        sym_u,
        terminal_state=_dynamic_pipeline_terminal_state(str(reason or "unknown")),
        terminal_reason=str(reason or "unknown"),
    )


def _log_dynamic_entry_eval_start(symbol: str, *, source: str, route: str) -> None:
    sym_u = str(symbol or "").strip().upper() or "?"
    _dynamic_timing_mark(sym_u, "eval")
    _log_dynamic_latency(sym_u)
    line = "DYNAMIC_ENTRY_EVAL_START symbol=%s source=%s route=%s" % (
        sym_u,
        str(source or "scanner_selected"),
        str(route or "dynamic_momentum_override"),
    )
    log.info(line)
    print(line, flush=True)
    log.info(
        "DYNAMIC_PIPELINE_STATE symbol=%s selected_timestamp=unknown entry_eval_started=true "
        "entry_eval_completed=false terminal_state=pending terminal_reason=entry_eval_started",
        sym_u,
    )


def _log_dynamic_entry_eval_dropped(symbol: str, *, reason: str) -> None:
    sym_u = str(symbol or "").strip().upper() or "?"
    line = "DYNAMIC_ENTRY_EVAL_DROPPED symbol=%s reason=%s" % (
        sym_u,
        str(reason or "unknown"),
    )
    log.info(line)
    print(line, flush=True)
    _log_dynamic_pipeline_terminal(
        sym_u,
        terminal_state=_dynamic_pipeline_terminal_state(str(reason or "unknown")),
        terminal_reason=str(reason or "unknown"),
        entry_eval_started=True,
    )


def _finalize_dynamic_entry_eval_audit(
    *,
    enqueued_symbols: set[str],
    started_symbols: set[str],
    dropped_symbols: set[str],
    reason: str = "not_processed_after_entry_loop",
) -> None:
    pending_symbols = (
        set(enqueued_symbols or set())
        - set(started_symbols or set())
        - set(dropped_symbols or set())
    )
    for sym in sorted(pending_symbols):
        sym_u = str(sym or "").strip().upper()
        if not sym_u:
            continue
        dropped_symbols.add(sym_u)
        _log_dynamic_entry_eval_dropped(sym_u, reason=reason)


def _log_dynamic_entry_scanset_debug(
    *,
    selected: Sequence[Any],
    universe_added: Sequence[Any],
    entry_scan_symbols: Sequence[Any],
) -> None:
    selected_list = [str(s).strip().upper() for s in selected or [] if str(s).strip()]
    added_list = [str(s).strip().upper() for s in universe_added or [] if str(s).strip()]
    entry_list = [str(s).strip().upper() for s in entry_scan_symbols or [] if str(s).strip()]
    line = (
        "DYNAMIC_ENTRY_SCANSET_DEBUG selected=%s universe_added=%s entry_scan_symbols=%s"
        % (selected_list, added_list, entry_list)
    )
    log.info(line)
    print(line, flush=True)


def _log_dynamic_selected_entry_eval_start(
    symbol: str,
    *,
    route_candidate: str,
    detail: str | None = None,
) -> None:
    sym_u = str(symbol or "").strip().upper() or "?"
    log.info(
        "DYNAMIC_SELECTED_ENTRY_EVAL_START symbol=%s route_candidate=%s detail=%s",
        sym_u,
        str(route_candidate or "unknown"),
        str(detail or "entry_eval_call"),
    )


def _dynamic_high_conviction_bypass_active(
    *,
    is_dynamic_candidate: bool,
    dynamic_score: Any,
    news_score: Any,
) -> bool:
    """Return whether a dynamic name is high-conviction for blocked-gate diagnostics."""
    if not is_dynamic_candidate:
        return False
    try:
        dyn_score_f = float(dynamic_score or 0.0)
    except (TypeError, ValueError):
        dyn_score_f = 0.0
    try:
        news_score_f = float(news_score or 0.0)
    except (TypeError, ValueError):
        news_score_f = 0.0
    return dyn_score_f > 250.0 or news_score_f >= 8.0


def _dynamic_high_conviction_news_override_cfg(
    config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    cfg = config if isinstance(config, Mapping) else {}
    trading_cfg = cfg.get("trading") if isinstance(cfg.get("trading"), Mapping) else {}
    trading_dyn = (
        trading_cfg.get("dynamic")
        if isinstance(trading_cfg.get("dynamic"), Mapping)
        else {}
    )
    raw = (
        trading_dyn.get("high_conviction_news_override")
        if isinstance(trading_dyn.get("high_conviction_news_override"), Mapping)
        else {}
    )

    def _as_float(key: str, default: float) -> float:
        try:
            value = float(raw.get(key, default))
        except (TypeError, ValueError):
            value = default
        return value if value == value else default

    enabled_raw = raw.get("enabled", False)
    if isinstance(enabled_raw, str):
        enabled = enabled_raw.strip().lower() not in ("0", "false", "no", "off", "")
    else:
        enabled = bool(enabled_raw)
    require_sent_raw = raw.get("require_positive_sentiment", True)
    if isinstance(require_sent_raw, str):
        require_sentiment = (
            require_sent_raw.strip().lower() not in ("0", "false", "no", "off", "")
        )
    else:
        require_sentiment = bool(require_sent_raw)
    return {
        "enabled": enabled,
        "min_catalyst_score": max(0.0, _as_float("min_catalyst_score", 8.0)),
        "min_event_score": max(0.0, _as_float("min_event_score", 7.0)),
        "min_news_score": max(0.0, _as_float("min_news_score", 7.0)),
        "min_relative_volume": max(0.0, _as_float("min_relative_volume", 1.5)),
        "max_catalyst_age_minutes": max(
            0.0,
            _as_float("max_catalyst_age_minutes", 180.0),
        ),
        "require_positive_sentiment": require_sentiment,
    }


def _dynamic_high_conviction_trend_prefilter_override_decision(
    config: Mapping[str, Any] | None,
    *,
    route: str | None,
    is_dynamic_candidate: bool,
    is_core_symbol: bool = False,
    is_etf: bool = False,
    news_score: Any = None,
    event_score: Any = None,
    catalyst_score: Any = None,
    catalyst_type: Any = None,
    catalyst_age_minutes: Any = None,
    relative_volume: Any = None,
    sentiment: Any = None,
    severe_bearish_lockout: bool = False,
    cooldown_active: bool = False,
) -> tuple[bool, str, float]:
    cfg = _dynamic_high_conviction_news_override_cfg(config)
    if not bool(cfg["enabled"]):
        return False, "disabled", 0.0
    route_s = str(route or "").strip().lower()
    if route_s not in {"dynamic", "dynamic_momentum", "momentum_breakout"}:
        return False, "route_not_dynamic", 0.0
    if not is_dynamic_candidate:
        return False, "not_dynamic", 0.0
    if is_core_symbol:
        return False, "core_symbol", 0.0
    if is_etf:
        return False, "etf_symbol", 0.0
    if severe_bearish_lockout:
        return False, "severe_bearish_lockout", 0.0
    if cooldown_active:
        return False, "cooldown", 0.0

    try:
        age = float(catalyst_age_minutes)
    except (TypeError, ValueError):
        age = None
    if age is None:
        return False, "missing_fresh_catalyst_age", 0.0

    merged = {
        "trading": {
            "dynamic": {
                "high_conviction_news_override": {
                    "enabled": cfg["enabled"],
                    "min_news_score": cfg["min_news_score"],
                    "min_event_score": cfg["min_event_score"],
                    "min_catalyst_score": cfg["min_catalyst_score"],
                    "min_relative_volume": cfg["min_relative_volume"],
                    "max_catalyst_age_minutes": cfg["max_catalyst_age_minutes"],
                    "require_positive_sentiment": cfg["require_positive_sentiment"],
                }
            }
        }
    }
    raw_hc = (
        ((config or {}).get("trading") or {}).get("dynamic") or {}
        if isinstance(config, Mapping)
        else {}
    )
    raw_hc_cfg = raw_hc.get("high_conviction_news_override") if isinstance(raw_hc, Mapping) else {}
    if isinstance(raw_hc_cfg, Mapping) and isinstance(raw_hc_cfg.get("thresholds"), Mapping):
        merged["trading"]["dynamic"]["high_conviction_news_override"]["thresholds"] = raw_hc_cfg.get("thresholds")
    allowed, reason, score_eff, _thresholds = evaluate_high_conviction_news_override(
        merged,
        catalyst_type=catalyst_type or "earnings_beat",
        news_score=news_score,
        event_score=event_score,
        catalyst_score=catalyst_score,
        relative_volume=relative_volume,
        sentiment=sentiment,
        catalyst_age_minutes=age,
    )
    if not allowed:
        return False, reason, score_eff

    return True, "high_conviction_fresh_catalyst", score_eff


def _news_trend_prefilter_override_decision(
    config: Mapping[str, Any] | None,
    *,
    news_score: Any = None,
    event_score: Any = None,
    catalyst_score: Any = None,
) -> tuple[bool, float, float]:
    enabled, threshold = _news_trend_prefilter_override_config(config)
    scores: list[float] = []
    for raw, scale_unit_interval in (
        (news_score, False),
        (event_score, False),
        (catalyst_score, True),
    ):
        try:
            score = float(raw or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        if score == score:
            if scale_unit_interval and 0.0 < score <= 1.0:
                score *= 10.0
            scores.append(score)
    score_eff = max(scores) if scores else 0.0
    return bool(enabled and score_eff >= threshold), score_eff, threshold


def _news_trend_prefilter_override_config(
    config: Mapping[str, Any] | None,
) -> tuple[bool, float]:
    cfg = config if isinstance(config, Mapping) else {}
    entries_cfg = cfg.get("entries") if isinstance(cfg.get("entries"), Mapping) else {}
    enabled_raw = entries_cfg.get(
        "news_override_trend_prefilter_enabled",
        cfg.get("news_override_trend_prefilter_enabled", True),
    )
    if isinstance(enabled_raw, str):
        enabled = enabled_raw.strip().lower() not in ("0", "false", "no", "off", "")
    else:
        enabled = bool(enabled_raw) if enabled_raw is not None else True
    threshold_raw = entries_cfg.get(
        "news_override_min_score",
        cfg.get("news_override_min_score", 8.0),
    )
    try:
        threshold = float(threshold_raw)
    except (TypeError, ValueError):
        threshold = 8.0
    threshold = max(0.0, threshold)
    return enabled, threshold


def _coerce_premarket_rank(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        rank = int(float(value))
    except (TypeError, ValueError):
        return None
    return rank if rank > 0 else None


def _catalyst_trend_override_decision(
    *,
    news_score: Any,
    catalyst_score: Any,
    premarket_rank: Any,
    momentum_confirmed: bool,
    spread_ok: bool,
    atr_ok: bool,
    price_above_vwap: bool,
    day_gain_pct: Any,
) -> tuple[bool, str, int | None]:
    """Strict catalyst-only bypass for the trend prefilter."""
    try:
        news_f = float(news_score or 0.0)
    except (TypeError, ValueError):
        news_f = 0.0
    try:
        catalyst_f = float(catalyst_score or 0.0)
    except (TypeError, ValueError):
        catalyst_f = 0.0
    rank_i = _coerce_premarket_rank(premarket_rank)
    try:
        gain_f = float(day_gain_pct)
    except (TypeError, ValueError):
        gain_f = float("nan")

    if news_f < 8.0 and catalyst_f < 0.80:
        return False, "weak_catalyst", rank_i
    if rank_i is None or rank_i > 10:
        return False, "premarket_rank", rank_i
    if not bool(momentum_confirmed):
        return False, "momentum_confirmation", rank_i
    if not bool(spread_ok):
        return False, "spread", rank_i
    if not bool(atr_ok):
        return False, "atr", rank_i
    if not bool(price_above_vwap):
        return False, "vwap", rank_i
    if not math.isfinite(gain_f) or gain_f <= 5.0:
        return False, "day_gain", rank_i
    return True, "ok", rank_i


def _premarket_artifact_score_fields(
    premarket_artifacts: Mapping[str, Any] | None,
    symbol: str,
) -> tuple[float, float, float]:
    if not isinstance(premarket_artifacts, Mapping):
        return 0.0, 0.0, 0.0
    row = premarket_artifacts.get(str(symbol or "").strip().upper())
    if not isinstance(row, Mapping):
        return 0.0, 0.0, 0.0
    out: list[float] = []
    for key in ("news_score", "event_score", "catalyst_score"):
        try:
            out.append(float(row.get(key) or 0.0))
        except (TypeError, ValueError):
            out.append(0.0)
    return out[0], out[1], out[2]


def _premarket_catalyst_rvol_bypass_allowed(
    *,
    route: str,
    catalyst_score: Any,
    event_score: Any,
    news_score: Any,
) -> bool:
    if str(route or "").strip().lower() != "premarket_catalyst_replay":
        return False

    def _score(value: Any) -> float:
        try:
            out = float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return out if math.isfinite(out) else 0.0

    return (
        _score(catalyst_score) >= 0.3 - 1e-9
        or _score(event_score) >= 3.0 - 1e-9
        or _score(news_score) >= 3.0 - 1e-9
    )


def _premarket_catalyst_fastlane_signal(
    *,
    premarket_injected: bool,
    news_score: Any,
    event_score: Any,
    catalyst_score: Any,
    catalyst_age_minutes: Any,
) -> bool:
    if not premarket_injected:
        return False

    def _score(value: Any) -> float:
        try:
            out = float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return out if math.isfinite(out) else 0.0

    try:
        age = float(catalyst_age_minutes)
    except (TypeError, ValueError):
        return False
    return (
        age <= 300.0
        and (
            _score(news_score) >= 7.0
            or _score(event_score) >= 7.0
            or _score(catalyst_score) >= 0.7
        )
    )


def _catalyst_fastlane_entry_trace_fields(
    *,
    premarket_injected: bool,
    news_score: Any,
    event_score: Any,
    catalyst_score: Any,
    catalyst_age_minutes: Any,
    relative_volume: Any,
    threshold: Any = 0.50,
) -> dict[str, Any]:
    def _score(value: Any) -> float:
        try:
            out = float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return out if math.isfinite(out) else 0.0

    try:
        age = float(catalyst_age_minutes)
    except (TypeError, ValueError):
        age = math.inf
    try:
        rel = float(relative_volume)
    except (TypeError, ValueError):
        rel = math.nan
    try:
        threshold_f = float(threshold)
    except (TypeError, ValueError):
        threshold_f = 0.50
    threshold_f = max(0.0, threshold_f)

    news_f = _score(news_score)
    event_f = _score(event_score)
    catalyst_f = _score(catalyst_score)
    strong = news_f >= 7.0 or event_f >= 7.0 or catalyst_f >= 0.7
    if not premarket_injected:
        reason = "not_premarket_injected"
    elif not strong:
        reason = "weak_catalyst"
    elif not math.isfinite(age) or age > 300.0:
        reason = "stale_catalyst"
    elif not math.isfinite(rel):
        reason = "relative_volume_unavailable"
    elif rel < threshold_f:
        reason = "relative_volume_below_catalyst_threshold"
    else:
        reason = "ok"
    return {
        "premarket_injected": bool(premarket_injected),
        "news_score": news_f,
        "event_score": event_f,
        "catalyst_score": catalyst_f,
        "age": age,
        "relative_volume": rel,
        "threshold": threshold_f,
        "eligible": reason == "ok",
        "reason": reason,
    }


def _log_catalyst_fastlane_entry_trace(symbol: str, fields: Mapping[str, Any]) -> None:
    age = fields.get("age")
    rel = fields.get("relative_volume")
    log.info(
        "CATALYST_FASTLANE_ENTRY_TRACE symbol=%s premarket_injected=%s news_score=%.2f "
        "event_score=%.2f catalyst_score=%.2f age=%s rel_volume=%s threshold=%.2f "
        "eligible=%s reason=%s",
        str(symbol or "").strip().upper(),
        str(bool(fields.get("premarket_injected"))).lower(),
        float(fields.get("news_score", 0.0) or 0.0),
        float(fields.get("event_score", 0.0) or 0.0),
        float(fields.get("catalyst_score", 0.0) or 0.0),
        "n/a" if not isinstance(age, (int, float)) or not math.isfinite(float(age)) else f"{float(age):.1f}",
        "n/a" if not isinstance(rel, (int, float)) or not math.isfinite(float(rel)) else f"{float(rel):.3f}",
        float(fields.get("threshold", 0.50) or 0.50),
        str(bool(fields.get("eligible"))).lower(),
        str(fields.get("reason") or "unknown"),
    )


_OPTION_ROUTE_SKIP_REASONS = {
    "entry_eval_false",
    "underlying_not_allowed",
    "require_top_signal_failed",
    "environment_blocked",
    "daily_cap",
    "cooldown",
    "gross_exposure",
    "no_contract_found",
    "selector_rejected_all",
    "fallback_to_stock",
    "stock_route_selected",
}


def _option_route_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _option_route_value(row: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in row:
            out = _option_route_float(row.get(key))
            if out is not None:
                return out
    return None


def _paper_option_route_observable(
    row_tl: Mapping[str, Any],
    *,
    entry_eval_final: bool | None = None,
) -> bool:
    """True when paper-option route diagnostics should be emitted for this stock signal."""
    if entry_eval_final is None:
        entry_eval_final = bool(
            row_tl.get("entry_eval_final")
            or row_tl.get("final")
            or row_tl.get("final_signal")
        )
    if entry_eval_final:
        return True

    catalyst_score = _option_route_value(row_tl, "catalyst_score", "ai_catalyst_score")
    event_score = _option_route_value(row_tl, "event_score")
    news_score = _option_route_value(row_tl, "news_score")
    if (
        (catalyst_score is not None and catalyst_score >= 0.3 - 1e-9)
        or (event_score is not None and event_score >= 3.0 - 1e-9)
        or (news_score is not None and news_score >= 3.0 - 1e-9)
    ):
        return True

    route_text = " ".join(
        str(row_tl.get(key) or "") for key in ("route", "source", "signal_source")
    ).lower()
    if "momentum" not in route_text:
        return False
    strength_eff = _option_route_value(row_tl, "strength_eff", "score", "composite_score")
    relative_volume = _option_route_value(row_tl, "relative_volume", "rel_volume", "rvol")
    return bool(
        (strength_eff is not None and strength_eff >= 0.8 - 1e-9)
        or (relative_volume is not None and relative_volume >= 2.0 - 1e-9)
    )


def _options_route_observability_active(
    config: Mapping[str, Any] | None,
    broker: Any = None,
) -> bool:
    opts = (config or {}).get("options") if isinstance(config, Mapping) else {}
    opts = opts if isinstance(opts, Mapping) else {}
    if not bool(opts.get("enabled")):
        return False
    if str(opts.get("mode") or "").strip().lower() == "paper_only":
        return True
    if broker is None:
        return False
    return bool(_options_runtime_enabled(broker, dict(config or {})))


def _paper_option_underlying_allowed(config: Mapping[str, Any] | None, symbol: str) -> bool:
    opts = (config or {}).get("options") if isinstance(config, Mapping) else {}
    opts = opts if isinstance(opts, Mapping) else {}
    allowed = {
        str(item or "").strip().upper()
        for item in (opts.get("allowed_underlyings") or [])
        if str(item or "").strip()
    }
    return bool(allowed) and str(symbol or "").strip().upper() in allowed


def _option_route_skip_reason_from_text(reason: Any, reason_codes: Sequence[Any] = ()) -> str:
    text = " ".join([str(reason or ""), *(str(code or "") for code in reason_codes)]).lower()
    if "daily" in text or "max option entries" in text:
        return "daily_cap"
    if "cooldown" in text:
        return "cooldown"
    if "gross" in text or "over_exposed" in text or "over-exposed" in text:
        return "gross_exposure"
    if "underlying" in text and "not allowed" in text:
        return "underlying_not_allowed"
    if "top_signal" in text or "top signal" in text or "top-signals" in text:
        return "require_top_signal_failed"
    if "environment" in text or "reduce_only" in text or "regime" in text:
        return "environment_blocked"
    if "fallback" in text:
        return "fallback_to_stock"
    if "stock" in text and "route" in text:
        return "stock_route_selected"
    if "no_chain" in text or "no option chain" in text or "no contract" in text:
        return "no_contract_found"
    if any(
        token in text
        for token in (
            "route_failed",
            "liquidity_failed",
            "spread_failed",
            "stale_quote",
            "missing_bid_ask",
            "selector",
            "contract",
        )
    ):
        return "selector_rejected_all"
    return "selector_rejected_all"


def _log_option_route_check(
    symbol: str,
    *,
    lane: str,
    row_tl: Mapping[str, Any],
    entry_eval_final: bool | None = None,
) -> None:
    log.info(
        "OPTION_ROUTE_CHECK symbol=%s lane=%s route=%s entry_eval_final=%s "
        "catalyst_score=%s event_score=%s news_score=%s relative_volume=%s strength_eff=%s",
        str(symbol or "").strip().upper(),
        str(lane or "unknown"),
        str(row_tl.get("route") or row_tl.get("source") or "unknown"),
        str(
            bool(
                row_tl.get("entry_eval_final")
                if entry_eval_final is None
                else entry_eval_final
            )
        ).lower(),
        "n/a"
        if _option_route_value(row_tl, "catalyst_score", "ai_catalyst_score") is None
        else f"{float(_option_route_value(row_tl, 'catalyst_score', 'ai_catalyst_score') or 0.0):.2f}",
        "n/a"
        if _option_route_value(row_tl, "event_score") is None
        else f"{float(_option_route_value(row_tl, 'event_score') or 0.0):.2f}",
        "n/a"
        if _option_route_value(row_tl, "news_score") is None
        else f"{float(_option_route_value(row_tl, 'news_score') or 0.0):.2f}",
        "n/a"
        if _option_route_value(row_tl, "relative_volume", "rel_volume", "rvol") is None
        else f"{float(_option_route_value(row_tl, 'relative_volume', 'rel_volume', 'rvol') or 0.0):.2f}",
        "n/a"
        if _option_route_value(row_tl, "strength_eff", "score", "composite_score") is None
        else f"{float(_option_route_value(row_tl, 'strength_eff', 'score', 'composite_score') or 0.0):.3f}",
    )


def _log_option_route_skipped(
    symbol: str,
    *,
    lane: str,
    reason: str,
    detail: Any = None,
    row_tl: Mapping[str, Any] | None = None,
    route: str | None = None,
    underlying: str | None = None,
) -> None:
    reason_clean = str(reason or "").strip()
    if reason_clean not in _OPTION_ROUTE_SKIP_REASONS:
        reason_clean = _option_route_skip_reason_from_text(reason_clean)
    row = row_tl if isinstance(row_tl, Mapping) else {}
    sym_u = str(symbol or row.get("sym_u") or row.get("symbol") or "").strip().upper()
    route_clean = str(route or row.get("route") or row.get("source") or "unknown")
    underlying_clean = str(underlying or row.get("underlying") or sym_u).strip().upper()
    log.info(
        "OPTION_ROUTE_SKIPPED symbol=%s route=%s underlying=%s lane=%s reason=%s detail=%s",
        sym_u,
        route_clean,
        underlying_clean,
        str(lane or "unknown"),
        reason_clean,
        str(detail or ""),
    )


def _dynamic_rvol_required_from_reason(reason: str, fallback: Any) -> float:
    try:
        fallback_f = float(fallback or 0.0)
    except (TypeError, ValueError):
        fallback_f = 0.0
    parts = str(reason or "").replace(",", " ").split()
    if len(parts) >= 4 and parts[0] == "relative_volume" and parts[2] in {"<=", "<"}:
        try:
            return float(parts[3])
        except (TypeError, ValueError):
            return fallback_f
    return fallback_f


def _premarket_artifact_metadata_fields(
    premarket_artifacts: Mapping[str, Any] | None,
    symbol: str,
) -> tuple[str, float | None]:
    if not isinstance(premarket_artifacts, Mapping):
        return "", None
    row = premarket_artifacts.get(str(symbol or "").strip().upper())
    if not isinstance(row, Mapping):
        return "", None
    catalyst_type = str(row.get("catalyst_type") or "").strip()
    age_raw = row.get("age_minutes")
    if age_raw is None:
        age_raw = row.get("catalyst_age_minutes")
    try:
        age_minutes = max(0.0, float(age_raw)) if age_raw is not None else None
    except (TypeError, ValueError):
        age_minutes = None
    return catalyst_type, age_minutes


def _dynamic_scan_accepted_metadata(
    dynamic_scan_accepted: Sequence[Any] | None,
) -> dict[str, dict[str, Any]]:
    """Return live-loop metadata for candidates accepted by the dynamic scanner."""
    out: dict[str, dict[str, Any]] = {}
    for row in dynamic_scan_accepted or []:
        sym_u = str(getattr(row, "symbol", "") or "").strip().upper()
        if not sym_u or not bool(getattr(row, "accepted", True)):
            continue
        quality = getattr(row, "quality", None)
        meta: dict[str, Any] = {
            "symbol": sym_u,
            "scanner_selected": True,
            "dynamic_scanner_selected": True,
            "selected_by_dynamic_scanner": True,
            "dynamic_selected": True,
            "entry_alignment_ok": True,
            "entry_alignment_passed": True,
            "alignment_ok": True,
            "alignment_passed": True,
        }
        for field in (
            "score",
            "price",
            "day_gain_pct",
            "avg_volume",
            "relative_volume",
            "spread_pct",
            "effective_min_rel_volume",
            "scanner_effective_min_rel_volume",
            "news_score",
            "event_score",
            "catalyst_score",
            "article_count",
            "catalyst_headline",
            "catalyst_type",
            "catalyst_age_minutes",
            "premarket_injected",
            "catalyst_fastlane_active",
        ):
            value = getattr(row, field, None)
            if value is not None:
                meta[field] = value
        if quality is not None:
            for dst, src in (
                ("scanner_price_above_vwap", "price_above_vwap"),
                ("scanner_five_min_trend_aligned", "five_min_trend_aligned"),
                ("scanner_intraday_range_pct", "intraday_range_pct"),
                ("scanner_atr_expansion_ratio", "atr_expansion_ratio"),
            ):
                value = getattr(quality, src, None)
                if value is not None:
                    meta[dst] = value
        meta.update({k: v for k, v in _dynamic_timing_metadata(sym_u).items() if v is not None})
        out[sym_u] = meta
    return out


def _as_finite_float_or_none(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def _dynamic_scanner_approved_momentum_ok(
    config: Mapping[str, Any] | None,
    scanner_meta: Mapping[str, Any] | None,
) -> tuple[bool, str]:
    """Validate scanner-accepted momentum fields before downstream bypasses."""
    if not isinstance(scanner_meta, Mapping) or not bool(
        scanner_meta.get("scanner_selected")
        or scanner_meta.get("dynamic_scanner_selected")
        or scanner_meta.get("selected_by_dynamic_scanner")
        or scanner_meta.get("dynamic_selected")
    ):
        return False, "not_scanner_selected"
    cfg = config if isinstance(config, Mapping) else {}
    dyn_cfg = cfg.get("dynamic_universe") if isinstance(cfg.get("dynamic_universe"), Mapping) else {}
    try:
        min_avg_volume = float(dyn_cfg.get("min_avg_volume", 5000) or 5000)
    except (TypeError, ValueError):
        min_avg_volume = 5000.0
    try:
        min_price = float(dyn_cfg.get("min_price", 2.0) or 2.0)
    except (TypeError, ValueError):
        min_price = 2.0
    try:
        max_price = float(dyn_cfg.get("max_price", 0.0) or 0.0)
    except (TypeError, ValueError):
        max_price = 0.0
    try:
        max_spread = float(dyn_cfg.get("max_spread_pct", 3.0) or 3.0)
    except (TypeError, ValueError):
        max_spread = 3.0
    price = _as_finite_float_or_none(scanner_meta.get("price"))
    if price is not None:
        if price < min_price - 1e-9:
            return False, "price %.2f < min %.2f" % (price, min_price)
        if max_price > 0.0 and price > max_price + 1e-9:
            return False, "price %.2f > max %.2f" % (price, max_price)
    spread = _as_finite_float_or_none(scanner_meta.get("spread_pct"))
    if spread is None:
        return False, "spread_unavailable"
    if spread > max_spread + 1e-9:
        return False, "spread %.3f > max %.3f" % (spread, max_spread)
    avg_volume = _as_finite_float_or_none(scanner_meta.get("avg_volume"))
    if avg_volume is None or avg_volume < min_avg_volume - 1e-9:
        return False, "avg_volume %.0f < min %.0f" % (float(avg_volume or 0.0), min_avg_volume)
    gain = _as_finite_float_or_none(scanner_meta.get("day_gain_pct"))
    if gain is None or gain < 15.0 - 1e-9:
        return False, "day_gain_pct %.2f < 15.00" % float(gain or 0.0)
    rel = _as_finite_float_or_none(scanner_meta.get("relative_volume"))
    if rel is None or rel < 1.2 - 1e-9:
        return False, "relative_volume %.3f < 1.200" % float(rel or 0.0)
    atr = _as_finite_float_or_none(scanner_meta.get("scanner_atr_expansion_ratio"))
    if atr is not None and atr <= 0.0:
        return False, "atr_expansion_ratio %.3f <= 0" % atr
    return True, "scanner_selected_dynamic_momentum"


def _dynamic_scanner_metadata_payload(scanner_meta: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(scanner_meta, Mapping):
        return {}
    payload: dict[str, Any] = {
        "scanner_selected": True,
        "dynamic_scanner_selected": True,
        "selected_by_dynamic_scanner": True,
        "dynamic_selected": True,
        "entry_alignment_ok": True,
        "entry_alignment_passed": True,
        "alignment_ok": True,
        "alignment_passed": True,
    }
    for key in (
        "price",
        "avg_volume",
        "spread_pct",
        "relative_volume",
        "effective_min_rel_volume",
        "scanner_effective_min_rel_volume",
        "scanner_price_above_vwap",
        "scanner_five_min_trend_aligned",
        "scanner_intraday_range_pct",
        "scanner_atr_expansion_ratio",
        "news_score",
        "event_score",
        "catalyst_score",
        "article_count",
        "catalyst_type",
        "catalyst_headline",
        "catalyst_age_minutes",
        "premarket_injected",
        "catalyst_fastlane_active",
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
    ):
        if key in scanner_meta:
            payload[key] = scanner_meta.get(key)
    return payload


def _dynamic_entry_scanner_approval_override_decision(
    config: Mapping[str, Any] | None,
    scanner_meta: Mapping[str, Any] | None,
    reject_reason: Any,
) -> tuple[bool, str]:
    reason = str(reject_reason or "")
    if not reason.startswith("need 5m breakout"):
        return False, "not_alignment_reject"
    return _dynamic_scanner_approved_momentum_ok(config, scanner_meta)


def _dynamic_short_history_fallback_decision(
    config: Mapping[str, Any] | None,
    *,
    is_dynamic_candidate: bool,
    available_bars: int,
    required_bars: int,
    news_score: Any = None,
    event_score: Any = None,
    catalyst_score: Any = None,
    scanner_selected: bool = False,
    scanner_meta: Mapping[str, Any] | None = None,
) -> tuple[bool, str]:
    cfg = config if isinstance(config, Mapping) else {}
    dyn_cfg = cfg.get("dynamic_universe") if isinstance(cfg.get("dynamic_universe"), Mapping) else {}
    enabled_raw = dyn_cfg.get("short_history_fallback_enabled", True)
    if isinstance(enabled_raw, str):
        enabled = enabled_raw.strip().lower() not in ("0", "false", "no", "off", "")
    else:
        enabled = bool(enabled_raw)
    if not enabled:
        return False, "disabled"
    if not is_dynamic_candidate:
        return False, "not_dynamic"
    try:
        bars = int(available_bars or 0)
    except (TypeError, ValueError):
        bars = 0
    try:
        need = int(required_bars or 0)
    except (TypeError, ValueError):
        need = 0
    try:
        min_bars = int(dyn_cfg.get("short_history_min_daily_bars", 90) or 90)
    except (TypeError, ValueError):
        min_bars = 90
    min_bars = max(1, min_bars)
    if bool(scanner_selected):
        try:
            scanner_min_bars = int(dyn_cfg.get("short_history_scanner_selected_min_daily_bars", 40) or 40)
        except (TypeError, ValueError):
            scanner_min_bars = 40
        scanner_min_bars = max(1, scanner_min_bars)
        if bars < scanner_min_bars:
            return False, "bars %d < scanner_selected_min %d" % (bars, scanner_min_bars)
        if need > 0 and bars >= need:
            return False, "full_history_available"
        scanner_ok, scanner_reason = _dynamic_scanner_approved_momentum_ok(config, scanner_meta)
        if scanner_ok:
            return True, scanner_reason
    if bars < min_bars:
        return False, "bars %d < fallback_min %d" % (bars, min_bars)
    if need > 0 and bars >= need:
        return False, "full_history_available"

    def _as_float(raw: Any) -> float:
        try:
            value = float(raw or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        return value if value == value else 0.0

    news = _as_float(news_score)
    event = _as_float(event_score)
    catalyst = _as_float(catalyst_score)
    catalyst_scaled = catalyst * 10.0 if 0.0 < catalyst <= 1.0 else catalyst
    effective = max(news, event, catalyst_scaled)
    try:
        min_score = float(dyn_cfg.get("short_history_min_catalyst_score", 3.0) or 3.0)
    except (TypeError, ValueError):
        min_score = 3.0
    if effective < float(min_score):
        return False, "catalyst_score %.2f < %.2f" % (effective, float(min_score))
    return True, "catalyst_short_history"


def _has_high_conviction_news_candidates(
    config: Mapping[str, Any] | None,
    premarket_artifacts: Mapping[str, Any] | None,
    dynamic_news_scores: Mapping[str, Any] | None = None,
    dynamic_event_scores: Mapping[str, Any] | None = None,
) -> bool:
    symbols = {
        str(sym or "").strip().upper()
        for sym in (premarket_artifacts or {}).keys()
        if str(sym or "").strip()
    }
    symbols.update(
        str(sym or "").strip().upper()
        for sym in (dynamic_news_scores or {}).keys()
        if str(sym or "").strip()
    )
    symbols.update(
        str(sym or "").strip().upper()
        for sym in (dynamic_event_scores or {}).keys()
        if str(sym or "").strip()
    )
    for sym in symbols:
        art_news, art_event, art_catalyst = _premarket_artifact_score_fields(
            premarket_artifacts,
            sym,
        )
        try:
            dyn_news = float((dynamic_news_scores or {}).get(sym, 0) or 0)
        except (TypeError, ValueError):
            dyn_news = 0.0
        try:
            dyn_event = float((dynamic_event_scores or {}).get(sym, 0) or 0)
        except (TypeError, ValueError):
            dyn_event = 0.0
        override, _score, _threshold = _news_trend_prefilter_override_decision(
            config,
            news_score=max(float(art_news), dyn_news),
            event_score=max(float(art_event), dyn_event),
            catalyst_score=art_catalyst,
        )
        if override:
            return True
    return False


def _entry_eval_route_log_from_metadata(
    default_route: str,
    metadata: Mapping[str, Any] | None,
) -> str:
    md = metadata if isinstance(metadata, Mapping) else {}
    if md.get("source") == "news_sentiment":
        return "news_override"
    if md.get("source") == "high_conviction_catalyst":
        return "news_trend_override"
    if md.get("alternate_entry") or md.get("source") in (
        "breakout",
        "mean_reversion",
        "volatility",
    ):
        return str(md.get("source", "alternate"))
    return str(default_route)


def _news_trend_override_max_day_loss(config: Mapping[str, Any] | None) -> float:
    cfg = config if isinstance(config, Mapping) else {}
    entries_cfg = cfg.get("entries") if isinstance(cfg.get("entries"), Mapping) else {}
    raw = entries_cfg.get(
        "max_day_loss_pct_for_news_override",
        cfg.get("max_day_loss_pct_for_news_override", -5.0),
    )
    try:
        val = float(raw)
    except (TypeError, ValueError):
        val = -5.0
    return min(0.0, val)


def _news_trend_override_reclaim_bars(config: Mapping[str, Any] | None) -> int:
    cfg = config if isinstance(config, Mapping) else {}
    entries_cfg = cfg.get("entries") if isinstance(cfg.get("entries"), Mapping) else {}
    raw = entries_cfg.get(
        "news_override_vwap_reclaim_bars",
        cfg.get("news_override_vwap_reclaim_bars", 5),
    )
    try:
        val = int(float(raw))
    except (TypeError, ValueError):
        val = 5
    return max(1, min(30, val))


def _news_trend_override_price_confirmation(
    *,
    price: Any,
    day_change_pct: Any,
    bars_1m: Any,
    bars_5m: Any,
    config: Mapping[str, Any] | None,
) -> tuple[bool, str, float | None, float | None]:
    try:
        day_change = float(day_change_pct)
    except (TypeError, ValueError):
        day_change = None
    max_loss = _news_trend_override_max_day_loss(config)
    if day_change is not None and day_change <= max_loss + 1e-9:
        return False, "day_loss_too_large", day_change, None

    try:
        px = float(price)
    except (TypeError, ValueError):
        px = math.nan
    vwap = session_vwap_from_bars(bars_1m)
    above_vwap = bool(
        vwap is not None
        and math.isfinite(float(vwap))
        and math.isfinite(px)
        and px > float(vwap)
    )
    reclaimed_vwap = False
    if not above_vwap and vwap is not None and bars_1m is not None and not getattr(bars_1m, "empty", True):
        try:
            closes = list(bars_1m["close"].astype(float))
            lookback = _news_trend_override_reclaim_bars(config)
            recent = closes[-max(2, lookback + 1) :]
            for prev_c, cur_c in zip(recent, recent[1:]):
                if float(prev_c) <= float(vwap) and float(cur_c) > float(vwap):
                    reclaimed_vwap = True
                    break
        except Exception:
            reclaimed_vwap = False
    if not (above_vwap or reclaimed_vwap):
        return False, "below_vwap", day_change, vwap

    momentum_ok = False
    if bars_5m is not None and not getattr(bars_5m, "empty", True):
        try:
            closes_5m = list(bars_5m["close"].astype(float))
            if len(closes_5m) >= 2 and float(closes_5m[-2]) > 0:
                ret_5m = (float(closes_5m[-1]) / float(closes_5m[-2]) - 1.0) * 100.0
                momentum_ok = ret_5m > 0.0 or float(closes_5m[-1]) > float(closes_5m[-2])
        except Exception:
            momentum_ok = False
    if not momentum_ok:
        return False, "no_5m_momentum", day_change, vwap
    return True, "ok", day_change, vwap


def _dynamic_fastlane_candidate(
    *,
    news_score: Any,
    catalyst_age_minutes: Any,
) -> bool:
    try:
        score = float(news_score or 0)
    except (TypeError, ValueError):
        score = 0.0
    try:
        age = float(catalyst_age_minutes)
    except (TypeError, ValueError):
        return False
    return score >= 7.0 and age <= 300.0


def _dynamic_fastlane_allowed(
    now: datetime,
    *,
    news_score: Any,
    catalyst_age_minutes: Any,
) -> tuple[bool, str]:
    if not _dynamic_fastlane_window_active(now):
        return False, "outside_open_fastlane_window"
    if not _dynamic_fastlane_candidate(
        news_score=news_score,
        catalyst_age_minutes=catalyst_age_minutes,
    ):
        return False, "weak_or_stale_news"
    return True, "strong_news_open_fastlane"


def _log_dynamic_fastlane(
    symbol: str,
    *,
    news_score: Any,
    catalyst_age_minutes: Any,
    allowed: bool,
    reason: str,
) -> None:
    sym_u = str(symbol or "").strip().upper()
    news_text = str(news_score if news_score is not None else "n/a")
    age_text = str(catalyst_age_minutes if catalyst_age_minutes is not None else "n/a")
    log.info(
        "DYNAMIC_FASTLANE symbol=%s news_score=%s catalyst_age_minutes=%s allowed=%s reason=%s",
        sym_u,
        news_text,
        age_text,
        str(bool(allowed)).lower(),
        reason,
    )
    log.info(
        "CATALYST_FASTLANE_CHECK symbol=%s news_score=%s catalyst_age_minutes=%s allowed=%s reason=%s",
        sym_u,
        news_text,
        age_text,
        str(bool(allowed)).lower(),
        reason,
    )
    if allowed:
        log.info("CATALYST_FASTLANE_ALLOWED symbol=%s reason=%s", sym_u, reason)
    else:
        log.info("CATALYST_FASTLANE_REJECT symbol=%s reason=%s", sym_u, reason)


def _dynamic_fastlane_startup_bypass_symbols(
    dynamic_symbols: Sequence[Any],
    strong_dynamic_map: Mapping[str, Mapping[str, Any]],
    now: datetime,
) -> list[str]:
    out: list[str] = []
    for sym in dynamic_symbols or []:
        sym_u = str(sym or "").strip().upper()
        if not sym_u:
            continue
        meta = strong_dynamic_map.get(sym_u, {})
        allowed, _reason = _dynamic_fastlane_allowed(
            now,
            news_score=meta.get("news_score"),
            catalyst_age_minutes=meta.get("age_minutes"),
        )
        if allowed:
            out.append(sym_u)
    return out


def _apply_strong_dynamic_etf_penalty(
    row: dict[str, Any],
    symbol: str,
    *,
    strong_dynamic_candidates_present: bool,
    dynamic_exposure_pct: float = 0.0,
    exposure_threshold_pct: float = 20.0,
) -> float:
    exposure_active = float(dynamic_exposure_pct or 0.0) > float(exposure_threshold_pct)
    if not strong_dynamic_candidates_present and not exposure_active:
        return 1.0
    sym_u = str(symbol or "").strip().upper()
    if sym_u not in {"SPY", "QQQ", "IWM"}:
        return 1.0
    penalty = 0.5
    for key in ("strength_eff", "composite_score", "priority_score", "score"):
        raw = row.get(key)
        try:
            if raw is not None and str(raw).strip() != "":
                row[key] = float(raw) * penalty
        except (TypeError, ValueError):
            continue
    row["dynamic_etf_penalty"] = penalty
    reason = "strong_dynamic_candidates" if strong_dynamic_candidates_present else "dynamic_exposure"
    log.info(
        "CORE_BUY_DEPRIORITIZED symbol=%s reason=%s dynamic_exposure_pct=%.2f threshold_pct=%.2f",
        sym_u,
        reason,
        float(dynamic_exposure_pct or 0.0),
        float(exposure_threshold_pct),
    )
    log.info(
        "DYNAMIC_ETF_PENALTY symbol=%s base_penalty=0.5 strong_dynamic_candidates_present=%s dynamic_exposure_pct=%.2f",
        sym_u,
        str(bool(strong_dynamic_candidates_present)).lower(),
        float(dynamic_exposure_pct or 0.0),
    )
    return penalty


def _dynamic_exposure_pct(
    positions: Sequence[Mapping[str, Any]] | None,
    *,
    dynamic_symbols: Sequence[Any],
    account_equity: float,
) -> float:
    """Current dynamic-only exposure percent used to deprioritize ETF fallback buys."""
    try:
        equity = float(account_equity)
    except (TypeError, ValueError):
        equity = 0.0
    if equity <= 0.0:
        return 0.0
    dynamic_set = {
        str(sym or "").strip().upper()
        for sym in dynamic_symbols or []
        if str(sym or "").strip()
    }
    if not dynamic_set:
        return 0.0
    total = 0.0
    for row in positions or []:
        sym = str(row.get("symbol") if isinstance(row, Mapping) else "").strip().upper()
        if sym not in dynamic_set:
            continue
        try:
            total += abs(float(row.get("market_value") or 0.0))
        except (TypeError, ValueError, AttributeError):
            continue
    return (total / equity) * 100.0


def _log_dynamic_universe_startup_config(config: dict[str, Any]) -> None:
    du = config.get("dynamic_universe") if isinstance(config, dict) else {}
    du_cfg = du if isinstance(du, dict) else {}
    entry_cfg = config.get("dynamic_momentum_entry") if isinstance(config, dict) else {}
    entry_cfg = entry_cfg if isinstance(entry_cfg, dict) else {}
    news_entry_cfg = entry_cfg.get("news_dynamic_entry") if isinstance(entry_cfg.get("news_dynamic_entry"), dict) else {}
    market_cfg = config.get("market") if isinstance(config, dict) else {}
    market_cfg = market_cfg if isinstance(market_cfg, dict) else {}
    open_protection_cfg = market_cfg.get("open_protection") if isinstance(market_cfg.get("open_protection"), dict) else {}
    portfolio_cfg = config.get("portfolio") if isinstance(config, dict) else {}
    portfolio_cfg = portfolio_cfg if isinstance(portfolio_cfg, dict) else {}
    settings = _dynamic_scan_settings(du_cfg)
    log.info(
        "DYNAMIC_CONFIG "
        "min_price=%s "
        "min_avg_volume=%s "
        "min_rel_volume=%s "
        "min_atr_expansion_ratio=%s",
        settings["min_price"],
        settings["min_avg_vol"],
        settings["min_rel_vol"],
        settings["min_atr_expansion_ratio"],
    )
    fields = {
        "enabled": bool(du_cfg.get("enabled", False)),
        "max_symbols": du_cfg.get("max_symbols"),
        "min_price": du_cfg.get("min_price"),
        "max_price": du_cfg.get("max_price"),
        "min_day_gain_pct": du_cfg.get("min_day_gain_pct"),
        "max_day_gain_pct": du_cfg.get("max_day_gain_pct"),
        "min_rel_volume": du_cfg.get("min_rel_volume"),
        "min_intraday_range_pct": du_cfg.get("min_intraday_range_pct"),
        "min_atr_expansion_ratio": du_cfg.get("min_atr_expansion_ratio"),
        "max_spread_pct": du_cfg.get("max_spread_pct"),
        "execution_max_spread_pct": du_cfg.get("execution_max_spread_pct"),
    }
    parts = ["DYNAMIC_UNIVERSE_CONFIG"]
    for key, value in fields.items():
        if isinstance(value, bool):
            text = "true" if value else "false"
        else:
            text = "unset" if value is None else str(value)
        parts.append(f"{key}={text}")
    msg = " ".join(parts)
    log.info(msg)
    print(msg, flush=True)
    try:
        _range_floor = float(du_cfg.get("min_intraday_range_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        _range_floor = 0.0
    if _range_floor <= 1.0:
        log.info("DYNAMIC_RANGE_LOOSENED old=1.25 new=%.1f", _range_floor)
    try:
        dynamic_min_history_bars = int(du_cfg.get("min_history_bars", 180) or 180)
    except (TypeError, ValueError):
        dynamic_min_history_bars = 180
    dynamic_min_history_bars = max(1, dynamic_min_history_bars)
    history_msg = f"DYNAMIC_HISTORY_CONFIG min_history_bars={dynamic_min_history_bars}"
    log.info(history_msg)
    print(history_msg, flush=True)
    loosened_fields = {
        "open_protection_minutes": open_protection_cfg.get("dynamic_scan_delay_minutes"),
        "min_relative_volume": entry_cfg.get("min_relative_volume", settings["min_rel_vol"]),
        "early_min_relative_volume": news_entry_cfg.get("early_min_relative_volume"),
        "min_day_gain_pct": entry_cfg.get("min_day_gain_pct", settings["min_gain"]),
        "max_symbols": settings["max_symbols"],
        "max_positions": portfolio_cfg.get("max_positions"),
        "dynamic_allocation": portfolio_cfg.get("target_dynamic_pct"),
    }
    loosened_parts = ["DYNAMIC_LOOSENED_CONFIG"]
    for key, value in loosened_fields.items():
        loosened_parts.append(f"{key}={'unset' if value is None else value}")
    loosened_msg = " ".join(loosened_parts)
    log.info(loosened_msg)
    print(loosened_msg, flush=True)


def _load_premarket_artifacts_into_runtime(
    *,
    engine: Any,
    project_root: Path,
    now: datetime,
) -> dict[str, dict[str, Any]]:
    readiness = check_premarket_readiness(project_root, now=now)
    max_age = "n/a" if readiness.max_age_minutes is None else f"{readiness.max_age_minutes:.1f}"
    runtime_status = "loaded" if readiness.present and readiness.status not in {"missing", "unreadable"} else readiness.status
    log.info(
        "PREMARKET_RUNTIME_LOAD status=%s fresh=%s rankings=%d catalysts=%d events=%d age_minutes=%s",
        runtime_status,
        str(bool(readiness.fresh and readiness.status != "fresh_empty")).lower(),
        int(readiness.ranking_count),
        int(readiness.catalyst_count),
        int(readiness.event_count),
        max_age,
    )
    artifact_summary = load_premarket_artifacts(project_root, now=now, emit_log=True)
    if not artifact_summary:
        return {}
    try:
        dyn_scores = dict(getattr(engine, "dynamic_news_scores", {}) or {})
        dyn_headlines = dict(getattr(engine, "dynamic_news_headlines", {}) or {})
        dyn_types = dict(getattr(engine, "dynamic_news_catalyst_types", {}) or {})
        dyn_event_scores = dict(getattr(engine, "dynamic_event_scores", {}) or {})
        dyn_catalyst_scores = dict(getattr(engine, "dynamic_catalyst_scores", {}) or {})
        for sym, data in artifact_summary.items():
            su = str(sym or "").strip().upper()
            if not su:
                continue
            news_score = int(data.get("news_score", 0) or 0)
            event_score = float(data.get("event_score", 0.0) or 0.0)
            catalyst_score = float(data.get("catalyst_score", 0.0) or 0.0)
            try:
                article_count = int(float(data.get("article_count", 0) or 0))
            except (TypeError, ValueError):
                article_count = 0
            headline = str(data.get("headline", "") or "").strip()
            catalyst_type = str(data.get("catalyst_type") or "").strip()
            if not _premarket_artifact_has_confirmed_metadata(data):
                log.info(
                    "CATALYST_METADATA_MISSING symbol=%s reason=missing_or_zero_metadata",
                    su,
                )
            if news_score > 0:
                dyn_scores[su] = max(int(dyn_scores.get(su, 0) or 0), news_score)
            elif event_score > 0 and su not in dyn_scores:
                dyn_scores[su] = int(math.ceil(event_score))
            if headline:
                dyn_headlines[su] = headline
            if catalyst_type:
                dyn_types[su] = catalyst_type
            if event_score > 0:
                dyn_event_scores[su] = max(float(dyn_event_scores.get(su, 0.0) or 0.0), event_score)
            if catalyst_score > 0:
                dyn_catalyst_scores[su] = max(
                    float(dyn_catalyst_scores.get(su, 0.0) or 0.0),
                    catalyst_score,
                )
            log.info(
                "PREMARKET_ARTIFACT_RUNTIME_SCORE symbol=%s news_score=%d event_score=%.2f "
                "catalyst_score=%.2f catalyst_type=%s source=%s headline=%s",
                su,
                int(news_score),
                float(event_score),
                float(catalyst_score),
                catalyst_type or "unknown",
                str(data.get("source") or "unknown"),
                headline[:180],
            )
            log.info(
                "CATALYST_RUNTIME_SYMBOL symbol=%s premarket_injected=true news_score=%.2f "
                "event_score=%.2f catalyst_score=%.2f article_count=%d rank=%s headline=%s",
                su,
                float(news_score),
                float(event_score),
                float(catalyst_score),
                int(article_count),
                str(data.get("premarket_rank") or "n/a"),
                headline[:180],
            )
        engine.dynamic_news_scores = dyn_scores
        engine.dynamic_news_headlines = dyn_headlines
        engine.dynamic_news_catalyst_types = dyn_types
        engine.dynamic_event_scores = dyn_event_scores
        engine.dynamic_catalyst_scores = dyn_catalyst_scores
    except Exception:
        log.debug("premarket artifact runtime merge failed", exc_info=True)
    return artifact_summary


def _premarket_runtime_ready_for_live(readiness: Any) -> bool:
    """True when live catalyst/news expansion has usable fresh premarket artifacts."""
    return premarket_runtime_ready(readiness)


def _live_premarket_runtime_guard_allows_dynamic(
    *,
    project_root: Path,
    now: datetime,
    is_live: bool,
) -> bool:
    """Guard live dynamic news/catalyst expansion when premarket artifacts are absent or stale."""
    if not bool(is_live):
        return True
    readiness = check_premarket_readiness(project_root, now=now)
    if _premarket_runtime_ready_for_live(readiness):
        return True
    log.info(
        "PREMARKET_RUNTIME_GUARD status=blocked reason=missing_or_stale_premarket_artifacts"
    )
    return False


def _log_premarket_artifact_startup_validation(
    *,
    project_root: Path,
    now: datetime,
) -> None:
    readiness = check_premarket_readiness(project_root, now=now)
    max_age = "n/a" if readiness.max_age_minutes is None else f"{readiness.max_age_minutes:.1f}"
    log.info(
        "PREMARKET_STARTUP_ARTIFACTS status=%s present=%s fresh=%s missing=%s stale=%s "
        "catalyst_ranked_symbols=%d rankings=%d catalysts=%d events=%d max_age_minutes=%s",
        readiness.status,
        str(readiness.present).lower(),
        str(readiness.fresh).lower(),
        ",".join(readiness.missing) or "none",
        ",".join(readiness.stale) or "none",
        readiness.catalyst_ranked_symbols,
        readiness.ranking_count,
        readiness.catalyst_count,
        readiness.event_count,
        max_age,
    )
    for artifact in readiness.artifacts:
        age = "n/a" if artifact.age_minutes is None else f"{artifact.age_minutes:.1f}"
        ttl = "n/a" if artifact.ttl_minutes is None else str(artifact.ttl_minutes)
        log.info(
            "PREMARKET_STARTUP_ARTIFACT kind=%s status=%s present=%s age_minutes=%s ttl_minutes=%s "
            "symbols=%d events=%d rankings=%d catalysts=%d path=%s",
            artifact.kind,
            artifact.status,
            str(artifact.present).lower(),
            age,
            ttl,
            artifact.symbols,
            artifact.events,
            artifact.rankings,
            artifact.catalysts,
            str(artifact.path),
        )


def _news_fast_lane_interval_seconds(config: Mapping[str, Any] | None) -> float | None:
    root = config or {}
    raw = root.get("news_fast_lane")
    if not isinstance(raw, Mapping):
        return None
    if not bool(raw.get("enabled", False)):
        return None
    try:
        return max(1.0, float(raw.get("scan_interval_seconds", 60) or 60))
    except (TypeError, ValueError):
        return 60.0


def _sync_options_position_state(
    *,
    broker: Any,
    config: Mapping[str, Any] | None,
    user_id: str,
    data_dir: Path,
    now: datetime,
    execution_manager: Any,
) -> _OptionsPositionSnapshot:
    snapshot = _sync_options_positions(
        broker,
        config,
        user_id=user_id,
        data_dir=data_dir,
        now=now,
        execution_manager=execution_manager,
    )
    if snapshot.kill_switch_on:
        log.info(
            "OPTIONS_KILL_SWITCH_ON user_id=%s block_new_entries=%s reasons=%s",
            user_id,
            snapshot.block_new_entries,
            ",".join(snapshot.kill_switch_reasons),
        )
    return snapshot


def _maybe_scan_options_for_dynamic_candidates(
    broker: Any,
    config: Mapping[str, Any] | None,
    dynamic_candidates: Sequence[Any],
    *,
    now: datetime,
) -> list[Any]:
    if not _options_scan_only_active(config):
        return []
    return _scan_dynamic_candidates_option_chains(
        broker,
        config,
        dynamic_candidates,
        log_dt=now,
        top_n=3,
        project_root=PROJECT_ROOT,
    )


def run_live_cycle(args: argparse.Namespace) -> None:
    from src.trading_control import (
        TradingControlBroker,
        emit_strategy_state_startup,
        emit_trading_mode_startup,
        resolve_trading_mode,
    )

    verbose = getattr(args, "verbose", False)
    _startup_service_begin_mono = time.monotonic()
    _startup_service_begin_dt = datetime.now(pytz.timezone("America/New_York"))
    log.info("STARTUP_SERVICE_BEGIN timestamp=%s", _startup_service_begin_dt.isoformat())

    config_path = PROJECT_ROOT / "config" / "default.yaml"
    _config_source_msg = "CONFIG_SOURCE=config/default.yaml"
    _config_loaded_msg = "CONFIG_PATH_LOADED path=%s" % config_path
    _git_commit_msg = "GIT_COMMIT=%s" % _current_git_commit()
    _premarket_version_msg = "PREMARKET_ENGINE_VERSION=%s" % PREMARKET_ENGINE_VERSION
    log.info(_config_source_msg)
    log.info(_config_loaded_msg)
    log.info(_git_commit_msg)
    log.info(_premarket_version_msg)
    print(_config_source_msg, flush=True)
    print(_config_loaded_msg, flush=True)
    print(_git_commit_msg, flush=True)
    print(_premarket_version_msg, flush=True)
    config = load_app_config(config_path)

    # ---------------------------------------------------------------------------
    # Multi-user setup via UserManager
    # ---------------------------------------------------------------------------
    users_path = PROJECT_ROOT / "config" / "users.yaml"
    try:
        selected_user = resolve_selected_user_id(
            cli_user=getattr(args, "user", None),
            users_path=users_path,
        )
    except ValueError as exc:
        print(f"USER_SELECTION_ERROR {exc}", file=sys.stderr, flush=True)
        notify_alpaca_loop_stopped(reason="user_selection_failed", detail=str(exc))
        sys.exit(2)
    log.info("USER_SELECTION selected=%s source=%s", selected_user, "cli" if getattr(args, "user", None) else ("env" if os.getenv("ALGO_USER") else "default"))
    user_manager = UserManager(config, users_path=users_path, selected_user_id=selected_user)

    if len(user_manager.list_users()) > 1 and (args.live or args.paper):
        print(
            "WARNING: --live/--paper flags are ignored in multi-user mode "
            "(each user has their own paper flag in users.yaml)"
        )

    # Single-user: --live / --paper must change broker.paper BEFORE UserContext
    # and AlpacaBroker are built (otherwise we still hit paper API with wrong keys).
    if not user_manager.multi_user and (args.live or args.paper):
        config = load_app_config(config_path)
        if args.live:
            config.setdefault("broker", {})["paper"] = False
        elif args.paper:
            config.setdefault("broker", {})["paper"] = True
        user_manager = UserManager(config, users_path=users_path, selected_user_id=selected_user)

    user_contexts = init_user_contexts(
        user_manager,
        project_root=PROJECT_ROOT,
        user_filter=selected_user,
    )
    if not user_contexts:
        print("No user contexts loaded. Exiting.")
        notify_alpaca_loop_stopped(reason="init_failed", detail="No user contexts loaded.")
        sys.exit(1)

    for _ctx_mode in user_contexts:
        try:
            _mode_state = resolve_trading_mode(
                _ctx_mode.config,
                cli_mode=getattr(args, "mode", None),
                paper=_ctx_mode.paper,
                live_operation=not bool(_ctx_mode.paper),
            )
        except Exception as exc:
            print(
                f"TRADING_MODE_ERROR user_id={_ctx_mode.user_id} error={exc}",
                file=sys.stderr,
                flush=True,
            )
            notify_alpaca_loop_stopped(reason="trading_mode_invalid", detail=str(exc))
            sys.exit(2)
        _ctx_mode.config.setdefault("trading_control", {})["mode"] = _mode_state.mode
        emit_trading_mode_startup(_mode_state)
        emit_strategy_state_startup(_ctx_mode.config)
        try:
            record_runtime_event(
                _ctx_mode.data_dir or (PROJECT_ROOT / "data"),
                user_id=_ctx_mode.user_id,
                event="SERVICE_STARTUP",
                timestamp=datetime.now(pytz.timezone("America/New_York")),
                project_root=PROJECT_ROOT,
                configured_mode=str((_ctx_mode.config.get("trading_control") or {}).get("mode") or "missing"),
                effective_mode=_mode_state.mode,
                live_orders_allowed=bool(_mode_state.live_orders_allowed),
                paper_orders_allowed=bool(_mode_state.mode == "paper"),
                broker_submission_allowed=bool(_mode_state.broker_orders_allowed),
                details={"config_source": str(config_path), "paper": bool(_ctx_mode.paper)},
            )
        except Exception:
            log.debug("runtime progress startup write failed user=%s", _ctx_mode.user_id, exc_info=True)
        _ctx_mode.broker = TradingControlBroker(
            _ctx_mode.broker,
            config=_ctx_mode.config,
            paper=_ctx_mode.paper,
            data_dir=_ctx_mode.data_dir or (PROJECT_ROOT / "data"),
            user_id=_ctx_mode.user_id,
        )

    log_startup_summary(user_contexts)

    # News sentiment follows the merged config in live and paper modes.

    # Use the first user's config for shared settings (calendar, intervals)
    # These are system-wide, not per-user
    first_config = user_contexts[0].config
    _log_dynamic_universe_startup_config(first_config)
    pm_runtime_cfg = log_premarket_startup_config(first_config)
    _startup_now = datetime.now(pytz.timezone("America/New_York"))
    _log_premarket_artifact_startup_validation(
        project_root=PROJECT_ROOT,
        now=_startup_now,
    )
    _startup_artifacts = _load_premarket_artifacts_into_runtime(
        engine=user_contexts[0].engine,
        project_root=PROJECT_ROOT,
        now=_startup_now,
    )
    if pm_runtime_cfg.enabled and pm_runtime_cfg.keep_alive_overnight and not _startup_artifacts:
        _startup_catchup_begin_mono = time.monotonic()
        run_premarket_scheduler_startup_catchup(
            first_config,
            _startup_now,
            project_root=PROJECT_ROOT,
            market_client=user_contexts[0].broker,
            force_jobs=["news_5am"],
        )
        _load_premarket_artifacts_into_runtime(
            engine=user_contexts[0].engine,
            project_root=PROJECT_ROOT,
            now=_startup_now,
        )
        _startup_catchup_seconds = time.monotonic() - _startup_catchup_begin_mono
        log.info(
            "STARTUP_DELAY reason=premarket_startup_catchup seconds=%.3f",
            _startup_catchup_seconds,
        )
    broker_cfg = first_config.get("broker", {})
    if broker_cfg.get("firm") != "alpaca":
        print("Config broker.firm is not 'alpaca'. Exiting.")
        notify_alpaca_loop_stopped(
            reason="init_failed",
            detail="broker.firm is not 'alpaca' (loop not started).",
        )
        sys.exit(1)

    # For backward compat: single-user uses first context's broker/engine directly
    broker = user_contexts[0].broker
    engine = user_contexts[0].engine
    mode = "PAPER" if user_contexts[0].paper else "LIVE (real money)"
    print("AlgoSphere — broker mode:", mode, flush=True)
    _log_options_startup_config(first_config, broker=broker)
    try:
        _startup_positions = broker.get_positions()
        _startup_pilot_report = broker_pilot_position_report(
            config=first_config,
            positions=_startup_positions,
            data_dir=user_contexts[0].data_dir or (PROJECT_ROOT / "data"),
            user_id=user_contexts[0].user_id,
            day=datetime.now(pytz.timezone("America/New_York")).date().isoformat(),
        )
        emit_controlled_live_equity_startup(
            first_config,
            managed_positions=int(_startup_pilot_report.get("pilot_managed_positions", 0) or 0),
        )
    except Exception:
        log.warning("CONTROLLED_LIVE_EQUITY_CONFIG unavailable", exc_info=True)
    _ca_startup = parse_capital_allocator_cfg(
        (user_contexts[0].config.get("portfolio") or {})
    )
    if _ca_startup.get("enabled"):
        print(
            "Stock entries: capital_allocator ON — allocator_input = entry_eval final-true (allowed+order) "
            "per symbol; then execute_capital_allocator_pass. Per-symbol dispatch_trend_long is disabled.",
            flush=True,
        )
    else:
        print(
            "Stock entries: per-symbol path — run_entry_gates then dispatch_trend_long (capital_allocator OFF).",
            flush=True,
        )
    if user_manager.multi_user and len(user_contexts) > 1:
        print(
            "  (Multi-user: each user merges portfolio.capital_allocator; confirm each users.yaml if needed.)",
            flush=True,
        )
    if not user_manager.multi_user:
        lk = bool(os.environ.get("ALPACA_LIVE_API_KEY_ID") and os.environ.get("ALPACA_LIVE_API_SECRET_KEY"))
        pk = bool(os.environ.get("APCA_API_KEY_ID") and os.environ.get("APCA_API_SECRET_KEY"))
        if user_contexts[0].paper:
            print("Alpaca env: APCA_* (paper) present:", pk, flush=True)
        else:
            print(
                "Alpaca env: ALPACA_LIVE_* present:",
                lk,
                "| APCA_* (paper only, not used for live):",
                pk,
                flush=True,
            )
            if not lk:
                print(
                    "  Set ALPACA_LIVE_API_KEY_ID and ALPACA_LIVE_API_SECRET_KEY from "
                    "Alpaca → Live → API Keys (paper keys will not work on live).",
                    flush=True,
                )
    calendar = MarketCalendar(first_config)
    regime_scorer = MarketRegimeScorer(first_config)
    et = pytz.timezone("America/New_York")
    exit_interval_min, entry_interval_min = resolve_live_loop_intervals(first_config)
    exit_interval_sec = exit_interval_min * 60
    ro_exit_min = reduce_only_mode_exit_interval_minutes(first_config)
    ro_exit_sec = ro_exit_min * 60
    entry_interval_sec = entry_interval_min * 60
    _dyn_ent_min_init, _dyn_ext_min_init = resolve_dynamic_momentum_intervals(first_config)
    _du_init = bool((first_config.get("dynamic_universe") or {}).get("enabled", False))
    _eff_dyn_ent_init = (
        _dyn_ent_min_init if _dyn_ent_min_init is not None else entry_interval_min
    )
    _eff_dyn_ext_init = (
        _dyn_ext_min_init if _dyn_ext_min_init is not None else exit_interval_min
    )
    _sleep_parts_init = [exit_interval_min, entry_interval_min]
    if _du_init:
        _sleep_parts_init.extend([_eff_dyn_ent_init, _eff_dyn_ext_init])
    _base_loop_sleep_min = min(_sleep_parts_init)
    _base_loop_sleep_sec = max(60, _base_loop_sleep_min * 60)
    # NOTE: In multi-user mode, tracker uses user_id-scoped files.
    # The legacy tracker_path is kept for backward compat (single-user).
    tracker_path = PROJECT_ROOT / "data" / "positions_tracked.json"
    # Per-lane entry timers (core universe vs dynamic momentum names).
    _last_core_entry_ts: dict[str, float] = {}
    _last_dynamic_entry_ts: dict[str, float] = {}
    _last_core_exit_ts: dict[str, float] = {}
    _last_dynamic_exit_ts: dict[str, float] = {}
    _last_live_risk_flatten_day: dict[str, str] = {}
    mq_cfg = first_config.get("market_quality", {})
    stale_quote_max_age = float(mq_cfg.get("stale_quote_max_age_seconds", 60))
    regime_pct_above_50d_ma = float(first_config.get("universe", {}).get("regime_min_pct_above_50d_ma", 0.30))
    ns_cfg = first_config.get("news_sentiment") or {}
    news_enabled = bool(ns_cfg.get("enabled", False))
    news_pipeline = NewsSentimentPipeline(first_config) if news_enabled else None
    news_rules = NewsRuleEngine.from_config(first_config) if news_enabled else None
    news_vol_lookback = int(ns_cfg.get("volume_lookback_days", 20))

    print(
        "Running until market close. Core: exits every %d min, entries every %d min."
        % (exit_interval_min, entry_interval_min)
        + (
            " Dynamic momentum (non-core): exits every %d min, entries every %d min."
            % (_eff_dyn_ext_init, _eff_dyn_ent_init)
            if _du_init
            else ""
        )
        + " Loop sleep ~%d min. Ctrl+C to stop." % (_base_loop_sleep_min,)
    )
    _news_summary = news_pipeline_summary()
    _news_pipeline_line = (
        "NEWS_PIPELINE articles_fetched=%d articles_after_filter=%d symbols_scored=%d"
        % (
            int(_news_summary.get("articles_fetched", 0)),
            int(_news_summary.get("articles_after_filter", 0)),
            int(_news_summary.get("symbols_scored", 0)),
        )
    )
    log.info(_news_pipeline_line)
    print(_news_pipeline_line, flush=True)
    log.info(
        "NEWS_PIPELINE_SUMMARY articles_fetched=%d articles_after_filter=%d symbols_scored=%d",
        int(_news_summary.get("articles_fetched", 0)),
        int(_news_summary.get("articles_after_filter", 0)),
        int(_news_summary.get("symbols_scored", 0)),
    )
    if news_enabled:
        _news_env_name, _news_key_loaded = _newsapi_startup_status(first_config)
        log.info(
            "NEWS_CONFIG enabled=%s key_loaded=%s provider=NewsAPI+FinBERT",
            bool(news_enabled),
            bool(_news_key_loaded),
        )
        log.info("NEWS_MODE batch")
        log.info("NEWS_MODE per_symbol disabled")
        print("NEWS_MODE batch", flush=True)
        print("NEWS_MODE per_symbol disabled", flush=True)
        if _news_key_loaded:
            print(
                "News sentiment: ON (NewsAPI + FinBERT). %s loaded."
                % _news_env_name
            )
        else:
            print(
                "News sentiment: ON (NewsAPI + FinBERT). Set %s in env."
                % _news_env_name
            )
    else:
        log.info(
            "NEWS_CONFIG enabled=%s key_loaded=%s provider=NewsAPI+FinBERT",
            False,
            False,
        )
        print("News sentiment: OFF.")
    print("-" * 50)

    loop_locks: list[UserLoopLock] = []
    if not args.no_lock:
        try:
            loop_locks = acquire_user_loop_locks(user_contexts, enabled=True)
            _locks_dir = user_contexts[0].data_dir / "locks"
            print(
                "Per-user loop lock: ON —",
                ", ".join(c.user_id for c in user_contexts),
                f"(locks: {_locks_dir})",
                flush=True,
            )
        except LoopLockError as exc:
            print(str(exc), file=sys.stderr)
            notify_alpaca_loop_stopped(reason="lock_failed", detail=str(exc))
            sys.exit(2)
    else:
        print("Per-user loop lock: OFF (--no-lock)", flush=True)

    _startup_ready_dt = datetime.now(pytz.timezone("America/New_York"))
    _startup_ready_seconds = time.monotonic() - _startup_service_begin_mono
    log.info("STARTUP_SERVICE_READY timestamp=%s", _startup_ready_dt.isoformat())
    if _startup_ready_seconds >= 1.0:
        log.info(
            "STARTUP_DELAY reason=initialization seconds=%.3f",
            _startup_ready_seconds,
        )

    _loop_stop_telegram_sent = False

    def _telegram_loop_stop(reason: str, detail: str = "") -> None:
        nonlocal _loop_stop_telegram_sent
        if _loop_stop_telegram_sent:
            return
        _loop_stop_telegram_sent = True
        notify_alpaca_loop_stopped(reason=reason, detail=detail)

    _user_ids_for_tg = [str(c.user_id) for c in user_contexts]

    _allocator_post_bulk_cooldown: dict[str, bool] = {}
    # dynamic_risk_budget: last time we logged a scheduled rebalance tick (per user_id)
    _drb_last_rebalance_ts: dict[str, float] = {}
    _news_eod_refresh_done: dict[str, date] = {}
    _last_heartbeat_monotonic: float | None = None
    _last_market_closed_intelligence_mode_log: float | None = None
    try:
        notify_alpaca_loop_started(mode_label=mode, user_ids=_user_ids_for_tg)
        while True:
            _reset_options_non_paper_log_flags()
            # while True → fetch_account → manage_positions → evaluate_entries → sleep (per user inside for)
            dt = datetime.now(et)
            now_sec = time.time()
            now_str = dt.strftime("%Y-%m-%d %H:%M:%S %Z")
            log.debug("%s — loop tick", now_str)
            log.debug(
                "%s — loop tick (~%s min sleep when in session)",
                now_str,
                _base_loop_sleep_min,
            )
            if not calendar.is_trading_allowed(dt):
                session = calendar.get_session_at(dt)
                market_closed = session == SessionType.CLOSED
                if market_closed:
                    pm_runtime_cfg = resolve_premarket_config(first_config)
                    if pm_runtime_cfg.enabled and pm_runtime_cfg.keep_alive_overnight:
                        run_due_premarket_jobs(
                            first_config,
                            dt,
                            project_root=PROJECT_ROOT,
                            market_client=user_contexts[0].broker,
                        )
                        _pm_mode_msg = "MARKET_CLOSED_INTELLIGENCE_MODE next_job=%s" % next_premarket_job(
                            first_config,
                            dt,
                        )
                        if (
                            _last_market_closed_intelligence_mode_log is None
                            or now_sec - _last_market_closed_intelligence_mode_log >= 900.0
                        ):
                            log.info(_pm_mode_msg)
                            print(_pm_mode_msg, flush=True)
                            _last_market_closed_intelligence_mode_log = now_sec
                        time.sleep(60)
                        continue
                    print(dt.strftime("%Y-%m-%d %H:%M ET"), "Market closed. Stopping.")
                    trade_date = dt.astimezone(et).date()
                    _report_log = logging.getLogger(__name__)
                    for _uctx_report in user_contexts:
                        try:
                            _br = _uctx_report.broker
                            _uid_r = _uctx_report.user_id
                            report_data = collect_daily_trading_report_data(
                                broker=_br,
                                config=_uctx_report.config,
                                trade_date=trade_date,
                            )
                            _slug_r = "".join(
                                c if c.isalnum() or c in ("-", "_") else "_" for c in str(_uid_r)
                            )
                            _report_path = PROJECT_ROOT / "reports" / f"daily_{_slug_r}.html"
                            _written = generate_report(
                                account=report_data.account,
                                positions=report_data.positions,
                                trades=report_data.trades,
                                exposure=report_data.exposure,
                                output_path=_report_path,
                                portfolio_history=report_data.portfolio_history,
                            )
                            print(
                                "[%s] — HTML dashboard %s" % (_uid_r, _written),
                                flush=True,
                            )
                            _postmortem_path = write_daily_postmortem_report(
                                report_data.trades,
                                report_date=trade_date,
                                reports_dir=PROJECT_ROOT / "reports" / "trade_postmortem",
                                user_id=str(_uid_r),
                            )
                            _report_log.info(
                                "[%s] trade postmortem report written=%s",
                                _uid_r,
                                _postmortem_path,
                            )
                            deliver_daily_report(
                                html_path=_written,
                                account=report_data.account,
                                exposure=report_data.exposure,
                                user_label=_uid_r,
                            )
                            try:
                                _json_outcomes_recorded = append_catalyst_outcomes_json(
                                    report_data.trades,
                                    user_id=str(_uid_r),
                                    observed_date=trade_date,
                                    path=PROJECT_ROOT / "data" / "analytics" / "catalyst_outcomes.json",
                                )
                                _outcomes_recorded = record_catalyst_outcomes_from_trades(
                                    _sqlite_store,
                                    user_id=str(_uid_r),
                                    trades=report_data.trades,
                                    observed_date=trade_date,
                                )
                                if _outcomes_recorded:
                                    _report_log.info(
                                        "[%s] catalyst outcomes recorded=%d",
                                        _uid_r,
                                        _outcomes_recorded,
                                    )
                                if _json_outcomes_recorded:
                                    _report_log.info(
                                        "[%s] catalyst JSON outcomes recorded=%d",
                                        _uid_r,
                                        _json_outcomes_recorded,
                                    )
                            except Exception:
                                _report_log.debug(
                                    "[%s] catalyst outcome recording failed",
                                    _uid_r,
                                    exc_info=True,
                                )
                        except Exception as _exc_r:
                            _report_log.warning(
                                "[%s] daily report failed: %s",
                                getattr(_uctx_report, "user_id", "?"),
                                _exc_r,
                            )
                    _telegram_loop_stop(
                        "market_closed",
                        "Regular session ended; loop exit after end-of-day handling.",
                    )
                    break
                print(dt.strftime("%Y-%m-%d %H:%M ET"), "Outside regular hours. Sleeping until next check.")
                time.sleep(_base_loop_sleep_sec)
                continue

            # ---- Per-user trading pass ----
            all_users_stopped = True
            did_any_entry_lane = False
            any_user_reduce_only = False
            _heartbeat_rows: list[HeartbeatUserSnapshot] = []
            for _uctx in user_contexts:
              try:
                _uid = _uctx.user_id
                broker = _uctx.broker
                engine = _uctx.engine
                config = _uctx.config
                _entry_decision_counts = {
                    "options_attempted": 0,
                    "options_selected": 0,
                    "options_ordered": 0,
                    "stock_fallback": 0,
                    "blocked_cooldown": 0,
                    "blocked_vwap": 0,
                    "blocked_option_liquidity": 0,
                }
                reset_options_cycle_stats()
                _dynamic_entry_enqueued_symbols: set[str] = set()
                _dynamic_entry_eval_started_symbols: set[str] = set()
                _dynamic_entry_eval_dropped_symbols: set[str] = set()
                _dynamic_aggressive_candidates_pending: list[dict[str, Any]] = []
                _log_options_disabled_non_paper_once(_uid, broker, config)
                _sqlite_store = get_sqlite_event_store(config)
                try:
                    setattr(broker, "_sqlite_event_store", _sqlite_store)
                    setattr(broker, "_sqlite_user_id", _uid)
                except Exception:
                    pass
                _default_sector = parse_sector_config(config)["default_sector"]

                # ---------- fetch_account() ----------
                print(dt.strftime("%H:%M ET"), "[%s] — in session, fetching account..." % _uid)
                sys.stdout.flush()
                try:
                    record_runtime_event(
                        _uctx.data_dir or (PROJECT_ROOT / "data"),
                        user_id=str(_uid),
                        event="ACCOUNT_FETCH_ATTEMPT",
                        timestamp=dt,
                        project_root=PROJECT_ROOT,
                        configured_mode=str((config.get("trading_control") or {}).get("mode") or "missing"),
                        effective_mode=str((config.get("trading_control") or {}).get("mode") or "missing"),
                        broker_submission_allowed=False,
                    )
                except Exception:
                    log.debug("runtime progress account attempt write failed user=%s", _uid, exc_info=True)
                account_equity = broker.get_equity()
                engine.update_equity(account_equity)
                engine.state.pdt.equity = account_equity
                try:
                    record_runtime_event(
                        _uctx.data_dir or (PROJECT_ROOT / "data"),
                        user_id=str(_uid),
                        event="ACCOUNT_FETCH_SUCCESS",
                        timestamp=dt,
                        project_root=PROJECT_ROOT,
                        configured_mode=str((config.get("trading_control") or {}).get("mode") or "missing"),
                        effective_mode=str((config.get("trading_control") or {}).get("mode") or "missing"),
                        broker_submission_allowed=False,
                        details={"equity_loaded": True},
                    )
                except Exception:
                    log.debug("runtime progress account success write failed user=%s", _uid, exc_info=True)

                _session_last_equity = None
                _session_cash: float | None = None
                if hasattr(broker, "get_account_snapshot"):
                    try:
                        _acct_snap_loop = broker.get_account_snapshot()
                        _le_loop = _acct_snap_loop.get("last_equity")
                        if _le_loop is not None and str(_le_loop).strip() != "":
                            _session_last_equity = float(_le_loop)
                        _c_loop = _acct_snap_loop.get("cash")
                        if _c_loop is not None and str(_c_loop).strip() != "":
                            try:
                                _session_cash = float(_c_loop)
                            except (TypeError, ValueError):
                                _session_cash = None
                    except Exception:
                        pass

                # Stop if portfolio risk says no more trading today
                can_trade, reason = engine.portfolio_risk.can_trade(
                    engine.state.portfolio_risk,
                    account_equity,
                    None,
                    dt.date(),
                    session_last_equity=_session_last_equity,
                )
                if not can_trade:
                    print(dt.strftime("%Y-%m-%d %H:%M ET"), "[%s]" % _uid, reason, "- Stopped for today.")
                    continue
                all_users_stopped = False

                _data_dir = _uctx.data_dir
                api = broker
                positions = api.get_positions()
                normalized_positions = _normalize_broker_positions(positions)
                _exposure_snapshot = compute_exposures(
                    float(account_equity),
                    normalized_positions,
                    SYMBOL_SECTOR,
                    default_sector=_default_sector,
                )
                _hb_cash = _session_cash
                if _hb_cash is None:
                    try:
                        _hb_cash = float(broker.get_buying_power())
                    except Exception:
                        _hb_cash = 0.0
                _pnl_pct_hb: float | None = None
                if _session_last_equity is not None and float(_session_last_equity) > 1e-9:
                    _pnl_pct_hb = (
                        (float(account_equity) - float(_session_last_equity))
                        / float(_session_last_equity)
                        * 100.0
                    )
                elif hasattr(broker, "get_portfolio_daily_pnl_for_date"):
                    try:
                        _dp_hb = broker.get_portfolio_daily_pnl_for_date(dt.date())
                        if _dp_hb is not None and _dp_hb.get("profit_loss_pct") is not None:
                            _pnl_pct_hb = float(_dp_hb["profit_loss_pct"])
                    except Exception:
                        pass
                _heartbeat_rows.append(
                    HeartbeatUserSnapshot(
                        user_id=str(_uid),
                        mode_label="PAPER" if _uctx.paper else "LIVE",
                        equity=float(account_equity),
                        position_count=len(normalized_positions),
                        gross_exposure_pct=float(_exposure_snapshot.gross_pct),
                        cash=float(_hb_cash),
                        pnl_today_pct=_pnl_pct_hb,
                    )
                )
                try:
                    _sqlite_store.record_portfolio_snapshot(
                        user_id=str(_uid),
                        equity=float(account_equity),
                        cash=float(_hb_cash),
                        buying_power=float(_hb_cash),
                        gross_exposure_pct=float(_exposure_snapshot.gross_pct),
                        net_exposure_pct=float(_exposure_snapshot.net_pct),
                        positions_count=len(normalized_positions),
                        payload={
                            "mode": "paper" if _uctx.paper else "live",
                            "pnl_today_pct": _pnl_pct_hb,
                        },
                        min_interval_seconds=300,
                    )
                    _sqlite_store.record_daily_performance(
                        user_id=str(_uid),
                        trading_date=dt.date(),
                        equity=float(account_equity),
                        pnl_pct=_pnl_pct_hb,
                        payload={"source": "live_heartbeat"},
                    )
                except Exception:
                    log.debug("[%s] SQLite portfolio snapshot hook failed", _uid, exc_info=True)
                _portfolio_mode_reduce_only = is_reduce_only_overexposed(
                    _exposure_snapshot.gross_pct, config
                )
                if _portfolio_mode_reduce_only:
                    any_user_reduce_only = True
                    _ec_ord = parse_risk_emergency_cancel_all_open_orders(config)
                    if bool(_ec_ord.get("enabled")):
                        try:
                            _g_thr = float(_ec_ord.get("gross_threshold", 1.2))
                        except (TypeError, ValueError):
                            _g_thr = 1.2
                        try:
                            _g_frac = float(_exposure_snapshot.gross_pct) / 100.0
                        except (TypeError, ValueError):
                            _g_frac = 0.0
                        if _g_frac > _g_thr + 1e-12:
                            _cao = getattr(broker, "cancel_all_orders", None)
                            if callable(_cao):
                                try:
                                    _cao()
                                    log.warning(
                                        "[%s] reduce_only + gross %.2fx equity (> %.2f): "
                                        "cancelled all open orders",
                                        _uid,
                                        _g_frac,
                                        _g_thr,
                                    )
                                except Exception:
                                    log.exception(
                                        "[%s] cancel_all_orders failed "
                                        "(reduce_only gross %.2fx equity)",
                                        _uid,
                                        _g_frac,
                                    )
                            else:
                                log.debug(
                                    "[%s] emergency_cancel_all_open_orders: broker "
                                    "has no cancel_all_orders — skipping",
                                    _uid,
                                )
                for p in positions:
                    sym = str(p.get("symbol") or "").strip().upper()
                    if not sym:
                        continue
                    qty_raw = int(float(p.get("qty") or 0))
                    avg_px = float(p.get("avg_price") or p.get("avg_entry_price") or 0) or 0.0
                    if avg_px <= 0 and qty_raw != 0:
                        avg_px = abs(float(p.get("cost_basis") or 0) / qty_raw)
                    reconcile_tracked(sym, qty_raw, avg_px, user_id=_uid, data_dir=_data_dir)
                tracked = load_tracked(_uid, data_dir=_data_dir)
                _prs = engine.state.portfolio_risk
                _prs.risk_counters_user_id = _uid
                _prs.risk_counters_data_dir = _data_dir

                current_positions = {
                    str(p["symbol"]).upper(): {
                        "notional": p["market_value"],
                        "stop_pct": tracked.get(str(p["symbol"]).upper(), {}).get("stop_pct", 1.5),
                    }
                    for p in positions
                }
                _uni_syms_raw = config.get("universe", {}).get("symbols", ["SPY"])
                paused = {p.upper() for p in config.get("universe", {}).get("paused_symbols", [])}
                core_symbols = [s for s in _uni_syms_raw if str(s).upper() not in paused]
                symbols = list(core_symbols)
                dynamic_symbols: list[str] = []
                dynamic_symbol_set: set[str] = set()
                _premarket_artifacts: dict[str, Any] = {}
                _premarket_injected_symbol_set: set[str] = set()
                _strong_dynamic_persistent_map: dict[str, dict[str, Any]] = {}
                _dynamic_scan_accepted_meta: dict[str, dict[str, Any]] = {}
                _dynamic_scan_selected_symbols: list[str] = []

                _runtime_dynamic_watch = {
                    str(s).strip().upper()
                    for s in (
                        getattr(engine, "dynamic_symbols", None)
                        or getattr(getattr(engine, "execution", None), "dynamic_symbols", None)
                        or []
                    )
                    if str(s).strip()
                }
                _runtime_dynamic_watch.update(
                    {
                        str(s).strip().upper()
                        for s in (
                            getattr(engine, "dynamic_news_scores", None) or {}
                        ).keys()
                        if str(s).strip()
                    }
                )
                _runtime_dynamic_watch.update(
                    {
                        str(p.get("symbol") or "").strip().upper()
                        for p in positions
                        if is_dynamic_symbol(str(p.get("symbol") or "").strip().upper(), core_symbols)
                    }
                )
                _runtime_dynamic_watch.discard("")
                _allocator_holdings_watch = getattr(engine, "allocator_holdings", None)
                if _allocator_holdings_watch is None and hasattr(engine, "execution"):
                    _allocator_holdings_watch = getattr(engine.execution, "allocator_holdings", None)
                if _allocator_holdings_watch is None:
                    _allocator_holdings_watch = {
                        str(p.get("symbol") or "").strip().upper()
                        for p in positions
                        if str(p.get("symbol") or "").strip()
                        and str(p.get("symbol") or "").strip().upper() not in core_symbols
                        and str(p.get("symbol") or "").strip().upper() not in _runtime_dynamic_watch
                    }
                else:
                    _allocator_holdings_watch = {
                        str(s).strip().upper()
                        for s in _allocator_holdings_watch
                        if str(s).strip()
                    }
                _symbol_classifications = {
                    sym: classify_symbol(
                        sym,
                        core_symbols,
                        allocator_holdings=_allocator_holdings_watch,
                        dynamic_symbols=_runtime_dynamic_watch,
                    )
                    for sym in sorted(
                        {
                            *{str(s).strip().upper() for s in symbols if str(s).strip()},
                            *{str(p.get("symbol") or "").strip().upper() for p in positions if str(p.get("symbol") or "").strip()},
                            *_runtime_dynamic_watch,
                            *_allocator_holdings_watch,
                        }
                    )
                    if sym
                }
                for _sym_cls, _cls in _symbol_classifications.items():
                    log.info("SYMBOL_CLASSIFICATION symbol=%s class=%s", _sym_cls, _cls)
                try:
                    engine.symbol_classifications = dict(_symbol_classifications)
                except Exception:
                    pass
                _news_phase_watch, _news_max_age_watch = news_refresh_phase_for_et(dt.astimezone(et))
                _news_fast_lane_watch = _news_fast_lane_interval_seconds(first_config)
                if _news_fast_lane_watch is not None:
                    _news_max_age_watch = min(
                        float(_news_max_age_watch or _news_fast_lane_watch),
                        float(_news_fast_lane_watch),
                    )
                if _news_phase_watch is not None and _runtime_dynamic_watch:
                    try:
                        log.info(
                            "NEWS_REFRESH phase=%s symbols=%d max_age_seconds=%.0f",
                            _news_phase_watch,
                            len(_runtime_dynamic_watch),
                            float(_news_max_age_watch or 0.0),
                        )
                        fetch_recent_news_catalysts(
                            broker,
                            sorted(_runtime_dynamic_watch),
                            config=config,
                            now=dt,
                            max_age_seconds=_news_max_age_watch,
                        )
                    except Exception:
                        log.debug("[%s] dynamic watchlist news refresh failed", _uid, exc_info=True)

                try:
                    dyn_cfg = dynamic_scan_cfg_with_entry_alignment(
                        config.get("dynamic_universe") or {},
                        config,
                    )
                    dyn_cfg["broker_is_paper"] = bool(getattr(_uctx, "paper", False))
                    _premarket_runtime_dynamic_allowed = True
                    _market_cfg = config.get("market") or {}
                    _open_prot = (
                        _market_cfg.get("open_protection")
                        if isinstance(_market_cfg, dict)
                        else {}
                    )
                    _open_prot = _open_prot if isinstance(_open_prot, dict) else {}
                    try:
                        _dyn_open_delay = float(
                            _open_prot.get("dynamic_scan_delay_minutes", 0) or 0
                        )
                    except (TypeError, ValueError):
                        _dyn_open_delay = 0.0
                    _dyn_open_protected = _dynamic_scan_open_protected(
                        dt,
                        enabled=bool(_open_prot.get("enabled", False)),
                        configured_delay_minutes=_dyn_open_delay,
                    )
                    if bool(dyn_cfg.get("enabled", False)):
                        _premarket_artifacts = _load_premarket_artifacts_into_runtime(
                            engine=engine,
                            project_root=PROJECT_ROOT,
                            now=dt,
                        )
                        _premarket_runtime_dynamic_allowed = _live_premarket_runtime_guard_allows_dynamic(
                            project_root=PROJECT_ROOT,
                            now=dt,
                            is_live=not bool(getattr(_uctx, "paper", False)),
                        )
                        _strong_dynamic_persistent_map = _strong_news_dynamic_persistence_map(
                            _premarket_artifacts,
                            [],
                        ) if _premarket_runtime_dynamic_allowed else {}

                    if bool(dyn_cfg.get("enabled", False)) and _premarket_runtime_dynamic_allowed:
                        if _dyn_open_protected:
                            _fastlane_symbols: list[str] = []
                            for _sym_fl, _meta_fl in _strong_dynamic_persistent_map.items():
                                _fl_allowed, _fl_reason = _dynamic_fastlane_allowed(
                                    dt,
                                    news_score=_meta_fl.get("news_score"),
                                    catalyst_age_minutes=_meta_fl.get("age_minutes"),
                                )
                                _log_dynamic_fastlane(
                                    _sym_fl,
                                    news_score=_meta_fl.get("news_score"),
                                    catalyst_age_minutes=_meta_fl.get("age_minutes"),
                                    allowed=_fl_allowed,
                                    reason=_fl_reason,
                                )
                                if _fl_allowed:
                                    _fastlane_symbols.append(_sym_fl)
                            dynamic_symbols = [
                                str(s).upper()
                                for s in _fastlane_symbols
                                if str(s).upper() not in paused and str(s).upper() not in symbols
                            ]
                            for _sym_pm in dynamic_symbols:
                                if (
                                    _sym_pm in _premarket_artifacts
                                    and _premarket_artifact_has_confirmed_metadata(
                                        _premarket_artifacts.get(_sym_pm)
                                    )
                                ):
                                    _premarket_injected_symbol_set.add(_sym_pm)
                            _dyn_news_scores = {
                                sym: int(meta.get("news_score") or 0)
                                for sym, meta in _strong_dynamic_persistent_map.items()
                            }
                            _dyn_news_headlines = {
                                sym: str(meta.get("headline") or "")
                                for sym, meta in _strong_dynamic_persistent_map.items()
                                if meta.get("headline")
                            }
                            _dyn_news_catalyst_types = {
                                sym: str(meta.get("catalyst_type") or "")
                                for sym, meta in _strong_dynamic_persistent_map.items()
                                if meta.get("catalyst_type")
                            }
                            _dyn_event_scores = dict(getattr(engine, "dynamic_event_scores", {}) or {})
                            _dyn_catalyst_scores = dict(getattr(engine, "dynamic_catalyst_scores", {}) or {})
                            _premarket_injected_rows = _inject_premarket_ranked_candidates(
                                config=config,
                                project_root=PROJECT_ROOT,
                                now=dt,
                                artifact_summary=_premarket_artifacts,
                                existing_symbols=list(symbols) + list(dynamic_symbols),
                                dynamic_symbols=dynamic_symbols,
                                paused_symbols=paused,
                            )
                            if _premarket_injected_rows:
                                for _pm_row in _premarket_injected_rows:
                                    _pm_sym = str(_pm_row.get("symbol") or "").strip().upper()
                                    if not _pm_sym:
                                        continue
                                    if _premarket_artifact_has_confirmed_metadata(
                                        _premarket_artifacts.get(_pm_sym)
                                        if isinstance(_premarket_artifacts, Mapping)
                                        else _pm_row
                                    ):
                                        _premarket_injected_symbol_set.add(_pm_sym)
                                    if _pm_sym not in dynamic_symbols:
                                        dynamic_symbols.append(_pm_sym)
                                    _dyn_news_scores[_pm_sym] = max(
                                        int(float(_dyn_news_scores.get(_pm_sym, 0) or 0)),
                                        int(math.ceil(float(_pm_row.get("news_score", 0.0) or 0.0))),
                                    )
                                    if _pm_row.get("headline"):
                                        _dyn_news_headlines[_pm_sym] = str(_pm_row.get("headline") or "")
                                    if _pm_row.get("catalyst_type"):
                                        _dyn_news_catalyst_types[_pm_sym] = str(_pm_row.get("catalyst_type") or "")
                                    _dyn_event_scores[_pm_sym] = max(
                                        float(_dyn_event_scores.get(_pm_sym, 0.0) or 0.0),
                                        float(_pm_row.get("event_score", 0.0) or 0.0),
                                    )
                                    _dyn_catalyst_scores[_pm_sym] = max(
                                        float(_dyn_catalyst_scores.get(_pm_sym, 0.0) or 0.0),
                                        float(_pm_row.get("catalyst_score", 0.0) or 0.0),
                                    )
                            if dynamic_symbols:
                                _dynamic_scan_selected_symbols = list(dynamic_symbols)
                                dynamic_symbol_set = set(dynamic_symbols)
                                _runtime_dynamic_watch.update(dynamic_symbol_set)
                                for _sym_dyn in dynamic_symbol_set:
                                    _symbol_classifications[_sym_dyn] = classify_symbol(
                                        _sym_dyn,
                                        core_symbols,
                                        allocator_holdings=_allocator_holdings_watch,
                                        dynamic_symbols=_runtime_dynamic_watch,
                                    )
                                symbols = list(dict.fromkeys(symbols + dynamic_symbols))
                                try:
                                    engine.dynamic_symbols = set(dynamic_symbols)
                                    engine.dynamic_news_scores = _dyn_news_scores
                                    engine.dynamic_news_headlines = _dyn_news_headlines
                                    engine.dynamic_news_catalyst_types = _dyn_news_catalyst_types
                                    engine.dynamic_event_scores = _dyn_event_scores
                                    engine.dynamic_catalyst_scores = _dyn_catalyst_scores
                                    if hasattr(engine, "execution") and hasattr(
                                        engine.execution, "set_dynamic_universe_symbols"
                                    ):
                                        engine.execution.set_dynamic_universe_symbols(dynamic_symbols)
                                except Exception:
                                    pass
                                print(
                                    f"DYNAMIC_UNIVERSE: base={len(core_symbols)} fastlane={dynamic_symbols} total={len(symbols)}",
                                    flush=True,
                                )
                                _dynamic_entry_projected_scan_symbols = _entry_scan_order_for_session(
                                    symbols,
                                    dynamic_symbols=dynamic_symbols,
                                    early_session=_market_open_accelerated_window_active(dt),
                                )
                                _log_dynamic_entry_scanset_debug(
                                    selected=_dynamic_scan_selected_symbols,
                                    universe_added=dynamic_symbols,
                                    entry_scan_symbols=_dynamic_entry_projected_scan_symbols,
                                )
                                _dynamic_entry_projected_scan_set = {
                                    str(s).strip().upper()
                                    for s in _dynamic_entry_projected_scan_symbols
                                    if str(s).strip()
                                }
                                _dynamic_universe_added_set = {
                                    str(s).strip().upper()
                                    for s in dynamic_symbols
                                    if str(s).strip()
                                }
                                for _sym_selected_entry in _dynamic_scan_selected_symbols:
                                    _sym_selected_u = str(_sym_selected_entry).strip().upper()
                                    if not _sym_selected_u:
                                        continue
                                    if _sym_selected_u not in _dynamic_universe_added_set:
                                        _dynamic_entry_eval_dropped_symbols.add(_sym_selected_u)
                                        _log_dynamic_entry_candidate_skipped(
                                            _sym_selected_u,
                                            reason="not_added_to_dynamic_universe",
                                        )
                                        _log_dynamic_entry_eval_dropped(
                                            _sym_selected_u,
                                            reason="not_added_to_dynamic_universe",
                                        )
                                    elif _sym_selected_u in _dynamic_entry_projected_scan_set:
                                        _dynamic_entry_enqueued_symbols.add(_sym_selected_u)
                                        _log_dynamic_entry_candidate_enqueued(
                                            _sym_selected_u,
                                            source="scanner_selected",
                                        )
                                    else:
                                        _dynamic_entry_eval_dropped_symbols.add(_sym_selected_u)
                                        _log_dynamic_entry_candidate_skipped(
                                            _sym_selected_u,
                                            reason="not_in_entry_scan",
                                        )
                                        _log_dynamic_entry_eval_dropped(
                                            _sym_selected_u,
                                            reason="not_in_entry_scan",
                                        )
                                try:
                                    _sqlite_store.record_dynamic_scan(
                                        user_id=str(_uid),
                                        selected=dynamic_symbols,
                                        payload={
                                            "status": "ok",
                                            "reason": "market_open_fastlane",
                                            "base_count": len(core_symbols),
                                            "added_count": len(dynamic_symbols),
                                            "total_count": len(symbols),
                                        },
                                    )
                                except Exception:
                                    log.debug("[%s] SQLite dynamic scan hook failed", _uid, exc_info=True)
                            else:
                                for _sym_fl, _meta_fl in _strong_dynamic_persistent_map.items():
                                    _fl_allowed, _fl_reason = _dynamic_fastlane_allowed(
                                        dt,
                                        news_score=_meta_fl.get("news_score"),
                                        catalyst_age_minutes=_meta_fl.get("age_minutes"),
                                    )
                                    _log_dynamic_fastlane(
                                        _sym_fl,
                                        news_score=_meta_fl.get("news_score"),
                                        catalyst_age_minutes=_meta_fl.get("age_minutes"),
                                        allowed=_fl_allowed,
                                        reason=_fl_reason,
                                    )
                            print(
                                dt.strftime("%H:%M ET"),
                                "DYNAMIC_SCAN skipped — market open protection (first %.0f min) open_protection_minutes=%.0f"
                                % (_dyn_open_delay, _dyn_open_delay),
                                flush=True,
                            )
                            try:
                                _sqlite_store.record_dynamic_scan(
                                    user_id=str(_uid),
                                    selected=[],
                                    payload={
                                        "status": "skipped",
                                        "reason": "market_open_protection",
                                        "delay_minutes": _dyn_open_delay,
                                        "base_count": len(core_symbols),
                                    },
                                )
                            except Exception:
                                log.debug("[%s] SQLite dynamic scan hook failed", _uid, exc_info=True)
                        else:
                            _news_phase, _news_max_age_seconds = news_refresh_phase_for_et(
                                dt.astimezone(et)
                            )
                            _news_fast_lane = _news_fast_lane_interval_seconds(first_config)
                            if _news_fast_lane is not None:
                                _news_max_age_seconds = min(
                                    float(_news_max_age_seconds or _news_fast_lane),
                                    float(_news_fast_lane),
                                )
                            if _news_phase is not None and _news_max_age_seconds is not None:
                                log.info(
                                    "NEWS_REFRESH phase=%s max_age_seconds=%.0f",
                                    _news_phase,
                                    float(_news_max_age_seconds),
                                )
                            dynamic_scan_result = scan_candidates_batch(
                                broker,
                                core_symbols,
                                dyn_cfg,
                                news_config=config,
                                news_max_age_seconds=_news_max_age_seconds,
                                premarket_artifacts=_premarket_artifacts,
                                history_user_id=str(_uid),
                                history_project_root=PROJECT_ROOT,
                            )
                            log.info(
                                "DYNAMIC_SCAN_START_END selected=%d accepted=%d rejected=%d elapsed_ms=%d",
                                len(dynamic_scan_result.selected or []),
                                len(dynamic_scan_result.accepted or []),
                                len(dynamic_scan_result.rejected or []),
                                int(getattr(dynamic_scan_result, "elapsed_ms", 0) or 0),
                            )
                            try:
                                record_runtime_event(
                                    _uctx.data_dir or (PROJECT_ROOT / "data"),
                                    user_id=str(_uid),
                                    event="SCAN_CYCLE_COMPLETED",
                                    timestamp=dt,
                                    project_root=PROJECT_ROOT,
                                    configured_mode=str((config.get("trading_control") or {}).get("mode") or "missing"),
                                    effective_mode=str((config.get("trading_control") or {}).get("mode") or "missing"),
                                    broker_submission_allowed=False,
                                    details={
                                        "scanner": "dynamic_universe",
                                        "selected": len(dynamic_scan_result.selected or []),
                                        "accepted": len(dynamic_scan_result.accepted or []),
                                        "rejected": len(dynamic_scan_result.rejected or []),
                                        "elapsed_ms": int(getattr(dynamic_scan_result, "elapsed_ms", 0) or 0),
                                    },
                                )
                            except Exception:
                                log.debug("runtime progress scan completion write failed user=%s", _uid, exc_info=True)
                            for _timing_row in list(dynamic_scan_result.accepted or []) + list(dynamic_scan_result.rejected or []):
                                _timing_sym = str(getattr(_timing_row, "symbol", "") or "").strip().upper()
                                if not _timing_sym:
                                    continue
                                _timing_quality = getattr(_timing_row, "quality", None)
                                _timing_eligible = bool(getattr(_timing_row, "accepted", False))
                                _dynamic_timing_observe_scan_candidate(
                                    symbol=_timing_sym,
                                    gain_pct=getattr(_timing_row, "day_gain_pct", 0.0),
                                    price=getattr(_timing_row, "price", 0.0),
                                    rel_volume=getattr(_timing_row, "relative_volume", 0.0),
                                    vwap_above=getattr(_timing_quality, "price_above_vwap", False)
                                    if _timing_quality is not None
                                    else False,
                                    config=config,
                                    is_live=not bool(getattr(_uctx, "paper", False)),
                                    now=dt,
                                    eligible=_timing_eligible,
                                    eligible_reason="scanner_selected"
                                    if _timing_eligible
                                    else str(getattr(_timing_row, "rejection_reason", None) or "scanner_rejected"),
                                )
                            _dynamic_scan_selected_symbols = [
                                str(s).strip().upper()
                                for s in (dynamic_scan_result.selected or [])
                                if str(s).strip()
                            ]
                            _strong_dynamic_persistent_map = _strong_news_dynamic_persistence_map(
                                _premarket_artifacts,
                                dynamic_scan_result.accepted,
                            )
                            dynamic_symbols = list(dynamic_scan_result.selected)
                            if _strong_dynamic_persistent_map:
                                dynamic_symbols = list(
                                    dict.fromkeys(
                                        dynamic_symbols
                                        + [
                                            sym
                                            for sym in _strong_dynamic_persistent_map.keys()
                                            if sym not in dynamic_symbols
                                        ]
                                    )
                                )
                            for _sym_pm in dynamic_symbols:
                                if (
                                    _sym_pm in _premarket_artifacts
                                    and _premarket_artifact_has_confirmed_metadata(
                                        _premarket_artifacts.get(_sym_pm)
                                    )
                                ):
                                    _premarket_injected_symbol_set.add(str(_sym_pm).upper())

                            dynamic_symbols = [
                                str(s).upper()
                                for s in dynamic_symbols
                                if str(s).upper() not in paused and str(s).upper() not in symbols
                            ]
                            dynamic_symbol_set = set(dynamic_symbols)
                            _runtime_dynamic_watch.update(dynamic_symbol_set)
                            for _sym_dyn in dynamic_symbol_set:
                                _symbol_classifications[_sym_dyn] = classify_symbol(
                                    _sym_dyn,
                                    core_symbols,
                                    allocator_holdings=_allocator_holdings_watch,
                                    dynamic_symbols=_runtime_dynamic_watch,
                                )
                            (
                                _dyn_news_scores,
                                _dyn_news_headlines,
                                _dyn_news_catalyst_types,
                                _dyn_event_scores,
                                _dyn_catalyst_scores,
                            ) = _dynamic_scan_runtime_score_maps(
                                dynamic_scan_result.accepted,
                                _strong_dynamic_persistent_map,
                            )
                            _dynamic_scan_accepted_meta = _dynamic_scan_accepted_metadata(
                                dynamic_scan_result.accepted
                            )
                            try:
                                _dynamic_aggressive_candidates_pending = build_dynamic_aggressive_scalp_candidates(
                                    list((_dynamic_scan_accepted_meta or {}).values()),
                                    config=config,
                                    user_id=str(_uid),
                                    current_positions=current_positions,
                                    tracked=tracked,
                                    positions=positions,
                                    open_order_symbols=_broker_open_order_symbols(broker),
                                )
                            except Exception:
                                _dynamic_aggressive_candidates_pending = []
                                log.debug("[%s] dynamic aggressive candidate build failed", _uid, exc_info=True)
                            _existing_event_scores = dict(getattr(engine, "dynamic_event_scores", {}) or {})
                            for _sym_evt, _score_evt in _dyn_event_scores.items():
                                try:
                                    _existing_event_scores[_sym_evt] = max(
                                        float(_existing_event_scores.get(_sym_evt, 0.0) or 0.0),
                                        float(_score_evt or 0.0),
                                    )
                                except (TypeError, ValueError):
                                    _existing_event_scores[_sym_evt] = float(_score_evt or 0.0)
                            _dyn_event_scores = _existing_event_scores
                            _existing_catalyst_scores = dict(getattr(engine, "dynamic_catalyst_scores", {}) or {})
                            for _sym_cat, _score_cat in _dyn_catalyst_scores.items():
                                try:
                                    _existing_catalyst_scores[_sym_cat] = max(
                                        float(_existing_catalyst_scores.get(_sym_cat, 0.0) or 0.0),
                                        float(_score_cat or 0.0),
                                    )
                                except (TypeError, ValueError):
                                    _existing_catalyst_scores[_sym_cat] = float(_score_cat or 0.0)
                            _dyn_catalyst_scores = _existing_catalyst_scores

                            _premarket_injected_rows = _inject_premarket_ranked_candidates(
                                config=config,
                                project_root=PROJECT_ROOT,
                                now=dt,
                                artifact_summary=_premarket_artifacts,
                                existing_symbols=list(symbols) + list(dynamic_symbols),
                                dynamic_symbols=dynamic_symbols,
                                paused_symbols=paused,
                            )
                            if _premarket_injected_rows:
                                for _pm_row in _premarket_injected_rows:
                                    _pm_sym = str(_pm_row.get("symbol") or "").strip().upper()
                                    if not _pm_sym:
                                        continue
                                    if _premarket_artifact_has_confirmed_metadata(
                                        _premarket_artifacts.get(_pm_sym)
                                        if isinstance(_premarket_artifacts, Mapping)
                                        else _pm_row
                                    ):
                                        _premarket_injected_symbol_set.add(_pm_sym)
                                    if _pm_sym not in dynamic_symbols:
                                        dynamic_symbols.append(_pm_sym)
                                    _dyn_news_scores[_pm_sym] = max(
                                        int(float(_dyn_news_scores.get(_pm_sym, 0) or 0)),
                                        int(math.ceil(float(_pm_row.get("news_score", 0.0) or 0.0))),
                                    )
                                    if _pm_row.get("headline"):
                                        _dyn_news_headlines[_pm_sym] = str(_pm_row.get("headline") or "")
                                    if _pm_row.get("catalyst_type"):
                                        _dyn_news_catalyst_types[_pm_sym] = str(_pm_row.get("catalyst_type") or "")
                                    _dyn_event_scores[_pm_sym] = max(
                                        float(_dyn_event_scores.get(_pm_sym, 0.0) or 0.0),
                                        float(_pm_row.get("event_score", 0.0) or 0.0),
                                    )
                                    _dyn_catalyst_scores[_pm_sym] = max(
                                        float(_dyn_catalyst_scores.get(_pm_sym, 0.0) or 0.0),
                                        float(_pm_row.get("catalyst_score", 0.0) or 0.0),
                                    )

                            dynamic_symbol_set = set(dynamic_symbols)
                            _runtime_dynamic_watch.update(dynamic_symbol_set)
                            for _sym_dyn in dynamic_symbol_set:
                                _symbol_classifications[_sym_dyn] = classify_symbol(
                                    _sym_dyn,
                                    core_symbols,
                                    allocator_holdings=_allocator_holdings_watch,
                                    dynamic_symbols=_runtime_dynamic_watch,
                                )
                            symbols = list(dict.fromkeys(symbols + dynamic_symbols))
                            try:
                                engine.dynamic_symbols = set(dynamic_symbols)
                                engine.dynamic_news_scores = _dyn_news_scores
                                engine.dynamic_news_headlines = _dyn_news_headlines
                                engine.dynamic_news_catalyst_types = _dyn_news_catalyst_types
                                engine.dynamic_event_scores = _dyn_event_scores
                                engine.dynamic_catalyst_scores = _dyn_catalyst_scores
                                if hasattr(engine, "execution") and hasattr(
                                    engine.execution, "set_dynamic_universe_symbols"
                                ):
                                    engine.execution.set_dynamic_universe_symbols(dynamic_symbols)
                            except Exception:
                                pass

                            try:
                                _maybe_scan_options_for_dynamic_candidates(
                                    broker,
                                    config,
                                    dynamic_scan_result.accepted,
                                    now=dt,
                                )
                            except Exception:
                                log.debug("[%s] dynamic options scan-only pass failed", _uid, exc_info=True)

                            print(
                                f"DYNAMIC_UNIVERSE: base={len(core_symbols)} added={dynamic_symbols} total={len(symbols)}",
                                flush=True,
                            )
                            _dynamic_entry_projected_scan_symbols = _entry_scan_order_for_session(
                                symbols,
                                dynamic_symbols=dynamic_symbols,
                                early_session=_market_open_accelerated_window_active(dt),
                            )
                            _log_dynamic_entry_scanset_debug(
                                selected=_dynamic_scan_selected_symbols,
                                universe_added=dynamic_symbols,
                                entry_scan_symbols=_dynamic_entry_projected_scan_symbols,
                            )
                            _dynamic_entry_projected_scan_set = {
                                str(s).strip().upper()
                                for s in _dynamic_entry_projected_scan_symbols
                                if str(s).strip()
                            }
                            _dynamic_universe_added_set = {
                                str(s).strip().upper()
                                for s in dynamic_symbols
                                if str(s).strip()
                            }
                            for _sym_selected_entry in _dynamic_scan_selected_symbols:
                                _sym_selected_u = str(_sym_selected_entry).strip().upper()
                                if not _sym_selected_u:
                                    continue
                                if _sym_selected_u not in _dynamic_universe_added_set:
                                    _dynamic_entry_eval_dropped_symbols.add(_sym_selected_u)
                                    _log_dynamic_entry_candidate_skipped(
                                        _sym_selected_u,
                                        reason="not_added_to_dynamic_universe",
                                    )
                                    _log_dynamic_entry_eval_dropped(
                                        _sym_selected_u,
                                        reason="not_added_to_dynamic_universe",
                                    )
                                elif _sym_selected_u in _dynamic_entry_projected_scan_set:
                                    _dynamic_entry_enqueued_symbols.add(_sym_selected_u)
                                    _log_dynamic_entry_candidate_enqueued(
                                        _sym_selected_u,
                                        source="scanner_selected",
                                    )
                                else:
                                    _dynamic_entry_eval_dropped_symbols.add(_sym_selected_u)
                                    _log_dynamic_entry_candidate_skipped(
                                        _sym_selected_u,
                                        reason="not_in_entry_scan",
                                    )
                                    _log_dynamic_entry_eval_dropped(
                                        _sym_selected_u,
                                        reason="not_in_entry_scan",
                                    )
                            try:
                                _dynamic_scan_candidates_payload = (
                                    dynamic_scan_candidates_to_dicts(dynamic_scan_result.accepted)
                                    + dynamic_scan_candidates_to_dicts(dynamic_scan_result.rejected)
                                )
                                _sqlite_store.record_dynamic_scan(
                                    user_id=str(_uid),
                                    selected=dynamic_symbols,
                                    candidates=_dynamic_scan_candidates_payload,
                                    payload={
                                        "status": "ok",
                                        "base_count": len(core_symbols),
                                        "added_count": len(dynamic_symbols),
                                        "total_count": len(symbols),
                                        "accepted_count": len(dynamic_scan_result.accepted),
                                        "rejected_count": len(dynamic_scan_result.rejected),
                                    },
                                )
                            except Exception:
                                log.debug("[%s] SQLite dynamic scan hook failed", _uid, exc_info=True)

                except Exception as e:
                    print(f"DYNAMIC_UNIVERSE: failed to refresh: {e}", flush=True)
                    try:
                        _sqlite_store.record_dynamic_scan(
                            user_id=str(_uid),
                            selected=[],
                            payload={
                                "status": "error",
                                "reason": str(e)[:300],
                                "base_count": len(core_symbols),
                            },
                        )
                    except Exception:
                        log.debug("[%s] SQLite dynamic scan error hook failed", _uid, exc_info=True)

                # Heartbeat: so you see the loop is running even when no trades
                # modes: reduce_only | normalization | normal (over_exposure: mild|high|critical|normal = band detail)
                _mode_line = portfolio_loop_mode(_exposure_snapshot.gross_pct, config)
                _tier_line = gross_exposure_tier(_exposure_snapshot.gross_pct, config)
                print(
                    dt.strftime("%H:%M ET"),
                    "— modes: %s | over_exposure: %s | equity $%.0f, book gross %.1f%% net %.1f%% of equity, checking %d symbols..."
                    % (
                        _mode_line,
                        _tier_line,
                        account_equity,
                        _exposure_snapshot.gross_pct,
                        _exposure_snapshot.net_pct,
                        len(symbols),
                    ),
                    flush=True,
                )
                sys.stdout.flush()

                _day_type_result = None
                _dt_mult_entry = 1.0
                _dt_mult_exit = 1.0
                _dt_mult_cd = 1.0
                _dt_mult_gross = 1.0
                _dt_mult_pos = 1.0
                _session_features = _session_feature_result(dt)
                _mr_dt_cfg = (config.get("market_regime") or {}).get("day_types")
                if isinstance(_mr_dt_cfg, dict) and bool(_mr_dt_cfg.get("enabled", False)):
                    try:
                        _spy_dt_u = str(regime_scorer.symbol_spy).upper()
                        _df_spy_1m_dt = broker.get_bars(
                            _spy_dt_u,
                            timeframe=TimeFrame.Minute,
                            start=(
                                _session_features["session_open"].astimezone(pytz.UTC)
                                if _session_features.get("session_open") is not None
                                else None
                            ),
                            end=dt.astimezone(pytz.UTC),
                            limit=420,
                        )
                        _vix_l_dt, _vix_p_dt = fetch_vix_context(
                            broker, regime_scorer.symbol_vix
                        )
                        _day_type_result = compute_day_type(
                            _df_spy_1m_dt,
                            vix_last=_vix_l_dt,
                            vix_prev_close=_vix_p_dt,
                            config=config,
                        )
                        _dt_mult_pos = float(_day_type_result.position_size_mult)
                        _dt_mult_entry = float(_day_type_result.entry_interval_mult)
                        _dt_mult_exit = float(_day_type_result.exit_interval_mult)
                        _dt_mult_cd = float(_day_type_result.cooldown_mult)
                        _dt_mult_gross = float(_day_type_result.gross_exposure_mult)
                        print(
                            dt.strftime("%H:%M ET"),
                            "— day type: %s | size×%.2f entry_int×%.2f exit_int×%.2f cooldown×%.2f gross×%.2f"
                            % (
                                _day_type_result.day_type,
                                _dt_mult_pos,
                                _dt_mult_entry,
                                _dt_mult_exit,
                                _dt_mult_cd,
                                _dt_mult_gross,
                            ),
                            flush=True,
                        )
                    except Exception as _exc_dt:
                        log.debug("[%s] day_type regime skipped: %s", _uid, _exc_dt)

                _bear_cfg_hb = (config.get("universe") or {}).get("bear_etfs") or {}
                _scal_hb = _bear_cfg_hb.get("controlled_scaling") or {}
                if bool(_scal_hb.get("enabled", False)):
                    _sym_hb = str(_scal_hb.get("symbol") or "SQQQ").upper()
                    _steps_hb = list(_scal_hb.get("steps") or [])
                    _row_hb = tracked.get(_sym_hb) or {}
                    _qty_hb = int(_row_hb.get("qty") or 0)
                    if _qty_hb > 0:
                        _sc_hb = int(_row_hb.get("scale_count") or 1)
                        _avg_hb = _row_hb.get("entry_price")
                        _avg_f = float(_avg_hb) if _avg_hb is not None else None
                        _lep_hb = _row_hb.get("last_entry_price")
                        if _lep_hb is None:
                            _lep_hb = _avg_hb
                        _lep_f = float(_lep_hb) if _lep_hb is not None else None
                        _bp_hb = next(
                            (
                                p
                                for p in positions
                                if str(p.get("symbol") or "").upper() == _sym_hb
                            ),
                            None,
                        )
                        _unrl = (
                            float(_bp_hb.get("unrealized_pl"))
                            if _bp_hb is not None and _bp_hb.get("unrealized_pl") is not None
                            else None
                        )
                        _log_inverse_state_line(
                            dt,
                            _sym_hb,
                            shares=_qty_hb,
                            scale_count=_sc_hb,
                            num_scale_steps=len(_steps_hb),
                            avg_entry=_avg_f,
                            last_entry=_lep_f,
                            unrealized_pnl=_unrl,
                        )

                # ---------- manage_positions() ----------
                # for pos in positions: manage_position(pos); tracker orphan cleanup
                _now_exit = time.time()
                _dyn_ent_min_u, _dyn_ext_min_u = resolve_dynamic_momentum_intervals(
                    config
                )
                _du_enabled_u = bool(
                    (config.get("dynamic_universe") or {}).get("enabled", False)
                )
                _eff_dyn_ext_sec_u = (
                    (_dyn_ext_min_u if _dyn_ext_min_u is not None else exit_interval_min)
                    * 60.0
                    * float(_dt_mult_exit)
                )
                _core_exit_sec_eff = float(exit_interval_sec) * float(_dt_mult_exit)
                _lc_ex = _last_core_exit_ts.get(_uid)
                _ld_ex = _last_dynamic_exit_ts.get(_uid)
                _core_exit_due = _lc_ex is None or (
                    _now_exit - _lc_ex
                ) >= _core_exit_sec_eff
                _dyn_exit_due = _du_enabled_u and (
                    _ld_ex is None
                    or (_now_exit - _ld_ex) >= _eff_dyn_ext_sec_u
                )
                if _core_exit_due:
                    _last_core_exit_ts[_uid] = _now_exit
                if _dyn_exit_due:
                    _last_dynamic_exit_ts[_uid] = _now_exit

                _exit_ctx = LiveExitContext(
                    user_id=_uid,
                    data_dir=_data_dir,
                    now=dt,
                    verbose=verbose,
                    broker=broker,
                    engine=engine,
                    config=config,
                    account_equity=float(account_equity),
                    symbols=symbols,
                    news_enabled=news_enabled,
                    news_pipeline=news_pipeline,
                    news_rules=news_rules,
                    exposure_snapshot=_exposure_snapshot,
                )
                _pilot_exit_classifications: dict[str, str] = {}
                try:
                    _pilot_report = broker_pilot_position_report(
                        config=config,
                        positions=positions,
                        data_dir=_data_dir,
                        user_id=str(_uid),
                        day=dt.astimezone(et).date().isoformat(),
                    )
                    _pilot_exit_classifications = pilot_exit_classification_map(_pilot_report)
                except Exception:
                    log.warning("[%s] bounded pilot position classification failed", _uid, exc_info=True)
                    _pilot_exit_classifications = {}

                _open_dynamic_news_symbols = [
                    str(p.get("symbol") or "").strip().upper()
                    for p in positions
                    if _symbol_classifications.get(
                        str(p.get("symbol") or "").strip().upper(),
                        classify_symbol(
                            str(p.get("symbol") or "").strip().upper(),
                            core_symbols,
                            allocator_holdings=_allocator_holdings_watch,
                            dynamic_symbols=_runtime_dynamic_watch,
                        ),
                    )
                    in {"CORE_WITH_DYNAMIC_SIGNAL", "DYNAMIC_ONLY"}
                ]
                _open_dynamic_news_symbols = list(dict.fromkeys(s for s in _open_dynamic_news_symbols if s))
                if _open_dynamic_news_symbols:
                    try:
                        fetch_recent_news_catalysts(
                            broker,
                            _open_dynamic_news_symbols,
                            config=config,
                            now=dt,
                            max_age_seconds=300.0,
                        )
                    except Exception:
                        log.debug("[%s] dynamic open-position news refresh failed", _uid, exc_info=True)

                _et_now = dt.astimezone(et)
                if (
                    _et_now.hour == 15
                    and _et_now.minute >= 45
                    and _news_eod_refresh_done.get(_uid) != _et_now.date()
                ):
                    try:
                        if _open_dynamic_news_symbols:
                            fetch_recent_news_catalysts(
                                broker,
                                _open_dynamic_news_symbols,
                                config=config,
                                now=dt,
                                max_age_seconds=0.0,
                                force_refresh=True,
                            )
                        _news_eod_refresh_done[_uid] = _et_now.date()
                        log.info(
                            "NEWS_EOD_REFRESH symbol_count=%d",
                            len(_open_dynamic_news_symbols),
                        )
                    except Exception:
                        log.debug("[%s] dynamic EOD news refresh failed", _uid, exc_info=True)

                def manage_position(pos: dict[str, Any]) -> None:
                    """Open-row exit pass (options + equities); run before entry logic."""
                    if is_option_position(pos):
                        if not _options_runtime_enabled(broker, config):
                            return
                        if not _core_exit_due:
                            return
                        manage_option_position(_exit_ctx, pos)
                    else:
                        _sym_p = str(pos.get("symbol") or "").strip().upper()
                        _pilot_class = _pilot_exit_classifications.get(_sym_p)
                        if _pilot_class == "PREEXISTING_ALLOWED":
                            if _core_exit_due:
                                _skip_msg = (
                                    "POSITION_MANAGER_POSITION_SKIPPED user_id=%s symbol=%s classification=PREEXISTING_ALLOWED reason=protected_preexisting_position"
                                    % (_uid, _sym_p)
                                )
                                log.info(_skip_msg)
                                print(_skip_msg, flush=True)
                                _eod_skip_msg = (
                                    "EOD_FLATTEN_SKIPPED user_id=%s symbol=%s classification=PREEXISTING_ALLOWED reason=protected_preexisting_position"
                                    % (_uid, _sym_p)
                                )
                                log.info(_eod_skip_msg)
                                print(_eod_skip_msg, flush=True)
                            return
                        if _pilot_class == "PILOT_MANAGED":
                            if not _core_exit_due:
                                return
                            evaluate_pilot_position(
                                _exit_ctx,
                                pos,
                                classification="PILOT_MANAGED",
                            )
                            return
                        _pos_class = _symbol_classifications.get(
                            _sym_p,
                            classify_symbol(
                                _sym_p,
                                core_symbols,
                                allocator_holdings=_allocator_holdings_watch,
                                dynamic_symbols=_runtime_dynamic_watch,
                            ),
                        )
                        _is_dyn_pos = _du_enabled_u and _pos_class == "DYNAMIC_ONLY"
                        if _is_dyn_pos:
                            if not _dyn_exit_due:
                                return
                        elif not _core_exit_due:
                            return
                        manage_stock_position(_exit_ctx, pos)

                if _core_exit_due:
                    _managed_count = sum(1 for _p in positions if _pilot_exit_classifications.get(str(_p.get("symbol") or "").strip().upper()) == "PILOT_MANAGED")
                    _cycle_msg = (
                        "POSITION_MANAGER_CYCLE_START user_id=%s managed_positions=%d total_broker_positions=%d exit_interval_seconds=%.1f"
                        % (_uid, _managed_count, len(positions), float(_core_exit_sec_eff))
                    )
                    log.info(_cycle_msg)
                    print(_cycle_msg, flush=True)
                    _exit_started_msg = "EXIT_CYCLE_STARTED user_id=%s managed_positions=%d" % (_uid, _managed_count)
                    log.info(_exit_started_msg)
                    print(_exit_started_msg, flush=True)
                for pos in positions:
                    manage_position(pos)
                if _core_exit_due:
                    _managed_done = sum(1 for _p in positions if _pilot_exit_classifications.get(str(_p.get("symbol") or "").strip().upper()) == "PILOT_MANAGED")
                    _cycle_end_msg = "POSITION_MANAGER_CYCLE_END user_id=%s managed_positions=%d" % (_uid, _managed_done)
                    log.info(_cycle_end_msg)
                    print(_cycle_end_msg, flush=True)
                    _exit_done_msg = "EXIT_CYCLE_COMPLETED user_id=%s" % _uid
                    log.info(_exit_done_msg)
                    print(_exit_done_msg, flush=True)

                if _shadow_live_options_active(config):
                    try:
                        _manage_shadow_option_positions(
                            broker=broker,
                            config=config,
                            user_id=_uid,
                            data_dir=_data_dir,
                            now=dt,
                            execution_manager=engine.execution,
                        )
                    except Exception:
                        log.debug("[%s] shadow option manager failed", _uid, exc_info=True)

                # ----- Tracker cleanup (orphan / zero-qty; open rows handled by manage_position) -----
                for symbol in list(tracked.keys()):
                    pos = tracked[symbol]
                    qty = int(pos.get("qty", 0))
                    if qty <= 0:
                        remove_tracked(symbol, user_id=_uid, data_dir=_data_dir)
                        _exit_ctx.notify_sqqq_tracker_removed(symbol)
                        continue
                    if not any(str(p.get("symbol") or "").upper() == symbol for p in positions):
                        remove_tracked(symbol, user_id=_uid, data_dir=_data_dir)
                        _exit_ctx.notify_sqqq_tracker_removed(symbol)
                        continue

                # Rank-based holding: full-exit longs outside top N (portfolio.rank_based_holding).
                _port_loop = config.get("portfolio")
                if (
                    (_core_exit_due or _dyn_exit_due)
                    and isinstance(_port_loop, dict)
                    and bool(
                        (_port_loop.get("rank_based_holding") or {}).get("enabled", False)
                    )
                ):
                    from src.live.rank_based_holding import maybe_sell_non_top_n_holdings

                    _bear_etf_universe = {
                        str(s).upper()
                        for s in ((config.get("universe") or {}).get("bear_etfs") or {}).get("symbols")
                        or []
                    }
                    _uni_sym_set = {str(s).upper() for s in symbols}
                    maybe_sell_non_top_n_holdings(
                        _exit_ctx,
                        positions=positions,
                        universe_symbols=_uni_sym_set,
                        bear_etf_symbols=_bear_etf_universe,
                        portfolio_cfg=_port_loop,
                        tracked=tracked,
                    )
                    tracked = load_tracked(_uid, data_dir=_data_dir)

                _options_position_snapshot = _sync_options_position_state(
                    broker=broker,
                    config=config,
                    user_id=str(_uid),
                    data_dir=_data_dir,
                    now=dt,
                    execution_manager=engine.execution,
                )

                _live_risk_guard = build_live_risk_guard_state(
                    data_dir=_data_dir,
                    user_id=str(_uid),
                    session_day=_et_now.date().isoformat(),
                    account_equity=float(account_equity),
                    positions=positions,
                    config=config,
                )
                if _live_risk_guard.triggered_guards:
                    record_guard_summary(
                        data_dir=_data_dir,
                        user_id=str(_uid),
                        day=_et_now.date().isoformat(),
                        state=_live_risk_guard,
                    )
                    log.warning(
                        "LIVE_RISK_GUARD_TRIGGERED user=%s guards=%s total_pnl=%.2f loss_pct_equity=%.4f "
                        "trend_long_blocked=%s new_entries_blocked=%s flatten_risk=%s sleeve_blocks=%s",
                        _uid,
                        ",".join(_live_risk_guard.triggered_guards),
                        float(_live_risk_guard.total_pnl),
                        float(_live_risk_guard.loss_pct_equity),
                        str(bool(_live_risk_guard.trend_long_entries_blocked)).lower(),
                        str(bool(_live_risk_guard.new_entries_blocked)).lower(),
                        str(bool(_live_risk_guard.flatten_risk)).lower(),
                        ",".join(
                            "%s:%s" % (k, v)
                            for k, v in sorted(dict(_live_risk_guard.sleeve_blocks or {}).items())
                        )
                        or "none",
                    )
                if _live_risk_guard.flatten_risk:
                    _guard_day = _et_now.date().isoformat()
                    if _last_live_risk_flatten_day.get(str(_uid)) != _guard_day:
                        _last_live_risk_flatten_day[str(_uid)] = _guard_day
                        for _risk_pos in positions:
                            _risk_sym = str(_risk_pos.get("symbol") or "").strip().upper()
                            if not _risk_sym or is_option_position(_risk_pos):
                                continue
                            try:
                                _risk_qty = abs(float(_risk_pos.get("qty") or 0.0))
                            except (TypeError, ValueError):
                                _risk_qty = 0.0
                            if _risk_qty <= 0.0:
                                continue
                            try:
                                _risk_order = submit_fractional_full_close(
                                    broker,
                                    _risk_sym,
                                    reason="intraday_loss_flatten",
                                    prefer_close_position=True,
                                )
                            except Exception:
                                log.exception(
                                    "LIVE_RISK_FLATTEN_ERROR user=%s symbol=%s reason=intraday_loss_flatten",
                                    _uid,
                                    _risk_sym,
                                )
                                continue
                            if _risk_order:
                                _exit_ctx.record_exit_action(_risk_sym)
                                _exit_ctx.note_daily_risk_order(_risk_sym, side="sell", full_exit=True)
                                _exit_ctx.log_sell_event(
                                    _risk_sym,
                                    "risk_emergency_deleverage",
                                    {
                                        "engine_reason": "intraday_loss_flatten",
                                        "qty": _risk_qty,
                                    },
                                )
                                log.warning(
                                    "LIVE_RISK_FLATTEN_SUBMITTED user=%s symbol=%s qty=%.9g reason=intraday_loss_flatten",
                                    _uid,
                                    _risk_sym,
                                    _risk_qty,
                                )

                # ---------- evaluate_entries() ----------
                # Core universe vs dynamic momentum: separate entry timers (see dynamic_universe.*_interval).
                now_sec = time.time()
                _open_accelerated_window = _market_open_accelerated_window_active(dt)
                _eff_dyn_ent_sec_u = (
                    (_dyn_ent_min_u if _dyn_ent_min_u is not None else entry_interval_min)
                    * 60.0
                    * float(_dt_mult_entry)
                )
                _entry_interval_sec_eff = float(entry_interval_sec) * float(
                    _dt_mult_entry
                )
                _eff_dyn_ent_sec_u, _entry_interval_sec_eff = _market_session_entry_cadence_seconds(
                    dt,
                    default_dynamic_seconds=_eff_dyn_ent_sec_u,
                    default_core_seconds=_entry_interval_sec_eff,
                )
                _lc_ent = _last_core_entry_ts.get(_uid)
                _ld_ent = _last_dynamic_entry_ts.get(_uid)
                do_core_entry = _lc_ent is None or (
                    now_sec - _lc_ent
                ) >= _entry_interval_sec_eff
                do_dynamic_entry = _du_enabled_u and (
                    _ld_ent is None
                    or (now_sec - _ld_ent) >= _eff_dyn_ent_sec_u
                )
                _ecfg = config.get("entries") or {}
                _entries_on = bool(
                    _ecfg.get("enable_new_entries", _ecfg.get("enabled", True))
                )
                _entry_scan_allowed = entry_scan_allowed_et(dt, _ecfg)
                _process_startup_warmup_active = _startup_no_new_entries_active(now_sec)
                try:
                    _account_state_loaded = float(account_equity) > 0
                except (TypeError, ValueError):
                    _account_state_loaded = False
                _premarket_required_for_warmup = bool(
                    (config.get("dynamic_universe") or {}).get("enabled", False)
                )
                startup_warmup_active, _startup_warmup_reason = _entry_startup_warmup_decision(
                    process_warmup_active=_process_startup_warmup_active,
                    session=calendar.get_session_at(dt),
                    account_loaded=_account_state_loaded,
                    positions_loaded=isinstance(positions, list),
                    premarket_required=_premarket_required_for_warmup,
                    premarket_loaded=bool(_premarket_artifacts),
                    local_state_loaded=isinstance(tracked, dict),
                )
                if _startup_warmup_reason == "intraday_restart":
                    log.info("STARTUP_WARMUP_SKIPPED reason=intraday_restart")
                elif startup_warmup_active and _startup_warmup_reason == "missing_state_or_premarket":
                    log.info("STARTUP_WARMUP_ACTIVE reason=missing_state_or_premarket")
                _dynamic_entry_block_reason: str | None = None
                if not _entries_on:
                    _last_core_entry_ts[_uid] = now_sec
                    _last_dynamic_entry_ts[_uid] = now_sec
                    do_core_entry = False
                    do_dynamic_entry = False
                    _dynamic_entry_block_reason = "entries_disabled"
                elif not _entry_scan_allowed:
                    log.info("ENTRY_LANE_BLOCKED reason=entry_scan_not_allowed")
                    do_core_entry = False
                    do_dynamic_entry = False
                    _dynamic_entry_block_reason = "entry_scan_not_allowed"
                else:
                    if do_core_entry and not startup_warmup_active:
                        _last_core_entry_ts[_uid] = now_sec
                    if do_dynamic_entry and not startup_warmup_active:
                        _last_dynamic_entry_ts[_uid] = now_sec

                # If dynamic scanner found names this tick, evaluate them immediately.
                # Otherwise they may wait until the next entry interval and miss the move.
                _fastlane_startup_symbols: list[str] = []
                if _du_enabled_u and dynamic_symbols:
                    _fastlane_startup_symbols = _dynamic_fastlane_startup_bypass_symbols(
                        dynamic_symbols,
                        _strong_dynamic_persistent_map,
                        dt,
                    )
                    for _sym_fl in dynamic_symbols:
                        _meta_fl = _strong_dynamic_persistent_map.get(str(_sym_fl).strip().upper(), {})
                        _fl_allowed, _fl_reason = _dynamic_fastlane_allowed(
                            dt,
                            news_score=_meta_fl.get("news_score"),
                            catalyst_age_minutes=_meta_fl.get("age_minutes"),
                        )
                        _log_dynamic_fastlane(
                            str(_sym_fl).strip().upper(),
                            news_score=_meta_fl.get("news_score"),
                            catalyst_age_minutes=_meta_fl.get("age_minutes"),
                            allowed=_fl_allowed,
                            reason=_fl_reason,
                        )
                if _du_enabled_u and dynamic_symbols and (
                    not startup_warmup_active or _fastlane_startup_symbols
                ):
                    do_dynamic_entry = True
                    _last_dynamic_entry_ts[_uid] = now_sec
                if startup_warmup_active and (do_core_entry or do_dynamic_entry):
                    if _fastlane_startup_symbols:
                        for _sym_fl in _fastlane_startup_symbols:
                            _meta_fl = _strong_dynamic_persistent_map.get(_sym_fl, {})
                            log.info(
                                "DYNAMIC_FASTLANE_BYPASS symbol=%s news_score=%s catalyst_age_minutes=%s reason=startup_warmup",
                                _sym_fl,
                                str(_meta_fl.get("news_score", "n/a")),
                                str(_meta_fl.get("age_minutes", "n/a")),
                            )
                        do_core_entry = False
                        do_dynamic_entry = True
                    else:
                        print(
                            "[live_bot] startup warmup active: blocking new entries",
                            flush=True,
                        )
                        do_core_entry = False
                        do_dynamic_entry = False
                        _dynamic_entry_block_reason = "startup_warmup"
                if _live_risk_guard.new_entries_blocked:
                    if do_core_entry or do_dynamic_entry:
                        log.warning(
                            "LIVE_RISK_ENTRY_BLOCK user=%s reason=intraday_loss_guard total_pnl=%.2f loss_pct_equity=%.4f",
                            _uid,
                            float(_live_risk_guard.total_pnl),
                            float(_live_risk_guard.loss_pct_equity),
                        )
                    do_core_entry = False
                    do_dynamic_entry = False
                    _dynamic_entry_block_reason = "intraday_loss_guard"
                elif _live_risk_guard.trend_long_entries_blocked:
                    if do_core_entry:
                        log.warning(
                            "LIVE_RISK_ENTRY_BLOCK user=%s route=trend_long reason=consecutive_live_trend_long_losses",
                            _uid,
                        )
                    do_core_entry = False

                _last_core_entry_age_sec = (
                    "none" if _lc_ent is None else f"{max(0.0, now_sec - float(_lc_ent)):.1f}"
                )
                _last_dynamic_entry_age_sec = (
                    "none" if _ld_ent is None else f"{max(0.0, now_sec - float(_ld_ent)):.1f}"
                )
                log.info(
                    "ENTRY_LANE_DECISION user=%s now_et=%s entries_on=%s entry_scan_allowed=%s "
                    "startup_warmup_active=%s startup_warmup_reason=%s process_warmup_active=%s "
                    "open_accelerated_window=%s do_core_entry=%s do_dynamic_entry=%s "
                    "last_core_entry_age_sec=%s last_dynamic_entry_age_sec=%s core_interval_sec=%.1f "
                    "dynamic_interval_sec=%.1f dynamic_symbols_count=%d fastlane_symbols_count=%d "
                    "premarket_loaded=%s account_loaded=%s positions_loaded=%s local_state_loaded=%s",
                    _uid,
                    dt.isoformat(),
                    _entries_on,
                    _entry_scan_allowed,
                    startup_warmup_active,
                    _startup_warmup_reason,
                    _process_startup_warmup_active,
                    _open_accelerated_window,
                    do_core_entry,
                    do_dynamic_entry,
                    _last_core_entry_age_sec,
                    _last_dynamic_entry_age_sec,
                    float(_entry_interval_sec_eff),
                    float(_eff_dyn_ent_sec_u),
                    len(dynamic_symbols or []),
                    len(_fastlane_startup_symbols),
                    bool(_premarket_artifacts),
                    _account_state_loaded,
                    isinstance(positions, list),
                    isinstance(tracked, dict),
                )
                if do_core_entry or do_dynamic_entry:
                    try:
                        record_runtime_event(
                            _data_dir or (PROJECT_ROOT / "data"),
                            user_id=str(_uid),
                            event="ENTRY_CYCLE_STARTED",
                            timestamp=dt,
                            project_root=PROJECT_ROOT,
                            configured_mode=str((config.get("trading_control") or {}).get("mode") or "missing"),
                            effective_mode=str((config.get("trading_control") or {}).get("mode") or "missing"),
                            broker_submission_allowed=False,
                            details={
                                "do_core_entry": bool(do_core_entry),
                                "do_dynamic_entry": bool(do_dynamic_entry),
                                "dynamic_symbols_count": len(dynamic_symbols or []),
                                "entry_scan_allowed": bool(_entry_scan_allowed),
                            },
                        )
                    except Exception:
                        log.debug("runtime progress entry start write failed user=%s", _uid, exc_info=True)
                else:
                    try:
                        record_runtime_event(
                            _data_dir or (PROJECT_ROOT / "data"),
                            user_id=str(_uid),
                            event="ENTRY_CYCLE_SKIPPED",
                            timestamp=dt,
                            project_root=PROJECT_ROOT,
                            configured_mode=str((config.get("trading_control") or {}).get("mode") or "missing"),
                            effective_mode=str((config.get("trading_control") or {}).get("mode") or "missing"),
                            broker_submission_allowed=False,
                            details={
                                "reason": _dynamic_entry_block_reason or "entry_not_due",
                                "entry_scan_allowed": bool(_entry_scan_allowed),
                                "dynamic_symbols_count": len(dynamic_symbols or []),
                            },
                        )
                    except Exception:
                        log.debug("runtime progress entry skip write failed user=%s", _uid, exc_info=True)
                if _dynamic_scan_selected_symbols and not do_dynamic_entry:
                    _skip_reason = _dynamic_entry_block_reason or "dynamic_entry_not_due"
                    for _sym_selected in _dynamic_scan_selected_symbols:
                        _sym_selected_u = str(_sym_selected).strip().upper()
                        if _sym_selected_u:
                            _dynamic_entry_eval_dropped_symbols.add(_sym_selected_u)
                        _log_dynamic_entry_candidate_skipped(
                            _sym_selected_u,
                            reason=_skip_reason,
                        )
                        _log_dynamic_entry_eval_dropped(
                            _sym_selected_u,
                            reason=_skip_reason,
                        )
                do_any_entry = do_core_entry or do_dynamic_entry
                if do_any_entry:
                    did_any_entry_lane = True

                bearish_regime = False
                pct_above_50d_universe: float | None = None
                if do_any_entry:
                    # Regime filter: if < 30% above 50D MA = bearish → bear-ETF path; else long entries
                    above_50d = 0
                    total_with_bars = 0
                    try:
                        for sym in symbols:
                            b = broker.get_bars(sym, timeframe="1Day", limit=55)
                            if b.empty or len(b) < 50:
                                continue
                            total_with_bars += 1
                            close = float(b["close"].iloc[-1])
                            ma50 = float(b["close"].rolling(50).mean().iloc[-1])
                            if close > ma50:
                                above_50d += 1
                        if total_with_bars > 0:
                            pct_above = above_50d / total_with_bars
                            pct_above_50d_universe = float(pct_above)
                            if pct_above < regime_pct_above_50d_ma:
                                bearish_regime = True
                                print(dt.strftime("%H:%M ET"), "— bearish regime: %.0f%% above 50D MA — long entries skipped (bear ETFs if breakdown)" % (pct_above * 100))
                    except Exception as e:
                        if verbose:
                            print(dt.strftime("%H:%M ET"), "— regime filter skip:", type(e).__name__, str(e)[:50])

                if do_any_entry:
                    boost_inverse_etf_priority = bool(bearish_regime)
                    _allowed_symbols_for_stock_orders = allowed_symbols_for_stock_orders_set(
                        config.get("portfolio") or {}
                    )
                    # Market regime: fetch SPY/QQQ/VIX/HYG/TLT bars for position size multiplier
                    regime_multiplier = None
                    regime_result = None
                    if regime_scorer.enabled:
                        try:
                            regime_bars = {}
                            for sym in regime_scorer.required_symbols():
                                b = broker.get_bars(sym, timeframe="1Day", limit=60)
                                if not b.empty and len(b) >= regime_scorer.ma_period_trend:
                                    regime_bars[sym] = b
                            if regime_bars:
                                regime_result = regime_scorer.compute(regime_bars)
                                regime_multiplier = regime_result.size_multiplier
                                print(dt.strftime("%H:%M ET"), "— regime score %d (%s), size mult %.2f" % (regime_result.score, regime_result.condition, regime_multiplier))
                        except Exception as e:
                            if verbose:
                                print(dt.strftime("%H:%M ET"), "— regime skip:", type(e).__name__, str(e)[:50])
                    _reg_score_bp = regime_result.score if regime_result is not None else None
                    _reg_cond_bp = regime_result.condition if regime_result is not None else None
                    bear_inv_regime_mult = regime_multiplier
                    if boost_inverse_etf_priority:
                        bear_inv_regime_mult = max(regime_multiplier, 1.0) if regime_multiplier is not None else 1.0
                    regime_entry_policy = compute_regime_entry_policy(
                        config,
                        regime_score=regime_result.score if regime_result is not None else None,
                        regime_scorer_enabled=regime_scorer.enabled,
                    )
                    _mr_ep_log = (config.get("market_regime") or {}).get("entry_policy") or {}
                    if bool(_mr_ep_log.get("enabled", True)):
                        print(
                            dt.strftime("%H:%M ET"),
                            "— regime entry policy: score=%s | SQQQ notion×%.2f | long×%.2f | SQQQ_severe=%s | long_MA_stack=%s"
                            % (
                                regime_entry_policy.score,
                                regime_entry_policy.sqqq_notional_fraction,
                                regime_entry_policy.long_notional_fraction,
                                regime_entry_policy.sqqq_requires_severe_breakdown,
                                regime_entry_policy.long_require_ma_stack,
                            ),
                            flush=True,
                        )
                    if verbose:
                        print(dt.strftime("%H:%M ET"), "Entry check: equity $%.0f, positions %d" % (account_equity, len(positions)))
                    open_orders = broker.get_open_orders()
                    open_order_symbols = {o.get("symbol", "").upper() for o in (open_orders or []) if o.get("symbol")}
                    _ex_for_full = config.get("execution")
                    _ex_for_full = _ex_for_full if isinstance(_ex_for_full, dict) else {}
                    try:
                        _n_strong_for_full = int(_ex_for_full.get("strong_signals_count", 0) or 0)
                    except (TypeError, ValueError):
                        _n_strong_for_full = 0
                    _n_strong_for_full = max(0, _n_strong_for_full)
                    try:
                        _smin_full = float(
                            _ex_for_full.get(
                                "strong_signal_strength_min",
                                _ex_for_full.get("rebalance_incoming_strength_block_min", 0.85),
                            )
                        )
                    except (TypeError, ValueError):
                        _smin_full = 0.85
                    _smin_full = max(0.0, min(1.0, _smin_full))
                    _dyn_rr_mult = dynamic_regime_strength_threshold_multiplier(
                        config
                    )
                    _entry_wave_strong_ct = 0
                    _entry_full_invest_flag = False
                    available_cash = scaled_buying_power_for_lane(
                        buying_power=broker.get_buying_power(),
                        equity=float(account_equity),
                        config=config,
                        regime_score=_reg_score_bp,
                        regime_condition=_reg_cond_bp,
                        full_invest=bool(_entry_full_invest_flag),
                        lane="stocks",
                    )
                    opts_enabled = bool((config.get("options") or {}).get("enabled"))
                    opts_allow_buy = bool(options_allow_new_entries(config))
                    if opts_enabled:
                        ou = (config.get("options") or {}).get("allowed_underlyings") or []
                        print(
                            dt.strftime("%H:%M ET"),
                            "— options: module on | new entries %s | open-position exits via exits.automation_enabled | allowed:"
                            % ("on" if opts_allow_buy else "off"),
                            ", ".join(str(x).upper() for x in ou) or "(none)",
                            flush=True,
                        )
                    ma_fast_period = engine.strategy.ma_fast
                    ma_slow_period = engine.strategy.ma_slow

                    bear_etf_universe_set = run_bear_inverse_flow(
                        BearInverseContext(
                            now=dt,
                            verbose=verbose,
                                        broker=broker,
                            engine=engine,
                            config=config,
                                        user_id=_uid,
                                        data_dir=_data_dir,
                            account_equity=float(account_equity),
                            exposure_snapshot=_exposure_snapshot,
                            allowed_symbols_for_stock_orders=_allowed_symbols_for_stock_orders,
                            open_order_symbols=open_order_symbols,
                            available_cash=available_cash,
                            stale_quote_max_age=stale_quote_max_age,
                            regime_entry_policy=regime_entry_policy,
                            regime_result=regime_result,
                            bear_inv_regime_mult=bear_inv_regime_mult,
                            bearish_regime=bearish_regime,
                            reduce_only=_portfolio_mode_reduce_only,
                        ),
                        positions,
                        tracked,
                        current_positions,
                    )

                    universe_cfg = config.get("universe", {})
                    bearish_allow_longs = bool(universe_cfg.get("bearish_allow_trend_long_entries", False))
                    bearish_max_norm = universe_cfg.get("bearish_max_normal_long_positions")
                    bearish_max_norm = int(bearish_max_norm) if bearish_max_norm is not None and str(bearish_max_norm).strip() != "" else None
                    bear_etf_set = bear_etf_universe_set

                    def _normal_long_position_count() -> int:
                        n = 0
                        for p in positions:
                            sym = p.get("symbol", "")
                            if not sym or str(sym).upper() in bear_etf_set:
                                continue
                            if int(float(p.get("qty") or 0)) > 0:
                                n += 1
                        return n

                    run_trend_long_entries = (not bearish_regime) or bearish_allow_longs
                    if run_trend_long_entries and bearish_regime and bearish_max_norm is not None:
                        n_norm = _normal_long_position_count()
                        if n_norm >= bearish_max_norm:
                            run_trend_long_entries = False
                            if verbose:
                                print(
                                    dt.strftime("%H:%M ET"),
                                    "— bearish: skip trend long entries (%d normal longs >= cap %d)" % (n_norm, bearish_max_norm),
                                )

                    if run_trend_long_entries and regime_entry_policy.long_entries_blocked:
                        run_trend_long_entries = False
                        print(
                            dt.strftime("%H:%M ET"),
                            "— regime entry policy: trend longs off (score %s)"
                            % (regime_entry_policy.score,),
                            flush=True,
                        )
                    elif run_trend_long_entries and regime_entry_policy.long_notional_fraction <= 0:
                        run_trend_long_entries = False
                        print(
                            dt.strftime("%H:%M ET"),
                            "— regime entry policy: trend longs off (long notion×0, score %s)"
                            % (regime_entry_policy.score,),
                            flush=True,
                        )

                    _scanner_dynamic_entry_lane_active = bool(_dynamic_entry_enqueued_symbols)
                    if _scanner_dynamic_entry_lane_active and not run_trend_long_entries:
                        log.info(
                            "DYNAMIC_ENTRY_DEDICATED_LANE_ACTIVE symbols=%s reason=scanner_selected",
                            ",".join(sorted(_dynamic_entry_enqueued_symbols)),
                        )

                    if run_trend_long_entries or _scanner_dynamic_entry_lane_active:
                        _hedge_ok, _hedge_reason = trend_long_hedge_requirement_ok(
                            config, positions, tracked
                        )
                        if not _hedge_ok:
                            run_trend_long_entries = False
                            print(
                                dt.strftime("%H:%M ET"),
                                "—",
                                _hedge_reason,
                                flush=True,
                            )

                    _trend_long_regime_mult = (
                        regime_multiplier if regime_multiplier is not None else 1.0
                    ) * float(regime_entry_policy.long_notional_fraction)
                    _trend_long_regime_mult *= float(_dt_mult_pos)

                    _np_cfg = (config.get("regime") or {}).get("neutral_probe")
                    _np_cfg_d = _np_cfg if isinstance(_np_cfg, dict) else None
                    _trend_long_regime_mult, _np_applied = apply_neutral_probe_size_floor(
                        _trend_long_regime_mult,
                        regime_condition=regime_result.condition
                        if regime_result is not None
                        else None,
                        probe_cfg=_np_cfg_d,
                    )
                    if _np_applied:
                        print(
                            dt.strftime("%H:%M ET"),
                            "— neutral probe: trend long size mult floored to %.2f (neutral regime)"
                            % (_trend_long_regime_mult,),
                            flush=True,
                        )

                    if run_trend_long_entries:
                        _news_override_mode = normalize_news_override_mode(
                            (config.get("news") or {}).get("override_mode", "full")
                        )
                        _port_cfg = config.get("portfolio") or {}
                        from src.dynamic_risk_budget import (
                            parse_dynamic_risk_budget,
                            rebalance_due,
                        )

                        _drb_cfg = parse_dynamic_risk_budget(config)
                        if _drb_cfg is not None and _drb_cfg.rebalance_interval_sec > 0:
                            _now_drb = time.time()
                            _prev_drb = _drb_last_rebalance_ts.get(_uid)
                            if _prev_drb is None:
                                _drb_last_rebalance_ts[_uid] = _now_drb
                            elif rebalance_due(
                                _now_drb, _prev_drb, _drb_cfg.rebalance_interval_sec
                            ):
                                log.info(
                                    "[%s] dynamic_risk_budget: scheduled rebalance tick (every %ds; "
                                    "buckets: %s)",
                                    _uid,
                                    _drb_cfg.rebalance_interval_sec,
                                    sorted(_drb_cfg.bucket_targets_pp.keys()),
                                )
                                _drb_last_rebalance_ts[_uid] = _now_drb
                        _add_on_gate_cfg = parse_add_on_gate_cfg(_port_cfg)
                        _cap_relief_cfg = parse_strong_signal_cap_relief(config)
                        _ca_cfg = parse_capital_allocator_cfg(_port_cfg)
                        _ca_cfg["news_candidates_present"] = bool(
                            _has_high_conviction_news_candidates(
                                config,
                                _premarket_artifacts,
                                getattr(engine, "dynamic_news_scores", {}) or {},
                                getattr(engine, "dynamic_event_scores", {}) or {},
                            )
                        )
                        _cap_alloc_enabled = bool(_ca_cfg["enabled"])
                        try:
                            _alloc_min_signal_strength = float(
                                _ca_cfg.get("min_signal_strength", 0.0) or 0.0
                            )
                        except (TypeError, ValueError):
                            _alloc_min_signal_strength = 0.0
                        _max_port_positions = max_portfolio_positions_from_config(_port_cfg)
                        _port_replace = bool(_port_cfg.get("enable_replacement", False))
                        _port_allow_add = bool(_port_cfg.get("allow_add", False))
                        _port_allow_add_on_strong_momentum = bool(
                            _port_cfg.get("allow_add_on_strong_momentum", False)
                        )
                        _pyramid_winners_cfg = parse_pyramid_into_winners_cfg(_port_cfg)
                        _rep_sub = _port_cfg.get("replacement")
                        _rep_sub = _rep_sub if isinstance(_rep_sub, dict) else {}
                        _rfc_cfg = parse_rebalance_free_capital_cfg(_port_cfg)
                        _max_age_raw = _rep_sub.get("max_position_age_bars")
                        if _max_age_raw is None or str(_max_age_raw).strip() == "":
                            _max_age_raw = _rep_sub.get("equal_strength_replace_after_bars")
                        _max_pos_age_bars = (
                            int(_max_age_raw)
                            if _max_age_raw is not None and str(_max_age_raw).strip() != ""
                            else None
                        )
                        _thr_raw = _rep_sub.get("replacement_threshold")
                        if _thr_raw is None or str(_thr_raw).strip() == "":
                            _thr_raw = _rep_sub.get("min_strength_diff")
                        _replacement_threshold = (
                            float(_thr_raw)
                            if _thr_raw is not None and str(_thr_raw).strip() != ""
                            else 0.0
                        )
                        _allow_equal_rep = bool(_rep_sub.get("allow_equal_replacement", False))
                        _max_rep_per_cycle = max_replacements_per_entry_cycle(_port_cfg)
                        _jit_raw = _rep_sub.get("strength_jitter_max")
                        _strength_jitter_max = (
                            float(_jit_raw)
                            if _jit_raw is not None and str(_jit_raw).strip() != ""
                            else 0.0
                        )
                        _stale_rep_raw = _rep_sub.get("replace_if_weakest_older_than_bars")
                        _replace_if_weakest_older_than = (
                            int(_stale_rep_raw)
                            if _stale_rep_raw is not None and str(_stale_rep_raw).strip() != ""
                            else None
                        )
                        _sig_rank_cfg = _port_cfg.get("signal_ranking")
                        _sig_rank_cfg = _sig_rank_cfg if isinstance(_sig_rank_cfg, dict) else {}
                        _alloc_cfg = parse_allocation_config(config)
                        _max_ranked_signals = effective_ranked_signals_cap(config)
                        _max_ranked_signals = max(_max_ranked_signals, 5)
                        _signal_ranking_active = (
                            bool(_sig_rank_cfg.get("enabled", True))
                            and _max_ranked_signals > 0
                            and alpha_rank_candidates(config)
                        )
                        _raw_sig_rank_mode = str(
                            _sig_rank_cfg.get("ranking_mode", SIGNAL_RANKING_MODE_TIER)
                        ).strip().lower()
                        _signal_ranking_mode = canonical_signal_ranking_mode(
                            _raw_sig_rank_mode,
                            allocation_rank_by_strength=bool(
                                _alloc_cfg.get("rank_by_signal_strength")
                            ),
                            allocation_rank_top_k_by=str(
                                _alloc_cfg.get("rank_top_k_by") or "strength_eff"
                            ),
                        )
                        _alpha_rm = alpha_signal_ranking_mode_override(config)
                        if _alpha_rm is not None:
                            _signal_ranking_mode = _alpha_rm
                        _sector_etfs = sector_etf_symbol_frozenset(config)
                        _event_trig_cfg = _sig_rank_cfg.get("event_triggers")
                        _event_trig_cfg = _event_trig_cfg if isinstance(_event_trig_cfg, dict) else {}
                        _atr_period_tl = int(getattr(engine.strategy, "atr_period", 14))
                        _recent_add_cfg = parse_recent_add_priority_cfg(_port_cfg)
                        _comp_w = effective_composite_weights(config)
                        _w_win_en, _w_win_n, _w_win_m = parse_winner_allocation_config(config)
                        _rcr_cfg = _port_cfg.get("ranked_capital_reallocation")
                        _rcr_cfg = _rcr_cfg if isinstance(_rcr_cfg, dict) else {}
                        _ranked_realloc_enabled = bool(_rcr_cfg.get("enabled", False))
                        try:
                            _ranked_realloc_trim_frac = float(
                                _rcr_cfg.get("trim_frac", 0.15)
                            )
                        except (TypeError, ValueError):
                            _ranked_realloc_trim_frac = 0.15

                        def _penalize_recent_if_needed(row_o: dict[str, Any], su_raw: str) -> None:
                            if not _recent_add_cfg.get("enabled"):
                                return
                            try:
                                wm = float(_recent_add_cfg.get("recent_minutes", 2880))
                            except (TypeError, ValueError):
                                wm = 2880.0
                            if wm <= 0:
                                return
                            key = str(su_raw).strip().upper()
                            if not key:
                                return
                            if not last_entry_within(key, wm, tracked=tracked, now_dt=dt):
                                return
                            try:
                                sem = float(_recent_add_cfg.get("strength_eff_multiplier", 0.72))
                            except (TypeError, ValueError):
                                sem = 0.72
                            try:
                                csm = float(_recent_add_cfg.get("composite_score_multiplier", 0.72))
                            except (TypeError, ValueError):
                                csm = 0.72
                            try:
                                extra_t = int(_recent_add_cfg.get("extra_priority_tier", 1))
                            except (TypeError, ValueError):
                                extra_t = 1
                            apply_recent_add_rank_penalty(
                                row_o,
                                is_recent_add=True,
                                strength_eff_multiplier=sem,
                                composite_score_multiplier=csm,
                                extra_priority_tier=max(0, extra_t),
                            )

                        _ranked_entry_queue: list[dict[str, Any]] = []
                        _cap_alloc_candidates: list[dict[str, Any]] = []
                        _entry_eval_exception_symbols: set[str] = set()
                        _entry_allocator_final_true: dict[str, dict[str, Any]] = {}
                        _entry_allocator_appended: set[str] = set()
                        _entry_allocator_input: set[str] = set()
                        _entry_allocator_submitted: set[str] = set()
                        _entry_allocator_skipped: set[str] = set()
                        if _dynamic_aggressive_candidates_pending:
                            _cap_alloc_candidates.extend(_dynamic_aggressive_candidates_pending)
                            for _aggr_row in _dynamic_aggressive_candidates_pending:
                                _aggr_sym = str(_aggr_row.get("symbol") or _aggr_row.get("sym_u") or "").strip().upper()
                                if _aggr_sym:
                                    _entry_allocator_appended.add(_aggr_sym)
                        def _log_allocator_reject(sym_raw: str, reason: str) -> None:
                            if not _cap_alloc_enabled:
                                return
                            sym_rej = str(sym_raw or "").strip().upper()
                            if not sym_rej:
                                return
                            _log_core_skip_reason(sym_rej, reason, core_symbols)
                            log.info(f"{sym_rej} ALLOCATOR_REJECT reason={reason}")
                            log.info(
                                "ALLOCATOR_REJECT_REASON symbol=%s reason=%s stage=live_queue",
                                sym_rej,
                                reason,
                            )
                        _tracked_keys_upper = {str(k).upper() for k in tracked}
                        _reg_score_for_scoring = (
                            int(regime_result.score)
                            if regime_result is not None and regime_result.score is not None
                            else None
                        )
                        breakout_cfg = _breakout_module_cfg(config)
                        breakout_enabled = bool(breakout_cfg.get("enabled", True))
                        _breakout_candidate_symbols: set[str] | None = None
                        breakout_candidates: list[dict[str, float | str]] = []
                        if not breakout_enabled:
                            _breakout_candidate_symbols = None
                            breakout_candidates = []
                        elif _reg_score_for_scoring is not None and _reg_score_for_scoring < 3:
                            _breakout_candidate_symbols = set()
                            print(
                                dt.strftime("%H:%M ET"),
                                "— breakout prefilter: regime score < 3, no breakout candidates",
                                flush=True,
                            )
                        elif _reg_score_for_scoring is not None:
                            try:
                                _end_utc = dt.astimezone(pytz.UTC)
                                _start_et = dt.astimezone(pytz.timezone("America/New_York")).replace(
                                    hour=9, minute=30, second=0, microsecond=0
                                )
                                _start_utc = _start_et.astimezone(pytz.UTC)
                                sector_data: dict[str, dict[str, float]] = {}
                                for sector_name, etf_symbol in SECTOR_ETFS.items():
                                    _sector_bars = broker.get_bars(
                                        etf_symbol,
                                        #timeframe=TimeFrame.Minute,
                                        timeframe=TimeFrame.Minute,
                                        start=_start_utc,
                                        end=_end_utc,
                                        limit=390,
                                    )
                                    _sector_snapshot = build_sector_snapshot(_sector_bars)
                                    if _sector_snapshot is not None:
                                        sector_data[sector_name] = _sector_snapshot

                                top_sectors = get_top_sectors(sector_data) if sector_data else []
                                breakout_universe: list[dict[str, float | str]] = []
                                for _symbol in symbols:
                                    _sector = infer_symbol_sector(_symbol)
                                    if _sector is None:
                                        continue
                                    _bars_1m = broker.get_bars(
                                        _symbol,
                                        timeframe=TimeFrame.Minute,
                                        start=_start_utc,
                                        end=_end_utc,
                                        limit=390,
                                    )
                                    _quote_bo = broker.get_latest_quote(_symbol)
                                    _spread_bo = (
                                        0.0
                                        if _quote_bo is None or _quote_bo.spread_pct is None
                                        else float(_quote_bo.spread_pct)
                                    )
                                    _snapshot_bo = build_breakout_snapshot(
                                        symbol=_symbol,
                                        sector=_sector,
                                        bars_1m=_bars_1m,
                                        spread_pct=_spread_bo,
                                    )
                                    if _snapshot_bo is not None:
                                        breakout_universe.append(_snapshot_bo)

                                breakout_candidates = find_breakouts(breakout_universe, top_sectors)
                                _breakout_candidate_symbols = {
                                    str(candidate["symbol"]).upper() for candidate in breakout_candidates
                                }
                                print(
                                    dt.strftime("%H:%M ET"),
                                    "— breakout prefilter: top sectors %s | candidates %s"
                                    % (
                                        ",".join(top_sectors) if top_sectors else "(none)",
                                        ",".join(sorted(_breakout_candidate_symbols))
                                        if _breakout_candidate_symbols
                                        else "(none)",
                                    ),
                                    flush=True,
                                )
                            except Exception as e:
                                _breakout_candidate_symbols = None
                                breakout_candidates = []
                                if verbose:
                                    print(
                                        dt.strftime("%H:%M ET"),
                                        "— breakout prefilter skip:",
                                        type(e).__name__,
                                        str(e)[:80],
                                    )

                        def _get_bars_for_scoring(sym: str):
                            return broker.get_bars(sym, timeframe="1Day", limit=220)

                        _acct_cash_hi: float | None = None
                        try:
                            _snap_hi = broker.get_account_snapshot()
                            if isinstance(_snap_hi, dict):
                                _raw_c = _snap_hi.get("cash")
                                if _raw_c is not None and str(_raw_c).strip() != "":
                                    _acct_cash_hi = float(_raw_c)
                        except Exception:
                            _acct_cash_hi = None
                        _high_cash_deploy = is_high_cash_deploy(
                            config,
                            cash=_acct_cash_hi,
                            equity=float(account_equity),
                        )
                        if verbose and _high_cash_deploy:
                            _cp = cash_pct_of_equity(
                                cash=_acct_cash_hi, equity=float(account_equity)
                            )
                            print(
                                dt.strftime("%H:%M ET"),
                                "— high cash deploy: cash %.1f%% of equity (≥ portfolio.high_cash_deploy_pct): "
                                "relaxed scoring + portfolio_brain caps"
                                % (_cp if _cp is not None else 0.0),
                                flush=True,
                            )

                        allowed_symbols = compute_scoring_allowed_symbols(
                            config,
                            symbols,
                            _get_bars_for_scoring,
                            _reg_score_for_scoring,
                            account_cash=_acct_cash_hi,
                            account_equity=float(account_equity),
                            dynamic_symbols=dynamic_symbols,
                        )
                        _universe_traded = {str(s).upper() for s in symbols}
                        _eligible_active = eligible_long_stock_symbols(
                            positions,
                            universe_symbols=_universe_traded,
                            bear_etf_symbols=bear_etf_universe_set,
                        )
                        _eligible_syms_upper = {str(s).upper() for s in _eligible_active}
                        _at_max_positions = (
                            _max_port_positions < 10**9
                            and not _port_replace
                            and len(_eligible_active) >= _max_port_positions
                        )
                        _breakout_processed_symbols: set[str] = set()
                        _breakout_symbols_held = {
                            str(sym).upper()
                            for sym, row in tracked.items()
                            if str((row or {}).get("entry_tag") or "").strip().lower() == "breakout"
                        }
                        _current_breakout_exposure = sum(
                            float((current_positions.get(sym) or {}).get("notional") or 0.0)
                            for sym in _breakout_symbols_held
                        )
                        _max_breakout_notional = _max_breakout_exposure(config, account_equity)
                        _breakout_trade_date = dt.date().isoformat()
                        _breakout_trade_count_today = int(
                            engine.state.breakout_trades_by_date.get(_breakout_trade_date, 0)
                        )
                        _breakout_max_trades = int(breakout_cfg.get("max_trades_per_day", 2) or 2)
                        _skip_breakouts = _breakout_trade_count_today >= _breakout_max_trades
                        _breakout_min_volume = float(breakout_cfg.get("min_volume", 2_000_000) or 2_000_000)
                        _breakout_min_price = float(breakout_cfg.get("min_price", 10) or 10)
                        if _at_max_positions:
                            if _port_allow_add:
                                _log_entry_skip(
                                    dt,
                                    "portfolio",
                                    "at cap (%d/%d); add-on buys only for symbols already held"
                                    % (len(_eligible_active), _max_port_positions),
                                    verbose=verbose,
                                    force=True,
                                )
                            else:
                                _log_entry_skip(
                                    dt,
                                    "portfolio",
                                    "max positions reached (%d/%d)"
                                    % (len(_eligible_active), _max_port_positions),
                                    verbose=verbose,
                                    force=True,
                                )
                        _entries_cd = config.get("entries") or {}
                        _raw_add_ratio = (config.get("entries") or {}).get(
                            "add_on_max_price_ratio_of_last_entry"
                        )
                        try:
                            _add_ratio = (
                                float(_raw_add_ratio)
                                if _raw_add_ratio is not None and str(_raw_add_ratio).strip() != ""
                                else None
                            )
                        except (TypeError, ValueError):
                            _add_ratio = None
                        _raw_max_pos_usd = (config.get("entries") or {}).get(
                            "max_position_market_value_usd", 0
                        )
                        try:
                            _max_pos_mval_usd = (
                                float(_raw_max_pos_usd)
                                if _raw_max_pos_usd is not None and str(_raw_max_pos_usd).strip() != ""
                                else 0.0
                            )
                        except (TypeError, ValueError):
                            _max_pos_mval_usd = 0.0
                        _rbt = parse_rebalance_sell_triggers(config)
                        _reb_det_gap = rebalance_signal_deterioration_min_gap(config)
                        _nsb_min_of_buy = parse_no_sell_within_min_of_buy(config)
                        _each_cycle_rebal = portfolio_rebalance_each_cycle(config) and (
                            _rbt.legacy or _rbt.allow_min_cash_target_trim
                        )
                        _cash_target_frac = min_cash_target_frac(config)
                        _rebalance_tol_pct = portfolio_rebalance_tolerance_pct(config)
                        _risk_max_adds = risk_max_adds_per_symbol_per_day(config)
                        _risk_min_add_gap = risk_min_minutes_between_adds(config)
                        _risk_max_new = risk_max_new_positions_per_cycle(
                            config,
                            regime_score=regime_result.score if regime_result is not None else None,
                        )
                        _em_delev = parse_risk_emergency_deleverage(config)
                        _bulk_pri = _em_delev.get("bulk_trim_priority")
                        _cycle_risk_state: dict[str, int] = {
                            "new_stock": 0,
                            "replacements": 0,
                        }
                        _et_date_iso = dt.strftime("%Y-%m-%d")
                        _et_day = dt.date()

                        def _live_risk_note(sym_l: str, side_l: str, full_exit_l: bool) -> None:
                            note_live_order_for_daily_risk(
                                engine, sym_l, _et_day, side=side_l, full_exit=full_exit_l
                            )
                            side_u = str(side_l or "").strip().lower()
                            if side_u not in {"sell", "buy"}:
                                return
                            tracked_live = load_tracked(_uid, data_dir=_data_dir)
                            row_live = (
                                tracked_live.get(str(sym_l or "").strip().upper())
                                if isinstance(tracked_live, dict)
                                else None
                            )
                            if not isinstance(row_live, dict):
                                return
                            if not entry_opened_same_calendar_day_et(
                                row_live.get("entry_time"),
                                dt,
                                entry_time_uncertain=bool(row_live.get("entry_time_uncertain")),
                            ):
                                return
                            engine.compliance.record_day_trade_if_applicable(
                                engine.state.pdt,
                                _et_day,
                                str(sym_l or "").strip().upper(),
                            )

                        def _log_trim_sell(sym_l: str, path: str) -> None:
                            log_sell(
                                sym_l,
                                "rebalance_trim",
                                {
                                    "user_id": _uid,
                                    "channel": "run_alpaca_loop",
                                    "path": path,
                                    "et_date": _et_day.isoformat(),
                                },
                            )
                        _add_on_momentum_bypass = bool(
                            (config.get("entries") or {}).get(
                                "add_on_allow_when_momentum_continues", False
                            )
                        )
                        _rfc_trims_done = 0
                        _gross_exposure_trims_done = 0
                        _rfc_full_exits_done = 0
                        _rfc_trimmed_symbols: set[str] = set()
                        _cycle_cash_target_trims = 0

                        def _rfc_skip_post_buy_cooldown(sym_trim: str) -> bool:
                            if _nsb_min_of_buy <= 0:
                                return False
                            if last_entry_within(
                                str(sym_trim).strip().upper(),
                                float(_nsb_min_of_buy),
                                tracked=tracked,
                                now_dt=dt,
                            ):
                                if verbose:
                                    print(
                                        dt.strftime("%H:%M ET"),
                                        "[%s] trim/sell skip — no_sell_within_min_of_buy (%.0f m after buy/scale) %s"
                                        % (
                                            _uid,
                                            float(_nsb_min_of_buy),
                                            str(sym_trim).strip().upper(),
                                        ),
                                        flush=True,
                                    )
                                return True
                            return False

                        def _after_strong_entry_candidate_for_full_invest(
                            row_tl: dict[str, Any],
                        ) -> None:
                            """Count high-strength entry rows; at ``execution.strong_signals_count`` zero the cash reserve (full-invest BP)."""
                            nonlocal available_cash, _entry_wave_strong_ct, _entry_full_invest_flag
                            if _n_strong_for_full <= 0:
                                return
                            if row_tl.get("strength_eff") is None:
                                return
                            try:
                                se = float(row_tl["strength_eff"])
                            except (TypeError, ValueError):
                                return
                            _sym_row = str(
                                row_tl.get("sym_u") or row_tl.get("symbol") or ""
                            ).strip().upper()
                            _smin_eff_fc = _smin_full
                            if (
                                _du_enabled_u
                                and _sym_row
                                and _symbol_classifications.get(
                                    _sym_row,
                                    classify_symbol(
                                        _sym_row,
                                        core_symbols,
                                        allocator_holdings=_allocator_holdings_watch,
                                        dynamic_symbols=_runtime_dynamic_watch,
                                    ),
                                )
                                == "DYNAMIC_ONLY"
                            ):
                                _smin_eff_fc = max(
                                    0.0,
                                    min(
                                        1.0,
                                        float(_smin_full) * float(_dyn_rr_mult),
                                    ),
                                )
                            if se < _smin_eff_fc - 1e-12:
                                return
                            _entry_wave_strong_ct += 1
                            _entry_full_invest_flag = _entry_wave_strong_ct >= _n_strong_for_full
                            available_cash = scaled_buying_power_for_lane(
                                buying_power=broker.get_buying_power(),
                                equity=float(account_equity),
                                config=config,
                                regime_score=_reg_score_bp,
                                regime_condition=_reg_cond_bp,
                                full_invest=bool(_entry_full_invest_flag),
                                lane="stocks",
                            )

                        def _rfc_refresh_after_sell() -> None:
                            """Reload positions, tracker, exposures, and effective BP after a RFC sell."""
                            nonlocal positions, tracked, current_positions, available_cash, _exposure_snapshot
                            positions = broker.get_positions()
                            normalized_positions = _normalize_broker_positions(positions)
                            _exposure_snapshot = compute_exposures(
                                float(account_equity),
                                normalized_positions,
                                SYMBOL_SECTOR,
                                default_sector=_default_sector,
                            )
                            for p in positions:
                                sym_r = str(p.get("symbol") or "").strip().upper()
                                if not sym_r:
                                    continue
                                qty_raw = int(float(p.get("qty") or 0))
                                avg_px = float(p.get("avg_price") or p.get("avg_entry_price") or 0) or 0.0
                                if avg_px <= 0 and qty_raw != 0:
                                    avg_px = abs(float(p.get("cost_basis") or 0) / qty_raw)
                                reconcile_tracked(sym_r, qty_raw, avg_px, user_id=_uid, data_dir=_data_dir)
                            tracked = load_tracked(_uid, data_dir=_data_dir)
                            current_positions = {
                                str(p["symbol"]).upper(): {
                                    "notional": p["market_value"],
                                    "stop_pct": tracked.get(str(p["symbol"]).upper(), {}).get("stop_pct", 1.5),
                                }
                                for p in positions
                            }
                            available_cash = scaled_buying_power_for_lane(
                                buying_power=broker.get_buying_power(),
                                equity=float(account_equity),
                                config=config,
                                regime_score=_reg_score_bp,
                                regime_condition=_reg_cond_bp,
                                full_invest=bool(_entry_full_invest_flag),
                                lane="stocks",
                            )

                        def _try_rotate_full_weakest_for_bp(
                            incoming_sym_upper: str,
                            incoming_eff_strength: float,
                            strength_cohort: list[float] | None = None,
                        ) -> bool:
                            """Sell 100% of weakest long when incoming strength > weakest (strict)."""
                            nonlocal _rfc_full_exits_done
                            if not bool(_rfc_cfg.get("rotate_full_weakest_when_stronger")):
                                return False
                            # Do not gate this on ``rebalance.trigger`` / ``allow_rfc_full_stronger_incoming``:
                            # when BP is short, explicit ``rotate_full_weakest_when_stronger`` means try
                            # ``plan_full_exit_weakest_when_stronger`` (incoming > weakest tracked strength).
                            if execution_rebalance_deferred_because_incoming_strong(
                                config,
                                incoming_strength=incoming_eff_strength,
                                strength_cohort=strength_cohort,
                            ):
                                return False
                            if _rfc_full_exits_done >= 1:
                                return False
                            plan = plan_full_exit_weakest_when_stronger(
                                tracked=tracked,
                                eligible_symbols=_eligible_active,
                                positions=positions,
                                rep_sub=_rep_sub,
                                now_dt=dt,
                                incoming_sym_upper=incoming_sym_upper,
                                incoming_signal_strength=float(incoming_eff_strength),
                                exclude_incoming_symbol=bool(_rfc_cfg["exclude_incoming_symbol"]),
                                broker=broker,
                                engine=engine,
                            )
                            if plan is None:
                                return False
                            wsym, sell_qty = plan
                            if _rfc_skip_post_buy_cooldown(wsym):
                                return False
                            _row_w = tracked.get(wsym) or {}
                            if _exit_ctx.same_day_close_blocked(wsym, _row_w):
                                return False
                            try:
                                emergency_prepare_symbol(
                                    broker, str(wsym), sleep_seconds=0
                                )
                                wquote = broker.get_latest_quote(wsym)
                                if not wquote:
                                    return False
                                _px_fb_w = rfc_fallback_open_mid_from_bars(
                                    broker, wsym
                                )
                                _mid_w = rfc_reference_mid_for_quote(
                                    wquote,
                                    fallback_1d_close=_px_fb_w,
                                    quote_mid=float(
                                        getattr(wquote, "mid", 0) or 0
                                    ),
                                )
                                w_spread = rfc_effective_spread_pct(
                                    wquote,
                                    stale_hint=True,
                                    stale_quote_max_age=stale_quote_max_age,
                                )
                                sell_order_w = build_safe_sell_order_request(
                                    broker,
                                    engine.execution,
                                    str(wsym),
                                    int(sell_qty),
                                    mid_price=float(_mid_w),
                                    spread_pct=(
                                        float(w_spread)
                                        if w_spread is not None
                                        else 0.15
                                    ),
                                    ignore_spread_gate=(
                                        wquote.skip_spread_check
                                        if wquote
                                        else False
                                    ),
                                    bid=float(wquote.bid) if wquote else None,
                                    ask=float(wquote.ask) if wquote else None,
                                    positions=positions,
                                )
                                if not sell_order_w:
                                    return False
                                if _exit_ctx.skip_exit_for_action_cap(
                                    wsym, "rebalance_free_capital_full"
                                ):
                                    return False
                                broker.submit_order(sell_order_w)
                                _live_risk_note(
                                    str(wsym).upper(), "sell", True
                                )
                                _log_trim_sell(
                                    str(wsym).upper(),
                                    "rebalance_free_capital_full",
                                )
                                _exit_ctx.record_exit_action(wsym)
                                _rfc_full_exits_done += 1
                                print(
                                    dt.strftime("%H:%M ET"),
                                    "[%s] rebalance_free_capital: SELL %s %d sh (full exit weakest; stronger incoming %s)"
                                    % (
                                        _uid,
                                        wsym,
                                        int(sell_order_w.quantity),
                                        incoming_sym_upper,
                                    ),
                                    flush=True,
                                )
                                _rfc_refresh_after_sell()
                                return True
                            except Exception as _trim_exc:
                                log.warning(
                                    "[%s] %s trim failed: %s: %s",
                                    _uid,
                                    str(wsym).upper(),
                                    type(_trim_exc).__name__,
                                    str(_trim_exc)[:200],
                                    exc_info=True,
                                )
                                return False

                        def _rfc_build_submit_notional(
                            wsym: str,
                            n_usd: float,
                            *,
                            exit_path: str,
                            log_incoming: str = "",
                        ) -> bool:
                            nonlocal _rfc_trims_done, _rfc_trimmed_symbols
                            if n_usd < 1.0:
                                return False
                            if _rfc_skip_post_buy_cooldown(wsym):
                                return False
                            _row_w0 = tracked.get(wsym) or {}
                            if _exit_ctx.same_day_close_blocked(wsym, _row_w0):
                                return False
                            wq0 = broker.get_latest_quote(wsym)
                            if not wq0:
                                return False
                            _px0 = rfc_fallback_open_mid_from_bars(broker, wsym)
                            _m0 = rfc_reference_mid_for_quote(
                                wq0,
                                fallback_1d_close=_px0,
                                quote_mid=float(getattr(wq0, "mid", 0) or 0),
                            )
                            w_sp0 = rfc_effective_spread_pct(
                                wq0,
                                stale_hint=True,
                                stale_quote_max_age=stale_quote_max_age,
                            )
                            try:
                                target_sh = int(
                                    float(n_usd) / max(float(_m0), 1e-12)
                                )
                            except (TypeError, ValueError, ZeroDivisionError):
                                return False
                            if target_sh < 1:
                                return False
                            sell0 = build_safe_sell_order_request(
                                broker,
                                engine.execution,
                                str(wsym),
                                target_sh,
                                mid_price=float(_m0),
                                spread_pct=(
                                    float(w_sp0) if w_sp0 is not None else 0.15
                                ),
                                ignore_spread_gate=(
                                    wq0.skip_spread_check if wq0 else False
                                ),
                                bid=float(wq0.bid) if wq0 else None,
                                ask=float(wq0.ask) if wq0 else None,
                                positions=positions,
                            )
                            if not sell0:
                                return False
                            if _exit_ctx.skip_exit_for_action_cap(wsym, exit_path):
                                return False
                            broker.submit_order(sell0)
                            _live_risk_note(str(wsym).upper(), "sell", False)
                            _log_trim_sell(str(wsym).upper(), exit_path)
                            _exit_ctx.record_exit_action(wsym)
                            _rfc_trims_done += 1
                            _rfc_trimmed_symbols.add(str(wsym).strip().upper())
                            _btx = _rfc_cfg.get("bulk_trim")
                            if (
                                isinstance(_btx, dict)
                                and bool(_btx.get("enabled", False))
                            ):
                                _allocator_post_bulk_cooldown[str(_uid)] = True
                                try:
                                    _cdm = float(
                                        _btx.get("buy_cooldown_minutes", 30) or 0
                                    )
                                except (TypeError, ValueError):
                                    _cdm = 0.0
                                if _cdm > 0.0:
                                    _exit_ctx.register_bulk_trim_sell(
                                        str(wsym).strip().upper(), _cdm
                                    )
                            _nshow = float(getattr(sell0, "notional", 0) or 0) or float(n_usd)
                            if exit_path == "rebalance_free_capital_trim":
                                print(
                                    dt.strftime("%H:%M ET"),
                                    "[%s] rebalance_free_capital: SELL %s notional $%.2f (bulk), incoming was %s"
                                    % (
                                        _uid,
                                        wsym,
                                        _nshow,
                                        log_incoming,
                                    ),
                                    flush=True,
                                )
                            elif exit_path == "rebalance_each_cycle_min_cash":
                                print(
                                    dt.strftime("%H:%M ET"),
                                    "[%s] rebalance_each_cycle: SELL %s notional $%.2f (bulk, min_cash lift)"
                                    % (_uid, wsym, _nshow),
                                    flush=True,
                                )
                            else:
                                print(
                                    dt.strftime("%H:%M ET"),
                                    "[%s] %s: SELL %s notional $%.2f (bulk)"
                                    % (_uid, exit_path, wsym, _nshow),
                                    flush=True,
                                )
                            _rfc_refresh_after_sell()
                            return True

                        def _rfc_trim_candidate_submit_safe(
                            sym_trim: str,
                            n_usd: float,
                            *,
                            exit_path: str,
                            log_incoming: str = "",
                        ) -> bool:
                            """
                            Per-symbol isolation: cancel blocking sells, run notional trim submit.
                            Failures log and return ``False`` without aborting the user pass.
                            """
                            try:
                                emergency_prepare_symbol(
                                    broker,
                                    str(sym_trim),
                                    sleep_seconds=0,
                                )
                                return _rfc_build_submit_notional(
                                    str(sym_trim),
                                    float(n_usd),
                                    exit_path=exit_path,
                                    log_incoming=log_incoming,
                                )
                            except Exception as _trim_exc:
                                log.warning(
                                    "[%s] %s trim failed: %s: %s",
                                    _uid,
                                    str(sym_trim).upper(),
                                    type(_trim_exc).__name__,
                                    str(_trim_exc)[:200],
                                    exc_info=True,
                                )
                                return False

                        def _rfc_top_protection() -> tuple[frozenset[str] | None, float]:
                            """
                            Top *n* long lines by notional: trim those with **lower** intensity
                            (see ``portfolio.rebalance_free_capital.top_position_protection``).
                            """
                            tpp = _rfc_cfg.get("top_position_protection")
                            if not isinstance(tpp, dict) or not bool(
                                tpp.get("enabled", True)
                            ):
                                return (None, 0.5)
                            try:
                                n = int(tpp.get("n", 3) or 0)
                            except (TypeError, ValueError):
                                n = 3
                            if n <= 0:
                                return (None, 0.5)
                            try:
                                im = float(tpp.get("intensity_mult", 0.5) or 0.5)
                            except (TypeError, ValueError):
                                im = 0.5
                            im = max(0.0, min(1.0, im))
                            if im <= 0.0:
                                im = 0.5
                            return (
                                get_top_n_positions(
                                    _eligible_active, positions, n=n
                                ),
                                im,
                            )

                        def _try_rebalance_free_capital_trim(
                            incoming_sym_upper: str,
                            *,
                            incoming_strength: float | None = None,
                            strength_cohort: list[float] | None = None,
                        ) -> bool:
                            """Sell a planned slice of the weakest eligible long to free BP; refresh state."""
                            nonlocal positions, tracked, current_positions, available_cash, _rfc_trims_done, _exposure_snapshot
                            if not _rfc_cfg["enabled"]:
                                return False
                            if not _rbt.legacy and not _rbt.allow_rfc_partial_trim:
                                return False
                            if execution_rebalance_deferred_because_incoming_strong(
                                config,
                                incoming_strength=incoming_strength,
                                strength_cohort=strength_cohort,
                            ):
                                return False
                            try:
                                _max_s = int(_rfc_cfg.get("max_trims_per_entry_scan", 1) or 1)
                            except (TypeError, ValueError):
                                _max_s = 1
                            _max_s = max(0, _max_s)
                            if _rfc_trims_done >= _max_s:
                                return False
                            _tp_set, _tp_int = _rfc_top_protection()
                            try:
                                _fp_w = max(
                                    0.0,
                                    float(
                                        _rfc_cfg.get("first_pass_winner_pnl_skip_pct", 3.0)
                                        or 0.0
                                    ),
                                )
                            except (TypeError, ValueError, OverflowError):
                                _fp_w = 3.0
                            _fp_win_arg = _fp_w if _fp_w > 1e-12 else None
                            _rfc_first_pass = _rfc_trims_done == 0
                            _btrim = _rfc_cfg.get("bulk_trim")
                            if (
                                isinstance(_btrim, dict)
                                and bool(_btrim.get("enabled", False))
                            ):
                                _plans = plan_bulk_notional_trims_for_free_capital(
                                    eligible_symbols=_eligible_active,
                                    positions=positions,
                                    tracked=tracked,
                                    rep_sub=_rep_sub,
                                    now_dt=dt,
                                    incoming_sym_upper=incoming_sym_upper,
                                    notional_per_symbol_usd=float(
                                        _btrim.get("notional_per_symbol_usd", 1500.0)
                                    ),
                                    max_symbols=int(
                                        _btrim.get("max_symbols_per_pass", 3) or 3
                                    ),
                                    exclude_incoming_symbol=bool(
                                        _rfc_cfg["exclude_incoming_symbol"]
                                    ),
                                    require_signal_deterioration=bool(
                                        (not _rbt.legacy) and _rbt.require_rfc_deterioration
                                    ),
                                    deterioration_min_gap=float(_reb_det_gap),
                                    broker=broker,
                                    engine=engine,
                                    top_positions=_tp_set,
                                    top_position_sell_intensity=_tp_int,
                                    is_first_rfc_pass=_rfc_first_pass,
                                    skip_if_unrealized_pnl_pct_above=_fp_win_arg,
                                    bulk_trim_priority=_bulk_pri,
                                )
                                if _plans:
                                    any_bulk = False
                                    for _p_sym, _p_n in _plans:
                                        if _rfc_trims_done >= _max_s:
                                            break
                                        if _rfc_trim_candidate_submit_safe(
                                            str(_p_sym),
                                            float(_p_n),
                                            exit_path="rebalance_free_capital_trim",
                                            log_incoming=incoming_sym_upper,
                                        ):
                                            any_bulk = True
                                    if any_bulk:
                                        return True
                            plan = plan_weakest_trim_for_free_capital(
                                tracked=tracked,
                                eligible_symbols=_eligible_active,
                                positions=positions,
                                rep_sub=_rep_sub,
                                now_dt=dt,
                                incoming_sym_upper=incoming_sym_upper,
                                trim_fraction=trim_fraction_by_gross_leverage(
                                    float(_exposure_snapshot.gross_pct)
                                ),
                                exclude_incoming_symbol=bool(_rfc_cfg["exclude_incoming_symbol"]),
                                broker=broker,
                                engine=engine,
                                require_signal_deterioration=bool(
                                    (not _rbt.legacy) and _rbt.require_rfc_deterioration
                                ),
                                deterioration_min_gap=float(_reb_det_gap),
                                trim_target=str(
                                    _rfc_cfg.get("trim_target", "weakest") or "weakest"
                                ),
                                largest_exposure_max_try=int(
                                    _rfc_cfg.get("largest_exposure_max_try", 3) or 3
                                ),
                                top_positions=_tp_set,
                                top_position_trim_intensity=_tp_int,
                                is_first_rfc_pass=_rfc_first_pass,
                                skip_if_unrealized_pnl_pct_above=_fp_win_arg,
                            )
                            if plan is None:
                                return False
                            wsym, sell_qty = plan
                            if _rfc_skip_post_buy_cooldown(wsym):
                                return False
                            _row_w = tracked.get(wsym) or {}
                            if _exit_ctx.same_day_close_blocked(wsym, _row_w):
                                return False
                            try:
                                emergency_prepare_symbol(
                                    broker, str(wsym), sleep_seconds=0
                                )
                                wquote = broker.get_latest_quote(wsym)
                                if not wquote:
                                    return False
                                _px_fb_w = rfc_fallback_open_mid_from_bars(
                                    broker, wsym
                                )
                                _mid_w = rfc_reference_mid_for_quote(
                                    wquote,
                                    fallback_1d_close=_px_fb_w,
                                    quote_mid=float(
                                        getattr(wquote, "mid", 0) or 0
                                    ),
                                )
                                w_spread = rfc_effective_spread_pct(
                                    wquote,
                                    stale_hint=True,
                                    stale_quote_max_age=stale_quote_max_age,
                                )
                                _pos_w2 = rfc_position_qty_floor_for_sell(
                                    int(sell_qty), str(wsym), positions
                                )
                                sell_order_w = engine.execution.build_order(
                                    wsym,
                                    "sell",
                                    sell_qty,
                                    _mid_w,
                                    float(w_spread)
                                    if w_spread is not None
                                    else 0.15,
                                    ignore_spread_gate=(
                                        wquote.skip_spread_check
                                        if wquote
                                        else False
                                    ),
                                    bid=float(wquote.bid) if wquote else None,
                                    ask=float(wquote.ask) if wquote else None,
                                    position_qty=_pos_w2,
                                )
                                if not sell_order_w:
                                    return False
                                if _exit_ctx.skip_exit_for_action_cap(
                                    wsym, "rebalance_free_capital_trim"
                                ):
                                    return False
                                broker.submit_order(sell_order_w)
                                _live_risk_note(
                                    str(wsym).upper(), "sell", False
                                )
                                _log_trim_sell(
                                    str(wsym).upper(),
                                    "rebalance_free_capital_trim",
                                )
                                _exit_ctx.record_exit_action(wsym)
                                _rfc_trims_done += 1
                                _rfc_trimmed_symbols.add(
                                    str(wsym).strip().upper()
                                )
                                _rfc_tlab = (
                                    "largest-notional"
                                    if rfc_uses_largest_exposure_notional_trim(
                                        str(
                                            _rfc_cfg.get("trim_target", "weakest")
                                            or "weakest"
                                        )
                                    )
                                    else "weakest"
                                )
                                print(
                                    dt.strftime("%H:%M ET"),
                                    "[%s] rebalance_free_capital: SELL %s %d sh (%s trim, incoming was %s)"
                                    % (
                                        _uid,
                                        wsym,
                                        int(sell_order_w.quantity),
                                        _rfc_tlab,
                                        incoming_sym_upper,
                                    ),
                                    flush=True,
                                )
                                _rfc_refresh_after_sell()
                                return True
                            except Exception as _trim_exc:
                                log.warning(
                                    "[%s] %s trim failed: %s: %s",
                                    _uid,
                                    str(wsym).upper(),
                                    type(_trim_exc).__name__,
                                    str(_trim_exc)[:200],
                                    exc_info=True,
                                )
                                return False

                        def _try_gross_exposure_cap_trim() -> bool:
                            """
                            When gross long MV %% of equity exceeds the effective max (adaptive + base,
                            no per-entry strength relief, ``cap_relax_factor`` 1.0), sell a slice of the
                            weakest eligible long (same plan/submit as rebalance_free_capital).
                            """
                            nonlocal positions, tracked, current_positions, available_cash, _exposure_snapshot, _gross_exposure_trims_done, _rfc_trims_done
                            _xeg = parse_portfolio_exposure_gates(config)
                            if not (
                                bool(_xeg["enabled"])
                                and bool(_xeg.get("force_trim_weakest_when_over_max", True))
                            ):
                                return False
                            if _gross_exposure_trims_done >= 1 or not _eligible_active:
                                return False
                            _rtrim_reg_score = (
                                int(regime_result.score)
                                if regime_result is not None
                                and regime_result.score is not None
                                else None
                            )
                            _rtrim_reg_cond = (
                                str(regime_result.condition)
                                if regime_result is not None
                                and regime_result.condition
                                else None
                            )
                            _meff = adaptive_effective_max_total_exposure(
                                config,
                                base_max_total_exposure_frac=float(_xeg["max_total_exposure_frac"]),
                                regime_score=_rtrim_reg_score,
                                regime_condition=_rtrim_reg_cond,
                                entry_wave_strong_signal_count=None,
                            )
                            _gross_over, _ = block_new_entries_total_exposure(
                                float(_exposure_snapshot.gross_pct),
                                enabled=True,
                                max_total_exposure_frac=_meff,
                                cap_relax_factor=1.0,
                            )
                            if not _gross_over:
                                return False
                            try:
                                _max_rfc_scan = int(_rfc_cfg.get("max_trims_per_entry_scan", 1))
                            except (TypeError, ValueError):
                                _max_rfc_scan = 1
                            _max_rfc_scan = max(0, _max_rfc_scan)
                            if _max_rfc_scan > 0 and _rfc_trims_done >= _max_rfc_scan:
                                return False
                            if execution_rebalance_deferred_because_incoming_strong(
                                config, incoming_strength=None, strength_cohort=None
                            ):
                                return False
                            _tp_set_g, _tp_int_g = _rfc_top_protection()
                            try:
                                _fp_wg = max(
                                    0.0,
                                    float(
                                        _rfc_cfg.get("first_pass_winner_pnl_skip_pct", 3.0)
                                        or 0.0
                                    ),
                                )
                            except (TypeError, ValueError, OverflowError):
                                _fp_wg = 3.0
                            _fp_win_g = _fp_wg if _fp_wg > 1e-12 else None
                            _rfc_first_g = _rfc_trims_done == 0

                            _tpg = _rfc_cfg.get("two_phase_gross_cap_unwind")
                            if isinstance(_tpg, dict) and bool(_tpg.get("enabled", False)):
                                try:
                                    _p1f_tp = float(
                                        _tpg.get("phase1_weakest_trim_fraction", 0.5)
                                        or 0.5
                                    )
                                except (TypeError, ValueError, OverflowError):
                                    _p1f_tp = 0.5
                                try:
                                    _p3max = int(
                                        _tpg.get("proportional_max_submits", 25) or 25
                                    )
                                except (TypeError, ValueError):
                                    _p3max = 25
                                _p3max = max(1, min(200, _p3max))
                                _max_rfc_gross = max(
                                    _max_rfc_scan, 2 + int(_p3max)
                                )

                                def _gross_cap_meff_tp() -> float:
                                    return float(_meff)

                                def _gross_still_over_cap_tp() -> bool:
                                    ovr, _x = block_new_entries_total_exposure(
                                        float(_exposure_snapshot.gross_pct),
                                        enabled=True,
                                        max_total_exposure_frac=_gross_cap_meff_tp(),
                                        cap_relax_factor=1.0,
                                    )
                                    return bool(ovr)

                                def _submit_gross_unwind_shares(
                                    wsg: str,
                                    sqty: int,
                                    *,
                                    print_tag: str,
                                ) -> bool:
                                    nonlocal _rfc_trims_done, _rfc_trimmed_symbols
                                    wsg = str(wsg or "").strip().upper()
                                    if not wsg or sqty < 1:
                                        return False
                                    if _rfc_skip_post_buy_cooldown(wsg):
                                        return False
                                    _rwg = tracked.get(wsg) or {}
                                    if _exit_ctx.same_day_close_blocked(wsg, _rwg):
                                        return False
                                    wq_tp = broker.get_latest_quote(wsg)
                                    if not wq_tp:
                                        return False
                                    _pfb_tp = rfc_fallback_open_mid_from_bars(
                                        broker, wsg
                                    )
                                    _md_tp = rfc_reference_mid_for_quote(
                                        wq_tp,
                                        fallback_1d_close=_pfb_tp,
                                        quote_mid=float(
                                            getattr(wq_tp, "mid", 0) or 0
                                        ),
                                    )
                                    w_sp_tp = rfc_effective_spread_pct(
                                        wq_tp,
                                        stale_hint=True,
                                        stale_quote_max_age=stale_quote_max_age,
                                    )
                                    _pos_tp = rfc_position_qty_floor_for_sell(
                                        int(sqty), str(wsg), positions
                                    )
                                    sell_tp = engine.execution.build_order(
                                        wsg,
                                        "sell",
                                        sqty,
                                        _md_tp,
                                        float(w_sp_tp) if w_sp_tp is not None else 0.15,
                                        ignore_spread_gate=wq_tp.skip_spread_check
                                        if wq_tp
                                        else False,
                                        bid=float(wq_tp.bid) if wq_tp else None,
                                        ask=float(wq_tp.ask) if wq_tp else None,
                                        position_qty=_pos_tp,
                                    )
                                    if not sell_tp:
                                        return False
                                    if _exit_ctx.skip_exit_for_action_cap(
                                        wsg, "exposure_gross_cap_trim"
                                    ):
                                        return False
                                    broker.submit_order(sell_tp)
                                    _live_risk_note(
                                        str(wsg).upper(), "sell", False
                                    )
                                    _log_trim_sell(
                                        str(wsg).upper(), "exposure_gross_cap_trim"
                                    )
                                    _exit_ctx.record_exit_action(wsg)
                                    _rfc_trims_done += 1
                                    _rfc_trimmed_symbols.add(
                                        str(wsg).strip().upper()
                                    )
                                    print(
                                        dt.strftime("%H:%M ET"),
                                        "[%s] two_phase_gross_unwind: SELL %s %d sh (%s; book %.1f%%, cap eff. %.0f%%)"
                                        % (
                                            _uid,
                                            wsg,
                                            int(sell_tp.quantity),
                                            print_tag,
                                            float(
                                                _exposure_snapshot.gross_pct
                                            ),
                                            float(_meff) * 100.0,
                                        ),
                                        flush=True,
                                    )
                                    _rfc_refresh_after_sell()
                                    return True

                                _tpu_any = False
                                if _rfc_trims_done < _max_rfc_gross:
                                    pl_tp = plan_weakest_gross_unwind_phase1(
                                        tracked=tracked,
                                        eligible_symbols=_eligible_active,
                                        positions=positions,
                                        rep_sub=_rep_sub,
                                        now_dt=dt,
                                        incoming_sym_upper="",
                                        phase1_weakest_trim_fraction=float(
                                            _p1f_tp
                                        ),
                                        exclude_incoming_symbol=bool(
                                            _rfc_cfg["exclude_incoming_symbol"]
                                        ),
                                        broker=broker,
                                        engine=engine,
                                    )
                                    if pl_tp:
                                        _ws, _q = pl_tp
                                        if _submit_gross_unwind_shares(
                                            _ws,
                                            int(_q),
                                            print_tag="phase-1 weakest slice",
                                        ):
                                            _tpu_any = True
                                if (
                                    _gross_still_over_cap_tp()
                                    and _rfc_trims_done < _max_rfc_gross
                                ):
                                    p2_plan = plan_full_exit_weakest_for_gross_delever(
                                        tracked=tracked,
                                        eligible_symbols=_eligible_active,
                                        positions=positions,
                                        rep_sub=_rep_sub,
                                        now_dt=dt,
                                        incoming_sym_upper="",
                                        exclude_incoming_symbol=bool(
                                            _rfc_cfg["exclude_incoming_symbol"]
                                        ),
                                        broker=broker,
                                        engine=engine,
                                    )
                                    if p2_plan:
                                        w2, q2 = p2_plan
                                        if _submit_gross_unwind_shares(
                                            w2,
                                            int(q2),
                                            print_tag="phase-2 sell all weakest",
                                        ):
                                            _tpu_any = True
                                if (
                                    _gross_still_over_cap_tp()
                                    and _rfc_trims_done < _max_rfc_gross
                                ):
                                    _tgt_pct = float(_meff) * 100.0
                                    _pls3 = plan_proportional_gross_delever_notional_trims(
                                        eligible_symbols=_eligible_active,
                                        positions=positions,
                                        tracked=tracked,
                                        rep_sub=_rep_sub,
                                        now_dt=dt,
                                        incoming_sym_upper="",
                                        exclude_incoming_symbol=bool(
                                            _rfc_cfg["exclude_incoming_symbol"]
                                        ),
                                        current_gross_pct=float(
                                            _exposure_snapshot.gross_pct
                                        ),
                                        target_gross_pct=_tgt_pct,
                                        account_equity=float(account_equity),
                                        max_submits=int(_p3max),
                                        get_bars=_get_bars_for_scoring,
                                        engine=engine,
                                    )
                                    for _g3s, _g3n in _pls3 or []:
                                        if not _gross_still_over_cap_tp():
                                            break
                                        if _rfc_trims_done >= _max_rfc_gross:
                                            break
                                        if _rfc_trim_candidate_submit_safe(
                                            str(_g3s),
                                            float(_g3n),
                                            exit_path="exposure_gross_cap_trim",
                                        ):
                                            _tpu_any = True
                                            _rfc_refresh_after_sell()
                                if _tpu_any:
                                    _gross_exposure_trims_done += 1
                                    return True
                                return False

                            _btr_g = _rfc_cfg.get("bulk_trim")
                            if (
                                isinstance(_btr_g, dict)
                                and bool(_btr_g.get("enabled", False))
                            ):
                                _gross_mult = max(
                                    0.0,
                                    float(_exposure_snapshot.gross_pct) / 100.0,
                                )
                                _em_tr = _em_delev.get(
                                    "emergency_deleverage_trigger"
                                )
                                _em_pc = _em_delev.get(
                                    "emergency_deleverage_pct"
                                )
                                _use_risk_emergency = (
                                    _em_tr is not None
                                    and _em_pc is not None
                                    and float(_em_pc) > 1e-12
                                    and _gross_mult >= float(_em_tr) - 1e-12
                                )
                                if _use_risk_emergency:
                                    _pls_g = plan_emergency_deleverage_portfolio_pct_trims(
                                        eligible_symbols=_eligible_active,
                                        positions=positions,
                                        tracked=tracked,
                                        rep_sub=_rep_sub,
                                        now_dt=dt,
                                        incoming_sym_upper="",
                                        portfolio_trim_pct=float(_em_pc),
                                        max_symbols=int(
                                            _btr_g.get(
                                                "max_symbols_per_pass", 3
                                            )
                                            or 3
                                        ),
                                        exclude_incoming_symbol=bool(
                                            _rfc_cfg["exclude_incoming_symbol"]
                                        ),
                                        bulk_trim_priority=_bulk_pri,
                                        require_signal_deterioration=False,
                                        broker=broker,
                                        engine=engine,
                                        top_positions=_tp_set_g,
                                        top_position_sell_intensity=_tp_int_g,
                                        is_first_rfc_pass=_rfc_first_g,
                                        skip_if_unrealized_pnl_pct_above=_fp_win_g,
                                    )
                                    _gg_exit = "risk_emergency_deleverage"
                                else:
                                    _em_trim_usd = emergency_bulk_trim_notional_usd(
                                        float(account_equity), _gross_mult
                                    )
                                    _pls_g = plan_bulk_notional_trims_for_free_capital(
                                        eligible_symbols=_eligible_active,
                                        positions=positions,
                                        tracked=tracked,
                                        rep_sub=_rep_sub,
                                        now_dt=dt,
                                        incoming_sym_upper="",
                                        notional_per_symbol_usd=float(
                                            _em_trim_usd
                                        ),
                                        max_symbols=int(
                                            _btr_g.get(
                                                "max_symbols_per_pass", 3
                                            )
                                            or 3
                                        ),
                                        exclude_incoming_symbol=bool(
                                            _rfc_cfg["exclude_incoming_symbol"]
                                        ),
                                        require_signal_deterioration=False,
                                        broker=broker,
                                        engine=engine,
                                        top_positions=_tp_set_g,
                                        top_position_sell_intensity=_tp_int_g,
                                        is_first_rfc_pass=_rfc_first_g,
                                        skip_if_unrealized_pnl_pct_above=_fp_win_g,
                                        bulk_trim_priority=_bulk_pri,
                                    )
                                    _gg_exit = "exposure_gross_cap_trim"
                                if _pls_g:
                                    any_gg = False
                                    for _g_sym, _g_n in _pls_g:
                                        if _rfc_trims_done >= _max_rfc_scan:
                                            break
                                        if _rfc_trim_candidate_submit_safe(
                                            str(_g_sym),
                                            float(_g_n),
                                            exit_path=_gg_exit,
                                        ):
                                            any_gg = True
                                    if any_gg:
                                        _gross_exposure_trims_done += 1
                                        return True

                            def _gross_liq_ref_mid(s: str) -> float:
                                wq0 = broker.get_latest_quote(s)
                                if not wq0:
                                    return 0.0
                                _pfb0 = rfc_fallback_open_mid_from_bars(broker, s)
                                return rfc_reference_mid_for_quote(
                                    wq0,
                                    fallback_1d_close=_pfb0,
                                    quote_mid=float(getattr(wq0, "mid", 0) or 0),
                                )

                            _gli = _rfc_cfg.get("gross_liquidation")
                            _gross_liq_map = None
                            if (
                                isinstance(_gli, dict)
                                and bool(_gli.get("enabled", False))
                            ):
                                try:
                                    _glt = float(
                                        _gli.get("target_gross_pct", 95.0) or 95.0
                                    )
                                except (TypeError, ValueError):
                                    _glt = 95.0
                                try:
                                    _glp = int(_gli.get("passes", 2) or 2)
                                except (TypeError, ValueError):
                                    _glp = 2
                                _glp = max(1, _glp)
                                _gross_liq_map = {
                                    "enabled": True,
                                    "account_equity": float(account_equity),
                                    "current_gross_pct": float(_exposure_snapshot.gross_pct),
                                    "target_gross_pct": _glt,
                                    "passes": _glp,
                                    "get_mid": _gross_liq_ref_mid,
                                }

                            plan_g = plan_weakest_trim_for_free_capital(
                                tracked=tracked,
                                eligible_symbols=_eligible_active,
                                positions=positions,
                                rep_sub=_rep_sub,
                                now_dt=dt,
                                incoming_sym_upper="",
                                trim_fraction=trim_fraction_by_gross_leverage(
                                    float(_exposure_snapshot.gross_pct)
                                ),
                                exclude_incoming_symbol=bool(_rfc_cfg["exclude_incoming_symbol"]),
                                broker=broker,
                                engine=engine,
                                require_signal_deterioration=False,
                                deterioration_min_gap=float(_reb_det_gap),
                                trim_target=str(
                                    _rfc_cfg.get("trim_target", "weakest") or "weakest"
                                ),
                                largest_exposure_max_try=int(
                                    _rfc_cfg.get("largest_exposure_max_try", 3) or 3
                                ),
                                gross_liquidation=_gross_liq_map,
                                top_positions=_tp_set_g,
                                top_position_trim_intensity=_tp_int_g,
                                is_first_rfc_pass=_rfc_first_g,
                                skip_if_unrealized_pnl_pct_above=_fp_win_g,
                            )
                            if plan_g is None:
                                return False
                            wsym_g, sell_qty_g = plan_g
                            if _rfc_skip_post_buy_cooldown(wsym_g):
                                return False
                            _row_g = tracked.get(wsym_g) or {}
                            if _exit_ctx.same_day_close_blocked(wsym_g, _row_g):
                                return False
                            wquote_g = broker.get_latest_quote(wsym_g)
                            if not wquote_g:
                                return False
                            _px_fb_g = rfc_fallback_open_mid_from_bars(broker, wsym_g)
                            _mid_g = rfc_reference_mid_for_quote(
                                wquote_g,
                                fallback_1d_close=_px_fb_g,
                                quote_mid=float(getattr(wquote_g, "mid", 0) or 0),
                            )
                            w_spread_g = rfc_effective_spread_pct(
                                wquote_g,
                                stale_hint=True,
                                stale_quote_max_age=stale_quote_max_age,
                            )
                            _pos_g = rfc_position_qty_floor_for_sell(
                                int(sell_qty_g), str(wsym_g), positions
                            )
                            sell_order_g = engine.execution.build_order(
                                wsym_g,
                                "sell",
                                sell_qty_g,
                                _mid_g,
                                float(w_spread_g) if w_spread_g is not None else 0.15,
                                ignore_spread_gate=wquote_g.skip_spread_check
                                if wquote_g
                                else False,
                                bid=float(wquote_g.bid) if wquote_g else None,
                                ask=float(wquote_g.ask) if wquote_g else None,
                                position_qty=_pos_g,
                            )
                            if not sell_order_g:
                                return False
                            if _exit_ctx.skip_exit_for_action_cap(
                                wsym_g, "exposure_gross_cap_trim"
                            ):
                                return False
                            broker.submit_order(sell_order_g)
                            _live_risk_note(str(wsym_g).upper(), "sell", False)
                            _log_trim_sell(str(wsym_g).upper(), "exposure_gross_cap_trim")
                            _exit_ctx.record_exit_action(wsym_g)
                            _gross_exposure_trims_done += 1
                            _rfc_trims_done += 1
                            _rfc_trimmed_symbols.add(str(wsym_g).strip().upper())
                            _gli2 = _rfc_cfg.get("gross_liquidation")
                            if (
                                isinstance(_gli2, dict)
                                and bool(_gli2.get("enabled", False))
                            ):
                                try:
                                    _gt2 = float(
                                        _gli2.get("target_gross_pct", 95.0) or 95.0
                                    )
                                except (TypeError, ValueError):
                                    _gt2 = 95.0
                                try:
                                    _gp2 = int(_gli2.get("passes", 2) or 2)
                                except (TypeError, ValueError):
                                    _gp2 = 2
                                _gl_msg = "de-lever to ~%.0f%% book, 1/%.0f gap slice" % (
                                    _gt2,
                                    max(1, int(_gp2)),
                                )
                            else:
                                _gl_msg = "gross-tiers slice"
                            print(
                                dt.strftime("%H:%M ET"),
                                "[%s] exposure_gates: SELL %s %d sh (gross cap: book %.1f%%, max eff. %.0f%%, %s)"
                                % (
                                    _uid,
                                    wsym_g,
                                    int(sell_order_g.quantity),
                                    float(_exposure_snapshot.gross_pct),
                                    float(_meff) * 100.0,
                                    _gl_msg,
                                ),
                                flush=True,
                            )
                            _rfc_refresh_after_sell()
                            return True

                        def _try_each_cycle_min_cash_trim() -> bool:
                            """One weakest partial sell when cash %% of equity is below ``min_cash_target_pct``."""
                            nonlocal _cycle_cash_target_trims
                            if not _each_cycle_rebal or _cash_target_frac <= 0.0:
                                return False
                            if _cycle_cash_target_trims >= 1:
                                return False
                            if _acct_cash_hi is None:
                                return False
                            _cp_ecc = cash_pct_of_equity(
                                cash=_acct_cash_hi, equity=float(account_equity)
                            )
                            if _cp_ecc is None:
                                return False
                            _target_cash_pct = float(_cash_target_frac) * 100.0
                            _trim_below_pct = max(0.0, _target_cash_pct - max(0.0, float(_rebalance_tol_pct)))
                            if _cp_ecc >= _trim_below_pct - 1e-3:
                                return False
                            if not _eligible_active:
                                return False
                            try:
                                _max_e = int(
                                    _rfc_cfg.get("max_trims_per_entry_scan", 1) or 1
                                )
                            except (TypeError, ValueError):
                                _max_e = 1
                            _max_e = max(0, _max_e)
                            _tp_set_c, _tp_int_c = _rfc_top_protection()
                            try:
                                _fp_wc = max(
                                    0.0,
                                    float(
                                        _rfc_cfg.get("first_pass_winner_pnl_skip_pct", 3.0)
                                        or 0.0
                                    ),
                                )
                            except (TypeError, ValueError, OverflowError):
                                _fp_wc = 3.0
                            _fp_win_c = _fp_wc if _fp_wc > 1e-12 else None
                            _rfc_first_c = _rfc_trims_done == 0
                            _btr_c = _rfc_cfg.get("bulk_trim")
                            if (
                                isinstance(_btr_c, dict)
                                and bool(_btr_c.get("enabled", False))
                            ):
                                _pls_c = plan_bulk_notional_trims_for_free_capital(
                                    eligible_symbols=_eligible_active,
                                    positions=positions,
                                    tracked=tracked,
                                    rep_sub=_rep_sub,
                                    now_dt=dt,
                                    incoming_sym_upper="",
                                    notional_per_symbol_usd=float(
                                        _btr_c.get("notional_per_symbol_usd", 1500.0)
                                    ),
                                    max_symbols=int(
                                        _btr_c.get("max_symbols_per_pass", 3) or 3
                                    ),
                                    exclude_incoming_symbol=False,
                                    require_signal_deterioration=False,
                                    broker=broker,
                                    engine=engine,
                                    top_positions=_tp_set_c,
                                    top_position_sell_intensity=_tp_int_c,
                                    is_first_rfc_pass=_rfc_first_c,
                                    skip_if_unrealized_pnl_pct_above=_fp_win_c,
                                    bulk_trim_priority=_bulk_pri,
                                )
                                if _pls_c and _rfc_trims_done < _max_e:
                                    any_c = False
                                    for _c_sym, _c_n in _pls_c:
                                        if _rfc_trims_done >= _max_e:
                                            break
                                        if _rfc_trim_candidate_submit_safe(
                                            str(_c_sym),
                                            float(_c_n),
                                            exit_path="rebalance_each_cycle_min_cash",
                                        ):
                                            any_c = True
                                    if any_c:
                                        _cycle_cash_target_trims += 1
                                        return True
                            plan_e = plan_weakest_trim_for_free_capital(
                                tracked=tracked,
                                eligible_symbols=_eligible_active,
                                positions=positions,
                                rep_sub=_rep_sub,
                                now_dt=dt,
                                incoming_sym_upper="",
                                trim_fraction=trim_fraction_by_gross_leverage(
                                    float(_exposure_snapshot.gross_pct)
                                ),
                                exclude_incoming_symbol=False,
                                broker=broker,
                                engine=engine,
                                trim_target=str(
                                    _rfc_cfg.get("trim_target", "weakest") or "weakest"
                                ),
                                largest_exposure_max_try=int(
                                    _rfc_cfg.get("largest_exposure_max_try", 3) or 3
                                ),
                                top_positions=_tp_set_c,
                                top_position_trim_intensity=_tp_int_c,
                                is_first_rfc_pass=_rfc_first_c,
                                skip_if_unrealized_pnl_pct_above=_fp_win_c,
                            )
                            if plan_e is None:
                                return False
                            wsym_e, sell_qty_e = plan_e
                            if _rfc_skip_post_buy_cooldown(wsym_e):
                                return False
                            _row_e = tracked.get(wsym_e) or {}
                            if _exit_ctx.same_day_close_blocked(wsym_e, _row_e):
                                return False
                            wquote_e = broker.get_latest_quote(wsym_e)
                            if not wquote_e:
                                return False
                            _px_fb_e = rfc_fallback_open_mid_from_bars(broker, wsym_e)
                            _mid_e = rfc_reference_mid_for_quote(
                                wquote_e,
                                fallback_1d_close=_px_fb_e,
                                quote_mid=float(getattr(wquote_e, "mid", 0) or 0),
                            )
                            w_spread_e = rfc_effective_spread_pct(
                                wquote_e,
                                stale_hint=True,
                                stale_quote_max_age=stale_quote_max_age,
                            )
                            _pos_e = rfc_position_qty_floor_for_sell(
                                int(sell_qty_e), str(wsym_e), positions
                            )
                            sell_order_e = engine.execution.build_order(
                                wsym_e,
                                "sell",
                                sell_qty_e,
                                _mid_e,
                                float(w_spread_e) if w_spread_e is not None else 0.15,
                                ignore_spread_gate=wquote_e.skip_spread_check if wquote_e else False,
                                bid=float(wquote_e.bid) if wquote_e else None,
                                ask=float(wquote_e.ask) if wquote_e else None,
                                position_qty=_pos_e,
                            )
                            if not sell_order_e:
                                return False
                            if _exit_ctx.skip_exit_for_action_cap(wsym_e, "rebalance_each_cycle_min_cash"):
                                return False
                            broker.submit_order(sell_order_e)
                            _live_risk_note(str(wsym_e).upper(), "sell", False)
                            _log_trim_sell(str(wsym_e).upper(), "rebalance_each_cycle_min_cash")
                            _exit_ctx.record_exit_action(wsym_e)
                            _cycle_cash_target_trims += 1
                            _rfc_trimmed_symbols.add(str(wsym_e).strip().upper())
                            print(
                                dt.strftime("%H:%M ET"),
                                "[%s] rebalance_each_cycle: SELL %s %d sh (cash %.1f%% < floor %.1f%%, target %.1f%%)"
                                % (
                                    _uid,
                                    wsym_e,
                                    int(sell_order_e.quantity),
                                    float(_cp_ecc),
                                    float(_trim_below_pct),
                                    float(_target_cash_pct),
                                ),
                                flush=True,
                            )
                            _rfc_refresh_after_sell()
                            return True

                        _try_gross_exposure_cap_trim()

                        if (
                            _each_cycle_rebal
                            and _cash_target_frac > 0.0
                            and _cycle_cash_target_trims < 1
                        ):
                            _try_each_cycle_min_cash_trim()

                        def _trend_long_dispatch_impl(row_tl: dict[str, Any]) -> bool:
                            apply_winner_size_multiplier_to_trend_row(
                                row_tl, engine=engine
                            )
                            sym_dispatch = str(row_tl.get("sym_u", "")).strip().upper()
                            notional_tl = float(row_tl.get("notional") or 0.0)
                            decision_tl = row_tl.get("decision")

                            def _gb_cap(s: str) -> object:
                                try:
                                    return broker.get_bars(
                                        s, timeframe="1Day", limit=220
                                    )
                                except Exception:
                                    return None

                            _r_skip = preflight_replacement_gates_on_dispatch(
                                port_replace=_port_replace,
                                max_port_positions=_max_port_positions,
                                n_eligible_active=len(_eligible_active),
                                sym_u=sym_dispatch,
                                current_position_keys=current_positions,
                                tracked=tracked,
                                eligible_active=_eligible_active,
                                positions=positions,
                                get_bars=_gb_cap,
                                engine=engine,
                                rep_sub=_rep_sub,
                                decision_tl=decision_tl,
                                notional_tl=float(notional_tl),
                                strength_jitter_max=_strength_jitter_max,
                                replacement_threshold=float(_replacement_threshold or 0.0),
                                allow_equal_replacement=bool(_allow_equal_rep),
                                cycle_replacements_done=int(
                                    _cycle_risk_state.get("replacements", 0) or 0
                                ),
                            )
                            if _r_skip is not None:
                                _log_entry_skip(
                                    dt,
                                    row_tl.get("symbol") or sym_dispatch,
                                    _r_skip,
                                    verbose=verbose,
                                    force=False,
                                )
                                return False
                            return dispatch_trend_long_after_buying_power(
                                row_tl,
                                dt=dt,
                                broker=broker,
                                config=config,
                                engine=engine,
                                verbose=verbose,
                                account_equity=account_equity,
                                positions=positions,
                                regime_result=regime_result,
                                bearish_regime=bearish_regime,
                                pct_above_50d_universe=pct_above_50d_universe,
                                allowed_symbols_for_stock_orders=_allowed_symbols_for_stock_orders,
                                max_port_positions=_max_port_positions,
                                port_replace=_port_replace,
                                port_allow_add=_effective_allow_add,
                                eligible_active=_eligible_active,
                                strength_jitter_max=_strength_jitter_max,
                                rep_sub=_rep_sub,
                                replace_if_weakest_older_than=_replace_if_weakest_older_than,
                                current_positions=current_positions,
                                user_id=_uid,
                                data_dir=_data_dir,
                                option_chain_for_underlying=_option_chain_for_underlying,
                                log_entry_skip=_log_entry_skip,
                                sector_etfs_for_risk=_sector_etfs,
                                cycle_risk_state=_cycle_risk_state,
                                et_date_iso=_et_date_iso,
                                gross_exposure_pct=float(_exposure_snapshot.gross_pct),
                                account_reduce_only=_portfolio_mode_reduce_only,
                                sector_exposure_pct=_exposure_snapshot.sector_pct,
                                symbol_sector=SYMBOL_SECTOR,
                                high_cash_deploy=_high_cash_deploy,
                                replacement_threshold=_replacement_threshold,
                                max_position_age_bars=_max_pos_age_bars,
                                allow_equal_replacement=_allow_equal_rep,
                                replacement_scan_state=_rep_scan_state,
                                per_cycle_exit_ctx=_exit_ctx,
                                live_risk_order_callback=_live_risk_note,
                            )

                        def _attempt_paper_option_entry_for_row(
                            row_tl: dict[str, Any],
                            *,
                            lane: str,
                        ) -> bool:
                            sym_opt = str(row_tl.get("sym_u") or row_tl.get("symbol") or "").strip().upper()
                            if not sym_opt:
                                return False
                            _paper_options_active_for_route = _paper_only_options_active(config)
                            _live_pilot_active_for_route = _live_pilot_options_active(config, broker)
                            _options_observability_active_for_route = _options_route_observability_active(
                                config,
                                broker,
                            )
                            _route_observable = bool(
                                _options_observability_active_for_route
                                and _paper_option_route_observable(row_tl)
                            )

                            def _route_skip(reason: str, detail: Any = None) -> None:
                                if _route_observable:
                                    _log_option_route_skipped(
                                        sym_opt,
                                        lane=lane,
                                        reason=reason,
                                        detail=detail,
                                        row_tl=row_tl,
                                    )

                            _preclassified_skip: str | None = None
                            _preclassified_detail: str | None = None
                            if _route_observable:
                                record_options_candidate(
                                    symbol=sym_opt,
                                    underlying=sym_opt,
                                    direction=str(row_tl.get("direction") or row_tl.get("side") or "unknown"),
                                    source=str(row_tl.get("source") or row_tl.get("route") or lane),
                                    stage="live_lane_eval",
                                )
                                _log_option_route_check(
                                    sym_opt,
                                    lane=lane,
                                    row_tl=row_tl,
                                )
                                if not _paper_option_underlying_allowed(config, sym_opt):
                                    _preclassified_skip = "underlying_not_allowed"
                                    _preclassified_detail = "allowed_underlyings"
                                elif not trend_long_options_top_signals_only_passes(
                                    config,
                                    row_tl,
                                ):
                                    _preclassified_skip = "require_top_signal_failed"
                                    _preclassified_detail = "top_signal_required"
                                else:
                                    try:
                                        _opt_env_block_live, _opt_env_reason_live = (
                                            options_entry_environment_blocks(
                                                config,
                                                gross_exposure_pct=float(_exposure_snapshot.gross_pct),
                                                reduce_only=bool(_portfolio_mode_reduce_only),
                                                regime_score=_entry_regime_score,
                                            )
                                        )
                                    except Exception:
                                        _opt_env_block_live, _opt_env_reason_live = False, None
                                    if _opt_env_block_live:
                                        _preclassified_skip = (
                                            "gross_exposure"
                                            if "gross" in str(_opt_env_reason_live or "").lower()
                                            else "environment_blocked"
                                        )
                                        _preclassified_detail = str(_opt_env_reason_live or "")

                            def _inactive_options_reason() -> str:
                                opts_inactive = (config or {}).get("options") if isinstance(config, Mapping) else {}
                                opts_inactive = opts_inactive if isinstance(opts_inactive, Mapping) else {}
                                mode_inactive = options_mode(dict(config or {}))
                                if not bool(opts_inactive.get("enabled")):
                                    return "options_disabled"
                                if mode_inactive == "paper_only" and not _broker_mode_is_paper(broker, config):
                                    return "paper_only_inactive"
                                if (
                                    mode_inactive in {"live", "live_long_premium", "long_premium_only"}
                                    and not _broker_mode_is_paper(broker, config)
                                    and not options_live_pilot_enabled(dict(config or {}))
                                ):
                                    return "live_pilot_disabled"
                                if mode_inactive != "paper_only" and _broker_mode_is_paper(broker, config):
                                    return "non_paper_mode"
                                return "non_paper_mode"

                            if not _paper_options_active_for_route and not _live_pilot_active_for_route:
                                _inactive_reason = _inactive_options_reason()
                                if _route_observable:
                                    record_options_rejection(
                                        symbol=sym_opt,
                                        stage="runtime",
                                        reason=_inactive_reason,
                                    )
                                if _inactive_reason == "live_pilot_disabled":
                                    log.info("OPTIONS_LIVE_BLOCKED reason=live_pilot_disabled")
                                log.info(
                                    "OPTIONS_ENTRY_LANE symbol=%s lane=%s action=skip reason=%s",
                                    sym_opt,
                                    lane,
                                    _inactive_reason,
                                )
                                _route_skip("stock_route_selected", _inactive_reason)
                                return False
                            if not _options_runtime_enabled(broker, config):
                                _runtime_reason = _inactive_options_reason()
                                if _route_observable:
                                    record_options_rejection(
                                        symbol=sym_opt,
                                        stage="runtime",
                                        reason=_runtime_reason,
                                    )
                                if _runtime_reason == "live_pilot_disabled":
                                    log.info("OPTIONS_LIVE_BLOCKED reason=live_pilot_disabled")
                                log.info(
                                    "OPTIONS_ENTRY_LANE symbol=%s lane=%s action=skip reason=%s",
                                    sym_opt,
                                    lane,
                                    _runtime_reason,
                                )
                                _route_skip("stock_route_selected", _runtime_reason)
                                return False
                            price_raw = row_tl.get("paper_current_price")
                            if price_raw is None:
                                quote_obj = row_tl.get("quote")
                                price_raw = getattr(quote_obj, "mid", None) if quote_obj is not None else None
                            if price_raw is None:
                                df_obj = row_tl.get("df")
                                try:
                                    if df_obj is not None and not getattr(df_obj, "empty", True):
                                        price_raw = float(df_obj["close"].iloc[-1])
                                except Exception:
                                    price_raw = None
                            try:
                                paper_price = float(price_raw)
                            except (TypeError, ValueError):
                                paper_price = 0.0
                            paper_vwap = row_tl.get("paper_session_vwap")
                            if paper_vwap is None:
                                try:
                                    _ny_opt = pytz.timezone("America/New_York")
                                    _start_opt = dt.astimezone(_ny_opt).replace(
                                        hour=9,
                                        minute=30,
                                        second=0,
                                        microsecond=0,
                                    )
                                    _bars_opt = broker.get_bars(
                                        sym_opt,
                                        timeframe=TimeFrame.Minute,
                                        start=_start_opt.astimezone(pytz.UTC),
                                        end=dt.astimezone(pytz.UTC),
                                        limit=390,
                                    )
                                    paper_vwap = session_vwap_from_bars(_bars_opt)
                                    if paper_price <= 0 and _bars_opt is not None and not getattr(_bars_opt, "empty", True):
                                        paper_price = float(_bars_opt["close"].iloc[-1])
                                except Exception:
                                    paper_vwap = None
                            if paper_vwap is None:
                                if _route_observable:
                                    record_options_rejection(
                                        symbol=sym_opt,
                                        stage="signal",
                                        reason="underlying_signal_missing",
                                    )
                                log.info(
                                    "OPTIONS_ENTRY_LANE symbol=%s lane=%s action=skip reason=session_vwap_unavailable",
                                    sym_opt,
                                    lane,
                                )
                                _route_skip("no_contract_found", "session_vwap_unavailable")
                                return False
                            if paper_price <= 0:
                                if _route_observable:
                                    record_options_rejection(
                                        symbol=sym_opt,
                                        stage="signal",
                                        reason="underlying_signal_missing",
                                    )
                                log.info(
                                    "OPTIONS_ENTRY_LANE symbol=%s lane=%s action=skip reason=price_unavailable",
                                    sym_opt,
                                    lane,
                                )
                                _route_skip("no_contract_found", "price_unavailable")
                                return False
                            log.info(
                                "OPTIONS_ENTRY_LANE symbol=%s lane=%s action=attempt reason=%s",
                                sym_opt,
                                lane,
                                "live_pilot_active" if _live_pilot_active_for_route else "paper_only_active",
                            )
                            _entry_decision_counts["options_attempted"] += 1
                            try:
                                result = _attempt_paper_option_entry(
                                    config,
                                    broker=broker,
                                    execution_manager=engine.execution,
                                    symbol=sym_opt,
                                    dt=dt,
                                    current_price=paper_price,
                                    session_vwap=float(paper_vwap),
                                    account_equity=float(account_equity),
                                    positions=positions,
                                    source=str(row_tl.get("source") or lane),
                                    conviction_score=float(row_tl.get("strength_eff"))
                                    if row_tl.get("strength_eff") is not None
                                    else None,
                                    scanner_score=float(
                                        row_tl.get("scanner_score")
                                        if row_tl.get("scanner_score") is not None
                                        else row_tl.get("dynamic_score")
                                    )
                                    if (
                                        row_tl.get("scanner_score") is not None
                                        or row_tl.get("dynamic_score") is not None
                                    )
                                    else None,
                                    news_score=float(row_tl.get("news_score"))
                                    if row_tl.get("news_score") is not None
                                    else None,
                                    event_score=float(row_tl.get("event_score"))
                                    if row_tl.get("event_score") is not None
                                    else None,
                                    catalyst_score=float(row_tl.get("catalyst_score"))
                                    if row_tl.get("catalyst_score") is not None
                                    else None,
                                    relative_volume=float(row_tl.get("relative_volume"))
                                    if row_tl.get("relative_volume") is not None
                                    else None,
                                    tracked=tracked if isinstance(tracked, dict) else None,
                                )
                            except Exception:
                                log.debug(
                                    "[%s] paper-only option entry helper failed for %s",
                                    _uid,
                                    sym_opt,
                                    exc_info=True,
                                )
                                log.info(
                                    "OPTIONS_ENTRY_LANE symbol=%s lane=%s action=skip reason=paper_entry_exception",
                                    sym_opt,
                                    lane,
                                )
                                _route_skip("selector_rejected_all", "paper_entry_exception")
                                return False
                            if result.placed:
                                _entry_decision_counts["options_selected"] += 1
                                _entry_decision_counts["options_ordered"] += 1
                                log.info(
                                    "PAPER_ONLY_OPTIONS_FILLED symbol=%s right=%s reason_codes=%s",
                                    sym_opt,
                                    result.right or "n/a",
                                    ",".join(result.reason_codes),
                                )
                                return True
                            if _route_observable:
                                record_options_rejection(
                                    symbol=sym_opt,
                                    stage="route",
                                    reason=result.reason or ",".join(result.reason_codes) or "no_contract_found",
                                )
                            if any(
                                code in set(result.reason_codes)
                                for code in (
                                    "route_failed",
                                    "liquidity_failed",
                                    "spread_failed",
                                    "stale_quote",
                                    "missing_bid_ask",
                                )
                            ):
                                _entry_decision_counts["blocked_option_liquidity"] += 1
                            log.info(
                                "PAPER_ONLY_OPTIONS_SKIPPED symbol=%s reason=%s reason_codes=%s",
                                sym_opt,
                                result.reason or "paper-only route not placed",
                                ",".join(result.reason_codes),
                            )
                            log.info(
                                "OPTIONS_NO_TRADE symbol=%s reason=%s reason_codes=%s",
                                sym_opt,
                                result.reason or "no_contract_selected",
                                ",".join(result.reason_codes) or "none",
                            )
                            _classified_skip = _option_route_skip_reason_from_text(
                                result.reason,
                                result.reason_codes,
                            )
                            if (
                                _preclassified_skip is not None
                                and _classified_skip in {"no_contract_found", "selector_rejected_all"}
                            ):
                                _classified_skip = _preclassified_skip
                            _route_skip(
                                _classified_skip,
                                _preclassified_detail
                                or result.reason
                                or ",".join(result.reason_codes),
                            )
                            return False

                        def _trend_long_dispatch_or_queue(row_tl: dict[str, Any]) -> bool:
                            # Allocator on: scan loop queues signals directly — never entry-dispatch here.
                            if _cap_alloc_enabled:
                                return False
                            if _attempt_paper_option_entry_for_row(
                                row_tl,
                                lane="ranked_or_direct",
                            ):
                                return True
                            if _paper_only_options_active(config):
                                _entry_decision_counts["stock_fallback"] += 1
                            if _options_route_observability_active(config, broker):
                                sym_fallback = str(
                                    row_tl.get("sym_u") or row_tl.get("symbol") or ""
                                ).strip().upper()
                                if sym_fallback and _paper_option_route_observable(row_tl):
                                    _log_option_route_skipped(
                                        sym_fallback,
                                        lane="ranked_or_direct",
                                        reason="fallback_to_stock",
                                        detail="paper_option_not_placed",
                                        row_tl=row_tl,
                                    )
                            return _trend_long_dispatch_impl(row_tl)

                        for _rfc_entry_pass in (1, 2):
                            if _rfc_entry_pass == 2 and not _rfc_trimmed_symbols:
                                break
                            _syms_scan = symbols if _rfc_entry_pass == 1 else sorted(_rfc_trimmed_symbols)
                            _dynamic_symbols_runtime = (
                                dynamic_symbols if "dynamic_symbols" in locals() else []
                            )
                            if _rfc_entry_pass == 1:
                                _syms_scan = _entry_scan_order_for_session(
                                    _syms_scan,
                                    dynamic_symbols=_dynamic_symbols_runtime,
                                    early_session=_open_accelerated_window,
                                )
                            dynamic_set = {
                                str(s).strip().upper()
                                for s in (_dynamic_symbols_runtime or [])
                                if str(s).strip()
                            }
                            _scanner_selected_dynamic_set = {
                                str(s).strip().upper()
                                for s in (_dynamic_scan_accepted_meta or {}).keys()
                                if str(s).strip()
                            }
                            dynamic_set |= _scanner_selected_dynamic_set
                            _dynamic_selected_count_map = {
                                str(s).strip().upper(): sum(
                                    1
                                    for _ds in (_dynamic_symbols_runtime or [])
                                    if str(_ds).strip().upper() == str(s).strip().upper()
                                )
                                for s in (_dynamic_symbols_runtime or [])
                                if str(s).strip()
                            }
                            if _rfc_entry_pass == 1 and dynamic_set:
                                _scan_seen_for_dynamic_bypass = {
                                    str(s).strip().upper()
                                    for s in (_syms_scan or [])
                                    if str(s).strip()
                                }
                                _dynamic_entry_runtime_symbols = list(
                                    dict.fromkeys(
                                        list(_dynamic_symbols_runtime or [])
                                        + sorted(_scanner_selected_dynamic_set)
                                    )
                                )
                                for _dyn_selected_raw in _dynamic_entry_runtime_symbols:
                                    _dyn_selected_u = str(_dyn_selected_raw).strip().upper()
                                    if not _dyn_selected_u or _dyn_selected_u in _scan_seen_for_dynamic_bypass:
                                        continue
                                    _syms_scan = list(_syms_scan or []) + [_dyn_selected_u]
                                    _scan_seen_for_dynamic_bypass.add(_dyn_selected_u)
                                    log.info(
                                        "DYNAMIC_ENTRY_PREFILTER_BYPASS symbol=%s reason=dynamic_selected",
                                        _dyn_selected_u,
                                    )
                                _scan_symbol_set = {
                                    str(s).strip().upper()
                                    for s in (_syms_scan or [])
                                    if str(s).strip()
                                }
                                _universe_symbol_set = {
                                    str(s).strip().upper()
                                    for s in (symbols or [])
                                    if str(s).strip()
                                } | set(dynamic_set)
                                for _dyn_selected_sym in sorted(dynamic_set):
                                    _dyn_in_universe = _dyn_selected_sym in _universe_symbol_set
                                    _dyn_in_scan = _dyn_selected_sym in _scan_symbol_set
                                    if not _dyn_in_universe:
                                        _dyn_entry_reason = "not_in_universe"
                                    elif not _dyn_in_scan:
                                        _dyn_entry_reason = "not_in_entry_scan"
                                    elif (
                                        not do_dynamic_entry
                                        and _dyn_selected_sym not in _scanner_selected_dynamic_set
                                    ):
                                        _dyn_entry_reason = "dynamic_entry_disabled"
                                    else:
                                        _dyn_entry_reason = "ok"
                                    _log_dynamic_selected_entry_trace(
                                        _dyn_selected_sym,
                                        in_universe=_dyn_in_universe,
                                        will_evaluate=bool(
                                            _dyn_in_universe
                                            and _dyn_in_scan
                                            and (
                                                do_dynamic_entry
                                                or _dyn_selected_sym in _scanner_selected_dynamic_set
                                            )
                                        ),
                                        reason=_dyn_entry_reason,
                                        in_dynamic_set=True,
                                        in_effective_universe=_dyn_in_scan,
                                        route_candidate="dynamic_momentum",
                                        selected_count=_dynamic_selected_count_map.get(_dyn_selected_sym, 1),
                                    )
                                    if not (
                                        _dyn_in_universe
                                        and _dyn_in_scan
                                        and (
                                            do_dynamic_entry
                                            or _dyn_selected_sym in _scanner_selected_dynamic_set
                                        )
                                    ):
                                        if _dyn_selected_sym in _scanner_selected_dynamic_set:
                                            _log_dynamic_entry_candidate_skipped(
                                                _dyn_selected_sym,
                                                reason=_dyn_entry_reason,
                                            )
                                        _log_dynamic_selected_entry_drop(
                                            _dyn_selected_sym,
                                            stage="pre_loop_membership",
                                            reason=_dyn_entry_reason,
                                            detail=(
                                                "in_universe=%s in_entry_scan=%s do_dynamic_entry=%s"
                                                % (
                                                    str(bool(_dyn_in_universe)).lower(),
                                                    str(bool(_dyn_in_scan)).lower(),
                                                    str(bool(do_dynamic_entry)).lower(),
                                                )
                                            ),
                                        )
                                    elif _dyn_selected_sym in _scanner_selected_dynamic_set:
                                        _log_dynamic_entry_candidate_enqueued(
                                            _dyn_selected_sym,
                                            source="scanner_selected",
                                        )
                                if _scanner_selected_dynamic_set:
                                    _scanner_ordered_symbols = [
                                        sym
                                        for sym in (_syms_scan or [])
                                        if str(sym or "").strip().upper()
                                        in _scanner_selected_dynamic_set
                                    ]
                                    _non_scanner_ordered_symbols = [
                                        sym
                                        for sym in (_syms_scan or [])
                                        if str(sym or "").strip().upper()
                                        not in _scanner_selected_dynamic_set
                                    ]
                                    _syms_scan = list(
                                        dict.fromkeys(
                                            _scanner_ordered_symbols
                                            + _non_scanner_ordered_symbols
                                        )
                                    )
                                    _ordered_scan_symbol_set = {
                                        str(sym or "").strip().upper()
                                        for sym in (_syms_scan or [])
                                        if str(sym or "").strip()
                                    }
                                    for _dyn_missing_lane in sorted(
                                        _dynamic_entry_enqueued_symbols
                                        - _ordered_scan_symbol_set
                                    ):
                                        _dynamic_entry_eval_dropped_symbols.add(
                                            _dyn_missing_lane
                                        )
                                        _log_dynamic_entry_eval_dropped(
                                            _dyn_missing_lane,
                                            reason="dynamic_lane_not_processed",
                                        )
                            _rep_scan_state: dict[str, int] = {
                                "count": 0,
                                "max": int(_max_rep_per_cycle),
                            }
                            _ad_cap_streak = 0
                            _lr_en, _lr_syms, _lr_hard, _ = liquid_spread_relief_parse(config)
                            _dme_cfg_rank = config.get("dynamic_momentum_entry") or {}
                            _ms_rank_cfg = (
                                _dme_cfg_rank.get("momentum_score")
                                if isinstance(_dme_cfg_rank, dict)
                                else None
                            )
                            _momentum_rank_on = (
                                isinstance(_dme_cfg_rank, dict)
                                and bool(_dme_cfg_rank.get("enabled", True))
                                and isinstance(_ms_rank_cfg, dict)
                                and bool(_ms_rank_cfg.get("enabled", True))
                            )
                            _dynamic_momentum_allowlist: frozenset[str] | None = None
                            _dynamic_momentum_scores: dict[str, float] = {}
                            _dynamic_momentum_rel_volumes: dict[str, float] = {}
                            _rank_pairs: list[tuple[str, float]] = []
                            try:
                                _dynamic_momentum_top_n = max(
                                    1,
                                    int(float(_dme_cfg_rank.get("momentum_top_n", 3))),
                                )
                            except (TypeError, ValueError):
                                _dynamic_momentum_top_n = 3
                            if (
                                _momentum_rank_on
                                and _du_enabled_u
                                and do_dynamic_entry
                                and dynamic_set
                            ):
                                _utc_rank = dt.astimezone(pytz.UTC)
                                _ny_rank = pytz.timezone("America/New_York")
                                _start_et_rank = dt.astimezone(_ny_rank).replace(
                                    hour=9,
                                    minute=30,
                                    second=0,
                                    microsecond=0,
                                )
                                _bar_start_rank = _start_et_rank.astimezone(pytz.UTC)
                                tf_5_rank = TimeFrame(5, TimeFrameUnit.Minute)
                                for _sym_rank in _syms_scan:
                                    _su_rank = str(_sym_rank).strip().upper()
                                    if _su_rank not in dynamic_set:
                                        continue
                                    try:
                                        df_1m_rank = broker.get_bars(
                                            _sym_rank,
                                            timeframe=TimeFrame.Minute,
                                            start=_bar_start_rank,
                                            end=_utc_rank,
                                            limit=390,
                                        )
                                        if df_1m_rank is None or getattr(
                                            df_1m_rank, "empty", True
                                        ):
                                            continue
                                        if len(df_1m_rank) < 5:
                                            continue
                                        df_5m_rank = broker.get_bars(
                                            _sym_rank,
                                            timeframe=tf_5_rank,
                                            start=_bar_start_rank,
                                            end=_utc_rank,
                                            limit=96,
                                        )
                                        _close_rank = float(df_1m_rank["close"].iloc[-1])
                                        if _close_rank <= 0:
                                            continue
                                        _ref_rank = _close_rank
                                        _snap_rank: dict[str, Any] = {}
                                        if hasattr(broker, "get_snapshot"):
                                            try:
                                                _sn_raw = broker.get_snapshot(_sym_rank)
                                                if isinstance(_sn_raw, dict):
                                                    _snap_rank = _sn_raw
                                            except Exception:
                                                pass
                                        try:
                                            _gain_rank = float(_snap_rank.get("day_gain_pct"))
                                            if not math.isfinite(_gain_rank):
                                                _gain_rank = None
                                        except (TypeError, ValueError):
                                            _gain_rank = None
                                        try:
                                            _vol_sn_rank = float(
                                                _snap_rank.get("volume", 0) or 0
                                            )
                                        except (TypeError, ValueError):
                                            _vol_sn_rank = 0.0
                                        _avg_rank = 1.0
                                        if hasattr(broker, "get_avg_volume"):
                                            try:
                                                _avg_rank = float(
                                                    broker.get_avg_volume(_sym_rank)
                                                )
                                            except Exception:
                                                _avg_rank = 1.0
                                        _avg_rank = max(1.0, _avg_rank)
                                        _rel_rank = (
                                            _vol_sn_rank / _avg_rank
                                            if _vol_sn_rank > 0
                                            else None
                                        )
                                        _dyn_sig_rank = compute_dynamic_entry_signals(
                                            df_1m_rank, _ref_rank
                                        )
                                        _brk_rank = five_min_breakout_from_bars(
                                            df_5m_rank, _ref_rank
                                        )
                                        _sc_rank, _ = compute_intraday_momentum_score(
                                            relative_volume=_rel_rank,
                                            gain_pct=_gain_rank,
                                            five_min_breakout=_brk_rank,
                                            distance_from_vwap_pct=float(
                                                _dyn_sig_rank.distance_from_vwap_pct
                                            ),
                                            cfg=_dme_cfg_rank
                                            if isinstance(_dme_cfg_rank, dict)
                                            else {},
                                        )
                                        _rank_pairs.append((_su_rank, _sc_rank))
                                        _dynamic_momentum_scores[_su_rank] = _sc_rank
                                        _dynamic_momentum_rel_volumes[_su_rank] = float(_rel_rank)
                                    except Exception:
                                        continue
                            if _rank_pairs:
                                _dynamic_momentum_allowlist = pick_top_n_momentum_symbols(
                                    _rank_pairs,
                                    top_n=_dynamic_momentum_top_n,
                                )
                            _dynamic_momentum_rank_map = {
                                _sym_ranked: _idx_ranked
                                for _idx_ranked, (_sym_ranked, _score_ranked) in enumerate(
                                    sorted(
                                        _rank_pairs,
                                        key=lambda _rank_pair: (-float(_rank_pair[1]), str(_rank_pair[0])),
                                    ),
                                    start=1,
                                )
                            }
                            for _sym_meta, _scanner_meta in _dynamic_scan_accepted_meta.items():
                                if _sym_meta not in dynamic_set:
                                    continue
                                _scanner_score = _as_finite_float_or_none(
                                    _scanner_meta.get("score")
                                )
                                if _scanner_score is not None and (
                                    _sym_meta not in _dynamic_momentum_scores
                                    or float(_dynamic_momentum_scores.get(_sym_meta, 0.0) or 0.0) <= 0.0
                                ):
                                    _dynamic_momentum_scores[_sym_meta] = _scanner_score
                                _scanner_rel = _as_finite_float_or_none(
                                    _scanner_meta.get("relative_volume")
                                )
                                if _scanner_rel is not None and (
                                    _sym_meta not in _dynamic_momentum_rel_volumes
                                    or float(_dynamic_momentum_rel_volumes.get(_sym_meta, 0.0) or 0.0) <= 0.0
                                ):
                                    _dynamic_momentum_rel_volumes[_sym_meta] = _scanner_rel
                            _dme_cfg = _dynamic_momentum_entry_effective_cfg(config)
                            _dme_source = (
                                "dynamic_momentum_entry+override"
                                if isinstance(config.get("dynamic_momentum_entry"), dict)
                                and isinstance(config.get("dynamic_momentum_override"), dict)
                                and bool(config.get("dynamic_momentum_override", {}).get("enabled"))
                                else "dynamic_momentum_entry"
                                if isinstance(config.get("dynamic_momentum_entry"), dict)
                                else "dynamic_momentum_override"
                                if isinstance(config.get("dynamic_momentum_override"), dict)
                                else "none"
                            )
                            _dme_on = isinstance(_dme_cfg, dict) and bool(_dme_cfg.get("enabled", False))
                            if _dme_on and isinstance(_dme_cfg, dict):
                                _adaptive_cfg = (
                                    _dme_cfg.get("adaptive_sensitivity")
                                    if isinstance(_dme_cfg.get("adaptive_sensitivity"), Mapping)
                                    else {}
                                )
                                if _adaptive_cfg:
                                    try:
                                        _lookback_adapt = int(float(_adaptive_cfg.get("lookback_trading_days", 10) or 10))
                                    except (TypeError, ValueError):
                                        _lookback_adapt = 10
                                    _adaptive_metrics = _load_recent_dynamic_metrics(
                                        data_dir=_data_dir,
                                        user_id=_uid,
                                        lookback_trading_days=_lookback_adapt,
                                    )
                                    _adaptive_context = {
                                        "environment": "paper" if bool(getattr(args, "paper", False)) else "live",
                                        "production": not bool(getattr(args, "paper", False)),
                                        "daily_loss_lockout": bool(
                                            getattr(_live_risk_guard, "new_entries_blocked", False)
                                        ),
                                        "market_regime": str(
                                            getattr(regime_result, "condition", "")
                                            or getattr(regime_result, "label", "")
                                            or ""
                                        ),
                                        "gross_exposure_pct": float(getattr(_exposure_snapshot, "gross_pct", 0.0) or 0.0),
                                        "gross_exposure_cap_pct": 100.0,
                                        "data_quality_bad": False,
                                        "spread_liquidity_bad": False,
                                    }
                                    _dme_cfg.setdefault("trading_control", dict(config.get("trading_control") or {}))
                                    _dme_cfg["adaptive_metrics"] = _adaptive_metrics
                                    _dme_cfg["adaptive_context"] = _adaptive_context
                                    _adaptive_state = _resolve_dynamic_adaptive_sensitivity(
                                        _dme_cfg,
                                        metrics=_adaptive_metrics,
                                        context=_adaptive_context,
                                        base_min_rvol=float(_dme_cfg.get("min_relative_volume", 1.5) or 1.5),
                                    )
                                    _prod_auto = bool(
                                        (
                                            (config.get("trading_control") or {}).get("adaptive_relaxation")
                                            if isinstance((config.get("trading_control") or {}).get("adaptive_relaxation"), Mapping)
                                            else {}
                                        ).get("production_auto_apply", False)
                                    )
                                    log.info("ADAPTIVE_RELAXATION production_auto_apply=%s", str(_prod_auto).lower())
                                    log.info(_render_dynamic_adaptive_config(_adaptive_state))
                            log.info(
                                "DYNAMIC_MOMENTUM_ENTRY_CONFIG enabled=%s source=%s",
                                str(bool(_dme_on)).lower(),
                                _dme_source,
                            )
                            _live_signal_scan_checked_count = 0
                            for symbol in _syms_scan:
                                _live_signal_scan_checked_count += 1
                                _sym_lane_u = str(symbol).strip().upper()
                                sym_u = _sym_lane_u
                                _is_scanner_selected_lane = _sym_lane_u in _scanner_selected_dynamic_set
                                _is_dyn_lane = _du_enabled_u and (
                                    _sym_lane_u in dynamic_set or _is_scanner_selected_lane
                                )
                                _is_dynamic_added = (
                                    sym_u in dynamic_set or sym_u in _scanner_selected_dynamic_set
                                )
                                _route_log = (
                                    "dynamic_momentum_override"
                                    if sym_u in _scanner_selected_dynamic_set
                                    else "momentum_breakout"
                                    if _is_dynamic_added
                                    else "trend_long"
                                )
                                if (
                                    _is_dyn_lane
                                    and not do_dynamic_entry
                                    and not _is_scanner_selected_lane
                                ):
                                    if _is_scanner_selected_lane:
                                        _dynamic_entry_eval_dropped_symbols.add(_sym_lane_u or "?")
                                        _log_dynamic_entry_candidate_skipped(
                                            _sym_lane_u or "?",
                                            reason="dynamic_entry_disabled",
                                        )
                                        _log_dynamic_entry_eval_dropped(
                                            _sym_lane_u or "?",
                                            reason="dynamic_entry_disabled",
                                        )
                                    _log_dynamic_selected_entry_drop(
                                        _sym_lane_u or "?",
                                        stage="route_disabled",
                                        reason="dynamic_entry_disabled",
                                        detail="do_dynamic_entry=false",
                                    )
                                    log.info(
                                        "DYNAMIC_SELECTED_DROPPED symbol=%s reason=%s",
                                        _sym_lane_u or "?",
                                        "dynamic_entry_disabled",
                                    )
                                    log.info(
                                        "DYNAMIC_SELECTED_ENTRY_SKIPPED symbol=%s reason=%s",
                                        _sym_lane_u or "?",
                                        "dynamic_entry_disabled",
                                    )
                                    continue
                                if not _is_dyn_lane and not run_trend_long_entries:
                                    continue
                                if not _is_dyn_lane and not do_core_entry:
                                    continue
                                _pyramid_relax_symbol_cap = False
                                if _portfolio_mode_reduce_only:
                                    if _rfc_entry_pass == 1 and symbol == _syms_scan[0]:
                                        print(
                                            dt.strftime("%H:%M ET"),
                                            "[%s] over_exposed_mode: reduce_only — skip new entries (gross %.1f%% of equity)"
                                            % (
                                                _uid,
                                                float(_exposure_snapshot.gross_pct),
                                            ),
                                            flush=True,
                                        )
                                    if _is_dyn_lane:
                                        if _is_scanner_selected_lane:
                                            _dynamic_entry_eval_dropped_symbols.add(_sym_lane_u or "?")
                                            _log_dynamic_entry_candidate_skipped(
                                                _sym_lane_u or "?",
                                                reason="portfolio_reduce_only",
                                            )
                                            _log_dynamic_entry_eval_dropped(
                                                _sym_lane_u or "?",
                                                reason="portfolio_reduce_only",
                                            )
                                        _log_dynamic_selected_entry_drop(
                                            _sym_lane_u or "?",
                                            stage="portfolio_mode",
                                            reason="portfolio_reduce_only",
                                        )
                                        log.info(
                                            "DYNAMIC_SELECTED_DROPPED symbol=%s reason=%s",
                                            _sym_lane_u or "?",
                                            "portfolio_reduce_only",
                                        )
                                    continue
                                _scanner_meta_entry = _dynamic_scan_accepted_meta.get(sym_u, {})
                                _scanner_meta_payload = _dynamic_scanner_metadata_payload(
                                    _scanner_meta_entry
                                )
                                _symbol_class = _symbol_classifications.get(
                                    sym_u,
                                    classify_symbol(
                                        sym_u,
                                        core_symbols,
                                        allocator_holdings=_allocator_holdings_watch,
                                        dynamic_symbols=_runtime_dynamic_watch,
                                    ),
                                )
                                _has_dynamic_signal = _symbol_class in {
                                    "CORE_WITH_DYNAMIC_SIGNAL",
                                    "DYNAMIC_ONLY",
                                }
                                _is_dynamic_candidate = (
                                    _symbol_class == "DYNAMIC_ONLY"
                                    or sym_u in _premarket_injected_symbol_set
                                    or sym_u in _scanner_selected_dynamic_set
                                )
                                _entry_effective_min_rel_volume = None
                                _entry_catalyst_fastlane_active = False
                                _entry_catalyst_min_relative_volume = None
                                _entry_check_source = (
                                    "premarket"
                                    if sym_u in _premarket_injected_symbol_set
                                    else "dynamic"
                                    if _is_dynamic_added or _is_dynamic_candidate
                                    else "core"
                                )
                                log.info(
                                    "ENTRY_CHECK_SYMBOL symbol=%s source=%s is_dynamic=%s",
                                    sym_u,
                                    _entry_check_source,
                                    str(bool(_is_dynamic_added or _is_dynamic_candidate)).lower(),
                                )
                                if _is_dynamic_added or _is_dynamic_candidate:
                                    try:
                                        _selected_scoring_allowed = should_apply_scoring_gate(
                                            scoring_allowed=allowed_symbols,
                                            sym_upper=sym_u,
                                            current_positions=set(current_positions.keys()),
                                            tracked_keys_upper=_tracked_keys_upper,
                                        )
                                    except Exception:
                                        _selected_scoring_allowed = False
                                    _selected_in_scoring_top_n = sym_u in (allowed_symbols or set())
                                    _selected_dynamic_bypass = bool(
                                        _selected_scoring_allowed and _is_dynamic_added
                                    )
                                    _selected_route_candidate = (
                                        "premarket"
                                        if sym_u in _premarket_injected_symbol_set
                                        else "dynamic_momentum"
                                        if _is_dynamic_added or _is_dynamic_candidate
                                        else "trend_long"
                                    )
                                    _log_dynamic_selected_entry_trace(
                                        sym_u,
                                        in_universe=_is_dynamic_added,
                                        will_evaluate=True,
                                        reason="processing",
                                        in_dynamic_set=sym_u in dynamic_set,
                                        in_effective_universe=sym_u in {
                                            str(_s or "").strip().upper()
                                            for _s in (_syms_scan or [])
                                            if str(_s or "").strip()
                                        },
                                        in_scoring_top_n=_selected_in_scoring_top_n,
                                        scoring_allowed=_selected_scoring_allowed,
                                        dynamic_bypass_applied=_selected_dynamic_bypass,
                                        route_candidate=_selected_route_candidate,
                                        selected_count=_dynamic_selected_count_map.get(sym_u, 0),
                                        rank=_dynamic_momentum_rank_map.get(sym_u),
                                    )

                                def _log_dynamic_selected_dropped(
                                    reason: str,
                                    *,
                                    stage: str = "pre_entry",
                                    detail: str | None = None,
                                ) -> None:
                                    if _is_dynamic_added or _is_dynamic_candidate:
                                        _reason_clean = str(reason or "unknown")
                                        if sym_u in _scanner_selected_dynamic_set:
                                            _dynamic_entry_eval_dropped_symbols.add(sym_u)
                                            _log_dynamic_entry_candidate_skipped(
                                                sym_u,
                                                reason=_reason_clean,
                                            )
                                            _log_dynamic_entry_eval_dropped(
                                                sym_u,
                                                reason=_reason_clean,
                                            )
                                        _log_dynamic_selected_entry_drop(
                                            sym_u,
                                            stage=stage,
                                            reason=_reason_clean,
                                            detail=detail,
                                        )
                                        log.info(
                                            "DYNAMIC_SELECTED_DROPPED symbol=%s reason=%s",
                                            sym_u,
                                            _reason_clean,
                                        )
                                        log.info(
                                            "DYNAMIC_SELECTED_ENTRY_SKIPPED symbol=%s reason=%s",
                                            sym_u,
                                            _reason_clean,
                                        )

                                _fastlane_meta_sym = _strong_dynamic_persistent_map.get(sym_u, {})
                                _fastlane_allowed_sym, _fastlane_reason_sym = _dynamic_fastlane_allowed(
                                    dt,
                                    news_score=_fastlane_meta_sym.get("news_score"),
                                    catalyst_age_minutes=_fastlane_meta_sym.get("age_minutes"),
                                )
                                if _is_dynamic_candidate:
                                    _log_dynamic_fastlane(
                                        sym_u,
                                        news_score=_fastlane_meta_sym.get("news_score"),
                                        catalyst_age_minutes=_fastlane_meta_sym.get("age_minutes"),
                                        allowed=_fastlane_allowed_sym,
                                        reason=_fastlane_reason_sym,
                                    )
                                (
                                    _rank_art_news,
                                    _rank_art_event,
                                    _rank_art_catalyst,
                                ) = _premarket_artifact_score_fields(
                                    _premarket_artifacts,
                                    sym_u,
                                )
                                _rank_art_type, _rank_art_age = _premarket_artifact_metadata_fields(
                                    _premarket_artifacts,
                                    sym_u,
                                )
                                try:
                                    _rank_engine_news = float(
                                        (getattr(engine, "dynamic_news_scores", {}) or {}).get(sym_u, 0.0)
                                        or 0.0
                                    )
                                except (TypeError, ValueError):
                                    _rank_engine_news = 0.0
                                try:
                                    _rank_engine_event = float(
                                        (getattr(engine, "dynamic_event_scores", {}) or {}).get(sym_u, 0.0)
                                        or 0.0
                                    )
                                except (TypeError, ValueError):
                                    _rank_engine_event = 0.0
                                try:
                                    _rank_engine_catalyst = float(
                                        (getattr(engine, "dynamic_catalyst_scores", {}) or {}).get(sym_u, 0.0)
                                        or 0.0
                                    )
                                except (TypeError, ValueError):
                                    _rank_engine_catalyst = 0.0
                                _rank_fastlane_age = (
                                    _rank_art_age
                                    if _rank_art_age is not None
                                    else _fastlane_meta_sym.get("age_minutes")
                                )
                                _rank_fastlane_news = max(float(_rank_art_news), float(_rank_engine_news))
                                _rank_fastlane_event = max(float(_rank_art_event), float(_rank_engine_event))
                                _rank_fastlane_catalyst = max(float(_rank_art_catalyst), float(_rank_engine_catalyst))
                                _rank_fastlane_rel = _dynamic_momentum_rel_volumes.get(sym_u)
                                _rank_artifact_row_for_fastlane = (
                                    _premarket_artifacts.get(sym_u)
                                    if isinstance(_premarket_artifacts, Mapping)
                                    else None
                                )
                                _rank_premarket_metadata_confirmed = (
                                    _premarket_artifact_has_confirmed_metadata(_rank_artifact_row_for_fastlane)
                                )
                                _rank_fastlane_threshold = 0.35
                                try:
                                    if isinstance(_dme_cfg_rank, Mapping):
                                        _rank_fastlane_threshold = float(
                                            _dme_cfg_rank.get("catalyst_min_relative_volume", 0.35) or 0.35
                                        )
                                except (TypeError, ValueError):
                                    _rank_fastlane_threshold = 0.35
                                _rank_fastlane_trace = _catalyst_fastlane_entry_trace_fields(
                                    premarket_injected=(
                                        sym_u in _premarket_injected_symbol_set
                                        and _rank_premarket_metadata_confirmed
                                    ),
                                    news_score=_rank_fastlane_news,
                                    event_score=_rank_fastlane_event,
                                    catalyst_score=_rank_fastlane_catalyst,
                                    catalyst_age_minutes=_rank_fastlane_age,
                                    relative_volume=_rank_fastlane_rel,
                                    threshold=_rank_fastlane_threshold,
                                )
                                if _is_dynamic_candidate:
                                    _log_catalyst_fastlane_entry_trace(sym_u, _rank_fastlane_trace)
                                _premarket_catalyst_fastlane_rank = _premarket_catalyst_fastlane_signal(
                                    premarket_injected=(
                                        sym_u in _premarket_injected_symbol_set
                                        and _rank_premarket_metadata_confirmed
                                    ),
                                    news_score=_rank_fastlane_news,
                                    event_score=_rank_fastlane_event,
                                    catalyst_score=_rank_fastlane_catalyst,
                                    catalyst_age_minutes=_rank_fastlane_age,
                                )
                                if (
                                    _is_dynamic_candidate
                                    and _dynamic_momentum_allowlist is not None
                                    and sym_u not in _dynamic_momentum_allowlist
                                ):
                                    if _premarket_catalyst_fastlane_rank and bool(_rank_fastlane_trace.get("eligible")):
                                        log.info(
                                            "CATALYST_FASTLANE_BYPASS_RANK symbol=%s",
                                            sym_u,
                                        )
                                    elif _fastlane_allowed_sym:
                                        log.info(
                                            "DYNAMIC_FASTLANE_BYPASS symbol=%s news_score=%s catalyst_age_minutes=%s reason=momentum_rank",
                                            sym_u,
                                            str(_fastlane_meta_sym.get("news_score", "n/a")),
                                            str(_fastlane_meta_sym.get("age_minutes", "n/a")),
                                        )
                                    else:
                                        _rank_score_text = (
                                            "%.4f" % float(_dynamic_momentum_scores[sym_u])
                                            if sym_u in _dynamic_momentum_scores
                                            else "n/a"
                                        )
                                        _log_entry_skip(
                                            dt,
                                            symbol,
                                            "dynamic momentum rank: not in top %d (score=%s)"
                                            % (_dynamic_momentum_top_n, _rank_score_text),
                                            verbose=verbose,
                                            force=False,
                                        )
                                        log.info(
                                            "DYNAMIC_MOMENTUM_RANK_NONBLOCKING symbol=%s top_n=%d score=%s",
                                            sym_u,
                                            int(_dynamic_momentum_top_n),
                                            _rank_score_text,
                                        )
                                _smin_symbol = (
                                    max(
                                        0.0,
                                        min(
                                            1.0,
                                            float(_smin_full) * float(_dyn_rr_mult),
                                        ),
                                    )
                                    if _is_dynamic_candidate
                                    else float(_smin_full)
                                )
                                _alloc_min_sym = (
                                    float(_alloc_min_signal_strength)
                                    * float(_dyn_rr_mult)
                                    if _is_dynamic_candidate
                                    else float(_alloc_min_signal_strength)
                                )
                                _sym_entry_cd = effective_per_symbol_buy_cooldown_min(
                                    _entries_cd, sym_u
                                )
                                _effective_allow_add = effective_allow_add_after_capital_trim(
                                    sym_u,
                                    portfolio_allow_add=_port_allow_add,
                                    symbols_trimmed_this_scan=_rfc_trimmed_symbols,
                                )
                                _max_alloc_sym_pct_symbol = effective_symbol_allocation_cap_pct(
                                    config,
                                    account_equity=float(account_equity),
                                    regime_score=_reg_score_for_scoring,
                                    symbol_upper=sym_u,
                                )
                                _has_sym_headroom = symbol_position_has_headroom_below_cap(
                                    sym_u,
                                    positions=positions,
                                    account_equity=float(account_equity),
                                    max_alloc_sym_pct=float(_max_alloc_sym_pct_symbol),
                                    max_pos_mval_usd=float(_max_pos_mval_usd),
                                )
                                if (
                                    not _effective_allow_add
                                    and _port_allow_add_on_strong_momentum
                                    and (sym_u in current_positions or sym_u in tracked)
                                    and (
                                        _has_sym_headroom
                                        or (
                                            bool(_pyramid_winners_cfg.get("enabled"))
                                            and not _has_sym_headroom
                                        )
                                    )
                                ):
                                    try:
                                        _df_mom = broker.get_bars(symbol, timeframe="1Day", limit=220)
                                        if not _df_mom.empty:
                                            _strong_trend = (
                                                engine.strategy.strong_trend_reconfirm_ok(
                                                    symbol, _df_mom, _reg_score_for_scoring
                                                )
                                                if engine.strategy.strong_trend_reconfirm_bypass_cooldown
                                                else engine.strategy.strong_momentum_structure_ok(
                                                    symbol, _df_mom, _reg_score_for_scoring
                                                )
                                            )
                                            _profit_pct = symbol_long_unrealized_pl_pct(
                                                sym_u, positions=positions
                                            )
                                            try:
                                                _pmin = float(
                                                    _pyramid_winners_cfg.get(
                                                        "min_unrealized_profit_pct", 5.0
                                                    )
                                                )
                                            except (TypeError, ValueError):
                                                _pmin = 5.0
                                            _profit_ok = (
                                                _profit_pct is not None
                                                and float(_profit_pct) >= _pmin - 1e-9
                                            )
                                            if _strong_trend:
                                                if _has_sym_headroom:
                                                    _effective_allow_add = True
                                                elif (
                                                    bool(_pyramid_winners_cfg.get("enabled"))
                                                    and _profit_ok
                                                ):
                                                    _effective_allow_add = True
                                                    _pyramid_relax_symbol_cap = True
                                    except Exception:
                                        pass
                                _holds_equity = effective_hold_for_risk(
                                    sym_u, current_positions, tracked
                                )
                                if (
                                    _risk_max_new > 0
                                    and int(_cycle_risk_state.get("new_stock", 0)) >= _risk_max_new
                                    and not _holds_equity
                                ):
                                    _log_entry_skip(
                                        dt,
                                        symbol,
                                        "risk max new positions per cycle (%d)" % _risk_max_new,
                                        verbose=verbose,
                                        force=True,
                                    )
                                    _log_dynamic_selected_dropped(
                                        "risk_max_new_positions_per_cycle",
                                        stage="max_positions",
                                        detail="risk_max_new=%d" % int(_risk_max_new),
                                    )
                                    continue
                                if _effective_allow_add and _holds_equity:
                                    _ok_add_d, _r_add_d = add_on_allowed_for_daily_cap(
                                        tracked, sym_u, _et_date_iso, _risk_max_adds
                                    )
                                    if not _ok_add_d:
                                        _log_entry_skip(
                                            dt,
                                            symbol,
                                            _r_add_d or "risk add-on cap",
                                            verbose=verbose,
                                            force=True,
                                        )
                                        _log_dynamic_selected_dropped(
                                            _r_add_d or "risk_add_on_cap",
                                            stage="position_constraint",
                                        )
                                        continue
                                    _ok_add_m, _r_add_m = add_on_allowed_for_min_minutes(
                                        tracked, sym_u, dt, _risk_min_add_gap
                                    )
                                    if not _ok_add_m:
                                        _log_entry_skip(
                                            dt,
                                            symbol,
                                            _r_add_m or "risk add-on spacing",
                                            verbose=verbose,
                                            force=True,
                                        )
                                        _log_dynamic_selected_dropped(
                                            _r_add_m or "risk_add_on_spacing",
                                            stage="position_constraint",
                                        )
                                        continue
                                if _max_pos_mval_usd > 0 or _max_alloc_sym_pct_symbol > 0:
                                    _pos_sz = symbol_long_position_market_value_usd(
                                        positions, sym_u
                                    )
                                    if _max_pos_mval_usd > 0 and _pos_sz > _max_pos_mval_usd + 1e-6:
                                        _log_entry_skip(
                                            dt,
                                            symbol,
                                            "position size $%.0f > max allowed $%.0f"
                                            % (_pos_sz, _max_pos_mval_usd),
                                            verbose=verbose,
                                            force=True,
                                        )
                                        _log_dynamic_selected_dropped(
                                            "position_size_above_max",
                                            stage="position_constraint",
                                            detail="position_size_above_max",
                                        )
                                        continue
                                    if (
                                        _max_alloc_sym_pct_symbol > 0
                                        and float(account_equity) > 0
                                        and _pos_sz
                                        > float(account_equity) * (_max_alloc_sym_pct_symbol / 100.0) + 1e-6
                                        and not _pyramid_relax_symbol_cap
                                    ):
                                        _eq_a = float(account_equity)
                                        _cur_pct = (_pos_sz / _eq_a) * 100.0 if _eq_a > 0 else 0.0
                                        _log_entry_skip(
                                            dt,
                                            symbol,
                                            "symbol allocation %.1f%% >= cap %.1f%%"
                                            % (_cur_pct, float(_max_alloc_sym_pct_symbol)),
                                            verbose=verbose,
                                            force=True,
                                        )
                                        _log_dynamic_selected_dropped(
                                            "symbol_allocation_cap",
                                            stage="position_constraint",
                                            detail="symbol_allocation_cap",
                                        )
                                        continue
                                if sym_u in current_positions:
                                    if not _effective_allow_add and not bool(
                                        _add_on_gate_cfg.get("enabled")
                                    ):
                                        _log_entry_skip(
                                            dt,
                                            symbol,
                                            "already in positions",
                                            verbose=verbose,
                                            force=False,
                                        )
                                        _log_dynamic_selected_dropped(
                                            "already_in_positions",
                                            stage="existing_position",
                                        )
                                        continue
                                if sym_u in open_order_symbols:
                                    _log_entry_skip(
                                        dt,
                                        symbol,
                                        "open order pending",
                                        verbose=verbose,
                                        force=False,
                                    )
                                    _log_dynamic_selected_dropped(
                                        "open_order_pending",
                                        stage="existing_order",
                                    )
                                    continue
                                if sym_u in tracked:
                                    if not _effective_allow_add and not bool(
                                        _add_on_gate_cfg.get("enabled")
                                    ):
                                        _log_entry_skip(
                                            dt,
                                            symbol,
                                            "in tracked state (pending exit/sync)",
                                            verbose=verbose,
                                            force=False,
                                        )
                                        _log_dynamic_selected_dropped(
                                            "tracked_state_pending_exit_sync",
                                            stage="existing_position",
                                        )
                                        continue
                                    _broker_qty_add = 0
                                    for _p in positions:
                                        if str(_p.get("symbol") or "").upper() == sym_u:
                                            _broker_qty_add = int(float(_p.get("qty") or 0))
                                            break
                                    if _broker_qty_add <= 0:
                                        _log_entry_skip(
                                            dt,
                                            symbol,
                                            "in tracked state (pending exit/sync)",
                                            verbose=verbose,
                                            force=False,
                                        )
                                        _log_dynamic_selected_dropped(
                                            "tracked_state_pending_exit_sync",
                                            stage="existing_position",
                                            detail="broker_qty<=0",
                                        )
                                        continue
                                if should_apply_scoring_gate(
                                    scoring_allowed=allowed_symbols,
                                    sym_upper=sym_u,
                                    current_positions=set(current_positions.keys()),
                                    tracked_keys_upper=_tracked_keys_upper,
                                ):
                                    if sym_u not in allowed_symbols and not _is_dynamic_added:
                                        _log_entry_skip(
                                        dt,
                                        symbol,
                                        "not in scoring top_n_candidates set",
                                        verbose=verbose,
                                        )
                                        _log_dynamic_selected_dropped(
                                            "not_in_scoring_top_n_candidates",
                                            stage="scoring_top_n",
                                            detail=(
                                                "in_scoring_top_n=false dynamic_bypass_applied=%s"
                                                % str(bool(_is_dynamic_added)).lower()
                                            ),
                                        )
                                        continue

                                try:
                                    df = broker.get_bars(symbol, timeframe="1Day", limit=220)
                                    min_hist = engine.strategy.min_history_bars_for_entry(symbol)
                                    need, default_need, _history_experiment_active = _dynamic_daily_history_requirement(
                                        config,
                                        symbol=symbol,
                                        ma_slow_period=ma_slow_period,
                                        min_history_bars=min_hist,
                                        is_dynamic_candidate=bool(_is_dynamic_added or _is_dynamic_candidate),
                                        broker_is_paper=bool(getattr(_uctx, "paper", False)),
                                        candidate_type=str(_symbol_class),
                                    )
                                    _got_bars = len(df) if not df.empty else 0
                                    if _is_dynamic_added or _is_dynamic_candidate:
                                        _history_mode = "paper" if bool(getattr(_uctx, "paper", False)) else "live"
                                        _history_candidate_type = str(_symbol_class or "unknown").strip().lower()
                                        log.info(
                                            "DYNAMIC_HISTORY_REQUIREMENT symbol=%s mode=%s candidate_type=%s required_bars=%d available_bars=%d",
                                            sym_u,
                                            _history_mode,
                                            _history_candidate_type,
                                            int(need),
                                            int(_got_bars),
                                        )
                                        log.info(
                                            "DYNAMIC_HISTORY_CONFIG min_history_bars=%d symbol=%s got_bars=%d default_need=%d",
                                            int(need),
                                            sym_u,
                                            int(_got_bars),
                                            int(default_need),
                                        )
                                    if _history_experiment_active:
                                        log.info(
                                            "DYNAMIC_HISTORY_EXPERIMENT symbol=%s got=%d need=%d default_need=%d mode=paper",
                                            sym_u,
                                            int(_got_bars),
                                            int(need),
                                            int(default_need),
                                        )
                                    if df.empty or len(df) < need:
                                        (
                                            _artifact_news_score_pre,
                                            _artifact_event_score_pre,
                                            _artifact_catalyst_score_pre,
                                        ) = _premarket_artifact_score_fields(
                                            _premarket_artifacts,
                                            sym_u,
                                        )
                                        try:
                                            _engine_news_score_pre = float(
                                                (getattr(engine, "dynamic_news_scores", {}) or {}).get(sym_u, 0.0)
                                                or 0.0
                                            )
                                        except (TypeError, ValueError):
                                            _engine_news_score_pre = 0.0
                                        try:
                                            _engine_event_score_pre = float(
                                                (getattr(engine, "dynamic_event_scores", {}) or {}).get(sym_u, 0.0)
                                                or 0.0
                                            )
                                        except (TypeError, ValueError):
                                            _engine_event_score_pre = 0.0
                                        try:
                                            _engine_catalyst_score_pre = float(
                                                (getattr(engine, "dynamic_catalyst_scores", {}) or {}).get(sym_u, 0.0)
                                                or 0.0
                                            )
                                        except (TypeError, ValueError):
                                            _engine_catalyst_score_pre = 0.0
                                        _history_scanner_meta = _dynamic_scan_accepted_meta.get(sym_u, {})
                                        _short_history_ok, _short_history_reason = _dynamic_short_history_fallback_decision(
                                            config,
                                            is_dynamic_candidate=bool(_is_dynamic_candidate),
                                            available_bars=_got_bars,
                                            required_bars=need,
                                            news_score=max(_artifact_news_score_pre, _engine_news_score_pre),
                                            event_score=max(_artifact_event_score_pre, _engine_event_score_pre),
                                            catalyst_score=max(_artifact_catalyst_score_pre, _engine_catalyst_score_pre),
                                            scanner_selected=bool(_history_scanner_meta),
                                            scanner_meta=_history_scanner_meta,
                                        )
                                        if _short_history_ok:
                                            log.info(
                                                "DYNAMIC_SHORT_HISTORY_FALLBACK symbol=%s bars=%d need=%d news_score=%.2f event_score=%.2f catalyst_score=%.2f reason=%s",
                                                sym_u,
                                                int(_got_bars),
                                                int(need),
                                                float(max(_artifact_news_score_pre, _engine_news_score_pre)),
                                                float(max(_artifact_event_score_pre, _engine_event_score_pre)),
                                                float(max(_artifact_catalyst_score_pre, _engine_catalyst_score_pre)),
                                                _short_history_reason,
                                            )
                                            if _short_history_reason == "scanner_selected_dynamic_momentum":
                                                log.info(
                                                    "DYNAMIC_SHORT_HISTORY_SCANNER_SELECTED_BYPASS symbol=%s bars=%d need=%d reason=%s",
                                                    sym_u,
                                                    int(_got_bars),
                                                    int(need),
                                                    _short_history_reason,
                                                )
                                        else:
                                            _final_reason = "not enough bars (got %d, need %d)" % (_got_bars, need)
                                            if _is_dynamic_candidate:
                                                log.info(
                                                    "DYNAMIC_HISTORY_BLOCK symbol=%s bars=%d need=%d scanner_selected=%s reason=%s",
                                                    sym_u,
                                                    int(_got_bars),
                                                    int(need),
                                                    str(bool(_history_scanner_meta)).lower(),
                                                    _short_history_reason,
                                                )
                                                log.info(
                                                    "DYNAMIC_NOT_TRADABLE symbol=%s reason=%s detail=%s news_score=%.2f event_score=%.2f catalyst_score=%.2f",
                                                    sym_u,
                                                    _final_reason,
                                                    _short_history_reason,
                                                    float(max(_artifact_news_score_pre, _engine_news_score_pre)),
                                                    float(max(_artifact_event_score_pre, _engine_event_score_pre)),
                                                    float(max(_artifact_catalyst_score_pre, _engine_catalyst_score_pre)),
                                                )
                                            _log_entry_skip(
                                                dt,
                                                symbol,
                                                _final_reason,
                                                verbose=verbose,
                                                force=False,
                                            )
                                            _log_dynamic_selected_dropped(
                                                "short_history",
                                                stage="history_guard",
                                                detail="got=%d need=%d fallback=%s"
                                                % (
                                                    int(_got_bars),
                                                    int(need),
                                                    str(_short_history_reason),
                                                ),
                                            )
                                            continue
                                    close = float(df["close"].iloc[-1])
                                    # Trend prefilter: above MAs (configurable) OR news+volume override
                                    trend_long_ok = True
                                    skip_ma_check = str(symbol).upper() == "SQQQ"
                                    skip_pullback_check = str(symbol).upper() == "SQQQ"
                                    ma_fast: float | None = None
                                    ma_slow: float | None = None
                                    if not (skip_ma_check and skip_pullback_check):
                                        if len(df) >= ma_fast_period:
                                            ma_fast = float(df["close"].rolling(ma_fast_period).mean().iloc[-1])
                                        if len(df) >= ma_slow_period:
                                            ma_slow = float(df["close"].rolling(ma_slow_period).mean().iloc[-1])
                                        _tf_cfg = (config.get("strategy") or {}).get("trend_filter")
                                        trend_long_ok = trend_long_scan_ma_filter_ok(
                                            close=close,
                                            ma_fast=ma_fast,
                                            ma_slow=ma_slow,
                                            trend_filter_cfg=_tf_cfg if isinstance(_tf_cfg, dict) else None,
                                            long_require_ma_stack=regime_entry_policy.long_require_ma_stack,
                                        )

                                    _sym_cd_window = float(_sym_entry_cd) * float(
                                        _dt_mult_cd
                                    )
                                    _cooldown_active = _sym_entry_cd > 0 and last_entry_within(
                                        sym_u,
                                        _sym_cd_window,
                                        tracked=tracked,
                                        now_dt=dt,
                                    )
                                    if _cooldown_active:
                                        _age_m_cd = last_tracker_fill_age_minutes(
                                            sym_u, tracked=tracked, now_dt=dt
                                        )
                                        _ago_i_cd = int(_age_m_cd) if _age_m_cd is not None else 0
                                        try:
                                            _dyn_score_cd = float(_dynamic_momentum_scores.get(sym_u, 0.0) or 0.0)
                                        except (TypeError, ValueError):
                                            _dyn_score_cd = 0.0
                                        _news_map_cd = getattr(engine, "dynamic_news_scores", {}) or {}
                                        try:
                                            _news_score_cd = float(_news_map_cd.get(sym_u, 0.0) or 0.0)
                                        except (TypeError, ValueError):
                                            _news_score_cd = 0.0
                                        if (
                                            _is_dynamic_candidate
                                            and (_dyn_score_cd > 250.0 or _news_score_cd >= 8.0)
                                        ):
                                            log.info(
                                                "DYNAMIC_HIGH_CONVICTION_BLOCKED symbol=%s reason=cooldown dynamic_score=%.2f news_score=%.2f",
                                                sym_u,
                                                float(_dyn_score_cd),
                                                float(_news_score_cd),
                                            )
                                        _entry_decision_counts["blocked_cooldown"] += 1
                                    if _cooldown_active:
                                        _tr_cd = tracked.get(sym_u) or {}
                                        _entry_iso_cd = str(_tr_cd.get("entry_time") or "")
                                        _pos_for_score = position_dict_for_signal_score(sym_u, positions)
                                        _pos_for_score["bars_held"] = (
                                            bars_held(_entry_iso_cd, dt) if _entry_iso_cd else 0
                                        )
                                        _md_score: dict[str, Any] = {}
                                        if ma_fast is not None and ma_slow is not None:
                                            _md_score[sym_u] = {
                                                "close": close,
                                                "ma_fast": ma_fast,
                                                "ma_slow": ma_slow,
                                            }
                                        _signal_score_cd = score_position(_pos_for_score, _md_score)
                                        if _signal_score_cd < COOLDOWN_BYPASS_MIN_SIGNAL_SCORE:
                                            _log_entry_skip(
                                                dt,
                                                symbol,
                                                "last fill %dm ago < %.0fm cooldown (signal_score=%d < %d)"
                                                % (
                                                    _ago_i_cd,
                                                    float(_sym_cd_window),
                                                    _signal_score_cd,
                                                    COOLDOWN_BYPASS_MIN_SIGNAL_SCORE,
                                                ),
                                                verbose=verbose,
                                                force=False,
                                            )
                                            _log_dynamic_selected_dropped(
                                                "cooldown",
                                                stage="cooldown",
                                                detail=(
                                                    "age_minutes=%d cooldown_minutes=%.0f signal_score=%d"
                                                    % (
                                                        _ago_i_cd,
                                                        float(_sym_cd_window),
                                                        int(_signal_score_cd),
                                                    )
                                                ),
                                            )
                                            continue

                                    sentiment_score = 0.0
                                    vol_ratio = None
                                    news_buy = False
                                    if (
                                        news_enabled
                                        and news_pipeline
                                        and news_rules
                                        and _is_dynamic_candidate
                                    ):
                                        sentiment_score = news_pipeline.sentiment_for_symbol(symbol)
                                        vol_ratio = volume_spike_ratio(df, news_vol_lookback)
                                        news_buy = news_rules.should_buy(sentiment_score, vol_ratio)
                                    if _news_override_mode == "off":
                                        news_buy = False

                                    _tr_add = tracked.get(sym_u) or {}
                                    _qty_tr = int(float(_tr_add.get("qty") or 0))
                                    _is_add_flow = (
                                        sym_u in current_positions
                                        or _qty_tr > 0
                                        or tracked_row_has_open_long(_tr_add)
                                    )

                                    _entry_regime_score = (
                                        regime_result.score if regime_result is not None else None
                                    )
                                    _atr_row = _atr(df["high"], df["low"], df["close"], 14)
                                    atr_pct = None
                                    if len(_atr_row) and _atr_row.iloc[-1] == _atr_row.iloc[-1] and close > 0:
                                        atr_pct = float((_atr_row.iloc[-1] / close) * 100.0)
                                    _ai_catalyst_score: int | None = None
                                    _ai_catalyst_summary: str | None = None

                                    _ae_cfg = (config.get("strategy") or {}).get("alternate_entries")
                                    _alt_match = None
                                    if (
                                        isinstance(_ae_cfg, dict)
                                        and _ae_cfg.get("enabled")
                                        and not trend_long_ok
                                        and not news_buy
                                        and not (
                                            _is_add_flow and not bool(_ae_cfg.get("allow_for_add_on", False))
                                        )
                                    ):
                                        _alt_match = evaluate_alternate_entries(
                                            df,
                                            config,
                                            trend_long_ok=False,
                                            regime_score=_entry_regime_score,
                                            atr_pct=atr_pct,
                                            symbol_upper=sym_u,
                                            breakout_intraday_allowlist=_breakout_candidate_symbols,
                                            skip_breakout_intraday_prefilter=_is_dynamic_candidate,
                                        )

                                    if _effective_allow_add and _add_ratio is not None:
                                        if _is_add_flow and not add_on_pullback_or_momentum_ok(
                                            close,
                                            _tr_add.get("last_entry_price"),
                                            _add_ratio,
                                            allow_momentum_bypass=_add_on_momentum_bypass,
                                            trend_long_ok=trend_long_ok,
                                            news_buy=news_buy,
                                        ):
                                            _log_entry_skip(
                                                dt,
                                                symbol,
                                                "add-on: price not below last entry × %.2f (momentum bypass disabled or trend/news off)"
                                                % float(_add_ratio),
                                                verbose=verbose,
                                                force=False,
                                            )
                                            _log_dynamic_selected_dropped(
                                                "add_on_pullback_or_momentum_failed",
                                                stage="position_constraint",
                                                detail="add_ratio=%.2f" % float(_add_ratio),
                                            )
                                            continue

                                    _dmo_cfg = config.get("dynamic_momentum_override") or {}
                                    _dmo_pullback_bypass = (
                                        isinstance(_dmo_cfg, dict)
                                        and bool(_dmo_cfg.get("enabled"))
                                        and _is_dynamic_candidate
                                        and bool(_dmo_cfg.get("allow_without_pullback"))
                                    )
                                    _news_score_dyn = 0
                                    _event_score_dyn = 0.0
                                    _news_score_effective_dyn = 0.0
                                    _news_headline_dyn = ""
                                    _news_catalyst_age_minutes_dyn: float | None = None
                                    _artifact_catalyst_type = ""
                                    _artifact_catalyst_age_minutes: float | None = None
                                    (
                                        _artifact_catalyst_type,
                                        _artifact_catalyst_age_minutes,
                                    ) = _premarket_artifact_metadata_fields(
                                        _premarket_artifacts,
                                        sym_u,
                                    )
                                    if _has_dynamic_signal or _is_dynamic_candidate:
                                        try:
                                            _news_score_dyn = int(
                                                (
                                                    getattr(
                                                        engine,
                                                        "dynamic_news_scores",
                                                        {},
                                                    )
                                                    or {}
                                                ).get(sym_u, 0)
                                                or 0
                                            )
                                        except (TypeError, ValueError):
                                            _news_score_dyn = 0
                                        try:
                                            _event_score_dyn = float(
                                                (
                                                    getattr(
                                                        engine,
                                                        "dynamic_event_scores",
                                                        {},
                                                    )
                                                    or {}
                                                ).get(sym_u, 0.0)
                                                or 0.0
                                            )
                                        except (TypeError, ValueError):
                                            _event_score_dyn = 0.0
                                        _news_headline_dyn = str(
                                            (
                                                getattr(
                                                    engine,
                                                    "dynamic_news_headlines",
                                                    {},
                                                )
                                                or {}
                                            ).get(sym_u, "")
                                            or ""
                                        )
                                        try:
                                            cached_news_score, cached_news_reason = get_cached_news_score(
                                                sym_u,
                                                now=dt,
                                                max_age_seconds=300.0,
                                            )
                                            if cached_news_reason != "cache":
                                                fetch_recent_news_catalysts(
                                                    broker,
                                                    [sym_u],
                                                    config=config,
                                                    now=dt,
                                                    max_age_seconds=300.0,
                                                )
                                                cached_news_score, cached_news_reason = get_cached_news_score(
                                                    sym_u,
                                                    now=dt,
                                                    max_age_seconds=300.0,
                                                )
                                            cached_cat = get_cached_news_catalyst(
                                                sym_u,
                                                now=dt,
                                                max_age_seconds=300.0,
                                                emit_log=False,
                                            )
                                            if isinstance(_premarket_artifacts, dict):
                                                try:
                                                    if _artifact_catalyst_age_minutes is not None:
                                                        _news_catalyst_age_minutes_dyn = _artifact_catalyst_age_minutes
                                                except Exception:
                                                    pass
                                            if cached_cat is not None:
                                                _news_score_dyn = int(cached_cat.score or 0)
                                                _event_score_dyn = max(
                                                    float(_event_score_dyn),
                                                    float(cached_cat.score or 0),
                                                )
                                                _news_headline_dyn = str(cached_cat.headline or "")
                                                if cached_cat.published_at is not None:
                                                    try:
                                                        _pub_dt_dyn = cached_cat.published_at
                                                        if getattr(_pub_dt_dyn, "tzinfo", None) is None:
                                                            _pub_dt_dyn = _pub_dt_dyn.replace(tzinfo=pytz.UTC)
                                                        _news_catalyst_age_minutes_dyn = max(
                                                            0.0,
                                                            (
                                                                dt.astimezone(pytz.UTC)
                                                                - _pub_dt_dyn.astimezone(pytz.UTC)
                                                            ).total_seconds()
                                                            / 60.0,
                                                        )
                                                    except Exception:
                                                        _news_catalyst_age_minutes_dyn = None
                                                try:
                                                    engine.dynamic_news_scores[sym_u] = _news_score_dyn
                                                    if _news_headline_dyn:
                                                        engine.dynamic_news_headlines[sym_u] = _news_headline_dyn
                                                    if cached_cat.catalyst_type:
                                                        engine.dynamic_news_catalyst_types[sym_u] = str(
                                                            cached_cat.catalyst_type
                                                        )
                                                    if _event_score_dyn > 0:
                                                        engine.dynamic_event_scores[sym_u] = float(_event_score_dyn)
                                                except Exception:
                                                    pass
                                            else:
                                                _news_score_dyn = int(cached_news_score or 0)
                                            _news_score_effective_dyn = max(
                                                float(_news_score_dyn or 0),
                                                float(_event_score_dyn or 0.0),
                                            )
                                            if _news_score_dyn or cached_news_reason != "cache":
                                                log.info(
                                                    "NEWS_SCORE symbol=%s score=%d event_score=%.2f reason=%s",
                                                    sym_u,
                                                    int(_news_score_dyn or 0),
                                                    float(_event_score_dyn or 0.0),
                                                    cached_news_reason,
                                                )
                                        except Exception:
                                            log.debug(
                                                "[%s] dynamic entry news refresh check failed for %s",
                                                _uid,
                                                sym_u,
                                                exc_info=True,
                                            )
                                    else:
                                        _news_score_effective_dyn = max(
                                            float(_news_score_dyn or 0),
                                            float(_event_score_dyn or 0.0),
                                        )
                                    _gain_pct_e: float | None = None
                                    _rel_vol_e: float | None = None
                                    _dyn_snap_prefetch: dict[str, Any] = {}
                                    if _is_dynamic_candidate:
                                        try:
                                            if hasattr(broker, "get_snapshot"):
                                                _raw_snap_prefetch = broker.get_snapshot(symbol)
                                                if isinstance(_raw_snap_prefetch, dict):
                                                    _dyn_snap_prefetch = _raw_snap_prefetch
                                        except Exception:
                                            _dyn_snap_prefetch = {}
                                        try:
                                            _gain_pct_e = float(_dyn_snap_prefetch.get("day_gain_pct"))
                                            if not math.isfinite(_gain_pct_e):
                                                _gain_pct_e = None
                                        except (TypeError, ValueError):
                                            _gain_pct_e = None
                                        if _gain_pct_e is None and df is not None and not getattr(df, "empty", True):
                                            try:
                                                if len(df) >= 2:
                                                    _pc = float(df["close"].iloc[-2])
                                                    _lc = float(df["close"].iloc[-1])
                                                    if _pc > 0:
                                                        _gain_pct_e = (_lc / _pc - 1.0) * 100.0
                                            except (TypeError, ValueError, KeyError):
                                                pass
                                        try:
                                            _vol_prefetch = float(_dyn_snap_prefetch.get("volume", 0) or 0)
                                        except (TypeError, ValueError):
                                            _vol_prefetch = 0.0
                                        _avg_v_prefetch = 1.0
                                        if hasattr(broker, "get_avg_volume"):
                                            try:
                                                _avg_v_prefetch = float(broker.get_avg_volume(symbol))
                                            except Exception:
                                                _avg_v_prefetch = 1.0
                                        _avg_v_prefetch = max(1.0, _avg_v_prefetch)
                                        if _vol_prefetch > 0:
                                            _rel_vol_e = _vol_prefetch / _avg_v_prefetch
                                        if _rel_vol_e is None and df is not None and not getattr(df, "empty", True):
                                            try:
                                                _dv = float(df["volume"].iloc[-1])
                                                if _dv > 0 and _dv == _dv:
                                                    _rel_vol_e = _dv / _avg_v_prefetch
                                            except (TypeError, ValueError, KeyError):
                                                pass
                                    _dynamic_spread_override_cap = dynamic_entry_spread_override_cap(
                                        gain_pct=_gain_pct_e,
                                        relative_volume=_rel_vol_e,
                                    )
                                    _news_early_prefilter = False
                                    (
                                        _artifact_news_score,
                                        _artifact_event_score,
                                        _artifact_catalyst_score,
                                    ) = _premarket_artifact_score_fields(
                                        _premarket_artifacts,
                                        sym_u,
                                    )
                                    try:
                                        _engine_news_score = float(
                                            (getattr(engine, "dynamic_news_scores", {}) or {}).get(
                                                sym_u,
                                                0.0,
                                            )
                                            or 0.0
                                        )
                                    except (TypeError, ValueError):
                                        _engine_news_score = 0.0
                                    try:
                                        _engine_event_score = float(
                                            (getattr(engine, "dynamic_event_scores", {}) or {}).get(
                                                sym_u,
                                                0.0,
                                            )
                                            or 0.0
                                        )
                                    except (TypeError, ValueError):
                                        _engine_event_score = 0.0
                                    try:
                                        _engine_catalyst_score = float(
                                            (getattr(engine, "dynamic_catalyst_scores", {}) or {}).get(
                                                sym_u,
                                                0.0,
                                            )
                                            or 0.0
                                        )
                                    except (TypeError, ValueError):
                                        _engine_catalyst_score = 0.0
                                    _news_trend_debug_news_score = max(
                                        float(_news_score_effective_dyn or 0.0),
                                        float(_artifact_news_score or 0.0),
                                        float(_engine_news_score or 0.0),
                                    )
                                    _news_trend_debug_event_score = max(
                                        float(_event_score_dyn or 0.0),
                                        float(_artifact_event_score or 0.0),
                                        float(_engine_event_score or 0.0),
                                    )
                                    _news_trend_debug_catalyst_score = max(
                                        float(_artifact_catalyst_score or 0.0),
                                        float(_engine_catalyst_score or 0.0),
                                    )
                                    (
                                        _news_trend_override,
                                        _news_trend_override_score,
                                        _news_trend_override_threshold,
                                    ) = _news_trend_prefilter_override_decision(
                                        config,
                                        news_score=_news_trend_debug_news_score,
                                        event_score=_news_trend_debug_event_score,
                                        catalyst_score=_news_trend_debug_catalyst_score,
                                    )
                                    if _is_dynamic_candidate and _news_trend_override:
                                        _news_trend_override = False
                                    (
                                        _news_trend_override_enabled,
                                        _news_trend_override_threshold_cfg,
                                    ) = _news_trend_prefilter_override_config(config)
                                    _dyn_prefilter_route = (
                                        "dynamic_momentum" if _is_dynamic_candidate else "trend_long"
                                    )
                                    _core_symbol_for_prefilter = sym_u in {
                                        str(s or "").strip().upper()
                                        for s in core_symbols
                                        if str(s or "").strip()
                                    }
                                    (
                                        _dyn_hc_prefilter_override,
                                        _dyn_hc_prefilter_reason,
                                        _dyn_hc_prefilter_score,
                                    ) = _dynamic_high_conviction_trend_prefilter_override_decision(
                                        config,
                                        route=_dyn_prefilter_route,
                                        is_dynamic_candidate=bool(_is_dynamic_candidate),
                                        is_core_symbol=bool(_core_symbol_for_prefilter),
                                        is_etf=sym_u in ETF_SYMBOLS,
                                        news_score=_news_trend_debug_news_score,
                                        event_score=_news_trend_debug_event_score,
                                        catalyst_score=_news_trend_debug_catalyst_score,
                                        catalyst_type=_artifact_catalyst_type,
                                        catalyst_age_minutes=_news_catalyst_age_minutes_dyn,
                                        relative_volume=_rel_vol_e,
                                        sentiment=sentiment_score,
                                        severe_bearish_lockout=bool(bearish_regime),
                                        cooldown_active=False,
                                    )
                                    _catalyst_trend_override = False
                                    _catalyst_trend_override_reason = "not_checked"
                                    _catalyst_trend_override_rank: int | None = None
                                    if (
                                        not trend_long_ok
                                        and not news_buy
                                        and _alt_match is None
                                        and not _dmo_pullback_bypass
                                        and not _news_early_prefilter
                                        and not _news_trend_override
                                        and not _dyn_hc_prefilter_override
                                    ):
                                        _artifact_rank_raw = None
                                        if isinstance(_premarket_artifacts, Mapping):
                                            _artifact_rank_row = _premarket_artifacts.get(sym_u)
                                            if isinstance(_artifact_rank_row, Mapping):
                                                _artifact_rank_raw = _artifact_rank_row.get(
                                                    "premarket_rank",
                                                    _artifact_rank_row.get("rank"),
                                                )
                                        _candidate_strong_for_trend = (
                                            float(_news_trend_debug_news_score or 0.0) >= 8.0
                                            or float(_news_trend_debug_catalyst_score or 0.0) >= 0.80
                                        )
                                        _prefilter_quote = None
                                        _prefilter_spread_pct = None
                                        _prefilter_spread_cap = None
                                        _prefilter_spread_ok = False
                                        _prefilter_price_above_vwap = False
                                        _prefilter_momentum_confirmed = False
                                        _prefilter_atr_ok = False
                                        _prefilter_ema20 = None
                                        _prefilter_bars_1m = None
                                        _prefilter_bars_5m = None
                                        if _candidate_strong_for_trend and _coerce_premarket_rank(_artifact_rank_raw) is not None:
                                            try:
                                                _prefilter_quote = broker.get_latest_quote(symbol)
                                            except Exception:
                                                _prefilter_quote = None
                                            _prefilter_skip_spread = _quote_skip_spread_check(_prefilter_quote)
                                            if (
                                                _prefilter_quote is not None
                                                and getattr(_prefilter_quote, "is_stale", None)
                                                and _prefilter_quote.is_stale(stale_quote_max_age)
                                            ):
                                                _prefilter_spread_pct = 0.15
                                            else:
                                                _prefilter_spread_pct = (
                                                    getattr(_prefilter_quote, "spread_pct", None)
                                                    if _prefilter_quote is not None
                                                    else 0.15
                                                )
                                            _prefilter_dynamic_cfg = (
                                                config.get("dynamic_universe") if isinstance(config, Mapping) else {}
                                            )
                                            _prefilter_dynamic_cfg = (
                                                _prefilter_dynamic_cfg
                                                if isinstance(_prefilter_dynamic_cfg, Mapping)
                                                else {}
                                            )
                                            if _is_dynamic_candidate:
                                                try:
                                                    _prefilter_spread_cap = float(
                                                        _prefilter_dynamic_cfg.get("execution_max_spread_pct", 8.0)
                                                    )
                                                except (TypeError, ValueError):
                                                    _prefilter_spread_cap = 8.0
                                                _prefilter_override_cap = dynamic_entry_spread_override_cap(
                                                    gain_pct=_gain_pct_e,
                                                    relative_volume=_rel_vol_e,
                                                )
                                                if _prefilter_override_cap is not None:
                                                    _prefilter_spread_cap = max(
                                                        float(_prefilter_spread_cap),
                                                        float(_prefilter_override_cap),
                                                    )
                                            else:
                                                _prefilter_spread_cap = engine.market_quality._max_spread_for_symbol(symbol)
                                            try:
                                                _prefilter_ignore_spread = engine.market_quality.should_ignore_spread_for_low_volume(
                                                    last_bar_volume_from_ohlcv(df)
                                                )
                                            except Exception:
                                                _prefilter_ignore_spread = False
                                            _prefilter_spread_relief = False
                                            try:
                                                _prefilter_spread_relief = (
                                                    bool(_lr_en)
                                                    and symbol_in_liquid_spread_relief_set(symbol, _lr_syms)
                                                    and (
                                                        _lr_hard is None
                                                        or float(_prefilter_spread_pct)
                                                        <= float(_lr_hard) + 1e-9
                                                    )
                                                )
                                            except Exception:
                                                _prefilter_spread_relief = False
                                            try:
                                                _prefilter_spread_ok = (
                                                    bool(_prefilter_skip_spread)
                                                    or bool(_prefilter_ignore_spread)
                                                    or bool(_prefilter_spread_relief)
                                                    or (
                                                        _prefilter_spread_pct is not None
                                                        and _prefilter_spread_cap is not None
                                                        and float(_prefilter_spread_pct)
                                                        <= float(_prefilter_spread_cap) + 1e-9
                                                    )
                                                )
                                            except (TypeError, ValueError):
                                                _prefilter_spread_ok = False
                                            _rs_atr_override = None
                                            if _entry_regime_score is not None:
                                                try:
                                                    _rs_atr_override = int(float(_entry_regime_score))
                                                except (TypeError, ValueError):
                                                    _rs_atr_override = None
                                            try:
                                                _max_atr_override = float(
                                                    engine.strategy.effective_max_atr_pct_for_entry(_rs_atr_override)
                                                )
                                                _prefilter_atr_ok = (
                                                    atr_pct is not None
                                                    and float(atr_pct) == float(atr_pct)
                                                    and float(atr_pct) <= _max_atr_override + 1e-9
                                                )
                                            except Exception:
                                                _prefilter_atr_ok = False
                                            try:
                                                if len(df) >= 20:
                                                    _prefilter_ema20 = float(
                                                        df["close"].ewm(span=20, adjust=False).mean().iloc[-1]
                                                    )
                                            except Exception:
                                                _prefilter_ema20 = None
                                            try:
                                                _prefilter_end = dt.astimezone(pytz.UTC)
                                                _prefilter_start_et = dt.astimezone(
                                                    pytz.timezone("America/New_York")
                                                ).replace(hour=9, minute=30, second=0, microsecond=0)
                                                _prefilter_start = _prefilter_start_et.astimezone(pytz.UTC)
                                                _prefilter_bars_1m = broker.get_bars(
                                                    symbol,
                                                    timeframe=TimeFrame.Minute,
                                                    start=_prefilter_start,
                                                    end=_prefilter_end,
                                                    limit=390,
                                                )
                                                _prefilter_bars_5m = broker.get_bars(
                                                    symbol,
                                                    timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                                                    start=_prefilter_start,
                                                    end=_prefilter_end,
                                                    limit=96,
                                                )
                                            except Exception:
                                                _prefilter_bars_1m = None
                                                _prefilter_bars_5m = None
                                            _prefilter_ref_px = float(close)
                                            if _prefilter_quote is not None and getattr(_prefilter_quote, "mid", None):
                                                try:
                                                    _prefilter_mid = float(_prefilter_quote.mid)
                                                    if _prefilter_mid > 0:
                                                        _prefilter_ref_px = _prefilter_mid
                                                except (TypeError, ValueError):
                                                    pass
                                            try:
                                                _prefilter_dyn_sig = compute_dynamic_entry_signals(
                                                    _prefilter_bars_1m,
                                                    _prefilter_ref_px,
                                                )
                                                _prefilter_price_above_vwap = bool(
                                                    _prefilter_dyn_sig.price_above_vwap
                                                )
                                            except Exception:
                                                _prefilter_dyn_sig = None
                                                _prefilter_price_above_vwap = False
                                            _prefilter_one_min_green = False
                                            try:
                                                if _prefilter_bars_1m is not None and len(_prefilter_bars_1m) >= 1:
                                                    _last_1m = _prefilter_bars_1m.iloc[-1]
                                                    _prefilter_one_min_green = float(_last_1m["close"]) > float(
                                                        _last_1m.get("open", _last_1m["close"])
                                                    )
                                            except Exception:
                                                _prefilter_one_min_green = False
                                            _prefilter_five_min_trend = False
                                            try:
                                                if _prefilter_bars_5m is not None and len(_prefilter_bars_5m) >= 2:
                                                    _prefilter_five_min_trend = float(
                                                        _prefilter_bars_5m["close"].iloc[-1]
                                                    ) > float(_prefilter_bars_5m["close"].iloc[0])
                                            except Exception:
                                                _prefilter_five_min_trend = False
                                            try:
                                                _prefilter_breakout = five_min_breakout_from_bars(
                                                    _prefilter_bars_5m,
                                                    _prefilter_ref_px,
                                                )
                                            except Exception:
                                                _prefilter_breakout = False
                                            _prefilter_momentum_confirmed = bool(
                                                _prefilter_price_above_vwap
                                                or _prefilter_one_min_green
                                                or _prefilter_five_min_trend
                                                or _prefilter_breakout
                                            )
                                            (
                                                _catalyst_trend_override,
                                                _catalyst_trend_override_reason,
                                                _catalyst_trend_override_rank,
                                            ) = _catalyst_trend_override_decision(
                                                news_score=_news_trend_debug_news_score,
                                                catalyst_score=_news_trend_debug_catalyst_score,
                                                premarket_rank=_artifact_rank_raw,
                                                momentum_confirmed=_prefilter_momentum_confirmed,
                                                spread_ok=_prefilter_spread_ok,
                                                atr_ok=_prefilter_atr_ok,
                                                price_above_vwap=_prefilter_price_above_vwap,
                                                day_gain_pct=_gain_pct_e,
                                            )
                                        if _catalyst_trend_override:
                                            log.info(
                                                "CATALYST_TREND_OVERRIDE symbol=%s close=%.4f ema20=%s news_score=%.2f catalyst_score=%.2f premarket_rank=%s",
                                                sym_u,
                                                float(close),
                                                "n/a"
                                                if _prefilter_ema20 is None
                                                else "%.4f" % float(_prefilter_ema20),
                                                float(_news_trend_debug_news_score),
                                                float(_news_trend_debug_catalyst_score),
                                                str(_catalyst_trend_override_rank or "n/a"),
                                            )
                                    if (
                                        sym_u in _scanner_selected_dynamic_set
                                        and not trend_long_ok
                                        and not news_buy
                                        and _alt_match is None
                                        and not _dmo_pullback_bypass
                                        and not _news_early_prefilter
                                        and not _news_trend_override
                                        and not _dyn_hc_prefilter_override
                                        and not _catalyst_trend_override
                                    ):
                                        log.info(
                                            "DYNAMIC_ENTRY_PREFILTER_BYPASS symbol=%s reason=scanner_selected",
                                            sym_u,
                                        )
                                        trend_long_ok = True
                                    if (
                                        not trend_long_ok
                                        and not news_buy
                                        and _alt_match is None
                                        and not _dmo_pullback_bypass
                                        and not _news_early_prefilter
                                        and not _news_trend_override
                                        and not _dyn_hc_prefilter_override
                                        and not _catalyst_trend_override
                                    ):
                                        if _is_dynamic_candidate:
                                            log.info(
                                                "DYNAMIC_HIGH_CONVICTION_TREND_PREFILTER_BLOCKED symbol=%s reason=%s score=%.2f catalyst_score=%.2f event_score=%.2f news_score=%.2f rvol=%.2f sentiment=%.2f age_minutes=%s",
                                                sym_u,
                                                _catalyst_trend_override_reason
                                                if _candidate_strong_for_trend
                                                else _dyn_hc_prefilter_reason,
                                                float(_dyn_hc_prefilter_score),
                                                float(_news_trend_debug_catalyst_score),
                                                float(_news_trend_debug_event_score),
                                                float(_news_trend_debug_news_score),
                                                float(_rel_vol_e or 0.0),
                                                float(sentiment_score or 0.0),
                                                "n/a"
                                                if _news_catalyst_age_minutes_dyn is None
                                                else "%.1f" % float(_news_catalyst_age_minutes_dyn),
                                            )
                                        _log_entry_skip(
                                            dt,
                                            symbol,
                                            "below MAs (trend prefilter); no news override or alternate entry (breakout / mean reversion / vol)",
                                            verbose=verbose,
                                            force=False,
                                        )
                                        _log_dynamic_selected_dropped(
                                            "trend_prefilter",
                                            stage="trend_prefilter",
                                            detail=(
                                                "trend_long_ok=false news_buy=%s alt_match=%s dyn_hc_override=%s"
                                                % (
                                                    str(bool(news_buy)).lower(),
                                                    str(_alt_match is not None).lower(),
                                                    str(bool(_dyn_hc_prefilter_override)).lower(),
                                                )
                                            ),
                                        )
                                        continue
                                    if not trend_long_ok and _catalyst_trend_override:
                                        trend_long_ok = True
                                    if (
                                        not trend_long_ok
                                        and _dyn_hc_prefilter_override
                                        and not _news_trend_override
                                    ):
                                        log.info(
                                            "DYNAMIC_HIGH_CONVICTION_TREND_PREFILTER_OVERRIDE symbol=%s reason=%s score=%.2f catalyst_score=%.2f event_score=%.2f news_score=%.2f rvol=%.2f sentiment=%.2f age_minutes=%s",
                                            sym_u,
                                            _dyn_hc_prefilter_reason,
                                            float(_dyn_hc_prefilter_score),
                                            float(_news_trend_debug_catalyst_score),
                                            float(_news_trend_debug_event_score),
                                            float(_news_trend_debug_news_score),
                                            float(_rel_vol_e or 0.0),
                                            float(sentiment_score or 0.0),
                                            "n/a"
                                            if _news_catalyst_age_minutes_dyn is None
                                            else "%.1f" % float(_news_catalyst_age_minutes_dyn),
                                        )

                                    _entry_eval_context = {
                                        "reference_price": None,
                                        "reference_price_source": "unavailable",
                                        "reference_price_available": False,
                                        "reference_price_unavailable_reason": "not_evaluated",
                                        "reference_price_attempted_sources": [],
                                        "reference_price_diagnostics": {},
                                    }
                                    _ref_px = None
                                    quote = broker.get_latest_quote(symbol)
                                    _paper_current_price = float(close)
                                    if quote is not None and getattr(quote, "mid", None):
                                        try:
                                            _quote_mid_for_options = float(quote.mid)
                                            if _quote_mid_for_options > 0:
                                                _paper_current_price = _quote_mid_for_options
                                        except (TypeError, ValueError):
                                            pass
                                    _entry_eval_context = _entry_evaluation_context(
                                        symbol=sym_u,
                                        route="dynamic_universe" if _is_dynamic_candidate else "trend_long",
                                        quote=quote,
                                        bars=df,
                                        current_price=close,
                                        now=dt,
                                        stale_quote_max_age=stale_quote_max_age,
                                    )
                                    _ref_px = _entry_eval_context.get("reference_price")
                                    log.info(
                                        "ENTRY_EVALUATION_CONTEXT symbol=%s route=%s reference_price=%s "
                                        "reference_price_source=%s reference_price_available=%s session_available=%s "
                                        "reference_price_reason=%s quote_age_seconds=%s bar_timestamp=%s attempted_sources=%s",
                                        sym_u,
                                        _entry_eval_context.get("route") or "n/a",
                                        "n/a" if _ref_px is None else "%.4f" % float(_ref_px),
                                        _entry_eval_context.get("reference_price_source", "unavailable"),
                                        str(bool(_entry_eval_context.get("reference_price_available"))).lower(),
                                        str(bool(_entry_eval_context.get("session_available"))).lower(),
                                        _entry_eval_context.get("reference_price_unavailable_reason") or "none",
                                        (_entry_eval_context.get("reference_price_diagnostics") or {}).get("quote_age_seconds"),
                                        (_entry_eval_context.get("reference_price_diagnostics") or {}).get("bar_timestamp"),
                                        ",".join(
                                            str(item.get("source"))
                                            for item in (_entry_eval_context.get("reference_price_attempted_sources") or [])
                                            if isinstance(item, Mapping)
                                        )
                                        or "none",
                                    )
                                    _paper_session_vwap: float | None = None
                                    _skip_nbbo_spread_tl = _quote_skip_spread_check(quote)
                                    if quote and getattr(quote, "is_stale", None) and quote.is_stale(stale_quote_max_age):
                                        spread_pct = 0.15
                                    else:
                                        spread_pct = quote.spread_pct if quote else 0.15
                                    dynamic_cfg = config.get("dynamic_universe") or {}
                                    if _is_dynamic_candidate:
                                        try:
                                            spread_cap = float(
                                                dynamic_cfg.get("execution_max_spread_pct", 8.0)
                                            )
                                        except (TypeError, ValueError):
                                            spread_cap = 8.0
                                        dynamic_guard_cfg = config.get("dynamic_entry_guard") or {}
                                        try:
                                            max_vwap_distance = float(
                                                dynamic_guard_cfg.get("max_vwap_distance_pct", 15.0)
                                            )
                                        except (TypeError, ValueError):
                                            max_vwap_distance = 15.0
                                        _ema_guard_raw = dynamic_guard_cfg.get(
                                            "require_ema_5_above_20"
                                        )
                                        require_dynamic_ema = (
                                            str(_ema_guard_raw).strip().lower()
                                            not in ("0", "false", "no", "off", "")
                                            if isinstance(_ema_guard_raw, str)
                                            else bool(_ema_guard_raw)
                                            if _ema_guard_raw is not None
                                            else True
                                        )
                                        _dmo_e = config.get("dynamic_momentum_override") or {}
                                        if (
                                            isinstance(_dmo_e, dict)
                                            and bool(_dmo_e.get("enabled"))
                                            and bool(_dmo_e.get("allow_without_ema_pullback"))
                                        ):
                                            require_dynamic_ema = False
                                        _dynamic_spread_override_cap = dynamic_entry_spread_override_cap(
                                            gain_pct=_gain_pct_e,
                                            relative_volume=_rel_vol_e,
                                        )
                                        if (
                                            _dynamic_spread_override_cap is not None
                                            and spread_pct is not None
                                            and not _skip_nbbo_spread_tl
                                            and spread_pct > spread_cap
                                            and float(spread_pct) <= float(_dynamic_spread_override_cap) + 1e-9
                                        ):
                                            log.info(
                                                "DYNAMIC_SPREAD_OVERRIDE symbol=%s spread=%.2f%% allowed_cap=%.2f%% reason=high_momentum",
                                                sym_u,
                                                float(spread_pct),
                                                float(_dynamic_spread_override_cap),
                                            )
                                            spread_cap = float(max(spread_cap, float(_dynamic_spread_override_cap)))
                                    else:
                                        _dynamic_spread_override_cap = None
                                        spread_cap = engine.market_quality._max_spread_for_symbol(symbol)
                                        max_vwap_distance = 2.0
                                        require_dynamic_ema = True
                                    _ignore_spread_lv_tl = engine.market_quality.should_ignore_spread_for_low_volume(
                                        last_bar_volume_from_ohlcv(df)
                                    )
                                    if (
                                        not _skip_nbbo_spread_tl
                                        and spread_pct is not None
                                        and spread_pct > spread_cap
                                        and not _ignore_spread_lv_tl
                                    ):
                                        if (
                                            _lr_en
                                            and symbol_in_liquid_spread_relief_set(
                                                symbol, _lr_syms
                                            )
                                            and (
                                                _lr_hard is None
                                                or float(spread_pct)
                                                <= float(_lr_hard) + 1e-9
                                            )
                                        ):
                                            pass
                                        else:
                                            _log_entry_skip(
                                                dt,
                                                symbol,
                                                "spread %.3f%% > cap %.3f%%"
                                                % (spread_pct, spread_cap),
                                                verbose=verbose,
                                                force=False,
                                            )
                                            _log_dynamic_selected_dropped(
                                                "spread_too_wide",
                                                stage="bad_quote_or_spread",
                                                detail="spread=%.3f cap=%.3f"
                                                % (float(spread_pct), float(spread_cap)),
                                            )
                                            continue
                                    if _news_trend_override:
                                        _nto_bars_1m = None
                                        _nto_bars_5m = None
                                        _nto_day_change = _gain_pct_e
                                        try:
                                            _ny_nto = pytz.timezone("America/New_York")
                                            _nto_end = dt.astimezone(pytz.UTC)
                                            _nto_start_et = dt.astimezone(_ny_nto).replace(
                                                hour=9,
                                                minute=30,
                                                second=0,
                                                microsecond=0,
                                            )
                                            _nto_start = _nto_start_et.astimezone(pytz.UTC)
                                            _nto_bars_1m = broker.get_bars(
                                                symbol,
                                                timeframe=TimeFrame.Minute,
                                                start=_nto_start,
                                                end=_nto_end,
                                                limit=390,
                                            )
                                            _nto_bars_5m = broker.get_bars(
                                                symbol,
                                                timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                                                start=_nto_start,
                                                end=_nto_end,
                                                limit=96,
                                            )
                                        except Exception:
                                            _nto_bars_1m = None
                                            _nto_bars_5m = None
                                        if _nto_day_change is None and hasattr(broker, "get_snapshot"):
                                            try:
                                                _nto_snap = broker.get_snapshot(symbol)
                                                if isinstance(_nto_snap, dict):
                                                    _nto_day_change = float(
                                                        _nto_snap.get("day_gain_pct")
                                                    )
                                            except Exception:
                                                _nto_day_change = None
                                        if _nto_day_change is None and df is not None and not getattr(df, "empty", True):
                                            try:
                                                if len(df) >= 2:
                                                    _prev_close_nto = float(df["close"].iloc[-2])
                                                    _last_close_nto = float(df["close"].iloc[-1])
                                                    if _prev_close_nto > 0:
                                                        _nto_day_change = (
                                                            _last_close_nto / _prev_close_nto - 1.0
                                                        ) * 100.0
                                            except Exception:
                                                _nto_day_change = None
                                        _nto_allowed, _nto_reason, _nto_day_change_eff, _nto_vwap = (
                                            _news_trend_override_price_confirmation(
                                                price=_paper_current_price,
                                                day_change_pct=_nto_day_change,
                                                bars_1m=_nto_bars_1m,
                                                bars_5m=_nto_bars_5m,
                                                config=config,
                                            )
                                        )
                                        if not _nto_allowed:
                                            if _nto_reason == "day_loss_too_large":
                                                log.info(
                                                    "NEWS_TREND_OVERRIDE_BLOCKED symbol=%s reason=day_loss_too_large day_change=%.1f",
                                                    sym_u,
                                                    float(_nto_day_change_eff or 0.0),
                                                )
                                            else:
                                                log.info(
                                                    "NEWS_TREND_OVERRIDE_BLOCKED symbol=%s reason=%s",
                                                    sym_u,
                                                    _nto_reason,
                                                )
                                            _news_trend_override = False
                                    _news_early_entry = False
                                    _news_early_entry_notional = 0.0
                                    _eff_for_cap: float | None = None
                                    if _is_dynamic_candidate:
                                        _utc_start = dt.astimezone(pytz.UTC)
                                        start_et = dt.astimezone(
                                            pytz.timezone("America/New_York")
                                        ).replace(hour=9, minute=30, second=0, microsecond=0)
                                        _bar_start = start_et.astimezone(pytz.UTC)
                                        df_1m = broker.get_bars(
                                            symbol,
                                            timeframe=TimeFrame.Minute,
                                            start=_bar_start,
                                            end=_utc_start,
                                            limit=390,
                                        )
                                        tf_5 = TimeFrame(5, TimeFrameUnit.Minute)
                                        df_5m = broker.get_bars(
                                            symbol,
                                            timeframe=tf_5,
                                            start=_bar_start,
                                            end=_utc_start,
                                            limit=96,
                                        )
                                        if _ref_px is None:
                                            _log_entry_skip(
                                                dt,
                                                symbol,
                                                "reference_price_unavailable",
                                                verbose=verbose,
                                                force=True,
                                            )
                                            _log_dynamic_selected_dropped(
                                                "reference_price_unavailable",
                                                stage="data_quality",
                                                detail=str(
                                                    _entry_eval_context.get(
                                                        "reference_price_unavailable_reason",
                                                        "no_valid_reference_price",
                                                    )
                                                ),
                                            )
                                            continue
                                        _dyn_sig = compute_dynamic_entry_signals(df_1m, _ref_px)
                                        _snap_e: dict[str, Any] = dict(_dyn_snap_prefetch)
                                        if not _snap_e and hasattr(broker, "get_snapshot"):
                                            try:
                                                _raw_snap = broker.get_snapshot(symbol)
                                                if isinstance(_raw_snap, dict):
                                                    _snap_e = _raw_snap
                                            except Exception:
                                                _snap_e = {}
                                        if _gain_pct_e is None:
                                            try:
                                                _gain_pct_e = float(_snap_e.get("day_gain_pct"))
                                                if not math.isfinite(_gain_pct_e):
                                                    _gain_pct_e = None
                                            except (TypeError, ValueError):
                                                _gain_pct_e = None
                                            if _gain_pct_e is None and df is not None and not getattr(
                                                df, "empty", True
                                            ):
                                                try:
                                                    if len(df) >= 2:
                                                        _pc = float(df["close"].iloc[-2])
                                                        _lc = float(df["close"].iloc[-1])
                                                        if _pc > 0:
                                                            _gain_pct_e = (_lc / _pc - 1.0) * 100.0
                                                except (TypeError, ValueError, KeyError):
                                                    pass
                                        if _rel_vol_e is None:
                                            try:
                                                _vol_sn = float(_snap_e.get("volume", 0) or 0)
                                            except (TypeError, ValueError):
                                                _vol_sn = 0.0
                                            _avg_v = 1.0
                                            if hasattr(broker, "get_avg_volume"):
                                                try:
                                                    _avg_v = float(broker.get_avg_volume(symbol))
                                                except Exception:
                                                    _avg_v = 1.0
                                            _avg_v = max(1.0, _avg_v)
                                            if _vol_sn > 0:
                                                _rel_vol_e = _vol_sn / _avg_v
                                            if _rel_vol_e is None and df is not None and not getattr(
                                                df, "empty", True
                                            ):
                                                try:
                                                    _dv = float(df["volume"].iloc[-1])
                                                    if _dv > 0 and _dv == _dv:
                                                        _rel_vol_e = _dv / _avg_v
                                                except (TypeError, ValueError, KeyError):
                                                    pass
                                        _vwap_guard_raw = dynamic_cfg.get("require_above_vwap", False)
                                        require_dynamic_vwap = (
                                            str(_vwap_guard_raw).strip().lower()
                                            not in ("0", "false", "no", "off", "")
                                            if isinstance(_vwap_guard_raw, str)
                                            else bool(_vwap_guard_raw)
                                        )
                                        _spread_for_dme = (
                                            float(spread_pct)
                                            if spread_pct is not None
                                            and spread_pct == spread_pct
                                            else None
                                        )
                                        _quote_unstable_dyn = False
                                        if quote is not None:
                                            if getattr(quote, "is_stale", None) and quote.is_stale(
                                                stale_quote_max_age
                                            ):
                                                _quote_unstable_dyn = True
                                            try:
                                                _bid_q = float(getattr(quote, "bid", 0) or 0)
                                                _ask_q = float(getattr(quote, "ask", 0) or 0)
                                                if _bid_q > 0 and _ask_q > 0 and _bid_q > _ask_q:
                                                    _quote_unstable_dyn = True
                                            except (TypeError, ValueError):
                                                pass
                                        if (
                                            _spread_for_dme is not None
                                            and float(_spread_for_dme) > 15.0
                                        ):
                                            _quote_unstable_dyn = True
                                        if _is_dynamic_candidate and _news_score_effective_dyn >= 3:
                                            _news_early_cfg = (
                                                dict(_dme_cfg) if isinstance(_dme_cfg, dict) else {}
                                            )
                                            if _dynamic_spread_override_cap is not None:
                                                _news_dyn_cfg = dict(
                                                    _news_early_cfg.get("news_dynamic_entry") or {}
                                                )
                                                _news_dyn_cfg["max_spread_pct"] = float(
                                                    max(
                                                        float(_news_dyn_cfg.get("max_spread_pct", 1.5) or 1.5),
                                                        float(_dynamic_spread_override_cap),
                                                    )
                                                )
                                                _news_early_cfg["news_dynamic_entry"] = _news_dyn_cfg
                                            _news_ok, _news_reason = news_early_entry_passes(
                                                news_score=int(math.ceil(_news_trend_debug_news_score)),
                                                relative_volume=_rel_vol_e,
                                                price_above_vwap=bool(_dyn_sig.price_above_vwap),
                                                spread_pct=_spread_for_dme,
                                                bars_1m=df_1m,
                                                cfg=_news_early_cfg,
                                            )
                                            if _news_ok:
                                                _news_early_entry = True
                                                _news_early_entry_notional = news_dynamic_starter_notional_usd(
                                                    _dme_cfg
                                                    if isinstance(_dme_cfg, dict)
                                                    else {},
                                                )
                                            else:
                                                log.info(
                                                    "NEWS_BLOCKED symbol=%s reason=%s",
                                                    sym_u,
                                                    _news_reason,
                                                )
                                        _ai_cat = score_ai_catalyst(sym_u, config, now=dt)
                                        _ai_summary = _ai_cat.summary.replace("\n", " ")[:160]
                                        _ai_catalyst_score = int(_ai_cat.score)
                                        _ai_catalyst_summary = _ai_summary
                                        log.info(
                                            "AI_CATALYST symbol=%s score=%d summary=%s",
                                            sym_u,
                                            _ai_catalyst_score,
                                            _ai_summary,
                                        )
                                        _high_momentum_bypass_e = high_momentum_bypass_ok(
                                            gain_pct=_gain_pct_e,
                                            relative_volume=_rel_vol_e,
                                            vwap_above=bool(_dyn_sig.price_above_vwap),
                                            spread_pct=_spread_for_dme,
                                            cfg=_dme_cfg
                                            if isinstance(_dme_cfg, dict)
                                            else {},
                                        )
                                        if _high_momentum_bypass_e:
                                            require_dynamic_ema = False
                                        _session_date_et = None
                                        try:
                                            _ny_ent = pytz.timezone("America/New_York")
                                            if getattr(dt, "tzinfo", None) is not None:
                                                _session_date_et = dt.astimezone(_ny_ent).date()
                                            else:
                                                _session_date_et = _ny_ent.localize(dt).date()
                                        except (TypeError, ValueError, Exception):
                                            _session_date_et = None
                                        _dyn_cd_active, _dyn_cd_rem = dynamic_reentry_cooldown_active(sym_u)
                                        if _dyn_cd_active:
                                            log.info(
                                                "DYNAMIC_REENTRY_BLOCK symbol=%s minutes_remaining=%d reason=post_exit_cooldown",
                                                sym_u,
                                                int(math.ceil(float(_dyn_cd_rem or 0.0))),
                                            )
                                            log.info(
                                                "DYNAMIC_REENTRY_BLOCK symbol=%s remaining_minutes=%d reason=post_exit_cooldown",
                                                sym_u,
                                                int(math.ceil(float(_dyn_cd_rem or 0.0))),
                                            )
                                            log.info(
                                                "DYNAMIC_REENTRY_COOLDOWN symbol=%s remaining_minutes=%d",
                                                sym_u,
                                                int(math.ceil(float(_dyn_cd_rem or 0.0))),
                                            )
                                            _log_entry_skip(
                                                dt,
                                                symbol,
                                                "dynamic re-entry cooldown",
                                                verbose=verbose,
                                                force=False,
                                            )
                                            _log_dynamic_selected_dropped(
                                                "dynamic_reentry_cooldown",
                                                stage="cooldown",
                                                detail="remaining_minutes=%d"
                                                % int(math.ceil(float(_dyn_cd_rem or 0.0))),
                                            )
                                            continue
                                        _dyn_session_vwap = session_vwap_from_bars(df_1m)
                                        _paper_current_price = float(_ref_px)
                                        _paper_session_vwap = (
                                            float(_dyn_session_vwap)
                                            if _dyn_session_vwap is not None
                                            else None
                                        )
                                        _dyn_vwap_ext = dynamic_entry_vwap_extension_pct(
                                            _ref_px,
                                            _dyn_session_vwap,
                                        )
                                        if _is_dynamic_candidate:
                                            log.info(
                                                "DYNAMIC_ENTRY_GUARD symbol=%s vwap=%s price=%.4f distance_from_vwap=%.3f news_score=%d",
                                                sym_u,
                                                "n/a"
                                                if _dyn_session_vwap is None
                                                else f"{float(_dyn_session_vwap):.4f}",
                                                float(_ref_px),
                                                float(_dyn_sig.distance_from_vwap_pct),
                                                int(math.ceil(_news_score_effective_dyn)),
                                            )
                                        _dyn_vwap_cap = float(
                                            dynamic_cfg.get("max_entry_vwap_extension_pct", 8.0)
                                        )
                                        try:
                                            _dyn_score_vwap = float(_dynamic_momentum_scores.get(sym_u, 0.0) or 0.0)
                                        except (TypeError, ValueError):
                                            _dyn_score_vwap = 0.0
                                        _high_conviction_vwap = _dynamic_high_conviction_bypass_active(
                                            is_dynamic_candidate=_is_dynamic_candidate,
                                            dynamic_score=_dyn_score_vwap,
                                            news_score=_news_score_effective_dyn,
                                        )
                                        if _high_conviction_vwap:
                                            log.info(
                                                "DYNAMIC_HIGH_CONVICTION_BLOCKED symbol=%s reason=vwap_extension_safety dynamic_score=%.2f news_score=%.2f",
                                                sym_u,
                                                float(_dyn_score_vwap),
                                                float(_news_score_effective_dyn or 0.0),
                                            )
                                        if _fastlane_allowed_sym:
                                            _dyn_vwap_cap = max(float(_dyn_vwap_cap), 25.0)
                                        if (
                                            _dyn_vwap_ext is not None
                                            and _dyn_vwap_ext > _dyn_vwap_cap + 1e-9
                                        ):
                                            _entry_decision_counts["blocked_vwap"] += 1
                                            log.info(
                                                "DYNAMIC_REJECT_EXTENDED symbol=%s price=%.2f vwap=%.2f extension_pct=%.2f max=%.2f",
                                                sym_u,
                                                float(_ref_px),
                                                float(_dyn_session_vwap or 0.0),
                                                float(_dyn_vwap_ext),
                                                _dyn_vwap_cap,
                                            )
                                            _log_entry_skip(
                                                dt,
                                                symbol,
                                                "dynamic vwap extension %.2f%% > %.2f%%"
                                                % (float(_dyn_vwap_ext), _dyn_vwap_cap),
                                                verbose=verbose,
                                                force=False,
                                            )
                                            _log_dynamic_selected_dropped(
                                                "dynamic_vwap_extension",
                                                stage="missing_ohlcv_or_entry_guard",
                                                detail="extension=%.2f cap=%.2f"
                                                % (float(_dyn_vwap_ext), float(_dyn_vwap_cap)),
                                            )
                                            continue
                                        if _fastlane_allowed_sym and _dyn_vwap_ext is not None:
                                            log.info(
                                                "DYNAMIC_FASTLANE_BYPASS symbol=%s news_score=%s catalyst_age_minutes=%s reason=vwap_extension",
                                                sym_u,
                                                str(_fastlane_meta_sym.get("news_score", "n/a")),
                                                str(_fastlane_meta_sym.get("age_minutes", "n/a")),
                                            )
                                        _entry_effective_min_rel_volume = None
                                        _entry_catalyst_fastlane_active = False
                                        _entry_catalyst_min_relative_volume = None
                                        if _dme_on and not _news_early_entry:
                                            _dme_eff_cfg = (
                                                dict(_dme_cfg) if isinstance(_dme_cfg, dict) else {}
                                            )
                                            if _dynamic_spread_override_cap is not None:
                                                _dme_eff_cfg["max_entry_spread_pct"] = float(
                                                    max(
                                                        float(_dme_eff_cfg.get("max_entry_spread_pct", 3.0) or 3.0),
                                                        float(_dynamic_spread_override_cap),
                                                    )
                                                )
                                            if _is_dynamic_candidate and _news_score_effective_dyn >= 7:
                                                _dme_eff_cfg["max_entry_spread_pct"] = float(
                                                    max(
                                                        float(_dme_eff_cfg.get("max_entry_spread_pct", 3.0) or 3.0),
                                                        4.0,
                                                    )
                                                )
                                            _entry_artifact_row_for_fastlane = (
                                                _premarket_artifacts.get(sym_u)
                                                if isinstance(_premarket_artifacts, Mapping)
                                                else None
                                            )
                                            _entry_premarket_metadata_confirmed = (
                                                _premarket_artifact_has_confirmed_metadata(_entry_artifact_row_for_fastlane)
                                            )
                                            _premarket_catalyst_fastlane_entry = _premarket_catalyst_fastlane_signal(
                                                premarket_injected=(
                                                    sym_u in _premarket_injected_symbol_set
                                                    and _entry_premarket_metadata_confirmed
                                                ),
                                                news_score=_news_trend_debug_news_score,
                                                event_score=_news_trend_debug_event_score,
                                                catalyst_score=_news_trend_debug_catalyst_score,
                                                catalyst_age_minutes=_news_catalyst_age_minutes_dyn,
                                            )
                                            _entry_fastlane_threshold = 0.35
                                            try:
                                                _entry_fastlane_threshold = float(
                                                    _dme_eff_cfg.get("catalyst_min_relative_volume", 0.35) or 0.35
                                                )
                                            except (TypeError, ValueError):
                                                _entry_fastlane_threshold = 0.35
                                            _entry_fastlane_trace = _catalyst_fastlane_entry_trace_fields(
                                                premarket_injected=(
                                                    sym_u in _premarket_injected_symbol_set
                                                    and _entry_premarket_metadata_confirmed
                                                ),
                                                news_score=_news_trend_debug_news_score,
                                                event_score=_news_trend_debug_event_score,
                                                catalyst_score=_news_trend_debug_catalyst_score,
                                                catalyst_age_minutes=_news_catalyst_age_minutes_dyn,
                                                relative_volume=_rel_vol_e,
                                                threshold=_entry_fastlane_threshold,
                                            )
                                            if _is_dynamic_candidate:
                                                _log_catalyst_fastlane_entry_trace(sym_u, _entry_fastlane_trace)
                                            if _premarket_catalyst_fastlane_entry and bool(_entry_fastlane_trace.get("eligible")):
                                                _dme_eff_cfg["catalyst_fastlane_active"] = True
                                                _dme_eff_cfg["catalyst_min_relative_volume"] = float(
                                                    _entry_fastlane_trace.get("threshold", 0.35) or 0.35
                                                )
                                            _entry_premarket_rank = None
                                            if isinstance(_premarket_artifacts, Mapping):
                                                _entry_artifact_row = _premarket_artifacts.get(sym_u)
                                                if isinstance(_entry_artifact_row, Mapping):
                                                    _entry_premarket_rank = _entry_artifact_row.get(
                                                        "premarket_rank",
                                                        _entry_artifact_row.get("rank"),
                                                    )
                                            _ok_m, _rsn_m = dynamic_momentum_entry_passes(
                                                gain_pct=_gain_pct_e,
                                                relative_volume=_rel_vol_e,
                                                vwap_above=bool(_dyn_sig.price_above_vwap),
                                                spread_pct=_spread_for_dme,
                                                bars_1m=df_1m,
                                                bars_5m=df_5m,
                                                ref_price=_ref_px,
                                                cfg=_dme_eff_cfg,
                                                session_date=_session_date_et,
                                                ai_catalyst_score=float(_ai_cat.score),
                                                symbol=sym_u,
                                                news_score=int(math.ceil(_news_score_effective_dyn)),
                                                event_score=float(_news_trend_debug_event_score or 0.0),
                                                catalyst_score=float(_news_trend_debug_catalyst_score or 0.0),
                                                catalyst_age_minutes=_news_catalyst_age_minutes_dyn,
                                                premarket_rank=_entry_premarket_rank,
                                                current_time=dt,
                                                is_dynamic=_is_dynamic_candidate,
                                                quote_unstable=_quote_unstable_dyn,
                                            )
                                            try:
                                                _entry_base_min_rel_volume = float(
                                                    _dme_eff_cfg.get(
                                                        "min_relative_volume",
                                                        _dme_eff_cfg.get("min_rel_volume", 0.0),
                                                    )
                                                    or 0.0
                                                )
                                            except (TypeError, ValueError):
                                                _entry_base_min_rel_volume = 0.0
                                            _entry_effective_min_rel_volume = (
                                                _entry_base_min_rel_volume
                                                if _entry_base_min_rel_volume > 0.0
                                                else None
                                            )
                                            _entry_catalyst_fastlane_active = bool(
                                                _dme_eff_cfg.get("catalyst_fastlane_active", False)
                                            )
                                            try:
                                                _entry_catalyst_min_relative_volume = float(
                                                    _dme_eff_cfg.get("catalyst_min_relative_volume", 0.0)
                                                    or 0.0
                                                )
                                            except (TypeError, ValueError):
                                                _entry_catalyst_min_relative_volume = None
                                            if _entry_catalyst_fastlane_active and _entry_catalyst_min_relative_volume:
                                                _entry_effective_min_rel_volume = min(
                                                    float(_entry_effective_min_rel_volume or _entry_catalyst_min_relative_volume),
                                                    float(_entry_catalyst_min_relative_volume),
                                                )
                                            if (
                                                _is_dynamic_candidate
                                                and _entry_effective_min_rel_volume is not None
                                                and not _entry_catalyst_fastlane_active
                                            ):
                                                (
                                                    _entry_effective_min_rel_volume,
                                                    _entry_adaptive_rvol_used,
                                                    _entry_adaptive_rvol_reason,
                                                ) = dynamic_adaptive_volume_min_relative_volume(
                                                    base_min_relative_volume=float(_entry_effective_min_rel_volume),
                                                    cfg=_dme_eff_cfg,
                                                    current_time=dt,
                                                    is_dynamic=True,
                                                    gain_pct=_gain_pct_e,
                                                    alignment_score=None,
                                                    momentum_confirmed=True,
                                                )
                                                if _entry_adaptive_rvol_used:
                                                    log.info(
                                                        "ENTRY_EVAL_DYNAMIC_RVOL_ADAPTIVE symbol=%s route=%s "
                                                        "effective_min=%.3f reason=%s",
                                                        sym_u,
                                                        str(_route_log or _entry_gates_route or "n/a"),
                                                        float(_entry_effective_min_rel_volume),
                                                        _entry_adaptive_rvol_reason,
                                                    )
                                            if (
                                                _ok_m
                                                and _is_dynamic_candidate
                                                and _entry_effective_min_rel_volume is not None
                                                and not _entry_catalyst_fastlane_active
                                                and _rel_vol_e < float(_entry_effective_min_rel_volume) - 1e-9
                                            ):
                                                _entry_rel_for_log = _finite_float_or_none(_rel_vol_e)
                                                if _entry_rel_for_log is None:
                                                    _entry_rel_for_log = 0.0
                                                _ok_m = False
                                                _rsn_m = (
                                                    "relative_volume %.2f < %.2f"
                                                    % (
                                                        _entry_rel_for_log,
                                                        float(_entry_effective_min_rel_volume),
                                                    )
                                                )
                                                log.info(
                                                    "ENTRY_EVAL_DYNAMIC_RVOL_GUARD symbol=%s route=%s "
                                                    "rel_volume=%.3f threshold_used=%.3f fastlane_active=false "
                                                    "allowed=false reason=%s",
                                                    sym_u,
                                                    str(_route_log or _entry_gates_route or "n/a"),
                                                    _entry_rel_for_log,
                                                    float(_entry_effective_min_rel_volume),
                                                    _rsn_m,
                                                )
                                            elif _ok_m and _is_dynamic_candidate and _entry_effective_min_rel_volume is not None:
                                                _entry_rel_for_log = _finite_float_or_none(_rel_vol_e)
                                                if _entry_rel_for_log is None:
                                                    _entry_rel_for_log = 0.0
                                                log.info(
                                                    "ENTRY_EVAL_DYNAMIC_RVOL_GUARD symbol=%s route=%s "
                                                    "rel_volume=%.3f threshold_used=%.3f fastlane_active=%s "
                                                    "allowed=true reason=relative_volume %.2f >= %.2f",
                                                    sym_u,
                                                    str(_route_log or _entry_gates_route or "n/a"),
                                                    _entry_rel_for_log,
                                                    float(_entry_effective_min_rel_volume),
                                                    str(bool(_entry_catalyst_fastlane_active)).lower(),
                                                    _entry_rel_for_log,
                                                    float(_entry_effective_min_rel_volume),
                                                )
                                            if (
                                                not _ok_m
                                                and _is_dynamic_candidate
                                            ):
                                                _scanner_override_ok, _scanner_override_reason = (
                                                    _dynamic_entry_scanner_approval_override_decision(
                                                        config,
                                                        _scanner_meta_entry,
                                                        _rsn_m,
                                                    )
                                                )
                                                if _scanner_override_ok:
                                                    log.info(
                                                        "DYNAMIC_ENTRY_SCANNER_APPROVAL_OVERRIDE symbol=%s reason=entry_alignment prior_reason=%s",
                                                        sym_u,
                                                        str(_rsn_m or ""),
                                                    )
                                                    _ok_m = True
                                                    _rsn_m = "ok scanner_selected_dynamic_momentum"
                                                elif _scanner_meta_entry:
                                                    log.info(
                                                        "DYNAMIC_ENTRY_SCANNER_APPROVAL_OVERRIDE_BLOCKED symbol=%s reason=%s prior_reason=%s",
                                                        sym_u,
                                                        _scanner_override_reason,
                                                        str(_rsn_m or ""),
                                                    )
                                            if not _ok_m:
                                                if _is_dynamic_candidate:
                                                    _dme_rvol_failure = (
                                                        str(_rsn_m or "").startswith("relative_volume ")
                                                        and "<" in str(_rsn_m or "")
                                                    )
                                                    if _dme_rvol_failure:
                                                        _artifact_backed = (
                                                            isinstance(_premarket_artifacts, Mapping)
                                                            and sym_u in _premarket_artifacts
                                                            and _premarket_artifact_has_confirmed_metadata(
                                                                _premarket_artifacts.get(sym_u)
                                                            )
                                                        )
                                                        _pre_allocator_route = (
                                                            "premarket_catalyst_replay"
                                                            if _artifact_backed
                                                            else "dynamic_universe"
                                                        )
                                                        _required_rvol = _dynamic_rvol_required_from_reason(
                                                            str(_rsn_m),
                                                            _dme_eff_cfg.get(
                                                                "min_relative_volume",
                                                                _dme_eff_cfg.get("min_rel_volume", 0.0),
                                                            ),
                                                        )
                                                        _rvol_bypass = (
                                                            _artifact_backed
                                                            and _premarket_catalyst_rvol_bypass_allowed(
                                                                route=_pre_allocator_route,
                                                                catalyst_score=_news_trend_debug_catalyst_score,
                                                                event_score=_news_trend_debug_event_score,
                                                                news_score=_news_trend_debug_news_score,
                                                            )
                                                        )
                                                        log.info(
                                                            "PRE_ALLOCATOR_DYNAMIC_RVOL_CHECK symbol=%s route=%s "
                                                            "relative_volume=%.3f required_relative_volume=%.3f "
                                                            "catalyst_score=%.2f event_score=%.2f news_score=%.2f "
                                                            "bypass=%s",
                                                            sym_u,
                                                            _pre_allocator_route,
                                                            float(_rel_vol_e or 0.0),
                                                            float(_required_rvol),
                                                            float(_news_trend_debug_catalyst_score or 0.0),
                                                            float(_news_trend_debug_event_score or 0.0),
                                                            float(_news_trend_debug_news_score or 0.0),
                                                            str(bool(_rvol_bypass)).lower(),
                                                        )
                                                        if _rvol_bypass:
                                                            _ok_m = True
                                                            _rsn_m = "ok news_catalyst"
                                                    if _rsn_m == "price not above session VWAP":
                                                        _vwap_guard_reason = (
                                                            "price above session VWAP"
                                                            if bool(_dyn_sig.price_above_vwap)
                                                            else "price not above session VWAP"
                                                        )
                                                        _vwap_guard_allowed = bool(
                                                            _dyn_sig.price_above_vwap
                                                        )
                                                        if _high_conviction_vwap and not bool(_dyn_sig.price_above_vwap):
                                                            log.info(
                                                                "DYNAMIC_HIGH_CONVICTION_BLOCKED symbol=%s reason=vwap_safety dynamic_score=%.2f news_score=%.2f",
                                                                sym_u,
                                                                float(_dyn_score_vwap),
                                                                float(_news_score_effective_dyn or 0.0),
                                                            )
                                                        _vwap_guard_distance = None
                                                        if _dyn_session_vwap is not None and float(_dyn_session_vwap) > 0:
                                                            try:
                                                                _vwap_guard_distance = (
                                                                    (float(_ref_px) - float(_dyn_session_vwap))
                                                                    / float(_dyn_session_vwap)
                                                                ) * 100.0
                                                            except Exception:
                                                                _vwap_guard_distance = None
                                                        _vwap_guard_line = (
                                                            "DYNAMIC_VWAP_GUARD symbol=%s price=%.4f vwap=%s distance_pct=%s news_score=%d allowed=%s reason=%s"
                                                            % (
                                                                sym_u,
                                                                float(_ref_px),
                                                                "n/a"
                                                                if _dyn_session_vwap is None
                                                                else f"{float(_dyn_session_vwap):.4f}",
                                                                "n/a"
                                                                if _vwap_guard_distance is None
                                                                else f"{float(_vwap_guard_distance):.3f}",
                                                                int(math.ceil(_news_score_effective_dyn)),
                                                                str(bool(_vwap_guard_allowed)).lower(),
                                                                _vwap_guard_reason,
                                                            )
                                                        )
                                                        log.info(_vwap_guard_line)
                                                        print(_vwap_guard_line, flush=True)
                                                        if _vwap_guard_allowed:
                                                            _ok_m = True
                                                            _rsn_m = "ok news_catalyst"
                                                        else:
                                                            _log_allocator_dynamic_skipped(
                                                                sym_u,
                                                                reason=f"dynamic momentum entry: {_rsn_m}",
                                                                spread=float(_spread_for_dme)
                                                                if _spread_for_dme is not None
                                                                else None,
                                                                spread_cap=float(
                                                                    _dme_eff_cfg.get("max_entry_spread_pct", 3.0)
                                                                    or 3.0
                                                                ),
                                                                vwap_above=bool(_dyn_sig.price_above_vwap),
                                                                strength_eff=float(_eff_for_cap)
                                                                if _eff_for_cap is not None
                                                                else None,
                                                                source="dynamic_universe",
                                                                news_score=float(_news_score_effective_dyn),
                                                                relative_volume=_finite_float_or_none(_rel_vol_e),
                                                            )
                                                            _log_entry_skip(
                                                                dt,
                                                                symbol,
                                                                "dynamic momentum entry: %s" % _rsn_m,
                                                                verbose=verbose,
                                                                force=False,
                                                            )
                                                            _log_dynamic_selected_dropped(
                                                                "dynamic_momentum_entry",
                                                                stage="missing_ohlcv_or_entry_guard",
                                                                detail=str(_rsn_m),
                                                            )
                                                            continue
                                                    if _rsn_m != "ok news_catalyst":
                                                        _log_allocator_dynamic_skipped(
                                                            sym_u,
                                                            reason=f"dynamic momentum entry: {_rsn_m}",
                                                            spread=float(_spread_for_dme)
                                                            if _spread_for_dme is not None
                                                            else None,
                                                            spread_cap=float(
                                                                _dme_eff_cfg.get("max_entry_spread_pct", 3.0)
                                                                or 3.0
                                                            ),
                                                            vwap_above=bool(_dyn_sig.price_above_vwap),
                                                            strength_eff=float(_eff_for_cap)
                                                            if _eff_for_cap is not None
                                                            else None,
                                                            source="dynamic_universe",
                                                            news_score=float(_news_score_effective_dyn),
                                                            relative_volume=_finite_float_or_none(_rel_vol_e),
                                                        )
                                                        _log_entry_skip(
                                                            dt,
                                                            symbol,
                                                            "dynamic momentum entry: %s" % _rsn_m,
                                                            verbose=verbose,
                                                            force=False,
                                                        )
                                                        _log_dynamic_selected_dropped(
                                                            "dynamic_momentum_entry",
                                                            stage="missing_ohlcv_or_entry_guard",
                                                            detail=str(_rsn_m),
                                                        )
                                                        continue
                                            if _rsn_m == "ok news_catalyst":
                                                _news_early_entry = True
                                                _news_early_entry_notional = news_dynamic_starter_notional_usd(
                                                    _dme_cfg
                                                    if isinstance(_dme_cfg, dict)
                                                    else {},
                                                )
                                        else:
                                            if not dynamic_entry_guard_passes(
                                                _dyn_sig,
                                                max_distance_from_vwap_pct=max_vwap_distance,
                                                require_above_vwap=require_dynamic_vwap,
                                                require_ema_5_above_20=require_dynamic_ema,
                                            ):
                                                _dyn_guard_reason = dynamic_entry_guard_failure_reason(
                                                    _dyn_sig,
                                                    max_distance_from_vwap_pct=max_vwap_distance,
                                                    require_above_vwap=require_dynamic_vwap,
                                                    require_ema_5_above_20=require_dynamic_ema,
                                                )

                                                if _dyn_guard_reason:
                                                    _log_entry_skip(
                                                        dt,
                                                        symbol,
                                                        "dynamic entry guard: %s"
                                                        % _dyn_guard_reason,
                                                        verbose=verbose,
                                                        force=False,
                                                    )
                                                    _log_dynamic_selected_dropped(
                                                        "dynamic_entry_guard",
                                                        stage="missing_ohlcv_or_entry_guard",
                                                        detail=str(_dyn_guard_reason),
                                                    )
                                                    continue
                                    if _dme_on and isinstance(_dme_cfg, dict) and bool(
                                        _dme_cfg.get("apply_legacy_dynamic_guard", False)
                                    ):
                                        if not _high_momentum_bypass_e and not dynamic_entry_guard_passes(
                                            _dyn_sig,
                                                max_distance_from_vwap_pct=max_vwap_distance,
                                                require_above_vwap=require_dynamic_vwap,
                                                require_ema_5_above_20=require_dynamic_ema,
                                            ):
                                                _dyn_guard_reason = dynamic_entry_guard_failure_reason(
                                                    _dyn_sig,
                                                    max_distance_from_vwap_pct=max_vwap_distance,
                                                    require_above_vwap=require_dynamic_vwap,
                                                    require_ema_5_above_20=require_dynamic_ema,
                                                )
                                                if _dyn_guard_reason:
                                                    _log_entry_skip(
                                                        dt,
                                                        symbol,
                                                        "dynamic entry guard: %s"
                                                        % _dyn_guard_reason,
                                                        verbose=verbose,
                                                        force=False,
                                                    )
                                                    _log_dynamic_selected_dropped(
                                                        "dynamic_entry_guard",
                                                        stage="missing_ohlcv_or_entry_guard",
                                                        detail=str(_dyn_guard_reason),
                                                    )
                                                    continue
                                    if _shadow_live_options_active(config):
                                        try:
                                            _shadow_option_result = _attempt_shadow_option_entry(
                                                config,
                                                broker=broker,
                                                execution_manager=engine.execution,
                                                symbol=sym_u,
                                                dt=dt,
                                                current_price=float(_ref_px),
                                                session_vwap=float(_dyn_session_vwap)
                                                if _dyn_session_vwap is not None
                                                else None,
                                                account_equity=float(account_equity),
                                                positions=positions,
                                                source="dynamic_universe",
                                                conviction_score=float(_eff_for_cap)
                                                if _eff_for_cap is not None
                                                else None,
                                                news_score=float(_news_score_effective_dyn)
                                                if _news_score_effective_dyn is not None
                                                else None,
                                                event_score=float(_event_score_dyn)
                                                if _event_score_dyn is not None
                                                else None,
                                                relative_volume=_finite_float_or_none(_rel_vol_e),
                                                tracked=tracked if isinstance(tracked, dict) else None,
                                                user_id=_uid,
                                                data_dir=_data_dir,
                                            )
                                        except Exception:
                                            log.debug(
                                                "[%s] shadow option entry helper failed for %s",
                                                _uid,
                                                sym_u,
                                                exc_info=True,
                                            )
                                            _shadow_option_result = None
                                        if _shadow_option_result is not None:
                                            if _shadow_option_result.intended:
                                                log.info(
                                                    "SHADOW_LIVE_OPTIONS_INTENDED symbol=%s right=%s filled=%s reason_codes=%s",
                                                    sym_u,
                                                    _shadow_option_result.right or "n/a",
                                                    str(_shadow_option_result.filled).lower(),
                                                    ",".join(_shadow_option_result.reason_codes),
                                                )
                                            else:
                                                log.info(
                                                    "SHADOW_LIVE_OPTIONS_SKIPPED symbol=%s reason=%s reason_codes=%s",
                                                    sym_u,
                                                    _shadow_option_result.reason or "shadow-live route not placed",
                                                    ",".join(_shadow_option_result.reason_codes),
                                                )
                                    est_buying_power_required = engine.execution.min_buying_power_for_equity_entry_probe(
                                        close
                                    )
                                    _eff_rfc_incoming: float | None = None
                                    if df is not None and not getattr(df, "empty", True):
                                        _rs_atr_e0 = None
                                        if _entry_regime_score is not None:
                                            try:
                                                _rs_atr_e0 = int(float(_entry_regime_score))
                                            except (TypeError, ValueError):
                                                _rs_atr_e0 = None
                                        _max_atr_e0 = float(
                                            engine.strategy.effective_max_atr_pct_for_entry(_rs_atr_e0)
                                        )
                                        _cr_e0, _, _den_e0 = trend_long_composite_rank(
                                            df,
                                            atr_pct=float(atr_pct)
                                            if atr_pct is not None and atr_pct == atr_pct
                                            else None,
                                            max_atr_pct=_max_atr_e0,
                                            event_triggers=_event_trig_cfg,
                                            atr_period=_atr_period_tl,
                                            composite_weights=_comp_w,
                                        )
                                        _den_e0f = float(_den_e0) if _den_e0 else 4.0
                                        _eff_rfc_incoming = effective_signal_strength(
                                            float(_cr_e0) / _den_e0f, _strength_jitter_max
                                        )
                                    while est_buying_power_required > available_cash:
                                        if not _try_rebalance_free_capital_trim(
                                            sym_u,
                                            incoming_strength=_eff_rfc_incoming,
                                            strength_cohort=None,
                                        ):
                                            break
                                    if (
                                        est_buying_power_required > available_cash
                                        and bool(_rfc_cfg.get("rotate_full_weakest_when_stronger"))
                                    ):
                                        if _eff_rfc_incoming is not None:
                                            _eff_one_sh = _eff_rfc_incoming
                                        else:
                                            _rs_atr_one = None
                                            if _entry_regime_score is not None:
                                                try:
                                                    _rs_atr_one = int(
                                                        float(_entry_regime_score)
                                                    )
                                                except (TypeError, ValueError):
                                                    _rs_atr_one = None
                                            _max_atr_one_sh = float(
                                                engine.strategy.effective_max_atr_pct_for_entry(
                                                    _rs_atr_one
                                                )
                                            )
                                            _cr_one, _, _den_one = trend_long_composite_rank(
                                                df,
                                                atr_pct=float(atr_pct)
                                                if atr_pct is not None
                                                and atr_pct == atr_pct
                                                else None,
                                                max_atr_pct=_max_atr_one_sh,
                                                event_triggers=_event_trig_cfg,
                                                atr_period=_atr_period_tl,
                                                composite_weights=_comp_w,
                                            )
                                            _d1f = float(_den_one) if _den_one else 4.0
                                            _eff_one_sh = effective_signal_strength(
                                                float(_cr_one) / _d1f, _strength_jitter_max
                                            )
                                        if _try_rotate_full_weakest_for_bp(
                                            sym_u, _eff_one_sh, strength_cohort=None
                                        ):
                                            pass
                                    if est_buying_power_required > available_cash:
                                        _log_entry_skip(
                                            dt,
                                            symbol,
                                            "insufficient buying power (min ~$%.2f > $%.2f available)"
                                            % (est_buying_power_required, available_cash),
                                            verbose=verbose,
                                            force=False,
                                        )
                                        continue
                                    min_vol_atr = engine.market_quality.min_volume_atr_ratio
                                    vol_atr_g = max(min_vol_atr, float(vol_ratio)) if vol_ratio is not None else 1.5
                                    if df is not None and not getattr(df, "empty", True):
                                        _rs_cap = None
                                        if _entry_regime_score is not None:
                                            try:
                                                _rs_cap = int(float(_entry_regime_score))
                                            except (TypeError, ValueError):
                                                _rs_cap = None
                                        _max_atr_cap = float(
                                            engine.strategy.effective_max_atr_pct_for_entry(_rs_cap)
                                        )
                                        _cr_cap, _, _den_cap = trend_long_composite_rank(
                                            df,
                                            atr_pct=float(atr_pct)
                                            if atr_pct is not None and atr_pct == atr_pct
                                            else None,
                                            max_atr_pct=_max_atr_cap,
                                            event_triggers=_event_trig_cfg,
                                            atr_period=_atr_period_tl,
                                            composite_weights=_comp_w,
                                        )
                                        _den_f = float(_den_cap) if _den_cap else 4.0
                                        _eff_for_cap = effective_signal_strength(
                                            float(_cr_cap) / _den_f, _strength_jitter_max
                                        )
                                    try:
                                        _n_boost = max(0, int(_entry_wave_strong_ct))
                                    except (TypeError, ValueError):
                                        _n_boost = 0
                                    if _eff_for_cap is not None:
                                        try:
                                            if float(_eff_for_cap) + 1e-12 >= float(
                                                _smin_symbol
                                            ):
                                                _n_boost = max(0, int(_entry_wave_strong_ct)) + 1
                                        except (TypeError, ValueError):
                                            pass
                                    if trend_long_blocked_by_portfolio_cap(
                                        max_positions=_max_port_positions,
                                        enable_replacement=_port_replace,
                                        allow_add=_effective_allow_add,
                                        num_eligible_long_stocks=len(_eligible_active),
                                        symbol_upper=sym_u,
                                        eligible_long_symbols_upper=_eligible_syms_upper,
                                        incoming_signal_strength=_eff_for_cap,
                                        cap_relief=_cap_relief_cfg,
                                        top_n_batch_mode=bool(
                                            _alloc_cfg.get("rank_by_signal_strength")
                                        ),
                                    ):
                                        continue
                                    if new_symbol_blocked_at_position_cap_only_replacement(
                                        max_positions=_max_port_positions,
                                        enable_replacement=_port_replace,
                                        current_positions=current_positions,
                                        symbol_upper=sym_u,
                                        incoming_signal_strength=_eff_for_cap,
                                        cap_relief=_cap_relief_cfg,
                                        defer_to_ranked_batch=bool(
                                            _alloc_cfg.get("rank_by_signal_strength")
                                        ),
                                    ):
                                        _log_entry_skip(
                                            dt,
                                            symbol,
                                            "at max positions (%d); new symbols require portfolio.enable_replacement"
                                            % (len(current_positions),),
                                            verbose=verbose,
                                            force=True,
                                        )
                                        continue

                                    _pi_est_clip = max(
                                        float(
                                            (config.get("entries") or {}).get("min_trade_size")
                                            or 1000
                                        ),
                                        float(account_equity) * 0.01,
                                    )
                                    _pi_est_clip = entry_target_dollars_for_symbol(
                                        float(_pi_est_clip),
                                        symbol=sym_u,
                                        core_symbols=core_symbols,
                                        account_equity=float(account_equity),
                                        config=config,
                                    )
                                    _pi_block, _pi_reason = portfolio_intelligence_blocks_entry(
                                        sym_u,
                                        positions=current_positions,
                                        account_equity=float(account_equity),
                                        proposed_notional=float(_pi_est_clip),
                                        config=config,
                                    )
                                    if _pi_block:
                                        _log_entry_skip(
                                            dt,
                                            symbol,
                                            _pi_reason or "portfolio_intelligence",
                                            verbose=verbose,
                                            force=False,
                                        )
                                        continue

                                    decision = None
                                    _cap_rel = cap_relax_factor_effective(
                                        config=config,
                                        cap_block_streak=_ad_cap_streak,
                                        entry_strength=_eff_for_cap,
                                        symbol_upper=sym_u,
                                    )
                                    if (
                                        _pyramid_relax_symbol_cap
                                        and bool(_pyramid_winners_cfg.get("enabled"))
                                    ):
                                        try:
                                            _prm = float(
                                                _pyramid_winners_cfg.get(
                                                    "cap_relax_multiplier", 1.15
                                                )
                                            )
                                        except (TypeError, ValueError):
                                            _prm = 1.15
                                        _prm = max(1.0, min(_prm, 3.0))
                                        _cap_rel = max(float(_cap_rel), _prm)
                                    theme_name = THEME_MAP.get(sym_u, sym_u)
                                    _entry_gates_route = "none"
                                    _entry_regime_condition = (
                                        regime_result.condition if regime_result is not None else None
                                    )
                                    log.info(
                                        "NEWS_OVERRIDE_DEBUG symbol=%s news_score=%.2f event_score=%.2f "
                                        "catalyst_score=%.2f normalized_score=%.2f threshold=%.2f "
                                        "enabled=%s is_core=%s",
                                        sym_u,
                                        float(_news_trend_debug_news_score),
                                        float(_news_trend_debug_event_score),
                                        float(_news_trend_debug_catalyst_score),
                                        float(_news_trend_override_score),
                                        float(_news_trend_override_threshold_cfg),
                                        str(bool(_news_trend_override_enabled)).lower(),
                                        str(
                                            sym_u
                                            in {
                                                str(s or "").strip().upper()
                                                for s in core_symbols
                                            }
                                        ).lower(),
                                    )
                                    if _news_trend_override:
                                        log.info(
                                            "NEWS_TREND_OVERRIDE symbol=%s score=%.2f reason=high_conviction_catalyst",
                                            sym_u,
                                            float(_news_trend_override_score),
                                        )
                                        _entry_gates_route = "news_trend_override"
                                        _override_strength = max(
                                            0.01,
                                            min(1.0, float(_news_trend_override_score) / 10.0),
                                        )
                                        _news_trend_entry_override = EntrySignal(
                                            symbol=sym_u,
                                            side="long",
                                            strength=_override_strength,
                                            stop_pct=engine.strategy.stop_loss_pct,
                                            take_profit_pct=engine.strategy.take_profit_pct,
                                            time_bars_exit=engine.strategy.time_bars_exit,
                                            metadata={
                                                "source": "high_conviction_catalyst",
                                                "news_trend_override": True,
                                                "news_score": float(_news_trend_override_score),
                                                "event_score": max(
                                                    float(_event_score_dyn or 0.0),
                                                    float(_artifact_event_score or 0.0),
                                                    float(_engine_event_score or 0.0),
                                                ),
                                                "catalyst_score": float(_artifact_catalyst_score or 0.0),
                                            },
                                        )
                                        decision = engine.run_entry_gates(
                                            symbol=symbol,
                                            dt=dt,
                                            account_equity=account_equity,
                                            current_positions=current_positions,
                                            sector_exposure_pct=_exposure_snapshot.sector_pct,
                                            spread_pct=spread_pct,
                                            volume_atr_ratio=vol_atr_g,
                                            atr_pct=atr_pct,
                                            ohlcv_df=df,
                                            symbol_sector=SYMBOL_SECTOR,
                                            log_strategy_context=verbose,
                                            regime_size_multiplier=_trend_long_regime_mult,
                                            entry_override=_news_trend_entry_override,
                                            regime_score=_entry_regime_score,
                                            skip_spread_check=_skip_nbbo_spread_tl,
                                            regime_condition=_entry_regime_condition,
                                            gross_exposure_pct=_exposure_snapshot.gross_pct,
                                            net_exposure_pct=_exposure_snapshot.net_pct,
                                            theme_exposure_pct=_exposure_snapshot.theme_pct,
                                            strategy_winrate=0.50,
                                            is_etf=sym_u in ETF_SYMBOLS,
                                            is_inverse_etf=sym_u in INVERSE_ETFS,
                                            theme_key=theme_name,
                                            session_last_equity=_session_last_equity,
                                            cap_relax_factor=_cap_rel,
                                            entry_wave_strong_signal_count=_n_boost,
                                            dynamic_symbols=list(dynamic_set),
                                            entry_route=_entry_gates_route,
                                        )
                                    elif _dyn_hc_prefilter_override:
                                        _entry_gates_route = "momentum_breakout"
                                        _dynamic_hc_entry_override = EntrySignal(
                                            symbol=sym_u,
                                            side="long",
                                            strength=max(
                                                0.01,
                                                min(0.85, float(_dyn_hc_prefilter_score) / 10.0),
                                            ),
                                            stop_pct=engine.strategy.stop_loss_pct,
                                            take_profit_pct=engine.strategy.take_profit_pct,
                                            time_bars_exit=engine.strategy.time_bars_exit,
                                            metadata={
                                                "source": "dynamic_momentum_override",
                                                "dynamic_high_conviction_news_override": True,
                                                "alternate_entry": True,
                                                "news_score": float(_news_trend_debug_news_score),
                                                "event_score": float(_news_trend_debug_event_score),
                                                "catalyst_score": float(_news_trend_debug_catalyst_score),
                                                "catalyst_age_minutes": _news_catalyst_age_minutes_dyn,
                                            },
                                        )
                                        decision = engine.run_entry_gates(
                                            symbol=symbol,
                                            dt=dt,
                                            account_equity=account_equity,
                                            current_positions=current_positions,
                                            sector_exposure_pct=_exposure_snapshot.sector_pct,
                                            spread_pct=spread_pct,
                                            volume_atr_ratio=vol_atr_g,
                                            atr_pct=atr_pct,
                                            ohlcv_df=df,
                                            symbol_sector=SYMBOL_SECTOR,
                                            log_strategy_context=verbose,
                                            regime_size_multiplier=_trend_long_regime_mult,
                                            entry_override=_dynamic_hc_entry_override,
                                            regime_score=_entry_regime_score,
                                            skip_spread_check=_skip_nbbo_spread_tl,
                                            regime_condition=_entry_regime_condition,
                                            gross_exposure_pct=_exposure_snapshot.gross_pct,
                                            net_exposure_pct=_exposure_snapshot.net_pct,
                                            theme_exposure_pct=_exposure_snapshot.theme_pct,
                                            strategy_winrate=0.50,
                                            is_etf=sym_u in ETF_SYMBOLS,
                                            is_inverse_etf=sym_u in INVERSE_ETFS,
                                            theme_key=theme_name,
                                            session_last_equity=_session_last_equity,
                                            cap_relax_factor=_cap_rel,
                                            entry_wave_strong_signal_count=_n_boost,
                                            dynamic_symbols=list(dynamic_set),
                                            entry_route=_entry_gates_route,
                                        )
                                    elif trend_long_ok:
                                        _entry_gates_route = trend_scan_route_label(
                                            is_dynamic_added=_is_dynamic_added
                                        )
                                        if sym_u in _scanner_selected_dynamic_set:
                                            _entry_gates_route = "dynamic_momentum_override"
                                        _pre_entry_override = None
                                        if _entry_gates_route in {"momentum_breakout", "dynamic_momentum_override"}:
                                            _pre_entry_override = EntrySignal(
                                                symbol=sym_u,
                                                side="long",
                                                strength=0.65,
                                                stop_pct=engine.strategy.stop_loss_pct,
                                                take_profit_pct=engine.strategy.take_profit_pct,
                                                time_bars_exit=engine.strategy.time_bars_exit,
                                                metadata={
                                                    "source": "dynamic_momentum_override",
                                                    "alternate_entry": True,
                                                    "ai_catalyst_score": _ai_catalyst_score,
                                                    "ai_catalyst_summary": _ai_catalyst_summary,
                                                },
                                            )

                                        _entry_gate_kwargs = {
                                            "symbol": symbol,
                                            "dt": dt,
                                            "account_equity": account_equity,
                                            "current_positions": current_positions,
                                            "sector_exposure_pct": _exposure_snapshot.sector_pct,
                                            "spread_pct": spread_pct,
                                            "volume_atr_ratio": vol_atr_g,
                                            "atr_pct": atr_pct,
                                            "ohlcv_df": df,
                                            "symbol_sector": SYMBOL_SECTOR,
                                            "log_strategy_context": verbose,
                                            "regime_size_multiplier": _trend_long_regime_mult,
                                            "regime_score": _entry_regime_score,
                                            "skip_spread_check": _skip_nbbo_spread_tl,
                                            "regime_condition": _entry_regime_condition,
                                            "gross_exposure_pct": _exposure_snapshot.gross_pct,
                                            "net_exposure_pct": _exposure_snapshot.net_pct,
                                            "theme_exposure_pct": _exposure_snapshot.theme_pct,
                                            "strategy_winrate": 0.50,
                                            "is_etf": sym_u in ETF_SYMBOLS,
                                            "is_inverse_etf": sym_u in INVERSE_ETFS,
                                            "theme_key": theme_name,
                                            "session_last_equity": _session_last_equity,
                                            "cap_relax_factor": _cap_rel,
                                            "entry_wave_strong_signal_count": _n_boost,
                                            "dynamic_symbols": list(dynamic_set),
                                            "entry_route": _entry_gates_route,
                                            "entry_override": _pre_entry_override,
                                            "dynamic_entry_momentum_score": (
                                                _finite_float_or_none(
                                                    (_scanner_meta_entry or {}).get("score")
                                                    or (_scanner_meta_entry or {}).get("entry_alignment_score")
                                                )
                                                if isinstance(_scanner_meta_entry, Mapping)
                                                else None
                                            ),
                                            "dynamic_entry_breakout_continuation": bool(
                                                _is_dynamic_candidate and getattr(_dyn_sig, "price_above_vwap", False)
                                            ),
                                        }
                                        if _is_dynamic_added or _is_dynamic_candidate:
                                            _log_dynamic_selected_entry_eval_start(
                                                sym_u,
                                                route_candidate=_entry_gates_route,
                                                detail="run_entry_gates_dynamic_ema_bypass",
                                            )
                                        decision = _run_entry_gates_dynamic_ema_bypass(
                                            engine,
                                            config=config,
                                            is_dynamic_candidate=_is_dynamic_candidate,
                                            entry_route=_entry_gates_route,
                                            run_kwargs=_entry_gate_kwargs,
                                        )
                                    elif _news_early_entry:
                                        _entry_gates_route = "news_catalyst"
                                        _news_entry_override = EntrySignal(
                                            symbol=sym_u,
                                            side="long",
                                            strength=0.62,
                                            stop_pct=engine.strategy.stop_loss_pct,
                                            take_profit_pct=engine.strategy.take_profit_pct,
                                            time_bars_exit=engine.strategy.time_bars_exit,
                                            metadata={
                                                "source": "news_catalyst",
                                                "alternate_entry": True,
                                                "news_score": int(math.ceil(_news_score_effective_dyn)),
                                                "news_headline": _news_headline_dyn,
                                                "starter_notional_usd": _news_early_entry_notional,
                                                "max_buy_notional_usd": _news_early_entry_notional,
                                                "add_on_requires_confirmation": True,
                                            },
                                        )
                                        decision = engine.run_entry_gates(
                                            symbol=symbol,
                                            dt=dt,
                                            account_equity=account_equity,
                                            current_positions=current_positions,
                                            sector_exposure_pct=_exposure_snapshot.sector_pct,
                                            spread_pct=spread_pct,
                                            volume_atr_ratio=vol_atr_g,
                                            atr_pct=atr_pct,
                                            ohlcv_df=df,
                                            symbol_sector=SYMBOL_SECTOR,
                                            log_strategy_context=verbose,
                                            regime_size_multiplier=_trend_long_regime_mult,
                                            entry_override=_news_entry_override,
                                            regime_score=_entry_regime_score,
                                            skip_spread_check=_skip_nbbo_spread_tl,
                                            regime_condition=_entry_regime_condition,
                                            gross_exposure_pct=_exposure_snapshot.gross_pct,
                                            net_exposure_pct=_exposure_snapshot.net_pct,
                                            theme_exposure_pct=_exposure_snapshot.theme_pct,
                                            strategy_winrate=0.50,
                                            is_etf=sym_u in ETF_SYMBOLS,
                                            is_inverse_etf=sym_u in INVERSE_ETFS,
                                            theme_key=theme_name,
                                            session_last_equity=_session_last_equity,
                                            cap_relax_factor=_cap_rel,
                                            entry_wave_strong_signal_count=_n_boost,
                                            dynamic_symbols=list(dynamic_set),
                                            entry_route=_entry_gates_route,
                                        )
                                    elif news_buy and _news_override_mode == "light":
                                        _entry_gates_route = "news_light"
                                        decision = engine.run_entry_gates(
                                            symbol=symbol,
                                            dt=dt,
                                            account_equity=account_equity,
                                            current_positions=current_positions,
                                            sector_exposure_pct=_exposure_snapshot.sector_pct,
                                            spread_pct=spread_pct,
                                            volume_atr_ratio=vol_atr_g,
                                            atr_pct=atr_pct,
                                            ohlcv_df=df,
                                            symbol_sector=SYMBOL_SECTOR,
                                            log_strategy_context=verbose,
                                            regime_size_multiplier=_trend_long_regime_mult,
                                            regime_score=_entry_regime_score,
                                            skip_spread_check=_skip_nbbo_spread_tl,
                                            regime_condition=_entry_regime_condition,
                                            gross_exposure_pct=_exposure_snapshot.gross_pct,
                                            net_exposure_pct=_exposure_snapshot.net_pct,
                                            theme_exposure_pct=_exposure_snapshot.theme_pct,
                                            strategy_winrate=0.50,
                                            is_etf=sym_u in ETF_SYMBOLS,
                                            is_inverse_etf=sym_u in INVERSE_ETFS,
                                            theme_key=theme_name,
                                            session_last_equity=_session_last_equity,
                                            cap_relax_factor=_cap_rel,
                                            entry_wave_strong_signal_count=_n_boost,
                                            dynamic_symbols=list(dynamic_set),
                                            entry_route=_entry_gates_route,
                                        )
                                    elif _alt_match is not None:
                                        _alt_strength = alternate_entry_signal_strength(config)
                                        _alt_override = EntrySignal(
                                            symbol=symbol,
                                            side="long",
                                            strength=_alt_strength,
                                            stop_pct=engine.strategy.stop_loss_pct,
                                            take_profit_pct=engine.strategy.take_profit_pct,
                                            time_bars_exit=engine.strategy.time_bars_exit,
                                            metadata={
                                                "source": _alt_match.kind,
                                                "alternate_entry": True,
                                            },
                                        )
                                        _entry_gates_route = "alternate"
                                        decision = engine.run_entry_gates(
                                            symbol=symbol,
                                            dt=dt,
                                            account_equity=account_equity,
                                            current_positions=current_positions,
                                            sector_exposure_pct=_exposure_snapshot.sector_pct,
                                            spread_pct=spread_pct,
                                            volume_atr_ratio=vol_atr_g,
                                            atr_pct=atr_pct,
                                            ohlcv_df=df,
                                            symbol_sector=SYMBOL_SECTOR,
                                            log_strategy_context=verbose,
                                            regime_size_multiplier=_trend_long_regime_mult,
                                            entry_override=_alt_override,
                                            regime_score=_entry_regime_score,
                                            skip_spread_check=_skip_nbbo_spread_tl,
                                            regime_condition=_entry_regime_condition,
                                            gross_exposure_pct=_exposure_snapshot.gross_pct,
                                            net_exposure_pct=_exposure_snapshot.net_pct,
                                            theme_exposure_pct=_exposure_snapshot.theme_pct,
                                            strategy_winrate=0.50,
                                            is_etf=sym_u in ETF_SYMBOLS,
                                            is_inverse_etf=sym_u in INVERSE_ETFS,
                                            theme_key=theme_name,
                                            session_last_equity=_session_last_equity,
                                            cap_relax_factor=_cap_rel,
                                            entry_wave_strong_signal_count=_n_boost,
                                            dynamic_symbols=list(dynamic_set),
                                            entry_route=_entry_gates_route,
                                        )
                                    if (decision is None or not decision.allowed) and news_buy and _news_override_mode == "full":
                                        entry_override = EntrySignal(
                                            symbol=symbol,
                                            side="long",
                                            strength=float(sentiment_score),
                                            stop_pct=engine.strategy.stop_loss_pct,
                                            take_profit_pct=engine.strategy.take_profit_pct,
                                            time_bars_exit=engine.strategy.time_bars_exit,
                                            metadata={
                                                "source": "news_sentiment",
                                                "news_sentiment": sentiment_score,
                                                "volume_ratio": vol_ratio,
                                            },
                                        )
                                        _entry_gates_route = "news_full"
                                        decision = engine.run_entry_gates(
                                            symbol=symbol,
                                            dt=dt,
                                            account_equity=account_equity,
                                            current_positions=current_positions,
                                            sector_exposure_pct=_exposure_snapshot.sector_pct,
                                            spread_pct=spread_pct,
                                            volume_atr_ratio=vol_atr_g,
                                            atr_pct=atr_pct,
                                            ohlcv_df=df,
                                            symbol_sector=SYMBOL_SECTOR,
                                            log_strategy_context=verbose,
                                            regime_size_multiplier=_trend_long_regime_mult,
                                            entry_override=entry_override,
                                            regime_score=_entry_regime_score,
                                            skip_spread_check=_skip_nbbo_spread_tl,
                                            regime_condition=_entry_regime_condition,
                                            gross_exposure_pct=_exposure_snapshot.gross_pct,
                                            net_exposure_pct=_exposure_snapshot.net_pct,
                                            theme_exposure_pct=_exposure_snapshot.theme_pct,
                                            strategy_winrate=0.50,
                                            is_etf=sym_u in ETF_SYMBOLS,
                                            is_inverse_etf=sym_u in INVERSE_ETFS,
                                            theme_key=theme_name,
                                            session_last_equity=_session_last_equity,
                                            cap_relax_factor=_cap_rel,
                                            entry_wave_strong_signal_count=_n_boost,
                                            dynamic_symbols=list(dynamic_set),
                                            entry_route=_entry_gates_route,
                                        )
                                    _route_log = trend_scan_route_label(
                                        is_dynamic_added=_is_dynamic_added
                                    )
                                    _sig_md = (
                                        (decision.entry_signal.metadata or {})
                                        if decision is not None and decision.entry_signal
                                        else {}
                                    )
                                    _route_log = _entry_eval_route_log_from_metadata(
                                        _route_log,
                                        _sig_md,
                                    )
                                    _risk_route_key = str(_route_log or _entry_gates_route or "").strip().lower()
                                    _risk_sleeve_key = sleeve_for_route(
                                        _risk_route_key,
                                        "dynamic_universe" if _is_dynamic_candidate else "trend_long",
                                    )
                                    _live_risk_candidate_block_reason: str | None = None
                                    if (
                                        _live_risk_guard.trend_long_entries_blocked
                                        and _risk_route_key == "trend_long"
                                    ):
                                        _live_risk_candidate_block_reason = "consecutive_live_trend_long_losses"
                                    elif _risk_sleeve_key in dict(_live_risk_guard.sleeve_blocks or {}):
                                        _live_risk_candidate_block_reason = "sleeve_churn_guard_%s" % _risk_sleeve_key
                                    log.info(
                                        "ENTRY_ROUTE_SELECTED symbol=%s route=%s override=%s score=%.2f",
                                        sym_u,
                                        _route_log,
                                        str(bool(_news_trend_override)).lower(),
                                        float(_news_trend_override_score or 0.0),
                                    )
                                    _te, _pe, _me, _ve = engine.strategy.entry_eval_components_for_log(
                                        symbol,
                                        df,
                                        spread_pct,
                                        atr_pct,
                                        regime_score=_entry_regime_score,
                                    )
                                    _reg_eval_ok = not regime_entry_policy.long_entries_blocked
                                    if decision is None:
                                        _eval_allowed = False
                                        _eval_reason = "no_decision"
                                        _spread_eval = _pos_eval = _cd_eval = None
                                    else:
                                        _eval_allowed = bool(decision.allowed)
                                        _eval_reason = (
                                            decision.reason
                                            if not decision.allowed
                                            else (decision.reason or "ok")
                                        )
                                        _spread_eval, _pos_eval, _cd_eval = infer_spread_position_cooldown_ok(
                                            allowed=decision.allowed,
                                            reason=decision.reason,
                                        )
                                    if _live_risk_candidate_block_reason is not None:
                                        _eval_allowed = False
                                        _eval_reason = _live_risk_candidate_block_reason
                                        _spread_eval, _pos_eval, _cd_eval = infer_spread_position_cooldown_ok(
                                            allowed=False,
                                            reason=_eval_reason,
                                        )
                                        log.warning(
                                            "LIVE_RISK_ENTRY_REJECT symbol=%s route=%s reason=%s",
                                            sym_u,
                                            str(_route_log or _entry_gates_route or "n/a"),
                                            _live_risk_candidate_block_reason,
                                        )
                                    _entry_quality_decision = None
                                    _entry_quality_meta: dict[str, Any] = {}
                                    _entry_quality_size_mult = 1.0
                                    _entry_quality_route = str(
                                        _route_log or _entry_gates_route or ""
                                    ).strip().lower()
                                    _sleeve_size_mult = float(
                                        dict(_live_risk_guard.sleeve_size_multipliers or {}).get(
                                            _risk_sleeve_key,
                                            1.0,
                                        )
                                    )
                                    if _eval_allowed and _sleeve_size_mult < 1.0:
                                        if _sleeve_size_mult <= 0.0:
                                            _eval_allowed = False
                                            _eval_reason = "sleeve_churn_guard_%s" % _risk_sleeve_key
                                            _spread_eval, _pos_eval, _cd_eval = infer_spread_position_cooldown_ok(
                                                allowed=False,
                                                reason=_eval_reason,
                                            )
                                            log.warning(
                                                "SLEEVE_BLOCK_TRIGGERED symbol=%s sleeve=%s count=%s",
                                                sym_u,
                                                _risk_sleeve_key,
                                                dict(_live_risk_guard.sleeve_loss_counts or {}).get(_risk_sleeve_key, 0),
                                            )
                                        else:
                                            _entry_quality_size_mult = min(_entry_quality_size_mult, _sleeve_size_mult)
                                            log.info(
                                                "ADAPTIVE_SLEEVE_SIZE symbol=%s sleeve=%s multiplier=%.3f loss_count=%s",
                                                sym_u,
                                                _risk_sleeve_key,
                                                _sleeve_size_mult,
                                                dict(_live_risk_guard.sleeve_loss_counts or {}).get(_risk_sleeve_key, 0),
                                            )
                                    _entry_quality_route_eligible = (
                                        _entry_quality_route == "trend_long"
                                        or (
                                            _is_dynamic_candidate
                                            and _entry_quality_route
                                            in {"dynamic_momentum_override", "dynamic_momentum", "dynamic_universe", "momentum_breakout"}
                                        )
                                    )
                                    if _eval_allowed and _entry_quality_route_eligible and _ref_px is None:
                                        _eval_allowed = False
                                        _eval_reason = "reference_price_unavailable"
                                        _spread_eval, _pos_eval, _cd_eval = infer_spread_position_cooldown_ok(
                                            allowed=False,
                                            reason=_eval_reason,
                                        )
                                        log.warning(
                                            "ENTRY_QUALITY_DATA_UNAVAILABLE symbol=%s route=%s reason=reference_price_unavailable source=%s",
                                            sym_u,
                                            _entry_quality_route or "n/a",
                                            _entry_eval_context.get(
                                                "reference_price_unavailable_reason",
                                                "no_valid_reference_price",
                                            ),
                                        )
                                    if (
                                        _eval_allowed
                                        and _entry_quality_route_eligible
                                    ):
                                        _quality_symbol_vwap = None
                                        _quality_market_ok = False
                                        _quality_market_vwap = None
                                        _quality_market_px = None
                                        _quality_market_distance_pct = None
                                        _quality_market_slope = None
                                        _quality_market_data_available = False
                                        _quality_market_state = "unavailable"
                                        _quality_qqq_ok = False
                                        _quality_qqq_vwap = None
                                        _quality_qqq_px = None
                                        _quality_sector_symbol = None
                                        _quality_sector_ok = True
                                        _quality_atr_abs = None
                                        _quality_news = 0.0
                                        _quality_event = 0.0
                                        _quality_catalyst = 0.0
                                        _quality_has_strong_catalyst = False
                                        _quality_session_features = _session_feature_result(dt)
                                        _quality_session_open = _quality_session_features.get("session_open")
                                        _quality_session_start_utc = (
                                            _quality_session_open.astimezone(pytz.UTC)
                                            if _quality_session_open is not None
                                            else None
                                        )
                                        _quality_session_end_utc = dt.astimezone(pytz.UTC) if getattr(dt, "tzinfo", None) is not None else None
                                        _quality_symbol_vwap = locals().get("_paper_session_vwap")
                                        if _quality_symbol_vwap is None:
                                            try:
                                                _quality_bars_1m = broker.get_bars(
                                                    sym_u,
                                                    timeframe="1Min",
                                                    start=_quality_session_start_utc,
                                                    end=_quality_session_end_utc,
                                                    limit=390,
                                                )
                                                _quality_symbol_vwap = session_vwap_from_bars(_quality_bars_1m)
                                            except Exception:
                                                _quality_symbol_vwap = None
                                        _market_feature = _market_vwap_feature_result(
                                            broker,
                                            symbol="SPY",
                                            start=_quality_session_start_utc,
                                            end=_quality_session_end_utc,
                                        )
                                        _quality_market_ok = bool(_market_feature["confirmed"])
                                        _quality_market_vwap = _market_feature["market_vwap"]
                                        _quality_market_px = _market_feature["market_price"]
                                        _quality_market_distance_pct = _market_feature["distance_pct"]
                                        _quality_market_slope = _market_feature["slope"]
                                        _quality_market_data_available = bool(_market_feature["data_available"])
                                        _quality_market_state = str(_market_feature["state"])
                                        _qqq_feature = _market_vwap_feature_result(
                                            broker,
                                            symbol="QQQ",
                                            start=_quality_session_start_utc,
                                            end=_quality_session_end_utc,
                                        )
                                        _quality_qqq_ok = bool(_qqq_feature["confirmed"])
                                        _quality_qqq_vwap = _qqq_feature["market_vwap"]
                                        _quality_qqq_px = _qqq_feature["market_price"]
                                        _quality_sector_symbol = sector_confirmation_symbol(sym_u, SYMBOL_SECTOR)
                                        _quality_sector_ok = True
                                        if _quality_sector_symbol:
                                            try:
                                                _quality_sector_bars = broker.get_bars(
                                                    _quality_sector_symbol,
                                                    timeframe="1Min",
                                                    start=_quality_session_start_utc,
                                                    end=_quality_session_end_utc,
                                                    limit=390,
                                                )
                                                _quality_sector_vwap = session_vwap_from_bars(_quality_sector_bars)
                                                _quality_sector_px = (
                                                    float(_quality_sector_bars["close"].iloc[-1])
                                                    if _quality_sector_bars is not None
                                                    and not getattr(_quality_sector_bars, "empty", True)
                                                    else None
                                                )
                                                _quality_sector_ok = bool(
                                                    _quality_sector_px is not None
                                                    and _quality_sector_vwap is not None
                                                    and float(_quality_sector_vwap) > 0.0
                                                    and float(_quality_sector_px) >= float(_quality_sector_vwap) - 1e-9
                                                )
                                            except Exception:
                                                _quality_sector_ok = False
                                        try:
                                            _quality_atr_abs = (
                                                float(_ref_px) * float(atr_pct) / 100.0
                                                if atr_pct is not None and atr_pct == atr_pct
                                                else None
                                            )
                                        except Exception:
                                            _quality_atr_abs = None
                                        try:
                                            _quality_news = float(_sig_md.get("news_score", 0.0)) if isinstance(_sig_md, dict) else 0.0
                                        except Exception:
                                            _quality_news = 0.0
                                        try:
                                            _quality_event = float(_sig_md.get("event_score", 0.0)) if isinstance(_sig_md, dict) else 0.0
                                        except Exception:
                                            _quality_event = 0.0
                                        try:
                                            _quality_catalyst = float(_sig_md.get("catalyst_score", 0.0)) if isinstance(_sig_md, dict) else 0.0
                                        except Exception:
                                            _quality_catalyst = 0.0
                                        _strong_catalyst_threshold = float(
                                            ((config.get("entry_quality") or {}) if isinstance(config.get("entry_quality"), dict) else {}).get(
                                                "strong_catalyst_threshold",
                                                6.0,
                                            )
                                        )
                                        _quality_has_strong_catalyst = max(
                                            _quality_news,
                                            _quality_event,
                                            _quality_catalyst,
                                        ) >= _strong_catalyst_threshold
                                        _entry_quality_decision = evaluate_entry_quality(
                                            route=str(_route_log or _entry_gates_route or "n/a"),
                                            symbol=sym_u,
                                            df=df,
                                            price=float(_ref_px),
                                            symbol_vwap=float(_quality_symbol_vwap)
                                            if _quality_symbol_vwap is not None
                                            else None,
                                            market_vwap_confirmed=bool(_quality_market_ok),
                                            spy_above_vwap=bool(_quality_market_ok),
                                            qqq_above_vwap=bool(_quality_qqq_ok),
                                            sector_confirmed=bool(_quality_sector_ok),
                                            regime_score=_entry_regime_score,
                                            trend_5m_positive=bool(_me),
                                            trend_15m_positive=bool(_te),
                                            relative_volume=_finite_float_or_none(locals().get("_rel_vol_e")),
                                            pullback_confirmed=bool(locals().get("_pe", True)),
                                            volume_confirmed=bool(locals().get("_ve", True)),
                                            spread_pct=spread_pct,
                                            is_live=bool(getattr(args, "live", False)),
                                            has_strong_catalyst=bool(_quality_has_strong_catalyst),
                                            news_score=_quality_news,
                                            catalyst_score=_quality_catalyst,
                                            event_score=_quality_event,
                                            momentum_confirmed=bool(_me),
                                            market_vwap_distance_pct=_quality_market_distance_pct,
                                            market_vwap_slope=_quality_market_slope,
                                            market_vwap_data_available=bool(_quality_market_data_available),
                                            day_gain_pct=_finite_float_or_none(locals().get("_gain_pct_e")),
                                            breakout_confirmed=bool(locals().get("_dyn_sig", None)),
                                            atr=_quality_atr_abs,
                                            atr_pct=float(atr_pct)
                                            if atr_pct is not None and atr_pct == atr_pct
                                            else None,
                                            max_atr_pct=float(
                                                engine.strategy.effective_max_atr_pct_for_entry(_entry_regime_score)
                                            ),
                                            config=config,
                                        )
                                        _entry_quality_meta = _entry_quality_metadata(_entry_quality_decision)
                                        log.info(
                                            "STRATEGY_QUALITY_SCORE symbol=%s route=%s score=%.2f allowed=%s reason=%s "
                                            "spy_vwap=%s qqq_vwap=%s symbol_vwap=%s sector_confirmed=%s no_chase=%s sizing_multiplier=%.3f",
                                            sym_u,
                                            _entry_quality_route,
                                            float(_entry_quality_decision.quality_score),
                                            str(bool(_entry_quality_decision.allowed)).lower(),
                                            _entry_quality_decision.reason,
                                            str(bool(_entry_quality_decision.market_vwap_confirmed)).lower(),
                                            str(bool(_quality_qqq_ok)).lower(),
                                            str(bool(_entry_quality_decision.symbol_vwap_confirmed)).lower(),
                                            str(bool(_entry_quality_decision.sector_confirmed)).lower(),
                                            str(bool(_entry_quality_decision.no_chase_ok)).lower(),
                                            float(_entry_quality_decision.sizing_multiplier),
                                        )
                                        if _entry_quality_decision.adaptive_scoring_used:
                                            _eq_components = _entry_quality_decision.score_components or {}
                                            log.info(
                                                "ENTRY_QUALITY_SCORE symbol=%s route=%s score=%.2f threshold=%.2f size_multiplier=%.2f "
                                                "passed=%s hard_gates_passed=true components=%s zero_score_factors=%s adaptive_entry=%s "
                                                "reason=%s market_vwap_confirmed=%s market_vwap_distance_pct=%s market_vwap_slope=%s "
                                                "market_vwap_score=%s market_vwap_state=%s market_vwap_data_available=%s "
                                                "unavailable_feature_policy=%s",
                                                sym_u,
                                                _entry_quality_route,
                                                float(_entry_quality_decision.quality_score),
                                                float(_entry_quality_decision.score_threshold or 0.0),
                                                float(_entry_quality_decision.sizing_multiplier),
                                                str(bool(_entry_quality_decision.allowed)).lower(),
                                                json.dumps(_eq_components, sort_keys=True, separators=(",", ":")),
                                                ",".join(_entry_quality_decision.zero_score_factors or ()) or "none",
                                                str(bool(_entry_quality_decision.adaptive_entry)).lower(),
                                                _entry_quality_decision.entry_quality_reason or _entry_quality_decision.reason,
                                                str(bool(_entry_quality_decision.market_vwap_confirmed)).lower(),
                                                "n/a" if _entry_quality_decision.market_vwap_distance_pct is None else "%.4f" % float(_entry_quality_decision.market_vwap_distance_pct),
                                                "n/a" if _entry_quality_decision.market_vwap_slope is None else "%.4f" % float(_entry_quality_decision.market_vwap_slope),
                                                "n/a" if _entry_quality_decision.market_vwap_score is None else "%.2f" % float(_entry_quality_decision.market_vwap_score),
                                                _entry_quality_decision.market_vwap_state,
                                                str(bool(_entry_quality_decision.market_vwap_data_available)).lower(),
                                                str(
                                                    ((config.get("entry_quality") or {}) if isinstance(config.get("entry_quality"), dict) else {}).get(
                                                        "unavailable_feature_policy",
                                                        "conservative",
                                                    )
                                                ),
                                            )
                                        log.info(
                                            "SECTOR_CONFIRMATION_RESULT symbol=%s sector_etf=%s confirmed=%s",
                                            sym_u,
                                            _quality_sector_symbol or "none",
                                            str(bool(_quality_sector_ok)).lower(),
                                        )
                                        if _entry_quality_decision.reason in {"vwap_distance_chase", "atr_extension_chase"}:
                                            log.info("NO_CHASE_BLOCK symbol=%s reason=%s", sym_u, _entry_quality_decision.reason)
                                        if _entry_quality_route != "trend_long" and not _quality_has_strong_catalyst:
                                            log.info(
                                                "DYNAMIC_NO_CATALYST_GUARD symbol=%s route=%s allowed=%s reason=%s starter=%s",
                                                sym_u,
                                                _entry_quality_route,
                                                str(bool(_entry_quality_decision.allowed)).lower(),
                                                _entry_quality_decision.reason,
                                                str(bool(_entry_quality_decision.starter)).lower(),
                                            )
                                        if not _entry_quality_decision.allowed:
                                            _eval_allowed = False
                                            _eval_reason = "entry_quality_%s" % _entry_quality_decision.reason
                                            _spread_eval, _pos_eval, _cd_eval = infer_spread_position_cooldown_ok(
                                                allowed=False,
                                                reason=_eval_reason,
                                            )
                                            if _entry_quality_route == "trend_long":
                                                log.info(
                                                    "TREND_LONG_QUALITY_BLOCK symbol=%s score=%.2f reason=%s",
                                                    sym_u,
                                                    float(_entry_quality_decision.quality_score),
                                                    _entry_quality_decision.reason,
                                                )
                                            if len(_entry_quality_decision.rejected_rules or ()) == 1:
                                                try:
                                                    record_trade_attribution_rejected_one_rule(
                                                        data_dir=_data_dir,
                                                        user_id=str(_uid),
                                                        timestamp=dt,
                                                        symbol=sym_u,
                                                        rejected_rule=_entry_quality_decision.rejected_rules[0],
                                                        features=_entry_quality_decision.features,
                                                        price=_ref_px,
                                                    )
                                                    log.info(
                                                        "REJECTED_ONE_RULE_RESEARCH symbol=%s rule=%s",
                                                        sym_u,
                                                        _entry_quality_decision.rejected_rules[0],
                                                    )
                                                except Exception:
                                                    log.debug("rejected-one-rule write failed", exc_info=True)
                                        else:
                                            _entry_quality_size_mult = min(
                                                _entry_quality_size_mult,
                                                float(_entry_quality_decision.sizing_multiplier),
                                            )
                                            if _entry_quality_decision.starter:
                                                log.info(
                                                    "STARTER_ENTRY_SIZE symbol=%s route=%s multiplier=%.3f",
                                                    sym_u,
                                                    _entry_quality_route,
                                                    float(_entry_quality_size_mult),
                                                )
                                    _final_route_for_rvol_guard = str(
                                        _route_log or _entry_gates_route or ""
                                    ).strip().lower()
                                    if (
                                        _is_dynamic_candidate
                                        and _final_route_for_rvol_guard == "dynamic_momentum_override"
                                    ):
                                        _entry_rel_for_log = _finite_float_or_none(_rel_vol_e)
                                        _entry_threshold_for_log = _finite_float_or_none(
                                            _entry_effective_min_rel_volume
                                        )
                                        _entry_fastlane_for_log = bool(
                                            _entry_catalyst_fastlane_active
                                        )
                                        _guard_result = "missing_threshold"
                                        if _entry_rel_for_log is None:
                                            _guard_result = "missing_relative_volume"
                                        elif _entry_threshold_for_log is not None:
                                            if (
                                                not _entry_fastlane_for_log
                                                and _entry_rel_for_log
                                                < _entry_threshold_for_log - 1e-9
                                            ):
                                                _guard_result = "fail"
                                                _eval_allowed = False
                                                _eval_reason = (
                                                    "relative_volume %.2f < %.2f"
                                                    % (
                                                        _entry_rel_for_log,
                                                        _entry_threshold_for_log,
                                                    )
                                                )
                                                _spread_eval, _pos_eval, _cd_eval = (
                                                    infer_spread_position_cooldown_ok(
                                                        allowed=False,
                                                        reason=_eval_reason,
                                                    )
                                                )
                                            else:
                                                _guard_result = "pass"
                                        log.info(
                                            "ENTRY_EVAL_DYNAMIC_RVOL_GUARD symbol=%s route=%s "
                                            "relative_volume=%s threshold_used=%s fastlane_active=%s "
                                            "guard_result=%s",
                                            sym_u,
                                            str(_route_log or _entry_gates_route or "n/a"),
                                            "n/a"
                                            if _entry_rel_for_log is None
                                            else "%.3f" % _entry_rel_for_log,
                                            "n/a"
                                            if _entry_threshold_for_log is None
                                            else "%.3f" % _entry_threshold_for_log,
                                            str(_entry_fastlane_for_log).lower(),
                                            _guard_result,
                                        )
                                    _allocator_entry_eval_followup_emitted = False
                                    _allocator_entry_eval_followup_payload = None
                                    if _cap_alloc_enabled and _eval_allowed:
                                        _entry_eval_alloc_score = _entry_eval_allocator_score(
                                            route=str(_route_log or _entry_gates_route or "n/a"),
                                            final=bool(_eval_allowed),
                                            trend=_te if _te is not None else trend_long_ok,
                                            pullback=_pe,
                                            momentum=_me,
                                            volatility=_ve,
                                        )
                                        if decision is None:
                                            _allocator_entry_eval_reason = "no_decision"
                                            _allocator_entry_eval_action = "skip"
                                        elif not getattr(decision, "allowed", False):
                                            _allocator_entry_eval_reason = (
                                                getattr(decision, "reason", None)
                                                or "entry_gates_blocked"
                                            )
                                            _allocator_entry_eval_action = "skip"
                                        elif (
                                            df is not None
                                            and not getattr(df, "empty", True)
                                            and bool(getattr(decision, "order_request", None))
                                        ):
                                            _allocator_entry_eval_reason = _eval_reason or "ok"
                                            _allocator_entry_eval_action = "append_now"
                                        elif not bool(getattr(decision, "order_request", None)):
                                            _allocator_entry_eval_reason = "no_order_request"
                                            _allocator_entry_eval_action = "skip"
                                        else:
                                            _allocator_entry_eval_reason = "missing_ohlcv"
                                            _allocator_entry_eval_action = "skip"
                                        _allocator_entry_eval_followup_payload = {
                                            "symbol": sym_u,
                                            "route": str(_route_log or _entry_gates_route or "n/a"),
                                            "reason": str(_allocator_entry_eval_reason or "ok"),
                                            "allocator_on": bool(_cap_alloc_enabled),
                                            "action": _allocator_entry_eval_action,
                                            "stage": "entry_eval",
                                            "score": _entry_eval_alloc_score,
                                            "decision_present": decision is not None,
                                            "decision_allowed": bool(
                                                decision is not None
                                                and getattr(decision, "allowed", False)
                                            ),
                                            "order_request_present": bool(
                                                decision is not None
                                                and getattr(decision, "order_request", None)
                                            ),
                                            "ohlcv_present": bool(
                                                df is not None and not getattr(df, "empty", True)
                                            ),
                                            "followup_emitted": False,
                                            "scanner_selected": bool(_is_dynamic_added or _is_dynamic_candidate),
                                            "relative_volume": _rel_vol_e,
                                            "rel_volume": _rel_vol_e,
                                            "effective_min_rel_volume": _entry_effective_min_rel_volume,
                                            "entry_effective_min_rel_volume": _entry_effective_min_rel_volume,
                                            "entry_eval_effective_min_rel_volume": _entry_effective_min_rel_volume,
                                            "catalyst_fastlane_active": bool(_entry_catalyst_fastlane_active),
                                            "catalyst_min_relative_volume": _entry_catalyst_min_relative_volume,
                                            "gain_pct": _gain_pct_e,
                                            "day_gain_pct": _gain_pct_e,
                                            "dynamic_score": float(
                                                _dynamic_momentum_scores.get(sym_u, 0.0) or 0.0
                                            )
                                            if _is_dynamic_candidate
                                            else 0.0,
                                            "scanner_score": float(
                                                _dynamic_momentum_scores.get(sym_u, 0.0) or 0.0
                                            )
                                            if _is_dynamic_candidate
                                            else 0.0,
                                            "signal_score": float(
                                                _dynamic_momentum_scores.get(sym_u, 0.0) or 0.0
                                            )
                                            if _is_dynamic_candidate
                                            else float(_entry_eval_alloc_score),
                                            "trend_long_quality_score": None
                                            if _entry_quality_decision is None
                                            else float(_entry_quality_decision.quality_score),
                                            "entry_quality_reason": None
                                            if _entry_quality_decision is None
                                            else _entry_quality_decision.reason,
                                            "starter_size": bool(
                                                _entry_quality_decision is not None
                                                and _entry_quality_decision.starter
                                            ),
                                            "entry_quality_size_multiplier": float(_entry_quality_size_mult),
                                        }
                                        _allocator_entry_eval_followup_payload.update(
                                            _scanner_meta_payload
                                        )
                                        _allocator_entry_eval_followup_payload.update(_entry_quality_meta)
                                        if _allocator_entry_eval_action == "skip":
                                            _allocator_entry_eval_followup_emitted = True
                                    if _is_dynamic_added or _is_dynamic_candidate:
                                        _log_dynamic_selected_entry_eval_start(
                                            sym_u,
                                            route_candidate=str(_route_log or _entry_gates_route or "unknown"),
                                            detail="log_entry_eval",
                                        )
                                    if sym_u in _scanner_selected_dynamic_set:
                                        _dynamic_entry_eval_started_symbols.add(sym_u)
                                        _log_dynamic_entry_eval_start(
                                            sym_u,
                                            source="scanner_selected",
                                            route="dynamic_momentum_override",
                                        )
                                    log_entry_eval(
                                        symbol=sym_u,
                                        route=_route_log,
                                        trend=_te if _te is not None else trend_long_ok,
                                        pullback=_pe,
                                        momentum=_me,
                                        volatility=_ve,
                                        regime=_reg_eval_ok,
                                        spread=_spread_eval,
                                        position=_pos_eval,
                                        cooldown=_cd_eval,
                                        final_signal=_eval_allowed,
                                        final_reason=_eval_reason,
                                        ai_catalyst_score=_ai_catalyst_score,
                                        event_store=_sqlite_store,
                                        user_id=str(_uid),
                                        allocator_followup=_allocator_entry_eval_followup_payload,
                                    )
                                    if _cap_alloc_enabled and _eval_allowed:
                                        _entry_allocator_final_true[sym_u] = {
                                            "symbol": sym_u,
                                            "route": str(_route_log or _entry_gates_route or "n/a"),
                                            "reason": str(_eval_reason or "ok"),
                                            "dynamic_candidate": bool(_is_dynamic_candidate),
                                            "scanner_selected": bool(_is_dynamic_added or _is_dynamic_candidate),
                                        }
                                        _entry_allocator_final_true[sym_u].update(
                                            _scanner_meta_payload
                                        )
                                        if (
                                            _allocator_entry_eval_followup_payload is not None
                                            and _allocator_entry_eval_followup_payload.get("action") == "skip"
                                        ):
                                            _entry_allocator_skipped.add(sym_u)
                                            _record_entry_terminal_outcome_live(
                                                store=_sqlite_store,
                                                user_id=str(_uid),
                                                symbol=sym_u,
                                                route=str(_route_log or _entry_gates_route or "n/a"),
                                                stage="skipped_with_reason",
                                                reason=str(
                                                    _allocator_entry_eval_followup_payload.get("reason")
                                                    or _eval_reason
                                                    or "entry_eval_allocator_skip"
                                                ),
                                                payload={
                                                    "entry_eval_final": True,
                                                    "dynamic_candidate": bool(_is_dynamic_candidate),
                                                    "scanner_selected": bool(_is_dynamic_added or _is_dynamic_candidate),
                                                    "route": str(_route_log or _entry_gates_route or "n/a"),
                                                    "source": "dynamic_universe"
                                                    if _is_dynamic_candidate
                                                    else "trend_long",
                                                    "decision_present": bool(
                                                        _allocator_entry_eval_followup_payload.get(
                                                            "decision_present"
                                                        )
                                                    ),
                                                    "decision_allowed": bool(
                                                        _allocator_entry_eval_followup_payload.get(
                                                            "decision_allowed"
                                                        )
                                                    ),
                                                    "order_request_present": bool(
                                                        _allocator_entry_eval_followup_payload.get(
                                                            "order_request_present"
                                                        )
                                                    ),
                                                    "ohlcv_present": bool(
                                                        _allocator_entry_eval_followup_payload.get(
                                                            "ohlcv_present"
                                                        )
                                                    ),
                                                    "relative_volume": _allocator_entry_eval_followup_payload.get(
                                                        "relative_volume"
                                                    ),
                                                    "rel_volume": _allocator_entry_eval_followup_payload.get(
                                                        "rel_volume"
                                                    ),
                                                    "effective_min_rel_volume": _allocator_entry_eval_followup_payload.get(
                                                        "effective_min_rel_volume"
                                                    ),
                                                    "entry_effective_min_rel_volume": _allocator_entry_eval_followup_payload.get(
                                                        "entry_effective_min_rel_volume"
                                                    ),
                                                    "catalyst_fastlane_active": _allocator_entry_eval_followup_payload.get(
                                                        "catalyst_fastlane_active"
                                                    ),
                                                    "catalyst_min_relative_volume": _allocator_entry_eval_followup_payload.get(
                                                        "catalyst_min_relative_volume"
                                                    ),
                                                    "gain_pct": _allocator_entry_eval_followup_payload.get(
                                                        "gain_pct"
                                                    ),
                                                    "day_gain_pct": _allocator_entry_eval_followup_payload.get(
                                                        "day_gain_pct"
                                                    ),
                                                    "dynamic_score": _allocator_entry_eval_followup_payload.get(
                                                        "dynamic_score"
                                                    ),
                                                    "scanner_score": _allocator_entry_eval_followup_payload.get(
                                                        "scanner_score"
                                                    ),
                                                    "signal_score": _allocator_entry_eval_followup_payload.get(
                                                        "signal_score"
                                                    ),
                                                },
                                                ts=dt,
                                            )
                                    if (
                                        _cap_alloc_enabled
                                        and _eval_allowed
                                        and not _allocator_entry_eval_followup_emitted
                                        and _allocator_entry_eval_followup_payload is not None
                                        and _allocator_entry_eval_followup_payload.get("action") == "append_now"
                                        and decision is not None
                                        and bool(getattr(decision, "allowed", False))
                                        and bool(getattr(decision, "order_request", None))
                                        and df is not None
                                        and not getattr(df, "empty", True)
                                    ):
                                        _immediate_notional = (
                                            decision.position_sizing.notional
                                            if getattr(decision, "position_sizing", None)
                                            else 0
                                        ) or 0
                                        _immediate_notional = entry_target_dollars_for_symbol(
                                            float(_immediate_notional),
                                            symbol=sym_u,
                                            core_symbols=core_symbols,
                                            account_equity=float(account_equity),
                                            config=config,
                                        )
                                        if _entry_quality_size_mult < 1.0:
                                            _immediate_notional = float(_immediate_notional) * float(_entry_quality_size_mult)
                                        _row_alloc_immediate = {
                                            "symbol": symbol,
                                            "sym_u": sym_u,
                                            "decision": decision,
                                            "df": df,
                                            "quote": quote,
                                            "notional": _immediate_notional,
                                            "user_id": str(_uid),
                                            "captured_at": dt,
                                            "entry_eval_final": True,
                                            "trend_long_ok": bool(
                                                trend_long_ok or (_alt_match is not None)
                                            ),
                                            "alternate_entry": _alt_match.kind
                                            if _alt_match
                                            else None,
                                            "atr_pct": atr_pct,
                                            "entry_regime_score": _entry_regime_score,
                                            "score": float(_entry_eval_alloc_score),
                                            "strength_eff": float(_entry_eval_alloc_score),
                                            "is_add_flow": bool(_is_add_flow),
                                            "dynamic_candidate": bool(_is_dynamic_candidate),
                                            "scanner_selected": bool(_is_dynamic_added or _is_dynamic_candidate),
                                            "source": "dynamic_universe"
                                            if _is_dynamic_candidate
                                            else "trend_long",
                                            "route": str(
                                                _route_log or _entry_gates_route or "n/a"
                                            ),
                                            "paper_current_price": float(_paper_current_price),
                                            "paper_session_vwap": _paper_session_vwap,
                                            "news_score": float(_news_score_effective_dyn),
                                            "event_score": float(_event_score_dyn),
                                            "catalyst_score": float(
                                                _news_trend_debug_catalyst_score or 0.0
                                            ),
                                            "catalyst_age_minutes": _news_catalyst_age_minutes_dyn,
                                            "relative_volume": _rel_vol_e,
                                            "rel_volume": _rel_vol_e,
                                            "effective_min_rel_volume": _entry_effective_min_rel_volume,
                                            "entry_effective_min_rel_volume": _entry_effective_min_rel_volume,
                                            "entry_eval_effective_min_rel_volume": _entry_effective_min_rel_volume,
                                            "catalyst_fastlane_active": bool(_entry_catalyst_fastlane_active),
                                            "catalyst_min_relative_volume": _entry_catalyst_min_relative_volume,
                                            "gain_pct": _gain_pct_e,
                                            "day_gain_pct": _gain_pct_e,
                                            "spread_pct": _spread_for_dme
                                            if _is_dynamic_candidate
                                            else spread_pct,
                                            "dynamic_score": float(
                                                _dynamic_momentum_scores.get(sym_u, 0.0) or 0.0
                                            )
                                            if _is_dynamic_candidate
                                            else 0.0,
                                            "scanner_score": float(
                                                _dynamic_momentum_scores.get(sym_u, 0.0) or 0.0
                                            )
                                            if _is_dynamic_candidate
                                            else 0.0,
                                            "signal_score": float(
                                                _dynamic_momentum_scores.get(sym_u, 0.0) or 0.0
                                            )
                                            if _is_dynamic_candidate
                                            else float(_entry_eval_alloc_score),
                                            "dynamic_symbol": bool(_is_dynamic_candidate),
                                            "is_dynamic": bool(_is_dynamic_candidate),
                                            "trend_long_quality_score": None
                                            if _entry_quality_decision is None
                                            else float(_entry_quality_decision.quality_score),
                                            "entry_quality_reason": None
                                            if _entry_quality_decision is None
                                            else _entry_quality_decision.reason,
                                            "starter_size": bool(
                                                _entry_quality_decision is not None
                                                and _entry_quality_decision.starter
                                            ),
                                            "entry_quality_size_multiplier": float(_entry_quality_size_mult),
                                        }
                                        _row_alloc_immediate.update(_scanner_meta_payload)
                                        _row_alloc_immediate.update(_entry_quality_meta)
                                        _append_entry_eval_allocator_candidate_now(
                                            _cap_alloc_candidates,
                                            _row_alloc_immediate,
                                            symbol=sym_u,
                                            route=str(_route_log or _entry_gates_route or "n/a"),
                                            reason=str(_eval_reason or "ok"),
                                            score=float(_entry_eval_alloc_score),
                                            allocator_on=bool(_cap_alloc_enabled),
                                            final=bool(_eval_allowed),
                                            stage="entry_eval",
                                        )
                                        _record_entry_allocator_stage_for_rows(
                                            [_row_alloc_immediate],
                                            stage="allocator_appended",
                                            reason="queued_from_entry_eval",
                                            store=_sqlite_store,
                                            user_id=str(_uid),
                                            ts=dt,
                                            symbols_out=_entry_allocator_appended,
                                            payload_extra={"append_stage": "entry_eval"},
                                        )
                                        _allocator_entry_eval_followup_emitted = True
                                        if _allocator_entry_eval_followup_payload is not None:
                                            _allocator_entry_eval_followup_payload[
                                                "followup_emitted"
                                            ] = True
                                    if _options_route_observability_active(config, broker):
                                        _option_route_eval_row = {
                                            "sym_u": sym_u,
                                            "symbol": sym_u,
                                            "route": _route_log,
                                            "source": _route_log,
                                            "entry_eval_final": bool(_eval_allowed),
                                            "news_score": _news_trend_debug_news_score,
                                            "event_score": _news_trend_debug_event_score,
                                            "catalyst_score": _news_trend_debug_catalyst_score,
                                            "relative_volume": _rel_vol_e,
                                        }
                                        if (
                                            not _eval_allowed
                                            and _paper_option_route_observable(
                                                _option_route_eval_row,
                                                entry_eval_final=False,
                                            )
                                        ):
                                            _log_option_route_check(
                                                sym_u,
                                                lane="entry_eval",
                                                row_tl=_option_route_eval_row,
                                                entry_eval_final=False,
                                            )
                                            _log_option_route_skipped(
                                                sym_u,
                                                lane="entry_eval",
                                                reason="entry_eval_false",
                                                detail=_eval_reason,
                                                row_tl=_option_route_eval_row,
                                            )
                                    try:
                                        _entry_attr_rel = locals().get("_rel_vol_e")
                                        _entry_attr_gain = locals().get("_gain_pct_e")
                                        _entry_attr_atr = locals().get("_atr_expansion_ratio")
                                        _entry_attr_vwap = locals().get("_dyn_sig")
                                        _entry_attr_payload = dict(_sig_md) if isinstance(_sig_md, dict) else {}
                                        _entry_attr_payload.update(
                                            {
                                                "symbol": sym_u,
                                                "route": _route_log,
                                                "source": _entry_attr_payload.get("source") or _route_log,
                                                "dynamic_candidate": bool(_is_dynamic_added or sym_u in dynamic_set),
                                                "final": bool(_eval_allowed),
                                                "reason": _eval_reason,
                                                "spread_pct": spread_pct,
                                                "relative_volume": _entry_attr_rel,
                                                "day_gain_pct": _entry_attr_gain,
                                                "atr_expansion_ratio": _entry_attr_atr,
                                                "regime_score": _entry_regime_score,
                                            }
                                        )
                                        if "vwap_above" not in _entry_attr_payload and _entry_attr_vwap is not None:
                                            _entry_attr_payload["vwap_above"] = bool(
                                                getattr(_entry_attr_vwap, "price_above_vwap", False)
                                            )
                                        record_trade_attribution_candidate(
                                            data_dir=_data_dir,
                                            user_id=str(_uid),
                                            timestamp=dt,
                                            candidate=_entry_attr_payload,
                                            regime_score=_entry_regime_score,
                                        )
                                        _capture_diag = capture_runtime_forward_bars(
                                            broker=broker,
                                            data_dir=_data_dir,
                                            user_id=str(_uid),
                                            timestamp=dt,
                                            symbols=[sym_u],
                                            config=config if isinstance(config, Mapping) else None,
                                        )
                                        if isinstance(_capture_diag, Mapping) and not _capture_diag.get("skipped"):
                                            log.info(
                                                "[%s] FORWARD_BAR_CAPTURE symbol=%s summary=%s reason=%s",
                                                str(_uid),
                                                sym_u,
                                                _capture_diag.get("summary"),
                                                _capture_diag.get("reason"),
                                            )
                                    except Exception:
                                        log.warning(
                                            "[%s] trade attribution entry/bar sidecar failed for %s",
                                            str(_uid),
                                            sym_u,
                                            exc_info=True,
                                        )
                                    if not _eval_allowed:
                                        try:
                                            _bp_block = float(available_cash)
                                        except (TypeError, ValueError):
                                            _bp_block = 0.0
                                        try:
                                            _sp_block = (
                                                float(spread_pct)
                                                if spread_pct is not None
                                                and spread_pct == spread_pct
                                                else 0.0
                                            )
                                        except (TypeError, ValueError):
                                            _sp_block = 0.0
                                        log_execution_block(
                                            symbol=sym_u,
                                            spread_pct=_sp_block,
                                            buying_power=_bp_block,
                                            cooldown_ok=_cd_eval,
                                            position_ok=_pos_eval,
                                        )
                                        if _news_early_entry:
                                            log.info(
                                                "NEWS_BLOCKED symbol=%s reason=%s",
                                                sym_u,
                                                _eval_reason,
                                            )
                                    elif _news_early_entry:
                                        try:
                                            _ne_notional = float(
                                                decision.position_sizing.notional
                                                if decision is not None
                                                and decision.position_sizing is not None
                                                else _news_early_entry_notional
                                            )
                                        except (TypeError, ValueError):
                                            _ne_notional = float(_news_early_entry_notional)
                                        log.info(
                                            "NEWS_EARLY_ENTRY symbol=%s notional=%.2f",
                                            sym_u,
                                            _ne_notional,
                                        )
                                    # ENTRY_EVAL final=F and "cap" in reason (e.g. symbol cap → zero shares): try rotation.
                                    if (
                                        decision is not None
                                        and not decision.allowed
                                        and replacement_entry_fail_reason_invites_cap_rotation(
                                            _eval_reason
                                        )
                                        and len(_eligible_active) > 0
                                    ):
                                        _ers_retry = None
                                        if _entry_regime_score is not None:
                                            try:
                                                _ers_retry = int(float(_entry_regime_score))
                                            except (TypeError, ValueError):
                                                _ers_retry = None
                                        _rep_min_in_sz = float(
                                            (_rep_sub or {}).get("min_notional_for_incoming_usd") or 750.0
                                        )
                                        _rot_sz = consider_replacement_for_sizing_reject(
                                            incoming_sym_upper=sym_u,
                                            decision=decision,
                                            tracked=tracked,
                                            eligible_active=_eligible_active,
                                            positions=positions,
                                            dt=dt,
                                            config=config,
                                            engine=engine,
                                            broker=broker,
                                            df=df,
                                            atr_pct=atr_pct,
                                            quote=quote,
                                            spread_pct_eval=float(spread_pct)
                                            if spread_pct is not None and spread_pct == spread_pct
                                            else None,
                                            regime_result=regime_result,
                                            entry_regime_score_int=_ers_retry,
                                            rep_sub=_rep_sub,
                                            strength_jitter_max=_strength_jitter_max,
                                            replace_if_weakest_older_than=_replace_if_weakest_older_than,
                                            max_position_age_bars=_max_pos_age_bars,
                                            allow_equal_replacement=_allow_equal_rep,
                                            replacement_threshold=_replacement_threshold,
                                            incoming_notional_usd=_rep_min_in_sz,
                                            replacement_scan_state=_rep_scan_state,
                                            user_id=_uid,
                                            data_dir=_data_dir,
                                            current_positions=current_positions,
                                            log_entry_skip=_log_entry_skip,
                                            verbose=verbose,
                                            cycle_risk_state=_cycle_risk_state,
                                            stale_quote_max_age=stale_quote_max_age,
                                            per_cycle_exit_ctx=_exit_ctx,
                                            live_risk_order_callback=_live_risk_note,
                                        )
                                        if _rot_sz and _entry_gates_route != "none":
                                            positions[:] = broker.get_positions()
                                            tracked = load_tracked(_uid, data_dir=_data_dir)
                                            normalized_positions = _normalize_broker_positions(positions)
                                            _exposure_snapshot = compute_exposures(
                                                float(account_equity),
                                                normalized_positions,
                                                SYMBOL_SECTOR,
                                                default_sector=_default_sector,
                                            )
                                            _common_gates_kw = dict(
                                                symbol=symbol,
                                                dt=dt,
                                                account_equity=account_equity,
                                                current_positions=current_positions,
                                                sector_exposure_pct=_exposure_snapshot.sector_pct,
                                                spread_pct=spread_pct,
                                                volume_atr_ratio=vol_atr_g,
                                                atr_pct=atr_pct,
                                                ohlcv_df=df,
                                                symbol_sector=SYMBOL_SECTOR,
                                                log_strategy_context=verbose,
                                                regime_size_multiplier=_trend_long_regime_mult,
                                                regime_score=_entry_regime_score,
                                                skip_spread_check=_skip_nbbo_spread_tl,
                                                regime_condition=_entry_regime_condition,
                                                gross_exposure_pct=_exposure_snapshot.gross_pct,
                                                net_exposure_pct=_exposure_snapshot.net_pct,
                                                theme_exposure_pct=_exposure_snapshot.theme_pct,
                                                strategy_winrate=0.50,
                                                is_etf=sym_u in ETF_SYMBOLS,
                                                is_inverse_etf=sym_u in INVERSE_ETFS,
                                                theme_key=theme_name,
                                                session_last_equity=_session_last_equity,
                                                cap_relax_factor=_cap_rel,
                                                entry_wave_strong_signal_count=_n_boost,
                                                dynamic_symbols=list(dynamic_set),
                                                entry_route=_entry_gates_route,
                                                dynamic_entry_momentum_score=(
                                                    _finite_float_or_none(
                                                        (_scanner_meta_entry or {}).get("score")
                                                        or (_scanner_meta_entry or {}).get("entry_alignment_score")
                                                    )
                                                    if isinstance(_scanner_meta_entry, Mapping)
                                                    else None
                                                ),
                                                dynamic_entry_breakout_continuation=bool(
                                                    _is_dynamic_candidate and getattr(_dyn_sig, "price_above_vwap", False)
                                                ),
                                            )
                                            if _entry_gates_route in (
                                                "trend_long",
                                                "momentum_breakout",
                                            ):
                                                _eo_retry = None
                                                if decision.entry_signal is not None:
                                                    _md_r = decision.entry_signal.metadata or {}
                                                    if _md_r.get("source") == "news_sentiment":
                                                        _eo_retry = decision.entry_signal
                                                    elif _md_r.get("alternate_entry") or _md_r.get(
                                                        "source"
                                                    ) in (
                                                        "breakout",
                                                        "mean_reversion",
                                                        "volatility",
                                                    ):
                                                        _eo_retry = decision.entry_signal
                                                if (
                                                    _entry_gates_route == "momentum_breakout"
                                                    and _eo_retry is None
                                                    and decision.entry_signal is None
                                                ):
                                                    _eo_retry = EntrySignal(
                                                        symbol=symbol,
                                                        side="long",
                                                        strength=0.65,
                                                        stop_pct=engine.strategy.stop_loss_pct,
                                                        take_profit_pct=engine.strategy.take_profit_pct,
                                                        time_bars_exit=engine.strategy.time_bars_exit,
                                                        metadata={
                                                            "source": "dynamic_momentum_override",
                                                            "alternate_entry": True,
                                                            "ai_catalyst_score": _ai_catalyst_score,
                                                            "ai_catalyst_summary": _ai_catalyst_summary,
                                                        },
                                                    )
                                                print(
                                                    f"DYNAMIC_OVERRIDE_RETRY sym={sym_u} "
                                                    f"route={_entry_gates_route} "
                                                    f"eo_retry={_eo_retry is not None}",
                                                    flush=True,
                                                )
                                                _retry_gates_kw = dict(_common_gates_kw)
                                                _retry_gates_kw["entry_override"] = _eo_retry
                                                decision = _run_entry_gates_dynamic_ema_bypass(
                                                    engine,
                                                    config=config,
                                                    is_dynamic_candidate=_is_dynamic_candidate,
                                                    entry_route=_entry_gates_route,
                                                    run_kwargs=_retry_gates_kw,
                                                )
                                            elif _entry_gates_route == "news_light":
                                                decision = engine.run_entry_gates(**_common_gates_kw)
                                            elif _entry_gates_route == "alternate" and _alt_match is not None:
                                                _alt_strength_r = alternate_entry_signal_strength(config)
                                                _alt_override_r = EntrySignal(
                                                    symbol=sym_u,
                                                    side="long",
                                                    strength=_alt_strength_r,
                                                    stop_pct=engine.strategy.stop_loss_pct,
                                                    take_profit_pct=engine.strategy.take_profit_pct,
                                                    time_bars_exit=engine.strategy.time_bars_exit,
                                                    metadata={
                                                        "source": _alt_match.kind,
                                                        "alternate_entry": True,
                                                    },
                                                )
                                                decision = engine.run_entry_gates(
                                                    **_common_gates_kw,
                                                    entry_override=_alt_override_r,
                                                )
                                            elif _entry_gates_route == "news_full":
                                                entry_override_cap = EntrySignal(
                                                    symbol=symbol,
                                                    side="long",
                                                    strength=float(sentiment_score),
                                                    stop_pct=engine.strategy.stop_loss_pct,
                                                    take_profit_pct=engine.strategy.take_profit_pct,
                                                    time_bars_exit=engine.strategy.time_bars_exit,
                                                    metadata={
                                                        "source": "news_sentiment",
                                                        "news_sentiment": sentiment_score,
                                                        "volume_ratio": vol_ratio,
                                                    },
                                                )
                                                decision = engine.run_entry_gates(
                                                    **_common_gates_kw,
                                                    entry_override=entry_override_cap,
                                                )
                                    _ad_cap_streak = adaptive_bump_streak(
                                        config, _ad_cap_streak, decision
                                                )
                                    # capital_allocator: only queue where entry_eval **final** matches dispatch
                                    # (``decision.allowed`` after optional rotation re-run of
                                    # :meth:`run_entry_gates` above), not the full scored universe.
                                    if _cap_alloc_enabled:
                                        _allocator_final_true_pending = bool(_eval_allowed)
                                        _allocator_final_true_handled = bool(
                                            _allocator_entry_eval_followup_emitted
                                        )

                                        def _mark_allocator_final_true_handled() -> None:
                                            nonlocal _allocator_final_true_handled
                                            if _allocator_final_true_pending:
                                                _allocator_final_true_handled = True

                                        def _record_entry_terminal_local(
                                            stage: str,
                                            reason: str,
                                            *,
                                            route: str | None = None,
                                            extra: dict[str, object] | None = None,
                                        ) -> None:
                                            _payload = {
                                                "dynamic_candidate": bool(_is_dynamic_candidate),
                                                "entry_eval_final": bool(_eval_allowed),
                                                "route": route or _route_log,
                                                "news_score": float(_news_score_effective_dyn),
                                                "event_score": float(_event_score_dyn),
                                                "catalyst_score": float(_news_trend_debug_catalyst_score or 0.0),
                                                "relative_volume": _rel_vol_e,
                                                "signal_strength_min": _alloc_min_sym,
                                                "options_enabled": bool(opts_enabled),
                                                "options_allow_buy": bool(opts_allow_buy),
                                            }
                                            if extra:
                                                _payload.update(extra)
                                            _record_entry_terminal_outcome_live(
                                                store=_sqlite_store,
                                                user_id=str(_uid),
                                                symbol=sym_u,
                                                route=route or _route_log,
                                                stage=stage,
                                                reason=reason,
                                                payload=_payload,
                                                ts=dt,
                                            )

                                        def _log_allocator_enqueue_skip(reason: str) -> None:
                                            if _eval_allowed and not _allocator_final_true_handled:
                                                _entry_allocator_skipped.add(sym_u)
                                                _log_allocator_enqueue_skip_symbol(
                                                    sym_u,
                                                    reason,
                                                    route=str(_route_log or _entry_gates_route or "n/a"),
                                                    allocator_on=bool(_cap_alloc_enabled),
                                                    final=bool(_eval_allowed),
                                                    stage="allocator_queue",
                                                )
                                                _mark_allocator_final_true_handled()

                                        if _allocator_final_true_handled:
                                            continue

                                        if _final_true_stock_candidate_can_enter_allocator(decision, df):
                                            _max_atr_rank_ca = float(
                                                engine.strategy.effective_max_atr_pct_for_entry(
                                                    _entry_regime_score
                                                )
                                            )
                                            _cr_ca, _rb_ca, _den_ca = trend_long_composite_rank(
                                                df,
                                                atr_pct=float(atr_pct)
                                                if atr_pct is not None and atr_pct == atr_pct
                                                else None,
                                                max_atr_pct=_max_atr_rank_ca,
                                                event_triggers=_event_trig_cfg,
                                                atr_period=_atr_period_tl,
                                                composite_weights=_comp_w,
                                            )
                                            _eff_ca = effective_signal_strength(
                                                float(_cr_ca) / float(_den_ca), _strength_jitter_max
                                            )
                                            _alloc_score_ca = _entry_eval_allocator_score(
                                                route=str(_route_log or _entry_gates_route or "n/a"),
                                                final=bool(_eval_allowed),
                                                trend=_te if _te is not None else trend_long_ok,
                                                pullback=_pe,
                                                momentum=_me,
                                                volatility=_ve,
                                                existing_score=_eff_ca,
                                            )
                                            _tier_ca = symbol_signal_priority_tier(sym_u, _sector_etfs)
                                            notional_ca = (
                                                (
                                                    decision.position_sizing.notional
                                                    if decision.position_sizing
                                                    else 0
                                                )
                                                or 0
                                            )
                                            notional_ca = entry_target_dollars_for_symbol(
                                                float(notional_ca),
                                                symbol=sym_u,
                                                core_symbols=core_symbols,
                                                account_equity=float(account_equity),
                                                config=config,
                                            )
                                            if _entry_quality_size_mult < 1.0:
                                                notional_ca = float(notional_ca) * float(_entry_quality_size_mult)
                                            _row_alloc = {
                                                "symbol": symbol,
                                                "sym_u": sym_u,
                                                "decision": decision,
                                                "df": df,
                                                "quote": quote,
                                                "notional": notional_ca,
                                                "user_id": str(_uid),
                                                "captured_at": dt,
                                                "entry_eval_final": True,
                                                "trend_long_ok": bool(
                                                    trend_long_ok or (_alt_match is not None)
                                                ),
                                                "alternate_entry": _alt_match.kind
                                                if _alt_match
                                                else None,
                                                "atr_pct": atr_pct,
                                                "entry_regime_score": _entry_regime_score,
                                                "tier": _tier_ca,
                                                "score": float(_alloc_score_ca),
                                                "strength_eff": _eff_ca,
                                                "composite_score": float(_cr_ca),
                                                "priority_score": row_signal_priority_score(
                                                    {"rank_breakdown": _rb_ca}
                                                ),
                                                "rank_breakdown": _rb_ca,
                                                "is_add_flow": bool(_is_add_flow),
                                                "dynamic_candidate": bool(_is_dynamic_candidate),
                                                "scanner_selected": bool(_is_dynamic_added or _is_dynamic_candidate),
                                                "is_dynamic": bool(_is_dynamic_candidate),
                                                "route": str(_route_log or _entry_gates_route or "n/a"),
                                                "source": "dynamic_universe"
                                                if _is_dynamic_candidate
                                                else "trend_long",
                                                "paper_current_price": float(_paper_current_price),
                                                "paper_session_vwap": _paper_session_vwap,
                                                "news_score": float(_news_score_effective_dyn),
                                                "event_score": float(_event_score_dyn),
                                                "catalyst_score": float(_news_trend_debug_catalyst_score or 0.0),
                                                "catalyst_age_minutes": _news_catalyst_age_minutes_dyn,
                                                "relative_volume": _rel_vol_e,
                                                "rel_volume": _rel_vol_e,
                                                "gain_pct": _gain_pct_e,
                                                "day_gain_pct": _gain_pct_e,
                                                "spread_pct": _spread_for_dme
                                                if _is_dynamic_candidate
                                                else spread_pct,
                                                "dynamic_score": float(
                                                    _dynamic_momentum_scores.get(sym_u, 0.0) or 0.0
                                                )
                                                if _is_dynamic_candidate
                                                else 0.0,
                                                "scanner_score": float(
                                                    _dynamic_momentum_scores.get(sym_u, 0.0) or 0.0
                                                )
                                                if _is_dynamic_candidate
                                                else 0.0,
                                                "signal_score": float(
                                                    _dynamic_momentum_scores.get(sym_u, 0.0) or 0.0
                                                )
                                                if _is_dynamic_candidate
                                                else float(_alloc_score_ca),
                                                "dynamic_symbol": bool(_is_dynamic_candidate),
                                                "trend_long_quality_score": None
                                                if _entry_quality_decision is None
                                                else float(_entry_quality_decision.quality_score),
                                                "entry_quality_reason": None
                                                if _entry_quality_decision is None
                                                else _entry_quality_decision.reason,
                                                "starter_size": bool(
                                                    _entry_quality_decision is not None
                                                    and _entry_quality_decision.starter
                                                ),
                                                "entry_quality_size_multiplier": float(_entry_quality_size_mult),
                                            }
                                            _row_alloc.update(_scanner_meta_payload)
                                            _row_alloc.update(_entry_quality_meta)
                                            _penalize_recent_if_needed(_row_alloc, sym_u)
                                            _dynamic_exposure_for_penalty = _dynamic_exposure_pct(
                                                positions,
                                                dynamic_symbols=dynamic_symbols,
                                                account_equity=float(account_equity),
                                            )
                                            _apply_strong_dynamic_etf_penalty(
                                                _row_alloc,
                                                sym_u,
                                                strong_dynamic_candidates_present=bool(
                                                    _strong_dynamic_persistent_map
                                                ),
                                                dynamic_exposure_pct=_dynamic_exposure_for_penalty,
                                            )
                                            if _is_dynamic_candidate:
                                                _log_allocator_dynamic_candidate(
                                                    sym_u,
                                                    reason="entry_eval_final",
                                                    strength_eff=float(_eff_ca),
                                                    source="dynamic_universe",
                                                    news_score=float(_news_score_effective_dyn),
                                                    relative_volume=_finite_float_or_none(_rel_vol_e),
                                                )
                                            if float(_eff_ca) + 1e-12 < float(_alloc_min_sym):
                                                if _is_dynamic_candidate:
                                                    _log_allocator_dynamic_skipped(
                                                        sym_u,
                                                        reason=(
                                                            "signal_strength %.3f < min %.3f"
                                                            % (
                                                                float(_eff_ca),
                                                                float(_alloc_min_sym),
                                                            )
                                                        ),
                                                        strength_eff=float(_eff_ca),
                                                        source="dynamic_universe",
                                                        news_score=float(_news_score_effective_dyn),
                                                        relative_volume=_finite_float_or_none(_rel_vol_e),
                                                    )
                                                _log_allocator_reject(
                                                    sym_u,
                                                    "signal_strength %.3f < min %.3f"
                                                    % (
                                                        float(_eff_ca),
                                                        float(_alloc_min_sym),
                                                    ),
                                                )
                                                _record_entry_terminal_local(
                                                    "skipped_with_reason",
                                                    "signal_strength_below_allocator_min",
                                                    extra={
                                                        "strength_eff": float(_eff_ca),
                                                        "signal_strength_min": float(_alloc_min_sym),
                                                    },
                                                )
                                                _log_allocator_enqueue_skip("signal_strength_below_allocator_min")
                                                continue
                                            if _is_add_flow:
                                                _es_ca = (
                                                    float(decision.entry_signal.strength)
                                                    if decision.entry_signal is not None
                                                    else None
                                                )
                                                _pv_ca = symbol_long_position_market_value_usd(
                                                    positions, sym_u
                                                )
                                                _ok_ca_add, _add_scale_ca, _rs_ca_add = (
                                                    add_on_passes_signal_and_scale(
                                                        gate_cfg=_add_on_gate_cfg,
                                                        entry_signal_strength=_es_ca,
                                                        position_market_value_usd=_pv_ca,
                                                    )
                                                )
                                                if not _ok_ca_add:
                                                    if _is_dynamic_candidate:
                                                        _log_allocator_dynamic_skipped(
                                                            sym_u,
                                                            reason=_rs_ca_add or "add-on gate",
                                                            strength_eff=float(_eff_ca),
                                                            source="dynamic_universe",
                                                            news_score=float(_news_score_effective_dyn),
                                                            relative_volume=_finite_float_or_none(_rel_vol_e),
                                                        )
                                                    _log_allocator_reject(
                                                        sym_u,
                                                        _rs_ca_add or "add-on gate",
                                                    )
                                                    _record_entry_terminal_local(
                                                        "skipped_with_reason",
                                                        _rs_ca_add or "add_on_gate",
                                                        extra={
                                                            "entry_signal_strength": _es_ca,
                                                            "add_on_scale": _add_scale_ca,
                                                        },
                                                    )
                                                    _log_entry_skip(
                                                        dt,
                                                        symbol,
                                                        _rs_ca_add or "add-on gate",
                                                        verbose=verbose,
                                                        force=False,
                                                    )
                                                    _log_allocator_enqueue_skip(_rs_ca_add or "add_on_gate")
                                                    continue
                                                if _add_scale_ca < 1.0 - 1e-9:
                                                    _row_alloc["notional"] = max(
                                                        0.0,
                                                        float(_row_alloc.get("notional") or 0.0)
                                                        * float(_add_scale_ca),
                                                    )
                                                    if _is_dynamic_candidate:
                                                        _log_allocator_dynamic_skipped(
                                                            sym_u,
                                                            reason=(
                                                                _rs_ca_add
                                                                or "add-on scaled to %.2f×"
                                                                % float(_add_scale_ca)
                                                            ),
                                                            strength_eff=float(_eff_ca),
                                                            source="dynamic_universe",
                                                            news_score=float(_news_score_effective_dyn),
                                                            relative_volume=_finite_float_or_none(_rel_vol_e),
                                                        )
                                                    _log_allocator_reject(
                                                        sym_u,
                                                        _rs_ca_add
                                                        or "add-on scaled to %.2f×"
                                                        % float(_add_scale_ca),
                                                    )
                                                try:
                                                    _inc_pct_ca = float(
                                                        _add_on_gate_cfg.get("incremental_add_pct", 0.02)
                                                        or 0.02
                                                    )
                                                except (TypeError, ValueError):
                                                    _inc_pct_ca = 0.02
                                                if _inc_pct_ca > 1.0 + 1e-9:
                                                    _inc_pct_ca = _inc_pct_ca / 100.0
                                                _inc_cap_ca = max(0.0, float(account_equity)) * max(
                                                    0.0, min(1.0, _inc_pct_ca)
                                                )
                                                if _inc_cap_ca > 0:
                                                    _row_alloc["notional"] = min(
                                                        float(_row_alloc.get("notional") or 0.0),
                                                        _inc_cap_ca,
                                                    )
                                            if _attempt_paper_option_entry_for_row(
                                                _row_alloc,
                                                lane="capital_allocator",
                                            ):
                                                _log_allocator_enqueue_skip("paper_option_entry_attempted")
                                                _record_entry_terminal_local(
                                                    "skipped_with_reason",
                                                    "paper_option_entry_attempted",
                                                    extra={"attempted_lane": "capital_allocator"},
                                                )
                                                continue
                                            if not trend_long_strength_uses_equity_allocator(
                                                strength_eff=float(_eff_ca),
                                                strong_signal_strength_min=float(
                                                    _smin_symbol
                                                ),
                                                options_enabled=opts_enabled,
                                                options_allow_new_entries=opts_allow_buy,
                                            ):
                                                log.info(
                                                    "[%s] capital_allocator: %s strength %.3f below strong %.3f — "
                                                    "queued because entry_eval final=true",
                                                    _uid,
                                                    sym_u,
                                                    float(_eff_ca),
                                                    float(_smin_symbol),
                                                )
                                            if _is_dynamic_candidate:
                                                _log_allocator_dynamic_selected(
                                                    sym_u,
                                                    reason="queued_for_allocator",
                                                    strength_eff=float(_eff_ca),
                                                    source="dynamic_universe",
                                                    news_score=float(_news_score_effective_dyn),
                                                    relative_volume=_finite_float_or_none(_rel_vol_e),
                                                )
                                            _append_capital_allocator_candidate(
                                                _cap_alloc_candidates,
                                                _row_alloc,
                                                symbol=sym_u,
                                                route=str(_route_log or _entry_gates_route or "n/a"),
                                                reason=str(_eval_reason or "ok"),
                                                score=float(_alloc_score_ca),
                                                allocator_on=bool(_cap_alloc_enabled),
                                                final=bool(_eval_allowed),
                                                stage="allocator_queue",
                                                emit_log=not _allocator_final_true_handled,
                                            )
                                            _record_entry_allocator_stage_for_rows(
                                                [_row_alloc],
                                                stage="allocator_appended",
                                                reason="queued_for_allocator",
                                                store=_sqlite_store,
                                                user_id=str(_uid),
                                                ts=dt,
                                                symbols_out=_entry_allocator_appended,
                                                payload_extra={"append_stage": "allocator_queue"},
                                            )
                                            _mark_allocator_final_true_handled()
                                        else:
                                            if decision is None:
                                                _alloc_reason = "no_decision"
                                            elif not decision.allowed:
                                                _alloc_reason = decision.reason or "entry_gates_blocked"
                                            elif not decision.order_request:
                                                _alloc_reason = "no_order_request"
                                            elif df is None or getattr(df, "empty", True):
                                                _alloc_reason = "missing_ohlcv"
                                            else:
                                                _alloc_reason = "not_queued"
                                            _log_allocator_reject(sym_u, _alloc_reason)
                                            if _eval_allowed:
                                                _log_allocator_enqueue_skip(_alloc_reason)
                                                _record_entry_terminal_local(
                                                    "skipped_with_reason",
                                                    _alloc_reason,
                                                    extra={
                                                        "decision_present": decision is not None,
                                                        "order_request_present": bool(
                                                            decision is not None
                                                            and decision.order_request
                                                        ),
                                                        "ohlcv_present": bool(
                                                            df is not None
                                                            and not getattr(df, "empty", True)
                                                        ),
                                                    },
                                                )
                                        if (
                                            _allocator_final_true_pending
                                            and not _allocator_final_true_handled
                                        ):
                                            _log_allocator_enqueue_skip("unhandled_allocator_enqueue_path")
                                            _record_entry_terminal_local(
                                                "skipped_with_reason",
                                                "unhandled_allocator_enqueue_path",
                                                extra={
                                                    "decision_present": decision is not None,
                                                    "decision_allowed": bool(
                                                        decision is not None
                                                        and getattr(decision, "allowed", False)
                                                    ),
                                                    "order_request_present": bool(
                                                        decision is not None
                                                        and getattr(decision, "order_request", None)
                                                    ),
                                                    "ohlcv_present": bool(
                                                        df is not None
                                                        and not getattr(df, "empty", True)
                                                    ),
                                                },
                                            )
                                        continue

                                    # Allocator off: signal → size → BP/rebalance → dispatch or rank queue.
                                    if decision is not None and decision.allowed and decision.order_request:
                                        _add_scale_sg_pending = 1.0
                                        if _is_add_flow:
                                            _es_add = (
                                                float(decision.entry_signal.strength)
                                                if decision.entry_signal is not None
                                                else None
                                            )
                                            _pv_add = symbol_long_position_market_value_usd(
                                                positions, sym_u
                                            )
                                            _ok_add_sg, _add_scale_sg, _rs_add_sg = add_on_passes_signal_and_scale(
                                                gate_cfg=_add_on_gate_cfg,
                                                entry_signal_strength=_es_add,
                                                position_market_value_usd=_pv_add,
                                            )
                                            if not _ok_add_sg:
                                                _log_entry_skip(
                                                    dt,
                                                    symbol,
                                                    _rs_add_sg or "add-on gate",
                                                    verbose=verbose,
                                                    force=False,
                                                )
                                                continue
                                            _add_scale_sg_pending = float(_add_scale_sg)
                                        notional = (decision.position_sizing.notional if decision.position_sizing else 0) or 0
                                        notional = entry_target_dollars_for_symbol(
                                            float(notional),
                                            symbol=sym_u,
                                            core_symbols=core_symbols,
                                            account_equity=float(account_equity),
                                            config=config,
                                        )
                                        if _add_scale_sg_pending < 1.0 - 1e-9:
                                            notional = max(
                                                0.0,
                                                float(notional) * float(_add_scale_sg_pending),
                                            )
                                            _log_entry_skip(
                                                dt,
                                                symbol,
                                                _rs_add_sg
                                                or "add-on scaled to %.2f×"
                                                % float(_add_scale_sg_pending),
                                                verbose=verbose,
                                                force=False,
                                            )
                                        if _is_add_flow:
                                            try:
                                                _inc_pct_sg = float(
                                                    _add_on_gate_cfg.get("incremental_add_pct", 0.02)
                                                    or 0.02
                                                )
                                            except (TypeError, ValueError):
                                                _inc_pct_sg = 0.02
                                            if _inc_pct_sg > 1.0 + 1e-9:
                                                _inc_pct_sg = _inc_pct_sg / 100.0
                                            _inc_cap_sg = max(0.0, float(account_equity)) * max(
                                                0.0, min(1.0, _inc_pct_sg)
                                            )
                                            if _inc_cap_sg > 0:
                                                notional = min(float(notional), _inc_cap_sg)
                                        _max_atr_rank_r = float(
                                            engine.strategy.effective_max_atr_pct_for_entry(
                                                _entry_regime_score
                                            )
                                        )
                                        _cr_r, _rb_r, _den_r = trend_long_composite_rank(
                                            df,
                                            atr_pct=float(atr_pct)
                                            if atr_pct is not None and atr_pct == atr_pct
                                            else None,
                                            max_atr_pct=_max_atr_rank_r,
                                            event_triggers=_event_trig_cfg,
                                            atr_period=_atr_period_tl,
                                            composite_weights=_comp_w,
                                        )
                                        _eff_r = effective_signal_strength(
                                            float(_cr_r) / float(_den_r), _strength_jitter_max
                                        )
                                        buying_power = scaled_buying_power_for_lane(
                                            buying_power=broker.get_buying_power(),
                                            equity=float(account_equity),
                                            config=config,
                                            regime_score=_reg_score_bp,
                                            regime_condition=_reg_cond_bp,
                                            full_invest=bool(_entry_full_invest_flag),
                                            lane="stocks",
                                        )
                                        while notional > buying_power:
                                            if not _try_rebalance_free_capital_trim(
                                                sym_u,
                                                incoming_strength=_eff_r,
                                                strength_cohort=None,
                                            ):
                                                break
                                            buying_power = scaled_buying_power_for_lane(
                                                buying_power=broker.get_buying_power(),
                                                equity=float(account_equity),
                                                config=config,
                                                regime_score=_reg_score_bp,
                                                regime_condition=_reg_cond_bp,
                                                full_invest=bool(_entry_full_invest_flag),
                                                lane="stocks",
                                            )
                                        if (
                                            notional > buying_power
                                            and bool(_rfc_cfg.get("rotate_full_weakest_when_stronger"))
                                        ):
                                            if _try_rotate_full_weakest_for_bp(
                                                sym_u, _eff_r, strength_cohort=None
                                            ):
                                                buying_power = scaled_buying_power_for_lane(
                                                    buying_power=broker.get_buying_power(),
                                                    equity=float(account_equity),
                                                    config=config,
                                                    regime_score=_reg_score_bp,
                                                    regime_condition=_reg_cond_bp,
                                                    full_invest=bool(_entry_full_invest_flag),
                                                    lane="stocks",
                                                )
                                        if notional > buying_power:
                                            _log_entry_skip(
                                                dt,
                                                symbol,
                                                "insufficient buying power for sized order (need $%.0f, have $%.0f after min cash reserve)"
                                                % (notional, buying_power),
                                                verbose=verbose,
                                                force=False,
                                            )
                                            continue
                                        _tier_r = symbol_signal_priority_tier(sym_u, _sector_etfs)
                                        _at_cap_rep_rank = (
                                            not bool(_alloc_cfg.get("rank_by_signal_strength"))
                                            and _max_port_positions < 10**9
                                            and len(_eligible_active) >= _max_port_positions
                                        )
                                        row_tl = {
                                            "symbol": symbol,
                                            "sym_u": sym_u,
                                            "decision": decision,
                                            "df": df,
                                            "quote": quote,
                                            "notional": notional,
                                            "trend_long_ok": bool(trend_long_ok or (_alt_match is not None)),
                                            "alternate_entry": _alt_match.kind if _alt_match else None,
                                            "atr_pct": atr_pct,
                                            "entry_regime_score": _entry_regime_score,
                                            "tier": _tier_r,
                                            "strength_eff": _eff_r,
                                            "composite_score": float(_cr_r),
                                            "priority_score": row_signal_priority_score(
                                                {"rank_breakdown": _rb_r}
                                            ),
                                            "rank_breakdown": _rb_r,
                                            "pyramid_skip_symbol_cap": bool(
                                                _pyramid_relax_symbol_cap
                                            ),
                                            "is_add_flow": bool(_is_add_flow),
                                            "dynamic_candidate": bool(_is_dynamic_candidate),
                                            "scanner_selected": bool(_is_dynamic_added or _is_dynamic_candidate),
                                            "source": "dynamic_universe"
                                            if _is_dynamic_candidate
                                            else "trend_long",
                                            "paper_current_price": float(_paper_current_price),
                                            "paper_session_vwap": _paper_session_vwap,
                                            "news_score": float(_news_score_effective_dyn),
                                            "event_score": float(_event_score_dyn),
                                            "relative_volume": _rel_vol_e,
                                        }
                                        _penalize_recent_if_needed(row_tl, sym_u)
                                        _force_rank_low_regime_new_entry = (
                                            not _is_add_flow
                                            and low_regime_stock_entry_top_n(
                                                config,
                                                regime_score=_entry_regime_score,
                                            )
                                            > 0
                                        )
                                        if (_signal_ranking_active or _force_rank_low_regime_new_entry) and not _at_cap_rep_rank:
                                            if _is_add_flow:
                                                _es_rq = (
                                                    float(decision.entry_signal.strength)
                                                    if decision.entry_signal is not None
                                                    else None
                                                )
                                                _pv_rq = symbol_long_position_market_value_usd(
                                                    positions, sym_u
                                                )
                                                _ok_rq_add, _add_scale_rq, _rs_rq_add = (
                                                    add_on_passes_signal_and_scale(
                                                        gate_cfg=_add_on_gate_cfg,
                                                        entry_signal_strength=_es_rq,
                                                        position_market_value_usd=_pv_rq,
                                                    )
                                                )
                                                if not _ok_rq_add:
                                                    _log_entry_skip(
                                                        dt,
                                                        symbol,
                                                        _rs_rq_add or "add-on gate",
                                                        verbose=verbose,
                                                        force=False,
                                                    )
                                                    continue
                                                if _add_scale_rq < 1.0 - 1e-9:
                                                    row_tl["notional"] = max(
                                                        0.0,
                                                        float(row_tl.get("notional") or 0.0)
                                                        * float(_add_scale_rq),
                                                    )
                                                    _log_entry_skip(
                                                        dt,
                                                        symbol,
                                                        _rs_rq_add
                                                        or "add-on scaled to %.2f×"
                                                        % float(_add_scale_rq),
                                                        verbose=verbose,
                                                        force=False,
                                                    )
                                                try:
                                                    _inc_pct_rq = float(
                                                        _add_on_gate_cfg.get("incremental_add_pct", 0.02)
                                                        or 0.02
                                                    )
                                                except (TypeError, ValueError):
                                                    _inc_pct_rq = 0.02
                                                if _inc_pct_rq > 1.0 + 1e-9:
                                                    _inc_pct_rq = _inc_pct_rq / 100.0
                                                _inc_cap_rq = max(0.0, float(account_equity)) * max(
                                                    0.0, min(1.0, _inc_pct_rq)
                                                )
                                                if _inc_cap_rq > 0:
                                                    row_tl["notional"] = min(
                                                        float(row_tl.get("notional") or 0.0),
                                                        _inc_cap_rq,
                                                    )
                                            _ranked_entry_queue.append(row_tl)
                                            _after_strong_entry_candidate_for_full_invest(row_tl)
                                            continue

                                        _after_strong_entry_candidate_for_full_invest(row_tl)
                                        _trend_long_dispatch_or_queue(row_tl)
                                    else:
                                        if decision is not None:
                                            _log_entry_skip(
                                                dt,
                                                symbol,
                                                decision.reason or "no entry signal",
                                                verbose=verbose,
                                                force=False,
                                            )
                                except Exception as e:
                                    _entry_eval_exception_symbols.add(
                                        str(symbol or "").strip().upper()
                                    )
                                    _exception_sym_u = str(symbol or "").strip().upper()
                                    _runtime_error_reason = _entry_evaluation_runtime_error_reason(e)
                                    log.error(
                                        "ENTRY_EVALUATION_RUNTIME_ERROR symbol=%s route=%s error_type=%s message=%s",
                                        _exception_sym_u,
                                        str(locals().get("_route_log") or locals().get("_entry_gates_route") or "n/a"),
                                        type(e).__name__,
                                        str(e)[:200],
                                        exc_info=True,
                                    )
                                    if _exception_sym_u in _scanner_selected_dynamic_set:
                                        _dynamic_entry_eval_dropped_symbols.add(_exception_sym_u)
                                        _log_dynamic_entry_eval_dropped(
                                            _exception_sym_u,
                                            reason=_runtime_error_reason,
                                        )
                                    log.error(
                                        "[%s] entry signal scan: %s — %s: %s",
                                        _uid,
                                        symbol,
                                        type(e).__name__,
                                        str(e)[:200],
                                        exc_info=True,
                                        #exc_info=log.isEnabledFor(logging.DEBUG),
                                    )
                                    _log_entry_skip(
                                        dt,
                                        symbol,
                                        _runtime_error_reason,
                                        verbose=verbose,
                                        force=False,
                                    )
                                    continue

                            _finalize_dynamic_entry_eval_audit(
                                enqueued_symbols=_dynamic_entry_enqueued_symbols,
                                started_symbols=_dynamic_entry_eval_started_symbols,
                                dropped_symbols=_dynamic_entry_eval_dropped_symbols,
                                reason="not_processed_after_entry_loop",
                            )

                            _log_live_signal_scan_end(
                                user_id=_uid,
                                pass_index=_rfc_entry_pass,
                                checked_count=_live_signal_scan_checked_count,
                                rows=_cap_alloc_candidates,
                                allocator_on=bool(_cap_alloc_enabled),
                            )

                        # Post-signal: ranked queue flush, then allocator plan + execute. Per-symbol
                        # failures in the loop above are logged; allocator path uses
                        # :func:`run_post_scan_capital_allocator` (fallback: keep prior cash) and
                        # per-order retry/partial in :func:`execute_capital_allocator_pass`.
                        if _cap_alloc_enabled:
                            _log_allocator_queue_summary(_cap_alloc_candidates)

                        _trim_ranked_top_cb: Callable[[str], bool] | None = None
                        if _ranked_realloc_enabled:

                            def _trim_weakest_for_blocked_top_ranked(incoming_sym: str) -> bool:
                                nonlocal tracked
                                positions[:] = broker.get_positions()
                                tracked = load_tracked(_uid, data_dir=_data_dir)
                                return execute_cap_pressure_partial_trim(
                                    incoming_sym_upper=str(incoming_sym).strip().upper(),
                                    eligible_active=_eligible_active,
                                    tracked=tracked,
                                    positions=positions,
                                    dt=dt,
                                    trim_frac=_ranked_realloc_trim_frac,
                                    max_symbols=1,
                                    broker=broker,
                                    engine=engine,
                                    rep_sub=_rep_sub,
                                    user_id=_uid,
                                    data_dir=_data_dir,
                                    log_entry_skip=_log_entry_skip,
                                    verbose=verbose,
                                    replacement_scan_state=_rep_scan_state,
                                    cycle_risk_state=_cycle_risk_state,
                                    per_cycle_exit_ctx=_exit_ctx,
                                    live_risk_order_callback=_live_risk_note,
                                    stale_quote_max_age=stale_quote_max_age,
                                )

                            _trim_ranked_top_cb = _trim_weakest_for_blocked_top_ranked

                        if (
                            not _portfolio_mode_reduce_only
                            and not _cap_alloc_enabled
                            and _ranked_entry_queue
                        ):
                            _ranked_entry_queue = _restrict_low_regime_new_stock_entries(
                                _ranked_entry_queue,
                                config=config,
                                sector_etfs=_sector_etfs,
                                ranking_mode=_signal_ranking_mode,
                                log_drop=lambda sym, why: _log_entry_skip(
                                    dt,
                                    sym,
                                    why,
                                    verbose=verbose,
                                    force=False,
                                ),
                            )
                            flush_ranked_trend_long_entry_queue(
                                _ranked_entry_queue,
                                max_take=_max_ranked_signals,
                                sector_etfs=_sector_etfs,
                                ranking_mode=_signal_ranking_mode,
                                log_entry_skip=_log_entry_skip,
                                dt=dt,
                                symbol_for_skip="PORTFOLIO",
                                verbose=verbose,
                                dispatch_row=_trend_long_dispatch_or_queue,
                                winner_allocation_enabled=bool(_w_win_en),
                                winner_top_n=int(_w_win_n),
                                winner_size_multiplier=float(_w_win_m),
                                trim_weakest_for_blocked_top=_trim_ranked_top_cb,
                                config=config,
                            )

                        if not _portfolio_mode_reduce_only and _cap_alloc_enabled:
                            _core_rebuild_cooldown_symbols = []
                            for _core_cd_sym in core_symbols:
                                _core_cd_u = str(_core_cd_sym or "").strip().upper()
                                if not _core_cd_u:
                                    continue
                                try:
                                    _core_cd_min = effective_per_symbol_buy_cooldown_min(
                                        _entries_cd,
                                        _core_cd_u,
                                    )
                                except Exception:
                                    _core_cd_min = 0.0
                                if _core_cd_min > 0 and last_entry_within(
                                    _core_cd_u,
                                    _core_cd_min,
                                    tracked=tracked,
                                    now_dt=dt,
                                ):
                                    _core_rebuild_cooldown_symbols.append(_core_cd_u)
                            if not _allow_core_rebuild_buys(config):
                                for _core_disabled_sym in core_symbols:
                                    _core_disabled_u = str(_core_disabled_sym or "").strip().upper()
                                    if _core_disabled_u:
                                        log.info("CORE_REBUILD_DISABLED symbol=%s", _core_disabled_u)
                                _core_rebuild_rows = []
                            else:
                                _core_rebuild_rows = build_core_rebuild_candidates(
                                    config=config,
                                    core_symbols=core_symbols,
                                    dynamic_symbols=dynamic_symbol_set,
                                    existing_candidates=_cap_alloc_candidates,
                                    positions=positions,
                                    equity=float(account_equity),
                                    cash=float(available_cash),
                                    broker=broker,
                                    open_order_symbols=open_order_symbols,
                                    cooldown_symbols=_core_rebuild_cooldown_symbols,
                                    max_positions=_max_port_positions,
                                    regime_score=_reg_score_bp,
                                    regime_condition=_reg_cond_bp,
                                    spread_cap_fn=engine.market_quality._max_spread_for_symbol,
                                    user_id=str(_uid),
                                    data_dir=_data_dir,
                                    now=dt,
                                    entry_eval_final_symbols=[
                                        str(
                                            row.get("sym_u") or row.get("symbol") or ""
                                        ).strip().upper()
                                        for row in _cap_alloc_candidates
                                        if isinstance(row, dict)
                                        and bool(row.get("entry_eval_final"))
                                    ],
                                    entry_eval_exception_symbols=_entry_eval_exception_symbols,
                                )
                            if _core_rebuild_rows:
                                _cap_alloc_candidates.extend(_core_rebuild_rows)

                        if _cap_alloc_enabled:
                            _log_allocator_queue_summary(_cap_alloc_candidates)
                            _log_allocator_queue_state(
                                "before_allocator_drain",
                                _cap_alloc_candidates,
                                allocator_on=_cap_alloc_enabled,
                            )

                        _allocator_drain_reason = "not_evaluated"
                        _log_allocator_drain_entry(
                            _cap_alloc_candidates,
                            allocator_on=bool(_cap_alloc_enabled),
                        )
                        if (
                            not _portfolio_mode_reduce_only
                            and _cap_alloc_enabled
                            and _cap_alloc_candidates
                            and not _allocator_skip_due_cooldown(str(_uid))
                        ):
                            _pre_low_regime_rows_by_symbol = {
                                str(row.get("sym_u") or row.get("symbol") or "").strip().upper(): row
                                for row in _cap_alloc_candidates
                                if isinstance(row, dict)
                                and str(row.get("sym_u") or row.get("symbol") or "").strip()
                            }

                            def _record_low_regime_allocator_drop(sym: str, why: str) -> None:
                                _drop_sym = str(sym).strip().upper()
                                _drop_row = _pre_low_regime_rows_by_symbol.get(_drop_sym, {})
                                if _drop_sym:
                                    _entry_allocator_skipped.add(_drop_sym)
                                if bool(_drop_row.get("entry_eval_final")):
                                    _log_allocator_enqueue_skip_symbol(_drop_sym, why)
                                if _drop_sym in dynamic_symbol_set:
                                    _log_allocator_dynamic_skipped(
                                        _drop_sym,
                                        reason=why,
                                        source="dynamic_universe",
                                    )
                                else:
                                    _log_allocator_reject(_drop_sym, why)
                                _record_entry_terminal_outcome_live(
                                    store=_sqlite_store,
                                    user_id=str(_uid),
                                    symbol=_drop_sym,
                                    route=str(
                                        _drop_row.get("route")
                                        or _drop_row.get("source")
                                        or "allocator"
                                    ),
                                    stage="skipped_with_reason",
                                    reason=why,
                                    payload={
                                        "dynamic_candidate": _drop_sym in dynamic_symbol_set,
                                        "entry_eval_final": bool(
                                            _drop_row.get("entry_eval_final")
                                        ),
                                        "profile_rule": "low_regime_new_stock_entry_top_n",
                                        "ranking_mode": _signal_ranking_mode,
                                        "entry_regime_score": _drop_row.get("entry_regime_score"),
                                        "strength_eff": _drop_row.get("strength_eff"),
                                    },
                                    ts=dt,
                                )

                            _pre_filter_allocator_rows = list(_cap_alloc_candidates)
                            _cap_alloc_candidates = _restrict_low_regime_new_stock_entries(
                                _cap_alloc_candidates,
                                config=config,
                                sector_etfs=_sector_etfs,
                                ranking_mode=_signal_ranking_mode,
                                log_drop=_record_low_regime_allocator_drop,
                            )
                            _log_allocator_queue_summary(_cap_alloc_candidates)
                            _allocator_all_candidates_filtered = not _cap_alloc_candidates
                            if not _cap_alloc_candidates:
                                _log_allocator_drain_skipped(
                                    "all_candidates_filtered",
                                    _pre_filter_allocator_rows,
                                )
                                _log_allocator_pass_skip(
                                    "all_candidates_filtered",
                                    _pre_filter_allocator_rows,
                                )
                                _log_allocator_skip_for_rows(
                                    "all_candidates_filtered",
                                    _pre_filter_allocator_rows,
                                )
                            entry_candidates = list(_cap_alloc_candidates)
                            for _cand in entry_candidates:
                                _cand_sym = str(_cand.get("sym_u") or _cand.get("symbol") or "").strip().upper()
                                if _cand_sym in dynamic_symbol_set:
                                    _log_allocator_dynamic_selected(
                                        _cand_sym,
                                        reason="post_filter_allocator_batch",
                                        strength_eff=float(_cand.get("strength_eff") or 0.0),
                                        source=str(_cand.get("source") or "dynamic_universe"),
                                        news_score=float(_cand.get("news_score") or 0.0),
                                        relative_volume=float(_cand.get("relative_volume") or 0.0),
                                    )
                            log.info(
                                "ENTRY_CANDIDATES count=%s symbols=%s",
                                len(entry_candidates),
                                [
                                    (
                                        c.symbol
                                        if hasattr(c, "symbol")
                                        else (
                                            c.get("symbol")
                                            if isinstance(c, dict)
                                            else str(c)
                                        )
                                    )
                                    for c in entry_candidates[:20]
                                ],
                            )
                            _record_entry_allocator_stage_for_rows(
                                _cap_alloc_candidates,
                                stage="allocator_input",
                                reason="allocator_pass_start",
                                store=_sqlite_store,
                                user_id=str(_uid),
                                ts=dt,
                                symbols_out=_entry_allocator_input,
                                payload_extra={"allocator_drain_reason": "allocator_pass"},
                            )
                            _allocator_bp_locked = scaled_buying_power_for_lane(
                                buying_power=broker.get_buying_power(),
                                equity=float(account_equity),
                                config=config,
                                regime_score=_reg_score_bp,
                                regime_condition=_reg_cond_bp,
                                full_invest=bool(_entry_full_invest_flag),
                                lane="stocks",
                            )
                            available_cash = _run_live_capital_allocator_pass(
                                _cap_alloc_candidates,
                                broker=broker,
                                engine=engine,
                                config=config,
                                dt=dt,
                                positions=positions,
                                tracked=tracked,
                                current_positions=current_positions,
                                eligible_active=_eligible_active,
                                account_equity=account_equity,
                                available_cash=available_cash,
                                locked_buying_power=_allocator_bp_locked,
                                ca_cfg=_ca_cfg,
                                user_id=_uid,
                                data_dir=_data_dir,
                                stale_quote_max_age=stale_quote_max_age,
                                strength_jitter_max=_strength_jitter_max,
                                et_date_iso=_et_date_iso,
                                cycle_risk_state=_cycle_risk_state,
                                verbose=verbose,
                                exit_context=_exit_ctx,
                                reg_score_bp=_reg_score_bp,
                                reg_cond_bp=_reg_cond_bp,
                                entry_full_invest_flag=_entry_full_invest_flag,
                                gross_exposure_pct=float(
                                    _exposure_snapshot.gross_pct
                                ),
                                entry_wave_strong_signal_count=int(
                                    _entry_wave_strong_ct
                                ),
                                symbol_sector=SYMBOL_SECTOR,
                                theme_map=THEME_MAP,
                            )
                            # Optional second pass when ``capital_allocator.single_pass_per_cycle`` is false.
                            available_cash = run_post_sell_reallocation(
                                _exit_ctx.had_equity_sell_this_pass(),
                                available_cash,
                                _cap_alloc_candidates,
                                broker=broker,
                                engine=engine,
                                config=config,
                                dt=dt,
                                positions=positions,
                                tracked=tracked,
                                current_positions=current_positions,
                                eligible_active=_eligible_active,
                                account_equity=account_equity,
                                ca_cfg=_ca_cfg,
                                user_id=_uid,
                                data_dir=_data_dir,
                                stale_quote_max_age=stale_quote_max_age,
                                strength_jitter_max=_strength_jitter_max,
                                et_date_iso=_et_date_iso,
                                cycle_risk_state=_cycle_risk_state,
                                verbose=verbose,
                                exit_context=_exit_ctx,
                                reg_score_bp=_reg_score_bp,
                                reg_cond_bp=_reg_cond_bp,
                                entry_full_invest_flag=_entry_full_invest_flag,
                                gross_exposure_pct=float(_exposure_snapshot.gross_pct),
                                entry_wave_strong_signal_count=int(
                                    _entry_wave_strong_ct
                                ),
                                symbol_sector=SYMBOL_SECTOR,
                                theme_map=THEME_MAP,
                            )
                            _cap_alloc_candidates = []
                            _allocator_drain_reason = (
                                "all_candidates_filtered"
                                if _allocator_all_candidates_filtered
                                else "allocator_pass"
                            )
                        elif (
                            not _portfolio_mode_reduce_only
                            and _cap_alloc_enabled
                            and _cap_alloc_candidates
                            and _allocator_skip_due_cooldown(str(_uid))
                        ):
                            _log_allocator_drain_skipped(
                                "post_bulk_trim_one_cycle_cooldown",
                                _cap_alloc_candidates,
                            )
                            for _cooldown_cand in _cap_alloc_candidates:
                                if not isinstance(_cooldown_cand, dict):
                                    continue
                                _cooldown_sym = str(
                                    _cooldown_cand.get("sym_u")
                                    or _cooldown_cand.get("symbol")
                                    or ""
                                ).strip().upper()
                                if not _cooldown_sym:
                                    continue
                                _entry_allocator_skipped.add(_cooldown_sym)
                                if bool(_cooldown_cand.get("entry_eval_final")):
                                    _log_allocator_enqueue_skip_symbol(
                                        _cooldown_sym,
                                        "post_bulk_trim_one_cycle_cooldown",
                                    )
                                _record_entry_terminal_outcome_live(
                                    store=_sqlite_store,
                                    user_id=str(_uid),
                                    symbol=_cooldown_sym,
                                    route=str(
                                        _cooldown_cand.get("route")
                                        or _cooldown_cand.get("source")
                                        or "allocator"
                                    ),
                                    stage="skipped_with_reason",
                                    reason="post_bulk_trim_one_cycle_cooldown",
                                    payload={
                                        "dynamic_candidate": _cooldown_sym in dynamic_symbol_set,
                                        "entry_eval_final": bool(
                                            _cooldown_cand.get("entry_eval_final")
                                        ),
                                        "cooldown_gate": "post_bulk_trim_1_cycle",
                                    },
                                    ts=dt,
                                )
                            _log_allocator_pass_skip(
                                "post_bulk_trim_one_cycle_cooldown",
                                _cap_alloc_candidates,
                            )
                            _log_allocator_skip_for_rows(
                                "post_bulk_trim_one_cycle_cooldown",
                                _cap_alloc_candidates,
                            )
                            _cap_alloc_candidates = []
                            _allocator_drain_reason = "post_bulk_trim_one_cycle_cooldown"
                            log.info(
                                "[%s] capital_allocator: skipped — post-bulk-trim 1-cycle cooldown",
                                str(_uid),
                            )
                        elif (
                            _portfolio_mode_reduce_only
                            and _cap_alloc_enabled
                            and _cap_alloc_candidates
                        ):
                            _log_allocator_drain_skipped(
                                "portfolio_reduce_only",
                                _cap_alloc_candidates,
                            )
                            _log_allocator_pass_skip(
                                "portfolio_reduce_only",
                                _cap_alloc_candidates,
                            )
                            _log_allocator_skip_for_rows(
                                "portfolio_reduce_only",
                                _cap_alloc_candidates,
                            )
                            for _reduce_only_cand in _cap_alloc_candidates:
                                if not isinstance(_reduce_only_cand, dict):
                                    continue
                                _reduce_only_sym = str(
                                    _reduce_only_cand.get("sym_u")
                                    or _reduce_only_cand.get("symbol")
                                    or ""
                                ).strip().upper()
                                if not _reduce_only_sym:
                                    continue
                                _entry_allocator_skipped.add(_reduce_only_sym)
                                if bool(_reduce_only_cand.get("entry_eval_final")):
                                    _log_allocator_enqueue_skip_symbol(
                                        _reduce_only_sym,
                                        "portfolio_reduce_only",
                                    )
                                _record_entry_terminal_outcome_live(
                                    store=_sqlite_store,
                                    user_id=str(_uid),
                                    symbol=_reduce_only_sym,
                                    route=str(
                                        _reduce_only_cand.get("route")
                                        or _reduce_only_cand.get("source")
                                        or "allocator"
                                    ),
                                    stage="skipped_with_reason",
                                    reason="portfolio_reduce_only",
                                    payload={
                                        "dynamic_candidate": _reduce_only_sym in dynamic_symbol_set,
                                        "entry_eval_final": bool(
                                            _reduce_only_cand.get("entry_eval_final")
                                        ),
                                    },
                                    ts=dt,
                                )
                            _cap_alloc_candidates = []
                            _allocator_drain_reason = "portfolio_reduce_only"
                        elif _cap_alloc_enabled:
                            if _portfolio_mode_reduce_only:
                                _allocator_skip_reason = "portfolio_reduce_only"
                            elif not _cap_alloc_candidates:
                                _allocator_skip_reason = "no_candidates"
                            elif _allocator_skip_due_cooldown(str(_uid)):
                                _allocator_skip_reason = "post_bulk_trim_one_cycle_cooldown"
                            else:
                                _allocator_skip_reason = "gate_conditions_not_met"
                            _log_allocator_drain_skipped(
                                _allocator_skip_reason,
                                _cap_alloc_candidates,
                            )
                            _log_allocator_pass_skip(
                                _allocator_skip_reason,
                                _cap_alloc_candidates,
                            )
                            if _cap_alloc_candidates:
                                _log_allocator_skip_for_rows(
                                    _allocator_skip_reason,
                                    _cap_alloc_candidates,
                                )
                                _record_entry_allocator_stage_for_rows(
                                    _cap_alloc_candidates,
                                    stage="skipped_with_reason",
                                    reason=_allocator_skip_reason,
                                    store=_sqlite_store,
                                    user_id=str(_uid),
                                    ts=dt,
                                    symbols_out=_entry_allocator_skipped,
                                    payload_extra={"allocator_drain_reason": _allocator_skip_reason},
                                )
                                _cap_alloc_candidates = []
                            _allocator_drain_reason = _allocator_skip_reason
                        else:
                            _allocator_skip_reason = "allocator_off"
                            _log_allocator_drain_skipped(
                                _allocator_skip_reason,
                                _cap_alloc_candidates,
                            )
                            if _cap_alloc_candidates:
                                _log_allocator_skip_for_rows(
                                    _allocator_skip_reason,
                                    _cap_alloc_candidates,
                                )
                                _record_entry_allocator_stage_for_rows(
                                    _cap_alloc_candidates,
                                    stage="skipped_with_reason",
                                    reason=_allocator_skip_reason,
                                    store=_sqlite_store,
                                    user_id=str(_uid),
                                    ts=dt,
                                    symbols_out=_entry_allocator_skipped,
                                    payload_extra={"allocator_drain_reason": _allocator_skip_reason},
                                )
                                _cap_alloc_candidates = []
                            _allocator_drain_reason = _allocator_skip_reason

                        _log_allocator_drain_exit(
                            _cap_alloc_candidates,
                            reason=_allocator_drain_reason,
                        )
                        _log_allocator_queue_state(
                            "after_drain",
                            _cap_alloc_candidates,
                            allocator_on=_cap_alloc_enabled,
                        )
                        if _cap_alloc_enabled:
                            _entry_allocator_missing = _log_entry_allocator_reconcile(
                                final_true=_entry_allocator_final_true,
                                appended=_entry_allocator_appended,
                                allocator_input=_entry_allocator_input,
                                submitted=_entry_allocator_submitted,
                                skipped=_entry_allocator_skipped,
                            )
                            for _missing_sym in sorted(_entry_allocator_missing):
                                _missing_row = _entry_allocator_final_true.get(_missing_sym) or {}
                                _record_entry_terminal_outcome_live(
                                    store=_sqlite_store,
                                    user_id=str(_uid),
                                    symbol=_missing_sym,
                                    route=str(_missing_row.get("route") or "n/a"),
                                    stage="skipped_with_reason",
                                    reason="allocator_handoff_missing",
                                    payload={
                                        "entry_eval_final": True,
                                        "dynamic_candidate": bool(
                                            _missing_row.get("dynamic_candidate")
                                        ),
                                    },
                                    ts=dt,
                                )
                        _log_allocator_drain_fatal(
                            "pending_after_allocator_drain",
                            _cap_alloc_candidates,
                            allocator_on=bool(_cap_alloc_enabled),
                            stage="after_drain",
                        )
                        if _cap_alloc_enabled:
                            _log_allocator_queue_state(
                                "before_sleep",
                                _cap_alloc_candidates,
                                allocator_on=_cap_alloc_enabled,
                            )
                            _log_allocator_drain_fatal(
                                "pending_before_sleep",
                                _cap_alloc_candidates,
                                allocator_on=True,
                                stage="before_sleep",
                            )

                _finalize_dynamic_entry_eval_audit(
                    enqueued_symbols=_dynamic_entry_enqueued_symbols,
                    started_symbols=_dynamic_entry_eval_started_symbols,
                    dropped_symbols=_dynamic_entry_eval_dropped_symbols,
                    reason="not_processed_after_entry_loop",
                )
                log.info(
                    "ENTRY_DECISION_SUMMARY options_attempted=%d options_selected=%d options_ordered=%d stock_fallback=%d blocked_cooldown=%d blocked_vwap=%d blocked_option_liquidity=%d",
                    int(_entry_decision_counts.get("options_attempted", 0)),
                    int(_entry_decision_counts.get("options_selected", 0)),
                    int(_entry_decision_counts.get("options_ordered", 0)),
                    int(_entry_decision_counts.get("stock_fallback", 0)),
                    int(_entry_decision_counts.get("blocked_cooldown", 0)),
                    int(_entry_decision_counts.get("blocked_vwap", 0)),
                    int(_entry_decision_counts.get("blocked_option_liquidity", 0)),
                )
                if locals().get("do_any_entry"):
                    try:
                        record_runtime_event(
                            _data_dir or (PROJECT_ROOT / "data"),
                            user_id=str(_uid),
                            event="ENTRY_CYCLE_COMPLETED",
                            timestamp=dt,
                            project_root=PROJECT_ROOT,
                            configured_mode=str((config.get("trading_control") or {}).get("mode") or "missing"),
                            effective_mode=str((config.get("trading_control") or {}).get("mode") or "missing"),
                            broker_submission_allowed=False,
                            details={
                                "enqueued_symbols": len(_dynamic_entry_enqueued_symbols),
                                "started_symbols": len(_dynamic_entry_eval_started_symbols),
                                "dropped_symbols": len(_dynamic_entry_eval_dropped_symbols),
                                "options_attempted": int(_entry_decision_counts.get("options_attempted", 0)),
                                "options_ordered": int(_entry_decision_counts.get("options_ordered", 0)),
                            },
                        )
                    except Exception:
                        log.debug("runtime progress entry completion write failed user=%s", _uid, exc_info=True)
                emit_options_cycle_summary()

              except Exception as _user_exc:
                try:
                    record_runtime_event(
                        locals().get("_data_dir") or (PROJECT_ROOT / "data"),
                        user_id=str(locals().get("_uid", "unknown")),
                        event="ACCOUNT_FETCH_FAILURE" if "account_equity" not in locals() else "ENTRY_CYCLE_FAILED",
                        timestamp=dt,
                        project_root=PROJECT_ROOT,
                        details={"error_type": type(_user_exc).__name__, "error": str(_user_exc)[:200]},
                    )
                except Exception:
                    pass
                try:
                    _finalize_dynamic_entry_eval_audit(
                        enqueued_symbols=locals().get("_dynamic_entry_enqueued_symbols", set()),
                        started_symbols=locals().get("_dynamic_entry_eval_started_symbols", set()),
                        dropped_symbols=locals().get("_dynamic_entry_eval_dropped_symbols", set()),
                        reason="not_processed_after_entry_loop",
                    )
                except Exception:
                    pass
                try:
                    _exc_allocator_on = bool(locals().get("_cap_alloc_enabled", False))
                    _exc_allocator_rows = locals().get("_cap_alloc_candidates", [])
                    if _exc_allocator_on:
                        _log_allocator_queue_summary(_exc_allocator_rows)
                        _log_allocator_queue_state(
                            "user_trading_pass_exception",
                            _exc_allocator_rows,
                            allocator_on=_exc_allocator_on,
                        )
                        _log_allocator_pass_skip(
                            "user_trading_pass_exception",
                            _exc_allocator_rows,
                        )
                        _log_allocator_drain_skipped(
                            "user_trading_pass_exception",
                            _exc_allocator_rows,
                        )
                        _log_allocator_skip_for_rows(
                            "user_trading_pass_exception",
                            _exc_allocator_rows,
                        )
                        _log_allocator_drain_exit(
                            _exc_allocator_rows,
                            reason="user_trading_pass_exception",
                        )
                        _log_allocator_drain_fatal(
                            "user_trading_pass_exception_pending",
                            _exc_allocator_rows,
                            allocator_on=_exc_allocator_on,
                            stage="user_trading_pass_exception",
                        )
                except Exception:
                    pass
                log.exception(
                    "[%s] user trading pass failed: %s: %s",
                    _uid,
                    type(_user_exc).__name__,
                    str(_user_exc)[:200],
                )
                print(dt.strftime("%H:%M ET"), "[%s] ERROR: %s: %s — skipping to next user" % (
                    _uid, type(_user_exc).__name__, str(_user_exc)[:120]))
                _em = str(_user_exc).lower()
                if "unauthorized" in _em or "401" in _em:
                    print(
                        "      Hint: use ALPACA_LIVE_* for live and APCA_* for paper; "
                        "they are different keys in the Alpaca dashboard.",
                        flush=True,
                    )
                continue
              finally:
                try:
                    _finalize_dynamic_entry_eval_audit(
                        enqueued_symbols=locals().get("_dynamic_entry_enqueued_symbols", set()),
                        started_symbols=locals().get("_dynamic_entry_eval_started_symbols", set()),
                        dropped_symbols=locals().get("_dynamic_entry_eval_dropped_symbols", set()),
                        reason="not_processed_after_entry_loop",
                    )
                except Exception:
                    pass
            # end for _uctx in user_contexts

            _hb_interval = heartbeat_interval_seconds()
            if _hb_interval is not None and _heartbeat_rows:
                _now_mono_hb = time.monotonic()
                if _last_heartbeat_monotonic is None or (
                    _now_mono_hb - _last_heartbeat_monotonic
                ) >= _hb_interval:
                    _health_failures = []
                    for _hctx in user_contexts:
                        _health_results = evaluate_runtime_health(
                            broker=_hctx.broker,
                            news_enabled=bool(news_enabled),
                            news_pipeline=news_pipeline,
                            news_rules=news_rules,
                            news_summary=news_pipeline_summary(),
                            process_start_ts=_PROCESS_START_TS,
                        )
                        _health_failures.extend(
                            (str(_hctx.user_id), result)
                            for result in failed_health_checks(_health_results)
                        )
                    notify_alpaca_loop_health_alert(_health_failures, now_et=dt)
                    notify_alpaca_loop_heartbeat(_heartbeat_rows, now_et=dt)
                    _last_heartbeat_monotonic = _now_mono_hb

            if all_users_stopped:
                print(dt.strftime("%Y-%m-%d %H:%M ET"), "All users stopped for today.")
                _telegram_loop_stop(
                    "all_users_stopped",
                    "Every user hit portfolio risk / cannot trade for the rest of the session.",
                )
                break

            _parts_sleep = [exit_interval_min, entry_interval_min]
            if _du_init:
                _parts_sleep.extend([_eff_dyn_ent_init, _eff_dyn_ext_init])
            _loop_sleep_min = min(_parts_sleep)
            if any_user_reduce_only:
                _loop_sleep_min = min(_loop_sleep_min, ro_exit_min)
            _session_dyn_sleep_sec, _session_core_sleep_sec = _market_session_entry_cadence_seconds(
                dt,
                default_dynamic_seconds=_loop_sleep_min * 60.0,
                default_core_seconds=_loop_sleep_min * 60.0,
            )
            _loop_sleep_min = min(
                _loop_sleep_min,
                max(1.0, float(_session_dyn_sleep_sec) / 60.0),
                max(1.0, float(_session_core_sleep_sec) / 60.0),
            )
            _normal_sleep_sec = max(60, _loop_sleep_min * 60)
            _poll_mode, _poll_options_mode = _live_loop_poll_context(user_contexts)
            _eff_sleep_sec, _sleep_reason = _live_loop_poll_sleep_seconds(
                first_config,
                mode=_poll_mode,
                options_mode=_poll_options_mode,
                default_sleep_seconds=_normal_sleep_sec,
            )
            _log_live_loop_sleep(
                _eff_sleep_sec,
                mode=_poll_mode,
                options_mode=_poll_options_mode,
                reason=_sleep_reason,
            )
            _sleep_label = (
                "~%d sec" % int(_eff_sleep_sec)
                if _eff_sleep_sec < 60
                else "~%d min" % int(round(float(_eff_sleep_sec) / 60.0))
            )
            print(
                dt.strftime("%H:%M ET"),
                "— sleeping %s (next wake). Entry lane ran this tick: %s.%s"
                % (
                    _sleep_label,
                    did_any_entry_lane,
                    (
                        " reduce_only exit cadence %d min"
                        % (ro_exit_min,)
                        if any_user_reduce_only
                        else ""
                    ),
                ),
                flush=True,
            )
            sys.stdout.flush()
            time.sleep(_eff_sleep_sec)

    finally:
        if not _loop_stop_telegram_sent:
            exc_type, exc_value, _ = sys.exc_info()
            if exc_type is KeyboardInterrupt:
                _telegram_loop_stop("keyboard_interrupt", "Ctrl+C / SIGINT")
            elif exc_type is SystemExit:
                _telegram_loop_stop(
                    "system_exit",
                    str(getattr(exc_value, "code", exc_value)),
                )
            elif exc_type is not None:
                _telegram_loop_stop(
                    exc_type.__name__,
                    str(exc_value)[:500] if exc_value is not None else "",
                )
        for lk in reversed(loop_locks):
            lk.release()
