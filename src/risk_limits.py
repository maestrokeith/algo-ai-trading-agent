"""
Optional ``risk.*`` limits for the live trend-long entry path.

Allocation inputs use **fractions of equity** in YAML by default: ``0.10`` means 10%.
Values ``> 1`` are treated as **percent points** (``10`` → 10%). Strings like ``"10%"`` work.

**Bucket exposure** (``risk.max_bucket_allocation_pct``): sums long stock ``market_value`` for
symbols sharing the same bucket key. Optional top-level ``risk_buckets`` maps bucket names to
ticker lists; symbols not in any list use fallback keys ``tier_0`` … ``tier_3`` (same tiers as
signal ranking: SPY/QQQ, NVDA/MSFT, sector ETFs, other).

**Mega-cap beta bucket** (``risk.mega_cap_beta_cap``): when set, overrides ``max_bucket_allocation_pct``
for the ``mega_cap_beta`` bucket only (same fraction / percent-point parsing as other risk caps).

**Per-tier cap** (``tier_0_cap`` … ``tier_N_cap``): optional; when a symbol falls into fallback key
``tier_<n>`` and ``risk.tier_<n>_cap`` is set, that fraction overrides ``max_bucket_allocation_pct`` for
that tier only.

**Regime-scaled bucket cap** (``adaptive.bucket_cap_multiplier``): optional multipliers on the **effective**
per-bucket cap by regime (``bullish`` / ``neutral`` / ``bearish``). Wired from the live loop using
:func:`adaptive.adaptive_bucket_cap_mult` and :func:`risk_effective_max_bucket_allocation_frac_for_bucket`.

**Top-signal bucket pass** (``execution.allow_bucket_override_for_top_signals`` + ``top_signal_percentile``):
when the entry would break the per-bucket cap, allow if :func:`execution.execution_bucket_top_signal_qualified`
is true for this symbol's ``strength_eff`` vs the current ranked cohort.

**Cross-bucket rebalance** (``portfolio.allocator.allow_cross_bucket_rebalance``, optional
``portfolio.capital_allocator`` override for the key): the effective cap for a target bucket is the
sleeve's own cap plus the sum of unused long-stock dollar headroom in **other** buckets, so a full
sleeve can still add when other sleeves (e.g. ``mega_cap_beta``) are below their cap.

**Sector sleeve cap (SYMBOL_SECTOR book vs equity)**: set ``portfolio.max_sector_pct`` (preferred) and/or
``risk.sector_cap_pct`` (legacy). When both are set, the **stricter (lower) %** wins, same as single-name caps;
when only one is set, that value applies. Otherwise
``position_sizing.max_exposure_per_sector_pct`` is the fallback (see
:func:`effective_max_sector_sleeve_pct`). Values use the same rules as other allocation keys (fraction, ``"30%"``, or percent points).

**Hold-time symbol cap** (``risk.enforce_position_caps_on_hold`` + ``risk.rebalance_on_breach``):
when both are true, the live loop may trim long equity when MV / equity exceeds the merged per-name
cap plus ``risk.rebalance_threshold_pct`` (percent points above the cap, ``0`` = trim on any overage).

**Add-on cadence** (``risk.max_adds_per_symbol_per_day``): operator alias ``max_addons_per_day`` —
same per-symbol ET-day cap; canonical key wins when both are set.

**Gross over-exposure bands** (``risk.over_exposure_levels``): optional ``mild`` / ``high`` / ``critical`` as
gross long MV / equity **fractions** (e.g. ``0.95`` = 95%). :func:`gross_exposure_tier` classifies the
book; ``high`` is the default threshold for :func:`resolve_reduce_only_gross_frac` (overrides
``portfolio.exposure_gates.overexposed_reduce_only_gross_frac`` when the map is set).

**No-recycle band** (``risk.no_recycle_above_pct``): when book gross (fraction of equity) is **strictly
above** this value, :func:`risk_no_recycle_blocks_allocator_buys` is true and the post-scan capital
allocator does not submit **buy** legs (see :func:`src.portfolio.allocator.run_post_scan_capital_allocator`).
Omit the key to leave the check disabled.
"""
from __future__ import annotations

from typing import Any

