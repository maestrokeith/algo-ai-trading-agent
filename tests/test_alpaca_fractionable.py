"""Tests for non-fractionable equity order routing on Alpaca."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from alpaca.common.exceptions import APIError

from src.brokers.alpaca_client import AlpacaBroker


def _broker() -> AlpacaBroker:
    b = AlpacaBroker.__new__(AlpacaBroker)
    b.paper = True
    b._fractionable_cache = {}
    b._tradable_cache = {}
    b._trading = MagicMock()
    b._data = MagicMock()
    b._feed_enum = None
    b.config = {}
    return b


def test_submit_notional_uses_qty_when_asset_not_fractionable() -> None:
    broker = _broker()
    asset = SimpleNamespace(fractionable=False)
    broker._trading.get_asset.return_value = asset
    broker.get_latest_quote = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(mid=10.0, bid=9.9, ask=10.1, spread_pct=1.0)
    )
    broker._stock_latest_trade_price = MagicMock(return_value=None)  # type: ignore[method-assign]
    broker._record_sqlite_trade_event = MagicMock()  # type: ignore[method-assign]

    broker.submit_notional_market_day({"symbol": "TEST", "notional": 250.0, "action": "buy"})

    broker._trading.submit_order.assert_called_once()
    req = broker._trading.submit_order.call_args.kwargs["order_data"]
    assert getattr(req, "qty", None) == 25
    assert getattr(req, "notional", None) is None


def test_submit_notional_logs_submit_attempt_and_success(caplog) -> None:
    broker = _broker()
    broker._trading.get_asset.return_value = SimpleNamespace(fractionable=True, tradable=True)
    broker.get_latest_quote = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(mid=10.0, bid=9.9, ask=10.1, spread_pct=1.0)
    )
    broker._record_sqlite_trade_event = MagicMock()  # type: ignore[method-assign]
    broker._trading.submit_order.return_value = SimpleNamespace(id="ord-1", status="accepted")
    caplog.set_level("INFO")

    result = broker.submit_notional_market_day({"symbol": "TEST", "notional": 250.0, "action": "buy"})

    assert result.id == "ord-1"
    assert broker.get_order_state("ord-1")["status"] == "accepted"
    assert "ORDER_INTENT symbol=TEST side=buy qty=n/a notional=250.00 source=broker" in caplog.text
    assert "ORDER_SUBMIT_ATTEMPT symbol=TEST side=buy qty=n/a notional=250.00 order_type=market" in caplog.text
    assert "ORDER_SUBMITTED symbol=TEST side=buy qty=n/a notional=250.00 order_id=ord-1 status=accepted" in caplog.text
    assert "ORDER_SUBMITTED symbol=TEST side=buy qty=n/a notional=250.00 source=broker order_id=ord-1 status=accepted" in caplog.text


def test_submit_notional_logs_filled_lifecycle(caplog) -> None:
    broker = _broker()
    broker._trading.get_asset.return_value = SimpleNamespace(fractionable=True, tradable=True)
    broker._record_sqlite_trade_event = MagicMock()  # type: ignore[method-assign]
    broker._trading.submit_order.return_value = SimpleNamespace(
        id="ord-fill",
        status="filled",
        filled_qty="2.5",
        filled_avg_price="101.25",
    )
    caplog.set_level("INFO")

    result = broker.submit_notional_market_day({"symbol": "TEST", "notional": 250.0, "action": "buy"})

    assert result.id == "ord-fill"
    assert "ORDER_INTENT symbol=TEST side=buy qty=n/a notional=250.00 source=broker" in caplog.text
    assert "ORDER_SUBMITTED symbol=TEST side=buy qty=n/a notional=250.00 source=broker order_id=ord-fill status=filled" in caplog.text
    assert "ORDER_FILLED symbol=TEST side=buy filled_qty=2.5 filled_avg_price=101.25 order_id=ord-fill" in caplog.text


def test_submit_notional_retries_whole_shares_on_fractionable_api_error(caplog) -> None:
    broker = _broker()
    broker._trading.get_asset.return_value = SimpleNamespace(fractionable=True, tradable=True)
    broker.get_latest_quote = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(mid=4.735, bid=4.72, ask=4.75, spread_pct=0.6)
    )
    broker._stock_latest_trade_price = MagicMock(return_value=None)  # type: ignore[method-assign]
    broker._record_sqlite_trade_event = MagicMock()  # type: ignore[method-assign]
    broker._trading.submit_order.side_effect = [
        APIError("asset is not fractionable"),
        SimpleNamespace(id="abc", status="filled"),
    ]

    caplog.set_level("INFO")
    result = broker.submit_notional_market_day(
        {"symbol": "TEST", "notional": 1200.0, "action": "buy"}
    )

    assert result.id == "abc"
    assert broker._trading.submit_order.call_count == 2
    first_req = broker._trading.submit_order.call_args_list[0].kwargs["order_data"]
    second_req = broker._trading.submit_order.call_args_list[1].kwargs["order_data"]
    assert getattr(first_req, "notional", None) == 1200.0
    assert getattr(second_req, "qty", None) == 253
    assert getattr(second_req, "notional", None) is None
    assert "ORDER_SUBMIT_ATTEMPT symbol=TEST side=buy qty=n/a notional=1200.00 order_type=market" in caplog.text
    assert "ORDER_NON_FRACTIONABLE_QTY symbol=TEST notional=1200.00 price=4.735 qty=253" in caplog.text
    assert "ORDER_SUBMIT_ATTEMPT symbol=TEST side=buy qty=253 notional=n/a order_type=market" in caplog.text
    assert "ORDER_SUBMITTED symbol=TEST side=buy qty=253 notional=n/a order_id=abc status=filled" in caplog.text
    assert "STOCK_RETRY_WHOLE_SHARES symbol=TEST notional=1200.00 price=4.735 qty=253" in caplog.text


def test_submit_notional_does_not_retry_when_asset_not_tradable() -> None:
    broker = _broker()
    broker._trading.get_asset.return_value = SimpleNamespace(fractionable=True, tradable=False)
    broker.get_latest_quote = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(mid=4.735, bid=4.72, ask=4.75, spread_pct=0.6)
    )
    broker._stock_latest_trade_price = MagicMock(return_value=None)  # type: ignore[method-assign]
    broker._record_sqlite_trade_event = MagicMock()  # type: ignore[method-assign]
    broker._trading.submit_order.side_effect = APIError("asset is not fractionable")

    result = broker.submit_notional_market_day(
        {"symbol": "TEST", "notional": 1200.0, "action": "buy"}
    )

    assert result is None
    broker._trading.submit_order.assert_called_once()


def test_is_asset_fractionable_caches_result() -> None:
    broker = _broker()
    broker._trading.get_asset.return_value = SimpleNamespace(fractionable=False)

    assert broker.is_asset_fractionable("ABC") is False
    assert broker.is_asset_fractionable("ABC") is False
    broker._trading.get_asset.assert_called_once()
