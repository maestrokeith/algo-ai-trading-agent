"""
Optional ``adaptive.*`` behaviour when the live loop shows many **valid** entry paths that still
do not fill (tight portfolio / market-quality / execution spread caps).
"""
from __future__ import annotations

from typing import Any

from src.portfolio_allocation import _regime_leg_for_cash_reserve

__all__ = [
    "adaptive_bump_streak",
    "adaptive_bucket_cap_mult",
    "adaptive_effective_max_total_exposure",
    "adaptive_strong_signal_for_cap_override",
    "cap_relax_factor_effective",
    "is_adaptive_cap_block_reason",
]


def _ad_cfg(config: dict[str, Any] | None) -> dict[str, Any]:
    raw = (config or {}).get("adaptive")
    return raw if isinstance(raw, dict) else {}


def _max_exposure_frac_ceiling(config: dict[str, Any] | None) -> float:
    """Upper bound for gross-book fraction (allows >1.0 for modest leverage / sync with portfolio caps)."""
    a = _ad_cfg(config)
    raw = a.get("max_exposure_frac_ceiling", 1.25)
    try:
        c = float(raw)
    except (TypeError, ValueError):
        c = 1.25
    if c != c:
        c = 1.25
    return max(1.0, min(2.0, c))


def is_adaptive_cap_block_reason(reason: str | None) -> bool:
    """
    True when a failed entry is plausibly due to a **cap** (book, symbol, sector, spread, etc.),
    not a soft strategy skip (e.g. cooldown, no bar signal). Used to advance the adaptive streak.
    """
    if reason is None or not str(reason).strip():
        return False
    s = str(reason).lower()
    if "cooldown" in s or "re-entry" in s or "profit booking" in s or "re-entry after profit" in s:
        return False
    if "not in universe" in s or "market closed" in s or "earnings" in s:
        return False
    if "pdt" in s or "day trade" in s or "compliance" in s:
        return False
    if "no entry signal" in s or "long-only" in s or "not eligible" in s:
        return False
    if "require new breakout" in s or "re-entry requires" in s:
        return False
    if "block" in s and "slippage" in s:
        return False
    if "blackout" in s:
        return False
    return any(
        k in s
        for k in (
            "cap",
            "spread",
            "market_quality",
            "exposure",
            "headroom",
            "bucket",
            "portfolio",
            "gross",
            "over cap",
            "at or above",
            "overexpos",
            "leaves no room",
            "book",
            "theme",
            "max open risk",
            "order build failed",
            "would exceed",
            "symbol ",
            "sector",
            "volatility",
            "dnt",
        )
    )


def cap_relax_factor_effective(
    *,
    config: dict[str, Any] | None,
    cap_block_streak: int,
    entry_strength: float | None = None,
    symbol_upper: str | None = None,
) -> float:
    """
    Base: when ``adaptive.relax_caps_if_entry_blocked`` is on and the cap-block streak
    (see :func:`adaptive_bump_streak`) is at least ``adaptive.min_consecutive_cap_blocks`` (default 2),
    return ``adaptive.relax_factor`` (≥ 1.0), else 1.0.

    When ``adaptive.allow_cap_override_on_strong_signal`` is on and :func:`adaptive_strong_signal_for_cap_override`
    is true for *entry_strength* / *symbol_upper*, multiply that base (clamped ≥ 1.0) by
    ``adaptive.override_multiplier`` (≥ 1.0) so strong signals get extra spread / exposure / sizing headroom
    even without a long cap-block streak.
    """
    a = _ad_cfg(config)
    base = 1.0
    if a.get("relax_caps_if_entry_blocked", False):
        try:
            need = int(a.get("min_consecutive_cap_blocks", 2))
        except (TypeError, ValueError):
            need = 2
        if need < 1:
            need = 1
        if int(cap_block_streak) >= need:
            try:
                f = float(a.get("relax_factor", 1.0))
            except (TypeError, ValueError):
                f = 1.0
            if f < 1.0:
                f = 1.0
            base = f
    if adaptive_strong_signal_for_cap_override(
        config, entry_strength=entry_strength, symbol_upper=symbol_upper
    ):
        try:
            om = float(a.get("override_multiplier", 1.0))
        except (TypeError, ValueError):
            om = 1.0
        if om < 1.0:
            om = 1.0
        return max(1.0, float(base)) * float(om)
    return base


