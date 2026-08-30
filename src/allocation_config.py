"""
Top-level ``allocation:`` config — **Top-K allocation**: rank passing trend-longs by
``allocation.rank_top_k_by`` (default ``strength_eff``, optional ``momentum_rs_volume``) when
``rank_by_signal_strength`` is true, then execute only the top ``allocate_top_n`` per scan.
See :func:`effective_ranked_signals_cap` for the unified cap vs ``portfolio.signal_ranking.max_signals_per_loop``.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

__all__ = [
    "parse_allocation_config",
    "effective_allocate_top_n",
    "effective_ranked_signals_cap",
    "low_regime_stock_entry_top_n",
]


def effective_allocate_top_n(
    raw: Any,
    *,
    lo: Any = None,
    hi: Any = None,
) -> int:
    """
    Normalize *raw* to a positive int.

    - ``6`` or ``"6"`` → 6
    - ``"5-8"`` or ``"5 – 8"`` → midpoint ``6``
    - *lo* / *hi* → ``(int(lo) + int(hi)) // 2`` when both set
    """
    if lo is not None and hi is not None and str(lo).strip() and str(hi).strip():
        try:
            a, b = int(float(lo)), int(float(hi))
        except (TypeError, ValueError):
            a, b = 6, 8
        a, b = (a, b) if a <= b else (b, a)
        return max(1, (a + b) // 2)
    if raw is None or str(raw).strip() == "":
        return 6
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        return effective_allocate_top_n(None, lo=raw[0], hi=raw[1])
    s = str(raw).strip()
    m = re.match(r"^\s*(\d+)\s*[-–]\s*(\d+)\s*$", s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        a, b = (a, b) if a <= b else (b, a)
        return max(1, (a + b) // 2)
    try:
        v = int(float(s))
    except (TypeError, ValueError):
        return 6
    return max(1, v)


def parse_allocation_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """
    - *rank_by_signal_strength* — collect candidates and keep the top *allocate_top_n* per scan using
      :func:`src.signal_ranking.canonical_signal_ranking_mode` + *rank_top_k_by* (default
      ``strength_eff``; ``momentum_rs_volume`` uses momentum + relative_strength + volume).
    - *allocate_top_n* — int, ``"5-8"`` range string, or ``[min, max]``; default effective 6.
    - *rank_top_k_by* — only when *rank_by_signal_strength* is true: ``strength_eff`` (default) or
      ``momentum_rs_volume``.
    """
    raw = (config or {}).get("allocation")
    if not isinstance(raw, dict):
        return {
            "rank_by_signal_strength": False,
            "allocate_top_n": 6,
            "rank_top_k_by": "strength_eff",
        }
    rank = bool(raw.get("rank_by_signal_strength", False))
    n = effective_allocate_top_n(
        raw.get("allocate_top_n"),
        lo=raw.get("allocate_top_n_min"),
        hi=raw.get("allocate_top_n_max"),
    )
    rtk = raw.get("rank_top_k_by")
    if rtk is None or str(rtk).strip() == "":
        rtk_s = "strength_eff"
    else:
        rtk_s = str(rtk).strip().lower()
    return {
        "rank_by_signal_strength": rank,
        "allocate_top_n": n,
        "rank_top_k_by": rtk_s,
    }


def effective_ranked_signals_cap(config: Mapping[str, Any] | None) -> int:
    """
    Single **Top-K** cap for the ranked entry queue flush and post-scan allocator candidate trim.

    When ``allocation.rank_by_signal_strength`` is true (recommended for Top-K), returns the effective
    ``allocate_top_n`` so behavior matches: sort by ``allocation.rank_top_k_by`` (default
    ``strength_eff``, or ``momentum_rs_volume``), keep top K only.

    Otherwise returns :func:`src.signal_ranking.max_signals_per_loop_from_portfolio` (YAML
    ``portfolio.signal_ranking.max_signals_per_loop`` / allocator fallbacks).
    """
    ac = parse_allocation_config(config if isinstance(config, dict) else None)
    if ac.get("rank_by_signal_strength"):
        return max(0, int(ac.get("allocate_top_n") or 6))
    from src.signal_ranking import max_signals_per_loop_from_portfolio

    port = (config or {}).get("portfolio") if isinstance(config, dict) else {}
    port = port if isinstance(port, dict) else {}
    return max_signals_per_loop_from_portfolio(port)


def low_regime_stock_entry_top_n(
    config: Mapping[str, Any] | None,
    *,
    regime_score: int | None,
) -> int:
    """Top-K cap for **new stock entries** when regime is weak enough to require selectivity."""
    if regime_score is None:
        return 0
    try:
        rs = int(regime_score)
    except (TypeError, ValueError):
        return 0
    cfg = config if isinstance(config, Mapping) else {}
    ex = cfg.get("execution")
    ex = ex if isinstance(ex, Mapping) else {}
    try:
        regime_max = int(float(ex.get("low_regime_top_n_regime_score_max", 3) or 3))
    except (TypeError, ValueError):
        regime_max = 3
    if rs > regime_max:
        return 0
    if "low_regime_top_n_stock_entries" in ex:
        n = effective_allocate_top_n(ex.get("low_regime_top_n_stock_entries"))
    else:
        n = int(parse_allocation_config(dict(cfg) if isinstance(cfg, dict) else None).get("allocate_top_n") or 5)
    return max(3, min(5, int(n)))
