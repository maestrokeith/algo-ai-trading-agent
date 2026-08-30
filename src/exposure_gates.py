"""
Portfolio exposure gates for new entries (gross book vs equity, technology sector sleeve) and
**over-exposed mode** (gross as a fraction of equity above a threshold → :func:`portfolio_loop_mode` ``"reduce_only"`` in the live loop; see :func:`is_reduce_only_overexposed`). The heartbeat also uses ``"normalization"`` / ``"normal"`` (see that function and ``risk.over_exposure_levels`` / :func:`gross_exposure_tier`).

**Layer 1 (pre-trade gross guard):** reject when projected long gross (current book + new buy) would
exceed the effective max gross cap — :func:`layer1_reject_buy_if_projected_gross_exceeds_max` /
:func:`skip_buy_if_projected_gross_over_max` (and :func:`block_new_entries_total_exposure` for the
current book alone). **HARD (no relief):** :func:`hard_exposure_reject_if_projected_gross_exceeds_cap`
compares ``projected = current_gross/eq + new/eq`` to ``min(1.0, cap)`` and always blocks when
``projected > 100%`` of equity in long gross.

Used by :meth:`src.trading_engine.TradingEngine.run_entry_gates` when ``portfolio.exposure_gates``
is enabled. :func:`skip_buy_if_projected_gross_over_max` enforces the same cap on **projected** gross
(current + order notional / equity) before placing a buy.
:func:`allocator_buys_disallowed_over_max_gross` uses the same effective cap (via
:func:`src.adaptive.adaptive_effective_max_total_exposure`) to **suppress capital_allocator buys** when
the current book is already over max. ``compute_exposures`` reports ``gross_pct``
(stock ``|MV|`` + abs(net signed options delta), as % of equity) and ``sector_pct`` as **percent of
equity** (0–100 scale); thresholds in config are **fractions**
of equity (e.g. ``0.9`` = 90%%).
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from src.adaptive import adaptive_effective_max_total_exposure
from src.risk_limits import gross_exposure_tier, resolve_reduce_only_gross_frac

log = logging.getLogger(__name__)


def parse_strong_signal_cap_relief(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """
    Optional relief when an entry is clearly strong:

    - *min_strength* — compare to entry / composite strength in ``[0, 1]`` scale.
    - *extra_gross_exposure_frac* — when gross book is slightly over ``max_total_exposure_frac``
      but strength ≥ min, allow up to this additional **fraction** of equity (e.g. ``0.02`` → +2%% gross).
    - *relief_symbols* — optional list of tickers; when set and non-empty, relief applies **only** to
      those names (see :func:`strong_signal_cap_relief_eligible_for_symbol`). When omitted, all symbols qualify.
    """
    out: dict[str, Any] = {
        "enabled": False,
        "min_strength": 0.82,
        "extra_gross_exposure_frac": 0.02,
        "relief_symbols_upper": None,
    }
    if not config:
        return out
    port = config.get("portfolio") or {}
    sub = port.get("strong_signal_cap_relief")
    if not isinstance(sub, dict):
        return out
    out["enabled"] = bool(sub.get("enabled", False))
    for key, default in (
        ("min_strength", 0.82),
        ("extra_gross_exposure_frac", 0.02),
    ):
        raw = sub.get(key)
        if raw is None or str(raw).strip() == "":
            out[key] = default
        else:
            try:
                out[key] = float(raw)
            except (TypeError, ValueError):
                out[key] = default
    raw_syms = sub.get("relief_symbols")
    if raw_syms is None:
        out["relief_symbols_upper"] = None
    elif isinstance(raw_syms, (list, tuple, set)):
        fs = frozenset(str(x).strip().upper() for x in raw_syms if x and str(x).strip())
        out["relief_symbols_upper"] = fs if fs else frozenset()
    else:
        out["relief_symbols_upper"] = None
    return out


def strong_signal_cap_relief_eligible_for_symbol(
    relief: Mapping[str, Any] | None,
    *,
    symbol_upper: str | None,
    entry_strength: float | None,
) -> bool:
    """
    True when ``strong_signal_cap_relief`` should apply for this symbol and strength.

    Requires relief ``enabled``, *entry_strength* ≥ ``min_strength``, and either no ``relief_symbols``
    list was configured (``relief_symbols_upper is None`` → all tickers) or *symbol_upper* is in the
    configured set. An explicit empty ``relief_symbols: []`` yields an empty frozenset → never eligible.
    """
    rel = relief if isinstance(relief, dict) else {}
    if not bool(rel.get("enabled")):
        return False
    if entry_strength is None:
        return False
    try:
        es = float(entry_strength)
        mn = float(rel.get("min_strength", 0.82))
    except (TypeError, ValueError):
        return False
    if es + 1e-12 < mn or es != es:
        return False
    rs = rel.get("relief_symbols_upper")
    if rs is None:
        return True
    if not isinstance(rs, frozenset) or len(rs) == 0:
        return False
    su = str(symbol_upper or "").strip().upper()
    return su in rs

def parse_equity_fraction_optional(raw: Any) -> float | None:
    """Parse optional YAML fraction (0.90) or percent points (90) → equity fraction."""
    if raw is None or str(raw).strip() == "":
        return None

    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None

    if v != v:  # nan
        return None

    # Accept:
    #   0.05 -> 0.05
    #   5    -> 0.05
    #   90   -> 0.90
    if v > 1.0:
        v = v / 100.0

    return max(0.0, v)

def parse_portfolio_hard_gross_entry_limits(
    config: Mapping[str, Any] | None,
) -> dict[str, float | None]:
    """Optional kill-switch gross bands on ``portfolio.*`` (no strong-signal relief)."""
    out: dict[str, float | None] = {
        "hard_stop_new_entries_above_gross": None,
        "hard_block_all_entries_above": None,
    }
    if not config:
        return out
    port = config.get("portfolio") if isinstance(config.get("portfolio"), dict) else {}
    out["hard_stop_new_entries_above_gross"] = parse_equity_fraction_optional(
        port.get("hard_stop_new_entries_above_gross")
    )
    out["hard_block_all_entries_above"] = parse_equity_fraction_optional(
        port.get("hard_block_all_entries_above")
    )
    return out


def portfolio_hard_gross_blocks_new_entry(
    gross_pct: float,
    config: Mapping[str, Any] | None,
) -> tuple[bool, str | None]:
    """
    Hard portfolio limits independent of ``exposure_gates`` relief:

    - **hard_block_all_entries_above** — when current gross (%% of equity) **fraction** is **≥** this,
      block **all** new entries (absolute ceiling).
    - **hard_stop_new_entries_above_gross** — when gross **fraction** is **strictly greater** than this,
      block new entries (stricter early stop than ``max_total_exposure_frac`` alone).
    """
    lim = parse_portfolio_hard_gross_entry_limits(config)
    hb = lim.get("hard_block_all_entries_above")
    hs = lim.get("hard_stop_new_entries_above_gross")
    try:
        g = float(gross_pct) / 100.0
    except (TypeError, ValueError):
        return False, None
    if g != g:
        return False, None
    if hb is not None and g >= float(hb) - 1e-12:
        return True, (
            "portfolio: hard_block_all_entries — gross %.2f%% of equity >= %.0f%% book ceiling"
            % (float(gross_pct), float(hb) * 100.0)
        )
    if hs is not None and g > float(hs) + 1e-12:
        return True, (
            "portfolio: hard_stop_new_entries — gross %.2f%% of equity > %.0f%%"
            % (float(gross_pct), float(hs) * 100.0)
        )
    return False, None


def allocator_buys_refused_when_gross_above_threshold(
    gross_pct: float,
    config: Mapping[str, Any] | None,
    ca_cfg: Mapping[str, Any] | None = None,
) -> bool:
    """
    When ``refuse_to_allocate_if_gross_above`` is set on ``capital_allocator`` or ``allocator``,
    allocator **buy** legs are suppressed if gross long MV / equity **fraction** is strictly above it.
    """
    thr: float | None = None
    if isinstance(ca_cfg, dict):
        thr = parse_equity_fraction_optional(ca_cfg.get("refuse_to_allocate_if_gross_above"))
    if thr is None and isinstance(config, dict):
        port = config.get("portfolio") if isinstance(config.get("portfolio"), dict) else {}
        sub = port.get("capital_allocator") if isinstance(port.get("capital_allocator"), dict) else {}
        thr = parse_equity_fraction_optional(sub.get("refuse_to_allocate_if_gross_above"))
        if thr is None:
            al = port.get("allocator") if isinstance(port.get("allocator"), dict) else {}
            thr = parse_equity_fraction_optional(al.get("refuse_to_allocate_if_gross_above"))
    if thr is None:
        return False
    try:
        g = float(gross_pct) / 100.0
    except (TypeError, ValueError):
        return False
    return g > float(thr) + 1e-12


def parse_portfolio_exposure_gates(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return normalized ``portfolio.exposure_gates`` (defaults when missing or disabled)."""
    out: dict[str, Any] = {
        "enabled": False,
        "max_total_exposure_frac": 0.9,
        "max_tech_sector_exposure_frac": 0.4,
        "tech_over_cap_size_multiplier": 0.5,
        "force_trim_weakest_when_over_max": True,
        "overexposed_reduce_only": True,
        "overexposed_reduce_only_gross_frac": 1.0,
        "soft_cap": {"enabled": False},
    }
    if not config:
        return out
    port = config.get("portfolio") or {}
    eg = port.get("exposure_gates")
    if not isinstance(eg, dict):
        return out
    out["enabled"] = bool(eg.get("enabled", False))
    for key, default in (
        ("max_total_exposure_frac", 0.9),
        ("max_tech_sector_exposure_frac", 0.4),
        ("tech_over_cap_size_multiplier", 0.5),
    ):
        raw = eg.get(key)
        if raw is None or str(raw).strip() == "":
            out[key] = default
        else:
            try:
                out[key] = float(raw)
            except (TypeError, ValueError):
                out[key] = default
    out["force_trim_weakest_when_over_max"] = bool(
        eg.get("force_trim_weakest_when_over_max", True)
    )
    out["overexposed_reduce_only"] = bool(eg.get("overexposed_reduce_only", True))
    raw_ox = eg.get("overexposed_reduce_only_gross_frac")
    if raw_ox is None or str(raw_ox).strip() == "":
        out["overexposed_reduce_only_gross_frac"] = 1.0
    else:
        try:
            out["overexposed_reduce_only_gross_frac"] = float(raw_ox)
        except (TypeError, ValueError):
            out["overexposed_reduce_only_gross_frac"] = 1.0
    sc_raw = eg.get("soft_cap")
    if isinstance(sc_raw, dict):
        out["soft_cap"] = {"enabled": bool(sc_raw.get("enabled", False))}
    else:
        out["soft_cap"] = {"enabled": False}
    return out


