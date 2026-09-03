"""Deterministic paper-trading risk engine."""

from __future__ import annotations

from dataclasses import dataclass
import math

from .trading_config import StrategyConfig


@dataclass(frozen=True)
class TradePlan:
    symbol: str
    side: int
    entry: float
    stop: float
    target: float
    quantity_lots: float
    initial_risk_currency: float
    atr: float

    @property
    def stop_distance(self) -> float:
        return abs(self.entry - self.stop)


class RiskEngine:
    def __init__(self, cfg: StrategyConfig | None = None) -> None:
        self.cfg = cfg or StrategyConfig()

    def plan(self, *, symbol: str, side: int, entry: float, atr_value: float, recent_low: float, recent_high: float, equity: float) -> TradePlan:
        if side not in (-1, 1):
            raise ValueError("side must be -1 or 1")
        if min(entry, atr_value, equity) <= 0:
            raise ValueError("entry, ATR and equity must be positive")
        cfg = self.cfg
        spec = cfg.instrument(symbol)
        atr_stop = atr_value * cfg.stop_atr_multiple
        if side == 1:
            stop = min(float(recent_low), entry - atr_stop)
            target = entry + (entry - stop) * cfg.reward_risk
        else:
            stop = max(float(recent_high), entry + atr_stop)
            target = entry - (stop - entry) * cfg.reward_risk
        stop_distance = abs(entry - stop)
        if stop_distance <= 0:
            raise ValueError("computed stop distance is invalid")
        risk_budget = equity * cfg.risk_fraction
        value_per_price = spec.value_per_price_unit(entry)
        raw_lots = risk_budget / (stop_distance * value_per_price)
        steps = math.floor(raw_lots / spec.lot_step + 1e-12)
        qty = max(0.0, steps * spec.lot_step)
        if qty < spec.min_lot:
            qty = 0.0
        risk_currency = qty * stop_distance * value_per_price
        if risk_currency > risk_budget * 1.001:
            raise AssertionError("position sizing exceeded risk budget")
        return TradePlan(symbol=spec.symbol, side=side, entry=entry, stop=stop, target=target, quantity_lots=round(qty, 8), initial_risk_currency=risk_currency, atr=atr_value)
