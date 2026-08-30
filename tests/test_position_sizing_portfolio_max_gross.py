"""``portfolio`` gross cap keys override :class:`PositionSizer` book ceiling."""

from __future__ import annotations

import pytest

from src.position_sizing import PositionSizer


def _base_ps() -> dict:
    return {
        "risk_per_trade_pct": 2.0,
        "max_open_risk_pct": 10.0,
        "max_exposure_per_symbol_pct": 50.0,
        "max_position_dollar_cap": 0,
        "max_exposure_per_sector_pct": 100.0,
        "volatility_sizing": {"enabled": False},
        "portfolio_heat": {"enabled": False},
        "high_vol_reduction": {"enabled": False},
        "dynamic_sizing": {"enabled": False},
        "max_gross_exposure_pct": 50.0,
        "max_net_exposure_pct": 100.0,
        "max_theme_exposure_pct": 100.0,
        "max_etf_exposure_pct": 100.0,
        "max_inverse_etf_exposure_pct": 100.0,
        "small_fill": {"enabled": False},
    }


def test_portfolio_max_gross_exposure_fraction_overrides_position_sizing() -> None:
    cfg = {
        "portfolio": {"max_gross_exposure": 0.95},
        "position_sizing": _base_ps(),
    }
    sz = PositionSizer(cfg)
    assert sz.max_gross_exposure_pct == pytest.approx(95.0)


def test_portfolio_max_gross_exposure_percent_over_one() -> None:
    cfg = {
        "portfolio": {"max_gross_exposure": 88.0},
        "position_sizing": _base_ps(),
    }
    sz = PositionSizer(cfg)
    assert sz.max_gross_exposure_pct == pytest.approx(88.0)


def test_omitted_portfolio_uses_position_sizing_pct() -> None:
    cfg = {"position_sizing": _base_ps()}
    sz = PositionSizer(cfg)
    assert sz.max_gross_exposure_pct == pytest.approx(50.0)


def test_portfolio_max_gross_exposure_pct_key_alias() -> None:
    cfg = {
        "portfolio": {"max_gross_exposure_pct": 0.90},
        "position_sizing": _base_ps(),
    }
    sz = PositionSizer(cfg)
    assert sz.max_gross_exposure_pct == pytest.approx(90.0)


def test_portfolio_target_gross_exposure_pct_when_max_omitted() -> None:
    cfg = {
        "portfolio": {"target_gross_exposure_pct": 0.87},
        "position_sizing": _base_ps(),
    }
    sz = PositionSizer(cfg)
    assert sz.max_gross_exposure_pct == pytest.approx(87.0)


def test_portfolio_max_gross_wins_over_target_gross() -> None:
    cfg = {
        "portfolio": {
            "max_gross_exposure_pct": 0.80,
            "target_gross_exposure_pct": 0.99,
        },
        "position_sizing": _base_ps(),
    }
    sz = PositionSizer(cfg)
    assert sz.max_gross_exposure_pct == pytest.approx(80.0)


def test_bullish_score_4_plus_raises_effective_max_gross() -> None:
    cfg = {
        "portfolio": {
            "max_gross_exposure": 0.90,
            "bullish_score_4_plus_max_gross_exposure_pct": 110,
        },
        "position_sizing": _base_ps(),
    }
    sz = PositionSizer(cfg)
    assert sz.max_gross_exposure_pct == pytest.approx(90.0)
    assert sz.effective_max_gross_exposure_pct("neutral", 4) == pytest.approx(90.0)
    assert sz.effective_max_gross_exposure_pct(None, 4) == pytest.approx(110.0)
    assert sz.effective_max_gross_exposure_pct("bullish", 4) == pytest.approx(110.0)
    assert sz.effective_max_gross_exposure_pct("bullish", 3) == pytest.approx(90.0)
