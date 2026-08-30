"""Tests for :mod:`src.application.services.execution_guard`."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.application.services.execution_guard import apply_cooldown


def test_apply_cooldown_noop_without_exit_context() -> None:
    plan = [{"action": "buy", "symbol": "SPY", "notional": 100.0}]
    out = apply_cooldown(plan, [])
    assert out == plan


def test_apply_cooldown_drops_buy_on_bulk_trim() -> None:
    ex = MagicMock()
    ex.bulk_trim_buy_cooldown_active = MagicMock(return_value=(True, "cooldown 1c"))
    ex.allocator_buy_blocked_by_priority = MagicMock(return_value=(False, None))
    plan = [{"action": "buy", "symbol": "AAPL", "notional": 200.0}]
    out = apply_cooldown(plan, [], exit_context=ex)
    assert out == []


def test_apply_cooldown_drops_buy_on_exit_priority() -> None:
    ex = MagicMock()
    ex.bulk_trim_buy_cooldown_active = MagicMock(return_value=(False, None))
    ex.allocator_buy_blocked_by_priority = MagicMock(return_value=(True, "exit first"))
    plan = [{"action": "buy", "symbol": "MSFT", "notional": 300.0}]
    out = apply_cooldown(
        plan,
        [{"symbol": "MSFT", "value": 1000.0, "score": 0.5}],
        exit_context=ex,
    )
    assert out == []


def test_apply_cooldown_keeps_sells() -> None:
    ex = MagicMock()
    ex.bulk_trim_buy_cooldown_active = MagicMock(return_value=(True, "cd"))
    plan = [{"action": "sell", "symbol": "X", "notional": 100.0}]
    out = apply_cooldown(plan, [], exit_context=ex)
    assert len(out) == 1 and out[0]["action"] == "sell"
