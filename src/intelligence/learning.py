"""Learning helpers for strategy ranking and filtering."""

from __future__ import annotations

from .schemas import StrategyStats


def confidence_adjustment(stats: StrategyStats) -> float:
    if stats.trades < 5:
        return 0.0
    if stats.avg_return is not None and stats.avg_return < 0:
        return -0.12
    if stats.win_rate is not None and stats.win_rate >= 0.6:
        return 0.06
    return 0.0