def is_reduce_only_overexposed(
    gross_pct: float, config: Mapping[str, Any] | None
) -> bool:
    """
    **Over-exposed mode** (``mode: reduce_only`` in the live loop): when gross long MV as
    a fraction of equity is **strictly above** *overexposed_reduce_only_gross_frac* (default
    ``1.0`` = 100% of equity), the stack should not open **new** risk-increasing longs
    (entries use :class:`~src.trading_engine.TradingEngine` to enforce).

    *gross_pct* is the same 0–100+ scale as :func:`~src.exposure.compute_exposures` (e.g. ``101``
    → 1.01× equity). Does not require ``exposure_gates.enabled``; only
    *overexposed_reduce_only* in :func:`parse_portfolio_exposure_gates`. The gross **fraction**
    threshold comes from :func:`src.risk_limits.resolve_reduce_only_gross_frac` (``risk.over_exposure_levels.high``
    when set, else ``portfolio.exposure_gates.overexposed_reduce_only_gross_frac``).
    """
    eg = parse_portfolio_exposure_gates(config)
    if not bool(eg.get("overexposed_reduce_only", True)):
        return False
    try:
        thr = float(
            resolve_reduce_only_gross_frac(dict(config) if config is not None else None)
        )
    except (TypeError, ValueError):
        thr = 1.0
    if thr != thr:  # nan
        thr = 1.0
    thr = max(0.0, thr)
    try:
        g = float(gross_pct) / 100.0
    except (TypeError, ValueError):
        return False
    if g != g:
        return False
    return g > thr + 1e-12


