"""Broker-neutral contracts and canonical models.

AlgoSphere strategy, sizing, lifecycle, and diagnostics should depend on these
canonical shapes when broker-specific responses need to cross module
boundaries. Existing Alpaca call sites can keep using the legacy adapter API
while new broker integrations normalize into these models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


class BrokerOrderStatus(str, Enum):
    NEW = "NEW"
    ACCEPTED = "ACCEPTED"
    PENDING = "PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    REPLACED = "REPLACED"
    UNKNOWN = "UNKNOWN"


TERMINAL_ORDER_STATUSES = {
    BrokerOrderStatus.FILLED,
    BrokerOrderStatus.CANCELED,
    BrokerOrderStatus.EXPIRED,
    BrokerOrderStatus.REJECTED,
    BrokerOrderStatus.REPLACED,
}


@dataclass(frozen=True)
class BrokerCapabilities:
    supports_fractional_equities: bool = False
    supports_options: bool = False
    supports_crypto: bool = False
    supports_shorting: bool = False
    supports_extended_hours: bool = False
    supports_market_data: bool = False


@dataclass
class BrokerAccount:
    broker: str
    account_id: str | None = None
    account_type: str | None = None
    status: str | None = None
    buying_power: float | None = None
    equity: float | None = None
    cash: float | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class BrokerPosition:
    broker: str
    symbol: str
    qty: float
    side: str = "long"
    market_value: float | None = None
    cost_basis: float | None = None
    avg_entry_price: float | None = None
    current_price: float | None = None
    unrealized_pl: float | None = None
    asset_class: str = "equity"
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class BrokerQuote:
    broker: str
    symbol: str
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    last: float | None = None
    previous_close: float | None = None
    timestamp: datetime | None = None
    source: str | None = None
    quote_age: float | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def mid(self) -> float:
        if self.bid and self.ask and self.bid > 0 and self.ask > 0:
            return (float(self.bid) + float(self.ask)) / 2.0
        if self.last and self.last > 0:
            return float(self.last)
        return 0.0

    @property
    def spread_pct(self) -> float:
        mid = self.mid
        if not self.bid or not self.ask or mid <= 0:
            return 0.0
        return abs(float(self.ask) - float(self.bid)) / mid * 100.0


@dataclass
class BrokerFill:
    broker: str
    broker_order_id: str
    symbol: str
    qty: float
    price: float
    filled_at: datetime | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class BrokerOrder:
    broker: str
    broker_order_id: str | None
    client_order_id: str | None
    symbol: str
    asset_class: str = "equity"
    side: str | None = None
    order_type: str | None = None
    qty: float | None = None
    notional: float | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    status: BrokerOrderStatus = BrokerOrderStatus.UNKNOWN
    submitted_at: datetime | None = None
    updated_at: datetime | None = None
    filled_at: datetime | None = None
    filled_qty: float | None = None
    average_fill_price: float | None = None
    raw_status: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)
    broker_review_status: str | None = None
    broker_review_warnings: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    broker_review_timestamp: datetime | None = None

    @property
    def id(self) -> str | None:
        return self.broker_order_id


@dataclass
class BrokerOptionContract:
    broker: str
    symbol: str
    underlying: str
    expiration_date: str | None = None
    strike_price: float | None = None
    option_type: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class BrokerOptionQuote(BrokerQuote):
    expiration_date: str | None = None
    strike_price: float | None = None
    option_type: str | None = None


@runtime_checkable
class BrokerClient(Protocol):
    @property
    def capabilities(self) -> BrokerCapabilities:
        ...

    def get_account(self) -> BrokerAccount:
        ...

    def get_buying_power(self) -> float:
        ...

    def get_equity(self) -> float:
        ...

    def list_positions(self) -> list[BrokerPosition]:
        ...

    def get_position(self, symbol: str) -> BrokerPosition | None:
        ...

    def get_quote(self, symbol: str) -> BrokerQuote | None:
        ...

    def get_quotes(self, symbols: Sequence[str]) -> dict[str, BrokerQuote]:
        ...

    def get_latest_trade(self, symbol: str) -> Mapping[str, Any] | None:
        ...

    def submit_order(self, order: Any) -> BrokerOrder:
        ...

    def get_order(self, order_id: str) -> BrokerOrder | None:
        ...

    def list_orders(self, status: str = "open", **kwargs: Any) -> list[BrokerOrder]:
        ...

    def cancel_order(self, order_id: str) -> Any:
        ...


class BrokerUnavailable(RuntimeError):
    """Raised when a configured broker cannot be used safely."""


class BrokerCapabilityError(RuntimeError):
    """Raised when a caller asks a broker to do an unsupported operation."""


class BrokerExecutionDisabled(RuntimeError):
    """Raised when execution is disabled by broker feature flags."""
