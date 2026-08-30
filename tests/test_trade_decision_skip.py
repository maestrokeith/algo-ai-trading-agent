"""Tests for :func:`src.trading_engine.trade_decision_skips_entry`."""

from __future__ import annotations

from src.execution import OrderRequest, OrderType
from src.strategy import EntrySignal
from src.trading_engine import TradeDecision, trade_decision_skips_entry


def test_trade_decision_skips_entry_none() -> None:
    assert trade_decision_skips_entry(None) is True


def test_trade_decision_skips_entry_denied() -> None:
    d = TradeDecision(allowed=False, reason="spread too wide")
    assert trade_decision_skips_entry(d) is True


def test_trade_decision_skips_entry_allowed_no_order() -> None:
    d = TradeDecision(allowed=True, reason="ok", order_request=None)
    assert trade_decision_skips_entry(d) is True


def test_trade_decision_skips_entry_actionable() -> None:
    req = OrderRequest(
        symbol="SPY",
        side="buy",
        quantity=1,
        order_type=OrderType.MARKET,
        limit_price=None,
    )
    d = TradeDecision(
        allowed=True,
        reason="ok",
        order_request=req,
        entry_signal=EntrySignal(
            symbol="SPY",
            side="long",
            strength=1.0,
            stop_pct=1.5,
            take_profit_pct=3.0,
            time_bars_exit=20,
            metadata={},
        ),
    )
    assert trade_decision_skips_entry(d) is False
