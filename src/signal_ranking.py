"""
Rank trend-long candidates for portfolio top-N and replacement.

Signal **priority tiers** (lower = higher priority) apply only when
``portfolio.signal_ranking.ranking_mode`` is ``tier_then_strength`` **and**
``allocation.rank_by_signal_strength`` is false:

1. SPY / QQQ — 2. NVDA / MSFT — 3. Sector/industry ETFs — 4. Others. Within a tier,
``strength_eff`` breaks ties.

When ``allocation.rank_by_signal_strength`` is **true**, Top-K uses ``allocation.rank_top_k_by``
(default ``strength_eff``). Set ``rank_top_k_by: momentum_rs_volume`` to rank by
:func:`row_momentum_rs_volume_score` (**momentum + relative_strength + volume**), avoiding the
legacy tier bias where broad ETFs could crowd out stronger tape names.

Optional ``ranking_mode: signal_priority`` (when **not** using allocation Top-K strength mode) ignores
tiers and keeps the top *N* by :func:`row_signal_priority_score` —
``trend_strength + momentum + volatility_expansion + relative_strength``.

Optional ``ranking_mode: composite_score`` ignores tiers and keeps the top *N* rows by weighted
entry rank (``composite_score``), i.e. ``sort(rows, key=composite)[...]``.

When ``portfolio.signal_ranking.recent_add_priority`` is enabled, symbols that already have an
open long with a fill / entry within ``recent_minutes`` (tracker ``last_add_time`` / ``last_scale_ts``
/ ``entry_time`` — see :func:`~src.position_tracker.last_entry_within`) get lower rank: optional tier bump,
and scaled-down ``strength_eff`` / ``composite_score`` for sorting (live loop applies before rank).

**Entry-rank (scaled to 0–3 to match the historical 3-pillar max):** weights from
``portfolio.signal_ranking.composite_weights`` (default 0.35 / 0.25 / 0.20 / 0.20) — see
:func:`trend_long_composite_rank`. Subscores are in ``[0, 1]``; the weighted sum in ``[0, 1]`` is
multiplied by 3, then event-trigger bonuses (``+0``/``+1`` each) are added. Callers normalize with
:func:`event_triggers_strength_denom` before :func:`portfolio_replacement.effective_signal_strength`.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

from src.strategy import _atr

# US-listed sector / industry / thematic ETFs (non-exhaustive; extend via config).
_DEFAULT_SECTOR_ETF_SYMBOLS: frozenset[str] = frozenset(
    {
        "XLF",
        "XLK",
        "XLE",
        "XLV",
        "XLI",
        "XLP",
        "XLY",
        "XLB",
        "XLU",
        "XLRE",
        "XLC",
        "XBI",
        "XOP",
        "XHB",
        "XRT",
        "XME",
        "GLD",
        "SLV",
        "USO",
        "TLT",
        "IEF",
        "LQD",
        "HYG",
        "SMH",
        "SOXX",
        "ITA",
        "XAR",
        "KRE",
        "KBE",
        "XES",
        "XPH",
        "XHS",
        "XHE",
        "XWEB",
        "XTH",
        "XSW",
        "REM",
        "VNQ",
        "IYR",
    }
)

_TIER_SPY_QQQ = 0
_TIER_NVDA_MSFT = 1
_TIER_SECTOR_ETF = 2
_TIER_OTHER = 3

# Default entry-rank weights (sum normalized to 1 in :func:`parse_composite_weights_from_portfolio`).
DEFAULT_COMPOSITE_WEIGHTS: dict[str, float] = {
    "trend_strength": 0.35,
    "momentum": 0.25,
    "volatility_expansion": 0.20,
    "relative_strength": 0.20,
}


def parse_composite_weights_from_portfolio(
    portfolio_cfg: Mapping[str, Any] | None,
) -> dict[str, float]:
    """
    Read ``portfolio.signal_ranking.composite_weights``; unknown keys ignored; renormalize to sum 1.
    """
    out = dict(DEFAULT_COMPOSITE_WEIGHTS)
    if not portfolio_cfg or not isinstance(portfolio_cfg, dict):
        return out
    sr = portfolio_cfg.get("signal_ranking")
    if not isinstance(sr, dict):
        return out
    cw = sr.get("composite_weights")
    if not isinstance(cw, dict):
        return out
    for k in DEFAULT_COMPOSITE_WEIGHTS:
        if k in cw and cw[k] is not None and str(cw[k]).strip() != "":
            try:
                out[k] = float(cw[k])
            except (TypeError, ValueError):
                pass
    s = sum(max(0.0, float(x)) for x in out.values())
    if s > 1e-12:
        for k in out:
            out[k] = max(0.0, float(out[k])) / s
    return out


def sector_etf_symbol_frozenset(config: Mapping[str, Any] | None) -> frozenset[str]:
    """
    Default sector-ETF set plus optional ``portfolio.signal_ranking.sector_etf_symbols`` list.
    """
    base = set(_DEFAULT_SECTOR_ETF_SYMBOLS)
    if not config:
        return frozenset(base)
    port = config.get("portfolio") or {}
    sr = port.get("signal_ranking")
    if not isinstance(sr, dict):
        return frozenset(base)
    raw = sr.get("sector_etf_symbols")
    if isinstance(raw, (list, tuple, set)):
        for x in raw:
            if x is not None and str(x).strip():
                base.add(str(x).strip().upper())
    return frozenset(base)


def symbol_signal_priority_tier(sym_u: str, sector_etfs: frozenset[str]) -> int:
    """Return 0 (best) .. 3 for use in sort keys (ascending tier = higher priority)."""
    s = str(sym_u).strip().upper()
    if s in ("SPY", "QQQ"):
        return _TIER_SPY_QQQ
    if s in ("NVDA", "MSFT"):
        return _TIER_NVDA_MSFT
    if s in sector_etfs:
        return _TIER_SECTOR_ETF
    return _TIER_OTHER


SIGNAL_RANKING_MODE_TIER = "tier_then_strength"
SIGNAL_RANKING_MODE_COMPOSITE = "composite_score"
# Sort by ``strength_eff`` only (ignore SPY/QQQ tier); use with ``allocation.rank_by_signal_strength``.
SIGNAL_RANKING_MODE_STRENGTH = "signal_strength"
# Unweighted sum of four ``[0, 1]`` pillars: trend + momentum + volatility expansion + relative strength.
SIGNAL_RANKING_MODE_SIGNAL_PRIORITY = "signal_priority"
# momentum + relative_strength + volume (from ``rank_breakdown``); max ~3.0 on [0,1] pillars.
SIGNAL_RANKING_MODE_MRV = "momentum_rs_volume"
SIGNAL_RANKING_MODE_MVE = "momentum_volume_ema"

_SIGNAL_PRIORITY_SUBSCORE_KEYS: tuple[str, ...] = (
    "trend_strength",
    "momentum",
    "volatility_expansion",
    "relative_strength",
)


def canonical_signal_ranking_mode(
    raw: str | None,
    *,
    allocation_rank_by_strength: bool = False,
    allocation_rank_top_k_by: str | None = None,
) -> str:
    """
    Normalize YAML / UI strings to :data:`SIGNAL_RANKING_MODE_*` constants.

    When *allocation_rank_by_strength* is true (``allocation.rank_by_signal_strength``), Top-K mode
    is chosen by *allocation_rank_top_k_by* (``allocation.rank_top_k_by``): default
    ``strength_eff`` → :data:`SIGNAL_RANKING_MODE_STRENGTH`; ``momentum_rs_volume`` →
    :data:`SIGNAL_RANKING_MODE_MRV`. *raw* is ignored in that branch.
    """
    if allocation_rank_by_strength:
        metric = str(allocation_rank_top_k_by or "strength_eff").strip().lower()
        if metric in (
            SIGNAL_RANKING_MODE_MRV,
            "mrv",
            "mom_rs_vol",
            "momentum_relative_strength_volume",
        ):
            return SIGNAL_RANKING_MODE_MRV
        if metric in (
            SIGNAL_RANKING_MODE_MVE,
            "mve",
            "momentum_volume_ema",
            "momentum_volume_spike_distance_from_ema",
        ):
            return SIGNAL_RANKING_MODE_MVE
        return SIGNAL_RANKING_MODE_STRENGTH
    s = str(raw or "").strip().lower()
    if not s:
        return SIGNAL_RANKING_MODE_TIER
    if s in (
        SIGNAL_RANKING_MODE_SIGNAL_PRIORITY,
        "priority_score",
        "priority",
        "four_pillar",
        "tmvr",
    ):
        return SIGNAL_RANKING_MODE_SIGNAL_PRIORITY
    if s in (SIGNAL_RANKING_MODE_COMPOSITE, "composite", "weighted_composite"):
        return SIGNAL_RANKING_MODE_COMPOSITE
    if s in (SIGNAL_RANKING_MODE_STRENGTH, "strength", "strength_eff"):
        return SIGNAL_RANKING_MODE_STRENGTH
    if s in (
        SIGNAL_RANKING_MODE_MRV,
        "mrv",
        "mom_rs_vol",
        "momentum_relative_strength_volume",
    ):
        return SIGNAL_RANKING_MODE_MRV
    if s in (
        SIGNAL_RANKING_MODE_MVE,
        "mve",
        "momentum_volume_ema",
        "momentum_volume_spike_distance_from_ema",
    ):
        return SIGNAL_RANKING_MODE_MVE
    return SIGNAL_RANKING_MODE_TIER


def row_signal_priority_score(row: Mapping[str, Any]) -> float:
    """
    **Signal priority score** — unweighted sum of four subscores in ``[0, 1]`` each (range ``[0, 4]``):

    ``trend_strength + momentum + volatility_expansion + relative_strength``

    Uses explicit ``priority_score`` on *row* when set; otherwise sums the four keys from
    ``rank_breakdown`` (event-trigger bonuses are excluded).

    Legacy 3-pillar breakdowns (``pnl_potential`` without volatility pillars) fall back to summing
    whatever numeric keys exist among the four canonical names plus ``pnl_potential`` only when none
    of the four-pillar keys are present (partial sums).
    """
    raw_ps = row.get("priority_score")
    if raw_ps is not None and str(raw_ps).strip() != "":
        try:
            v = float(raw_ps)
            return v if v == v else 0.0
        except (TypeError, ValueError):
            pass
    rb = row.get("rank_breakdown")
    if not isinstance(rb, dict):
        return 0.0
    s = 0.0
    any_four = False
    for k in _SIGNAL_PRIORITY_SUBSCORE_KEYS:
        if k not in rb:
            continue
        any_four = True
        try:
            x = float(rb[k])
        except (TypeError, ValueError):
            continue
        if x == x:
            s += x
    if any_four:
        return float(s)
    if "pnl_potential" in rb and "volatility_expansion" not in rb:
        for k in ("trend_strength", "momentum", "pnl_potential"):
            if k not in rb:
                continue
            try:
                x = float(rb[k])
            except (TypeError, ValueError):
                continue
            if x == x:
                s += x
        return float(s)
    for k, val in rb.items():
        if k in ("volatility_breakout", "volume_anomaly"):
            continue
        if k not in _SIGNAL_PRIORITY_SUBSCORE_KEYS and k != "pnl_potential":
            continue
        try:
            x = float(val)
        except (TypeError, ValueError):
            continue
        if x == x:
            s += x
    return float(s)


def row_momentum_rs_volume_score(row: Mapping[str, Any]) -> float:
    """
    **Momentum + relative strength + volume** — unweighted sum of up to three ``[0, 1]`` subscores
    from ``rank_breakdown``: ``momentum``, ``relative_strength``, and volume (``volume_signal`` or
    ``volume``). Used for Top-K when ``allocation.rank_top_k_by: momentum_rs_volume``.

    When :func:`apply_recent_add_rank_penalty` marked the row, scales by ``recent_add_rank_scale``.
    """
    raw_ps = row.get("momentum_rs_volume_score")
    if raw_ps is not None and str(raw_ps).strip() != "":
        try:
            v = float(raw_ps)
            return v if v == v else 0.0
        except (TypeError, ValueError):
            pass
    rb = row.get("rank_breakdown")
    if not isinstance(rb, dict):
        return 0.0
    total = 0.0
    for k in ("momentum", "relative_strength"):
        if k not in rb:
            continue
        try:
            x = float(rb[k])
        except (TypeError, ValueError):
            continue
        if x == x:
            total += x
    vol = 0.0
    picked = False
    for vk in ("volume_signal", "volume"):
        if vk not in rb:
            continue
        try:
            vv = float(rb[vk])
        except (TypeError, ValueError):
            continue
        if vv == vv:
            vol = vv
            picked = True
            break
    if picked:
        total += vol
    scale = 1.0
    if row.get("recent_add_penalized"):
        try:
            scale = float(row.get("recent_add_rank_scale", 0.72))
        except (TypeError, ValueError):
            scale = 0.72
        scale = max(0.0, min(1.0, scale))
    return float(total * scale)


def row_momentum_volume_ema_score(row: Mapping[str, Any]) -> float:
    raw = row.get("momentum_volume_ema_score")
    if raw is not None and str(raw).strip() != "":
        try:
            v = float(raw)
            return v if v == v else 0.0
        except (TypeError, ValueError):
            pass
    rb = row.get("rank_breakdown")
    if not isinstance(rb, dict):
        return 0.0
    total = 0.0
    for key in ("momentum", "trend_strength"):
        if key not in rb:
            continue
        try:
            x = float(rb[key])
        except (TypeError, ValueError):
            continue
        if x == x:
            total += x
    for vk in ("volume_signal", "relative_strength", "volume"):
        if vk not in rb:
            continue
        try:
            vv = float(rb[vk])
        except (TypeError, ValueError):
            continue
        if vv == vv:
            total += vv
            break
    scale = 1.0
    if row.get("recent_add_penalized"):
        try:
            scale = float(row.get("recent_add_rank_scale", 0.72))
        except (TypeError, ValueError):
            scale = 0.72
        scale = max(0.0, min(1.0, scale))
    return float(total * scale)


def max_signals_per_loop_from_portfolio(portfolio_cfg: Mapping[str, Any] | None) -> int:
    """
    Max ranked trend-long signals per live-loop pass (when **not** using Top-K by strength).

    When ``allocation.rank_by_signal_strength`` is true, the live loop uses
    :func:`src.allocation_config.effective_ranked_signals_cap` instead (single K from ``allocate_top_n``).

    Uses ``portfolio.signal_ranking.max_signals_per_loop`` when set; otherwise, if
    ``portfolio.allocator`` has ``type`` ``rank_based`` or ``ranked``, uses ``allocator.top_n``, else
    ``allocator.max_positions``, else ``10``; otherwise ``3``.
    """
    port = portfolio_cfg if isinstance(portfolio_cfg, dict) else {}
    sr = port.get("signal_ranking")
    sr = sr if isinstance(sr, dict) else {}
    raw = sr.get("max_signals_per_loop")
    if raw is not None and str(raw).strip() != "":
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 3
    alloc = port.get("allocator")
    alloc = alloc if isinstance(alloc, dict) else {}
    _at = str(alloc.get("type", "")).strip().lower()
    if _at in ("rank_based", "ranked"):
        for key in ("top_n", "max_positions"):
            raw_n = alloc.get(key)
            if raw_n is not None and str(raw_n).strip() != "":
                try:
                    return max(0, int(raw_n))
                except (TypeError, ValueError):
                    continue
        return max(0, 10)
    return 3


def parse_recent_add_priority_cfg(portfolio_cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    """
    ``portfolio.signal_ranking.recent_add_priority`` — lower priority for symbols added / scaled recently.

    Keys: ``enabled``, ``recent_minutes`` (window for :func:`~src.position_tracker.last_entry_within`),
    ``strength_eff_multiplier``, ``composite_score_multiplier`` (each in ``(0, 1]`` applied when recent),
    ``extra_priority_tier`` (non-negative integer tiers to add toward the bottom bucket).
    """
    out: dict[str, Any] = {
        "enabled": False,
        "recent_minutes": 2880.0,
        "strength_eff_multiplier": 0.72,
        "composite_score_multiplier": 0.72,
        "extra_priority_tier": 1,
    }
    if not portfolio_cfg or not isinstance(portfolio_cfg, dict):
        return out
    sr = portfolio_cfg.get("signal_ranking")
    if not isinstance(sr, dict):
        return out
    sub = sr.get("recent_add_priority")
    if not isinstance(sub, dict):
        return out
    out["enabled"] = bool(sub.get("enabled", False))
    for key, default in (
        ("recent_minutes", 2880.0),
        ("strength_eff_multiplier", 0.72),
        ("composite_score_multiplier", 0.72),
    ):
        raw = sub.get(key)
        if raw is None or str(raw).strip() == "":
            out[key] = default
        else:
            try:
                out[key] = float(raw)
            except (TypeError, ValueError):
                out[key] = default
    raw_et = sub.get("extra_priority_tier")
    if raw_et is None or str(raw_et).strip() == "":
        out["extra_priority_tier"] = 1
    else:
        try:
            out["extra_priority_tier"] = max(0, int(raw_et))
        except (TypeError, ValueError):
            out["extra_priority_tier"] = 1
    return out


def apply_recent_add_rank_penalty(
    row: dict[str, Any],
    *,
    is_recent_add: bool,
    strength_eff_multiplier: float = 0.72,
    composite_score_multiplier: float = 0.72,
    extra_priority_tier: int = 1,
) -> None:
    """In-place: lower sort priority when *is_recent_add* (tier mode + strength/composite sorts)."""
    if not is_recent_add:
        return
    sm = float(strength_eff_multiplier)
    cm = float(composite_score_multiplier)
    sm = max(0.0, min(1.0, sm))
    cm = max(0.0, cm)
    et = max(0, int(extra_priority_tier))
    try:
        se = float(row.get("strength_eff", 0.0))
        row["strength_eff"] = se * sm
    except (TypeError, ValueError):
        pass
    if row.get("composite_score") is not None:
        try:
            row["composite_score"] = float(row["composite_score"]) * cm
        except (TypeError, ValueError):
            pass
    if row.get("priority_score") is not None:
        try:
            row["priority_score"] = float(row["priority_score"]) * cm
        except (TypeError, ValueError):
            pass
    try:
        t = int(row.get("tier", _TIER_OTHER))
    except (TypeError, ValueError):
        t = _TIER_OTHER
    row["tier"] = min(_TIER_OTHER, t + et)
    row["recent_add_rank_scale"] = cm
    row["recent_add_penalized"] = True


def row_composite_score(row: Mapping[str, Any]) -> float:
    """
    Total entry-rank (≈ ``[0, 3]`` base + event bonuses), for dedupe and ``composite_score`` sort.

    Prefers ``composite_score`` (set from :func:`trend_long_composite_rank`); else reconstructs from
    ``rank_breakdown`` (legacy 3-pillar or new 4-pillar, excluding binary trigger keys).
    """
    raw = row.get("composite_score")
    if raw is not None and str(raw).strip() != "":
        try:
            v = float(raw)
            return v if v == v else 0.0
        except (TypeError, ValueError):
            pass
    rb = row.get("rank_breakdown")
    if not isinstance(rb, dict):
        return 0.0
    if "pnl_potential" in rb and "volatility_expansion" not in rb:
        s = 0.0
        for k in ("trend_strength", "momentum", "pnl_potential"):
            if k in rb:
                try:
                    s += float(rb[k])
                except (TypeError, ValueError):
                    pass
        return s
    if "volatility_expansion" in rb and "relative_strength" in rb:
        w = dict(DEFAULT_COMPOSITE_WEIGHTS)
        try:
            w01 = _clamp01(
                w["trend_strength"] * float(rb.get("trend_strength", 0.0))
                + w["momentum"] * float(rb.get("momentum", 0.0))
                + w["volatility_expansion"] * float(rb.get("volatility_expansion", 0.0))
                + w["relative_strength"] * float(rb.get("relative_strength", 0.0))
            )
        except (TypeError, ValueError):
            w01 = 0.0
        return 3.0 * w01
    s = 0.0
    for k, val in rb.items():
        if k in ("volatility_breakout", "volume_anomaly"):
            continue
        try:
            x = float(val)
        except (TypeError, ValueError):
            continue
        if x == x:
            s += x
    return s


def rank_trend_long_candidate_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    max_take: int,
    sector_etfs: frozenset[str],
    sym_key: str = "sym_u",
    strength_key: str = "strength_eff",
    ranking_mode: str = SIGNAL_RANKING_MODE_TIER,
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    * ``tier_then_strength`` (default) — sort by (priority tier, -strength_eff, symbol).
    * ``composite_score`` — sort by (-composite raw score, symbol); *sector_etfs* unused.
    * ``signal_priority`` — sort by (-:func:`row_signal_priority_score`, symbol); *sector_etfs* unused.
    * ``momentum_rs_volume`` — sort by (-:func:`row_momentum_rs_volume_score`, symbol).
    * ``momentum_volume_ema`` — sort by (-:func:`row_momentum_volume_ema_score`, symbol).
    """
    if max_take <= 0:
        return [], [str(r[sym_key]).upper() for r in rows]
    mode = (ranking_mode or SIGNAL_RANKING_MODE_TIER).strip().lower()
    if mode == SIGNAL_RANKING_MODE_STRENGTH or mode in ("strength", "strength_eff"):
        enriched_st: list[tuple[float, str, dict[str, Any]]] = []
        for r in rows:
            row = dict(r)
            su = str(row[sym_key]).upper()
            try:
                eff = float(row.get(strength_key) or 0.0)
            except (TypeError, ValueError):
                eff = 0.0
            enriched_st.append((-eff, su, row))
        enriched_st.sort()
        chosen = [e[-1] for e in enriched_st[:max_take]]
        chosen_syms = {str(c[sym_key]).upper() for c in chosen}
        dropped = [str(r[sym_key]).upper() for r in rows if str(r[sym_key]).upper() not in chosen_syms]
        return chosen, dropped
    if mode == SIGNAL_RANKING_MODE_SIGNAL_PRIORITY:
        enriched_ps: list[tuple[float, str, dict[str, Any]]] = []
        for r in rows:
            row = dict(r)
            su = str(row[sym_key]).upper()
            ps = row_signal_priority_score(row)
            enriched_ps.append((-ps, su, row))
        enriched_ps.sort()
        chosen = [e[-1] for e in enriched_ps[:max_take]]
        chosen_syms = {str(c[sym_key]).upper() for c in chosen}
        dropped = [str(r[sym_key]).upper() for r in rows if str(r[sym_key]).upper() not in chosen_syms]
        return chosen, dropped
    if mode == SIGNAL_RANKING_MODE_MRV or mode in ("momentum_rs_volume", "mrv"):
        enriched_mv: list[tuple[float, str, dict[str, Any]]] = []
        for r in rows:
            row = dict(r)
            su = str(row[sym_key]).upper()
            mv = row_momentum_rs_volume_score(row)
            enriched_mv.append((-mv, su, row))
        enriched_mv.sort()
        chosen = [e[-1] for e in enriched_mv[:max_take]]
        chosen_syms = {str(c[sym_key]).upper() for c in chosen}
        dropped = [str(r[sym_key]).upper() for r in rows if str(r[sym_key]).upper() not in chosen_syms]
        return chosen, dropped
    if mode == SIGNAL_RANKING_MODE_MVE or mode in ("momentum_volume_ema", "mve"):
        enriched_mve: list[tuple[float, str, dict[str, Any]]] = []
        for r in rows:
            row = dict(r)
            su = str(row[sym_key]).upper()
            mv = row_momentum_volume_ema_score(row)
            enriched_mve.append((-mv, su, row))
        enriched_mve.sort()
        chosen = [e[-1] for e in enriched_mve[:max_take]]
        chosen_syms = {str(c[sym_key]).upper() for c in chosen}
        dropped = [str(r[sym_key]).upper() for r in rows if str(r[sym_key]).upper() not in chosen_syms]
        return chosen, dropped
    if mode == SIGNAL_RANKING_MODE_COMPOSITE:
        enriched_cs: list[tuple[float, str, dict[str, Any]]] = []
        for r in rows:
            row = dict(r)
            su = str(row[sym_key]).upper()
            sc = row_composite_score(row)
            enriched_cs.append((-sc, su, row))
        enriched_cs.sort()
        chosen = [e[-1] for e in enriched_cs[:max_take]]
        chosen_syms = {str(c[sym_key]).upper() for c in chosen}
        dropped = [str(r[sym_key]).upper() for r in rows if str(r[sym_key]).upper() not in chosen_syms]
        return chosen, dropped

    enriched: list[tuple[int, float, str, dict[str, Any]]] = []
    for r in rows:
        row = dict(r)
        su = str(row[sym_key]).upper()
        tier = symbol_signal_priority_tier(su, sector_etfs)
        eff = float(row.get(strength_key) or 0.0)
        enriched.append((tier, -eff, su, row))
    enriched.sort()
    chosen = [e[-1] for e in enriched[:max_take]]
    chosen_syms = {str(c[sym_key]).upper() for c in chosen}
    dropped = [str(r[sym_key]).upper() for r in rows if str(r[sym_key]).upper() not in chosen_syms]
    return chosen, dropped


