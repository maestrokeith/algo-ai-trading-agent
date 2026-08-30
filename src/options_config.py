"""
Resolve ``options`` YAML: canonical keys, legacy aliases, and percent/fraction conventions.

* ``per_trade``: preferred alias for one-order premium budget vs equity (same fraction rules as
  ``max_premium_pct_of_equity``).
* ``max_premium_pct_of_equity``: values ``<= 1`` are **fractions of equity** (``0.02`` → 2%%); values **> 1**
  are **percent points** (``2`` → 2%%).
* ``total_exposure_limit`` / ``max_options_notional_pct``: preferred aliases for aggregate long-premium
  vs equity; values ``<= 1`` are fractions (``0.20`` → 20%%); values **> 1** are percent points
  (``20`` → 20%%). Implemented in :func:`src.portfolio_allocation.effective_options_total_cap_frac`.
* ``max_bid_ask_spread_pct``: values **strictly below 1** are **fraction of mid × 100** (``0.015`` → 1.5%%);
  values ``>= 1`` are **percent** in the same units as :func:`options_selector._mid_spread` (``5`` → 5%%).
* ``enable_only_if_gross_below`` — alias for the gross gate (see :func:`options_entry_environment_blocks`).
* ``require_top_signal`` — alias for ``top_signals_only``.
* ``max_option_positions`` — alias for ``max_open_option_positions`` / ``max_positions``.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _opts(config: dict[str, Any] | None) -> dict[str, Any]:
    o = (config or {}).get("options")
    return o if isinstance(o, dict) else {}


def allow_new_entries(config: dict[str, Any] | None) -> bool:
    """New long-premium buys allowed (exits use ``options.exits.automation_enabled``)."""
    o = _opts(config)
    if "new_entries_enabled" in o:
        return bool(o.get("new_entries_enabled"))
    return bool(o.get("allow_new_entries", True))


def options_enabled(config: dict[str, Any] | None) -> bool:
    """Top-level options kill switch. ``enabled=false`` blocks all option orders."""
    return bool(_opts(config).get("enabled", False))


def options_mode(config: dict[str, Any] | None) -> str:
    """Configured options mode; defaults to ``paper_only`` for promotion safety."""
    return str(_opts(config).get("mode") or "paper_only").strip().lower()


def _live_pilot_flag(o: Mapping[str, Any]) -> bool:
    nested = o.get("live_pilot")
    if isinstance(nested, Mapping) and "enabled" in nested:
        return bool(nested.get("enabled"))
    return bool(o.get("live_pilot_enabled", False))


def options_paper_only(config: dict[str, Any] | None) -> bool:
    """True unless config explicitly opts into a non-paper mode."""
    return options_mode(config) == "paper_only"


def live_options_explicitly_enabled(config: dict[str, Any] | None) -> bool:
    """
    Live options are off unless both mode is live-capable and an explicit live flag is set.

    This is intentionally stricter than ``options.enabled`` so paper experiments cannot
    accidentally activate live option orders.
    """
    o = _opts(config)
    mode = options_mode(config)
    return _live_pilot_flag(o) and mode in {
        "long_premium_only",
        "live",
        "live_long_premium",
    }


def options_live_pilot_enabled(config: dict[str, Any] | None) -> bool:
    """Dedicated live-options pilot flag; false keeps live options disabled."""
    o = _opts(config)
    return _live_pilot_flag(o) and options_mode(config) in {
        "live",
        "live_long_premium",
        "long_premium_only",
    }


def options_ordering_allowed(
    config: dict[str, Any] | None,
    *,
    broker_is_paper: bool,
) -> tuple[bool, str | None]:
    """Common kill-switch/mode validation before any option order path."""
    if not options_enabled(config):
        return False, "options.enabled is false"
    mode = options_mode(config)
    if broker_is_paper:
        if mode not in {"paper_only", "long_premium_only", "scan_only", "shadow_live"}:
            return False, "options.mode %r is not paper-capable" % mode
        return True, None
    if not live_options_explicitly_enabled(config):
        return False, "live options not explicitly enabled"
    if not options_live_pilot_enabled(config):
        return False, "live options pilot is disabled"
    return True, None


def fallback_to_stock(config: dict[str, Any] | None) -> bool:
    """When options routing does not place an order, run the stock leg if true."""
    port = (config or {}).get("portfolio")
    if isinstance(port, dict):
        ca = port.get("capital_allocator")
        if isinstance(ca, dict) and "allow_stock_fallback_if_options_fail" in ca:
            return bool(ca.get("allow_stock_fallback_if_options_fail"))
        al = port.get("allocator")
        if isinstance(al, dict) and "allow_stock_fallback_if_options_fail" in al:
            return bool(al.get("allow_stock_fallback_if_options_fail"))
    o = _opts(config)
    if "allow_fallback_to_shares" in o:
        return bool(o.get("allow_fallback_to_shares"))
    return bool(o.get("fallback_to_stock", True))


def _fraction_or_percent_to_frac(raw: Any, *, default_frac: float) -> float:
    """``(0,1]`` → fraction; ``>1`` → percent/100; else default."""
    if raw is None or str(raw).strip() == "":
        return default_frac
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default_frac
    if v <= 0:
        return default_frac
    if v <= 1.0:
        return min(1.0, v)
    return min(1.0, v / 100.0)


def max_premium_frac_of_equity(config: dict[str, Any] | None) -> float:
    """
    Fraction of equity usable as premium budget for one new trade (before per-trade $ cap).

    Prefers ``options.per_trade``; else ``options.max_premium_pct_of_equity``; else
    ``options.risk_per_trade_pct`` / 100.
    """
    o = _opts(config)
    if o.get("per_trade") is not None and str(o.get("per_trade")).strip() != "":
        return _fraction_or_percent_to_frac(o.get("per_trade"), default_frac=0.02)
    if o.get("max_premium_pct_of_equity") is not None and str(o.get("max_premium_pct_of_equity")).strip() != "":
        return _fraction_or_percent_to_frac(o.get("max_premium_pct_of_equity"), default_frac=0.02)
    raw = o.get("risk_per_trade_pct")
    if raw is None or str(raw).strip() == "":
        return 0.02
    try:
        return max(0.0, min(1.0, float(raw) / 100.0))
    except (TypeError, ValueError):
        return 0.02


def max_premium_per_trade_usd(config: dict[str, Any] | None) -> float | None:
    """Hard ceiling in dollars on premium for one order; ``None`` if unset."""
    o = _opts(config)
    raw = o.get("max_premium_per_trade")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return v


def target_dte_bounds(config: dict[str, Any] | None) -> tuple[int, int]:
    """``(min_days, max_days)`` inclusive DTE window for contract selection."""
    o = _opts(config)
    cs = o.get("contract_selection") if isinstance(o.get("contract_selection"), dict) else {}
    _dmin_alias = o.get("target_dte_min")
    if _dmin_alias is None or str(_dmin_alias).strip() == "":
        _dmin_alias = o.get("min_dte")
    if _dmin_alias is not None and str(_dmin_alias).strip() != "":
        try:
            dmin = max(0, int(_dmin_alias))
        except (TypeError, ValueError):
            dmin = int(cs.get("expiry_min_days", 14))
    else:
        dmin = int(cs.get("expiry_min_days", 14))
    _dmax_alias = o.get("target_dte_max")
    if _dmax_alias is None or str(_dmax_alias).strip() == "":
        _dmax_alias = o.get("max_dte")
    if _dmax_alias is not None and str(_dmax_alias).strip() != "":
        try:
            dmax = max(dmin, int(_dmax_alias))
        except (TypeError, ValueError):
            dmax = int(cs.get("expiry_max_days", 35))
    else:
        dmax = int(cs.get("expiry_max_days", 35))
    return dmin, dmax


def conviction_band_from_entry_strength(strength: float | None) -> str | None:
    """Map entry strength (0–1 or 0–100) to weak / medium / strong (same bands as equity sizing)."""
    if strength is None:
        return None
    try:
        s = float(strength)
    except (TypeError, ValueError):
        return None
    if s > 1.0 and s <= 100.0:
        s = s / 100.0
    elif s > 100.0:
        s = 1.0
    s = max(0.0, min(1.0, s))
    if s >= 0.67:
        return "strong"
    if s <= 0.33:
        return "weak"
    return "medium"


def _conviction_band_label(raw: Any) -> str | None:
    if raw is None or str(raw).strip() == "":
        return None
    s = str(raw).strip().lower()
    if s in ("weak", "low"):
        return "weak"
    if s in ("medium", "med"):
        return "medium"
    if s in ("strong", "high"):
        return "strong"
    return None


def _conviction_rank_from_band(band: str | None) -> int | None:
    if band is None:
        return None
    return {"weak": 0, "medium": 1, "strong": 2}.get(band, None)


def options_conviction_required_min_rank(config: dict[str, Any] | None) -> int | None:
    """
    Minimum entry conviction rank for option routing when ``options.conviction_required`` is set.

    Accepts ``low``/``weak``, ``medium``, ``high``/``strong``. ``high`` is an alias for ``strong``.
    """
    o = _opts(config)
    return _conviction_rank_from_band(_conviction_band_label(o.get("conviction_required")))


def options_conviction_entry_allowed(config: dict[str, Any] | None, signal: Any) -> tuple[bool, str | None]:
    """
    When ``conviction_required`` is set and the signal carries ``conviction_band`` or
    ``conviction_score``, require signal rank >= configured floor. Missing band+score → allow
    (e.g. bear-ETF path without strength).
    """
    need = options_conviction_required_min_rank(config)
    if need is None:
        return True, None
    raw_band = getattr(signal, "conviction_band", None)
    band = _conviction_band_label(raw_band) if raw_band is not None else None
    if band is None:
        band = conviction_band_from_entry_strength(getattr(signal, "conviction_score", None))
    got = _conviction_rank_from_band(band)
    if got is None:
        return True, None
    if got >= need:
        return True, None
    req_label = str((_opts(config) or {}).get("conviction_required") or "").strip().lower()
    sig_label = band or "n/a"
    return False, "options conviction_required %r not met (signal %s)" % (req_label, sig_label)


def _score_value(raw: Any, *, unit_interval_scale: bool = False) -> float | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    if unit_interval_scale and 0.0 <= v <= 1.0:
        return v
    return v


def dynamic_options_entry_eligible(
    config: dict[str, Any] | None,
    *,
    scanner_score: Any = None,
    news_score: Any = None,
    catalyst_score: Any = None,
) -> tuple[bool, str]:
    """Paper dynamic options eligibility gate before chain evaluation."""
    o = _opts(config)
    dyn = o.get("dynamic_entry")
    dyn = dyn if isinstance(dyn, Mapping) else {}
    scanner_min = float(dyn.get("min_scanner_score", 50.0) or 50.0)
    news_min = float(dyn.get("min_news_score", 8.0) or 8.0)
    catalyst_min = float(dyn.get("min_catalyst_score", 0.70) or 0.70)
    scanner = _score_value(scanner_score)
    news = _score_value(news_score)
    catalyst = _score_value(catalyst_score, unit_interval_scale=True)
    if scanner is not None and scanner >= scanner_min:
        return True, "scanner_score"
    if news is not None and news >= news_min:
        return True, "news_score"
    if catalyst is not None and catalyst >= catalyst_min:
        return True, "catalyst_score"
    return (
        False,
        "dynamic_options_weak_signal scanner_score=%s<%.2f news_score=%s<%.2f catalyst_score=%s<%.2f"
        % (
            "n/a" if scanner is None else "%.2f" % scanner,
            scanner_min,
            "n/a" if news is None else "%.2f" % news,
            news_min,
            "n/a" if catalyst is None else "%.2f" % catalyst,
            catalyst_min,
        ),
    )


def paper_dynamic_options_spread_cap(config: dict[str, Any] | None, *, broker_is_paper: bool) -> float | None:
    """Optional wider paper-only spread cap for eligible dynamic option selection."""
    if not broker_is_paper:
        return None
    o = _opts(config)
    if options_mode(config) != "paper_only":
        return None
    dyn = o.get("dynamic_entry")
    dyn = dyn if isinstance(dyn, Mapping) else {}
    relax = dyn.get("paper_spread_relaxation")
    relax = relax if isinstance(relax, Mapping) else {}
    if not bool(relax.get("enabled", False)):
        return None
    raw = relax.get("max_bid_ask_spread_pct", relax.get("max_spread_pct", o.get("max_bid_ask_spread_pct")))
    if raw is None or str(raw).strip() == "":
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return v * 100.0 if v < 1.0 else v


def min_option_delta(config: dict[str, Any] | None) -> float | None:
    """Absolute delta floor for contract selection; ``None`` if unset or invalid."""
    o = _opts(config)
    raw = o.get("min_delta")
    if raw is None or str(raw).strip() == "":
        raw = o.get("target_delta_min")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return v


def max_option_delta(config: dict[str, Any] | None) -> float | None:
    """Upper bound on ``abs(delta)`` when greeks exist; ``None`` if unset (no cap)."""
    o = _opts(config)
    raw = o.get("max_delta")
    if raw is None or str(raw).strip() == "":
        raw = o.get("target_delta_max")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return v


def never_bypass_stock_risk_caps(config: dict[str, Any] | None) -> bool:
    """
    When true (default): option buys must pass the same strict bucket allocation check as stocks
    (no top-signal bucket waiver, no cross-bucket headroom), and the portfolio-full options bypass
    path is disabled — see :func:`src.trend_long_ranked_dispatch.dispatch_trend_long_after_buying_power`.
    """
    o = _opts(config)
    return bool(o.get("never_bypass_stock_risk_caps", True))


def options_entry_environment_blocks(
    config: dict[str, Any] | None,
    *,
    gross_exposure_pct: float | None,
    reduce_only: bool,
    regime_score: int | None = None,
) -> tuple[bool, str | None]:
    """
    When ``(True, reason)``, new long-option entries are skipped (trend-long may still buy stock).

    * ``disable_if_gross_exposure_above`` — fraction (0.80) or percent points (80) vs ``gross_exposure_pct``.
    * ``enable_only_if_gross_below`` — alias: same threshold, block when gross is **not** below this
      (equivalent to ``disable_if_gross_exposure_above``). Ignored if ``disable_if_gross_exposure_above`` is set.
    * ``disable_if_reduce_only`` — block when the live loop is in reduce-only mode.
    * ``disable_if_over_exposed`` — block when :func:`~src.risk_limits.gross_exposure_tier` is not ``normal``.
    * ``min_regime_score_for_entries`` — block when the current regime score is below this floor.
    """
    o = _opts(config)
    if not o:
        return False, None
    raw_min_regime = o.get("min_regime_score_for_entries")
    if raw_min_regime is not None and str(raw_min_regime).strip() != "":
        try:
            min_regime = int(float(raw_min_regime))
        except (TypeError, ValueError):
            min_regime = None
        if min_regime is not None:
            try:
                rs = int(regime_score) if regime_score is not None else None
            except (TypeError, ValueError):
                rs = None
            if rs is None or rs < min_regime:
                return True, "options off: regime_score %s < min %d" % (
                    "n/a" if rs is None else str(rs),
                    min_regime,
                )
    g = None
    if gross_exposure_pct is not None:
        try:
            gf = float(gross_exposure_pct)
            if gf == gf:
                g = gf
        except (TypeError, ValueError):
            g = None
    raw_thr = o.get("disable_if_gross_exposure_above")
    if raw_thr is None or str(raw_thr).strip() == "":
        raw_thr = o.get("enable_only_if_gross_below")
    if g is not None and raw_thr is not None and str(raw_thr).strip() != "":
        try:
            thr = float(raw_thr)
        except (TypeError, ValueError):
            thr = None
        if thr is not None and thr == thr:
            thr_pct = thr * 100.0 if thr <= 1.0 else thr
            if g > thr_pct + 1e-9:
                return True, "options off: gross %.1f%% > cap %.1f%%" % (g, thr_pct)
    if bool(o.get("disable_if_reduce_only", False)) and reduce_only:
        return True, "options off: reduce_only mode"
    if bool(o.get("disable_if_over_exposed", False)) and g is not None:
        from src.risk_limits import gross_exposure_tier

        tier = gross_exposure_tier(float(g), dict(config) if config else None)
        if tier != "normal":
            return True, "options off: gross tier %s (disable_if_over_exposed)" % tier
    return False, None


def max_open_option_positions_cap(config: dict[str, Any] | None) -> int:
    """
    Max simultaneous long option lines (distinct OCC symbols). Prefers ``max_positions``,
    then ``max_option_positions``, then ``max_open_option_positions``; defaults to ``1``.
    """
    o = _opts(config)
    for key in ("max_positions", "max_option_positions", "max_open_option_positions"):
        if key not in o or o.get(key) is None or str(o.get(key)).strip() == "":
            continue
        try:
            return max(0, int(o[key]))
        except (TypeError, ValueError):
            continue
    return 1


def max_bid_ask_spread_pct_cap(config: dict[str, Any] | None) -> float:
    """
    Max bid/ask vs mid as **percent** (same units as :func:`options_selector._mid_spread` ``spread_pct``).

    Prefers top-level ``options.max_bid_ask_spread_pct``, then ``contract_selection.max_bid_ask_spread_pct``,
    default ``5.0``. Values in ``(0, 1]`` are treated as **fraction of mid** → multiplied by 100.
    """
    o = _opts(config)
    cs = o.get("contract_selection") if isinstance(o.get("contract_selection"), dict) else {}
    raw = o.get("max_bid_ask_spread_pct")
    if raw is None or str(raw).strip() == "":
        raw = cs.get("max_bid_ask_spread_pct", 5.0)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 5.0
    if v <= 0:
        return 5.0
    if v < 1.0:
        return v * 100.0
    return v


def trend_long_options_top_signals_only_passes(
    config: Mapping[str, Any] | None,
    row_tl: Mapping[str, Any],
) -> bool:
    """
    When ``options.top_signals_only`` or ``options.require_top_signal`` is true, allow options routing
    only if the row was tagged as a ranked top signal (``row_tl['in_top_signals']``, see winner
    allocation / ranked flush).

    When false or unset, returns True (no extra gate).
    """
    o = _opts(config)
    need_top = bool(o.get("top_signals_only", False)) or bool(
        o.get("require_top_signal", False)
    )
    if not need_top:
        return True
    return bool(row_tl.get("in_top_signals"))


def effective_entry_strength_for_options_fallback(
    row_tl: Mapping[str, Any],
    decision: Any,
    strength_jitter_max: float,
) -> float | None:
    """Prefer ranked ``strength_eff``; else jittered entry signal strength."""
    from src.portfolio_replacement import effective_signal_strength

    se = row_tl.get("strength_eff")
    if se is not None:
        try:
            return float(se)
        except (TypeError, ValueError):
            pass
    es = getattr(getattr(decision, "entry_signal", None), "strength", None)
    if es is None:
        return None
    try:
        base = float(es)
    except (TypeError, ValueError):
        return None
    return float(effective_signal_strength(base, float(strength_jitter_max or 0.0)))


def _merged_bypass_when_full_options(o: dict[str, Any]) -> dict[str, Any]:
    """
    Merge legacy ``portfolio_full_strong_signal_options`` with ``bypass_when_full``;
    latter wins on key conflicts.
    """
    legacy = o.get("portfolio_full_strong_signal_options")
    new = o.get("bypass_when_full")
    m: dict[str, Any] = {}
    if isinstance(legacy, dict):
        m.update(legacy)
    if isinstance(new, dict):
        m.update(new)
    return m


def portfolio_full_strong_signal_small_call_cap_usd(
    config: Mapping[str, Any] | None,
    *,
    max_port_positions: int,
    n_eligible_long_stocks: int,
    symbol_upper: str,
    current_position_keys: Mapping[str, Any],
    row_tl: Mapping[str, Any],
    decision: Any,
    strength_jitter_max: float,
    account_equity: float,
) -> tuple[bool, float | None]:
    """
    When at max **equity** distinct longs and the incoming symbol is new, if signal strength clears
    ``min_signal_strength``, return ``(True, cap_usd)`` for a second options pass with
    ``premium_budget_cap_usd=cap_usd`` (controlled bypass of equity caps via long premium).

    Config (merged from ``options.bypass_when_full`` and legacy
    ``options.portfolio_full_strong_signal_options``):

    * ``allow_when_full`` (preferred) or legacy ``enabled``
    * ``max_option_allocation_per_trade`` — fraction of equity (``0.04``) or percent points (``4``)
      per trade ceiling for this bypass path (typical **3–5%%** band)
    * ``max_premium_usd``, ``max_premium_pct_of_equity`` — optional extra ceilings (``min()`` applies)

    Returns ``(False, None)`` when this escape hatch does not apply.
    """
    o = _opts(config)
    sub = _merged_bypass_when_full_options(o)
    if not sub:
        return False, None
    allow = bool(sub.get("allow_when_full", sub.get("enabled", False)))
    if not allow:
        return False, None
    if max_port_positions >= 10**9:
        return False, None
    if n_eligible_long_stocks < max_port_positions:
        return False, None
    su = str(symbol_upper).strip().upper()
    if not su or su in current_position_keys:
        return False, None
    try:
        min_st = float(sub.get("min_signal_strength", 0.82) or 0.82)
    except (TypeError, ValueError):
        min_st = 0.82
    eff = effective_entry_strength_for_options_fallback(
        row_tl, decision, strength_jitter_max
    )
    if eff is None or eff + 1e-12 < min_st:
        return False, None
    caps: list[float] = []
    raw_alloc = sub.get("max_option_allocation_per_trade")
    if raw_alloc is not None and str(raw_alloc).strip() != "":
        af = _fraction_or_percent_to_frac(raw_alloc, default_frac=0.0)
        if af > 0:
            caps.append(float(account_equity) * af)
    raw_usd = sub.get("max_premium_usd")
    if raw_usd is not None and str(raw_usd).strip() != "":
        try:
            u = float(raw_usd)
            if u > 0:
                caps.append(u)
        except (TypeError, ValueError):
            pass
    raw_pct = sub.get("max_premium_pct_of_equity")
    if raw_pct is not None and str(raw_pct).strip() != "":
        try:
            p = float(raw_pct)
            if p > 1.0:
                p = p / 100.0
            caps.append(float(account_equity) * max(0.0, min(1.0, p)))
        except (TypeError, ValueError):
            pass
    if not caps:
        caps.append(float(account_equity) * 0.04)
    cap_usd = min(caps)
    if cap_usd <= 0:
        return False, None
    return True, cap_usd
