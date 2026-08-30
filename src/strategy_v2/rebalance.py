"""Rebalancer targets and trim/add plan (skeleton for 15m cadence)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RebalanceTarget:
    symbol: str
    weight: float  # 0–1 of equity or of risk budget; caller defines


def compute_targets_v2(
    cfg: dict[str, Any] | None,
    *,
    regime_score: int,
    equity: float,
) -> list[RebalanceTarget]:
    """
    Bucket allocator placeholder. Returns empty until universe/bucket weights are wired.

    Future: map ``strategy_v2.portfolio`` + regime to `{QQQ: w, NVDA: w, ...}`.
    """
    _ = (cfg, regime_score, equity)
    return []


def rebalance_plan(
    current_weights: dict[str, float],
    target_weights: dict[str, float],
    *,
    tol: float = 0.005,
) -> list[tuple[str, str, float]]:
    """
    Return list of (symbol, action, delta_weight) where action is ``trim`` | ``add``.

    *delta_weight* is positive magnitude of change in weight space.
    """
    out: list[tuple[str, str, float]] = []
    syms = set(current_weights) | set(target_weights)
    for s in syms:
        c = float(current_weights.get(s, 0.0))
        t = float(target_weights.get(s, 0.0))
        d = t - c
        if abs(d) <= tol:
            continue
        if d < 0:
            out.append((s, "trim", abs(d)))
        else:
            out.append((s, "add", d))
    return out
