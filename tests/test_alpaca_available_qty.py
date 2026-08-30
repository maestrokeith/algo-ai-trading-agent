"""Tests for AlpacaBroker available-qty helpers used by rotation trims."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest


def _build_alpaca_mocks():
    alpaca = types.ModuleType("alpaca")
    trading = types.ModuleType("alpaca.trading")
    trading_client = types.ModuleType("alpaca.trading.client")
    trading_requests = types.ModuleType("alpaca.trading.requests")
    trading_enums = types.ModuleType("alpaca.trading.enums")
    data = types.ModuleType("alpaca.data")
    data_historical = types.ModuleType("alpaca.data.historical")
    data_requests = types.ModuleType("alpaca.data.requests")
    data_timeframe = types.ModuleType("alpaca.data.timeframe")
    data_enums = types.ModuleType("alpaca.data.enums")

    trading_client.TradingClient = MagicMock(name="TradingClient")
    trading_requests.GetOrdersRequest = MagicMock()
    trading_requests.GetPortfolioHistoryRequest = MagicMock()
    trading_requests.LimitOrderRequest = MagicMock()
    trading_requests.MarketOrderRequest = MagicMock()
    trading_enums.OrderSide = MagicMock()
    trading_enums.OrderType = MagicMock()
    trading_enums.TimeInForce = MagicMock()
    data_historical.StockHistoricalDataClient = MagicMock(name="StockHistoricalDataClient")
    data_historical.NewsClient = MagicMock(name="NewsClient")
    data_historical_option = types.ModuleType("alpaca.data.historical.option")
    data_historical_option.OptionHistoricalDataClient = MagicMock(
        name="OptionHistoricalDataClient"
    )
    data_requests.StockBarsRequest = MagicMock()
    data_requests.StockLatestQuoteRequest = MagicMock()
    data_requests.NewsRequest = MagicMock()
    data_requests.OptionChainRequest = MagicMock()
    data_timeframe.TimeFrame = MagicMock()
    data_enums.DataFeed = MagicMock()
    data_enums.DataFeed.IEX = "IEX"
    data_enums.OptionsFeed = MagicMock()
    data_enums.OptionsFeed.INDICATIVE = "INDICATIVE"

    modules = {
        "alpaca": alpaca,
        "alpaca.trading": trading,
        "alpaca.trading.client": trading_client,
        "alpaca.trading.requests": trading_requests,
        "alpaca.trading.enums": trading_enums,
        "alpaca.data": data,
        "alpaca.data.historical": data_historical,
        "alpaca.data.historical.option": data_historical_option,
        "alpaca.data.requests": data_requests,
        "alpaca.data.timeframe": data_timeframe,
        "alpaca.data.enums": data_enums,
    }
    return modules


@pytest.fixture(autouse=True)
def _patch_alpaca_sdk():
    saved = {}
    for name, mod in _build_alpaca_mocks().items():
        saved[name] = sys.modules.get(name)
        sys.modules[name] = mod
    for mod_name in list(sys.modules):
        if mod_name.startswith("src.brokers"):
            del sys.modules[mod_name]
    yield
    for name, orig in saved.items():
        if orig is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = orig
    for mod_name in list(sys.modules):
        if mod_name.startswith("src.brokers"):
            del sys.modules[mod_name]


def test_available_position_qty_uses_qty_available_field() -> None:
    from src.brokers.alpaca_client import AlpacaBroker

    broker = AlpacaBroker(api_key="k", secret="s", paper=True)
    broker.get_position = MagicMock(
        return_value={
            "symbol": "AAPL",
            "qty": 9.027,
            "qty_available": 1.027,
            "qty_held_for_orders": 8.0,
        }
    )
    pos_qty, reserved, available = broker.available_position_qty("AAPL")
    assert pos_qty == pytest.approx(9.027)
    assert available == pytest.approx(1.027)
    assert reserved == pytest.approx(8.0)


def test_available_position_qty_sums_open_sell_orders_when_no_held_field() -> None:
    from src.brokers.alpaca_client import AlpacaBroker

    broker = AlpacaBroker(api_key="k", secret="s", paper=True)
    broker.get_position = MagicMock(return_value={"symbol": "AAPL", "qty": 10.0})
    broker.get_open_orders = MagicMock(
        return_value=[
            {"symbol": "AAPL", "side": "sell", "qty": 8},
            {"symbol": "AAPL", "side": "buy", "qty": 5},
        ]
    )
    pos_qty, reserved, available = broker.available_position_qty("AAPL")
    assert pos_qty == pytest.approx(10.0)
    assert reserved == pytest.approx(8.0)
    assert available == pytest.approx(2.0)


def test_qty_reserved_by_open_orders_sell_side_only() -> None:
    from src.brokers.alpaca_client import AlpacaBroker

    broker = AlpacaBroker(api_key="k", secret="s", paper=True)
    broker.get_open_orders = MagicMock(
        return_value=[
            {"symbol": "MSFT", "side": "sell", "qty": 3},
            {"symbol": "MSFT", "side": "buy", "qty": 10},
        ]
    )
    assert broker.qty_reserved_by_open_orders("MSFT") == pytest.approx(3.0)
