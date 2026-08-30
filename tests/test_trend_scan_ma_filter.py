"""Tests for live-loop trend MA prefilter (strategy.trend_filter)."""

from __future__ import annotations

from src.strategy import trend_long_scan_ma_filter_ok


def test_require_both_ma_default_true() -> None:
    tf = {}
    assert trend_long_scan_ma_filter_ok(
        close=100.0,
        ma_fast=90.0,
        ma_slow=95.0,
        trend_filter_cfg=tf,
        long_require_ma_stack=False,
    )
    assert not trend_long_scan_ma_filter_ok(
        close=94.0,
        ma_fast=90.0,
        ma_slow=95.0,
        trend_filter_cfg=tf,
        long_require_ma_stack=False,
    )


def test_allow_above_fast_only() -> None:
    tf = {"require_above_both_ma": False, "allow_above_fast_ma": True}
    assert trend_long_scan_ma_filter_ok(
        close=100.0,
        ma_fast=99.0,
        ma_slow=105.0,
        trend_filter_cfg=tf,
        long_require_ma_stack=False,
    )
    assert not trend_long_scan_ma_filter_ok(
        close=98.0,
        ma_fast=99.0,
        ma_slow=105.0,
        trend_filter_cfg=tf,
        long_require_ma_stack=False,
    )


def test_ema_tolerance_pct_allows_slightly_below_ma() -> None:
    tf = {"require_above_both_ma": False, "allow_above_fast_ma": True, "ema_tolerance_pct": 0.1}
    # ma_fast=100; 0.1%% slack → threshold 99.9
    assert trend_long_scan_ma_filter_ok(
        close=99.95,
        ma_fast=100.0,
        ma_slow=200.0,
        trend_filter_cfg=tf,
        long_require_ma_stack=False,
    )
    assert not trend_long_scan_ma_filter_ok(
        close=99.85,
        ma_fast=100.0,
        ma_slow=200.0,
        trend_filter_cfg=tf,
        long_require_ma_stack=False,
    )


def test_ma_stack_blocks_when_fast_not_above_slow() -> None:
    tf = {"require_above_both_ma": False, "allow_above_fast_ma": True}
    assert not trend_long_scan_ma_filter_ok(
        close=103.0,
        ma_fast=100.0,
        ma_slow=101.0,
        trend_filter_cfg=tf,
        long_require_ma_stack=True,
    )
