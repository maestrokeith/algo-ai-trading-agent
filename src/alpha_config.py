"""
Top-level ``alpha:`` — ranked candidate selection and pillar weights for composite scoring.

Merges with ``portfolio.signal_ranking`` / :func:`src.signal_ranking.parse_composite_weights_from_portfolio`.
When ``alpha.scoring`` is set, weights map to internal pillars:

* ``weight_trend`` → ``trend_strength``
* ``weight_momentum`` → ``momentum``
* ``weight_volatility`` → ``volatility_expansion``
* ``weight_pullback`` → ``relative_strength`` (uses participation / RS subscore in :func:`trend_long_composite_rank`)
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.signal_ranking import (
    SIGNAL_RANKING_MODE_COMPOSITE,
    parse_composite_weights_from_portfolio,
)


def _alpha_dict(config: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    a = config.get("alpha")
    return a if isinstance(a, dict) else {}


def alpha_rank_candidates(config: Mapping[str, Any] | None) -> bool:
    """
    When ``alpha`` is absent, returns ``True`` (unchanged behavior).

    When ``alpha`` is present, defaults ``rank_candidates`` to ``True``; set ``false`` to disable
    ranked flush / queue path gates that consult this flag (live loop).
    """
    a = _alpha_dict(config)
    if not a:
        return True
    return bool(a.get("rank_candidates", True))


def alpha_cap_ranked_take(config: Mapping[str, Any] | None, requested_max: int) -> int:
    """
    Cap how many ranked rows are dispatched per flush (after sorting).

    ``alpha.select_top_k`` clamps to ``requested_max``. ``alpha.only_best: true`` is shorthand for
    ``select_top_k: 1``.
    """
    rm = max(0, int(requested_max))
    if not isinstance(config, dict):
        return rm
    a = config.get("alpha")
    if not isinstance(a, dict):
        return rm
    if bool(a.get("only_best", False)):
        return max(0, min(1, rm))
    raw = a.get("select_top_k")
    if raw is None or str(raw).strip() == "":
        return rm
    try:
        k = int(raw)
    except (TypeError, ValueError):
        return rm
    return max(0, min(k, rm))


def alpha_selection_method(config: Mapping[str, Any] | None) -> str:
    """``top_n`` | other strings from YAML; default ``top_n``."""
    a = _alpha_dict(config)
    raw = a.get("selection_method")
    if raw is None or str(raw).strip() == "":
        return "top_n"
    return str(raw).strip().lower()


def parse_alpha_scoring_weights(config: Mapping[str, Any] | None) -> dict[str, float] | None:
    """
    Build canonical composite keys from ``alpha.scoring``; return ``None`` if unset.

    Renormalizes positive weights to sum 1.
    """
    a = _alpha_dict(config)
    sc = a.get("scoring")
    if not isinstance(sc, dict):
        return None
    trend = sc.get("weight_trend")
    mom = sc.get("weight_momentum")
    vol = sc.get("weight_volatility")
    pull = sc.get("weight_pullback")
    if all(
        x is None or (isinstance(x, str) and str(x).strip() == "")
        for x in (trend, mom, vol, pull)
    ):
        return None
    out: dict[str, float] = {
        "trend_strength": 0.0,
        "momentum": 0.0,
        "volatility_expansion": 0.0,
        "relative_strength": 0.0,
    }
    key_map = (
        ("weight_trend", "trend_strength"),
        ("weight_momentum", "momentum"),
        ("weight_volatility", "volatility_expansion"),
        ("weight_pullback", "relative_strength"),
    )
    for yaml_k, canon_k in key_map:
        raw = sc.get(yaml_k)
        if raw is not None and str(raw).strip() != "":
            try:
                out[canon_k] = max(0.0, float(raw))
            except (TypeError, ValueError):
                pass
    s = sum(out.values())
    if s <= 1e-12:
        return None
    for k in out:
        out[k] = max(0.0, float(out[k])) / s
    return out


def effective_composite_weights(config: Mapping[str, Any] | None) -> dict[str, float]:
    """
    Prefer ``alpha.scoring`` when present; else ``portfolio.signal_ranking.composite_weights``.
    """
    aw = parse_alpha_scoring_weights(config)
    if aw is not None:
        return aw
    port = (config or {}).get("portfolio") if isinstance(config, dict) else {}
    return parse_composite_weights_from_portfolio(port if isinstance(port, dict) else {})


def alpha_signal_ranking_mode_override(config: Mapping[str, Any] | None) -> str | None:
    """
    When ``alpha`` requests ``selection_method: top_n`` with scoring weights, use composite ranking.

    Returns ``None`` to keep YAML-driven ``portfolio.signal_ranking.ranking_mode``.

    Does **not** override when ``allocation.rank_by_signal_strength`` is true (allocator Top-K
    owns the sort key via ``rank_top_k_by``).
    """
    from src.allocation_config import parse_allocation_config

    ac = parse_allocation_config(config if isinstance(config, dict) else None)
    if bool(ac.get("rank_by_signal_strength")):
        return None
    a = _alpha_dict(config)
    if not a or not bool(a.get("rank_candidates", True)):
        return None
    if parse_alpha_scoring_weights(config) is None:
        return None
    sm = alpha_selection_method(config)
    if sm in ("top_n", "topn", "top-n"):
        return SIGNAL_RANKING_MODE_COMPOSITE
    return None