from src.adaptive import adaptive_bucket_cap_mult
from src.execution import execution_bucket_top_signal_qualified
from src.exposure import ETF_SYMBOLS
from src.options_premium_risk import is_option_symbol
from src.portfolio_allocation import max_allocation_per_symbol_pct
from src.position_tracker import minutes_since_iso, tracked_row_has_open_long
from src.signal_ranking import symbol_signal_priority_tier


def parse_allocation_fraction(raw: Any) -> float:
    """Return a fraction in ``[0, 1]``, or ``0`` if unset/invalid."""
    if raw is None or str(raw).strip() == "":
        return 0.0
    s = str(raw).strip()
    if s.endswith("%"):
        try:
            return max(0.0, min(1.0, float(s[:-1].strip()) / 100.0))
        except ValueError:
            return 0.0
    try:
        v = float(s)
    except (TypeError, ValueError):
        return 0.0
    if v < 0:
        return 0.0
    if v <= 1.0:
        return min(1.0, v)
    return min(1.0, v / 100.0)


def _risk_cfg(config: dict[str, Any] | None) -> dict[str, Any]:
    r = (config or {}).get("risk")
    return r if isinstance(r, dict) else {}


def parse_risk_over_exposure_levels(
    config: dict[str, Any] | None,
) -> dict[str, float]:
    """
    Return ``risk.over_exposure_levels`` as gross long MV / equity **fractions** (0.95 = 95%).

    Keys ``mild`` / ``high`` / ``critical`` default to 0.95 / 1.0 / 1.05. Enforces
    ``mild <= high <= critical`` after parse.
    """
    out: dict[str, float] = {"mild": 0.95, "high": 1.0, "critical": 1.05}
    r = _risk_cfg(config)
    raw = r.get("over_exposure_levels")
    if not isinstance(raw, dict):
        return out
    for key, default in (("mild", 0.95), ("high", 1.0), ("critical", 1.05)):
        v = raw.get(key)
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f == f and f >= 0.0:
            out[key] = f
    if out["mild"] > out["high"]:
        out["mild"] = out["high"]
    if out["high"] > out["critical"]:
        out["high"] = out["critical"]
    if out["mild"] > out["high"]:
        out["mild"] = out["high"]
    return out


def risk_no_recycle_above_frac(config: dict[str, Any] | None) -> float | None:
    """
    ``risk.no_recycle_above_pct`` as gross long MV / equity **fraction** (``0.94`` = 94%% of equity),
    or ``None`` if the key is omitted/empty (feature off).

    Uses the same rules as :func:`parse_allocation_fraction` (``0.94`` vs ``94``).
    """
    r = _risk_cfg(config)
    raw = r.get("no_recycle_above_pct")
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        return None
    f = parse_allocation_fraction(raw)
    if f <= 0.0 or f != f:
        return None
    return min(1.0, f)


def risk_no_recycle_blocks_allocator_buys(
    gross_exposure_pct: float,
    config: dict[str, Any] | None,
) -> bool:
    """
    True when the book is **strictly** above the no-recycle band: gross (%% of equity) / 100
    ``>`` ``risk_no_recycle_above_frac``. Omitted *no_recycle_above_pct* → never blocks.

    This is used to set ``allocator_disable_buys`` for the post-scan capital allocator
    (no new buys / adds while over the band; sells/rotation can still be planned).
    """
    thr = risk_no_recycle_above_frac(config)
    if thr is None or thr <= 0.0:
        return False
    g = max(0.0, float(gross_exposure_pct)) / 100.0
    return g > thr + 1e-12


def resolve_reduce_only_gross_frac(config: dict[str, Any] | None) -> float:
    """
    Gross / equity **fraction** above which reduce-only / over-exposed mode applies.

    If ``risk.over_exposure_levels`` is set with a ``high`` value, that wins; else
    ``portfolio.exposure_gates.overexposed_reduce_only_gross_frac`` (default ``1.0``).
    """
    r = _risk_cfg(config)
    oel = r.get("over_exposure_levels")
    if isinstance(oel, dict):
        h = oel.get("high")
        if h is not None and str(h).strip() != "":
            try:
                hf = float(h)
            except (TypeError, ValueError):
                pass
            else:
                if hf == hf and hf >= 0.0:
                    return min(10.0, max(0.0, hf))
    port = (config or {}).get("portfolio") or {}
    eg = port.get("exposure_gates")
    if isinstance(eg, dict):
        v = eg.get("overexposed_reduce_only_gross_frac")
        if v is not None and str(v).strip() != "":
            try:
                vf = float(v)
            except (TypeError, ValueError):
                return 1.0
            if vf == vf and vf >= 0.0:
                return min(10.0, max(0.0, vf))
    return 1.0


