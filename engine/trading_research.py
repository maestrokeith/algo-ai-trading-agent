"""Safe facade for quantitative research experiments.

The public boundary is deliberately paper/simulation-only. There are no broker
credentials, live order methods, or autonomous real-money execution hooks here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import pandas as pd

from .paper_scalper import BacktestResult, PaperScalperBacktester
from .trading_config import LIVE_EXECUTION, PAPER_ONLY, StrategyConfig


@dataclass(frozen=True)
class ResearchSignal:
    symbol: str
    timeframe: str
    action: str
    confidence: float
    features: Mapping[str, Any]


class TradingResearchAdapter:
    PAPER_ONLY = PAPER_ONLY
    EXECUTION_ENABLED = LIVE_EXECUTION

    def __init__(self, cfg: StrategyConfig | None = None) -> None:
        self.cfg = cfg or StrategyConfig()
        self.backtester = PaperScalperBacktester(self.cfg)

    def evaluate(self, *, symbol: str, timeframe: str, features: Mapping[str, Any]) -> ResearchSignal:
        return ResearchSignal(symbol=self.cfg.instrument(symbol).symbol, timeframe=timeframe, action="HOLD", confidence=0.0, features=dict(features))

    def backtest(self, symbol: str, frame: pd.DataFrame, monte_carlo_simulations: int = 250) -> BacktestResult:
        return self.backtester.run(symbol, frame, monte_carlo_simulations)

    def can_execute(self) -> bool:
        return False
