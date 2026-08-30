"""Tests for position_sizing.small_fill (minimum 1 share when headroom allows)."""

from __future__ import annotations

import pytest

from src.position_sizing import PositionSizer


def _sf_base(**ps_over: object) -> dict:
    ps: dict = {
        "risk_per_trade_pct": 0.01,
        "max_open_risk_pct": 5.0,
        "max_exposure_per_symbol_pct": 100.0,
        "max_position_dollar_cap": 0,
        "max_exposure_per_sector_pct": 100.0,
        "volatility_sizing": {"enabled": False},
        "portfolio_heat": {"enabled": False},
        "high_vol_reduction": {"enabled": False},
        "dynamic_sizing": {"enabled": False},
        "max_gross_exposure_pct": 100.0,
        "max_net_exposure_pct": 100.0,
        "max_theme_exposure_pct": 100.0,
        "max_etf_exposure_pct": 100.0,
        "max_inverse_etf_exposure_pct": 100.0,
        "small_fill": {"enabled": True, "min_trade_value": 1.0},
        # Tests in this file assert legacy zero-share paths; production default is relief **on**.
        "micro_cap_relief": {"enabled": False},
    }
    ps.update(ps_over)
    return {"position_sizing": ps}


def test_small_fill_forces_one_share_when_risk_rounds_to_zero() -> None:
    """Tiny risk budget → int shares 0, but symbol cap has room for ≥1 share."""
    sz = PositionSizer(_sf_base())
    r = sz.size_position(
        1_000.0,
        100.0,
        1.5,
        "SPY",
        {},
        {},
        atr_pct=None,
        conviction_score=1.0,
    )
    assert r.shares == 1
    assert r.reject_reason is None
    assert r.trim_reason is not None
    assert "small_fill_minimum_1_share" in r.trim_reason


def test_small_fill_skips_when_headroom_below_one_share() -> None:
    sz = PositionSizer(_sf_base(max_exposure_per_symbol_pct=5.0))
    r = sz.size_position(
        1_000.0,
        100.0,
        1.5,
        "SPY",
        {},
        {},
        atr_pct=None,
        conviction_score=1.0,
    )
    assert r.shares == 0
    assert r.reject_reason == "symbol cap remaining yields zero shares"


def test_small_fill_skips_when_capacity_not_above_min_trade_floor() -> None:
    sz = PositionSizer(_sf_base(small_fill={"enabled": True, "min_trade_value_usd": 1500.0}))
    r = sz.size_position(
        1_000.0,
        100.0,
        1.5,
        "SPY",
        {},
        {},
        atr_pct=None,
        conviction_score=1.0,
    )
    assert r.shares == 0
    assert r.reject_reason == "symbol cap remaining yields zero shares"


def test_small_fill_disabled_preserves_zero_shares() -> None:
    sz = PositionSizer(_sf_base(small_fill={"enabled": False}))
    r = sz.size_position(
        1_000.0,
        100.0,
        1.5,
        "SPY",
        {},
        {},
        atr_pct=None,
        conviction_score=1.0,
    )
    assert r.shares == 0
    assert r.reject_reason == "symbol cap remaining yields zero shares"


def test_small_fill_one_share_helper() -> None:
    sz = PositionSizer(_sf_base(small_fill={"enabled": True, "min_trade_value": 100.0}))
    assert sz._small_fill_one_share_ok(150.0, 100.0) is True
    assert sz._small_fill_one_share_ok(100.0, 100.0) is True
    assert sz._small_fill_one_share_ok(99.0, 100.0) is False
    assert sz._small_fill_one_share_ok(500.0, 600.0) is False


def test_small_fill_min_trade_value_usd_legacy_alias() -> None:
    sz = PositionSizer({"position_sizing": {"small_fill": {"enabled": True, "min_trade_value_usd": 250.0}}})
    assert sz.small_fill_min_trade_value_usd == pytest.approx(250.0)


def test_small_fill_default_min_trade_value_when_omitted() -> None:
    sz = PositionSizer({"position_sizing": {"small_fill": {"enabled": True}}})
    assert sz.small_fill_min_trade_value_usd == pytest.approx(500.0)


def test_min_trade_dollars_alias_when_small_fill_omits_floor() -> None:
    sz = PositionSizer(
        {
            "position_sizing": {
                "small_fill": {"enabled": True},
                "min_trade_dollars": 200,
            }
        }
    )
    assert sz.small_fill_min_trade_value_usd == pytest.approx(200.0)


def test_small_fill_min_trade_value_wins_over_min_trade_dollars() -> None:
    sz = PositionSizer(
        {
            "position_sizing": {
                "min_trade_dollars": 999.0,
                "small_fill": {"enabled": True, "min_trade_value": 120.0},
            }
        }
    )
    assert sz.small_fill_min_trade_value_usd == pytest.approx(120.0)


def test_micro_cap_relief_one_share_when_headroom_below_one_share() -> None:
    """Dust headroom under symbol %% cap: allow 1 share (default production behavior)."""
    sz = PositionSizer(
        _sf_base(
            risk_per_trade_pct=1.0,
            max_exposure_per_symbol_pct=5.0,
            micro_cap_relief={"enabled": True, "mode": "one_share"},
        )
    )
    r = sz.size_position(
        1_000.0,
        100.0,
        1.5,
        "SPY",
        {},
        {},
        atr_pct=None,
        conviction_score=1.0,
    )
    assert r.shares == 1
    assert r.reject_reason is None
    assert r.trim_reason is not None
    assert "micro_cap_relief_one_share" in r.trim_reason


def test_micro_cap_relief_full_risk_mode() -> None:
    sz = PositionSizer(
        _sf_base(
            risk_per_trade_pct=1.0,
            max_exposure_per_symbol_pct=5.0,
            micro_cap_relief={"enabled": True, "mode": "full_risk"},
        )
    )
    r = sz.size_position(
        1_000.0,
        100.0,
        1.5,
        "SPY",
        {},
        {},
        atr_pct=None,
        conviction_score=1.0,
    )
    assert r.shares >= 1
    assert r.reject_reason is None
    assert "micro_cap_relief_full_risk" in (r.trim_reason or "")
