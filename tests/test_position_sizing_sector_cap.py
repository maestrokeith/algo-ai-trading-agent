"""Sector exposure cap clips share count (cap-aware sizing)."""

from __future__ import annotations

import pytest

from src.position_sizing import PositionSizer


def _sec_cfg(**over: object) -> dict:
    ps: dict = {
        "risk_per_trade_pct": 2.0,
        "max_open_risk_pct": 10.0,
        "max_exposure_per_symbol_pct": 50.0,
        "max_position_dollar_cap": 0,
        "max_exposure_per_sector_pct": 12.0,
        "volatility_sizing": {"enabled": False},
        "portfolio_heat": {"enabled": False},
        "high_vol_reduction": {"enabled": False},
        "dynamic_sizing": {"enabled": False},
        "max_gross_exposure_pct": 100.0,
        "max_net_exposure_pct": 100.0,
        "max_theme_exposure_pct": 100.0,
        "max_etf_exposure_pct": 100.0,
        "max_inverse_etf_exposure_pct": 100.0,
        "small_fill": {"enabled": False},
    }
    ps.update(over)
    return {"position_sizing": ps}


def test_sector_cap_clips_shares_instead_of_rejecting() -> None:
    """Headroom under max_exposure_per_sector_pct reduces shares; trade still sizes."""
    sz = PositionSizer(_sec_cfg())
    r = sz.size_position(
        100_000.0,
        100.0,
        1.5,
        "NVDA",
        {},
        {"tech": 11.0},
        symbol_sector={"NVDA": "tech"},
        atr_pct=None,
        conviction_score=1.0,
    )
    assert r.reject_reason is None
    assert r.shares == 10
    assert r.notional == pytest.approx(1000.0)
    assert r.trim_reason is not None
    assert "sector_exposure_cap" in r.trim_reason
    assert r.exposure_pct == pytest.approx(1.0)


def test_risk_sector_cap_pct_overrides_max_exposure_per_sector() -> None:
    cfg = _sec_cfg(max_exposure_per_sector_pct=99.0)
    cfg["risk"] = {"sector_cap_pct": 15.0}
    sz = PositionSizer(cfg)
    assert sz.max_exposure_per_sector_pct == pytest.approx(15.0)


def test_risk_sector_cap_pct_fraction_form() -> None:
    cfg = _sec_cfg(max_exposure_per_sector_pct=99.0)
    cfg["risk"] = {"sector_cap_pct": 0.35}
    sz = PositionSizer(cfg)
    assert sz.max_exposure_per_sector_pct == pytest.approx(35.0)


def test_portfolio_max_sector_pct_overrides_position_sizing_fallback() -> None:
    cfg = _sec_cfg(max_exposure_per_sector_pct=12.0)
    cfg["portfolio"] = {"max_sector_pct": 0.30}
    sz = PositionSizer(cfg)
    assert sz.max_exposure_per_sector_pct == pytest.approx(30.0)


def test_portfolio_max_sector_mins_with_risk_sector_cap() -> None:
    cfg = _sec_cfg(max_exposure_per_sector_pct=99.0)
    cfg["portfolio"] = {"max_sector_pct": 0.30}
    cfg["risk"] = {"sector_cap_pct": 0.20}
    sz = PositionSizer(cfg)
    assert sz.max_exposure_per_sector_pct == pytest.approx(20.0)


def test_sector_at_max_rejects_before_clip() -> None:
    sz = PositionSizer(_sec_cfg())
    r = sz.size_position(
        100_000.0,
        100.0,
        1.5,
        "NVDA",
        {},
        {"tech": 12.0},
        symbol_sector={"NVDA": "tech"},
        atr_pct=None,
        conviction_score=1.0,
    )
    assert r.shares == 0
    assert "at or above" in (r.reject_reason or "")


def test_enforce_caps_on_unknown_false_skips_sector_for_unmapped_symbol() -> None:
    """Unmapped symbol: do not apply per-sector cap when ``sector.enforce_caps_on_unknown`` is false."""
    cfg = _sec_cfg(
        max_exposure_per_symbol_pct=50.0, max_exposure_per_sector_pct=12.0
    )
    cfg["sector"] = {"default_sector": "other", "enforce_caps_on_unknown": False}
    sz = PositionSizer(cfg)
    r = sz.size_position(
        100_000.0,
        100.0,
        1.5,
        "ZZZ",
        {},
        {"other": 20.0},
        symbol_sector={},
        atr_pct=None,
        conviction_score=1.0,
    )
    assert r.reject_reason is None
    assert "sector_exposure_cap" not in (r.trim_reason or "")
    assert r.shares == 500
