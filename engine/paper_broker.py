"""Deterministic paper broker with spread, slippage and stop management.

This broker is intentionally disconnected from every real brokerage API.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count
from typing import Dict

import pandas as pd

from .market_data import session_name
from .risk_engine import TradePlan
from .trading_config import PAPER_ONLY, LIVE_EXECUTION, StrategyConfig


@dataclass
class PaperPosition:
    trade_id: str
    symbol: str
    side: int
    quantity_lots: float
    entry: float
    stop: float
    initial_stop: float
    target: float
    atr: float
    opened_at: pd.Timestamp
    initial_risk_currency: float
    breakeven_moved: bool = False
    trailing_active: bool = False


@dataclass(frozen=True)
class ClosedTrade:
    trade_id: str
    symbol: str
    side: int
    quantity_lots: float
    opened_at: pd.Timestamp
    closed_at: pd.Timestamp
    entry: float
    exit: float
    initial_stop: float
    target: float
    pnl: float
    r_multiple: float
    exit_reason: str
    session: str


class PaperBroker:
    PAPER_ONLY = PAPER_ONLY
    LIVE_EXECUTION = LIVE_EXECUTION

    def __init__(self, cfg: StrategyConfig | None = None, equity: float | None = None) -> None:
        self.cfg = cfg or StrategyConfig()
        self.starting_equity = float(equity if equity is not None else self.cfg.initial_equity)
        self.cash = self.starting_equity
        self.positions: Dict[str, PaperPosition] = {}
        self.closed_trades: list[ClosedTrade] = []
        self._ids = count(1)

    @property
    def equity(self) -> float:
        return self.cash

    def can_execute_live(self) -> bool:
        return False

    def _slippage(self, symbol: str) -> float:
        spec = self.cfg.instrument(symbol)
        return spec.tick_size * self.cfg.slippage_ticks

    def _entry_fill(self, plan: TradePlan, spread: float) -> tuple[float, float, float]:
        slip = self._slippage(plan.symbol)
        adjustment = (spread / 2.0 + slip) * plan.side
        fill = plan.entry + adjustment
        delta = fill - plan.entry
        return fill, plan.stop + delta, plan.target + delta

    def open_trade(self, plan: TradePlan, opened_at: pd.Timestamp, spread: float) -> PaperPosition:
        if plan.quantity_lots <= 0:
            raise ValueError("paper order quantity must be positive")
        trade_id = f"PAPER-{next(self._ids):06d}"
        fill, stop, target = self._entry_fill(plan, spread)
        position = PaperPosition(trade_id=trade_id, symbol=plan.symbol, side=plan.side, quantity_lots=plan.quantity_lots, entry=fill, stop=stop, initial_stop=stop, target=target, atr=plan.atr, opened_at=pd.Timestamp(opened_at), initial_risk_currency=plan.initial_risk_currency)
        self.positions[trade_id] = position
        return position

    def _exit_fill(self, position: PaperPosition, intended_price: float, spread: float) -> float:
        slip = self._slippage(position.symbol)
        return intended_price - position.side * (spread / 2.0 + slip)

    def _close(self, position: PaperPosition, timestamp: pd.Timestamp, intended_price: float, spread: float, reason: str) -> ClosedTrade:
        spec = self.cfg.instrument(position.symbol)
        exit_fill = self._exit_fill(position, intended_price, spread)
        gross = position.side * (exit_fill - position.entry) * position.quantity_lots * spec.value_per_price_unit(position.entry)
        commission = position.quantity_lots * spec.commission_per_lot_round_turn
        pnl = gross - commission
        self.cash += pnl
        initial_risk = max(position.initial_risk_currency, 1e-12)
        trade = ClosedTrade(trade_id=position.trade_id, symbol=position.symbol, side=position.side, quantity_lots=position.quantity_lots, opened_at=position.opened_at, closed_at=pd.Timestamp(timestamp), entry=position.entry, exit=exit_fill, initial_stop=position.initial_stop, target=position.target, pnl=pnl, r_multiple=pnl / initial_risk, exit_reason=reason, session=session_name(position.opened_at))
        self.closed_trades.append(trade)
        self.positions.pop(position.trade_id, None)
        return trade

    def update_symbol(self, symbol: str, timestamp: pd.Timestamp, bar: pd.Series) -> list[ClosedTrade]:
        key = symbol.upper().replace("/", "")
        spread = float(bar.get("spread", self.cfg.instrument(key).default_spread))
        closed: list[ClosedTrade] = []
        for position in list(self.positions.values()):
            if position.symbol != key:
                continue
            high, low = float(bar["high"]), float(bar["low"])
            favorable = (high - position.entry) if position.side == 1 else (position.entry - low)
            if favorable >= self.cfg.breakeven_trigger_atr * position.atr and not position.breakeven_moved:
                position.stop = max(position.stop, position.entry) if position.side == 1 else min(position.stop, position.entry)
                position.breakeven_moved = True
            if favorable >= self.cfg.trailing_trigger_atr * position.atr:
                position.trailing_active = True
                if position.side == 1:
                    position.stop = max(position.stop, high - self.cfg.trailing_atr_multiple * position.atr)
                else:
                    position.stop = min(position.stop, low + self.cfg.trailing_atr_multiple * position.atr)
            if position.side == 1:
                stop_hit, target_hit = low <= position.stop, high >= position.target
            else:
                stop_hit, target_hit = high >= position.stop, low <= position.target
            if stop_hit and target_hit:
                if self.cfg.conservative_same_bar_exit:
                    closed.append(self._close(position, timestamp, position.stop, spread, "stop_same_bar"))
                else:
                    closed.append(self._close(position, timestamp, position.target, spread, "target_same_bar"))
            elif stop_hit:
                closed.append(self._close(position, timestamp, position.stop, spread, "stop"))
            elif target_hit:
                closed.append(self._close(position, timestamp, position.target, spread, "target"))
        return closed

    def close_all(self, timestamp: pd.Timestamp, bars_by_symbol: Dict[str, pd.Series]) -> list[ClosedTrade]:
        closed: list[ClosedTrade] = []
        for position in list(self.positions.values()):
            bar = bars_by_symbol[position.symbol]
            spread = float(bar.get("spread", self.cfg.instrument(position.symbol).default_spread))
            closed.append(self._close(position, timestamp, float(bar["close"]), spread, "end_of_test"))
        return closed
