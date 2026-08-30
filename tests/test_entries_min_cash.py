"""Tests for entries.min_trade_size buying-power gate (loop_helpers)."""
from __future__ import annotations

from src.loop_helpers import (
    entries_insufficient_buying_power,
    entries_min_trade_size_dollars,
)


def test_min_trade_size_zero_or_omit_disabled() -> None:
    assert entries_min_trade_size_dollars({}) == 0.0
    assert entries_min_trade_size_dollars({"min_trade_size": 0}) == 0.0
    assert not entries_insufficient_buying_power(0.0, {"min_trade_size": 0})
    assert not entries_insufficient_buying_power(0.0, {})


def test_min_trade_size_parsed() -> None:
    assert entries_min_trade_size_dollars({"min_trade_size": 100}) == 100.0
    assert entries_min_trade_size_dollars({"min_trade_size": "50"}) == 50.0


def test_insufficient_when_below_threshold() -> None:
    assert entries_insufficient_buying_power(49.99, {"min_trade_size": 50})
    assert not entries_insufficient_buying_power(50.0, {"min_trade_size": 50})


def test_invalid_min_trade_size_treated_as_zero() -> None:
    assert entries_min_trade_size_dollars({"min_trade_size": "x"}) == 0.0
    assert not entries_insufficient_buying_power(0.0, {"min_trade_size": "x"})
