"""Tests for AlpacaBroker — credential resolution and __init__ refactor.

Since alpaca-py may not be installed in the test environment, we mock the
SDK classes so we can verify the credential/paper resolution logic without
making real API calls.
"""

import sys
import types
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Mock the alpaca SDK before importing our module
# ---------------------------------------------------------------------------

def _build_alpaca_mocks():
    """Create a minimal mock tree for the alpaca SDK."""
    alpaca = types.ModuleType("alpaca")
    alpaca_common = types.ModuleType("alpaca.common")
    alpaca_common_exceptions = types.ModuleType("alpaca.common.exceptions")
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
    trading_enums.OrderType = SimpleNamespace(MARKET="market")
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
    alpaca_common_exceptions.APIError = type("APIError", (Exception,), {})

    modules = {
        "alpaca": alpaca,
        "alpaca.common": alpaca_common,
        "alpaca.common.exceptions": alpaca_common_exceptions,
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
    return (
        modules,
        trading_client.TradingClient,
        data_historical.StockHistoricalDataClient,
        data_historical_option.OptionHistoricalDataClient,
    )


_mocks, MockTradingClient, MockDataClient, MockOptionHistoricalClient = _build_alpaca_mocks()


@pytest.fixture(autouse=True)
def _patch_alpaca_sdk():
    """Inject mock alpaca SDK modules for the duration of every test."""
    saved = {}
    for name, mod in _mocks.items():
        saved[name] = sys.modules.get(name)
        sys.modules[name] = mod

    # Force re-import so the module picks up our mocks
    for mod_name in list(sys.modules):
        if mod_name.startswith("src.brokers"):
            del sys.modules[mod_name]

    yield

    # Restore original modules
    for name, orig in saved.items():
        if orig is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = orig
    for mod_name in list(sys.modules):
        if mod_name.startswith("src.brokers"):
            del sys.modules[mod_name]


def _import_broker():
    """Import AlpacaBroker after mocks are in place."""
    from src.brokers.alpaca_client import AlpacaBroker
    return AlpacaBroker


def test_order_submitted_telemetry_uses_final_bounded_notional(caplog):
    from src.brokers import alpaca_client as ac

    order = SimpleNamespace(id="299a7450-e2d8-4cdf-9224-70be9e04c30b", status="new", qty="0.330702357")
    with caplog.at_level(logging.INFO):
        ac._log_order_submitted(
            "IWM",
            "buy",
            order,
            qty=None,
            notional=99.99,
            metadata={
                "allocator_requested_notional": 1312.50,
                "allocator_requested_qty": 4,
                "bounded_pilot_applied": True,
                "final_submitted_qty": 0.330702357,
                "final_reference_price": 302.35,
                "final_estimated_notional": 99.99,
                "broker_request_type": "notional",
            },
        )

    line = caplog.text
    assert "ORDER_SUBMITTED symbol=IWM" in line
    assert "notional=99.99" in line
    assert "allocator_requested_notional=1312.5" in line
    assert "final_submitted_qty=0.330702357" in line
    assert "bounded_pilot_applied=true" in line


# ---------------------------------------------------------------------------
# Tests: explicit credentials (multi-user mode)
# ---------------------------------------------------------------------------

class TestExplicitCredentials:

    def test_explicit_key_secret_paper(self):
        AlpacaBroker = _import_broker()
        broker = AlpacaBroker(api_key="my_key", secret="my_secret", paper=True)
        assert broker.paper is True
        MockTradingClient.assert_called_with("my_key", "my_secret", paper=True)

    def test_explicit_key_secret_live(self):
        AlpacaBroker = _import_broker()
        broker = AlpacaBroker(api_key="live_key", secret="live_secret", paper=False)
        assert broker.paper is False
        MockTradingClient.assert_called_with("live_key", "live_secret", paper=False)

    def test_explicit_credentials_ignore_env(self, monkeypatch):
        monkeypatch.setenv("APCA_API_KEY_ID", "env_key")
        monkeypatch.setenv("APCA_API_SECRET_KEY", "env_secret")
        AlpacaBroker = _import_broker()
        broker = AlpacaBroker(api_key="explicit_key", secret="explicit_secret", paper=True)
        MockTradingClient.assert_called_with("explicit_key", "explicit_secret", paper=True)

    def test_explicit_paper_overrides_config(self):
        AlpacaBroker = _import_broker()
        config = {"broker": {"paper": True}}
        broker = AlpacaBroker(config=config, api_key="k", secret="s", paper=False)
        assert broker.paper is False

    def test_explicit_with_config_overrides(self):
        AlpacaBroker = _import_broker()
        config = {"broker": {"data_feed": "sip", "api_retry_times": 5}}
        broker = AlpacaBroker(config=config, api_key="k", secret="s", paper=True)
        assert broker._retry_times == 5


# ---------------------------------------------------------------------------
# Tests: legacy env var resolution (backward compat)
# ---------------------------------------------------------------------------

class TestLegacyEnvCredentials:

    def test_paper_from_env(self, monkeypatch):
        monkeypatch.setenv("APCA_API_KEY_ID", "paper_key")
        monkeypatch.setenv("APCA_API_SECRET_KEY", "paper_secret")
        AlpacaBroker = _import_broker()
        broker = AlpacaBroker()
        assert broker.paper is True
        MockTradingClient.assert_called_with("paper_key", "paper_secret", paper=True)

    def test_missing_credentials_raises(self, monkeypatch):
        monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
        monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
        monkeypatch.delenv("ALPACA_LIVE_API_KEY_ID", raising=False)
        monkeypatch.delenv("ALPACA_LIVE_API_SECRET_KEY", raising=False)
        AlpacaBroker = _import_broker()
        with pytest.raises(ValueError, match="Alpaca paper credentials required"):
            AlpacaBroker()

    def test_paper_env_override(self, monkeypatch):
        monkeypatch.setenv("APCA_PAPER", "false")
        monkeypatch.setenv("ALPACA_LIVE_API_KEY_ID", "live_k")
        monkeypatch.setenv("ALPACA_LIVE_API_SECRET_KEY", "live_s")
        AlpacaBroker = _import_broker()
        broker = AlpacaBroker()
        assert broker.paper is False

    def test_config_paper_default(self, monkeypatch):
        monkeypatch.delenv("APCA_PAPER", raising=False)
        monkeypatch.delenv("ALPACA_LIVE", raising=False)
        monkeypatch.setenv("ALPACA_LIVE_API_KEY_ID", "lk")
        monkeypatch.setenv("ALPACA_LIVE_API_SECRET_KEY", "ls")
        AlpacaBroker = _import_broker()
        broker = AlpacaBroker(config={"broker": {"paper": False}})
        assert broker.paper is False

    def test_live_env_resolved_creds_passed_to_option_client(self, monkeypatch):
        """OptionHistoricalDataClient must use resolved_key/secret, not ctor args (often None)."""
        monkeypatch.delenv("APCA_PAPER", raising=False)
        monkeypatch.delenv("ALPACA_LIVE", raising=False)
        monkeypatch.setenv("ALPACA_LIVE_API_KEY_ID", "live_k")
        monkeypatch.setenv("ALPACA_LIVE_API_SECRET_KEY", "live_s")
        MockOptionHistoricalClient.reset_mock()
        AlpacaBroker = _import_broker()
        AlpacaBroker(config={"broker": {"paper": False}})
        MockOptionHistoricalClient.assert_called_once_with("live_k", "live_s")

    def test_live_does_not_fall_back_to_paper_env(self, monkeypatch):
        """Live API must not use APCA_* when ALPACA_LIVE_* are unset (avoids 401)."""
        monkeypatch.delenv("ALPACA_LIVE_API_KEY_ID", raising=False)
        monkeypatch.delenv("ALPACA_LIVE_API_SECRET_KEY", raising=False)
        monkeypatch.setenv("APCA_API_KEY_ID", "paper_k")
        monkeypatch.setenv("APCA_API_SECRET_KEY", "paper_s")
        AlpacaBroker = _import_broker()
        with pytest.raises(ValueError, match="Alpaca LIVE credentials required"):
            AlpacaBroker(config={"broker": {"paper": False}})


# ---------------------------------------------------------------------------
# Tests: mixed — explicit partial args should still fail
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_explicit_key_without_secret_falls_back_to_env(self, monkeypatch):
        """api_key alone is not enough — both must be provided for explicit mode."""
        monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
        monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
        AlpacaBroker = _import_broker()
        # api_key given but secret is None → falls to env path → env not set → raises
        with pytest.raises(ValueError, match="Alpaca paper credentials required"):
            AlpacaBroker(api_key="only_key")

    def test_no_config_defaults(self):
        AlpacaBroker = _import_broker()
        broker = AlpacaBroker(api_key="k", secret="s", paper=True)
        assert broker.config == {}
        assert broker._retry_times == 3
        assert broker._retry_delay_sec == 3.0


# ---------------------------------------------------------------------------
# Tests: fill price helpers
# ---------------------------------------------------------------------------

class TestFilledAvgPriceFromOrder:

    def test_returns_float_when_filled_avg_price_set(self):
        from src.brokers.alpaca_client import filled_avg_price_from_order

        o = MagicMock()
        o.filled_avg_price = "101.5"
        o.filled_average_price = None
        assert filled_avg_price_from_order(o) == 101.5

    def test_legacy_filled_average_price(self):
        from src.brokers.alpaca_client import filled_avg_price_from_order

        o = MagicMock()
        del o.filled_avg_price
        o.filled_average_price = 99.0
        assert filled_avg_price_from_order(o) == 99.0

    def test_none_and_zero_fallback(self):
        from src.brokers.alpaca_client import filled_avg_price_from_order

        assert filled_avg_price_from_order(None) is None
        o = MagicMock()
        o.filled_avg_price = 0
        o.filled_average_price = None
        assert filled_avg_price_from_order(o) is None


class TestResolveEntryPriceFromFill:

    def test_uses_submit_response_fill(self):
        AlpacaBroker = _import_broker()
        broker = AlpacaBroker(api_key="k", secret="s", paper=True, config={"trading_control": {"mode": "paper"}})
        o = MagicMock()
        o.id = "oid1"
        o.filled_avg_price = 55.25
        assert broker.resolve_entry_price_from_fill(o, 50.0) == 55.25

    def test_refetches_when_submit_has_no_fill(self):
        AlpacaBroker = _import_broker()
        broker = AlpacaBroker(api_key="k", secret="s", paper=True, config={"trading_control": {"mode": "paper"}})
        o = MagicMock()
        o.id = "oid2"
        o.filled_avg_price = None
        o.filled_average_price = None

        filled = MagicMock()
        filled.filled_avg_price = 60.0
        broker._trading.get_order_by_id = MagicMock(return_value=filled)

        assert broker.resolve_entry_price_from_fill(o, 50.0) == 60.0
        broker._trading.get_order_by_id.assert_called_once_with("oid2")

    def test_fallback_when_refetch_empty(self):
        AlpacaBroker = _import_broker()
        broker = AlpacaBroker(api_key="k", secret="s", paper=True, config={"trading_control": {"mode": "paper"}})
        o = SimpleNamespace(id="oid3", filled_avg_price=None, filled_average_price=None)
        o2 = SimpleNamespace()
        broker._trading.get_order_by_id = MagicMock(return_value=o2)
        assert broker.resolve_entry_price_from_fill(o, 42.5) == 42.5


class TestSubmitOrderNotional:

    def test_submit_notional_market_day_matches_rest_shape(self):
        AlpacaBroker = _import_broker()
        from alpaca.trading.requests import MarketOrderRequest

        MarketOrderRequest.reset_mock()
        broker = AlpacaBroker(api_key="k", secret="s", paper=True, config={"trading_control": {"mode": "paper"}})
        broker._trading.submit_order = MagicMock(return_value=MagicMock(id="o1"))
        out = broker.submit_notional_market_day(
            {"symbol": "spy", "notional": 750.0, "action": "buy"}
        )
        assert out is not None
        broker._trading.submit_order.assert_called_once()
        MarketOrderRequest.assert_called_once()
        _args, kw = MarketOrderRequest.call_args
        assert kw.get("notional") == pytest.approx(750.0)
        assert kw.get("symbol") == "SPY"
        assert kw.get("time_in_force") is not None
        assert kw.get("type") == "market"

    def test_submit_notional_market_day_accepts_side_key(self):
        AlpacaBroker = _import_broker()
        from alpaca.trading.requests import MarketOrderRequest

        MarketOrderRequest.reset_mock()
        broker = AlpacaBroker(api_key="k", secret="s", paper=True, config={"trading_control": {"mode": "paper"}})
        broker._trading.submit_order = MagicMock(return_value=MagicMock())
        broker.submit_notional_market_day({"symbol": "QQQ", "notional": 100.0, "side": "sell"})
        broker._trading.submit_order.assert_called_once()

    def test_submit_notional_market_day_returns_none_when_invalid(self):
        AlpacaBroker = _import_broker()
        broker = AlpacaBroker(api_key="k", secret="s", paper=True, config={"trading_control": {"mode": "paper"}})
        broker._trading.submit_order = MagicMock()
        assert broker.submit_notional_market_day({"symbol": "X", "notional": -1, "action": "buy"}) is None
        broker._trading.submit_order.assert_not_called()

    def test_shadow_blocks_alpaca_notional_market_day_before_sdk_submit(self):
        AlpacaBroker = _import_broker()
        broker = AlpacaBroker(api_key="k", secret="s", paper=False, config={"trading_control": {"mode": "shadow"}})
        broker._trading.submit_order = MagicMock(side_effect=AssertionError("SDK submit must not be called"))

        out = broker.submit_notional_market_day({"symbol": "SPY", "notional": 100.0, "action": "buy", "route": "trend_long"})

        assert out.status == "shadow"
        broker._trading.submit_order.assert_not_called()

    def test_shadow_blocks_alpaca_sell_and_close_helpers_before_sdk_calls(self):
        AlpacaBroker = _import_broker()
        broker = AlpacaBroker(api_key="k", secret="s", paper=False, config={"trading_control": {"mode": "shadow"}})
        broker._trading.submit_order = MagicMock(side_effect=AssertionError("SDK sell must not be called"))
        broker._trading.close_position = MagicMock(side_effect=AssertionError("SDK close must not be called"))

        sell = broker.submit_market_sell("SPY", 1)
        close = broker.close_position("SPY")

        assert sell.status == "shadow"
        assert close.status == "shadow"
        broker._trading.submit_order.assert_not_called()
        broker._trading.close_position.assert_not_called()

    def test_submit_order_passes_notional_to_market_order_request(self):
        AlpacaBroker = _import_broker()
        from alpaca.trading.requests import MarketOrderRequest
        from src.execution import OrderRequest, OrderType

        MarketOrderRequest.reset_mock()
        broker = AlpacaBroker(api_key="k", secret="s", paper=True, config={"trading_control": {"mode": "paper"}})
        broker._trading.submit_order = MagicMock(return_value=MagicMock())
        order = OrderRequest(
            symbol="SPY",
            side="buy",
            quantity=0,
            order_type=OrderType.MARKET,
            notional=1000.0,
        )
        broker.submit_order(order)
        broker._trading.submit_order.assert_called_once()
        MarketOrderRequest.assert_called_once()
        _args, kw = MarketOrderRequest.call_args
        assert kw.get("notional") == pytest.approx(1000.0)
        assert kw.get("symbol") == "SPY"
        assert kw.get("time_in_force") is not None
        assert kw.get("type") == "market"
        assert "qty" not in kw or kw.get("qty") is None


class TestOrderListing:

    def test_list_orders_returns_raw_open_orders(self):
        AlpacaBroker = _import_broker()
        from alpaca.trading.requests import GetOrdersRequest

        GetOrdersRequest.reset_mock()
        broker = AlpacaBroker(api_key="k", secret="s", paper=True)
        raw = [SimpleNamespace(symbol="AAA", side="sell")]
        broker._trading.get_orders = MagicMock(return_value=raw)

        out = broker.list_orders(status="open")

        assert out == raw
        broker._trading.get_orders.assert_called_once()
        GetOrdersRequest.assert_called_once()
        _args, kw = GetOrdersRequest.call_args
        assert kw.get("status") == "open"
        assert kw.get("limit") == 500


class TestTradeUpdateStream:

    def _broker(self, enabled: bool = True):
        AlpacaBroker = _import_broker()
        return AlpacaBroker(
            config={"broker": {"trade_update_stream": {"enabled": enabled}}, "trading_control": {"mode": "paper"}},
            api_key="k",
            secret="s",
            paper=True,
        )

    def test_partial_fill_updates_local_order_state(self, caplog):
        broker = self._broker()
        update = SimpleNamespace(
            event="partial_fill",
            order=SimpleNamespace(
                id="ord-1",
                symbol="nvda",
                side="buy",
                status="partially_filled",
                qty="10",
                filled_qty="4",
                filled_avg_price="101.25",
                updated_at="2026-06-18T13:35:00Z",
            ),
        )

        with caplog.at_level(logging.INFO):
            state = broker.handle_trade_update(update)

        assert state["status"] == "partially_filled"
        assert broker.get_order_state("ord-1") == state
        assert state["symbol"] == "NVDA"
        assert state["filled_qty"] == "4"
        assert "ALPACA_TRADE_STREAM_EVENT" in caplog.text
        assert "ALPACA_TRADE_STREAM_FILL" in caplog.text

    def test_full_fill_updates_local_order_state(self, caplog):
        broker = self._broker()
        update = {
            "event": "fill",
            "order": {
                "id": "ord-2",
                "symbol": "AAPL",
                "side": "sell",
                "status": "filled",
                "qty": "5",
                "filled_qty": "5",
                "filled_avg_price": "199.5",
                "filled_at": "2026-06-18T13:36:00Z",
            },
        }

        with caplog.at_level(logging.INFO):
            state = broker.handle_trade_update(update)

        assert state["status"] == "filled"
        assert broker.get_order_state("ord-2")["avg_fill_price"] == "199.5"
        assert "ALPACA_TRADE_STREAM_FILL" in caplog.text

    @pytest.mark.parametrize(
        ("event", "expected"),
        [("rejected", "rejected"), ("canceled", "canceled"), ("expired", "expired"), ("new", "new"), ("accepted", "accepted")],
    )
    def test_terminal_and_open_events_update_local_order_state(self, event, expected, caplog):
        broker = self._broker()
        update = SimpleNamespace(
            event=event,
            order=SimpleNamespace(
                id=f"ord-{event}",
                symbol="MSFT",
                side="buy",
                status=expected,
                qty="1",
                filled_qty="0",
                filled_avg_price=None,
                updated_at="2026-06-18T13:37:00Z",
            ),
        )

        with caplog.at_level(logging.INFO):
            state = broker.handle_trade_update(update)

        assert state["status"] == expected
        assert broker.get_order_state(f"ord-{event}")["status"] == expected
        if event == "rejected":
            assert "ALPACA_TRADE_STREAM_REJECTED" in caplog.text

    def test_stream_setup_failure_falls_back_to_polling(self, caplog):
        broker = self._broker(enabled=True)

        def fail_factory(*_args, **_kwargs):
            raise RuntimeError("stream down")

        with caplog.at_level(logging.INFO):
            started = broker.start_trade_update_stream(stream_factory=fail_factory)

        assert started is False
        assert "ALPACA_TRADE_STREAM_FALLBACK_POLLING" in caplog.text
        assert "setup_failed" in caplog.text

    def test_stream_disabled_keeps_polling_fallback_and_order_submit_behavior(self, caplog):
        from src.execution import OrderRequest, OrderType

        broker = self._broker(enabled=False)
        broker._trading.submit_order = MagicMock(
            return_value=SimpleNamespace(
                id="ord-submit",
                symbol="SPY",
                side="buy",
                status="accepted",
                qty="1",
                filled_qty="0",
                filled_avg_price=None,
                updated_at="2026-06-18T13:38:00Z",
            )
        )
        order = OrderRequest(symbol="SPY", side="buy", quantity=1, order_type=OrderType.MARKET)

        with caplog.at_level(logging.INFO):
            assert broker.start_trade_update_stream() is False
            result = broker.submit_order(order)

        assert result.id == "ord-submit"
        broker._trading.submit_order.assert_called_once()
        assert broker.get_order_state("ord-submit")["status"] == "accepted"
        assert "ALPACA_TRADE_STREAM_FALLBACK_POLLING reason=disabled" in caplog.text
