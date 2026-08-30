"""Tests for :func:`src.loop_helpers.emergency_prepare_symbol`."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.loop_helpers import (
    EmergencyPrepareResult,
    broker_available_qty_for_symbol,
    emergency_prepare_symbol,
)


def test_emergency_prepare_symbol_cancels_only_matching_sell_orders() -> None:
    broker = MagicMock()
    broker.get_open_orders.return_value = [
        {"id": "a", "symbol": "SPY", "side": "sell"},
        {"id": "b", "symbol": "SPY", "side": "buy"},
        {"id": "c", "symbol": "QQQ", "side": "sell"},
        {"id": "d", "symbol": "spy", "side": "SELL"},
    ]
    broker.get_position.return_value = {"symbol": "SPY", "qty": 42}
    r = emergency_prepare_symbol(broker, "SPY", sleep_seconds=0)
    assert r == EmergencyPrepareResult(2, 42.0)
    cancelled = {c.args[0] for c in broker.cancel_order_by_id.call_args_list}
    assert cancelled == {"a", "d"}
    broker.get_position.assert_called_once_with("SPY")


def test_emergency_prepare_symbol_falls_back_to_get_positions_qty() -> None:
    broker = MagicMock(spec=["get_open_orders", "cancel_order_by_id", "get_positions"])
    broker.get_open_orders.return_value = []
    broker.get_positions.return_value = [{"symbol": "QQQ", "qty": 100}]
    r = emergency_prepare_symbol(broker, "QQQ", sleep_seconds=0)
    assert r.available_qty == pytest.approx(100.0)
    assert r.cancelled_sell_orders == 0


def test_emergency_prepare_symbol_no_position_returns_zero_qty() -> None:
    broker = MagicMock()
    broker.get_open_orders.return_value = []
    broker.get_position.return_value = None
    broker.get_positions.return_value = []
    r = emergency_prepare_symbol(broker, "SPY", sleep_seconds=0)
    assert r.available_qty == pytest.approx(0.0)


def test_emergency_prepare_symbol_without_cancel_helpers_still_returns_qty() -> None:
    broker = MagicMock(spec=["get_position"])
    broker.get_position.return_value = MagicMock(qty="17")
    r = emergency_prepare_symbol(broker, "X", sleep_seconds=0)
    assert r.cancelled_sell_orders == 0
    assert r.available_qty == pytest.approx(17.0)


def test_emergency_prepare_symbol_empty_symbol_returns_zero_tuple() -> None:
    broker = MagicMock()
    r = emergency_prepare_symbol(broker, "  ", sleep_seconds=0)
    assert r == EmergencyPrepareResult(0, 0.0)


def test_emergency_prepare_symbol_get_orders_raises_still_fetches_qty() -> None:
    broker = MagicMock()
    broker.get_open_orders.side_effect = RuntimeError("network")
    broker.get_position.return_value = {"symbol": "SPY", "qty": 3}
    r = emergency_prepare_symbol(broker, "SPY", sleep_seconds=0)
    assert r.cancelled_sell_orders == 0
    assert r.available_qty == pytest.approx(3.0)


def test_emergency_prepare_symbol_sleep_skipped_when_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = MagicMock()
    broker.get_open_orders.return_value = [{"id": "x", "symbol": "X", "side": "sell"}]
    broker.get_position.return_value = {"qty": 1}
    slept: list[float] = []
    monkeypatch.setattr(
        "src.loop_helpers.time.sleep", lambda t: slept.append(float(t))
    )
    emergency_prepare_symbol(broker, "X", sleep_seconds=0)
    assert slept == []


def test_emergency_prepare_symbol_sleeps_after_cancel_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = MagicMock()
    broker.get_open_orders.return_value = [{"id": "x", "symbol": "X", "side": "sell"}]
    broker.get_position.return_value = {"qty": 1}
    slept: list[float] = []

    def _capture(t: float) -> None:
        slept.append(float(t))

    monkeypatch.setattr("src.loop_helpers.time.sleep", _capture)
    emergency_prepare_symbol(broker, "X", sleep_seconds=0.25)
    assert len(slept) == 1
    assert slept[0] == pytest.approx(0.25)


def test_broker_available_qty_for_symbol_uses_get_position() -> None:
    b = MagicMock()
    b.get_position.return_value = {"qty": 9}
    assert broker_available_qty_for_symbol(b, "A") == pytest.approx(9.0)