def gross_exposure_tier(
    gross_pct: float, config: dict[str, Any] | None
) -> str:
    """
    One of ``normal`` / ``mild`` / ``high`` / ``critical`` from :func:`parse_risk_over_exposure_levels`
    and *gross_pct* (0–100+ scale, from :func:`src.exposure.compute_exposures`).
    """
    lv = parse_risk_over_exposure_levels(config)
    try:
        g = float(gross_pct) / 100.0
    except (TypeError, ValueError):
        return "normal"
    if g != g:
        return "normal"
    m, h, c = lv["mild"], lv["high"], lv["critical"]
    if g + 1e-12 < m:
        return "normal"
    if g + 1e-12 < h:
        return "mild"
    if g + 1e-12 < c:
        return "high"
    return "critical"


def risk_max_symbol_allocation_frac(
    config: dict[str, Any] | None,
    *,
    symbol_upper: str | None = None,
) -> float:
    raw = _risk_cfg(config).get("max_symbol_allocation_pct")
    if isinstance(raw, dict):
        sym_u = str(symbol_upper or "").strip().upper()
        selected = None
        if sym_u in ETF_SYMBOLS:
            selected = raw.get("etf")
        if selected is None or str(selected).strip() == "":
            selected = raw.get("default")
        if selected is None or str(selected).strip() == "":
            selected = raw.get("base")
        return parse_allocation_fraction(selected)
    return parse_allocation_fraction(raw)


def risk_max_bucket_allocation_frac(config: dict[str, Any] | None) -> float:
    return parse_allocation_fraction(_risk_cfg(config).get("max_bucket_allocation_pct"))


def risk_max_bucket_allocation_frac_for_bucket(config: dict[str, Any] | None, bucket_key: str) -> float:
    """
    Equity fraction cap for *bucket_key* long-stock MV / equity.

    The ``mega_cap_beta`` bucket may use ``risk.mega_cap_beta_cap`` when set (and > 0 after parse);
    fallback ``tier_<n>`` keys may use ``risk.tier_<n>_cap`` when set; otherwise
    ``risk.max_bucket_allocation_pct`` applies.
    """
    r = _risk_cfg(config)
    bk = str(bucket_key or "").strip()
    if bk == "mega_cap_beta":
        raw = r.get("mega_cap_beta_cap")
        if raw is not None and str(raw).strip() != "":
            parsed = parse_allocation_fraction(raw)
            if parsed > 0:
                return parsed
    if bk.startswith("tier_") and len(bk) > 5:
        suf = bk[5:]
        if suf.isdigit():
            _cap_n = r.get("tier_%s_cap" % suf)
            if _cap_n is not None and str(_cap_n).strip() != "":
                parsed_t = parse_allocation_fraction(_cap_n)
                if parsed_t > 0:
                    return parsed_t
    return risk_max_bucket_allocation_frac(config)


def risk_effective_max_bucket_allocation_frac_for_bucket(
    config: dict[str, Any] | None,
    bucket_key: str,
    *,
    regime_condition: str | None = None,
    regime_score: int | None = None,
) -> float:
    """
    Per-bucket equity fraction cap after optional ``adaptive.bucket_cap_multiplier`` (regime) scaling.

    The base is :func:`risk_max_bucket_allocation_frac_for_bucket`; result is
    ``min(1, base * adaptive_bucket_cap_mult(...))``.
    """
    base = risk_max_bucket_allocation_frac_for_bucket(config, bucket_key)
    if base <= 0:
        return 0.0
    m = adaptive_bucket_cap_mult(
        config,
        regime_condition=regime_condition,
        regime_score=regime_score,
    )
    return min(1.0, max(0.0, base * m))


