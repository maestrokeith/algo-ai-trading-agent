"""Tests for :mod:`src.dynamic_risk_budget`."""

from __future__ import annotations

import pytest

from src.dynamic_risk_budget import (
    MODE_DYNAMIC_RISK_BUDGET,
    dynamic_risk_budget_enabled,
    effective_drb_cash_buffer_frac,
    hedge_bucket_target_pct,
    parse_dynamic_risk_budget,
    rebalance_due,
    rebalance_interval_sec,
)


def test_rebalance_interval_sec() -> None:
    assert rebalance_interval_sec("15m") == 900
    assert rebalance_interval_sec("1h") == 3600
    assert rebalance_interval_sec(120) == 120
    assert rebalance_interval_sec("30s") == 30
    assert rebalance_interval_sec("") == 0


def test_parse_dynamic_risk_budget_none_without_mode() -> None:
    assert parse_dynamic_risk_budget({}) is None
    assert parse_dynamic_risk_budget(
        {"portfolio": {"allocator": {"type": "ranked"}}}
    ) is None


def test_parse_dynamic_risk_budget() -> None:
    c = parse_dynamic_risk_budget(
        {
            "portfolio": {
                "allocator": {
                    "mode": MODE_DYNAMIC_RISK_BUDGET,
                    "rebalance_frequency": "15m",
                    "cash_buffer_pct": 5,
                    "buckets": {
                        "hedge": {"target": 10},
                        "core_trend": 40,
                    },
                }
            }
        }
    )
    assert c is not None
    assert c.rebalance_interval_sec == 900
    assert c.cash_buffer_pp == pytest.approx(5.0)
    assert c.hedge_target_pp == pytest.approx(10.0)
    assert c.bucket_targets_pp["core_trend"] == pytest.approx(40.0)
    s = sum(c.bucket_fracs.values())
    assert s == pytest.approx(1.0, abs=1e-6)


def test_effective_drb_cash_buffer_and_hedge() -> None:
    cfg = {
        "portfolio": {
            "allocator": {
                "mode": MODE_DYNAMIC_RISK_BUDGET,
                "buckets": {"hedge": 10},
                "cash_buffer_pct": 20,
            }
        }
    }
    assert effective_drb_cash_buffer_frac(cfg) == pytest.approx(0.2)
    assert hedge_bucket_target_pct(cfg) == pytest.approx(10.0)
    assert dynamic_risk_budget_enabled(cfg) is True


def test_rebalance_due() -> None:
    assert rebalance_due(100.0, None, 60) is True
    assert rebalance_due(200.0, 100.0, 60) is True
    assert rebalance_due(150.0, 100.0, 60) is False
    assert rebalance_due(200.0, 100.0, 0) is False


def test_position_sizer_inverse_cap_from_hedge_bucket() -> None:
    from src.position_sizing import PositionSizer

    sz = PositionSizer(
        {
            "position_sizing": {"max_inverse_etf_exposure_pct": 100.0},
            "portfolio": {
                "allocator": {
                    "mode": "dynamic_risk_budget",
                    "buckets": {"hedge": {"target": 10}},
                }
            },
        }
    )
    assert sz.max_inverse_etf_exposure_pct == pytest.approx(10.0)
