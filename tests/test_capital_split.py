"""Tests for ``portfolio.capital_split`` stocks vs options deployable BP partition."""

from __future__ import annotations

import pytest

from src.portfolio_allocation import (
    capital_split_stock_option_fracs,
    scaled_buying_power_for_lane,
)


def test_capital_split_disabled_legacy_shared_pool() -> None:
    assert capital_split_stock_option_fracs({}) == (1.0, 1.0)
    assert capital_split_stock_option_fracs({"portfolio": {}}) == (1.0, 1.0)


def test_capital_split_enabled_70_30() -> None:
    cfg = {
        "portfolio": {
            "capital_split": {"enabled": True, "stocks": 0.70, "options": 0.30}
        }
    }
    st, op = capital_split_stock_option_fracs(cfg)
    assert st == pytest.approx(0.70)
    assert op == pytest.approx(0.30)


def test_capital_split_percent_strings() -> None:
    cfg = {
        "portfolio": {
            "capital_split": {"enabled": True, "stocks": "70%", "options": "30%"}
        }
    }
    st, op = capital_split_stock_option_fracs(cfg)
    assert st == pytest.approx(0.70)
    assert op == pytest.approx(0.30)


def test_capital_split_infer_missing_side() -> None:
    cfg = {"portfolio": {"capital_split": {"enabled": True, "stocks": 0.8}}}
    st, op = capital_split_stock_option_fracs(cfg)
    assert st == pytest.approx(0.8)
    assert op == pytest.approx(0.2)


def test_capital_split_renormalizes_when_sum_not_one() -> None:
    cfg = {
        "portfolio": {"capital_split": {"enabled": True, "stocks": 2.0, "options": 3.0}}
    }
    st, op = capital_split_stock_option_fracs(cfg)
    assert st == pytest.approx(0.4)
    assert op == pytest.approx(0.6)


def test_scaled_buying_power_for_lane_stocks() -> None:
    cfg = {
        "portfolio": {
            "min_cash_reserve_pct": 0,
            "capital_split": {"enabled": True, "stocks": 0.5, "options": 0.5},
        }
    }
    # equity 100k, no reserve → deployable 100k from equity cap; stocks lane → 50k
    bp = scaled_buying_power_for_lane(
        buying_power=100_000.0,
        equity=100_000.0,
        config=cfg,
        regime_score=None,
        regime_condition=None,
        full_invest=False,
        lane="stocks",
    )
    assert bp == pytest.approx(50_000.0)


def test_scaled_buying_power_for_lane_options_fraction() -> None:
    cfg = {
        "portfolio": {
            "min_cash_reserve_pct": 0,
            "capital_split": {"enabled": True, "stocks": 0.7, "options": 0.3},
        }
    }
    bp = scaled_buying_power_for_lane(
        buying_power=100_000.0,
        equity=100_000.0,
        config=cfg,
        lane="options",
    )
    assert bp == pytest.approx(30_000.0)