def top_trend_long_candidates_by_composite_score(
    rows: Sequence[Mapping[str, Any]],
    *,
    max_take: int,
    sector_etfs: frozenset[str],
    sym_key: str = "sym_u",
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    ``top_candidates = sort(rows, key=row_composite_score, reverse=True)[:max_take]``.

    Same return shape as :func:`rank_trend_long_candidate_rows` with ``ranking_mode=composite_score``.
    """
    return rank_trend_long_candidate_rows(
        rows,
        max_take=max_take,
        sector_etfs=sector_etfs,
        sym_key=sym_key,
        ranking_mode=SIGNAL_RANKING_MODE_COMPOSITE,
    )


def _clamp01(x: float) -> float:
    if x != x:  # NaN
        return 0.0
    return max(0.0, min(1.0, float(x)))


def event_triggers_strength_denom(event_triggers: Mapping[str, Any] | None) -> float:
    """
    Normalizer for ``effective_signal_strength(total / denom, ...)``.

    Base composite is a weighted 4-pillar score, scaled to max 3 (see :func:`trend_long_composite_rank`).
    Each enabled event trigger can add at most 1 point, so *denom* is ``3 +`` (count of enabled
    trigger slots).
    """
    if not event_triggers or not bool(event_triggers.get("enabled", False)):
        return 3.0
    extra = 0.0
    vb = event_triggers.get("volatility_breakout")
    if isinstance(vb, dict) and bool(vb.get("enabled", True)):
        extra += 1.0
    va = event_triggers.get("volume_anomaly")
    if isinstance(va, dict) and bool(va.get("enabled", True)):
        extra += 1.0
    return 3.0 + extra


def pnl_potential_from_vol_and_volume(*, volatility_quality: float, volume: float) -> float:
    """
    Single ``[0, 1]`` term: blend orderly tape (volatility_quality) and participation (volume).

    Weights are equal by default — both inputs are already ``[0, 1]`` from the same pipelines as
    :func:`trend_momentum_volume_subscores` / ATR-vs-cap quality.
    """
    vq = _clamp01(float(volatility_quality))
    vol = _clamp01(float(volume))
    return _clamp01(0.5 * vq + 0.5 * vol)


def _atr_pct_series(df: pd.DataFrame, period: int) -> pd.Series | None:
    """Daily ATR%% series (ATR / close * 100), same TR definition as :func:`src.strategy._atr`."""
    if df is None or df.empty or "close" not in df.columns or period < 1:
        return None
    close = df["close"].astype(float)
    if "high" in df.columns and "low" in df.columns:
        high = df["high"].astype(float)
        low = df["low"].astype(float)
    else:
        high = low = close
    if len(close) < period + 2:
        return None
    atr = _atr(high, low, close, period)
    out = (atr / close) * 100.0
    return out


def _atr_expansion_subscore(
    df: pd.DataFrame,
    *,
    atr_period: int,
    baseline_bars: int = 20,
) -> float:
    """
    ``[0, 1]`` score: ATR%% *today* relative to the mean ATR%% over the prior *baseline_bars* days
    (excludes today). Rewards **expansion** (ratio > 1); flat or contracting → lower.
    """
    ser = _atr_pct_series(df, atr_period)
    if ser is None:
        return 0.0
    valid = ser.dropna()
    n = int(baseline_bars)
    if n < 1 or len(valid) < n + 1:
        return 0.0
    atr_today = float(valid.iloc[-1])
    baseline = float(valid.iloc[-(n + 1) : -1].mean())
    if baseline <= 0.0 or baseline != baseline or atr_today != atr_today:
        return 0.0
    r = atr_today / baseline
    return _clamp01(max(0.0, (r - 1.0) / 1.5))


def _volatility_breakout_bonus(
    df: pd.DataFrame,
    vb: Mapping[str, Any],
    atr_period: int,
) -> float:
    """
    +1 when today's ATR%% exceeds *atr_multiple* times the mean ATR%% over the prior *baseline_bars*
    days (excludes today), matching ``ATR_today > 1.5 * ATR_20d`` style rules.
    """
    if not bool(vb.get("enabled", True)):
        return 0.0
    mult = float(vb.get("atr_multiple", 1.5))
    n = int(vb.get("baseline_bars", 20))
    if n < 1:
        return 0.0
    ser = _atr_pct_series(df, atr_period)
    if ser is None:
        return 0.0
    valid = ser.dropna()
    if len(valid) < n + 1:
        return 0.0
    atr_today = float(valid.iloc[-1])
    baseline = float(valid.iloc[-(n + 1) : -1].mean())
    if baseline <= 0.0 or baseline != baseline or atr_today != atr_today:
        return 0.0
    return 1.0 if atr_today > mult * baseline else 0.0


def _volume_anomaly_bonus(df: pd.DataFrame, va: Mapping[str, Any], *, avg_volume_bars: int) -> float:
    """+1 when last volume exceeds *volume_multiple* times mean volume over prior *avg_volume_bars* days (excludes today)."""
    if not bool(va.get("enabled", True)):
        return 0.0
    mult = float(va.get("volume_multiple", 2.0))
    n = int(va.get("avg_volume_bars", avg_volume_bars))
    if n < 1 or "volume" not in df.columns or len(df) < n + 1:
        return 0.0
    vol_s = df["volume"].astype(float)
    prior = vol_s.iloc[-(n + 1) : -1]
    if prior.empty:
        return 0.0
    avg = float(prior.mean())
    last_v = float(vol_s.iloc[-1])
    if avg <= 0.0 or last_v != last_v:
        return 0.0
    return 1.0 if last_v > mult * avg else 0.0


def trend_momentum_volume_subscores(
    df: pd.DataFrame,
    *,
    ma_slow: int = 200,
    momentum_bars: int = 10,
    volume_bars: int = 20,
) -> dict[str, float]:
    """
    Three 0–1 components aligned with :func:`trend_long_composite_rank` (trend / momentum / volume).

    Trend strength uses the slow MA only (same as composite ranking).
    """
    out = {"trend_strength": 0.0, "momentum": 0.0, "volume": 0.0}
    if df is None or df.empty or "close" not in df.columns:
        return out
    close = df["close"].astype(float)
    trend_strength = 0.0
    if len(close) >= ma_slow:
        ma_s = close.rolling(ma_slow).mean().iloc[-1]
        if not pd.isna(ma_s) and float(ma_s) > 0:
            px = float(close.iloc[-1])
            raw = (px - float(ma_s)) / px if px > 0 else 0.0
            trend_strength = _clamp01(raw * 25.0)
    momentum = 0.0
    if len(close) > momentum_bars:
        prev = float(close.iloc[-1 - momentum_bars])
        cur = float(close.iloc[-1])
        if prev > 0:
            roc = cur / prev - 1.0
            momentum = _clamp01(max(0.0, roc) * 20.0)
    volume = 0.0
    if "volume" in df.columns and len(df) >= volume_bars:
        vol_s = df["volume"].astype(float)
        avg = float(vol_s.rolling(volume_bars).mean().iloc[-1])
        last_v = float(vol_s.iloc[-1])
        if avg > 0 and last_v == last_v:
            ratio = last_v / avg
            volume = _clamp01((ratio - 0.5) / 1.5)
    out["trend_strength"] = float(trend_strength)
    out["momentum"] = float(momentum)
    out["volume"] = float(volume)
    return out


def confidence_score_trend_momentum_volume(
    df: pd.DataFrame | None,
    *,
    ma_slow: int = 200,
    momentum_bars: int = 10,
    volume_bars: int = 20,
) -> tuple[float, dict[str, float]]:
    """
    ``confidence_score = trend_strength + momentum_strength + volume_signal`` (each in ``[0, 1]``).

    Uses the same subscores as composite ranking; returned breakdown uses the names above
    (``momentum_strength`` / ``volume_signal`` map to momentum and volume subscores).
    """
    if df is None or df.empty or "close" not in df.columns:
        z = {"trend_strength": 0.0, "momentum_strength": 0.0, "volume_signal": 0.0}
        return 0.0, z
    d = trend_momentum_volume_subscores(
        df,
        ma_slow=ma_slow,
        momentum_bars=momentum_bars,
        volume_bars=volume_bars,
    )
    bd = {
        "trend_strength": d["trend_strength"],
        "momentum_strength": d["momentum"],
        "volume_signal": d["volume"],
    }
    total = bd["trend_strength"] + bd["momentum_strength"] + bd["volume_signal"]
    return float(total), bd


def trend_long_composite_rank(
    df: pd.DataFrame,
    *,
    atr_pct: float | None,
    max_atr_pct: float,
    ma_fast: int = 20,
    ma_slow: int = 200,
    momentum_bars: int = 10,
    volume_bars: int = 20,
    event_triggers: Mapping[str, Any] | None = None,
    atr_period: int = 14,
    composite_weights: Mapping[str, float] | None = None,
) -> tuple[float, dict[str, float], float]:
    """
    Return ``(total, breakdown, strength_denom)``.

    Base (before event-trigger bonuses) is a weighted sum of four ``[0,1]`` subscores
    (see ``DEFAULT_COMPOSITE_WEIGHTS``), then multiplied by 3 to preserve the same scale
    as the old ``trend + mom + pnl (max 3)`` model:

    * *trend_strength* — close vs slow MA.
    * *momentum* — N-bar return.
    * *volatility_expansion* — ATR%% today vs prior baseline.
    * *relative_strength* — relative volume vs rolling average (participation).

    *volatility_breakout* / *volume_anomaly* — optional +1 each (event triggers; unchanged).
    """
    w = (
        dict(composite_weights) if isinstance(composite_weights, dict) else None
    ) or dict(DEFAULT_COMPOSITE_WEIGHTS)
    strength_denom = event_triggers_strength_denom(event_triggers)
    if df is None or df.empty or "close" not in df.columns:
        z = {
            "trend_strength": 0.0,
            "momentum": 0.0,
            "volatility_expansion": 0.0,
            "relative_strength": 0.0,
        }
        return 0.0, z, strength_denom

    tvm = trend_momentum_volume_subscores(
        df,
        ma_slow=ma_slow,
        momentum_bars=momentum_bars,
        volume_bars=volume_bars,
    )
    trend_strength = float(tvm["trend_strength"])
    momentum = float(tvm["momentum"])
    rel_vol = float(tvm["volume"])
    relative_strength = rel_vol

    volatility_expansion = _atr_expansion_subscore(
        df, atr_period=atr_period, baseline_bars=20
    )

    weighted_01 = _clamp01(
        float(w.get("trend_strength", 0.35)) * trend_strength
        + float(w.get("momentum", 0.25)) * momentum
        + float(w.get("volatility_expansion", 0.20)) * volatility_expansion
        + float(w.get("relative_strength", 0.20)) * relative_strength
    )

    breakdown: dict[str, float] = {
        "trend_strength": trend_strength,
        "momentum": momentum,
        "volatility_expansion": volatility_expansion,
        "relative_strength": relative_strength,
    }
    # Scale 0..1 to 0..3 so legacy (total / denom) stays comparable.
    base_total = 3.0 * weighted_01
    bonus = 0.0
    if event_triggers and bool(event_triggers.get("enabled", False)):
        et = dict(event_triggers)
        vb_cfg = et.get("volatility_breakout")
        if isinstance(vb_cfg, dict) and bool(vb_cfg.get("enabled", True)):
            bv = _volatility_breakout_bonus(df, vb_cfg, atr_period)
            breakdown["volatility_breakout"] = bv
            bonus += bv
        va_cfg = et.get("volume_anomaly")
        if isinstance(va_cfg, dict) and bool(va_cfg.get("enabled", True)):
            av = _volume_anomaly_bonus(df, va_cfg, avg_volume_bars=volume_bars)
            breakdown["volume_anomaly"] = av
            bonus += av
    total = base_total + bonus
    return float(total), breakdown, float(strength_denom)