def adaptive_bucket_cap_mult(
    config: dict[str, Any] | None,
    *,
    regime_condition: str | None = None,
    regime_score: int | None = None,
) -> float:
    """
    Multiplier for **per-bucket** long-stock MV / equity cap (``risk.*``) by market regime.

    Reads ``adaptive.bucket_cap_multiplier`` (``bullish`` / ``neutral`` / ``bearish``). The engine uses
    ``"defensive"`` for the lowest bucket; the YAML key ``bearish`` is a synonym (``defensive`` is tried
    first, then ``bearish``). If ``regime_condition`` is a known name, that wins; otherwise
    ``regime_score`` maps: ``>=4`` bullish, ``>=2`` neutral, else defensive. Missing or unknown keys
    in the table → ``1.0`` (no change to the base cap).
    """
    a = _ad_cfg(config)
    raw = a.get("bucket_cap_multiplier")
    if not isinstance(raw, dict) or not raw:
        return 1.0

    def _parse_one(key: str) -> float | None:
        v = raw.get(key)
        if v is None or str(v).strip() == "":
            return None
        try:
            m = float(v)
        except (TypeError, ValueError):
            return None
        if m <= 0:
            return None
        return min(3.0, max(0.01, m))

    def _defensive_value() -> float | None:
        m = _parse_one("defensive")
        if m is not None:
            return m
        return _parse_one("bearish")

    c = (regime_condition or "").strip().lower() if regime_condition else None
    if c == "bearish":
        c = "defensive"
    if c in ("bullish", "neutral", "defensive"):
        if c == "defensive":
            m = _defensive_value()
        else:
            m = _parse_one(c)
        return 1.0 if m is None else m

    if regime_score is not None:
        try:
            s = int(regime_score)
        except (TypeError, ValueError):
            return 1.0
        if s >= 4:
            m = _parse_one("bullish")
        elif s >= 2:
            m = _parse_one("neutral")
        else:
            m = _defensive_value()
        return 1.0 if m is None else m
    return 1.0


def adaptive_strong_signal_for_cap_override(
    config: dict[str, Any] | None,
    *,
    entry_strength: float | None,
    symbol_upper: str | None,
) -> bool:
    """
    True when ``adaptive.allow_cap_override_on_strong_signal`` is on, *entry_strength* meets
    ``adaptive.min_strength_for_cap_override`` (default 0.82), and optional
    ``adaptive.cap_override_relief_symbols`` includes the symbol (or the list is omitted/empty
    for all tickers).
    """
    a = _ad_cfg(config)
    if not a.get("allow_cap_override_on_strong_signal", False):
        return False
    if entry_strength is None or entry_strength != entry_strength:  # nan
        return False
    try:
        es = float(entry_strength)
    except (TypeError, ValueError):
        return False
    if es != es:
        return False
    try:
        mn = float(a.get("min_strength_for_cap_override", 0.82))
    except (TypeError, ValueError):
        mn = 0.82
    if es + 1e-12 < mn:
        return False
    raw_syms = a.get("cap_override_relief_symbols")
    if (
        raw_syms is not None
        and isinstance(raw_syms, (list, tuple, set))
        and len(raw_syms) > 0
    ):
        want = {str(x).strip().upper() for x in raw_syms if x and str(x).strip()}
        su = str(symbol_upper or "").strip().upper()
        if not su or su not in want:
            return False
    return True