def portfolio_loop_mode(
    gross_pct: float, config: Mapping[str, Any] | None
) -> str:
    """
    Top-level **heartbeat mode** for the live loop (three labels, separate from
    :func:`gross_exposure_tier` *band* detail):

    * ``reduce_only`` — :func:`is_reduce_only_overexposed` (gross above the reduce-only threshold).
    * ``normalization`` — not in reduce-only, but the book is in a non-``normal`` over-exposure
      **tier** (mild / high / critical from ``risk.over_exposure_levels``). De-risk / trim activity
      may be active; new entries are not yet blocked by reduce-only.
    * ``normal`` — tier is ``normal`` (gross below the *mild* band).
    """
    if is_reduce_only_overexposed(gross_pct, config):
        return "reduce_only"
    t = gross_exposure_tier(
        float(gross_pct), dict(config) if config is not None else None
    )
    if t == "normal":
        return "normal"
    return "normalization"


def block_new_entries_total_exposure(
    gross_pct: float,
    *,
    enabled: bool,
    max_total_exposure_frac: float,
    entry_strength: float | None = None,
    relief: Mapping[str, Any] | None = None,
    symbol_upper: str | None = None,
    cap_relax_factor: float = 1.0,
) -> tuple[bool, str | None]:
    """
    When *gross_pct* is book gross long MV as %% of equity, compare ``gross_exposure = gross_pct/100``
    to *max_gross* (``max_total_exposure_frac`` after relax/relief). If ``gross_exposure > max_gross``,
    new entries are blocked.

    When :func:`strong_signal_cap_relief_eligible_for_symbol` is true for *relief*, *symbol_upper*,
    and *entry_strength*, *max_gross* may include ``extra_gross_exposure_frac`` (slight cap breach).
    """
    if not enabled:
        return False, None
    cap = max(float(max_total_exposure_frac), 0.0)
    try:
        _crf = float(cap_relax_factor)
    except (TypeError, ValueError):
        _crf = 1.0
    if _crf > 1.0:
        cap = min(1.0, cap * _crf)
    gross_exposure = float(gross_pct) / 100.0
    rel = relief if isinstance(relief, dict) else {}
    eff_cap = cap
    if strong_signal_cap_relief_eligible_for_symbol(
        rel, symbol_upper=symbol_upper, entry_strength=entry_strength
    ):
        try:
            extra = max(0.0, float(rel.get("extra_gross_exposure_frac", 0.02)))
        except (TypeError, ValueError):
            extra = 0.0
        eff_cap = min(1.0, cap + extra)
    max_gross = eff_cap
    block_new_entries = bool(gross_exposure > max_gross + 1e-12)
    if block_new_entries:
        return True, (
            "exposure_gate: gross book %.1f%% of equity exceeds max %.0f%% — block new entries"
            % (float(gross_pct), max_gross * 100.0)
        )
    return False, None


