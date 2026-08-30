"""
Plan partial sells of a tracked long to free buying power for new entries (RFC).

When the live config sets ``rebalance.trigger`` to include ``signal_deterioration``,
:func:`plan_weakest_trim_for_free_capital` can require the chosen name to show live hold health
materially **below** persisted entry :func:`tracked_signal_strength` (see
:func:`position_has_signal_deterioration`). Otherwise, if a new order needs BP, the trim target is
``portfolio.rebalance_free_capital.trim_target``: default **weakest** (lowest replacement score) or
**largest_exposure_notional** (``market_value`` desc, try up to *largest_exposure_max_try*), then
sell a **slice**, refresh BP, retry entry.

Uses the same weakest selection as portfolio replacement and the same min-hold gate as
``portfolio.replacement``. Trim size: ``trim_pct_min``/``trim_pct_max`` (%% of shares) or
``trim_fraction`` (0–1). Optional ``trim_band_uniform`` draws X uniformly in the band each trim.

The live loop records trimmed symbols so ``effective_allow_add_after_capital_trim`` can treat
add-backs like ``portfolio.allow_add`` for that symbol in the same entry scan (capital reuse).

``run_alpaca_loop`` uses :func:`trim_fraction_by_gross_leverage` (gross book vs equity) for RFC /
min-cash when ``gross_liquidation.enabled`` is false, or :func:`gross_liquidation_trim_shares` for
gross-cap trims that sell toward ``target_gross_pct`` of equity over a small number of passes
(``gross_liquidation.passes``, e.g. 2 to move from ~107%% book toward 95%% in about two live cycles).

**Bulk trim** (``rebalance_free_capital.bulk_trim``): in one pass, place **up to** *N* market sells for a
**fixed USD notional each** (config default $1500 on the top names by position ``market_value`` for
RFC / min-cash; **gross cap emergency** path uses :func:`emergency_bulk_trim_notional_usd` as a
fraction of equity by leverage tier: ``>1.5×`` equity → 20%, ``>1.2×`` → 10%, else 5%). Realized with
:meth:`src.execution.ExecutionManager.build_order` ``notional=`` (``run_alpaca_loop``) when enabled.

**First pass** of an entry scan (``_rfc_trims_done == 0``) can **skip** trims on lines with unrealized
PnL % vs ``|cost_basis|`` above ``first_pass_winner_pnl_skip_pct`` (default 3) — see
:func:`broker_position_unrealized_pnl_pct` and the *is_first_rfc_pass* / *skip_if_unrealized_pnl_pct_above*
args on the planners. Set the threshold to ``0`` in config to turn off.

**Two-phase gross unwind** (``rebalance_free_capital.two_phase_gross_cap_unwind``): when gross book
exceeds the effective cap, ``run_alpaca_loop`` can: (1) sell about half of the **weakest** long;
(2) if still over cap, full exit the **weakest**; (3) if still over,
:func:`plan_proportional_gross_delever_notional_trims` (weakest-first waterfill) across
min-hold-ok longs. Set
``enabled: false`` to use the legacy gross-cap path (bulk / share tiers / gross_liquidation).

Legacy compatibility module: related newer portfolio helpers live under
``src/portfolio/``. Keep this root path for existing imports until migration is
complete. Because this repo already has ``src/portfolio/rebalance.py``, split
RFC surfaces live as ``src/portfolio/rebalance_*.py`` modules rather than a
sibling ``src/portfolio/rebalance/`` package.
"""
from __future__ import annotations

import random
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, AbstractSet

import pandas as pd

from src.options_premium_risk import is_option_symbol
from src.portfolio_replacement import (
    hold_health_score_normalized,
    replacement_hold_strength,
    replacement_weakest_min_hold_ok,
    tracked_signal_strength,
    weakest_replacement_hold,
)


def _engine_st_health_bars(engine: Any) -> tuple[int, int, int]:
    s = getattr(engine, "strategy", None) if engine is not None else None
    if s is None:
        return 200, 10, 20
    ms = int(getattr(s, "ma_slow", 200) or 200)
    mb_r = getattr(s, "_composite_rank_momentum_bars", None)
    vb_r = getattr(s, "_composite_rank_volume_bars", None)
    mb = int(mb_r if mb_r is not None else getattr(s, "momentum_bars", 10) or 10)
    vb = int(vb_r if vb_r is not None else getattr(s, "volume_bars", 20) or 20)
    return ms, mb, vb


def position_has_signal_deterioration(
    symbol_upper: str,
    tracked: dict[str, Any],
    positions: list[dict[str, Any]],
    get_bars: Callable[[str], Any] | None,
    engine: Any | None,
    *,
    min_gap: float = 0.05,
) -> bool:
    """
    True when live :func:`hold_health_score_normalized` is below the tracked entry
    :func:`tracked_signal_strength` by at least *min_gap* on a ``[0, 1]`` scale.
    """
    if get_bars is None or engine is None:
        return False
    su = str(symbol_upper).strip().upper()
    row = tracked.get(su) or {}
    try:
        entry = float(tracked_signal_strength(row))
    except (TypeError, ValueError):
        entry = 1.0
    try:
        df = get_bars(su)
    except Exception:
        return False
    if df is None:
        return False
    if hasattr(df, "empty") and bool(getattr(df, "empty", False)):
        return False
    if not isinstance(df, pd.DataFrame) or len(df) < 2:
        return False
    ma_slow, m_bars, v_bars = _engine_st_health_bars(engine)
    try:
        h = float(
            hold_health_score_normalized(
                su, positions, df, ma_slow=ma_slow, momentum_bars=m_bars, volume_bars=v_bars
            )
        )
    except Exception:
        return False
    thr = max(0.0, float(entry) - float(max(0.0, min(0.5, min_gap))))
    return h < thr


def rebalance_trim_fraction_for_attempt(
    rfc_cfg: Mapping[str, Any],
    *,
    rng: random.Random | None = None,
) -> float:
    """
    Fraction of broker long shares to sell for one rebalance-free-capital trim.

    When ``trim_band_uniform`` is true and ``trim_pct_lo``/``trim_pct_hi`` are set (percent points
    0–100), returns ``uniform(lo, hi) / 100``; otherwise returns ``trim_fraction`` from config parse.
    """
    gen = rng if rng is not None else random
    if bool(rfc_cfg.get("trim_band_uniform")):
        lo = rfc_cfg.get("trim_pct_lo")
        hi = rfc_cfg.get("trim_pct_hi")
        if lo is not None and hi is not None:
            try:
                lo_p = max(0.0, min(100.0, float(lo)))
                hi_p = max(0.0, min(100.0, float(hi)))
            except (TypeError, ValueError):
                return max(0.0, min(1.0, float(rfc_cfg.get("trim_fraction", 0.15))))
            if hi_p < lo_p:
                lo_p, hi_p = hi_p, lo_p
            return max(0.0, min(1.0, float(gen.uniform(lo_p, hi_p)) / 100.0))
    try:
        return max(0.0, min(1.0, float(rfc_cfg.get("trim_fraction", 0.15))))
    except (TypeError, ValueError):
        return 0.15


