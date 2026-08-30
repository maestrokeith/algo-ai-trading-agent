"""Structured one-line log for live-loop entry evaluation."""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)


def _fmt_bool(v: bool | None) -> str:
    if v is None:
        return "n/a"
    return "T" if v else "F"


def infer_spread_position_cooldown_ok(
    *, allowed: bool, reason: str | None
) -> tuple[bool | None, bool | None, bool | None]:
    """Best-effort flags from :class:`TradeDecision` (denied reasons are approximate)."""
    if allowed:
        return (True, True, True)
    if reason is None:
        return (None, None, None)
    r = reason.lower()
    spread_ok = True
    if "market_quality" in r and "spread" in r:
        spread_ok = False
    if r.startswith("spread ") or ("spread" in r and "max" in r and "market_quality" not in r):
        spread_ok = False
    cooldown_ok = "cooldown" not in r
    position_ok = not any(
        x in r for x in ("max open risk", "would exceed max open risk", "reject", "sizing.risk")
    )
    return (spread_ok, position_ok, cooldown_ok)


def trend_scan_route_label(*, is_dynamic_added: bool) -> str:
    """Entry-eval route when the main branch is the trend scan: core vs dynamic-universe add-on."""
    return "momentum_breakout" if is_dynamic_added else "trend_long"


def log_execution_block(
    *,
    symbol: str,
    spread_pct: float | None,
    buying_power: float | None,
    cooldown_ok: bool | None,
    position_ok: bool | None,
) -> None:
    """Structured INFO line when entry gates deny (spread / BP / inferred cooldown & position flags)."""
    sym = str(symbol or "").strip().upper() or "?"
    try:
        sp = float(spread_pct) if spread_pct is not None and spread_pct == spread_pct else 0.0
    except (TypeError, ValueError):
        sp = 0.0
    try:
        bp = float(buying_power) if buying_power is not None and buying_power == buying_power else 0.0
    except (TypeError, ValueError):
        bp = 0.0
    cd = cooldown_ok if cooldown_ok is not None else False
    po = position_ok if position_ok is not None else False
    logger.info(
        "EXECUTION_BLOCK symbol=%s spread=%.2f bp=%.2f cooldown=%s position=%s",
        sym,
        sp,
        bp,
        cd,
        po,
    )


