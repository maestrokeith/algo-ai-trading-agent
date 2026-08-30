"""
v1: long premium — filter chain rows, rank (ATM-first, then cheaper mid), pick the first row that fits
budget (``mid×100``), spread cap, and liquidity.

Primary entry path: :func:`select_first_ranked_candidate_within_budget` (used by the live router).
:func:`select_option_contract` remains for single-ATM selection without a premium budget.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

from .options_config import max_bid_ask_spread_pct_cap, max_option_delta, min_option_delta, target_dte_bounds
from .options_premium_risk import max_premium_budget_usd

# US equity OCC: root (1–6 letters) + YYMMDD + C|P + strike in thousandths (8 digits)
_OCC_OPTION_FULL_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OptionContractCandidate:
    """One tradable series (e.g. from an options chain snapshot)."""

    symbol: str
    strike: float
    expiration: date
    right: str  # "call" | "put"
    open_interest: int
    volume: int
    bid: float
    ask: float
    delta: float | None = None  # broker greeks when present; ``min_delta`` skips unknowns
    iv: float | None = None

@dataclass(frozen=True)
class SelectedOptionContract:
    """Chosen contract after ATM + liquidity filters."""

    symbol: str
    strike: float
    expiration: date
    right: str
    bid: float
    ask: float
    mid: float
    spread_pct: float
    open_interest: int
    volume: int
    iv: float | None = None


@dataclass(frozen=True)
class OptionContractScore:
    """Scored option contract candidate."""

    candidate: OptionContractCandidate
    selected: SelectedOptionContract | None
    score: float
    accepted: bool
    reason_codes: tuple[str, ...]
    components: dict[str, float]


OPTION_REJECT_COUNTER_KEYS: tuple[str, ...] = (
    "spread_failed",
    "liquidity_failed",
    "premium_over_budget",
    "delta_failed",
    "dte_failed",
    "stale_quote",
    "missing_bid_ask",
    "contract_not_tradable",
)

OPTION_FILTER_COUNTER_KEYS: tuple[str, ...] = (
    "quote_fail",
    "spread_fail",
    "liquidity_fail",
    "volume_fail",
    "open_interest_fail",
    "delta_fail",
    "expiry_fail",
    "budget_fail",
)


def _contract_selection_cfg(config: dict[str, Any]) -> dict[str, Any]:
    o = config.get("options") or {}
    return o.get("contract_selection") or {}


def _signal_float(signal: Any, *names: str, default: float | None = None) -> float | None:
    for name in names:
        raw = getattr(signal, name, None)
        if raw is None and isinstance(signal, Mapping):
            raw = signal.get(name)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return default


def _signal_text(signal: Any, *names: str) -> str | None:
    for name in names:
        raw = getattr(signal, name, None)
        if raw is None and isinstance(signal, Mapping):
            raw = signal.get(name)
        if raw is None or str(raw).strip() == "":
            continue
        return str(raw).strip()
    return None


def _score_0_10(raw: float | None) -> float:
    if raw is None:
        return 0.0
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if v <= 0:
        return 0.0
    if v <= 1.0:
        return max(0.0, min(10.0, v * 10.0))
    if v <= 100.0:
        return max(0.0, min(10.0, v / 10.0))
    return 10.0


def _preferred_window_score(value: float | None, pref_lo: float, pref_hi: float, hard_lo: float, hard_hi: float) -> float:
    if value is None:
        return 0.0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v < hard_lo or v > hard_hi:
        return 0.0
    if pref_lo <= v <= pref_hi:
        return 10.0
    if v < pref_lo:
        span = max(pref_lo - hard_lo, 1e-9)
        return max(0.0, min(10.0, 10.0 * (v - hard_lo) / span))
    span = max(hard_hi - pref_hi, 1e-9)
    return max(0.0, min(10.0, 10.0 * (hard_hi - v) / span))


def _delta_fit_score(delta: float | None) -> float:
    if delta is None:
        return 0.0
    try:
        d = abs(float(delta))
    except (TypeError, ValueError):
        return 0.0
    # Preferred 0.40-0.55, hard range 0.35-0.60.
    if 0.40 <= d <= 0.55:
        return 10.0
    if 0.35 <= d < 0.40:
        return 5.0 + ((d - 0.35) / 0.05) * 5.0
    if 0.55 < d <= 0.60:
        return 5.0 + ((0.60 - d) / 0.05) * 5.0
    return 0.0


def _cheapness_score(mid: float) -> float:
    if mid <= 0:
        return 0.0
    # Smaller mid is better; keep this a gentle tiebreaker rather than a dominant term.
    return max(0.0, min(10.0, 10.0 - (mid * 1.5)))


def _tight_spread_score(spread_pct: float, *, cap: float) -> float:
    if cap <= 0:
        return 0.0
    if spread_pct < 0:
        return 0.0
    if spread_pct > cap:
        return 0.0
    return max(0.0, min(10.0, 10.0 * (1.0 - (spread_pct / cap))))


def _liquidity_score(open_interest: int, volume: int) -> float:
    oi_score = max(0.0, min(10.0, (max(0, open_interest) - 500.0) / 150.0))
    vol_score = max(0.0, min(10.0, (max(0, volume) - 100.0) / 90.0))
    return (oi_score + vol_score) / 2.0


def _liquidity_penalty(open_interest: int, volume: int) -> float:
    # Low-liquidity contracts near the floor get a mild penalty even when they clear hard gates.
    oi_penalty = max(0.0, 4.0 - (_liquidity_score(open_interest, 0) / 2.5))
    vol_penalty = max(0.0, 4.0 - (_liquidity_score(0, volume) / 2.5))
    return oi_penalty + vol_penalty


def _iv_penalty(iv: float | None) -> float:
    if iv is None:
        return 0.0
    try:
        v = float(iv)
    except (TypeError, ValueError):
        return 0.0
    if v <= 0:
        return 0.0
    if v <= 0.60:
        return 0.0
    return max(0.0, min(10.0, (v - 0.60) * 20.0))


def _near_expiry_penalty(dte: int) -> float:
    if dte < 0:
        return 10.0
    if 7 <= dte <= 21:
        return 0.0
    if dte < 7:
        return float(7 - dte) * 1.5
    return float(dte - 21) * 0.5


def _contract_selection_signals(signal: Any | None) -> dict[str, float | None]:
    catalyst_type = _signal_text(signal, "catalyst_type", "news_catalyst_type", "event_type")
    catalyst_bonus = _catalyst_type_score(catalyst_type)
    return {
        "momentum": _signal_float(signal, "momentum_score", "conviction_score", "score", default=0.0),
        "news": _signal_float(signal, "news_score", default=0.0),
        "event": _signal_float(signal, "event_score", default=0.0),
        "relative_volume": _signal_float(signal, "relative_volume", default=None),
        "catalyst_type": catalyst_bonus,
    }


def _catalyst_type_score(catalyst_type: str | None) -> float:
    text = str(catalyst_type or "").strip().lower()
    if not text:
        return 0.0
    weights = {
        "earnings": 10.0,
        "guidance": 9.0,
        "fda": 9.0,
        "approval": 8.5,
        "m&a": 8.0,
        "deal": 8.0,
        "partnership": 7.5,
        "analyst": 7.0,
        "ai": 7.0,
        "sec_filing": 6.0,
    }
    return weights.get(text, 4.0)


def _contract_score_components(
    *,
    selected: SelectedOptionContract,
    candidate: OptionContractCandidate,
    signal: Any | None,
    underlying_spot: float,
    as_of: date,
    max_spread: float,
) -> dict[str, float]:
    dte = _days_to_expiry(candidate.expiration, as_of)
    sig = _contract_selection_signals(signal)
    strike_dist_pct = abs(float(candidate.strike) - float(underlying_spot)) / max(float(underlying_spot), 1e-9) * 100.0
    rel_vol = sig["relative_volume"]
    rel_component = 0.0
    if rel_vol is not None and rel_vol > 0:
        rel_component = max(0.0, min(10.0, min(rel_vol, 5.0) / 5.0 * 10.0))
    momentum_component = _score_0_10(sig["momentum"])
    news_component = _score_0_10(sig["news"])
    event_component = _score_0_10(sig["event"])
    catalyst_component = max(float(sig["catalyst_type"] or 0.0), news_component, event_component)
    spread_component = _tight_spread_score(float(selected.spread_pct), cap=max_spread)
    oi_component = max(0.0, min(10.0, (float(selected.open_interest) - 500.0) / 150.0))
    vol_component = max(0.0, min(10.0, (float(selected.volume) - 100.0) / 90.0))
    delta_component = _delta_fit_score(candidate.delta)
    atm_component = max(0.0, 10.0 - (strike_dist_pct * 0.8))
    cheap_component = _cheapness_score(float(selected.mid))
    iv_penalty = _iv_penalty(candidate.iv)
    expiry_penalty = _near_expiry_penalty(dte)
    liquidity_penalty = _liquidity_penalty(int(selected.open_interest), int(selected.volume))
    return {
        "momentum": momentum_component,
        "news": news_component,
        "event": event_component,
        "catalyst_strength": catalyst_component,
        "relative_volume": rel_component,
        "tight_spread": spread_component,
        "open_interest": oi_component,
        "option_volume": vol_component,
        "delta_fit": delta_component,
        "atm_fit": atm_component,
        "cheapness": cheap_component,
        "high_iv_penalty": iv_penalty,
        "near_expiry_penalty": expiry_penalty,
        "low_liquidity_penalty": liquidity_penalty,
    }


def _score_from_components(components: Mapping[str, float]) -> float:
    positive = sum(
        float(components.get(key, 0.0))
        for key in (
            "momentum",
            "news",
            "event",
            "catalyst_strength",
            "relative_volume",
            "tight_spread",
            "open_interest",
            "option_volume",
            "delta_fit",
            "atm_fit",
            "cheapness",
        )
    )
    negative = sum(
        float(components.get(key, 0.0))
        for key in ("high_iv_penalty", "near_expiry_penalty", "low_liquidity_penalty")
    )
    return positive - negative


def _score_reason_codes(
    *,
    accepted: bool,
    selected: SelectedOptionContract | None,
    candidate: OptionContractCandidate,
    reason: str | None,
    signal: Any | None,
    as_of: date | None = None,
    components: Mapping[str, float] | None = None,
) -> tuple[str, ...]:
    codes: list[str] = []
    if accepted:
        dte = _days_to_expiry(candidate.expiration, as_of or date.today())
        if 7 <= dte <= 21:
            codes.append("dte_pref")
        elif dte < 7:
            codes.append("near_expiry")
        else:
            codes.append("dte_ok")
        if selected is not None and float(selected.spread_pct) <= 8.0:
            codes.append("spread_ok")
        if selected is not None and int(selected.open_interest) >= 500:
            codes.append("open_interest_ok")
        if selected is not None and int(selected.volume) >= 100:
            codes.append("volume_ok")
        if candidate.delta is not None and 0.40 <= abs(float(candidate.delta)) <= 0.55:
            codes.append("delta_pref")
        momentum = _signal_float(signal, "momentum_score", "conviction_score", "score", default=0.0) or 0.0
        news = _signal_float(signal, "news_score", default=0.0) or 0.0
        event = _signal_float(signal, "event_score", default=0.0) or 0.0
        catalyst_type = _signal_text(signal, "catalyst_type", "news_catalyst_type", "event_type")
        rel = _signal_float(signal, "relative_volume", default=0.0) or 0.0
        if momentum > 0:
            codes.append("momentum_boost")
        if news > 0 or event > 0:
            codes.append("news_event_boost")
        if news >= 7 or event >= 7 or _catalyst_type_score(catalyst_type) >= 7:
            codes.append("catalyst_boost")
        if rel > 0:
            codes.append("relative_volume_boost")
        return tuple(codes)
    text = str(reason or "").strip().lower()
    if not text:
        return ("rejected",)
    if "not tradable" in text or "contract_not_tradable" in text:
        codes.append("contract_not_tradable")
    if "unstable quote" in text or "stale quote" in text:
        codes.append("stale_quote")
    if "invalid bid/ask" in text or "missing bid" in text or "missing ask" in text:
        codes.append("missing_bid_ask")
    if "dte" in text:
        codes.append("dte")
    if "spread" in text:
        codes.append("spread")
    if "open_interest" in text:
        codes.append("open_interest")
    if "volume" in text:
        codes.append("volume")
    if "liquidity" in text:
        codes.append("liquidity")
    if "delta" in text or "greeks" in text:
        codes.append("delta")
    if "budget" in text or "premium" in text:
        codes.append("budget")
    if not codes:
        codes.append("rejected")
    return tuple(codes)


def _log_option_score_table(
    *,
    symbol: str,
    right: str,
    chain_rows: int,
    scored: Sequence[OptionContractScore],
    selected: OptionContractScore | None,
    as_of: date | None = None,
) -> None:
    accepted = sorted((s for s in scored if s.accepted), key=lambda s: (-s.score, float(s.selected.mid if s.selected else 0.0), str(s.candidate.symbol)))
    rejected = sorted((s for s in scored if not s.accepted), key=lambda s: (-s.score, float(s.candidate.strike), str(s.candidate.symbol)))
    log.info(
        "OPTIONS_CHAIN_SCANNED symbol=%s right=%s chain_rows=%d accepted=%d rejected=%d selected=%s",
        symbol,
        right,
        chain_rows,
        len(accepted),
        len(rejected),
        selected.selected.symbol if selected is not None and selected.selected is not None else "none",
    )
    if selected is not None and selected.selected is not None:
        selected_dte = _days_to_expiry(selected.selected.expiration, as_of or date.today())
        log.info(
            "OPTIONS_CONTRACT_SELECTED symbol=%s right=%s contract=%s score=%.2f reason_codes=%s",
            symbol,
            right,
            selected.selected.symbol,
            selected.score,
            ",".join(selected.reason_codes),
        )
        log.info(
            "OPTIONS_CONTRACT_SELECTION symbol=%s underlying=%s right=%s contract=%s result=accepted expiry=%s strike=%.4g delta=%s bid=%.4g ask=%.4g spread_pct=%.2f rejection_reason=none",
            symbol,
            symbol,
            right,
            selected.selected.symbol,
            selected.selected.expiration.isoformat(),
            float(selected.selected.strike),
            "n/a" if selected.candidate.delta is None else "%.4g" % float(selected.candidate.delta),
            float(selected.selected.bid),
            float(selected.selected.ask),
            float(selected.selected.spread_pct),
        )
        log.info(
            "OPTION_SELECTED symbol=%s right=%s contract=%s underlying=%s call_put=%s option_symbol=%s strike=%.4g expiry=%s expiration=%s dte=%d bid=%.4g ask=%.4g mid=%.4g premium=%.2f spread_pct=%.2f open_interest=%d volume=%d score=%.2f ranking_score=%.2f selected_reason=%s reason_codes=%s",
            symbol,
            right,
            selected.selected.symbol,
            symbol,
            right,
            selected.selected.symbol,
            float(selected.selected.strike),
            selected.selected.expiration.isoformat(),
            selected.selected.expiration.isoformat(),
            int(selected_dte),
            float(selected.selected.bid),
            float(selected.selected.ask),
            float(selected.selected.mid),
            float(selected.selected.mid) * 100.0,
            float(selected.selected.spread_pct),
            int(selected.selected.open_interest),
            int(selected.selected.volume),
            selected.score,
            selected.score,
            ",".join(selected.reason_codes) or "selected",
            ",".join(selected.reason_codes),
        )
    for rank, item in enumerate(accepted[:5], start=1):
        sel = item.selected
        comp = item.components
        log.info(
            "OPTIONS_CONTRACT_SCORE symbol=%s right=%s rank=%d contract=%s score=%.2f momentum=%.2f news=%.2f event=%.2f catalyst=%.2f rel_vol=%.2f spread=%.2f oi=%.0f vol=%.0f delta_fit=%.2f atm_fit=%.2f cheap=%.2f iv_penalty=%.2f near_expiry_penalty=%.2f liquidity_penalty=%.2f reason_codes=%s",
            symbol,
            right,
            rank,
            sel.symbol if sel is not None else item.candidate.symbol,
            item.score,
            float(comp.get("momentum", 0.0)),
            float(comp.get("news", 0.0)),
            float(comp.get("event", 0.0)),
            float(comp.get("catalyst_strength", 0.0)),
            float(comp.get("relative_volume", 0.0)),
            float(comp.get("tight_spread", 0.0)),
            float(comp.get("open_interest", 0.0)),
            float(comp.get("option_volume", 0.0)),
            float(comp.get("delta_fit", 0.0)),
            float(comp.get("atm_fit", 0.0)),
            float(comp.get("cheapness", 0.0)),
            float(comp.get("high_iv_penalty", 0.0)),
            float(comp.get("near_expiry_penalty", 0.0)),
            float(comp.get("low_liquidity_penalty", 0.0)),
            ",".join(item.reason_codes),
        )
    for rank, item in enumerate(rejected[:5], start=1):
        log.info(
            "OPTIONS_CONTRACT_REJECTED symbol=%s right=%s rank=%d contract=%s score=%.2f reason_codes=%s",
            symbol,
            right,
            rank,
            item.candidate.symbol,
            item.score,
            ",".join(item.reason_codes),
        )


def _primary_reject_reason(codes: Sequence[str]) -> str:
    code_set = {str(c or "").strip().lower() for c in codes}
    if "contract_not_tradable" in code_set:
        return "contract_not_tradable"
    if "stale_quote" in code_set or "unstable_quote" in code_set:
        return "stale_quote"
    if "missing_bid_ask" in code_set or "invalid_quote" in code_set:
        return "missing_bid_ask"
    if any(c.startswith("delta") or c == "delta" for c in code_set):
        return "delta_failed"
    if "budget" in code_set or "premium_over_budget" in code_set:
        return "premium_over_budget"
    if "spread" in code_set:
        return "spread_failed"
    if "liquidity" in code_set:
        return "liquidity_failed"
    if "dte" in code_set:
        return "dte_failed"
    return "rejected"


def _option_filter_reason(item: OptionContractScore) -> str:
    code_set = {str(c or "").strip().lower() for c in item.reason_codes}
    if (
        "contract_not_tradable" in code_set
        or "stale_quote" in code_set
        or "unstable_quote" in code_set
        or "missing_bid_ask" in code_set
        or "invalid_quote" in code_set
    ):
        return "quote_fail"
    if any(c.startswith("delta") or c == "delta" for c in code_set):
        return "delta_fail"
    if "budget" in code_set or "premium_over_budget" in code_set:
        return "budget_fail"
    if "spread" in code_set:
        return "spread_fail"
    if "open_interest" in code_set:
        return "open_interest_fail"
    if "volume" in code_set:
        return "volume_fail"
    if "liquidity" in code_set:
        return "liquidity_fail"
    if "dte" in code_set:
        return "expiry_fail"
    return "liquidity_fail"


def _option_filter_counts(
    scored: Sequence[OptionContractScore],
    *,
    extra_counts: Mapping[str, int] | None = None,
) -> dict[str, int]:
    counts = {key: 0 for key in OPTION_FILTER_COUNTER_KEYS}
    for key, value in (extra_counts or {}).items():
        if key in counts:
            counts[key] += int(value)
        elif key == "dte_failed":
            counts["expiry_fail"] += int(value)
        elif key == "premium_over_budget":
            counts["budget_fail"] += int(value)
        elif key == "spread_failed":
            counts["spread_fail"] += int(value)
        elif key == "delta_failed":
            counts["delta_fail"] += int(value)
        elif key in ("stale_quote", "missing_bid_ask", "contract_not_tradable"):
            counts["quote_fail"] += int(value)
    for item in scored:
        if item.accepted:
            continue
        counts[_option_filter_reason(item)] += 1
    return counts


def _options_funnel_stage_counts(
    *,
    chain_n: int,
    selected: int,
    scored: Sequence[OptionContractScore],
    extra_counts: Mapping[str, int] | None = None,
) -> dict[str, int]:
    """Return staged option-selection funnel counts without changing gate behavior."""
    filter_counts = _option_filter_counts(scored, extra_counts=extra_counts)
    primary_counts = {key: 0 for key in OPTION_REJECT_COUNTER_KEYS}
    for key, value in (extra_counts or {}).items():
        if key in primary_counts:
            primary_counts[key] += int(value)
    for item in scored:
        if item.accepted:
            continue
        reason = _primary_reject_reason(item.reason_codes)
        if reason in primary_counts:
            primary_counts[reason] += 1
    dte_rejects = int(filter_counts["expiry_fail"])
    delta_rejects = int(filter_counts["delta_fail"])
    budget_rejects = int(filter_counts["budget_fail"])
    spread_rejects = int(filter_counts["spread_fail"])
    volume_rejects = int(filter_counts["volume_fail"])
    oi_rejects = int(filter_counts["open_interest_fail"])

    contracts_after_dte = max(0, int(chain_n) - dte_rejects)
    contracts_after_delta = max(0, contracts_after_dte - delta_rejects)
    contracts_after_budget = max(0, contracts_after_delta - budget_rejects)
    contracts_after_spread = max(0, contracts_after_budget - spread_rejects)
    contracts_after_volume = max(0, contracts_after_spread - volume_rejects)
    contracts_after_open_interest = max(0, contracts_after_volume - oi_rejects)
    return {
        "chains_loaded": 1 if int(chain_n) > 0 else 0,
        "contracts_examined": int(chain_n),
        "contracts_after_dte": contracts_after_dte,
        "contracts_after_delta": contracts_after_delta,
        "contracts_after_budget": contracts_after_budget,
        "contracts_after_spread": contracts_after_spread,
        "contracts_after_volume": contracts_after_volume,
        "contracts_after_open_interest": contracts_after_open_interest,
        "contracts_selected": int(selected),
        "spread_rejects": spread_rejects,
        "volume_rejects": volume_rejects,
        "oi_rejects": oi_rejects,
        "open_interest_rejects": oi_rejects,
        "budget_rejects": budget_rejects,
        "dte_rejects": dte_rejects,
        "delta_rejects": delta_rejects,
        "quote_rejects": int(filter_counts["quote_fail"]),
        "stale_quote_rejects": int(primary_counts["stale_quote"]),
        "liquidity_rejects": int(filter_counts["liquidity_fail"]),
    }


def _log_options_funnel(
    *,
    symbol: str,
    chain_n: int,
    selected: int,
    scored: Sequence[OptionContractScore],
    extra_counts: Mapping[str, int] | None = None,
) -> None:
    """Emit canonical paper-options funnel counters for one chain scan."""
    filter_counts = _option_filter_counts(scored, extra_counts=extra_counts)
    stage_counts = _options_funnel_stage_counts(
        chain_n=chain_n,
        selected=selected,
        scored=scored,
        extra_counts=extra_counts,
    )
    log.info(
        "OPTIONS_FUNNEL underlying=%s underlyings_seen=%d chains_loaded=%d contracts_examined=%d contracts_after_dte=%d contracts_after_delta=%d contracts_after_budget=%d contracts_after_spread=%d contracts_after_volume=%d contracts_after_open_interest=%d contracts_rejected_quote=%d contracts_rejected_spread=%d contracts_rejected_volume=%d contracts_rejected_open_interest=%d contracts_rejected_delta=%d contracts_rejected_dte=%d spread_rejects=%d volume_rejects=%d oi_rejects=%d open_interest_rejects=%d budget_rejects=%d dte_rejects=%d delta_rejects=%d quote_rejects=%d stale_quote_rejects=%d contracts_selected=%d orders_submitted=%d orders_filled=%d orders_rejected=%d",
        str(symbol or "").strip().upper(),
        1 if str(symbol or "").strip() else 0,
        int(stage_counts["chains_loaded"]),
        int(stage_counts["contracts_examined"]),
        int(stage_counts["contracts_after_dte"]),
        int(stage_counts["contracts_after_delta"]),
        int(stage_counts["contracts_after_budget"]),
        int(stage_counts["contracts_after_spread"]),
        int(stage_counts["contracts_after_volume"]),
        int(stage_counts["contracts_after_open_interest"]),
        int(filter_counts["quote_fail"]),
        int(filter_counts["spread_fail"]),
        int(filter_counts["volume_fail"]),
        int(filter_counts["open_interest_fail"]),
        int(filter_counts["delta_fail"]),
        int(filter_counts["expiry_fail"]),
        int(stage_counts["spread_rejects"]),
        int(stage_counts["volume_rejects"]),
        int(stage_counts["oi_rejects"]),
        int(stage_counts["open_interest_rejects"]),
        int(stage_counts["budget_rejects"]),
        int(stage_counts["dte_rejects"]),
        int(stage_counts["delta_rejects"]),
        int(stage_counts["quote_rejects"]),
        int(stage_counts["stale_quote_rejects"]),
        int(stage_counts["contracts_selected"]),
        0,
        0,
        0,
    )


def _log_contract_reject_detail(
    *,
    symbol: str,
    item: OptionContractScore,
    as_of: date | None = None,
) -> None:
    c = item.candidate
    _mid, spread_pct = _mid_spread(float(c.bid), float(c.ask))
    log.info(
        "OPTIONS_CONTRACT_REJECT option_symbol=%s underlying=%s reason=%s expiry=%s strike=%.4g bid=%.4g ask=%.4g spread_pct=%.2f volume=%d open_interest=%d delta=%s dte=%d",
        str(c.symbol).strip().upper(),
        str(symbol or "").strip().upper(),
        _option_filter_reason(item),
        c.expiration.isoformat(),
        float(c.strike),
        float(c.bid),
        float(c.ask),
        float(spread_pct),
        int(c.volume),
        int(c.open_interest),
        "n/a" if c.delta is None else "%.4g" % float(c.delta),
        int(_days_to_expiry(c.expiration, as_of or date.today())),
    )


def _best_rejected_option(
    scored: Sequence[OptionContractScore],
    *,
    underlying_spot: float | None = None,
) -> OptionContractScore | None:
    rejected = [s for s in scored if not s.accepted]
    if not rejected:
        return None
    rejected.sort(
        key=lambda s: (
            abs(float(s.candidate.strike) - float(underlying_spot))
            if underlying_spot is not None
            else abs(float(s.candidate.strike)),
            _option_filter_reason(s),
            str(s.candidate.symbol),
        )
    )
    return rejected[0]


def _log_option_scan_summary(
    *,
    symbol: str,
    right: str | None = None,
    chain_n: int,
    selected: int,
    scored: Sequence[OptionContractScore],
    extra_counts: Mapping[str, int] | None = None,
    budget: float | None = None,
    underlying_spot: float | None = None,
    dte_range: tuple[int, int] | None = None,
    as_of: date | None = None,
) -> None:
    counts = {key: 0 for key in OPTION_REJECT_COUNTER_KEYS}
    for key, value in (extra_counts or {}).items():
        if key in counts:
            counts[key] += int(value)
    for item in scored:
        if item.accepted:
            continue
        reason = _primary_reject_reason(item.reason_codes)
        if reason in counts:
            counts[reason] += 1
    log.info(
        "OPTION_SCAN_SUMMARY symbol=%s chain_n=%d selected=%d spread_failed=%d liquidity_failed=%d premium_over_budget=%d delta_failed=%d dte_failed=%d stale_quote=%d missing_bid_ask=%d contract_not_tradable=%d",
        str(symbol or "").strip().upper(),
        int(chain_n),
        int(selected),
        counts["spread_failed"],
        counts["liquidity_failed"],
        counts["premium_over_budget"],
        counts["delta_failed"],
        counts["dte_failed"],
        counts["stale_quote"],
        counts["missing_bid_ask"],
        counts["contract_not_tradable"],
    )
    _log_options_funnel(
        symbol=symbol,
        chain_n=chain_n,
        selected=selected,
        scored=scored,
        extra_counts=extra_counts,
    )
    filter_counts = _option_filter_counts(scored, extra_counts=extra_counts)
    log.info(
        "OPTION_FILTER_SUMMARY symbol=%s chain_rows=%d selected=%d quote_fail=%d spread_fail=%d liquidity_fail=%d volume_fail=%d open_interest_fail=%d delta_fail=%d expiry_fail=%d budget_fail=%d",
        str(symbol or "").strip().upper(),
        int(chain_n),
        int(selected),
        filter_counts["quote_fail"],
        filter_counts["spread_fail"],
        filter_counts["liquidity_fail"],
        filter_counts["volume_fail"],
        filter_counts["open_interest_fail"],
        filter_counts["delta_fail"],
        filter_counts["expiry_fail"],
        filter_counts["budget_fail"],
    )
    best_rejected = _best_rejected_option(scored, underlying_spot=underlying_spot)
    if best_rejected is not None:
        c_best = best_rejected.candidate
        mid_best, spread_best = _mid_spread(float(c_best.bid), float(c_best.ask))
        log.info(
            "OPTION_BEST_REJECTED symbol=%s contract=%s strike=%.4g expiry=%s type=%s bid=%.4g ask=%.4g mid=%.4g spread_pct=%.2f delta=%s volume=%d open_interest=%d reject_reason=%s reason_codes=%s",
            str(symbol or "").strip().upper(),
            str(c_best.symbol).strip().upper(),
            float(c_best.strike),
            c_best.expiration.isoformat(),
            str(c_best.right or "").strip().lower(),
            float(c_best.bid),
            float(c_best.ask),
            float(mid_best),
            float(spread_best),
            "n/a" if c_best.delta is None else "%.4g" % float(c_best.delta),
            int(c_best.volume),
            int(c_best.open_interest),
            _option_filter_reason(best_rejected),
            ",".join(best_rejected.reason_codes),
        )
    rejected = sorted(
        (s for s in scored if not s.accepted),
        key=lambda s: (
            abs(float(s.candidate.strike) - float(underlying_spot))
            if underlying_spot is not None
            else abs(float(s.candidate.strike)),
            _primary_reject_reason(s.reason_codes),
            str(s.candidate.symbol),
        ),
    )
    for item in rejected[:10]:
        c = item.candidate
        mid, spread_pct = _mid_spread(float(c.bid), float(c.ask))
        premium = mid * 100.0 if mid > 0 else 0.0
        dte = _days_to_expiry(c.expiration, as_of or date.today())
        reason = _primary_reject_reason(item.reason_codes)
        log.info(
            "OPTION_REJECT_DETAIL symbol=%s contract=%s strike=%.4g expiry=%s type=%s bid=%.4g ask=%.4g mid=%.4g spread_pct=%.2f delta=%s volume=%d open_interest=%d premium=%.2f budget=%.2f reject_reason=%s",
            str(symbol or "").strip().upper(),
            str(c.symbol).strip().upper(),
            float(c.strike),
            c.expiration.isoformat(),
            str(c.right or "").strip().lower(),
            float(c.bid),
            float(c.ask),
            float(mid),
            float(spread_pct),
            "n/a" if c.delta is None else "%.4g" % float(c.delta),
            int(c.volume),
            int(c.open_interest),
            float(premium),
            float(budget or 0.0),
            reason,
        )
        log.info(
            "OPTION_NEAR_MISS underlying=%s contract=%s option_symbol=%s symbol=%s underlying_price=%s direction=%s strike=%.4g expiration=%s call_put=%s dte=%d bid=%.4g ask=%.4g mid=%.4g premium=%.2f estimated_cost=%.2f spread_pct=%.2f volume=%d open_interest=%d rejection_reason=%s",
            str(symbol or "").strip().upper(),
            str(c.symbol).strip().upper(),
            str(c.symbol).strip().upper(),
            str(symbol or "").strip().upper(),
            "n/a" if underlying_spot is None else "%.6g" % float(underlying_spot),
            str(right or "n/a").strip().lower(),
            float(c.strike),
            c.expiration.isoformat(),
            str(c.right or right or "").strip().lower(),
            int(dte),
            float(c.bid),
            float(c.ask),
            float(mid),
            float(premium),
            float(premium),
            float(spread_pct),
            int(c.volume),
            int(c.open_interest),
            reason,
        )
    for item in rejected:
        c = item.candidate
        mid, spread_pct = _mid_spread(float(c.bid), float(c.ask))
        premium = mid * 100.0 if mid > 0 else 0.0
        log.info(
            "OPTION_CANDIDATE_REJECT underlying=%s contract=%s option_symbol=%s symbol=%s underlying_price=%s direction=%s strike=%.4g expiration=%s call_put=%s dte=%d premium=%.2f estimated_cost=%.2f spread_pct=%.2f volume=%d open_interest=%d rejection_reason=%s reason=%s",
            str(symbol or "").strip().upper(),
            str(c.symbol).strip().upper(),
            str(c.symbol).strip().upper(),
            str(symbol or "").strip().upper(),
            "n/a" if underlying_spot is None else "%.6g" % float(underlying_spot),
            str(right or "n/a").strip().lower(),
            float(c.strike),
            c.expiration.isoformat(),
            str(c.right or right or "").strip().lower(),
            int(_days_to_expiry(c.expiration, as_of or date.today())),
            float(premium),
            float(premium),
            float(spread_pct),
            int(c.volume),
            int(c.open_interest),
            _primary_reject_reason(item.reason_codes),
            _option_filter_reason(item),
        )
        _log_contract_reject_detail(symbol=symbol, item=item, as_of=as_of)

    stage_counts = _options_funnel_stage_counts(
        chain_n=chain_n,
        selected=selected,
        scored=scored,
        extra_counts=extra_counts,
    )
    top_rejection_reason = "none"
    if rejected:
        reason_counts: dict[str, int] = {}
        for item in rejected:
            reason = _primary_reject_reason(item.reason_codes)
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        top_rejection_reason = sorted(reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    dte_range_used = (
        "%d-%d" % (int(dte_range[0]), int(dte_range[1]))
        if dte_range is not None
        else "n/a"
    )
    log.info(
        "OPTIONS_CHAIN_SUMMARY underlying=%s direction=%s chain_size=%d spot_price=%s dte_range_used=%s budget_used=%s selected_count=%d surviving_contracts=%d top_rejection_reason=%s chains_loaded=%d contracts_examined=%d contracts_after_dte=%d contracts_after_delta=%d contracts_after_budget=%d contracts_after_spread=%d contracts_after_volume=%d contracts_after_open_interest=%d contracts_selected=%d spread_rejects=%d volume_rejects=%d oi_rejects=%d open_interest_rejects=%d budget_rejects=%d dte_rejects=%d delta_rejects=%d quote_rejects=%d stale_quote_rejects=%d liquidity_rejects=%d",
        str(symbol or "").strip().upper(),
        str(right or "n/a").strip().lower(),
        int(chain_n),
        "n/a" if underlying_spot is None else "%.6g" % float(underlying_spot),
        dte_range_used,
        "n/a" if budget is None else "%.2f" % float(budget),
        int(stage_counts["contracts_selected"]),
        int(stage_counts["contracts_after_open_interest"]),
        top_rejection_reason,
        int(stage_counts["chains_loaded"]),
        int(stage_counts["contracts_examined"]),
        int(stage_counts["contracts_after_dte"]),
        int(stage_counts["contracts_after_delta"]),
        int(stage_counts["contracts_after_budget"]),
        int(stage_counts["contracts_after_spread"]),
        int(stage_counts["contracts_after_volume"]),
        int(stage_counts["contracts_after_open_interest"]),
        int(stage_counts["contracts_selected"]),
        int(stage_counts["spread_rejects"]),
        int(stage_counts["volume_rejects"]),
        int(stage_counts["oi_rejects"]),
        int(stage_counts["open_interest_rejects"]),
        int(stage_counts["budget_rejects"]),
        int(stage_counts["dte_rejects"]),
        int(stage_counts["delta_rejects"]),
        int(stage_counts["quote_rejects"]),
        int(stage_counts["stale_quote_rejects"]),
        int(stage_counts["liquidity_rejects"]),
    )


def _extra_dte_reject_count(
    config: dict[str, Any],
    *,
    intent_underlying: str,
    want_right: str,
    chain: Sequence[OptionContractCandidate],
    as_of: date,
) -> int:
    dte_min, dte_max = target_dte_bounds(config)
    count = 0
    for c in chain:
        if _symbol_underlying(c.symbol) != str(intent_underlying or "").upper():
            continue
        if _norm_right_candidate(c) != want_right:
            continue
        dte = _days_to_expiry(c.expiration, as_of)
        if dte < dte_min or dte > dte_max:
            count += 1
    return count


def _days_to_expiry(exp: date, as_of: date) -> int:
    return max(0, (exp - as_of).days)


def _mid_spread(bid: float, ask: float) -> tuple[float, float]:
    if bid <= 0 or ask <= 0:
        return 0.0, 999.0
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return 0.0, 999.0
    spread_pct = abs(ask - bid) / mid * 100.0
    return mid, spread_pct


def validate_option_liquidity(
    contract: SelectedOptionContract,
    config: dict[str, Any],
) -> tuple[bool, str]:
    """Return (ok, reason) using options spread cap (top-level or contract_selection)."""
    max_spread = float(max_bid_ask_spread_pct_cap(config))
    cs = _contract_selection_cfg(config)
    min_oi = int(cs.get("min_open_interest", 0))
    min_vol = int(cs.get("min_volume", 0))

    if contract.spread_pct > max_spread:
        return False, "spread %.2f%% > max %.2f%%" % (contract.spread_pct, max_spread)
    oi = int(contract.open_interest)
    # Brokers may omit OI in chain snapshots (we map missing → 0). Enforce min_oi only when OI is known.
    if min_oi > 0 and oi > 0 and oi < min_oi:
        return False, "open_interest %d < min %d" % (oi, min_oi)
    if contract.volume < min_vol:
        return False, "volume %d < min %d" % (contract.volume, min_vol)
    return True, "ok"


def _norm_right_candidate(c: OptionContractCandidate) -> str | None:
    cr = str(c.right or "").strip().lower()
    if cr in ("calls", "call"):
        return "call"
    if cr in ("puts", "put"):
        return "put"
    return None


def _normalize_intent_right(intent_right: str) -> tuple[str | None, str | None]:
    want_right = str(intent_right or "").strip().lower()
    if want_right in ("calls", "call"):
        return "call", None
    if want_right in ("puts", "put"):
        return "put", None
    return None, "invalid option right %r" % intent_right


def _filtered_option_candidates_for_intent(
    config: dict[str, Any],
    intent_underlying: str,
    want_right: str,
    candidates: Sequence[OptionContractCandidate],
    *,
    as_of: date,
) -> tuple[list[OptionContractCandidate], str | None, dict[str, int]]:
    """
    Same DTE / underlying / right filters as :func:`select_option_contract` (without ATM / spot).

    Returns (filtered, error_if_empty, stats).
    """
    u = str(intent_underlying or "").upper()
    dte_min, dte_max = target_dte_bounds(config)
    o_top = config.get("options") or {}

    filtered: list[OptionContractCandidate] = []
    n_chain = len(candidates)
    n_underlying = 0
    n_underlying_right = 0
    for c in candidates:
        if _symbol_underlying(c.symbol) != u:
            continue
        n_underlying += 1
        cr = _norm_right_candidate(c)
        if cr is None or cr != want_right:
            continue
        n_underlying_right += 1
        dte = _days_to_expiry(c.expiration, as_of)
        if not bool(o_top.get("allow_0dte", False)) and dte < 1:
            continue
        if not bool(o_top.get("allow_weeklies", True)):
            # Heuristic: short-dated Friday expiries treated as weeklies for filtering.
            if dte <= 7 and int(c.expiration.weekday()) == 4:
                continue
        if dte < dte_min or dte > dte_max:
            continue
        filtered.append(c)

    stats = {
        "n_chain": n_chain,
        "n_underlying": n_underlying,
        "n_underlying_right": n_underlying_right,
        "dte_min": dte_min,
        "dte_max": dte_max,
    }
    if not filtered:
        return [], (
            "no contracts in DTE window [%d, %d] days for %s %s | "
            "chain_rows=%d rows_matching_underlying=%d rows_matching_underlying_and_%s=%d rows_passing_dte=0"
            % (dte_min, dte_max, u, want_right, n_chain, n_underlying, want_right, n_underlying_right)
        ), stats
    return filtered, None, stats


def candidate_to_selected_contract(
    config: dict[str, Any],
    candidate: OptionContractCandidate,
    want_right: str,
) -> tuple[SelectedOptionContract | None, str | None]:
    """Build a :class:`SelectedOptionContract` from a chain row; enforce liquidity gates."""
    mid, sp = _mid_spread(candidate.bid, candidate.ask)
    if mid <= 0:
        return None, "invalid bid/ask mid for %s (bid=%s ask=%s)" % (candidate.symbol, candidate.bid, candidate.ask)
    if sp > 15.0:
        log.warning("Unstable quote %s", candidate.symbol)
        return None, "unstable quote for %s: spread %.2f%% > 15%%" % (candidate.symbol, sp)
    selected = SelectedOptionContract(
        symbol=str(candidate.symbol).strip().upper(),
        strike=float(candidate.strike),
        expiration=candidate.expiration,
        right=want_right,
        bid=float(candidate.bid),
        ask=float(candidate.ask),
        mid=mid,
        spread_pct=sp,
        open_interest=int(candidate.open_interest),
        volume=int(candidate.volume),
        iv=candidate.iv,
    )
    ok, liq_reason = validate_option_liquidity(selected, config)
    if not ok:
        return None, "liquidity check failed for %s: %s" % (selected.symbol, liq_reason)
    return selected, None


def lower_strike_candidates_same_series(
    config: dict[str, Any],
    intent_underlying: str,
    want_right: str,
    candidates: Sequence[OptionContractCandidate],
    reference: SelectedOptionContract,
    *,
    as_of: date | None = None,
    max_candidates: int | None = None,
) -> list[OptionContractCandidate]:
    """
    Same-expiry / same-right rows with **strictly lower** strike than ``reference``,
    ordered from the closest lower strike downward (typical chain step-down).

    Uses the same DTE window as :func:`select_option_contract`.
    """
    as_of_d = as_of or date.today()
    filtered, err, _ = _filtered_option_candidates_for_intent(
        config, intent_underlying, want_right, candidates, as_of=as_of_d
    )
    if err is not None or not filtered:
        return []
    ref_exp = reference.expiration
    ref_strike = float(reference.strike)
    subs = [
        c
        for c in filtered
        if c.expiration == ref_exp and _norm_right_candidate(c) == want_right and float(c.strike) < ref_strike
    ]
    subs.sort(key=lambda x: -float(x.strike))
    if max_candidates is not None and max_candidates > 0:
        subs = subs[: int(max_candidates)]
    return subs


def build_candidates(
    symbol: str,
    chain: Sequence[OptionContractCandidate],
    expiries: Sequence[date] | None,
    strikes: Sequence[float] | None,
    *,
    want_right: str,
    config: dict[str, Any],
    as_of: date,
) -> list[OptionContractCandidate]:
    """
    Filter broker *chain* to one underlying, *want_right*, DTE window, then optional
    expiry / strike subsets (``None`` = no extra filter).
    """
    filtered, err, _ = _filtered_option_candidates_for_intent(
        config, str(symbol).upper(), want_right, chain, as_of=as_of
    )
    if err is not None or not filtered:
        return []
    rows = list(filtered)
    if expiries:
        ex_set = set(expiries)
        rows = [c for c in rows if c.expiration in ex_set]
    if strikes:
        sk = [float(s) for s in strikes]

        def _near(c: OptionContractCandidate) -> bool:
            k = float(c.strike)
            return any(abs(k - s) < 1e-4 for s in sk)

        rows = [c for c in rows if _near(c)]
    return rows


def rank_candidates_atm_then_cheaper(
    candidates: Sequence[OptionContractCandidate],
    underlying_spot: float,
) -> list[OptionContractCandidate]:
    """
    Sort for sequential scan: closest strike to spot first, then lower **mid cost** per contract
    (helps when ATM is over budget but a nearby strike is cheaper).
    """
    spot = float(underlying_spot)
    rows = list(candidates)

    def _key(c: OptionContractCandidate) -> tuple[float, float, str]:
        mid, _sp = _mid_spread(c.bid, c.ask)
        mid_cost = mid * 100.0 if mid > 0 else float("inf")
        return (abs(float(c.strike) - spot), mid_cost, str(c.symbol))

    rows.sort(key=_key)
    return rows


def select_first_ranked_candidate_within_budget(
    config: dict[str, Any],
    *,
    intent_underlying: str,
    intent_right: str,
    chain: Sequence[OptionContractCandidate] | None,
    underlying_spot: float | None,
    equity: float,
    positions: list[dict[str, Any]] | None,
    as_of: date | None = None,
    signal: Any | None = None,
    expiries: Sequence[date] | None = None,
    strikes: Sequence[float] | None = None,
    premium_budget_cap_usd: float | None = None,
) -> tuple[SelectedOptionContract | None, str | None]:
    """
    Ranked scan over ``build_candidates`` → ``rank_candidates_atm_then_cheaper``:
    return the first row with ``mid×100 <= budget``, ``spread_pct <= max_spread``, and liquidity OK.

    If nothing qualifies, returns ``(None, reason)``.
    """
    chain_rows = 0 if chain is None else len(chain)
    log.info(
        "OPTION_SCAN_START symbol=%s right=%s chain_rows=%d path=ranked_budget spot=%s equity=%s",
        str(intent_underlying or "").strip().upper(),
        str(intent_right or "").strip().lower(),
        int(chain_rows),
        "n/a" if underlying_spot is None else "%.6g" % float(underlying_spot),
        "%.2f" % float(equity),
    )
    log.info(
        "OPTION_CHAIN_LOADED symbol=%s right=%s chain_rows=%d path=ranked_budget",
        str(intent_underlying or "").strip().upper(),
        str(intent_right or "").strip().lower(),
        int(chain_rows),
    )
    if chain is None:
        return None, "candidates is None (chain not passed from loop/broker)"
    if len(chain) == 0:
        return None, "candidates is empty (0 rows after broker chain fetch / quote filter)"

    want_right, bad = _normalize_intent_right(intent_right)
    if bad is not None or want_right is None:
        return None, bad or "invalid option right"

    cs = _contract_selection_cfg(config)
    moneyness = str(cs.get("moneyness", "ATM")).strip().upper()
    if moneyness != "ATM":
        return None, "moneyness %r not supported (v1: ATM only)" % moneyness

    if underlying_spot is None or float(underlying_spot) <= 0:
        return None, "underlying spot missing or non-positive for ATM (underlying_spot=%r)" % (
            underlying_spot,
        )

    as_of_d = as_of or date.today()
    budget, b_err = max_premium_budget_usd(config, equity=float(equity), positions=positions)
    if premium_budget_cap_usd is not None:
        try:
            cap = float(premium_budget_cap_usd)
        except (TypeError, ValueError):
            cap = float("nan")
        if cap == cap and cap > 0:
            budget = min(budget, cap)
    if budget <= 0:
        return None, b_err or "no premium budget for options"

    built = build_candidates(
        intent_underlying,
        chain,
        expiries,
        strikes,
        want_right=want_right,
        config=config,
        as_of=as_of_d,
    )
    if not built:
        want_for_dte = want_right
        _log_option_scan_summary(
            symbol=str(intent_underlying or "").upper(),
            right=want_for_dte,
            chain_n=len(chain),
            selected=0,
            scored=[],
            extra_counts={
                "dte_failed": _extra_dte_reject_count(
                    config,
                    intent_underlying=intent_underlying,
                    want_right=want_for_dte,
                    chain=chain,
                    as_of=as_of_d,
                )
            },
            budget=budget,
            underlying_spot=float(underlying_spot),
            dte_range=target_dte_bounds(config),
            as_of=as_of_d,
        )
        return None, "no candidates in DTE / expiry / strike filters"

    ranked = rank_candidates_atm_then_cheaper(built, float(underlying_spot))
    max_spread = float(max_bid_ask_spread_pct_cap(config))
    min_d = min_option_delta(config)
    max_d = max_option_delta(config)
    _need_greeks = (min_d is not None and min_d > 0) or (max_d is not None and max_d > 0)
    if _need_greeks and ranked and not any(c.delta is not None for c in ranked):
        return None, "options delta band set but chain rows have no delta/greeks (check options market data feed)"

    scored: list[OptionContractScore] = []
    for c in ranked:
        mid, sp = _mid_spread(c.bid, c.ask)
        if mid <= 0:
            scored.append(
                OptionContractScore(c, None, 0.0, False, ("missing_bid_ask",), {"mid": 0.0, "spread": sp})
            )
            continue
        if bool(getattr(c, "tradable", True)) is False:
            scored.append(
                OptionContractScore(c, None, 0.0, False, ("contract_not_tradable",), {"mid": mid, "spread": sp})
            )
            continue
        if sp > 15.0:
            log.warning("Unstable quote %s", c.symbol)
            scored.append(
                OptionContractScore(
                    c,
                    None,
                    0.0,
                    False,
                    ("unstable_quote",),
                    {"mid": mid, "spread": sp},
                )
            )
            continue
        if min_d is not None and min_d > 0:
            if c.delta is None:
                scored.append(
                    OptionContractScore(c, None, 0.0, False, ("delta_missing",), {"mid": mid, "spread": sp})
                )
                continue
            if abs(float(c.delta)) + 1e-9 < float(min_d):
                scored.append(
                    OptionContractScore(c, None, 0.0, False, ("delta_below_min",), {"mid": mid, "spread": sp})
                )
                continue
        if max_d is not None and max_d > 0:
            if c.delta is None:
                scored.append(
                    OptionContractScore(c, None, 0.0, False, ("delta_missing",), {"mid": mid, "spread": sp})
                )
                continue
            if abs(float(c.delta)) > float(max_d) + 1e-9:
                scored.append(
                    OptionContractScore(c, None, 0.0, False, ("delta_above_max",), {"mid": mid, "spread": sp})
                )
                continue
        mid_cost = mid * 100.0
        if mid_cost > budget + 1e-6:
            scored.append(
                OptionContractScore(
                    c,
                    None,
                    0.0,
                    False,
                    ("premium_over_budget",),
                    {"mid": mid, "spread": sp, "mid_cost": mid_cost},
                )
            )
            continue
        if sp > max_spread + 1e-9:
            scored.append(
                OptionContractScore(c, None, 0.0, False, ("spread",), {"mid": mid, "spread": sp})
            )
            continue
        sel, err = candidate_to_selected_contract(config, c, want_right)
        if sel is None:
            scored.append(
                OptionContractScore(
                    c,
                    None,
                    0.0,
                    False,
                    _score_reason_codes(
                        accepted=False,
                        selected=None,
                        candidate=c,
                        reason=err,
                        signal=signal,
                        as_of=as_of_d,
                    ),
                    {"mid": mid, "spread": sp},
                )
            )
            continue
        comps = _contract_score_components(
            selected=sel,
            candidate=c,
            signal=signal,
            underlying_spot=float(underlying_spot),
            as_of=as_of_d,
            max_spread=max_spread,
        )
        score = _score_from_components(comps)
        reason_codes = _score_reason_codes(
            accepted=True,
            selected=sel,
            candidate=c,
            reason=None,
            signal=signal,
            as_of=as_of_d,
            components=comps,
        )
        scored.append(
            OptionContractScore(
                c,
                sel,
                score,
                True,
                reason_codes,
                comps,
            )
        )

    accepted = [s for s in scored if s.accepted and s.selected is not None]
    selected_score: OptionContractScore | None = None
    if accepted:
        accepted.sort(
            key=lambda s: (
                -s.score,
                float(s.selected.mid if s.selected is not None else 0.0),
                abs(float(s.candidate.strike) - float(underlying_spot)),
                str(s.candidate.symbol),
            )
        )
        selected_score = accepted[0]
        _log_option_scan_summary(
            symbol=str(intent_underlying or "").upper(),
            right=want_right,
            chain_n=len(chain),
            selected=1,
            scored=scored,
            extra_counts={
                "dte_failed": _extra_dte_reject_count(
                    config,
                    intent_underlying=intent_underlying,
                    want_right=want_right,
                    chain=chain,
                    as_of=as_of_d,
                )
            },
            budget=budget,
            underlying_spot=float(underlying_spot),
            dte_range=target_dte_bounds(config),
            as_of=as_of_d,
        )
        _log_option_score_table(
            symbol=str(intent_underlying or "").upper(),
            right=want_right,
            chain_rows=len(scored),
            scored=scored,
            selected=selected_score,
            as_of=as_of_d,
        )
        return selected_score.selected, None

    _log_option_scan_summary(
        symbol=str(intent_underlying or "").upper(),
        right=want_right,
        chain_n=len(chain),
        selected=0,
        scored=scored,
        extra_counts={
            "dte_failed": _extra_dte_reject_count(
                config,
                intent_underlying=intent_underlying,
                want_right=want_right,
                chain=chain,
                as_of=as_of_d,
            )
        },
        budget=budget,
        underlying_spot=float(underlying_spot),
        dte_range=target_dte_bounds(config),
        as_of=as_of_d,
    )
    _log_option_score_table(
        symbol=str(intent_underlying or "").upper(),
        right=want_right,
        chain_rows=len(scored),
        scored=scored,
        selected=None,
        as_of=as_of_d,
    )
    return None, "no candidate within budget, spread, and liquidity gates"


def select_option_contract(
    config: dict[str, Any],
    intent_underlying: str,
    intent_right: str,
    *,
    candidates: Sequence[OptionContractCandidate] | None,
    underlying_spot: float | None,
    as_of: date | None = None,
    signal: Any | None = None,
) -> tuple[SelectedOptionContract | None, str | None]:
    """
    Filter by underlying, call/put, DTE window; pick ATM by closest strike to spot.

    If `candidates` is None or empty, returns (None, reason) — wire broker chain here.
    If `underlying_spot` is None, cannot do ATM — returns (None, reason).
    """
    chain_rows = 0 if candidates is None else len(candidates)
    log.info(
        "OPTION_SCAN_START symbol=%s right=%s chain_rows=%d path=atm_select spot=%s",
        str(intent_underlying or "").strip().upper(),
        str(intent_right or "").strip().lower(),
        int(chain_rows),
        "n/a" if underlying_spot is None else "%.6g" % float(underlying_spot),
    )
    log.info(
        "OPTION_CHAIN_LOADED symbol=%s right=%s chain_rows=%d path=atm_select",
        str(intent_underlying or "").strip().upper(),
        str(intent_right or "").strip().lower(),
        int(chain_rows),
    )
    if candidates is None:
        return None, "candidates is None (chain not passed from loop/broker)"
    if len(candidates) == 0:
        return None, "candidates is empty (0 rows after broker chain fetch / quote filter)"

    want_right, bad = _normalize_intent_right(intent_right)
    if bad is not None or want_right is None:
        return None, bad or "invalid option right"

    cs = _contract_selection_cfg(config)
    moneyness = str(cs.get("moneyness", "ATM")).strip().upper()
    if moneyness != "ATM":
        return None, "moneyness %r not supported (v1: ATM only)" % moneyness

    if underlying_spot is None or underlying_spot <= 0:
        return None, "underlying spot missing or non-positive for ATM (underlying_spot=%r)" % (underlying_spot,)

    as_of = as_of or date.today()
    filtered, filt_err, _ = _filtered_option_candidates_for_intent(
        config, str(intent_underlying or "").upper(), want_right, candidates, as_of=as_of
    )
    if filt_err is not None:
        return None, filt_err
    max_spread = float(max_bid_ask_spread_pct_cap(config))
    min_d = min_option_delta(config)
    max_d = max_option_delta(config)
    scored: list[OptionContractScore] = []
    for c in filtered:
        sel, err = candidate_to_selected_contract(config, c, want_right)
        if sel is None:
            scored.append(
                OptionContractScore(
                    c,
                    None,
                    0.0,
                    False,
                    _score_reason_codes(
                        accepted=False,
                        selected=None,
                        candidate=c,
                        reason=err,
                        signal=signal,
                        as_of=as_of,
                    ),
                    {},
                )
            )
            continue
        if bool(getattr(c, "tradable", True)) is False:
            scored.append(
                OptionContractScore(c, None, 0.0, False, ("contract_not_tradable",), {})
            )
            continue
        if min_d is not None and min_d > 0:
            if c.delta is None:
                scored.append(
                    OptionContractScore(c, None, 0.0, False, ("delta_missing",), {})
                )
                continue
            if abs(float(c.delta)) + 1e-9 < float(min_d):
                scored.append(
                    OptionContractScore(c, None, 0.0, False, ("delta_below_min",), {})
                )
                continue
        if max_d is not None and max_d > 0:
            if c.delta is None:
                scored.append(
                    OptionContractScore(c, None, 0.0, False, ("delta_missing",), {})
                )
                continue
            if abs(float(c.delta)) > float(max_d) + 1e-9:
                scored.append(
                    OptionContractScore(c, None, 0.0, False, ("delta_above_max",), {})
                )
                continue
        comps = _contract_score_components(
            selected=sel,
            candidate=c,
            signal=signal,
            underlying_spot=float(underlying_spot),
            as_of=as_of,
            max_spread=max_spread,
        )
        score = _score_from_components(comps)
        scored.append(
            OptionContractScore(
                c,
                sel,
                score,
                True,
                _score_reason_codes(
                    accepted=True,
                    selected=sel,
                    candidate=c,
                    reason=None,
                    signal=signal,
                    as_of=as_of,
                    components=comps,
                ),
                comps,
            )
        )

    accepted = [s for s in scored if s.accepted and s.selected is not None]
    if not accepted:
        _log_option_scan_summary(
            symbol=str(intent_underlying or "").upper(),
            right=want_right,
            chain_n=len(candidates),
            selected=0,
            scored=scored,
            extra_counts={
                "dte_failed": _extra_dte_reject_count(
                    config,
                    intent_underlying=intent_underlying,
                    want_right=want_right,
                    chain=candidates,
                    as_of=as_of,
                )
            },
            budget=None,
            underlying_spot=float(underlying_spot),
            dte_range=target_dte_bounds(config),
            as_of=as_of,
        )
        _log_option_score_table(
            symbol=str(intent_underlying or "").upper(),
            right=want_right,
            chain_rows=len(scored),
            scored=scored,
            selected=None,
            as_of=as_of,
        )
        return None, "no candidate within budget, spread, and liquidity gates"
    accepted.sort(
        key=lambda s: (
            -s.score,
            float(s.selected.mid if s.selected is not None else 0.0),
            abs(float(s.candidate.strike) - float(underlying_spot)),
            str(s.candidate.symbol),
        )
    )
    selected = accepted[0]
    _log_option_scan_summary(
        symbol=str(intent_underlying or "").upper(),
        right=want_right,
        chain_n=len(candidates),
        selected=1,
        scored=scored,
        extra_counts={
            "dte_failed": _extra_dte_reject_count(
                config,
                intent_underlying=intent_underlying,
                want_right=want_right,
                chain=candidates,
                as_of=as_of,
            )
        },
        budget=None,
        underlying_spot=float(underlying_spot),
        dte_range=target_dte_bounds(config),
        as_of=as_of,
    )
    _log_option_score_table(
        symbol=str(intent_underlying or "").upper(),
        right=want_right,
        chain_rows=len(scored),
        scored=scored,
        selected=selected,
        as_of=as_of,
    )
    return selected.selected, None


def _symbol_underlying(occ_symbol: str) -> str:
    """Best-effort OCC root (letters before first digit)."""
    s = str(occ_symbol or "").strip().upper()
    for i, ch in enumerate(s):
        if ch.isdigit():
            return s[:i] if i else s
    return s


def parse_occ_equity_option_symbol(occ_symbol: str) -> tuple[str, date, str, float] | None:
    """
    Parse a standard US equity OCC option symbol into root, expiry, right, strike.

    Returns (root, expiration, 'call'|'put', strike) or None if the string does not match.
    """
    s = str(occ_symbol or "").strip().upper()
    m = _OCC_OPTION_FULL_RE.match(s)
    if not m:
        return None
    root, yymmdd, cp, strike8 = m.groups()
    yy, mo, day = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6])
    year = 2000 + yy if yy < 70 else 1900 + yy
    try:
        exp = date(year, mo, day)
    except ValueError:
        return None
    right = "call" if cp == "C" else "put"
    strike = int(strike8) / 1000.0
    return root, exp, right, strike
