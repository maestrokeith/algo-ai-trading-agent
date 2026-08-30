"""Map market regime score to v2 long / hedge multipliers (YAML-driven)."""
from __future__ import annotations

from typing import Any


def _v2(cfg: dict[str, Any] | None) -> dict[str, Any]:
    if not cfg:
        return {}
    return cfg.get("strategy_v2") or {}


def regime_long_mult_for_score(regime_score: int, cfg: dict[str, Any] | None) -> float:
    """``regime.bullish|neutral|bearish`` using ``score_min`` ordering (high to low)."""
    r = _v2(cfg).get("regime") or {}
    bull = r.get("bullish") or {}
    neu = r.get("neutral") or {}
    bear = r.get("bearish") or {}
    s = int(regime_score)
    if s >= int(bull.get("score_min", 4)):
        return float(bull.get("long_mult", 1.0))
    if s >= int(neu.get("score_min", 2)):
        return float(neu.get("long_mult", 0.5))
    return float(bear.get("long_mult", 0.2))


def regime_hedge_mult_for_score(regime_score: int, cfg: dict[str, Any] | None) -> float:
    """Hedge notion as fraction of book / equity — same tiering as long_mult."""
    r = _v2(cfg).get("regime") or {}
    bull = r.get("bullish") or {}
    neu = r.get("neutral") or {}
    bear = r.get("bearish") or {}
    s = int(regime_score)
    if s >= int(bull.get("score_min", 4)):
        return float(bull.get("hedge_mult", 0.0))
    if s >= int(neu.get("score_min", 2)):
        return float(neu.get("hedge_mult", 0.15))
    return float(bear.get("hedge_mult", 0.5))