def log_entry_eval(
    *,
    symbol: str,
    route: str,
    trend: bool | None,
    pullback: bool | None,
    momentum: bool | None,
    volatility: bool | None,
    regime: bool | None,
    spread: bool | None,
    position: bool | None,
    cooldown: bool | None,
    final_signal: bool,
    final_reason: str | None,
    ai_catalyst_score: int | None = None,
    event_store: Any | None = None,
    user_id: str | None = None,
    allocator_followup: Mapping[str, Any] | None = None,
) -> None:
    """Print a single ENTRY_EVAL line for grep/monitoring."""
    payload = {
        "trend": _fmt_bool(trend),
        "pullback": _fmt_bool(pullback),
        "momentum": _fmt_bool(momentum),
        "volatility": _fmt_bool(volatility),
        "regime": _fmt_bool(regime),
        "spread": _fmt_bool(spread),
        "position": _fmt_bool(position),
        "cooldown": _fmt_bool(cooldown),
    }
    if ai_catalyst_score is not None:
        payload["ai_catalyst_score"] = int(ai_catalyst_score)
    if event_store is not None:
        try:
            event_store.record_entry_evaluation(
                user_id=user_id,
                symbol=symbol,
                route=route,
                final=final_signal,
                reason=final_reason,
                payload=payload,
            )
            event_store.record_signal(
                user_id=user_id,
                symbol=symbol,
                signal_type=route,
                decision="entry" if final_signal else "skip",
                reason=final_reason,
                payload=payload,
            )
        except Exception:
            logger.debug("SQLite entry evaluation hook failed", exc_info=True)
    print(
        "%s ENTRY_EVAL route=%s trend=%s pullback=%s momentum=%s vol=%s regime=%s "
        "spread=%s pos=%s cooldown=%s final=%s reason=%s"
        % (
            symbol,
            route,
            _fmt_bool(trend),
            _fmt_bool(pullback),
            _fmt_bool(momentum),
            _fmt_bool(volatility),
            _fmt_bool(regime),
            _fmt_bool(spread),
            _fmt_bool(position),
            _fmt_bool(cooldown),
            "T" if final_signal else "F",
            (final_reason or "").replace("\n", " ")[:200],
        ),
        flush=True,
    )
    if allocator_followup is not None and bool(allocator_followup.get("allocator_on")) and final_signal:
        action = str(allocator_followup.get("action") or "skip").strip().lower()
        sym = str(allocator_followup.get("symbol") or symbol or "").strip().upper()
        route_text = str(allocator_followup.get("route") or route or "n/a")
        reason_text = str(allocator_followup.get("reason") or final_reason or "ok")
        stage = str(allocator_followup.get("stage") or "entry_eval")
        allocator_on_text = str(bool(allocator_followup.get("allocator_on"))).lower()
        final_text = str(bool(final_signal)).lower()
        print(
            "ENTRY_EVAL_PASS symbol=%s route=%s reason=%s allocator_on=%s"
            % (
                sym,
                route_text,
                reason_text,
                allocator_on_text,
            ),
            flush=True,
        )
        print(
            "ENTRY_TO_ALLOCATOR_TRACE symbol=%s route=%s decision_present=%s "
            "decision_allowed=%s order_request_present=%s ohlcv_present=%s "
            "allocator_on=%s followup_emitted=%s"
            % (
                sym,
                route_text,
                str(bool(allocator_followup.get("decision_present", True))).lower(),
                str(bool(allocator_followup.get("decision_allowed", final_signal))).lower(),
                str(bool(allocator_followup.get("order_request_present", False))).lower(),
                str(bool(allocator_followup.get("ohlcv_present", False))).lower(),
                allocator_on_text,
                str(bool(allocator_followup.get("followup_emitted", False))).lower(),
            ),
            flush=True,
        )
        print(
            "ENTRY_TO_ALLOCATOR_FOLLOWUP_START symbol=%s route=%s action=%s stage=%s"
            % (
                sym,
                route_text,
                action,
                stage,
            ),
            flush=True,
        )
        if action == "enqueue":
            try:
                score = float(allocator_followup.get("score", 0.0) or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            print(
                "ALLOCATOR_ENQUEUE symbol=%s route=%s reason=%s score=%.4f allocator_on=%s final=%s stage=%s"
                % (
                    sym,
                    route_text,
                    reason_text,
                    score,
                    allocator_on_text,
                    final_text,
                    stage,
                ),
                flush=True,
            )
            print(
                "ENTRY_TO_ALLOCATOR_FOLLOWUP_END symbol=%s route=%s result=enqueue stage=%s"
                % (sym, route_text, stage),
                flush=True,
            )
        elif action == "skip":
            print(
                "ENTRY_TO_ALLOCATOR_FOLLOWUP_SKIPPED symbol=%s reason=%s route=%s stage=%s"
                % (sym, reason_text, route_text, stage),
                flush=True,
            )
            print(
                "ALLOCATOR_APPEND_SKIPPED symbol=%s route=%s reason=%s allocator_on=%s final=%s stage=%s"
                % (
                    sym,
                    route_text,
                    reason_text,
                    allocator_on_text,
                    final_text,
                    stage,
                ),
                flush=True,
            )
            print(
                "ENTRY_TO_ALLOCATOR_FOLLOWUP_END symbol=%s route=%s result=skipped reason=%s stage=%s"
                % (sym, route_text, reason_text, stage),
                flush=True,
            )
            print(
                "ALLOCATOR_ENQUEUE_SKIP symbol=%s route=%s reason=%s allocator_on=%s final=%s stage=%s"
                % (
                    sym,
                    route_text,
                    reason_text,
                    allocator_on_text,
                    final_text,
                    stage,
                ),
                flush=True,
            )


def _fmt_opt_float(v: float | None, *, nd: int = 2) -> str:
    if v is None or v != v:
        return "n/a"
    return ("%." + str(nd) + "f") % float(v)


def log_options_gate(
    *,
    symbol: str,
    gross_exposure_pct: float | None,
    reduce_only: bool,
    spread_pct: float | None = None,
    dte: int | None = None,
    delta: float | None = None,
    final: bool,
    reason: str,
) -> None:
    """
    Single OPTIONS_GATE line for grep / dashboards.

    *gross_exposure_pct* — same 0–100+ scale as :func:`src.exposure.compute_exposures` (78 → gross=0.78).
    *spread_pct* — same units as :class:`~src.options_selector.SelectedOptionContract.spread_pct`
    (percent of mid; 4.0 → spread=0.04).
    """
    sym = str(symbol).strip().upper()
    if gross_exposure_pct is not None and gross_exposure_pct == gross_exposure_pct:
        gross_show = _fmt_opt_float(float(gross_exposure_pct) / 100.0, nd=2)
    else:
        gross_show = "n/a"
    if spread_pct is not None and spread_pct == spread_pct:
        sp_frac = float(spread_pct) / 100.0
        spread_show = _fmt_opt_float(sp_frac, nd=2)
    else:
        spread_show = "n/a"
    dte_show = str(int(dte)) if dte is not None else "n/a"
    if delta is not None and delta == delta:
        delta_show = _fmt_opt_float(abs(float(delta)), nd=2)
    else:
        delta_show = "n/a"
    print(
        "OPTIONS_GATE symbol=%s gross=%s reduce_only=%s spread=%s dte=%s delta=%s final=%s reason=%s"
        % (
            sym,
            gross_show,
            str(reduce_only),
            spread_show,
            dte_show,
            delta_show,
            str(final),
            (reason or "").replace("\n", " ")[:200],
        ),
        flush=True,
    )


def option_delta_from_chain(occ_symbol: str, chain: Sequence[Any] | None) -> float | None:
    """Match OCC symbol on chain rows and return absolute delta when present."""
    if not chain:
        return None
    want = str(occ_symbol).strip().upper()
    for c in chain:
        if str(getattr(c, "symbol", "") or "").strip().upper() != want:
            continue
        raw = getattr(c, "delta", None)
        if raw is None:
            return None
        try:
            return abs(float(raw))
        except (TypeError, ValueError):
            return None
    return None
