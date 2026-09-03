"""Configuration for the paper-only FX/metals research engine.

Nothing in this module enables broker connectivity or live order placement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

PAPER_ONLY = True
LIVE_EXECUTION = False


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    tick_size: float
    default_spread: float
    contract_multiplier: float
    quote_currency: str = "USD"
    min_lot: float = 0.01
    lot_step: float = 0.01
    commission_per_lot_round_turn: float = 0.0

    def value_per_price_unit(self, price: float) -> float:
        """Approximate USD P&L from a 1.0 price move for one paper lot."""
        if price <= 0:
            raise ValueError("price must be positive")
        if self.quote_currency == "USD":
            return self.contract_multiplier
        if self.quote_currency in {"JPY", "CAD"}:
            return self.contract_multiplier / price
        return self.contract_multiplier


INSTRUMENTS: Dict[str, InstrumentSpec] = {
    "EURUSD": InstrumentSpec("EURUSD", 0.00001, 0.00010, 100_000.0),
    "GBPUSD": InstrumentSpec("GBPUSD", 0.00001, 0.00012, 100_000.0),
    "USDJPY": InstrumentSpec("USDJPY", 0.001, 0.010, 100_000.0, quote_currency="JPY"),
    "AUDUSD": InstrumentSpec("AUDUSD", 0.00001, 0.00012, 100_000.0),
    "USDCAD": InstrumentSpec("USDCAD", 0.00001, 0.00014, 100_000.0, quote_currency="CAD"),
    "XAUUSD": InstrumentSpec("XAUUSD", 0.01, 0.20, 100.0),
    "XAGUSD": InstrumentSpec("XAGUSD", 0.001, 0.025, 5_000.0),
}


@dataclass(frozen=True)
class StrategyConfig:
    initial_equity: float = 10_000.0
    risk_fraction: float = 0.005
    max_risk_fraction: float = 0.01
    max_total_open_risk_fraction: float = 0.02
    max_positions: int = 3
    max_positions_per_symbol: int = 1
    htf_fast_ema: int = 50
    htf_slow_ema: int = 200
    ltf_fast_ema: int = 9
    ltf_slow_ema: int = 21
    rsi_period: int = 14
    atr_period: int = 14
    volume_ma_period: int = 20
    atr_median_window: int = 100
    swing_lookback: int = 5
    long_rsi_low: float = 45.0
    long_rsi_high: float = 65.0
    short_rsi_low: float = 35.0
    short_rsi_high: float = 55.0
    rsi_midline: float = 50.0
    min_atr_pct: float = 0.00005
    max_atr_pct: float = 0.01
    atr_spike_multiple: float = 3.0
    stop_atr_multiple: float = 1.5
    reward_risk: float = 1.25
    breakeven_trigger_atr: float = 1.0
    trailing_trigger_atr: float = 1.5
    trailing_atr_multiple: float = 1.0
    max_spread_multiple: float = 2.5
    slippage_ticks: float = 0.25
    conservative_same_bar_exit: bool = True
    instruments: Dict[str, InstrumentSpec] = field(default_factory=lambda: dict(INSTRUMENTS))

    def __post_init__(self) -> None:
        if not 0 < self.risk_fraction <= self.max_risk_fraction <= 0.01:
            raise ValueError("risk_fraction must be > 0 and capped at 1%")
        if not 0 < self.max_total_open_risk_fraction <= 0.10:
            raise ValueError("max_total_open_risk_fraction must be sensible")
        if self.max_positions < 1 or self.max_positions_per_symbol < 1:
            raise ValueError("position limits must be positive")
        if self.reward_risk <= 0:
            raise ValueError("reward_risk must be positive")

    def instrument(self, symbol: str) -> InstrumentSpec:
        key = symbol.upper().replace("/", "")
        try:
            return self.instruments[key]
        except KeyError as exc:
            raise KeyError(f"unsupported research instrument: {symbol}") from exc