def skip_buy_if_projected_gross_over_max(
    *,
    current_gross_pct: float,
    buy_notional: float,
    account_equity: float,
    enabled: bool,
    max_total_exposure_frac: float,
    entry_strength: float | None = None,
    relief: Mapping[str, Any] | None = None,
    symbol_upper: str | None = None,
    cap_relax_factor: float = 1.0,
) -> tuple[bool, str | None]:
    """
    **Before a buy:** estimate projected gross long MV as % of equity as
    ``current_gross_pct + (buy_notional / account_equity) * 100`` and apply the same
    *max* / *relief* / *cap_relax_factor* math as :func:`block_new_entries_total_exposure`.
    If the projected book would exceed the effective cap, return ``(True, reason)`` so the caller
    can skip the order even when **current** book is still under the cap.
    """
    if not enabled:
        return False, None
    try:
        eq = float(account_equity)
        n = float(buy_notional)
    except (TypeError, ValueError):
        return False, None
    if eq <= 0.0 or n <= 0.0:
        return False, None
    add_pct = n / eq * 100.0
    projected_gross_pct = float(current_gross_pct) + add_pct
    blocked, _ = block_new_entries_total_exposure(
        projected_gross_pct,
        enabled=True,
        max_total_exposure_frac=max_total_exposure_frac,
        entry_strength=entry_strength,
        relief=relief,
        symbol_upper=symbol_upper,
        cap_relax_factor=cap_relax_factor,
    )
    if not blocked:
        return False, None
    return True, (
        "exposure_gate: projected gross %.1f%% of equity (current %.1f%% + buy ~%.1f%%) over max eff. cap — skip buy"
        % (float(projected_gross_pct), float(current_gross_pct), float(add_pct))
    )


