"""Tests for :mod:`src.profit_reentry_gate`."""

from __future__ import annotations

from src.profit_reentry_gate import (
    entries_reentry_pullback_cfg,
    profit_reentry_price_allowed,
)


def test_entries_reentry_pullback_cfg_defaults() -> None:
    allow, buf = entries_reentry_pullback_cfg({})
    assert allow is False
    assert buf == 0.0


def test_entries_reentry_pullback_cfg_reads_entries() -> None:
    cfg = {
        "entries": {
            "allow_reentry_on_pullback": True,
            "reentry_price_buffer_pct": 1.0,
        }
    }
    allow, buf = entries_reentry_pullback_cfg(cfg)
    assert allow is True
    assert buf == 1.0


def test_profit_reentry_strict_blocks_at_or_below_exit() -> None:
    ok, reason = profit_reentry_price_allowed(
        100.0,
        100.0,
        require_price_above_exit_after_profit=True,
        allow_reentry_on_pullback=False,
        reentry_price_buffer_pct=0.5,
    )
    assert not ok and reason


def test_profit_reentry_pullback_allows_slight_dip() -> None:
    # exit 100, 0.5%% buffer → floor 99.5; close 99.7 passes
    ok, reason = profit_reentry_price_allowed(
        99.7,
        100.0,
        require_price_above_exit_after_profit=False,
        allow_reentry_on_pullback=True,
        reentry_price_buffer_pct=0.5,
    )
    assert ok and reason is None


def test_profit_reentry_pullback_blocks_deep_dip() -> None:
    ok, reason = profit_reentry_price_allowed(
        99.0,
        100.0,
        require_price_above_exit_after_profit=False,
        allow_reentry_on_pullback=True,
        reentry_price_buffer_pct=0.5,
    )
    assert not ok and reason and "pullback buffer" in reason


def test_neither_gate_skips_check() -> None:
    ok, _ = profit_reentry_price_allowed(
        50.0,
        100.0,
        require_price_above_exit_after_profit=False,
        allow_reentry_on_pullback=False,
        reentry_price_buffer_pct=0.5,
    )
    assert ok
