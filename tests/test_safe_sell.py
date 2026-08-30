"""Tests for :mod:`src.safe_sell`."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.safe_sell import (
    available_sell_qty_shares,
    build_safe_sell_order_request,
    clamp_sell_qty_for_open_orders,
    maybe_submit_dust_cleanup,
    open_sell_orders_held_qty,
    submit_fractional_full_close,
)


def test_open_sell_orders_held_qty_sums_sell_side() -> None:
    b = MagicMock()
    b.get_open_orders.return_value = [
        {"id": "1", "symbol": "SPY", "side": "sell", "qty": 3},
        {"id": "2", "symbol": "SPY", "side": "buy", "qty": 10},
        {"id": "3", "symbol": "SPY", "side": "SELL", "qty": 2},
    ]
    assert open_sell_orders_held_qty(b, "SPY") == pytest.approx(5.0)


def test_available_subtracts_held_from_position() -> None:
    b = MagicMock()
    b.get_position.return_value = {"symbol": "SPY", "qty": 100}
    b.get_open_orders.return_value = [
        {"symbol": "SPY", "side": "sell", "qty": 15},
    ]
    pq, held, avail = available_sell_qty_shares(b, "SPY")
    assert pq == pytest.approx(100.0)
    assert held == pytest.approx(15.0)
    assert avail == pytest.approx(85.0)


def test_available_prefers_broker_available_position_qty() -> None:
    b = MagicMock()
    b.available_position_qty = MagicMock(return_value=(10.0, 8.0, 2.0))
    b.get_position.return_value = {
        "symbol": "SPY",
        "qty": 10.0,
        "qty_available": 2.0,
        "qty_held_for_orders": 8.0,
    }
    b.get_open_orders.return_value = [{"symbol": "SPY", "side": "sell", "qty": 8}]

    pq, held, avail = available_sell_qty_shares(b, "SPY")

    assert pq == pytest.approx(10.0)
    assert held == pytest.approx(8.0)
    assert avail == pytest.approx(2.0)
    b.available_position_qty.assert_called_once_with("SPY")


def test_clamp_respects_available() -> None:
    b = MagicMock()
    b.get_position.return_value = {"qty": 50}
    b.get_open_orders.return_value = [{"symbol": "X", "side": "sell", "qty": 40}]
    assert clamp_sell_qty_for_open_orders(b, "X", 100) == 10


def test_clamp_includes_fractional_tail_for_final_chunk() -> None:
    b = MagicMock()
    b.get_position.return_value = {"qty": 7.4}
    b.get_open_orders.return_value = []
    assert clamp_sell_qty_for_open_orders(b, "X", 7) == pytest.approx(7.4)


def test_clamp_allows_sub_one_share_final_position() -> None:
    b = MagicMock()
    b.get_position.return_value = {"qty": 0.4}
    b.get_open_orders.return_value = []
    assert clamp_sell_qty_for_open_orders(b, "X", 0.4) == pytest.approx(0.4)


def test_build_safe_sell_order_request_none_when_flat() -> None:
    b = MagicMock()
    b.get_position.return_value = None
    b.get_positions.return_value = []
    ex = MagicMock()
    ex.build_order.return_value = MagicMock()
    assert (
        build_safe_sell_order_request(
            b,
            ex,
            "ZZZ",
            10,
            mid_price=100.0,
            spread_pct=0.1,
            positions=[],
        )
        is None
    )
    ex.build_order.assert_not_called()


def test_build_safe_sell_order_request_calls_build_order_with_clamped_qty() -> None:
    b = MagicMock()
    b.available_position_qty = MagicMock(return_value=(20.0, 12.0, 8.0))
    ex = MagicMock()
    fake_req = MagicMock()
    ex.build_order.return_value = fake_req
    req = build_safe_sell_order_request(
        b,
        ex,
        "A",
        50,
        mid_price=10.0,
        spread_pct=0.2,
        positions=[{"symbol": "A", "qty": 20}],
    )
    assert req is fake_req
    kw = ex.build_order.call_args
    assert kw[0][2] == 8  # min(50, 20-12)


def test_build_safe_sell_order_request_none_when_fully_held_by_open_orders() -> None:
    b = MagicMock()
    b.available_position_qty = MagicMock(return_value=(10.0, 10.0, 0.0))
    ex = MagicMock()
    ex.build_order.return_value = MagicMock()

    req = build_safe_sell_order_request(
        b,
        ex,
        "AAPL",
        10,
        mid_price=190.0,
        spread_pct=0.1,
        positions=[{"symbol": "AAPL", "qty": 10}],
    )

    assert req is None
    ex.build_order.assert_not_called()


def test_build_safe_sell_order_request_passes_fractional_final_qty() -> None:
    b = MagicMock()
    b.get_position.return_value = {"qty": 7.4}
    b.get_open_orders.return_value = []
    ex = MagicMock()
    fake_req = MagicMock()
    ex.build_order.return_value = fake_req
    req = build_safe_sell_order_request(
        b,
        ex,
        "A",
        7,
        mid_price=10.0,
        spread_pct=0.2,
        positions=[{"symbol": "A", "qty": 7.4}],
    )
    assert req is fake_req
    args = ex.build_order.call_args[0]
    kwargs = ex.build_order.call_args.kwargs
    assert args[2] == pytest.approx(7.4)
    assert kwargs["position_qty"] == pytest.approx(7.4)


def test_full_exit_sells_exact_fractional_qty_with_market_sell_fallback(caplog: pytest.LogCaptureFixture) -> None:
    b = MagicMock()
    b.available_position_qty = MagicMock(return_value=(0.771667788, 0.0, 0.771667788))
    b.close_position = None
    b.submit_market_sell.return_value = MagicMock(id="dust-pltr")

    with caplog.at_level("INFO", logger="src.safe_sell"):
        order = submit_fractional_full_close(b, "PLTR", reason="stop_loss")

    assert order.id == "dust-pltr"
    b.submit_market_sell.assert_called_once_with("PLTR", pytest.approx(0.771667788))
    assert "FRACTIONAL_FULL_CLOSE symbol=PLTR qty=0.771667788 reason=stop_loss" in caplog.text


def test_full_exit_prefers_close_position_when_available() -> None:
    b = MagicMock()
    b.available_position_qty = MagicMock(return_value=(0.775422036, 0.0, 0.775422036))
    b.close_position.return_value = MagicMock(id="close-crwd")

    order = submit_fractional_full_close(b, "CRWD", reason="hard_exit")

    assert order.id == "close-crwd"
    b.close_position.assert_called_once_with("CRWD")
    b.submit_market_sell.assert_not_called()


def test_dust_cleanup_closes_small_unprotected_leftover(caplog: pytest.LogCaptureFixture) -> None:
    b = MagicMock()
    b.available_position_qty = MagicMock(return_value=(0.771667788, 0.0, 0.771667788))
    b.close_position.return_value = MagicMock(id="close-pltr")

    with caplog.at_level("INFO", logger="src.safe_sell"):
        maybe_submit_dust_cleanup(
            b,
            "PLTR",
            market_value=86.34,
            config={"execution": {"dust_cleanup_threshold_usd": 100}},
            protected_symbols=["AAPL"],
            active_intent_symbols=[],
        )

    b.close_position.assert_called_once_with("PLTR")
    assert "DUST_POSITION_DETECTED symbol=PLTR market_value=86.34 threshold=100.00" in caplog.text
    assert "DUST_CLEANUP_ORDER_SUBMITTED symbol=PLTR qty=0.771667788" in caplog.text


def test_dust_cleanup_skips_when_active_buy_or_hold_intent(caplog: pytest.LogCaptureFixture) -> None:
    b = MagicMock()
    b.available_position_qty = MagicMock(return_value=(0.771667788, 0.0, 0.771667788))

    with caplog.at_level("INFO", logger="src.safe_sell"):
        maybe_submit_dust_cleanup(
            b,
            "PLTR",
            market_value=86.34,
            active_intent_symbols=["PLTR"],
        )

    b.close_position.assert_not_called()
    b.submit_market_sell.assert_not_called()
    assert "DUST_CLEANUP_SKIPPED symbol=PLTR reason=active_buy_or_hold_intent" in caplog.text


def test_dust_cleanup_skips_when_open_sell_order_exists(caplog: pytest.LogCaptureFixture) -> None:
    b = MagicMock()
    b.available_position_qty = MagicMock(return_value=(0.771667788, 0.771667788, 0.0))

    with caplog.at_level("INFO", logger="src.safe_sell"):
        maybe_submit_dust_cleanup(b, "PLTR", market_value=86.34)

    b.close_position.assert_not_called()
    b.submit_market_sell.assert_not_called()
    assert "DUST_CLEANUP_SKIPPED symbol=PLTR reason=open_sell_order" in caplog.text


def test_dust_cleanup_skips_core_protected_positions(caplog: pytest.LogCaptureFixture) -> None:
    b = MagicMock()
    b.available_position_qty = MagicMock(return_value=(0.771667788, 0.0, 0.771667788))

    with caplog.at_level("INFO", logger="src.safe_sell"):
        maybe_submit_dust_cleanup(
            b,
            "AAPL",
            market_value=86.34,
            protected_symbols=["AAPL"],
        )

    b.close_position.assert_not_called()
    b.submit_market_sell.assert_not_called()
    assert "DUST_CLEANUP_SKIPPED symbol=AAPL reason=protected_position" in caplog.text