def _normalize_gross_cap_fraction_0_1(raw: float) -> float:
    """``max_total_exposure_frac`` style: 0.9 or 90 → 0.0–1.0."""
    try:
        c = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if c != c:
        return 0.0
    if c > 1.0 + 1e-9:
        c = c / 100.0
    return max(0.0, min(1.0, c))


def hard_exposure_reject_if_projected_gross_exceeds_cap(
    *,
    current_gross_pct: float,
    buy_notional: float,
    account_equity: float,
    max_gross_cap_frac: float,
    exposure_gates_enabled: bool = True,
) -> tuple[bool, str | None]:
    """
    **HARD pre-trade guard.** Let long gross (fraction of equity) be::

        projected = current_gross_pct/100 + buy_notional/account_equity

    * If ``projected > 1.0`` (100%% of equity) → always reject the buy.
    * If *exposure_gates_enabled* and ``projected > min(1.0, cap)`` with *cap* from
      *max_gross_cap_frac* (same 0–1 / %% convention as the rest of this module) → reject.
      No strong-signal relief, no *cap_relax_factor*.

    If exposure gates are disabled, only the ``projected > 1.0`` backstop is enforced.
    """
    try:
        eq = float(account_equity)
        n = float(buy_notional)
    except (TypeError, ValueError):
        return False, None
    if eq <= 1e-12 or n < 0.0:
        return False, None
    cur = max(0.0, float(current_gross_pct) / 100.0)
    p = cur + n / eq
    if p > 1.0 + 1e-9:
        return True, (
            "hard_exposure: projected long gross %.2f%% of equity > 100%% — skip buy" % (p * 100.0,)
        )
    if not exposure_gates_enabled:
        return False, None
    c = min(1.0, _normalize_gross_cap_fraction_0_1(float(max_gross_cap_frac)))
    if p > c + 1e-9:
        return True, (
            "hard_exposure: projected long gross %.2f%% of equity > cap %.0f%% (HARD) — skip buy"
            % (p * 100.0, c * 100.0)
        )
    return False, None


