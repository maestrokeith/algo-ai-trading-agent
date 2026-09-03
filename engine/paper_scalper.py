"""End-to-end paper backtest orchestration for the multi-filter scalper."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .analytics import grouped_statistics, monte_carlo_trade_paths, performance_metrics
from .paper_broker import ClosedTrade, PaperBroker
from .portfolio import PortfolioRiskBook
from .risk_engine import RiskEngine
from .signal_engine import generate_signals
from .trading_config import LIVE_EXECUTION, PAPER_ONLY, StrategyConfig


@dataclass(frozen=True)
class BacktestResult:
    symbol: str
    metrics: dict
    trades: tuple[ClosedTrade, ...]
    instrument_stats: pd.DataFrame
    session_stats: pd.DataFrame
    monte_carlo: pd.DataFrame


class PaperScalperBacktester:
    PAPER_ONLY = PAPER_ONLY
    LIVE_EXECUTION = LIVE_EXECUTION

    def __init__(self, cfg: StrategyConfig | None = None) -> None:
        self.cfg = cfg or StrategyConfig()
        self.risk = RiskEngine(self.cfg)

    def run(self, symbol: str, frame: pd.DataFrame, monte_carlo_simulations: int = 250) -> BacktestResult:
        features = generate_signals(frame, symbol, self.cfg)
        broker = PaperBroker(self.cfg, self.cfg.initial_equity)
        book = PortfolioRiskBook(self.cfg)
        if len(features) < 3:
            raise ValueError("not enough bars for backtest")
        active_id: str | None = None
        for i in range(1, len(features)):
            timestamp = features.index[i]
            current = features.iloc[i]
            for trade in broker.update_symbol(symbol, timestamp, current):
                book.unregister(trade.trade_id)
                if active_id == trade.trade_id:
                    active_id = None
            previous = features.iloc[i - 1]
            if active_id is not None or int(previous.get("signal", 0)) == 0:
                continue
            side = int(previous["signal"])
            atr_value = float(previous["atr"])
            if pd.isna(atr_value) or atr_value <= 0:
                continue
            entry = float(current["open"])
            plan = self.risk.plan(symbol=symbol, side=side, entry=entry, atr_value=atr_value, recent_low=float(previous["recent_low"]), recent_high=float(previous["recent_high"]), equity=broker.equity)
            ok, _ = book.can_open(plan, broker.equity)
            if not ok:
                continue
            spread = float(current.get("spread", self.cfg.instrument(symbol).default_spread))
            position = broker.open_trade(plan, timestamp, spread)
            book.register(position.trade_id, plan, broker.equity)
            active_id = position.trade_id
        if broker.positions:
            last = features.iloc[-1]
            last_ts = features.index[-1]
            for trade in broker.close_all(last_ts, {self.cfg.instrument(symbol).symbol: last}):
                book.unregister(trade.trade_id)
        trades = tuple(broker.closed_trades)
        metrics = performance_metrics(trades, self.cfg.initial_equity)
        return BacktestResult(symbol=self.cfg.instrument(symbol).symbol, metrics=metrics, trades=trades, instrument_stats=grouped_statistics(trades, "symbol"), session_stats=grouped_statistics(trades, "session"), monte_carlo=monte_carlo_trade_paths(trades, self.cfg.initial_equity, simulations=monte_carlo_simulations))

    def can_execute_live(self) -> bool:
        return False