def risk_max_adds_per_symbol_per_day(config: dict[str, Any] | None) -> int:
    """
    Max add-on fills per symbol per ET calendar day (``0`` = unlimited).

    Prefers ``risk.max_adds_per_symbol_per_day``; if unset or empty, uses ``risk.max_addons_per_day``.
    """
    r = _risk_cfg(config)
    raw = r.get("max_adds_per_symbol_per_day")
    if raw is None or str(raw).strip() == "":
        raw = r.get("max_addons_per_day")
    if raw is None or str(raw).strip() == "":
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def risk_min_minutes_between_adds(config: dict[str, Any] | None) -> float:
    raw = _risk_cfg(config).get("min_minutes_between_adds")
    if raw is None or str(raw).strip() == "":
        return 0.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def risk_max_new_positions_per_cycle(
    config: dict[str, Any] | None,
    *,
    regime_score: int | None = None,
) -> int:
    """Prefer regime override, then ``alpha.max_new_positions_per_cycle``, else ``risk.*``."""
    if regime_score is not None:
        try:
            rs = int(regime_score)
        except (TypeError, ValueError):
            rs = None
        if rs is not None and isinstance(config, dict):
            reg = config.get("regime")
            reg = reg if isinstance(reg, dict) else {}
            reg_bucket = reg.get(f"score_{rs}")
            reg_bucket = reg_bucket if isinstance(reg_bucket, dict) else {}
            raw_reg = reg_bucket.get("max_new_positions")
            if raw_reg is not None and str(raw_reg).strip() != "":
                try:
                    return max(0, int(raw_reg))
                except (TypeError, ValueError):
                    pass
    if isinstance(config, dict):
        a = config.get("alpha")
        if isinstance(a, dict):
            raw_a = a.get("max_new_positions_per_cycle")
            if raw_a is not None and str(raw_a).strip() != "":
                try:
                    return max(0, int(raw_a))
                except (TypeError, ValueError):
                    pass
    raw = _risk_cfg(config).get("max_new_positions_per_cycle")
    if raw is None or str(raw).strip() == "":
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def risk_enforce_position_caps_on_hold(config: dict[str, Any] | None) -> bool:
    """When true, the live loop evaluates merged per-symbol %% caps on existing longs (see breach trim)."""
    return bool(_risk_cfg(config).get("enforce_position_caps_on_hold", False))


def risk_rebalance_on_breach(config: dict[str, Any] | None) -> bool:
    """When true with :func:`risk_enforce_position_caps_on_hold`, submit trim sells on cap breach."""
    return bool(_risk_cfg(config).get("rebalance_on_breach", False))


def risk_rebalance_threshold_pct(config: dict[str, Any] | None) -> float:
    """
    Extra percent-of-equity headroom above the merged symbol cap before a cap rebalance trim fires.

    Example: cap ``8%%`` and threshold ``1.0`` → trim when position MV / equity ``> 9%%``.
    ``0`` trims as soon as MV is above the cap (subject to float tolerance).
    """
    raw = _risk_cfg(config).get("rebalance_threshold_pct")
    if raw is None or str(raw).strip() == "":
        return 0.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def symbol_allocation_breach_trim_shares(
    *,
    equity: float,
    position_market_value_usd: float,
    qty: int,
    mid_price: float,
    cap_pct: float,
    rebalance_threshold_pct: float,
) -> int:
    """
    Whole-share count to sell so the line moves back toward the cap, when over
    ``cap_pct + rebalance_threshold_pct`` (both in percent-of-equity points).

    Returns ``0`` when inputs are invalid, cap is off, or there is no breach.
    """
    eq = float(equity)
    if eq <= 0.0 or cap_pct <= 0.0:
        return 0
    q = int(qty)
    px = float(mid_price)
    if q <= 0 or px <= 0.0:
        return 0
    mv = float(position_market_value_usd)
    if mv <= 0.0:
        return 0
    pos_pct = mv / eq * 100.0
    trigger_pct = float(cap_pct) + max(0.0, float(rebalance_threshold_pct))
    if pos_pct <= trigger_pct + 1e-9:
        return 0
    cap_usd = eq * (float(cap_pct) / 100.0)
    excess_usd = mv - cap_usd
    if excess_usd <= 0.0:
        return 0
    raw_sh = int(excess_usd / px)
    return min(max(1, raw_sh), q)