def max_additional_buy_usd_hard_stack(
    current_gross_pct: float,
    account_equity: float,
    max_gross_cap_frac: float,
    exposure_gates_enabled: bool,
) -> float:
    """
    Extra long dollars that fit under the **hard** gross ceiling (same math as
    :func:`hard_exposure_reject_if_projected_gross_exceeds_cap`), before considering relief.
    """
    try:
        eq = float(account_equity)
    except (TypeError, ValueError):
        return 0.0
    if eq <= 1e-12:
        return 0.0
    cur = max(0.0, float(current_gross_pct) / 100.0)
    if not exposure_gates_enabled:
        cap_f = 1.0
    else:
        cap_f = min(1.0, _normalize_gross_cap_fraction_0_1(float(max_gross_cap_frac)))
    return max(0.0, (cap_f - cur) * eq)


def max_additional_buy_usd_layer1_stack(
    current_gross_pct: float,
    account_equity: float,
    max_total_exposure_frac: float,
    relief: Mapping[str, Any] | None,
    symbol_upper: str | None,
    entry_strength: float | None,
    cap_relax_factor: float,
) -> float:
    """
    Maximum buy USD keeping projected gross under the **layer-1** effective cap (relief / relax),
    matching :func:`skip_buy_if_projected_gross_over_max`.
    """
    try:
        eq = float(account_equity)
    except (TypeError, ValueError):
        return 0.0
    if eq <= 1e-12:
        return 0.0
    cap = max(float(max_total_exposure_frac), 0.0)
    try:
        _crf = float(cap_relax_factor)
    except (TypeError, ValueError):
        _crf = 1.0
    if _crf > 1.0:
        cap = min(1.0, cap * _crf)
    rel = relief if isinstance(relief, dict) else {}
    eff_cap = cap
    if strong_signal_cap_relief_eligible_for_symbol(
        rel, symbol_upper=symbol_upper, entry_strength=entry_strength
    ):
        try:
            extra = max(0.0, float(rel.get("extra_gross_exposure_frac", 0.02)))
        except (TypeError, ValueError):
            extra = 0.0
        eff_cap = min(1.0, cap + extra)
    max_proj_pct = eff_cap * 100.0
    add_pct = max_proj_pct - float(current_gross_pct)
    return max(0.0, add_pct / 100.0 * eq)


def soft_cap_max_buy_usd(
    *,
    current_gross_pct: float,
    account_equity: float,
    max_total_exposure_frac: float,
    exposure_gates_enabled: bool,
    relief: Mapping[str, Any] | None,
    symbol_upper: str | None,
    entry_strength: float | None,
    cap_relax_factor: float,
) -> float:
    """
    Conservative allowed buy notional for **soft cap** — intersection of hard and layer-1 headroom.
    """
    h = max_additional_buy_usd_hard_stack(
        current_gross_pct,
        account_equity,
        max_total_exposure_frac,
        exposure_gates_enabled,
    )
    if not exposure_gates_enabled:
        return max(0.0, h)
    l1 = max_additional_buy_usd_layer1_stack(
        current_gross_pct,
        account_equity,
        max_total_exposure_frac,
        relief,
        symbol_upper,
        entry_strength,
        cap_relax_factor,
    )
    return max(0.0, min(h, l1))


def layer1_reject_buy_if_projected_gross_exceeds_max(
    *,
    current_gross_pct: float,
    buy_notional: float,
    account_equity: float,
    enabled: bool,
    max_total_exposure_frac: float,
    entry_strength: float | None = None,
    relief: Mapping[str, Any] | None = None,
    symbol_upper: str | None = None,
    cap_relax_factor: float = 1.0,
) -> tuple[bool, str | None]:
    """
    **Layer 1 — pre-trade guard (projected book).** Reject the buy if, as a fraction of equity,
    ``current_gross + new_trade`` would exceed the effective max gross long cap (same as
    :func:`skip_buy_if_projected_gross_over_max`). Call after position sizing has produced a
    *buy_notional*; run after :func:`hard_exposure_reject_if_projected_gross_exceeds_cap` in
    :meth:`~src.trading_engine.TradingEngine.run_entry_gates`.
    """
    return skip_buy_if_projected_gross_over_max(
        current_gross_pct=current_gross_pct,
        buy_notional=buy_notional,
        account_equity=account_equity,
        enabled=enabled,
        max_total_exposure_frac=max_total_exposure_frac,
        entry_strength=entry_strength,
        relief=relief,
        symbol_upper=symbol_upper,
        cap_relax_factor=cap_relax_factor,
    )


