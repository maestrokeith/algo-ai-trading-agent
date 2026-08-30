"""
Lean-style context and data structures: AlgorithmContext, Portfolio, Slice, Bar.

Context is passed to Initialize() and OnEndOfDay(); algorithm uses it to read
portfolio state and submit orders.
"""
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass
class Bar:
    """Single symbol OHLCV bar at a time."""
    symbol: str
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Slice:
    """Data slice at a point in time (e.g. one day). Maps symbol -> Bar."""
    time: datetime
    bars: dict[str, Bar]

    def get(self, symbol: str) -> Bar | None:
        return self.bars.get(symbol)

    def __contains__(self, symbol: str) -> bool:
        return symbol in self.bars

    def symbols(self) -> list[str]:
        return list(self.bars.keys())


@dataclass
class Position:
    """Single position (symbol, quantity, entry price, etc.)."""
    symbol: str
    quantity: int
    entry_price: float
    entry_time: datetime
    stop_pct: float
    partial_taken: bool = False
    trail_high: float | None = None
    smart_scale_out_index: int = 0

    @property
    def market_value(self) -> float:
        """Requires current price to be set externally for live use."""
        return 0.0  # Backtest engine computes from bar close


class Portfolio:
    """Portfolio state: cash, positions, total value. Updated by engine."""
    def __init__(self, initial_cash: float):
        self._cash = initial_cash
        self._positions: dict[str, Position] = {}
        self._total_value = initial_cash  # Engine sets this after mark-to-market

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def total_portfolio_value(self) -> float:
        return self._total_value

    def set_total_value(self, value: float) -> None:
        self._total_value = value

    def set_cash(self, value: float) -> None:
        self._cash = value

    def positions(self) -> dict[str, Position]:
        return dict(self._positions)

    def get_position(self, symbol: str) -> Position | None:
        return self._positions.get(symbol)

    def add_position(self, pos: Position) -> None:
        self._positions[pos.symbol] = pos

    def remove_position(self, symbol: str) -> None:
        self._positions.pop(symbol, None)

    def set_positions(self, positions: dict[str, "Position"]) -> None:
        """Replace all positions (used by engine to sync state)."""
        self._positions = dict(positions)

    def invested(self) -> bool:
        return len(self._positions) > 0


class AlgorithmContext:
    """
    Context passed to algorithm.Initialize() and OnEndOfDay().
    Provides Time, Portfolio, Universe, Config, and order submission (handled by engine).
    """
    def __init__(self, config: dict[str, Any], initial_cash: float = 100_000.0):
        self.config = config
        self._time: datetime | None = None
        self.portfolio = Portfolio(initial_cash)
        # Universe: symbols to trade (from config, Lean-style)
        _sym = config.get("universe", {}).get("symbols", [])
        self._universe = [_sym] if isinstance(_sym, str) else list(_sym or [])
        # Orders the algorithm requests this bar (engine consumes and simulates)
        self._order_requests: list[dict[str, Any]] = []

    @property
    def universe(self) -> list[str]:
        """Symbols in the trading universe (Lean-style)."""
        return self._universe

    @property
    def Universe(self) -> list[str]:
        """Lean-style alias for universe."""
        return self._universe

    @property
    def time(self) -> datetime:
        if self._time is None:
            return datetime.min
        return self._time

    def set_time(self, dt: datetime) -> None:
        self._time = dt

    @property
    def order_requests(self) -> list[dict[str, Any]]:
        return self._order_requests

    def clear_orders(self) -> None:
        self._order_requests.clear()

    def market_order(self, symbol: str, quantity: int, stop_pct: float | None = None) -> None:
        """Request a market order (long: quantity > 0, short: quantity < 0). Optional stop_pct for new longs."""
        self._order_requests.append({
            "type": "market",
            "symbol": symbol,
            "quantity": quantity,
            "stop_pct": stop_pct,
        })

    def set_holdings(self, symbol: str, percentage: float) -> None:
        """Request target allocation as fraction of portfolio (e.g. 0.25 = 25%). Engine converts to quantity."""
        self._order_requests.append({"type": "set_holdings", "symbol": symbol, "percentage": percentage})
