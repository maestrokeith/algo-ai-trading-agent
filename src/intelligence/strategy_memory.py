"""Strategy memory facade."""

from __future__ import annotations

from pathlib import Path

from .schemas import StrategyStats
from .trade_memory import TradeMemory


class StrategyMemory:
    def __init__(self, path: str | Path = "data/algo_memory.db") -> None:
        self.trade_memory = TradeMemory(path)

    def stats(self) -> list[StrategyStats]:
        return self.trade_memory.strategy_stats()
