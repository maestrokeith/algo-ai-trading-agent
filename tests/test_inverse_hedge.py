"""Tests for :mod:`src.inverse_hedge`."""

from __future__ import annotations

from src.inverse_hedge import hedge_symbol, long_hedge_position_held


def test_hedge_symbol_default() -> None:
    assert hedge_symbol({}) == "SQQQ"


def test_hedge_symbol_override() -> None:
    cfg = {"strategy_v2": {"hedging": {"symbol": "spxs"}}}
    assert hedge_symbol(cfg) == "SPXS"


def test_long_hedge_from_positions() -> None:
    assert long_hedge_position_held({}, [{"symbol": "SQQQ", "qty": 1}], {})


def test_long_hedge_from_tracked() -> None:
    assert long_hedge_position_held({}, [], {"SQQQ": {"qty": 2}})


def test_long_hedge_from_tracked_notional() -> None:
    assert long_hedge_position_held({}, [], {"SQQQ": {"notional": 1000.0}})
