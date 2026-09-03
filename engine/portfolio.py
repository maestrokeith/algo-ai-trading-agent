"""Portfolio-level deterministic exposure limits for paper research."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from .risk_engine import TradePlan
from .trading_config import StrategyConfig


@dataclass(frozen=True)
class ExposureRecord:
    trade_id: str
    symbol: str
    side: int
    risk_currency: float


class PortfolioRiskBook:
    def __init__(self, cfg: StrategyConfig | None = None) -> None:
        self.cfg = cfg or StrategyConfig()
        self._records: Dict[str, ExposureRecord] = {}

    @property
    def records(self) -> tuple[ExposureRecord, ...]:
        return tuple(self._records.values())

    @property
    def total_open_risk(self) -> float:
        return sum(r.risk_currency for r in self._records.values())

    def symbol_count(self, symbol: str) -> int:
        key = symbol.upper().replace("/", "")
        return sum(1 for r in self._records.values() if r.symbol == key)

    def can_open(self, plan: TradePlan, equity: float) -> tuple[bool, str]:
        if plan.quantity_lots <= 0:
            return False, "quantity_below_minimum"
        if len(self._records) >= self.cfg.max_positions:
            return False, "max_positions"
        if self.symbol_count(plan.symbol) >= self.cfg.max_positions_per_symbol:
            return False, "symbol_limit"
        max_risk = equity * self.cfg.max_total_open_risk_fraction
        if self.total_open_risk + plan.initial_risk_currency > max_risk + 1e-9:
            return False, "portfolio_risk_limit"
        return True, "approved"

    def register(self, trade_id: str, plan: TradePlan, equity: float) -> None:
        ok, reason = self.can_open(plan, equity)
        if not ok:
            raise ValueError(f"portfolio rejected trade: {reason}")
        self._records[trade_id] = ExposureRecord(trade_id=trade_id, symbol=plan.symbol, side=plan.side, risk_currency=plan.initial_risk_currency)

    def unregister(self, trade_id: str) -> None:
        self._records.pop(trade_id, None)