def trim_fraction_by_gross_leverage(gross_pct: float) -> float:
    """
    **Live loop** (``run_alpaca_loop``): dynamic trim **share of position** from gross long MV / equity.

    *gross_pct* is the same 0–100+ scale as :func:`src.exposure.compute_exposures`. Let
    ``g = gross_pct / 100`` (book as multiple of equity):

    - ``g > 1.05`` → ``0.6`` (aggressive)
    - ``g > 1.00`` → ``0.4``
    - else → ``0.25``
    """
    try:
        g = float(gross_pct) / 100.0
    except (TypeError, ValueError):
        return 0.25
    if g != g:  # nan
        return 0.25
    if g > 1.05 + 1e-12:
        return 0.6
    if g > 1.0 + 1e-12:
        return 0.4
    return 0.25


def emergency_bulk_trim_notional_usd(
    account_equity: float,
    gross_exposure: float,
) -> float:
    """
    **Emergency / gross-cap** bulk market trim: notional per symbol from leverage vs equity.

    *gross_exposure* is long gross / equity as a **multiple** (``gross_pct/100`` from
    :func:`src.exposure.compute_exposures`).

    - ``gross_exposure > 1.5`` → 20% of account equity (aggressive cut)
    - ``> 1.2`` → 10%
    - else → 5%

    The result is floored at $1 when equity is positive so planning never uses a non-positive
    tranche. Call sites may still cap by line size.
    """
    try:
        eq = max(0.0, float(account_equity))
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if eq != eq or eq <= 0.0:
        return 0.0
    try:
        g = float(gross_exposure)
    except (TypeError, ValueError, OverflowError):
        g = 0.0
    if g != g:
        g = 0.0
    if g > 1.5 + 1e-12:
        out = eq * 0.20
    elif g > 1.2 + 1e-12:
        out = eq * 0.10
    else:
        out = eq * 0.05
    return max(1.0, float(out))


def rfc_uses_largest_exposure_notional_trim(s: str | None) -> bool:
    """
    True when *s* (config ``trim_target`` or the same param on :func:`plan_weakest_trim_for_free_capital`)
    refers to notional-``market_value`` ordering instead of the weakest-replacement name.
    """
    t = re.sub(r"[^a-z0-9_]+", "_", str(s or "weakest").strip().lower())
    if not t or t in ("weakest", "weakest_replacement", "weakest_score"):
        return False
    return t in (
        "largest",
        "largest_exposure",
        "largest_exposure_notional",
        "largest_notional",
        "largest_mkt",
    )


def _parse_bulk_trim_cfg(sub: Mapping[str, Any] | None) -> dict[str, Any]:
    """``portfolio.rebalance_free_capital.bulk_trim`` (optional nested block)."""
    dflt = {
        "enabled": False,
        "notional_per_symbol_usd": 1500.0,
        "max_symbols_per_pass": 3,
        "buy_cooldown_minutes": 30.0,
    }
    if not sub or not isinstance(sub, dict):
        return dict(dflt)
    b = (sub or {}).get("bulk_trim")
    if not isinstance(b, dict):
        return dict(dflt)
    en = bool(b.get("enabled", False))
    try:
        n_usd = float(b.get("notional_per_symbol_usd", 1500.0) or 1500.0)
    except (TypeError, ValueError):
        n_usd = 1500.0
    try:
        mx = int(b.get("max_symbols_per_pass", 3) or 3)
    except (TypeError, ValueError):
        mx = 3
    mx = max(1, min(25, mx))
    n_usd = max(1.0, n_usd)
    try:
        cd = float(b.get("buy_cooldown_minutes", 30) or 30.0)
    except (TypeError, ValueError):
        cd = 30.0
    cd = max(0.0, cd)
    return {
        "enabled": en,
        "notional_per_symbol_usd": n_usd,
        "max_symbols_per_pass": mx,
        "buy_cooldown_minutes": cd,
    }


def _parse_gross_liquidation_cfg(sub: Mapping[str, Any] | None) -> dict[str, Any]:
    """``portfolio.rebalance_free_capital.gross_liquidation`` (optional nested block)."""
    dflt = {"enabled": False, "target_gross_pct": 95.0, "passes": 2}
    if not sub or not isinstance(sub, dict):
        return dict(dflt)
    gl = (sub or {}).get("gross_liquidation")
    if not isinstance(gl, dict):
        return dict(dflt)
    try:
        tgt = float(gl.get("target_gross_pct", 95.0) or 95.0)
    except (TypeError, ValueError):
        tgt = 95.0
    try:
        p = int(gl.get("passes", 2) or 2)
    except (TypeError, ValueError):
        p = 2
    p = max(1, p)
    return {
        "enabled": bool(gl.get("enabled", False)),
        "target_gross_pct": max(0.0, min(500.0, tgt)),
        "passes": p,
    }


def _parse_top_position_protection_cfg(sub: Mapping[str, Any] | None) -> dict[str, Any]:
    """
    ``portfolio.rebalance_free_capital.top_position_protection`` — largest *n* lines by notional
    are trimmed **less aggressively** (see :func:`get_top_n_positions`, *intensity* in ``(0,1]``).
    """
    dflt: dict[str, Any] = {"enabled": True, "n": 3, "intensity_mult": 0.5}
    if not sub or not isinstance(sub, dict):
        return dict(dflt)
    t = (sub or {}).get("top_position_protection")
    if not isinstance(t, dict):
        return dict(dflt)
    en = t.get("enabled", True)
    if isinstance(en, str):
        en = str(en).strip().lower() not in ("0", "false", "no", "off", "")
    en = bool(en) if en is not None else True
    try:
        n = int(t.get("n", 3) or 3)
    except (TypeError, ValueError):
        n = 3
    n = max(0, min(25, n))
    try:
        im = float(t.get("intensity_mult", 0.5) or 0.5)
    except (TypeError, ValueError):
        im = 0.5
    im = max(0.0, min(1.0, im))
    if im <= 0.0 and en:
        im = 0.5
    return {"enabled": en, "n": n, "intensity_mult": im}