def symbol_caps_regime_override_allocation_frac(
    config: dict[str, Any] | None, regime_score: int | None
) -> float | None:
    """
    Return ``portfolio.capital_allocator.symbol_caps.regime_<n>`` as an equity **fraction** ``(0,1]``,
    or ``None`` if unset. Used to relax (or tighten) the risk per-name line by regime so e.g. score 4
    can use 15% while ``risk.max_symbol_allocation_pct`` stays 9.5%.
    """
    if config is None or regime_score is None:
        return None
    p = (config or {}).get("portfolio")
    if not isinstance(p, dict):
        return None
    ca = p.get("capital_allocator")
    if not isinstance(ca, dict):
        return None
    sc = ca.get("symbol_caps")
    if not isinstance(sc, dict):
        return None
    key = "regime_%d" % int(regime_score)
    raw = sc.get(key)
    if raw is None or str(raw).strip() == "":
        return None
    f = float(parse_allocation_fraction(str(raw).strip()))
    if f != f or f <= 0.0:
        return None
    return max(0.0, min(1.0, f))


def effective_symbol_allocation_cap_pct(
    config: dict[str, Any] | None,
    *,
    account_equity: float | None = None,
    regime_score: int | None = None,
    symbol_upper: str | None = None,
) -> float:
    """
    Merge ``portfolio`` symbol cap with ``risk.max_symbol_allocation_pct``;
    the **stricter** (lower) cap wins. Returns percent points ``0``–``100`` (``0`` = off).

    When ``portfolio.capital_allocator.symbol_caps.regime_<n>`` is set and *regime_score* matches
    *n*, that **fraction of equity** replaces ``risk.max_symbol_allocation_pct`` for this merge
    (so bullish regime 4 can allow 15% concentration while the default risk line remains 9.5%).

    When ``portfolio.symbol_allocation_cap`` is ``dynamic``, pass *account_equity* so the
    cap reflects ``min_trade_size_usd`` and ``floor_pct`` (see ``portfolio_allocation``).
    """
    port_pct = float(max_allocation_per_symbol_pct(config, account_equity=account_equity))
    r_override = symbol_caps_regime_override_allocation_frac(config, regime_score)
    if r_override is not None and r_override > 0.0:
        risk_pct = r_override * 100.0
    else:
        risk_pct = risk_max_symbol_allocation_frac(
            config,
            symbol_upper=symbol_upper,
        ) * 100.0
    if risk_pct <= 0 and port_pct <= 0:
        return 0.0
    if risk_pct <= 0:
        return port_pct
    if port_pct <= 0:
        return risk_pct
    return min(port_pct, risk_pct)


def effective_max_sector_sleeve_pct(config: dict[str, Any] | None) -> float:
    """
    Per-``SYMBOL_SECTOR`` long-book cap as percent of equity (percent points, e.g. ``30`` = 30%%).

    * If both ``portfolio.max_sector_pct`` and ``risk.sector_cap_pct`` are set: **min (stricter wins).**
    * If only ``risk.sector_cap_pct`` is set: that value (legacy: does not merge with
      ``position_sizing.max_exposure_per_sector_pct`` the way ``min`` would; risk alone wins).
    * If only ``portfolio.max_sector_pct`` is set: that value.
    * Otherwise: ``position_sizing.max_exposure_per_sector_pct`` (code default 40.0; YAML often 30).
    """
    ps = (config or {}).get("position_sizing") or {}
    try:
        base = float(ps.get("max_exposure_per_sector_pct", 40.0))
    except (TypeError, ValueError):
        base = 40.0
    port = (config or {}).get("portfolio") or {}
    rsk = (config or {}).get("risk") or {}
    raw_p = port.get("max_sector_pct")
    raw_r = rsk.get("sector_cap_pct")
    p_pp: float | None = None
    r_pp: float | None = None
    if raw_p is not None and str(raw_p).strip() != "":
        f = parse_allocation_fraction(raw_p) * 100.0
        if f > 0.0:
            p_pp = float(f)
    if raw_r is not None and str(raw_r).strip() != "":
        try:
            scv = float(raw_r)
        except (TypeError, ValueError):
            scv = 0.0
        if scv > 0.0:
            r_pp = float(scv * 100.0) if scv <= 1.0 else float(scv)
    if r_pp is not None and p_pp is not None:
        return min(r_pp, p_pp)
    if r_pp is not None:
        return r_pp
    if p_pp is not None:
        return p_pp
    return base


