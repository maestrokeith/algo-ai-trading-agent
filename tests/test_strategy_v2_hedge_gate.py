"""Tests for trend-long hedge position gate (require inverse sleeve)."""

from __future__ import annotations

import pytest

from src.strategy_v2.hedge import (
    allow_sqqq_logic,
    long_hedge_position_held,
    trend_long_hedge_requirement_ok,
)


def test_long_hedge_held_from_positions() -> None:
    cfg = {"strategy_v2": {"hedging": {"symbol": "SQQQ"}}}
    pos = [{"symbol": "SQQQ", "qty": 1}]
    assert long_hedge_position_held(cfg, pos, {})


def test_long_hedge_held_from_tracked() -> None:
    cfg = {}
    assert long_hedge_position_held(cfg, [], {"SQQQ": {"qty": 2}})


def test_long_hedge_not_held_zero_qty() -> None:
    cfg = {}
    assert not long_hedge_position_held(cfg, [{"symbol": "SQQQ", "qty": 0}], {})


@pytest.mark.parametrize(
    "cfg_extra,positions,tracked,bearish_regime,expect_ok",
    [
        ({}, [], {}, False, True),
        ({"universe": {"require_sqqq_for_trend_long_entries": False}}, [], {}, False, True),
        ({"universe": {"require_sqqq_for_trend_long_entries": True}}, [], {}, False, True),
        ({"universe": {"require_sqqq_for_trend_long_entries": True}}, [], {}, True, False),
        (
            {"universe": {"require_sqqq_for_trend_long_entries": True}},
            [{"symbol": "SQQQ", "qty": 1}],
            {},
            True,
            True,
        ),
        (
            {
                "universe": {"require_sqqq_for_trend_long_entries": True},
                "strategy_v2": {"hedging": {"symbol": "SPXS"}},
            },
            [{"symbol": "SQQQ", "qty": 5}],
            {},
            True,
            False,
        ),
        (
            {
                "universe": {"require_sqqq_for_trend_long_entries": True},
                "strategy_v2": {"hedging": {"symbol": "SPXS"}},
            },
            [{"symbol": "SPXS", "qty": 1}],
            {},
            True,
            True,
        ),
    ],
)
def test_trend_long_hedge_requirement_ok(
    cfg_extra: dict,
    positions: list,
    tracked: dict,
    bearish_regime: bool,
    expect_ok: bool,
) -> None:
    cfg: dict = {}
    cfg.update(cfg_extra)
    ok, reason = trend_long_hedge_requirement_ok(
        cfg, positions, tracked, bearish_regime=bearish_regime
    )
    assert ok is expect_ok
    if not expect_ok:
        assert reason is not None
        assert "blocked" in reason or "off" in reason.lower()


def test_allow_sqqq_logic_held_and_not_held() -> None:
    cfg = {"universe": {"require_sqqq_for_trend_long_entries": True}}
    ok, _ = allow_sqqq_logic(cfg, [{"symbol": "SQQQ", "qty": 1}], {})
    assert ok is True
    ok2, r2 = allow_sqqq_logic(cfg, [], {})
    assert ok2 is False
    assert r2 is not None and "SQQQ" in r2


def test_require_sqqq_regime_condition_bearish_uses_allow_sqqq_logic() -> None:
    ok, reason = trend_long_hedge_requirement_ok(
        {"universe": {"require_sqqq_for_trend_long_entries": True}},
        [],
        {},
        regime_condition="bearish",
        bearish_regime=False,
    )
    assert ok is False
    assert reason is not None and "blocked" in reason


def test_require_sqqq_bullish_allows_longs_without_hedge() -> None:
    ok, reason = trend_long_hedge_requirement_ok(
        {"universe": {"require_sqqq_for_trend_long_entries": True}},
        [],
        {},
        regime_condition="bullish",
    )
    assert ok is True
    assert reason is None


def test_require_sqqq_neutral_allows_longs_without_hedge() -> None:
    ok, _ = trend_long_hedge_requirement_ok(
        {"universe": {"require_sqqq_for_trend_long_entries": True}},
        [],
        {},
        regime_condition="neutral",
    )
    assert ok is True


def test_require_sqqq_defensive_blocks_trend_longs() -> None:
    ok, reason = trend_long_hedge_requirement_ok(
        {"universe": {"require_sqqq_for_trend_long_entries": True}},
        [{"symbol": "SQQQ", "qty": 100}],
        {},
        regime_condition="defensive",
    )
    assert ok is False
    assert reason is not None
    assert "defensive" in reason.lower()


def test_require_sqqq_defensive_case_insensitive() -> None:
    ok, reason = trend_long_hedge_requirement_ok(
        {"universe": {"require_sqqq_for_trend_long_entries": True}},
        [],
        {},
        regime_condition="Defensive",
    )
    assert ok is False
    assert reason is not None
