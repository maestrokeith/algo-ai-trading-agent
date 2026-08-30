"""
Explicit ranked signal pipeline: **valid** → **rank by mode** → **take top K**.

Mirrors ``candidates = get_valid_signals()`` → full sort (same order as
:func:`src.signal_ranking.rank_trend_long_candidate_rows`) → ``selected = ranked[:k]``.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from src.signal_ranking import (
    SIGNAL_RANKING_MODE_COMPOSITE,
    SIGNAL_RANKING_MODE_MVE,
    SIGNAL_RANKING_MODE_MRV,
    SIGNAL_RANKING_MODE_SIGNAL_PRIORITY,
    SIGNAL_RANKING_MODE_STRENGTH,
    SIGNAL_RANKING_MODE_TIER,
    rank_trend_long_candidate_rows,
    row_composite_score,
    row_momentum_volume_ema_score,
    row_momentum_rs_volume_score,
    row_signal_priority_score,
)

__all__ = [
    "get_valid_signals",
    "rank_all_by_mode",
    "row_numeric_score",
    "select_top_signals",
]


def get_valid_signals(
    rows: Sequence[dict[str, Any]],
    *,
    is_valid: Callable[[dict[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    """Return rows that pass an optional predicate (default: drop ``None`` only)."""
    if is_valid is None:
        return [r for r in rows if r is not None]
    return [r for r in rows if is_valid(r)]


def rank_all_by_mode(
    rows: list[dict[str, Any]],
    *,
    sector_etfs: frozenset[str],
    ranking_mode: str,
    sym_key: str = "sym_u",
    strength_key: str = "strength_eff",
) -> list[dict[str, Any]]:
    """
    Full ordering for *rows* using the same rules as Top-K ranking with ``max_take`` = len(rows).
    """
    n = len(rows)
    if n == 0:
        return []
    chosen, _ = rank_trend_long_candidate_rows(
        rows,
        max_take=n,
        sector_etfs=sector_etfs,
        sym_key=sym_key,
        strength_key=strength_key,
        ranking_mode=ranking_mode,
    )
    return chosen


def select_top_signals(ranked: Sequence[dict[str, Any]], k: int) -> list[dict[str, Any]]:
    """``selected = ranked[:k]`` (non-negative *k*)."""
    kk = max(0, int(k))
    if kk <= 0:
        return []
    return list(ranked[:kk])


def row_numeric_score(
    row: dict[str, Any],
    *,
    ranking_mode: str,
    sym_key: str = "sym_u",
    strength_key: str = "strength_eff",
) -> float:
    """
    Single scalar aligned with the active ranking mode (for logs / debugging).

    Tier mode reports ``strength_eff`` only (tier is discrete); ordering still follows
    :func:`rank_trend_long_candidate_rows`.
    """
    mode = (ranking_mode or SIGNAL_RANKING_MODE_TIER).strip().lower()
    if mode == SIGNAL_RANKING_MODE_STRENGTH or mode in ("strength", "strength_eff"):
        try:
            return float(row.get(strength_key) or 0.0)
        except (TypeError, ValueError):
            return 0.0
    if mode == SIGNAL_RANKING_MODE_SIGNAL_PRIORITY:
        return float(row_signal_priority_score(row))
    if mode == SIGNAL_RANKING_MODE_MRV or mode in ("momentum_rs_volume", "mrv"):
        return float(row_momentum_rs_volume_score(row))
    if mode == SIGNAL_RANKING_MODE_MVE or mode in ("momentum_volume_ema", "mve"):
        return float(row_momentum_volume_ema_score(row))
    if mode == SIGNAL_RANKING_MODE_COMPOSITE or mode in ("composite", "weighted_composite"):
        return float(row_composite_score(row))
    # tier_then_strength and unknown: surface strength for a comparable scalar
    try:
        return float(row.get(strength_key) or 0.0)
    except (TypeError, ValueError):
        return 0.0