def adaptive_effective_max_total_exposure(
    config: dict[str, Any] | None,
    *,
    base_max_total_exposure_frac: float,
    regime_score: int | None = None,
    regime_condition: str | None = None,
    entry_wave_strong_signal_count: int | None = None,
) -> float:
    """
    Final **fractional** cap for total gross long book vs equity used by
    :func:`src.exposure_gates.block_new_entries_total_exposure`.

    If ``adaptive.max_exposure_by_regime`` is a non-empty dict, resolve
    **bullish** / **neutral** / **bearish** the same way as
    :func:`portfolio_allocation.effective_min_cash_reserve_frac` (condition label wins;
    else score ≥4 bullish, ≥2 neutral, else bearish; ``defensive`` → **bearish** for lookup).
    Read per-leg cap from the table; missing key for that leg falls back to *base_max_total_exposure_frac*.
    The ``defensive`` key in YAML is a synonym of ``bearish`` (``bearish`` tried first).

    Optional ``adaptive.regime_<n>`` (e.g. ``regime_3``) with a ``max_exposure`` key applies to
    that **exact** ``regime_score``: the effective cap is the **greater** of the leg from
    ``max_exposure_by_regime`` and that value (so 0.92 is a **floor** when the leg is lower;
    a leg of 0.95 is unchanged if ``regime_3`` is 0.92) (0–1).

    If ``adaptive.boost_exposure_if_many_signals`` is true and
    *entry_wave_strong_signal_count* is ≥ ``adaptive.signal_threshold`` (default 5), add
    ``adaptive.boost_amount`` to the cap (capped at ``adaptive.max_exposure_frac_ceiling``).

    When ``adaptive.bullish_score_4_plus_max_exposure_frac`` is set and regime score is ≥ 4 with a
    **bullish** leg (:func:`~src.portfolio_allocation._regime_leg_for_cash_reserve`), the effective
    cap is at least that fraction (e.g. 1.10 for 110% gross vs equity).
    """
    a = _ad_cfg(config)
    ceil = _max_exposure_frac_ceiling(config)
    try:
        b = float(base_max_total_exposure_frac)
    except (TypeError, ValueError):
        b = 0.9
    if b != b:
        b = 0.9
    b = max(0.0, min(ceil, b))
    eff = b

    raw = a.get("max_exposure_by_regime")
    if isinstance(raw, dict) and raw:

        def _one(key: str) -> float | None:
            v = raw.get(key) if key in raw else None
            if v is None or str(v).strip() == "":
                return None
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            if f != f:
                return None
            return max(0.0, min(ceil, f))

        leg = _regime_leg_for_cash_reserve(regime_condition, regime_score)
        r_cap: float | None = None
        if leg is not None:
            r_cap = _one(leg)
            if r_cap is None and leg == "bearish":
                m = _one("defensive")
                if m is not None:
                    r_cap = m
        if r_cap is not None:
            eff = r_cap

    if regime_score is not None:
        try:
            rs = int(regime_score)
        except (TypeError, ValueError):
            rs = -1
        if rs >= 0:
            sub = a.get(f"regime_{rs}")
            if isinstance(sub, dict) and sub.get("max_exposure") is not None:
                try:
                    rs_cap = float(sub.get("max_exposure"))
                except (TypeError, ValueError):
                    rs_cap = None
                if rs_cap is not None and rs_cap == rs_cap:
                    rsv = max(0.0, min(ceil, float(rs_cap)))
                    eff = max(0.0, min(ceil, max(eff, rsv)))

    bs4 = a.get("bullish_score_4_plus_max_exposure_frac")
    if bs4 is not None and str(bs4).strip() != "":
        try:
            rs_i = int(regime_score) if regime_score is not None else -1
        except (TypeError, ValueError):
            rs_i = -1
        if rs_i >= 4:
            leg_s = _regime_leg_for_cash_reserve(regime_condition, regime_score)
            if leg_s == "bullish":
                try:
                    bsv = float(bs4)
                except (TypeError, ValueError):
                    bsv = None
                if bsv is not None and bsv == bsv:
                    bsv = max(0.0, min(ceil, float(bsv)))
                    eff = max(eff, bsv)

    if a.get("boost_exposure_if_many_signals", False):
        try:
            th = int(a.get("signal_threshold", 5))
        except (TypeError, ValueError):
            th = 5
        th = max(0, th)
        try:
            boost = float(a.get("boost_amount", 0.02))
        except (TypeError, ValueError):
            boost = 0.02
        boost = max(0.0, boost) if boost == boost else 0.0
        n: int | None = None
        if entry_wave_strong_signal_count is not None:
            try:
                n = max(0, int(entry_wave_strong_signal_count))
            except (TypeError, ValueError):
                n = 0
        if n is not None and n >= th:
            eff = min(ceil, eff + boost)
    return max(0.0, min(ceil, eff))


def adaptive_bump_streak(
    config: dict[str, Any] | None,
    streak: int,
    decision: Any | None,
) -> int:
    """
    After an entry *TradeDecision* from the engine, return an updated cap-block
    streak (0 when adaptive is off, when entry is allowed, or on non–cap failure).
    """
    a = _ad_cfg(config)
    if not a.get("relax_caps_if_entry_blocked", False):
        return 0
    if decision is None:
        return int(streak)
    if bool(decision.allowed):
        return 0
    r = decision.reason
    if is_adaptive_cap_block_reason(r if isinstance(r, str) else (str(r) if r is not None else None)):
        return int(streak) + 1
    return 0
