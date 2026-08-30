"""Tests for portfolio.risk_book_mode presets (apply_risk_book_mode)."""

from __future__ import annotations

import copy

import pytest

from src.config_loader import load_config
from src.risk_book_mode import apply_risk_book_mode


def _minimal_cfg() -> dict:
    return {
        "portfolio": {
            "risk_book_mode": "balanced",
            "exposure_gates": {"enabled": True, "max_total_exposure_frac": 0.5},
            "target_gross_exposure_pct": 0.5,
            "min_cash_reserve_pct": 99,
        },
        "adaptive": {
            "max_exposure_by_regime": {"bullish": 0.5, "neutral": 0.5, "bearish": 0.5},
            "boost_exposure_if_many_signals": False,
            "regime_3": {"max_exposure": 0.11},
        },
        "cash_management": {
            "reserve_by_regime": {"bullish": 99, "neutral": 99, "bearish": 99},
        },
    }


@pytest.mark.parametrize(
    "mode,max_gross,res_pct,bear_ad,bear_cash,boost",
    [
        ("safe", 0.85, 15, 0.85, 15, False),
        ("balanced", 0.95, 5, 0.95, 5, True),
        ("aggressive", 1.0, 1, 0.98, 2, True),
    ],
)
def test_apply_risk_book_mode_presets(
    mode: str,
    max_gross: float,
    res_pct: float,
    bear_ad: float,
    bear_cash: int,
    boost: bool,
) -> None:
    cfg = copy.deepcopy(_minimal_cfg())
    cfg["portfolio"]["risk_book_mode"] = mode
    apply_risk_book_mode(cfg)
    port = cfg["portfolio"]
    assert port["exposure_gates"]["max_total_exposure_frac"] == pytest.approx(max_gross)
    assert port["target_gross_exposure_pct"] == pytest.approx(max_gross)
    assert port["min_cash_reserve_pct"] == pytest.approx(res_pct)
    ad = cfg["adaptive"]
    assert ad["max_exposure_by_regime"]["bullish"] == pytest.approx(max_gross)
    assert ad["max_exposure_by_regime"]["neutral"] == pytest.approx(max_gross)
    assert ad["max_exposure_by_regime"]["bearish"] == pytest.approx(bear_ad)
    assert ad["boost_exposure_if_many_signals"] is boost
    assert cfg["cash_management"]["reserve_by_regime"]["bullish"] == pytest.approx(res_pct)
    assert cfg["cash_management"]["reserve_by_regime"]["neutral"] == pytest.approx(res_pct)
    assert cfg["cash_management"]["reserve_by_regime"]["bearish"] == pytest.approx(bear_cash)
    r3 = cfg["adaptive"]["regime_3"]
    assert r3["max_exposure"] == pytest.approx(ad["max_exposure_by_regime"]["neutral"])


@pytest.mark.parametrize("skip", ["custom", "none", "off", "false", ""])
def test_apply_risk_book_mode_skips_custom_like(skip: str) -> None:
    cfg = _minimal_cfg()
    if skip:
        cfg["portfolio"]["risk_book_mode"] = skip
    else:
        del cfg["portfolio"]["risk_book_mode"]
    before = copy.deepcopy(cfg)
    apply_risk_book_mode(cfg)
    assert cfg == before


def test_load_config_default_yaml_portfolio_custom() -> None:
    """default.yaml uses ``risk_book_mode: custom`` so explicit gross / cash reserve are not preset-overridden."""
    cfg = load_config()
    assert cfg["portfolio"].get("risk_book_mode") == "custom"
    assert cfg["portfolio"]["exposure_gates"]["max_total_exposure_frac"] == pytest.approx(0.95)
    assert cfg["portfolio"]["min_cash_reserve_pct"] == pytest.approx(12)
    assert cfg["adaptive"]["max_exposure_by_regime"]["neutral"] == pytest.approx(0.95)
