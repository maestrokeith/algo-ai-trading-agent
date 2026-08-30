"""Tests for :func:`src.loop_helpers.cancel_orders_older_than` and :func:`cancel_stale_orders`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from src.loop_helpers import cancel_orders_older_than, cancel_stale_orders


def test_cancel_orders_older_than_zero_is_noop() -> None:
    broker = MagicMock()
    now = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)
    assert cancel_orders_older_than(broker, minutes=0, now=now) == 0
    assert cancel_orders_older_than(broker, minutes=-1, now=now) == 0
    broker.get_open_orders.assert_not_called()


def test_cancel_orders_older_than_10_minutes() -> None:
    now = datetime(2026, 4, 10, 14, 0, 0, tzinfo=timezone.utc)
    old = now - timedelta(minutes=15)
    broker = MagicMock()
    broker.get_open_orders.return_value = [
        {"id": "a1", "symbol": "SPY", "submitted_at": old},
    ]
    n = cancel_orders_older_than(broker, minutes=10, now=now, verbose=False)
    assert n == 1
    broker.cancel_order_by_id.assert_called_once_with("a1")


def test_cancel_stale_orders_disabled_when_zero() -> None:
    broker = MagicMock()
    now = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)
    assert cancel_stale_orders(broker, now=now, entries_cfg={}) == 0
    assert cancel_stale_orders(broker, now=now, entries_cfg={"cancel_orders_older_than_minutes": 0}) == 0
    assert cancel_stale_orders(broker, now=now, entries_cfg={"cancel_stale_open_orders_minutes": 0}) == 0
    broker.get_open_orders.assert_not_called()


def test_cancel_stale_orders_skips_without_broker_methods() -> None:
    broker = object()
    now = datetime.now(timezone.utc)
    assert cancel_stale_orders(broker, now=now, entries_cfg={"cancel_orders_older_than_minutes": 60}) == 0


def test_cancel_stale_orders_cancels_only_old_orders() -> None:
    now = datetime(2026, 4, 10, 14, 0, 0, tzinfo=timezone.utc)
    old = now - timedelta(hours=3)
    recent = now - timedelta(minutes=15)

    broker = MagicMock()
    broker.get_open_orders.return_value = [
        {"id": "old1", "symbol": "AAPL", "submitted_at": old},
        {"id": "new1", "symbol": "MSFT", "submitted_at": recent},
        {"id": "", "symbol": "QQQ", "submitted_at": old},
    ]

    n = cancel_stale_orders(
        broker,
        now=now,
        entries_cfg={"cancel_orders_older_than_minutes": 60},
        verbose=False,
    )
    assert n == 1
    broker.cancel_order_by_id.assert_called_once_with("old1")


def test_cancel_stale_orders_iso_string_submitted_at() -> None:
    now = datetime(2026, 4, 10, 14, 0, 0, tzinfo=timezone.utc)
    old_iso = (now - timedelta(hours=2)).isoformat()

    broker = MagicMock()
    broker.get_open_orders.return_value = [
        {"id": "x1", "symbol": "SPY", "submitted_at": old_iso},
    ]

    n = cancel_stale_orders(
        broker,
        now=now,
        entries_cfg={"cancel_orders_older_than_minutes": 30},
        verbose=False,
    )
    assert n == 1
    broker.cancel_order_by_id.assert_called_once_with("x1")


def test_cancel_stale_orders_continues_on_cancel_failure() -> None:
    now = datetime(2026, 4, 10, 14, 0, 0, tzinfo=timezone.utc)
    old = now - timedelta(hours=2)

    broker = MagicMock()
    broker.get_open_orders.return_value = [
        {"id": "fail", "symbol": "A", "submitted_at": old},
        {"id": "ok", "symbol": "B", "submitted_at": old},
    ]

    def cancel_side_effect(oid: str) -> None:
        if oid == "fail":
            raise RuntimeError("api")

    broker.cancel_order_by_id.side_effect = cancel_side_effect

    n = cancel_stale_orders(
        broker,
        now=now,
        entries_cfg={"cancel_orders_older_than_minutes": 30},
        verbose=False,
    )
    assert n == 1
    assert broker.cancel_order_by_id.call_count == 2


def test_cancel_orders_older_than_minutes_takes_precedence_over_legacy() -> None:
    now = datetime(2026, 4, 10, 14, 0, 0, tzinfo=timezone.utc)
    submitted = now - timedelta(minutes=45)
    broker = MagicMock()
    broker.get_open_orders.return_value = [{"id": "p", "symbol": "IWM", "submitted_at": submitted}]
    n = cancel_stale_orders(
        broker,
        now=now,
        entries_cfg={
            "cancel_orders_older_than_minutes": 30,
            "cancel_stale_open_orders_minutes": 120,
        },
        verbose=False,
    )
    assert n == 1
    broker.cancel_order_by_id.assert_called_once_with("p")


def test_cancel_stale_orders_legacy_key_when_new_missing() -> None:
    now = datetime(2026, 4, 10, 14, 0, 0, tzinfo=timezone.utc)
    old = now - timedelta(hours=1)
    broker = MagicMock()
    broker.get_open_orders.return_value = [{"id": "x", "symbol": "QQQ", "submitted_at": old}]
    n = cancel_stale_orders(
        broker,
        now=now,
        entries_cfg={"cancel_stale_open_orders_minutes": 20},
        verbose=False,
    )
    assert n == 1
    broker.cancel_order_by_id.assert_called_once_with("x")