def _risk_buckets_raw(config: dict[str, Any] | None) -> dict[str, Any]:
    raw = (config or {}).get("risk_buckets")
    return raw if isinstance(raw, dict) else {}


def risk_bucket_key_for_symbol(
    config: dict[str, Any] | None,
    sym_upper: str,
    sector_etfs: frozenset[str],
) -> str:
    """
    Bucket id for ``max_bucket_allocation_pct`` aggregation.

    When ``risk_buckets`` (top-level YAML) is a non-empty dict, the first list that contains
    *sym_upper* wins. Otherwise, or if the symbol is not listed, returns ``tier_<n>`` from
    :func:`symbol_signal_priority_tier`.
    """
    su = str(sym_upper).strip().upper()
    raw = _risk_buckets_raw(config)
    if raw:
        for name, tickers in raw.items():
            bname = str(name).strip()
            if not bname or not isinstance(tickers, (list, tuple, set)):
                continue
            tick_set = {str(x).strip().upper() for x in tickers if x is not None and str(x).strip()}
            if su in tick_set:
                return bname
    return "tier_%d" % symbol_signal_priority_tier(su, sector_etfs)


def sum_long_stock_mv_in_bucket(
    positions: list[dict[str, Any]] | None,
    bucket_key: str,
    config: dict[str, Any] | None,
    sector_etfs: frozenset[str],
) -> float:
    """Sum abs ``market_value`` for long **stock** rows whose risk bucket key matches *bucket_key*."""
    if not positions:
        return 0.0
    total = 0.0
    for p in positions:
        sym = str(p.get("symbol") or "").strip().upper()
        if not sym or is_option_symbol(sym):
            continue
        try:
            qty = int(float(p.get("qty") or 0))
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            continue
        if risk_bucket_key_for_symbol(config, sym, sector_etfs) != bucket_key:
            continue
        try:
            total += abs(float(p.get("market_value") or 0))
        except (TypeError, ValueError):
            continue
    return total


def allow_cross_bucket_rebalance(config: dict[str, Any] | None) -> bool:
    """Read ``allow_cross_bucket_rebalance`` from ``portfolio.capital_allocator`` (if key present) else ``portfolio.allocator``."""
    p = (config or {}).get("portfolio")
    p = p if isinstance(p, dict) else {}
    ca = p.get("capital_allocator")
    ca = ca if isinstance(ca, dict) else {}
    al = p.get("allocator")
    al = al if isinstance(al, dict) else {}
    if "allow_cross_bucket_rebalance" in ca:
        return bool(ca.get("allow_cross_bucket_rebalance", False))
    return bool(al.get("allow_cross_bucket_rebalance", False))


def risk_sleeve_bucket_key_union(
    config: dict[str, Any] | None,
    positions: list[dict[str, Any]] | None,
    sector_etfs: frozenset[str],
) -> set[str]:
    """
    All named ``risk_buckets`` keys, plus one bucket id per long-stock *positions* row.

    Do not assume empty ``tier_0..3`` sleeves exist: each is only relevant when a held symbol (or
    a candidate) maps to that key; otherwise the aggregate would over-count “unused” tier cap.
    """
    s: set[str] = set()
    for n in _risk_buckets_raw(config).keys():
        bn = str(n).strip()
        if bn:
            s.add(bn)
    for p in positions or []:
        sym = str(p.get("symbol") or "").strip().upper()
        if not sym or is_option_symbol(sym):
            continue
        try:
            q = int(float(p.get("qty") or 0))
        except (TypeError, ValueError):
            q = 0
        if q <= 0:
            continue
        s.add(risk_bucket_key_for_symbol(config, sym, sector_etfs))
    return s


