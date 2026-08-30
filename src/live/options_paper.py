"""Paper-only options entry helpers for live-loop dynamic signals."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from src.entry_router import EntryRouteSignal, route_to_options_executor
from src.live.options_chain import log_options_disabled_non_paper, option_chain_for_underlying, options_runtime_enabled
from src.options_config import (
    allow_new_entries,
    dynamic_options_entry_eligible,
    options_live_pilot_enabled,
    options_mode,
    paper_dynamic_options_spread_cap,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PaperOptionEntryResult:
    placed: bool
    reason: str | None
    reason_codes: tuple[str, ...]
    direction: str | None = None
    right: str | None = None


def paper_only_options_active(config: Mapping[str, Any] | None) -> bool:
    opts = (config or {}).get("options")
    if not isinstance(opts, Mapping):
        return False
    return (
        bool(opts.get("enabled"))
        and bool(allow_new_entries(dict(config or {})))
        and str(opts.get("mode") or "").strip().lower() == "paper_only"
    )


def live_pilot_options_active(config: Mapping[str, Any] | None, broker: Any) -> bool:
    """True when a live broker is explicitly in the strict long-premium pilot."""
    if bool(getattr(broker, "paper", True)):
        return False
    cfg = dict(config or {})
    opts = cfg.get("options") if isinstance(cfg.get("options"), Mapping) else {}
    return (
        bool(opts.get("enabled"))
        and bool(allow_new_entries(cfg))
        and options_mode(cfg) in {"live", "live_long_premium", "long_premium_only"}
        and options_live_pilot_enabled(cfg)
    )


def _daily_loss_pct_from_snapshot(snapshot: Mapping[str, Any] | None) -> float | None:
    if not isinstance(snapshot, Mapping):
        return None
    try:
        equity = float(snapshot.get("equity") or 0.0)
    except (TypeError, ValueError):
        equity = 0.0
    if equity <= 0:
        return None
    last_equity_raw = snapshot.get("last_equity")
    try:
        last_equity = float(last_equity_raw) if last_equity_raw is not None and str(last_equity_raw).strip() != "" else None
    except (TypeError, ValueError):
        last_equity = None
    if last_equity is not None and last_equity > 0:
        return ((equity - last_equity) / last_equity) * 100.0
    pnl_today_raw = snapshot.get("pnl_today")
    try:
        pnl_today = float(pnl_today_raw) if pnl_today_raw is not None and str(pnl_today_raw).strip() != "" else None
    except (TypeError, ValueError):
        pnl_today = None
    if pnl_today is None:
        return None
    return (pnl_today / equity) * 100.0


def _option_direction_from_vwap(price: float, vwap: float) -> tuple[str, str, str]:
    if price >= vwap:
        return "bullish", "call", "above_vwap"
    return "bearish", "put", "below_vwap"


def paper_only_relaxed_options_config(
    config: Mapping[str, Any],
    *,
    dynamic_eligible: bool = False,
    broker_is_paper: bool = True,
) -> dict[str, Any]:
    """Paper-only profile: one contract max; wider spread only for eligible dynamic signals."""
    cfg = dict(config or {})
    opts = dict(cfg.get("options") or {})
    if str(opts.get("mode") or "").strip().lower() == "paper_only":
        opts["min_regime_score_for_entries"] = 2
        opts["max_contracts_per_trade"] = 1
        opts["v1_max_contracts_per_trade"] = 1
        cfg["options"] = opts
        if dynamic_eligible:
            cap = paper_dynamic_options_spread_cap(cfg, broker_is_paper=broker_is_paper)
            if cap is not None:
                opts["max_bid_ask_spread_pct"] = float(cap)
                cs = dict(opts.get("contract_selection") or {})
                cs["max_bid_ask_spread_pct"] = float(cap)
                opts["contract_selection"] = cs
    cfg["options"] = opts
    return cfg


def attempt_paper_option_entry(
    config: Mapping[str, Any],
    *,
    broker: Any,
    execution_manager: Any,
    symbol: str,
    dt: datetime,
    current_price: float,
    session_vwap: float | None,
    account_equity: float,
    positions: list[dict[str, Any]] | None,
    source: str,
    conviction_score: float | None = None,
    scanner_score: float | None = None,
    news_score: float | None = None,
    event_score: float | None = None,
    catalyst_score: float | None = None,
    relative_volume: float | None = None,
    tracked: Mapping[str, Any] | None = None,
    chain_candidates: Sequence[Any] | None = None,
    enforce_dynamic_gate: bool = True,
) -> PaperOptionEntryResult:
    """
    Try a tightly gated option entry for a dynamic signal.

    This never emits a stock order. When no contract can be selected, it returns False and leaves
    stock flow untouched for callers.
    """
    broker_is_paper = bool(getattr(broker, "paper", False))
    effective_config = paper_only_relaxed_options_config(
        config,
        dynamic_eligible=False,
        broker_is_paper=broker_is_paper,
    )
    if not options_runtime_enabled(broker, effective_config):
        log_options_disabled_non_paper()
        return PaperOptionEntryResult(False, "options disabled outside paper mode", ("non_paper_mode",))
    live_pilot_active = live_pilot_options_active(effective_config, broker)
    if not paper_only_options_active(effective_config) and not live_pilot_active:
        return PaperOptionEntryResult(False, "options.mode is not paper_only or live pilot", ("mode_off",))

    eligible = True
    eligible_reason = "dynamic_gate_not_required"
    if enforce_dynamic_gate:
        eligible, eligible_reason = dynamic_options_entry_eligible(
            dict(config or {}),
            scanner_score=scanner_score if scanner_score is not None else conviction_score,
            news_score=news_score,
            catalyst_score=catalyst_score,
        )
    if not eligible:
        log.info(
            "OPTIONS_DYNAMIC_ELIGIBILITY symbol=%s eligible=false reason=%s scanner_score=%s news_score=%s catalyst_score=%s",
            str(symbol or "").strip().upper(),
            eligible_reason,
            "n/a" if scanner_score is None else "%.2f" % float(scanner_score),
            "n/a" if news_score is None else "%.2f" % float(news_score),
            "n/a" if catalyst_score is None else "%.3f" % float(catalyst_score),
        )
        return PaperOptionEntryResult(False, eligible_reason, ("dynamic_options_weak_signal",))

    effective_config = paper_only_relaxed_options_config(
        config,
        dynamic_eligible=True,
        broker_is_paper=broker_is_paper,
    )
    if session_vwap is None or session_vwap <= 0:
        return PaperOptionEntryResult(False, "session VWAP unavailable", ("no_vwap",))

    direction, right, vwap_reason = _option_direction_from_vwap(float(current_price), float(session_vwap))
    route_mode = "live_pilot" if live_pilot_active else "paper_only"
    reason_codes = [route_mode, eligible_reason, vwap_reason, right]
    log.info(
        "OPTIONS_DYNAMIC_ELIGIBILITY symbol=%s eligible=true reason=%s scanner_score=%s news_score=%s catalyst_score=%s",
        str(symbol or "").strip().upper(),
        eligible_reason,
        "n/a" if scanner_score is None else "%.2f" % float(scanner_score),
        "n/a" if news_score is None else "%.2f" % float(news_score),
        "n/a" if catalyst_score is None else "%.3f" % float(catalyst_score),
    )

    opts = effective_config.get("options") or {}
    max_daily_loss_pct = opts.get("max_daily_loss_pct")
    if max_daily_loss_pct is not None and str(max_daily_loss_pct).strip() != "":
        try:
            max_daily_loss = abs(float(max_daily_loss_pct))
        except (TypeError, ValueError):
            max_daily_loss = 0.0
        if max_daily_loss > 0 and hasattr(broker, "get_account_snapshot"):
            try:
                snap = broker.get_account_snapshot()
            except Exception:
                snap = None
            daily_loss_pct = _daily_loss_pct_from_snapshot(snap if isinstance(snap, Mapping) else None)
            if daily_loss_pct is not None and daily_loss_pct <= -max_daily_loss + 1e-9:
                reason = (
                    "daily options loss %.2f%% <= -%.2f%%"
                    % (daily_loss_pct, max_daily_loss)
                )
                return PaperOptionEntryResult(False, reason, tuple(reason_codes + ["daily_loss_block"]), direction, right)

    boosted = max(
        [float(v) for v in (conviction_score, news_score, event_score) if v is not None],
        default=0.0,
    )
    if boosted > 0:
        reason_codes.append("boosted")
    else:
        reason_codes.append("no_boost")

    sig = EntryRouteSignal(
        underlying=str(symbol or "").strip().upper(),
        direction=direction,
        source=str(source or "dynamic"),
        stock_symbol=str(symbol or "").strip().upper(),
        conviction_score=boosted if boosted > 0 else conviction_score,
        news_score=news_score,
        event_score=event_score,
        relative_volume=relative_volume,
    )

    chain = list(chain_candidates) if chain_candidates is not None else option_chain_for_underlying(
        broker,
        effective_config,
        str(symbol or "").strip().upper(),
        dt,
    )
    if not chain:
        reason = "no option chain rows"
        log_label = "OPTIONS_LIVE_PILOT_BLOCK" if live_pilot_active else "OPTIONS_PAPER_ONLY_BLOCK"
        log.info(
            "%s symbol=%s reason=%s reason_codes=%s",
            log_label,
            symbol,
            reason,
            ",".join(reason_codes + ["no_chain"]),
        )
        return PaperOptionEntryResult(False, reason, tuple(reason_codes + ["no_chain"]), direction, right)

    placed = route_to_options_executor(
        effective_config,
        sig,
        log_dt=dt,
        account_equity=float(account_equity),
        positions=positions,
        broker=broker,
        execution_manager=execution_manager,
        chain_candidates=chain,
        underlying_spot=float(current_price),
        tracked=tracked if isinstance(tracked, Mapping) else None,
    )
    if placed:
        log_label = "OPTIONS_LIVE_PILOT_PLACED" if live_pilot_active else "OPTIONS_PAPER_ONLY_PLACED"
        log.info(
            "%s symbol=%s right=%s price=%.2f vwap=%.2f reason_codes=%s",
            log_label,
            str(symbol or "").strip().upper(),
            right,
            float(current_price),
            float(session_vwap),
            ",".join(reason_codes),
        )
        return PaperOptionEntryResult(True, None, tuple(reason_codes), direction, right)

    reason = "live pilot options route failed" if live_pilot_active else "paper-only options route failed"
    log_label = "OPTIONS_LIVE_PILOT_BLOCK" if live_pilot_active else "OPTIONS_PAPER_ONLY_BLOCK"
    log.info(
        "%s symbol=%s reason=%s reason_codes=%s",
        log_label,
        str(symbol or "").strip().upper(),
        reason,
        ",".join(reason_codes + ["route_failed"]),
    )
    return PaperOptionEntryResult(False, reason, tuple(reason_codes + ["route_failed"]), direction, right)