def _parse_two_phase_gross_cap_unwind_cfg(
    rfc_sub: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """
    ``portfolio.rebalance_free_capital.two_phase_gross_cap_unwind`` — pro-style gross de-lever when
    book exceeds the effective cap (weakest 50% → full exit weakest → proportional book trim).
    """
    dflt: dict[str, Any] = {
        "enabled": False,
        "phase1_weakest_trim_fraction": 0.5,
        "proportional_max_submits": 25,
    }
    if not rfc_sub or not isinstance(rfc_sub, dict):
        return dict(dflt)
    raw = (rfc_sub or {}).get("two_phase_gross_cap_unwind")
    if not isinstance(raw, dict):
        return dict(dflt)
    en = raw.get("enabled", False)
    if isinstance(en, str):
        en = str(en).strip().lower() not in ("0", "false", "no", "off", "")
    en = bool(en)
    try:
        p1 = float(
            raw.get("phase1_weakest_trim_fraction", dflt["phase1_weakest_trim_fraction"]) or 0.5
        )
    except (TypeError, ValueError):
        p1 = 0.5
    p1 = max(0.05, min(0.99, p1))
    try:
        pmax = int(
            raw.get("proportional_max_submits", dflt["proportional_max_submits"]) or 25
        )
    except (TypeError, ValueError):
        pmax = 25
    pmax = max(1, min(200, pmax))
    return {"enabled": en, "phase1_weakest_trim_fraction": p1, "proportional_max_submits": pmax}


def parse_rebalance_free_capital_cfg(portfolio_cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    """Read ``portfolio.rebalance_free_capital`` with safe defaults."""
    sub: dict[str, Any] = {}
    if portfolio_cfg:
        raw = portfolio_cfg.get("rebalance_free_capital")
        if isinstance(raw, dict):
            sub = raw
    enabled = bool(sub.get("enabled", False))
    # Trim size: prefer ``trim_pct_min`` + ``trim_pct_max`` (percent of position, e.g. 20–30 → midpoint 25%%),
    # else ``trim_fraction`` (0–1), else default 0.15 (mid of a 10–20%% band).
    trim_fraction: float
    trim_pct_lo: float | None = None
    trim_pct_hi: float | None = None
    trim_band_uniform = bool(sub.get("trim_band_uniform", False))
    raw_lo = sub.get("trim_pct_min")
    raw_hi = sub.get("trim_pct_max")
    if raw_lo is not None and str(raw_lo).strip() != "" and raw_hi is not None and str(raw_hi).strip() != "":
        try:
            lo_p = max(0.0, min(100.0, float(raw_lo)))
            hi_p = max(0.0, min(100.0, float(raw_hi)))
            if hi_p < lo_p:
                lo_p, hi_p = hi_p, lo_p
            trim_pct_lo, trim_pct_hi = lo_p, hi_p
            trim_fraction = ((lo_p + hi_p) / 2.0) / 100.0
        except (TypeError, ValueError):
            try:
                trim_fraction = float(sub.get("trim_fraction", 0.15))
            except (TypeError, ValueError):
                trim_fraction = 0.15
    else:
        try:
            trim_fraction = float(sub.get("trim_fraction", 0.15))
        except (TypeError, ValueError):
            trim_fraction = 0.15
    trim_fraction = max(0.0, min(1.0, trim_fraction))
    try:
        max_trims = int(sub.get("max_trims_per_entry_scan", 1))
    except (TypeError, ValueError):
        max_trims = 1
    max_trims = max(0, max_trims)
    exclude_incoming = bool(sub.get("exclude_incoming_symbol", True))
    rotate_full = bool(sub.get("rotate_full_weakest_when_stronger", False))
    raw_t = str(sub.get("trim_target", "weakest") or "weakest")
    trim_target: str = (
        "largest_exposure_notional" if rfc_uses_largest_exposure_notional_trim(raw_t) else "weakest"
    )
    try:
        le_try = int(sub.get("largest_exposure_max_try", 3))
    except (TypeError, ValueError):
        le_try = 3
    le_try = max(0, le_try)
    if le_try == 0:
        le_try = 3
    glr = _parse_gross_liquidation_cfg(sub)
    blr = _parse_bulk_trim_cfg(sub)
    tpp = _parse_top_position_protection_cfg(sub)
    try:
        _raw_fp = sub.get("first_pass_winner_pnl_skip_pct", 3.0)
        _fp_skip = float(_raw_fp) if _raw_fp is not None and str(_raw_fp).strip() != "" else 3.0
    except (TypeError, ValueError, OverflowError):
        _fp_skip = 3.0
    _fp_skip = max(0.0, min(10_000.0, _fp_skip))
    tpg = _parse_two_phase_gross_cap_unwind_cfg(sub)
    return {
        "enabled": enabled,
        "trim_fraction": trim_fraction,
        "trim_pct_lo": trim_pct_lo,
        "trim_pct_hi": trim_pct_hi,
        "trim_band_uniform": trim_band_uniform,
        "max_trims_per_entry_scan": max_trims,
        "exclude_incoming_symbol": exclude_incoming,
        "rotate_full_weakest_when_stronger": rotate_full,
        "trim_target": trim_target,
        "largest_exposure_max_try": le_try,
        "gross_liquidation": glr,
        "bulk_trim": blr,
        "top_position_protection": tpp,
        "first_pass_winner_pnl_skip_pct": _fp_skip,
        "two_phase_gross_cap_unwind": tpg,
    }


def broker_long_shares_for_symbol(positions: list[dict[str, Any]], sym_upper: str) -> int:
    """Whole-share long qty for *sym_upper* from broker rows (options excluded)."""
    su = str(sym_upper).strip().upper()
    for p in positions:
        s = str(p.get("symbol") or "").strip().upper()
        if s != su:
            continue
        if is_option_symbol(s):
            return 0
        return max(0, int(float(p.get("qty") or 0)))
    return 0


def broker_position_market_value_usd(positions: list[dict[str, Any]], sym_upper: str) -> float:
    """
    Long ``market_value`` (notional) for *sym_upper* from broker position rows, abs USD.

    Falls back to ``0.0`` if the symbol is not present, is an options root, or ``market_value`` is missing/invalid.
    """
    su = str(sym_upper).strip().upper()
    for p in positions:
        s = str(p.get("symbol") or "").strip().upper()
        if s != su:
            continue
        if is_option_symbol(s):
            return 0.0
        try:
            return abs(float(p.get("market_value") or 0))
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def broker_position_unrealized_pnl_pct(
    positions: list[dict[str, Any]], sym_upper: str
) -> float | None:
    """
    Unrealized P/L as **percent of cost basis** for a long **stock** row (``100 * unrealized_pl / |cost_basis|``).

    Returns ``None`` if the symbol is missing, is an option, or cost basis is not meaningful.
    """
    su = str(sym_upper).strip().upper()
    for p in positions:
        s = str(p.get("symbol") or "").strip().upper()
        if s != su:
            continue
        if is_option_symbol(s):
            return None
        try:
            ul = float(p.get("unrealized_pl") or 0)
            cb = float(p.get("cost_basis") or 0)
        except (TypeError, ValueError):
            return None
        acb = abs(float(cb))
        if acb < 1e-9:
            return None
        return 100.0 * ul / acb
    return None


def trim_candidate_symbols_largest_exposure_notional(
    eligible_symbols: list[str],
    positions: list[dict[str, Any]],
    *,
    max_candidates: int = 3,
) -> list[str]:
    """
    *eligible_symbols* in broker-notional (``market_value``) descending order, tie-broken by list order.

    Skips names that cannot be partially trimmed (long stock ``qty`` < 2). Picks at most *max_candidates* symbols.
    """
    cap = int(max(0, max_candidates))
    if cap <= 0 or not eligible_symbols:
        return []
    out: list[tuple[str, float, int]] = []
    for i, raw in enumerate(eligible_symbols):
        s = str(raw or "").strip().upper()
        if not s:
            continue
        bq = broker_long_shares_for_symbol(positions, s)
        if bq < 2:
            continue
        mv = broker_position_market_value_usd(positions, s)
        out.append((s, mv, i))
    out.sort(key=lambda t: (-t[1], t[2]))
    return [a[0] for a in out[:cap]]


def long_stock_symbols_by_market_value_desc(
    eligible_symbols: list[str],
    positions: list[dict[str, Any]],
) -> list[str]:
    """
    *eligible_symbols* with at least one long **stock** share, ordered by :func:`broker_position_market_value_usd` desc.
    (Includes single-share names — notional market sells are allowed.)
    """
    out: list[tuple[str, float, int]] = []
    for i, raw in enumerate(eligible_symbols):
        s = str(raw or "").strip().upper()
        if not s:
            continue
        if broker_long_shares_for_symbol(positions, s) < 1:
            continue
        mv = broker_position_market_value_usd(positions, s)
        if mv <= 0.0:
            continue
        out.append((s, mv, i))
    out.sort(key=lambda t: (-t[1], t[2]))
    return [a[0] for a in out]


def get_top_n_positions(
    eligible_symbols: list[str],
    positions: list[dict[str, Any]],
    *,
    n: int = 3,
) -> frozenset[str]:
    """
    The top *n* long **stock** line(s) by ``|market_value|`` among *eligible_symbols* (descending).

    Uses the same ordering as :func:`long_stock_symbols_by_market_value_desc` (ties: eligible order).
    For use with *protected* / less-aggressive trim logic.
    """
    k = int(n)
    if k <= 0 or not eligible_symbols:
        return frozenset()
    k = min(k, 25)
    ordered = long_stock_symbols_by_market_value_desc(eligible_symbols, positions)
    return frozenset(ordered[:k])


def _normalize_bulk_trim_priority_token(raw: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(raw or "").strip().lower())


def symbols_ordered_for_bulk_trim_priority(
    eligible_symbols: list[str],
    positions: list[dict[str, Any]],
    priority: Sequence[str] | None,
) -> list[str]:
    """
    Eligible long **stock** lines ordered for bulk trims using ``risk.bulk_trim_priority`` keys.

    Supported tokens (repeat for multi-key sort, leftmost most significant):

    - ``highest_weight`` / ``largest_exposure`` / ``largest_notional`` — descending ``market_value``.
    - ``weakest_pnl`` / ``weakest_pnl_pct`` — ascending unrealized P/L %% vs cost (worst first).
    - ``smallest_weight`` / ``smallest_exposure`` — ascending ``market_value``.

    Unknown tokens fall back to largest-notional for that key position.
    """
    if not priority:
        return long_stock_symbols_by_market_value_desc(eligible_symbols, positions)
    toks = [_normalize_bulk_trim_priority_token(x) for x in priority if str(x).strip()]
    if not toks:
        return long_stock_symbols_by_market_value_desc(eligible_symbols, positions)

    _largest = frozenset(
        {
            "highest_weight",
            "largest_exposure",
            "largest_notional",
            "largest",
            "by_weight",
            "weight",
        }
    )
    _weakest_pnl = frozenset(
        {
            "weakest_pnl",
            "weakest_pnl_pct",
            "worst_pnl",
            "lowest_pnl",
        }
    )
    _smallest = frozenset(
        {
            "smallest_weight",
            "smallest_exposure",
            "smallest_notional",
            "smallest",
        }
    )

    elig_raw = list(eligible_symbols)
    idx_map = {str(s).strip().upper(): i for i, s in enumerate(elig_raw)}

    def _mv(sym: str) -> float:
        return broker_position_market_value_usd(positions, sym)

    def _pnl(sym: str) -> float:
        x = broker_position_unrealized_pnl_pct(positions, sym)
        return float(x) if x is not None else 0.0

    rows: list[tuple[str, float, int]] = []
    for raw in elig_raw:
        s = str(raw or "").strip().upper()
        if not s:
            continue
        if broker_long_shares_for_symbol(positions, s) < 1:
            continue
        mv = _mv(s)
        if mv <= 0.0:
            continue
        rows.append((s, mv, idx_map.get(s, 999)))

    def sort_key(tup: tuple[str, float, int]) -> tuple[Any, ...]:
        s, _mv0, ix = tup
        parts: list[Any] = []
        for tok in toks:
            if tok in _largest:
                parts.append(-_mv(s))
            elif tok in _weakest_pnl:
                parts.append(_pnl(s))
            elif tok in _smallest:
                parts.append(_mv(s))
            else:
                parts.append(-_mv(s))
        parts.append(ix)
        return tuple(parts)

    rows.sort(key=sort_key)
    return [a[0] for a in rows]


def plan_emergency_deleverage_portfolio_pct_trims(
    *,
    eligible_symbols: list[str],
    positions: list[dict[str, Any]],
    tracked: dict[str, Any],
    rep_sub: Mapping[str, Any] | None,
    now_dt: datetime,
    incoming_sym_upper: str,
    portfolio_trim_pct: float,
    max_symbols: int,
    exclude_incoming_symbol: bool,
    bulk_trim_priority: Sequence[str] | None,
    require_signal_deterioration: bool = False,
    deterioration_min_gap: float = 0.05,
    broker: Any | None = None,
    engine: Any | None = None,
    top_positions: AbstractSet[str] | None = None,
    top_position_sell_intensity: float = 0.5,
    is_first_rfc_pass: bool = False,
    skip_if_unrealized_pnl_pct_above: float | None = None,
) -> list[tuple[str, float]]:
    """
    Greedy notional sells totaling ``portfolio_trim_pct ×`` sum(eligible long ``market_value``),
    visiting symbols in :func:`symbols_ordered_for_bulk_trim_priority` order until the budget is met
    or *max_symbols* legs are placed.
    """
    inc = str(incoming_sym_upper or "").strip().upper()
    elig = (
        [s for s in eligible_symbols if str(s).upper() != inc]
        if exclude_incoming_symbol
        else list(eligible_symbols)
    )
    try:
        ftrim = max(0.0, min(1.0, float(portfolio_trim_pct)))
    except (TypeError, ValueError):
        return []
    total_mv = 0.0
    for s in elig:
        total_mv += broker_position_market_value_usd(positions, str(s).strip().upper())
    target_usd = max(0.0, total_mv * ftrim)
    if target_usd < 1.0:
        return []
    try:
        _tpi = max(0.0, min(1.0, float(top_position_sell_intensity)))
    except (TypeError, ValueError):
        _tpi = 0.5
    mcap = int(max(0, max_symbols))
    if mcap <= 0 or not elig:
        return []
    _skip_win: float | None
    if (
        is_first_rfc_pass
        and skip_if_unrealized_pnl_pct_above is not None
        and float(skip_if_unrealized_pnl_pct_above) > 0.0
    ):
        try:
            _skip_win = max(0.0, float(skip_if_unrealized_pnl_pct_above))
        except (TypeError, ValueError, OverflowError):
            _skip_win = None
    else:
        _skip_win = None

    def _gb_trim(s: str) -> Any:
        if broker is None:
            return None
        try:
            return broker.get_bars(s, timeframe="1Day", limit=220)
        except Exception:
            return None

    raw_mh = rep_sub.get("min_hold_minutes") if rep_sub else None
    mh: float | None = None
    if raw_mh is not None and str(raw_mh).strip() != "":
        try:
            mh = float(raw_mh)
        except (TypeError, ValueError):
            mh = None

    ordered = symbols_ordered_for_bulk_trim_priority(elig, positions, bulk_trim_priority)
    _top = {str(s).strip().upper() for s in (top_positions or ())} if top_positions else set()
    remaining = float(target_usd)
    out: list[tuple[str, float]] = []
    for sym in ordered:
        if len(out) >= mcap:
            break
        if remaining < 1.0:
            break
        if _skip_win is not None:
            _p_pct = broker_position_unrealized_pnl_pct(
                positions, str(sym).strip().upper()
            )
            if _p_pct is not None and _p_pct > _skip_win + 1e-9:
                continue
        if require_signal_deterioration:
            if not position_has_signal_deterioration(
                sym,
                tracked,
                positions,
                _gb_trim,
                engine,
                min_gap=float(deterioration_min_gap),
            ):
                continue
        row = tracked.get(sym) or {}
        ok, _reason = replacement_weakest_min_hold_ok(
            weakest_entry_time_iso=row.get("entry_time"),
            now=now_dt,
            min_hold_minutes=mh,
        )
        if not ok:
            continue
        mv = broker_position_market_value_usd(positions, sym)
        if mv <= 0.0:
            continue
        su = str(sym).strip().upper()
        want = min(mv, remaining)
        if _top and su in _top and _tpi < 1.0 - 1e-12:
            want = max(1.0, want * _tpi)
        want = min(want, mv, remaining)
        if want < 1.0:
            continue
        out.append((su, round(want, 2)))
        remaining -= want
    return out


def plan_bulk_notional_trims_for_free_capital(
    *,
    eligible_symbols: list[str],
    positions: list[dict[str, Any]],
    tracked: dict[str, Any],
    rep_sub: Mapping[str, Any] | None,
    now_dt: datetime,
    incoming_sym_upper: str,
    notional_per_symbol_usd: float,
    max_symbols: int,
    exclude_incoming_symbol: bool,
    require_signal_deterioration: bool = False,
    deterioration_min_gap: float = 0.05,
    broker: Any | None = None,
    engine: Any | None = None,
    top_positions: AbstractSet[str] | None = None,
    top_position_sell_intensity: float = 0.5,
    is_first_rfc_pass: bool = False,
    skip_if_unrealized_pnl_pct_above: float | None = None,
    bulk_trim_priority: Sequence[str] | None = None,
) -> list[tuple[str, float]]:
    """
    Return up to *max_symbols* ``(symbol, sell_notional_usd)`` for **largest** broker positions
    (``market_value``), each notional min(requested, position ``market_value``) and at least
    $1. Applies min-hold and, when *require_signal_deterioration* is set, the same
    :func:`position_has_signal_deterioration` gate as :func:`plan_weakest_trim_for_free_capital`
    (requires live bars + engine for true results).

    When *bulk_trim_priority* is set (from ``risk.bulk_trim_priority``), use
    :func:`symbols_ordered_for_bulk_trim_priority` instead of raw largest-notional order.

    When *top_positions* is set (from :func:`get_top_n_positions`), names in the set
    are reduced **less aggressively** by applying *top_position_sell_intensity* (0–1) to the
    requested notional (default ``0.5`` = half the slice vs non-top names).

    **Do not cut winners early** on the first pass of the entry scan: when *is_first_rfc_pass*
    and *skip_if_unrealized_pnl_pct_above* is set (> 0), skip any line where
    :func:`broker_position_unrealized_pnl_pct` is **strictly greater** than that threshold
    (vs ``|cost_basis|``).
    """
    inc = str(incoming_sym_upper or "").strip().upper()
    elig = (
        [s for s in eligible_symbols if str(s).upper() != inc]
        if exclude_incoming_symbol
        else list(eligible_symbols)
    )
    n_req = max(1.0, float(notional_per_symbol_usd or 0.0))
    try:
        _tpi = max(0.0, min(1.0, float(top_position_sell_intensity)))
    except (TypeError, ValueError):
        _tpi = 0.5
    mcap = int(max(0, max_symbols))
    if mcap <= 0 or not elig:
        return []
    _skip_win: float | None
    if (
        is_first_rfc_pass
        and skip_if_unrealized_pnl_pct_above is not None
        and float(skip_if_unrealized_pnl_pct_above) > 0.0
    ):
        try:
            _skip_win = max(0.0, float(skip_if_unrealized_pnl_pct_above))
        except (TypeError, ValueError, OverflowError):
            _skip_win = None
    else:
        _skip_win = None
    def _gb_trim(s: str) -> Any:
        if broker is None:
            return None
        try:
            return broker.get_bars(s, timeframe="1Day", limit=220)
        except Exception:
            return None
    raw_mh = rep_sub.get("min_hold_minutes") if rep_sub else None
    mh: float | None = None
    if raw_mh is not None and str(raw_mh).strip() != "":
        try:
            mh = float(raw_mh)
        except (TypeError, ValueError):
            mh = None
    if bulk_trim_priority:
        ordered = symbols_ordered_for_bulk_trim_priority(
            elig, positions, bulk_trim_priority
        )
    else:
        ordered = long_stock_symbols_by_market_value_desc(elig, positions)
    _top = {str(s).strip().upper() for s in (top_positions or ())} if top_positions else set()
    out: list[tuple[str, float]] = []
    for sym in ordered:
        if len(out) >= mcap:
            break
        if _skip_win is not None:
            _p_pct = broker_position_unrealized_pnl_pct(positions, str(sym).strip().upper())
            if _p_pct is not None and _p_pct > _skip_win + 1e-9:
                continue
        if require_signal_deterioration:
            if not position_has_signal_deterioration(
                sym,
                tracked,
                positions,
                _gb_trim,
                engine,
                min_gap=float(deterioration_min_gap),
            ):
                continue
        row = tracked.get(sym) or {}
        ok, _reason = replacement_weakest_min_hold_ok(
            weakest_entry_time_iso=row.get("entry_time"),
            now=now_dt,
            min_hold_minutes=mh,
        )
        if not ok:
            continue
        mv = broker_position_market_value_usd(positions, sym)
        if mv <= 0.0:
            continue
        su = str(sym).strip().upper()
        n_leg = n_req
        if _top and su in _top and _tpi < 1.0 - 1e-12:
            n_leg = max(1.0, n_req * _tpi)
        n_eff = min(n_leg, float(mv))
        if n_eff < 1.0:
            continue
        out.append((str(sym).strip().upper(), round(n_eff, 2)))
    return out


def trim_qty_for_fraction(total_qty: int, fraction: float) -> int | None:
    """
    Shares to sell for a partial trim.

    Returns ``None`` when a partial trim is not possible (e.g. only one share).
    Never returns a full exit: at most ``total_qty - 1``.
    """
    qty = int(total_qty)
    if qty < 2:
        return None
    f = max(0.0, min(1.0, float(fraction)))
    if f <= 0.0:
        return None
    raw = int(qty * f)
    trim_qty = max(1, raw)
    trim_qty = min(trim_qty, qty - 1)
    return trim_qty if trim_qty >= 1 else None


def gross_liquidation_trim_shares(
    *,
    account_equity: float,
    current_gross_pct: float,
    target_gross_pct: float,
    passes: int,
    bqty: int,
    mid_price: float,
) -> int | None:
    """
    Whole-share count to sell **from one position** when de-leveraging the **total book** toward
    *target_gross_pct* (``compute_exposures`` scale: 100 = long gross MV equal to equity).

    Dollar gap to the target: ``(current - target) / 100 * equity``; this pass takes ``1/passes`` of
    that notional, converted to shares with ``int(notional / mid)`` (at least 1 when notional and
    price allow). Caps at *bqty* (full exit allowed). Returns:

    * ``None`` when inputs are invalid or mid is not quotable (caller should use fraction-based trim);
    * ``0`` when book is not meaningfully over target;
    * ``1..bqty`` otherwise.
    """
    bq = int(bqty)
    if bq < 1:
        return None
    if mid_price <= 0.0 or mid_price != mid_price:
        return None
    eq = float(account_equity)
    if eq <= 0.0 or eq != eq:
        return None
    try:
        g = float(current_gross_pct)
        t = float(target_gross_pct)
    except (TypeError, ValueError):
        return None
    p = int(max(1, passes))
    excess_pct = g - t
    if excess_pct <= 1e-6:
        return 0
    want_usd = (excess_pct / 100.0) * eq / float(p)
    if want_usd <= 0.0 or want_usd != want_usd:
        return 0
    raw = int(want_usd / float(mid_price))
    if raw < 1:
        raw = 1
    return int(min(max(1, raw), bq))


def just_trimmed_position(symbol_upper: str, symbols_trimmed_this_scan: AbstractSet[str]) -> bool:
    """
    True when *symbol_upper* was partially sold this entry scan only to free buying power
    (``portfolio.rebalance_free_capital``), so the live loop may treat add-backs like ``allow_add``.
    """
    su = str(symbol_upper or "").strip().upper()
    return su in symbols_trimmed_this_scan


def effective_allow_add_after_capital_trim(
    symbol_upper: str,
    *,
    portfolio_allow_add: bool,
    symbols_trimmed_this_scan: AbstractSet[str],
) -> bool:
    """Capital reuse bias: allow add-back to a trimmed symbol even when ``portfolio.allow_add`` is false."""
    if portfolio_allow_add:
        return True
    return just_trimmed_position(symbol_upper, symbols_trimmed_this_scan)


def plan_weakest_trim_for_free_capital(
    *,
    tracked: dict[str, Any],
    eligible_symbols: list[str],
    positions: list[dict[str, Any]],
    rep_sub: Mapping[str, Any] | None,
    now_dt: datetime,
    incoming_sym_upper: str,
    trim_fraction: float,
    exclude_incoming_symbol: bool,
    broker: Any | None = None,
    engine: Any | None = None,
    require_signal_deterioration: bool = False,
    deterioration_min_gap: float = 0.05,
    trim_target: str = "weakest",
    largest_exposure_max_try: int = 3,
    gross_liquidation: Mapping[str, Any] | None = None,
    top_positions: AbstractSet[str] | None = None,
    top_position_trim_intensity: float = 0.5,
    is_first_rfc_pass: bool = False,
    skip_if_unrealized_pnl_pct_above: float | None = None,
) -> tuple[str, int] | None:
    """
    Return ``(symbol, sell_qty)`` or ``None`` if no trim is allowed.

    *eligible_symbols* should match live replacement (long stocks in universe, no bear ETFs).
    When *trim_target* is ``weakest`` (default), the candidate is the lowest-replacement-score hold.
    When *trim_target* selects largest broker notional, symbols are ordered by
    :func:`broker_position_market_value_usd` descending; up to *largest_exposure_max_try* are tried
    in order (each must pass min-hold and, when required, signal deterioration) until one yields a
    non-empty sell.

    When *gross_liquidation* is set with ``enabled: True``, share count comes from
    :func:`gross_liquidation_trim_shares` (reference mid from ``gross_liquidation["get_mid"]``) instead
    of *trim_fraction*; full position exits are allowed. When Mid is bad or the helper returns
    ``None``, falls back to *trim_fraction*.

    When *top_positions* is set (see :func:`get_top_n_positions`), partial trims for those names
    use *trim_fraction* × *top_position_trim_intensity* (default ``0.5``) so the book **reduces less
    aggressively** on the largest lines. The same intensity scales de-lever share counts from
    *gross_liquidation* when that path is active.

    On the **first** RFC pass of the entry scan, lines with :func:`broker_position_unrealized_pnl_pct`
    **above** *skip_if_unrealized_pnl_pct_above* are skipped so winners are not cut early
    (same rule as :func:`plan_bulk_notional_trims_for_free_capital`).
    """
    inc = str(incoming_sym_upper).strip().upper()
    elig = (
        [s for s in eligible_symbols if str(s).upper() != inc]
        if exclude_incoming_symbol
        else list(eligible_symbols)
    )

    def _gb_trim(s: str) -> Any:
        if broker is None:
            return None
        try:
            return broker.get_bars(s, timeframe="1Day", limit=220)
        except Exception:
            return None

    if rfc_uses_largest_exposure_notional_trim(trim_target):
        try:
            mtry = int(largest_exposure_max_try)
        except (TypeError, ValueError):
            mtry = 3
        if mtry <= 0:
            mtry = 3
        candidates = trim_candidate_symbols_largest_exposure_notional(
            elig, positions, max_candidates=mtry
        )
    else:
        weakest, _st = weakest_replacement_hold(
            tracked,
            elig,
            positions=positions,
            get_bars=_gb_trim if broker is not None else None,
            engine=engine,
            rep_sub=rep_sub,
        )
        candidates = [weakest] if weakest else []

    raw_mh = rep_sub.get("min_hold_minutes") if rep_sub else None
    mh: float | None = None
    if raw_mh is not None and str(raw_mh).strip() != "":
        try:
            mh = float(raw_mh)
        except (TypeError, ValueError):
            mh = None

    _top = {str(x).strip().upper() for x in (top_positions or ()) if str(x).strip()}
    try:
        _tint = max(0.0, min(1.0, float(top_position_trim_intensity)))
    except (TypeError, ValueError):
        _tint = 0.5
    _base_tf = max(0.0, min(1.0, float(trim_fraction)))
    _w_skip: float | None
    if (
        is_first_rfc_pass
        and skip_if_unrealized_pnl_pct_above is not None
        and float(skip_if_unrealized_pnl_pct_above) > 0.0
    ):
        try:
            _w_skip = max(0.0, float(skip_if_unrealized_pnl_pct_above))
        except (TypeError, ValueError, OverflowError):
            _w_skip = None
    else:
        _w_skip = None
    for sym in candidates:
        if not sym:
            continue
        if _w_skip is not None:
            _p_pct = broker_position_unrealized_pnl_pct(
                positions, str(sym).strip().upper()
            )
            if _p_pct is not None and _p_pct > _w_skip + 1e-9:
                continue
        if require_signal_deterioration:
            if not position_has_signal_deterioration(
                sym,
                tracked,
                positions,
                _gb_trim,
                engine,
                min_gap=float(deterioration_min_gap),
            ):
                continue
        row = tracked.get(sym) or {}
        ok, _reason = replacement_weakest_min_hold_ok(
            weakest_entry_time_iso=row.get("entry_time"),
            now=now_dt,
            min_hold_minutes=mh,
        )
        if not ok:
            continue
        bqty = broker_long_shares_for_symbol(positions, sym)
        su = str(sym).strip().upper()
        eff_tf = _base_tf
        if _top and su in _top and _tint < 1.0 - 1e-12:
            eff_tf = max(0.0, min(1.0, _base_tf * _tint))
        glq = gross_liquidation
        tq: int | None
        liq_delever: bool = False
        if glq and bool(glq.get("enabled")):
            get_mid = glq.get("get_mid")
            if callable(get_mid):
                try:
                    mid = float(get_mid(str(sym).strip().upper()))
                except Exception:
                    mid = 0.0
            else:
                mid = 0.0
            tq_liq = gross_liquidation_trim_shares(
                account_equity=float(glq.get("account_equity", 0) or 0),
                current_gross_pct=float(glq.get("current_gross_pct", 0) or 0),
                target_gross_pct=float(glq.get("target_gross_pct", 0) or 0),
                passes=int(glq.get("passes", 2) or 2),
                bqty=bqty,
                mid_price=mid,
            )
            if tq_liq is not None and mid > 0.0:
                if tq_liq < 1:
                    continue
                tq = tq_liq
                liq_delever = True
            else:
                tq = trim_qty_for_fraction(bqty, eff_tf)
        else:
            tq = trim_qty_for_fraction(bqty, eff_tf)
        if (
            tq is not None
            and tq >= 1
            and _top
            and su in _top
            and _tint < 1.0 - 1e-12
            and liq_delever
        ):
            # *gross_liquidation* share count does not go through *eff_tf*; scale de-lever for top here.
            tq = max(1, int(float(tq) * _tint + 0.5))
        if tq is None or tq < 1:
            continue
        return sym, tq
    return None


def plan_full_exit_weakest_when_stronger(
    *,
    tracked: dict[str, Any],
    eligible_symbols: list[str],
    positions: list[dict[str, Any]],
    rep_sub: Mapping[str, Any] | None,
    now_dt: datetime,
    incoming_sym_upper: str,
    incoming_signal_strength: float,
    exclude_incoming_symbol: bool,
    broker: Any | None = None,
    engine: Any | None = None,
) -> tuple[str, int] | None:
    """
    When buying power is short: sell **all** shares of the weakest eligible long if and only if
    *incoming_signal_strength* is **strictly greater** than that position's stored
    :func:`~src.portfolio_replacement.tracked_signal_strength`.

    Same min-hold gate as partial trim / replacement (``portfolio.replacement.min_hold_minutes``).
    Returns ``(symbol, broker_qty)`` or ``None``.
    """
    inc = str(incoming_sym_upper).strip().upper()
    elig = (
        [s for s in eligible_symbols if str(s).upper() != inc]
        if exclude_incoming_symbol
        else list(eligible_symbols)
    )
    def _gb_exit(s: str) -> Any:
        if broker is None:
            return None
        try:
            return broker.get_bars(s, timeframe="1Day", limit=220)
        except Exception:
            return None

    weakest, w_st = weakest_replacement_hold(
        tracked,
        elig,
        positions=positions,
        get_bars=_gb_exit if broker is not None else None,
        engine=engine,
        rep_sub=rep_sub,
    )
    if not weakest:
        return None
    if float(incoming_signal_strength) <= float(w_st):
        return None
    row = tracked.get(weakest) or {}
    raw_mh = rep_sub.get("min_hold_minutes") if rep_sub else None
    mh: float | None = None
    if raw_mh is not None and str(raw_mh).strip() != "":
        try:
            mh = float(raw_mh)
        except (TypeError, ValueError):
            mh = None
    ok, _reason = replacement_weakest_min_hold_ok(
        weakest_entry_time_iso=row.get("entry_time"),
        now=now_dt,
        min_hold_minutes=mh,
    )
    if not ok:
        return None
    bqty = broker_long_shares_for_symbol(positions, weakest)
    if bqty < 1:
        return None
    return weakest, bqty


def plan_weakest_gross_unwind_phase1(
    *,
    tracked: dict[str, Any],
    eligible_symbols: list[str],
    positions: list[dict[str, Any]],
    rep_sub: Mapping[str, Any] | None,
    now_dt: datetime,
    incoming_sym_upper: str,
    phase1_weakest_trim_fraction: float,
    exclude_incoming_symbol: bool,
    broker: Any | None = None,
    engine: Any | None = None,
) -> tuple[str, int] | None:
    """
    Two-phase gross unwind, phase 1: sell about *phase1_weakest_trim_fraction* of the weakest
    long. If the weakest line is a single share (no partial 50% possible), close that one share
    to reduce the book.
    """
    f = max(0.05, min(0.99, float(phase1_weakest_trim_fraction or 0.5)))
    pl = plan_weakest_trim_for_free_capital(
        tracked=tracked,
        eligible_symbols=eligible_symbols,
        positions=positions,
        rep_sub=rep_sub,
        now_dt=now_dt,
        incoming_sym_upper=incoming_sym_upper,
        trim_fraction=f,
        exclude_incoming_symbol=exclude_incoming_symbol,
        broker=broker,
        engine=engine,
        require_signal_deterioration=False,
        trim_target="weakest",
        largest_exposure_max_try=3,
        gross_liquidation=None,
        top_positions=None,
        is_first_rfc_pass=False,
        skip_if_unrealized_pnl_pct_above=None,
    )
    if pl is not None:
        return pl
    inc = str(incoming_sym_upper or "").strip().upper()
    elig = (
        [s for s in eligible_symbols if str(s).upper() != inc]
        if exclude_incoming_symbol
        else list(eligible_symbols)
    )

    def _gb(s: str) -> Any:
        if broker is None:
            return None
        try:
            return broker.get_bars(s, timeframe="1Day", limit=220)
        except Exception:
            return None

    wk, _st = weakest_replacement_hold(
        tracked,
        elig,
        positions=positions,
        get_bars=_gb if broker is not None else None,
        engine=engine,
        rep_sub=rep_sub,
    )
    if not wk:
        return None
    bq = broker_long_shares_for_symbol(positions, wk)
    if bq != 1:
        return None
    row = tracked.get(wk) or {}
    raw_mh = rep_sub.get("min_hold_minutes") if rep_sub else None
    mh: float | None = None
    if raw_mh is not None and str(raw_mh).strip() != "":
        try:
            mh = float(raw_mh)
        except (TypeError, ValueError):
            mh = None
    ok, _reason = replacement_weakest_min_hold_ok(
        weakest_entry_time_iso=row.get("entry_time"),
        now=now_dt,
        min_hold_minutes=mh,
    )
    if not ok:
        return None
    return str(wk).strip().upper(), 1


def plan_full_exit_weakest_for_gross_delever(
    *,
    tracked: dict[str, Any],
    eligible_symbols: list[str],
    positions: list[dict[str, Any]],
    rep_sub: Mapping[str, Any] | None,
    now_dt: datetime,
    incoming_sym_upper: str,
    exclude_incoming_symbol: bool,
    broker: Any | None = None,
    engine: Any | None = None,
) -> tuple[str, int] | None:
    """
    De-lever when gross is over cap: sell **all** long shares of the **weakest** eligible name
    (no incoming-signal-strength check). Same min-hold gate as :func:`plan_weakest_trim_for_free_capital`.
    """
    inc = str(incoming_sym_upper or "").strip().upper()
    elig = (
        [s for s in eligible_symbols if str(s).upper() != inc]
        if exclude_incoming_symbol
        else list(eligible_symbols)
    )

    def _gb(s: str) -> Any:
        if broker is None:
            return None
        try:
            return broker.get_bars(s, timeframe="1Day", limit=220)
        except Exception:
            return None

    weakest, _w_st = weakest_replacement_hold(
        tracked,
        elig,
        positions=positions,
        get_bars=_gb if broker is not None else None,
        engine=engine,
        rep_sub=rep_sub,
    )
    if not weakest:
        return None
    row = tracked.get(weakest) or {}
    raw_mh = rep_sub.get("min_hold_minutes") if rep_sub else None
    mh: float | None = None
    if raw_mh is not None and str(raw_mh).strip() != "":
        try:
            mh = float(raw_mh)
        except (TypeError, ValueError):
            mh = None
    ok, _reason = replacement_weakest_min_hold_ok(
        weakest_entry_time_iso=row.get("entry_time"),
        now=now_dt,
        min_hold_minutes=mh,
    )
    if not ok:
        return None
    bqty = broker_long_shares_for_symbol(positions, weakest)
    if bqty < 1:
        return None
    return str(weakest).strip().upper(), bqty


def plan_proportional_gross_delever_notional_trims(
    *,
    eligible_symbols: list[str],
    positions: list[dict[str, Any]],
    tracked: dict[str, Any],
    rep_sub: Mapping[str, Any] | None,
    now_dt: datetime,
    incoming_sym_upper: str,
    exclude_incoming_symbol: bool,
    current_gross_pct: float,
    target_gross_pct: float,
    account_equity: float,
    max_submits: int = 25,
    get_bars: Callable[[str], Any] | None = None,
    engine: Any | None = None,
) -> list[tuple[str, float]]:
    """
    De-lever a **dollar** amount (gross over target) from long **stock** lines (min-hold only).

    Positions are **ranked by strength** (lowest :func:`replacement_hold_strength` first = weakest);
    the gap is covered with a **waterfill** on that order: take from the weakest line up to its
    ``|market_value|``, then the next, until the gap is covered or *max_submits* orders are placed.

    *get_bars* / *engine* follow ``portfolio.replacement`` / ``rebalance`` conventions for the same
    strength scale as rotation’s weakest (see :func:`replacement_hold_strength`).
    """
    inc = str(incoming_sym_upper or "").strip().upper()
    elig = (
        [s for s in eligible_symbols if str(s).upper() != inc]
        if exclude_incoming_symbol
        else list(eligible_symbols)
    )
    try:
        g = float(current_gross_pct)
        t = float(target_gross_pct)
    except (TypeError, ValueError):
        return []
    try:
        eq = float(account_equity)
    except (TypeError, ValueError):
        return []
    if eq <= 0.0 or eq != eq:
        return []
    excess_pct = g - t
    if excess_pct <= 1e-6:
        return []
    want_usd = (excess_pct / 100.0) * eq
    if want_usd < 1.0 or want_usd != want_usd:
        return []
    raw_mh = rep_sub.get("min_hold_minutes") if rep_sub else None
    mh: float | None = None
    if raw_mh is not None and str(raw_mh).strip() != "":
        try:
            mh = float(raw_mh)
        except (TypeError, ValueError):
            mh = None
    mcap = int(max(1, max_submits))
    rows: list[tuple[str, float]] = []
    for sym in long_stock_symbols_by_market_value_desc(elig, positions):
        row = tracked.get(sym) or {}
        ok, _reason = replacement_weakest_min_hold_ok(
            weakest_entry_time_iso=row.get("entry_time"),
            now=now_dt,
            min_hold_minutes=mh,
        )
        if not ok:
            continue
        mv = broker_position_market_value_usd(positions, sym)
        if mv < 1.0:
            continue
        rows.append((str(sym).strip().upper(), float(mv)))
    if not rows:
        return []
    rows.sort(
        key=lambda t: (
            replacement_hold_strength(
                t[0],
                tracked,
                positions,
                get_bars=get_bars,
                engine=engine,
                rep_sub=rep_sub,
                weakest_pick=None,
            ),
            t[0],
        )
    )
    out: list[tuple[str, float]] = []
    rem = float(want_usd)
    for s, mv in rows:
        if rem < 1.0:
            break
        if len(out) >= mcap:
            break
        n_leg = min(float(mv), rem)
        if n_leg >= 1.0:
            n_round = round(float(n_leg), 2)
            out.append((s, n_round))
            rem -= n_round
    return out