def other_sleeves_dollar_headroom(
    config: dict[str, Any] | None,
    positions: list[dict[str, Any]] | None,
    sector_etfs: frozenset[str],
    equity: float,
    exclude_bucket: str,
    *,
    regime_condition: str | None = None,
    regime_score: int | None = None,
    bucket_cap_mult: float = 1.0,
) -> float:
    """
    Sum of ``max(0, cap * equity * bucket_cap_mult - MV)`` for every **other** risk sleeve, using the
    same :func:`risk_effective_max_bucket_allocation_frac_for_bucket` per sleeve as the bucket gate.
    *bucket_cap_mult* (default 1.0) is the high-cash bucket multiplier from portfolio brain, if any.
    """
    eq = max(0.0, float(equity))
    if eq <= 0:
        return 0.0
    bm = max(0.0, min(3.0, float(bucket_cap_mult)))
    exc = str(exclude_bucket or "").strip()
    keys = risk_sleeve_bucket_key_union(config, positions, sector_etfs)
    if exc:
        keys.add(exc)
    total = 0.0
    for b in keys:
        if b == exc:
            continue
        f = risk_effective_max_bucket_allocation_frac_for_bucket(
            config, b, regime_condition=regime_condition, regime_score=regime_score
        )
        if f <= 0:
            continue
        fr = min(1.0, f * bm)
        cap_d = fr * eq
        mv = sum_long_stock_mv_in_bucket(positions, b, config, sector_etfs)
        total += max(0.0, cap_d - mv)
    return total


def bucket_allocation_allows(
    *,
    positions: list[dict[str, Any]] | None,
    equity: float,
    sym_upper: str,
    proposed_notional: float,
    sector_etfs: frozenset[str],
    config: dict[str, Any] | None,
    regime_condition: str | None = None,
    regime_score: int | None = None,
    entry_strength: float | None = None,
    strength_cohort: list[float] | None = None,
    allow_top_signal_bucket_override: bool = True,
    allow_cross_bucket_rebalance_headroom: bool = True,
) -> tuple[bool, str | None]:
    if equity <= 0:
        return True, None
    bkey = risk_bucket_key_for_symbol(config, sym_upper, sector_etfs)
    cap_frac = risk_effective_max_bucket_allocation_frac_for_bucket(
        config,
        bkey,
        regime_condition=regime_condition,
        regime_score=regime_score,
    )
    if cap_frac <= 0:
        return True, None
    cur = sum_long_stock_mv_in_bucket(positions, bkey, config, sector_etfs)
    ceiling = float(equity) * cap_frac
    prop = float(proposed_notional)
    if cur + prop > ceiling + 1e-6:
        if bool(allow_top_signal_bucket_override) and entry_strength is not None and execution_bucket_top_signal_qualified(
            config,
            strength=entry_strength,
            strength_cohort=strength_cohort,
        ):
            return True, None
        if bool(allow_cross_bucket_rebalance_headroom) and allow_cross_bucket_rebalance(config):
            osl = other_sleeves_dollar_headroom(
                config,
                positions,
                sector_etfs,
                float(equity),
                bkey,
                regime_condition=regime_condition,
                regime_score=regime_score,
                bucket_cap_mult=1.0,
            )
            if cur + prop <= ceiling + osl + 1e-6:
                return True, None
        eq = float(equity)
        total_pct = (cur + prop) / eq * 100.0 if eq > 0 else 0.0
        cap_pct = cap_frac * 100.0
        return False, "bucket %s %.1f%% >= cap %.1f%%" % (bkey, total_pct, cap_pct)
    return True, None


def tracked_add_on_count_for_et_day(tracked: dict[str, Any], sym_upper: str, et_date_iso: str) -> int:
    row = tracked.get(str(sym_upper).strip().upper()) or {}
    if str(row.get("adds_et_date") or "") != str(et_date_iso):
        return 0
    try:
        return max(0, int(row.get("adds_et_date_count") or 0))
    except (TypeError, ValueError):
        return 0


def add_on_allowed_for_daily_cap(
    tracked: dict[str, Any],
    sym_upper: str,
    et_date_iso: str,
    max_adds: int,
) -> tuple[bool, str | None]:
    if max_adds <= 0:
        return True, None
    used = tracked_add_on_count_for_et_day(tracked, sym_upper, et_date_iso)
    if used >= max_adds:
        return False, "add-ons today %d >= max %d" % (used, max_adds)
    return True, None


