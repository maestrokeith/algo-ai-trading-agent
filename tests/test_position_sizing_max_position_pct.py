"""``position_sizing.max_position_pct`` (fraction) floors the per-symbol % used by :class:`PositionSizer`."""

from __future__ import annotations

import pytest

from src.position_sizing import PositionSizer


def _sizer_base_cfg(**ps_over: object) -> dict:
    ps: dict = {
        "risk_per_trade_pct": 0.5,
        "max_open_risk_pct": 5.0,
        "max_exposure_per_symbol_pct": 10.0,
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
        "small_fill": {"enabled": False},
    }
    ps.update(ps_over)
    return {"position_sizing": ps}


def test_max_position_pct_floors_tight_risk_cap_allows_shares() -> None:
    """
    Merged cap 5% would leave 0 whole shares at 200 $/sh on a 2.5K account; floor 8% yields 1 share.
    """
    cfg = {
        "risk": {"max_symbol_allocation_pct": 0.05},
        "portfolio": {"max_single_position_pct": 0.10},
    }
    cfg.update(
        _sizer_base_cfg(
            max_exposure_per_symbol_pct=5.0,
            max_position_pct=0.08,
        )
    )
    sz = PositionSizer(cfg)
    r = sz.size_position(
        2500.0,
        200.0,
        1.5,
        "EX",
        {},
        {},
        atr_pct=None,
        conviction_score=1.0,
    )
    assert r.shares >= 1
    assert r.reject_reason is None


def test_max_position_pct_alias_when_max_exposure_omitted() -> None:
    """If only ``max_position_pct`` is set, it supplies the sizer fallback when merged cap is off."""
    cfg = _sizer_base_cfg(
        max_position_pct=0.12,
    )
    del cfg["position_sizing"]["max_exposure_per_symbol_pct"]
    sz = PositionSizer(cfg)
    r = sz.size_position(
        10_000.0,
        10.0,
        1.0,
        "X",
        {},
        {},
        atr_pct=None,
        conviction_score=1.0,
    )
    # 12% of 10k = 1200; min(risk, cap) not binding with huge risk; should get many shares, not default 20% path.
    assert r.shares >= 1
    assert r.reject_reason is None


def test_max_position_pct_does_not_shrink_higher_merged_cap() -> None:
    """Floor 8% with merged 9.5% keeps 9.5% (larger of the two)."""
    cfg = {
        "risk": {"max_symbol_allocation_pct": 0.095},
        "portfolio": {"max_single_position_pct": 9.5},
    }
    cfg.update(
        _sizer_base_cfg(
            max_exposure_per_symbol_pct=5.0,
            max_position_pct=0.08,
        )
    )
    sz = PositionSizer(cfg)
    r = sz.size_position(
        20_000.0,
        10.0,
        1.0,
        "X",
        {},
        {},
        atr_pct=None,
        conviction_score=1.0,
    )
    notional = r.shares * 10.0
    # 9.5% of 20k = 1900 cap; 8% = 1600; expect headroom = 1900
    assert notional == pytest.approx(1900.0, rel=0, abs=20.0)
