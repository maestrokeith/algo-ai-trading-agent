"""Structured options observability helpers for live-cycle diagnostics."""
from __future__ import annotations

import logging
from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class OptionsCycleStats:
    """Per-cycle option route counters; no trading decisions depend on this state."""

    symbols_evaluated: set[str] = field(default_factory=set)
    signals_generated: int = 0
    candidates_rejected: int = 0
    contracts_found: int = 0
    orders_submitted: int = 0
    fills: int = 0
    rejection_reasons: Counter[str] = field(default_factory=Counter)


_cycle_stats: ContextVar[OptionsCycleStats] = ContextVar(
    "options_cycle_stats",
    default=OptionsCycleStats(),
)


def reset_options_cycle_stats() -> OptionsCycleStats:
    """Reset and return the current cycle stats object."""
    stats = OptionsCycleStats()
    _cycle_stats.set(stats)
    return stats


def current_options_cycle_stats() -> OptionsCycleStats:
    """Return the active options cycle stats object."""
    return _cycle_stats.get()


def normalize_options_rejection_reason(reason: str | None) -> str:
    """Map free-form option gate text to stable rejection buckets."""
    text = str(reason or "").strip().lower()
    if not text:
        return "order_submission_failed"
    checks = [
        ("options_disabled", ("options.enabled is false", "options disabled", "runtime_disabled", "options_runtime_disabled")),
        ("live_pilot_disabled", ("live_pilot_disabled", "pilot is disabled")),
        ("mode_not_live", ("mode", "not explicitly enabled", "not paper_only or live pilot", "non_paper_mode")),
        ("broker_not_supported", ("broker_not_supported", "no option client", "no option data")),
        ("market_closed", ("market closed",)),
        ("symbol_not_allowed", ("underlying not allowed", "not_optionable", "allowed_underlyings")),
        ("no_contract_found", ("no option chain", "no_chain", "no_contract", "no ranked candidate", "candidates is empty")),
        ("expiration_filter", ("expiration", "expiry", "dte")),
        ("delta_filter", ("delta",)),
        ("liquidity_filter", ("liquidity",)),
        ("open_interest_filter", ("open interest", "open_interest")),
        ("volume_filter", ("low_volume", "volume")),
        ("spread_filter", ("spread", "wide_spread")),
        ("premium_too_high", ("premium", "budget", "too expensive")),
        ("portfolio_exposure_limit", ("exposure", "cap")),
        ("daily_trade_limit", ("daily cap", "entries per day", "daily_trade")),
        ("daily_loss_limit", ("daily loss",)),
        ("position_limit", ("position", "holding equity", "duplicate contract", "max_option_positions")),
        ("entry_quality_gate", ("conviction", "weak signal", "entry quality", "dynamic_options_weak_signal")),
        ("underlying_signal_missing", ("missing_account_equity", "missing account", "underlying_signal", "could not build option intent")),
        ("order_submission_failed", ("order failed", "submission", "place_order", "could not build order")),
    ]
    for bucket, needles in checks:
        if any(needle in text for needle in needles):
            return bucket
    return "order_submission_failed"


def record_options_candidate(
    *,
    symbol: str,
    underlying: str,
    direction: str,
    source: str,
    stage: str = "candidate",
) -> None:
    """Log and count an options candidate reaching the route."""
    sym = str(symbol or underlying or "OPTIONS").strip().upper()
    und = str(underlying or sym).strip().upper()
    current_options_cycle_stats().symbols_evaluated.add(sym)
    log.info(
        "OPTIONS_CANDIDATE symbol=%s underlying=%s direction=%s source=%s stage=%s",
        sym,
        und,
        str(direction or "unknown").strip().lower(),
        str(source or "unknown").strip(),
        str(stage or "candidate").strip(),
    )


def record_options_signal(
    *,
    symbol: str,
    eligible: bool,
    reason: str,
) -> None:
    """Count option signal evaluation and log normalized rejection when ineligible."""
    stats = current_options_cycle_stats()
    if eligible:
        stats.signals_generated += 1
        return
    record_options_rejection(symbol=symbol, stage="signal", reason=reason)


def record_options_rejection(*, symbol: str, stage: str, reason: str) -> str:
    """Log and count a normalized option rejection reason."""
    normalized = normalize_options_rejection_reason(reason)
    stats = current_options_cycle_stats()
    stats.candidates_rejected += 1
    stats.rejection_reasons[normalized] += 1
    log.info(
        "OPTIONS_REJECT symbol=%s stage=%s reason=%s raw_reason=%s",
        str(symbol or "OPTIONS").strip().upper(),
        str(stage or "unknown").strip(),
        normalized,
        str(reason or "unknown").replace(" ", "_"),
    )
    return normalized


def record_options_contract_found(symbol: str) -> None:
    current_options_cycle_stats().contracts_found += 1
    log.info("OPTIONS_CONTRACT_FOUND symbol=%s", str(symbol or "OPTIONS").strip().upper())


def record_options_order_submitted(symbol: str) -> None:
    current_options_cycle_stats().orders_submitted += 1
    log.info("OPTIONS_ORDER_SUBMITTED_EVENT symbol=%s", str(symbol or "OPTIONS").strip().upper())


def record_options_order_accepted(symbol: str) -> None:
    log.info("OPTIONS_ORDER_ACCEPTED symbol=%s", str(symbol or "OPTIONS").strip().upper())


def record_options_fill(symbol: str) -> None:
    current_options_cycle_stats().fills += 1
    log.info("OPTIONS_ORDER_FILLED symbol=%s", str(symbol or "OPTIONS").strip().upper())


def format_top_rejection_reasons(stats: OptionsCycleStats, *, limit: int = 3) -> str:
    if not stats.rejection_reasons:
        return "none"
    return ",".join(f"{reason}:{count}" for reason, count in stats.rejection_reasons.most_common(limit))


def emit_options_cycle_summary(stats: OptionsCycleStats | None = None) -> None:
    """Emit the requested per-live-cycle option summary."""
    s = stats or current_options_cycle_stats()
    log.info(
        "OPTIONS_CYCLE_SUMMARY symbols_evaluated=%d signals_generated=%d candidates_rejected=%d "
        "contracts_found=%d orders_submitted=%d fills=%d top_rejection_reasons=%s",
        len(s.symbols_evaluated),
        int(s.signals_generated),
        int(s.candidates_rejected),
        int(s.contracts_found),
        int(s.orders_submitted),
        int(s.fills),
        format_top_rejection_reasons(s),
    )