def technology_sector_book_frac(sector_pct: Mapping[str, Any]) -> float:
    """Technology sleeve as fraction of equity (``sector_pct`` values are %% points from :func:`compute_exposures`)."""
    try:
        v = float((sector_pct or {}).get("technology", 0.0))
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, v / 100.0)


def entry_size_multiplier_tech_sector_over_cap(
    sym_u: str,
    sector_pct: Mapping[str, Any],
    symbol_sector: Mapping[str, Any],
    *,
    enabled: bool,
    max_tech_sector_exposure_frac: float,
    tech_over_cap_size_multiplier: float,
) -> float:
    """
    When technology book fraction exceeds *max_tech_sector_exposure_frac*, scale **new** entries
    whose resolved sector is ``technology`` by *tech_over_cap_size_multiplier* (e.g. ``0.5``).

    Non-tech symbols keep multiplier ``1.0``.
    """
    if not enabled:
        return 1.0
    tech_frac = technology_sector_book_frac(sector_pct)
    cap = max(float(max_tech_sector_exposure_frac), 0.0)
    if tech_frac <= cap + 1e-12:
        return 1.0
    su = str(sym_u).strip().upper()
    sm = dict(symbol_sector or {})
    sec_raw = sm.get(su) or sm.get(sym_u) or ""
    sec = str(sec_raw).strip().lower()
    if sec != "technology":
        return 1.0
    m = float(tech_over_cap_size_multiplier)
    if m <= 0 or m != m:
        return 1.0
    if m < 1.0:
        log.info(
            "exposure_gate: tech book %.1f%% of equity > %.0f%% — applying size multiplier %.2f for %s",
            tech_frac * 100.0,
            cap * 100.0,
            m,
            su,
        )
    return m


def allocator_buys_disallowed_over_max_gross(
    gross_pct: float,
    config: Mapping[str, Any] | None,
    *,
    regime_score: int | None = None,
    regime_condition: str | None = None,
    entry_wave_strong_signal_count: int | None = None,
) -> bool:
    """
    True when ``portfolio.exposure_gates`` is on and the **current** gross long book (%% of equity
    from :func:`src.exposure.compute_exposures`) is strictly **above** the same effective
    ``max_total_exposure_frac`` used by
    :func:`block_new_entries_total_exposure` (including :func:`src.adaptive.adaptive_effective_max_total_exposure`).

    Used by the post-scan **capital allocator** in ``run_alpaca_loop`` to drop **buy** rows while
    still allowing allocator **sells** (e.g. rotation) until gross is back under the cap.
    """
    eg = parse_portfolio_exposure_gates(config)
    if not bool(eg.get("enabled", False)):
        return False
    try:
        base = float(eg["max_total_exposure_frac"])
    except (TypeError, KeyError, ValueError):
        base = 0.9
    meff = adaptive_effective_max_total_exposure(
        dict(config) if config is not None else None,
        base_max_total_exposure_frac=base,
        regime_score=regime_score,
        regime_condition=regime_condition,
        entry_wave_strong_signal_count=entry_wave_strong_signal_count,
    )
    blocked, _ = block_new_entries_total_exposure(
        float(gross_pct),
        enabled=True,
        max_total_exposure_frac=meff,
        cap_relax_factor=1.0,
    )
    return bool(blocked)
