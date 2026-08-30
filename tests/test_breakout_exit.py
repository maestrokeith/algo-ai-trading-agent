"""Tests for breakout-specific exit rules."""

from __future__ import annotations

from src.strategies.breakout_exit import evaluate_breakout_exit
from src.strategy import ExitReason


def test_breakout_exit_full_take_profit_has_priority() -> None:
    sig = evaluate_breakout_exit(
        symbol="NVDA",
        pnl_pct=2.6,
        price=105.0,
        vwap=104.0,
        ema9=104.5,
        hold_minutes=10.0,
        current_qty=10,
        partial_taken=False,
    )
    assert sig is not None
    assert sig.reason == ExitReason.TAKE_PROFIT


def test_breakout_exit_partial_take_profit_only_once() -> None:
    sig = evaluate_breakout_exit(
        symbol="NVDA",
        pnl_pct=1.6,
        price=101.6,
        vwap=101.0,
        ema9=101.2,
        hold_minutes=10.0,
        current_qty=10,
        partial_taken=False,
    )
    assert sig is not None
    assert sig.reason == ExitReason.PARTIAL_TAKE_PROFIT
    assert sig.metadata["qty_to_sell"] == 5

    sig2 = evaluate_breakout_exit(
        symbol="NVDA",
        pnl_pct=1.6,
        price=101.6,
        vwap=101.0,
        ema9=101.2,
        hold_minutes=10.0,
        current_qty=10,
        partial_taken=True,
    )
    assert sig2 is None


def test_breakout_exit_signal_break_on_vwap_or_ema9() -> None:
    sig = evaluate_breakout_exit(
        symbol="NVDA",
        pnl_pct=0.4,
        price=100.0,
        vwap=100.5,
        ema9=99.8,
        hold_minutes=10.0,
        current_qty=10,
        partial_taken=False,
    )
    assert sig is not None
    assert sig.reason == ExitReason.SIGNAL_EXIT


def test_breakout_exit_stop_loss_and_timeout() -> None:
    sig = evaluate_breakout_exit(
        symbol="NVDA",
        pnl_pct=-0.8,
        price=99.2,
        vwap=99.0,
        ema9=99.1,
        hold_minutes=10.0,
        current_qty=10,
        partial_taken=False,
    )
    assert sig is not None
    assert sig.reason == ExitReason.STOP_LOSS

    sig2 = evaluate_breakout_exit(
        symbol="NVDA",
        pnl_pct=0.5,
        price=100.5,
        vwap=100.0,
        ema9=100.1,
        hold_minutes=61.0,
        current_qty=10,
        partial_taken=False,
    )
    assert sig2 is not None
    assert sig2.reason == ExitReason.TIME_BARS


def test_breakout_exit_trailing_stop_after_partial() -> None:
    sig = evaluate_breakout_exit(
        symbol="NVDA",
        pnl_pct=1.0,
        price=100.4,
        vwap=100.0,
        ema9=100.1,
        hold_minutes=20.0,
        current_qty=10,
        partial_taken=True,
        trail_high=101.0,
        trailing_stop_pct=0.5,
    )
    assert sig is not None
    assert sig.reason == ExitReason.TRAILING_STOP