def add_on_allowed_for_min_minutes(
    tracked: dict[str, Any],
    sym_upper: str,
    now_dt: Any,
    min_minutes: float,
) -> tuple[bool, str | None]:
    if min_minutes <= 0:
        return True, None
    row = tracked.get(str(sym_upper).strip().upper()) or {}
    last_add = row.get("last_add_time")
    if not last_add:
        return True, None
    m = minutes_since_iso(str(last_add), now_dt)
    if m is None:
        return True, None
    if m < float(min_minutes):
        ago = int(max(0, m))
        return False, "last add %dm ago < %.0fm cooldown" % (ago, float(min_minutes))
    return True, None


def effective_hold_for_risk(sym_upper: str, current_positions: dict[str, Any], tracked: dict[str, Any]) -> bool:
    """True if we already have a long stock position or a tracked row with qty / notional open."""
    su = str(sym_upper).strip().upper()
    if su in current_positions:
        return True
    return tracked_row_has_open_long(tracked.get(su))


def parse_risk_emergency_deleverage(config: dict[str, Any] | None) -> dict[str, Any]:
    """
    Read ``risk.emergency_deleverage_trigger`` (gross long MV / equity **multiple**, e.g. ``1.2``),
    ``risk.emergency_deleverage_pct`` (fraction of total long book MV to sell in one emergency pass, e.g. ``0.30``),
    and ``risk.bulk_trim_priority`` (ordered sort keys for RFC bulk / emergency line selection).

    Omitted keys → ``None``; legacy :func:`emergency_bulk_trim_notional_usd` tiers apply when
    *emergency_deleverage_trigger* or *emergency_deleverage_pct* is unset.
    """
    r = _risk_cfg(config)
    out: dict[str, Any] = {
        "emergency_deleverage_trigger": None,
        "emergency_deleverage_pct": None,
        "bulk_trim_priority": None,
    }
    raw_tr = r.get("emergency_deleverage_trigger")
    if raw_tr is not None and str(raw_tr).strip() != "":
        try:
            tr = float(raw_tr)
            if tr == tr and tr > 0.0:
                out["emergency_deleverage_trigger"] = tr
        except (TypeError, ValueError):
            pass
    raw_pc = r.get("emergency_deleverage_pct")
    if raw_pc is not None and str(raw_pc).strip() != "":
        try:
            pc = float(raw_pc)
            if pc != pc:
                pass
            elif pc > 1.0 + 1e-9:
                pc = pc / 100.0
            if pc > 0.0:
                out["emergency_deleverage_pct"] = max(0.0, min(1.0, pc))
        except (TypeError, ValueError):
            pass
    bp = r.get("bulk_trim_priority")
    if isinstance(bp, (list, tuple)):
        pr = [str(x).strip() for x in bp if x is not None and str(x).strip()]
        if pr:
            out["bulk_trim_priority"] = pr
    return out


def parse_risk_emergency_cancel_all_open_orders(
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Read ``risk.emergency_cancel_all_open_orders`` (bool) and optional
    ``risk.emergency_cancel_all_open_orders_gross`` (gross long MV / equity **multiple**, default ``1.2``).

    When enabled, the live loop may call ``broker.cancel_all_orders()`` while in reduce-only mode and
    gross exposure strictly exceeds *gross_threshold*.
    """
    r = _risk_cfg(config)
    raw_en = r.get("emergency_cancel_all_open_orders")
    if isinstance(raw_en, str):
        enabled = str(raw_en).strip().lower() in ("1", "true", "yes", "on")
    else:
        enabled = bool(raw_en) if raw_en is not None else False
    out: dict[str, Any] = {"enabled": enabled, "gross_threshold": 1.2}
    raw_g = r.get("emergency_cancel_all_open_orders_gross")
    if raw_g is not None and str(raw_g).strip() != "":
        try:
            g = float(raw_g)
            if g == g and g > 0.0:
                out["gross_threshold"] = g
        except (TypeError, ValueError):
            pass
    return out
