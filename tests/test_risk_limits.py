"""Tests for optional ``risk.*`` limits (allocation, add-on caps)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.risk_limits import (
    add_on_allowed_for_daily_cap,
    add_on_allowed_for_min_minutes,
    allow_cross_bucket_rebalance,
    bucket_allocation_allows,
    effective_hold_for_risk,
    effective_max_sector_sleeve_pct,
    effective_symbol_allocation_cap_pct,
    gross_exposure_tier,
    other_sleeves_dollar_headroom,
    parse_allocation_fraction,
    parse_risk_emergency_cancel_all_open_orders,
    parse_risk_emergency_deleverage,
    parse_risk_over_exposure_levels,
    resolve_reduce_only_gross_frac,
    risk_no_recycle_above_frac,
    risk_no_recycle_blocks_allocator_buys,
    risk_bucket_key_for_symbol,
    risk_effective_max_bucket_allocation_frac_for_bucket,
    risk_enforce_position_caps_on_hold,
    risk_max_adds_per_symbol_per_day,
    risk_max_bucket_allocation_frac_for_bucket,
    risk_rebalance_on_breach,
    risk_rebalance_threshold_pct,
    sum_long_stock_mv_in_bucket,
    symbol_allocation_breach_trim_shares,
    tracked_add_on_count_for_et_day,
)
from src.signal_ranking import sector_etf_symbol_frozenset


def test_effective_hold_for_risk_tracked_notional() -> None:
    assert effective_hold_for_risk("NVDA", {}, {"NVDA": {"notional": 1000.0, "qty": 0}}) is True


def test_parse_allocation_fraction() -> None:
    assert parse_allocation_fraction(None) == 0.0
    assert parse_allocation_fraction(0.10) == pytest.approx(0.10)
    assert parse_allocation_fraction(10) == pytest.approx(0.10)
    assert parse_allocation_fraction("15%") == pytest.approx(0.15)


def test_parse_risk_over_exposure_levels_defaults() -> None:
    got = parse_risk_over_exposure_levels({})
    assert got["mild"] == pytest.approx(0.95)
    assert got["high"] == pytest.approx(1.0)
    assert got["critical"] == pytest.approx(1.05)


def test_resolve_reduce_only_prefers_risk_high() -> None:
    cfg = {
        "risk": {"over_exposure_levels": {"high": 0.98}},
        "portfolio": {
            "exposure_gates": {"overexposed_reduce_only_gross_frac": 1.0}
        },
    }
    assert resolve_reduce_only_gross_frac(cfg) == pytest.approx(0.98)


def test_resolve_reduce_only_falls_back_to_portfolio() -> None:
    cfg = {
        "portfolio": {
            "exposure_gates": {"overexposed_reduce_only_gross_frac": 0.99}
        }
    }
    assert resolve_reduce_only_gross_frac(cfg) == pytest.approx(0.99)


def test_gross_exposure_tier_bands() -> None:
    cfg = {
        "risk": {
            "over_exposure_levels": {
                "mild": 0.95,
                "high": 1.0,
                "critical": 1.05,
            }
        }
    }
    assert gross_exposure_tier(94.0, cfg) == "normal"
    assert gross_exposure_tier(96.0, cfg) == "mild"
    assert gross_exposure_tier(100.0, cfg) == "high"
    assert gross_exposure_tier(106.0, cfg) == "critical"


def test_risk_no_recycle_above_frac_omitted() -> None:
    assert risk_no_recycle_above_frac({}) is None
    assert risk_no_recycle_blocks_allocator_buys(100.0, {}) is False


def test_risk_no_recycle_blocks_only_above_band() -> None:
    cfg = {"risk": {"no_recycle_above_pct": 0.94}}
    assert risk_no_recycle_above_frac(cfg) == pytest.approx(0.94)
    assert risk_no_recycle_blocks_allocator_buys(94.0, cfg) is False
    assert risk_no_recycle_blocks_allocator_buys(94.01, cfg) is True
    assert risk_no_recycle_blocks_allocator_buys(100.0, cfg) is True


def test_effective_symbol_cap_stricter_of_portfolio_and_risk() -> None:
    cfg = {
        "portfolio": {"max_allocation_per_symbol": "15%"},
        "risk": {"max_symbol_allocation_pct": 0.10},
    }
    assert effective_symbol_allocation_cap_pct(cfg) == pytest.approx(10.0)


def test_effective_symbol_cap_regime_4_uses_symbol_caps_not_risk_95() -> None:
    """``capital_allocator.symbol_caps.regime_4`` relaxes the merge vs ``risk.max_symbol_allocation_pct``."""
    cfg = {
        "portfolio": {
            "max_single_position_pct": 18.0,
            "capital_allocator": {
                "symbol_caps": {
                    "soft": 0.10,
                    "hard": 0.18,
                    "regime_4": 0.15,
                }
            },
        },
        "risk": {"max_symbol_allocation_pct": 0.095},
    }
    assert effective_symbol_allocation_cap_pct(cfg, regime_score=3) == pytest.approx(9.5)
    assert effective_symbol_allocation_cap_pct(cfg, regime_score=4) == pytest.approx(15.0)


def test_effective_symbol_cap_risk_only() -> None:
    cfg = {"portfolio": {}, "risk": {"max_symbol_allocation_pct": 0.12}}
    assert effective_symbol_allocation_cap_pct(cfg) == pytest.approx(12.0)


def test_effective_symbol_cap_uses_etf_lane_when_configured() -> None:
    cfg = {
        "portfolio": {},
        "risk": {"max_symbol_allocation_pct": {"default": 15, "etf": 22}},
    }
    assert effective_symbol_allocation_cap_pct(cfg, symbol_upper="SPY") == pytest.approx(22.0)
    assert effective_symbol_allocation_cap_pct(cfg, symbol_upper="QQQ") == pytest.approx(22.0)
    assert effective_symbol_allocation_cap_pct(cfg, symbol_upper="IWM") == pytest.approx(22.0)
    assert effective_symbol_allocation_cap_pct(cfg, symbol_upper="AAPL") == pytest.approx(15.0)
    assert effective_symbol_allocation_cap_pct(cfg) == pytest.approx(15.0)


def test_effective_max_sector_sleeve_portfolio_only_uses_30() -> None:
    cfg = {
        "portfolio": {"max_sector_pct": 0.30},
        "position_sizing": {"max_exposure_per_sector_pct": 12.0},
    }
    assert effective_max_sector_sleeve_pct(cfg) == pytest.approx(30.0)


def test_effective_max_sector_sleeve_risk_only_unchanged() -> None:
    cfg = {
        "position_sizing": {"max_exposure_per_sector_pct": 99.0},
        "risk": {"sector_cap_pct": 15.0},
    }
    assert effective_max_sector_sleeve_pct(cfg) == pytest.approx(15.0)


def test_effective_max_sector_sleeve_mins_risk_and_portfolio() -> None:
    cfg = {
        "portfolio": {"max_sector_pct": 0.30},
        "risk": {"sector_cap_pct": 0.40},
        "position_sizing": {"max_exposure_per_sector_pct": 25.0},
    }
    assert effective_max_sector_sleeve_pct(cfg) == pytest.approx(30.0)


def test_effective_max_sector_sleeve_stricter_risk() -> None:
    cfg = {
        "portfolio": {"max_sector_pct": 0.30},
        "risk": {"sector_cap_pct": 0.20},
    }
    assert effective_max_sector_sleeve_pct(cfg) == pytest.approx(20.0)


def test_effective_max_sector_sleeve_fallback_to_position_sizing() -> None:
    cfg = {"position_sizing": {"max_exposure_per_sector_pct": 22.0}}
    assert effective_max_sector_sleeve_pct(cfg) == pytest.approx(22.0)


def test_effective_symbol_cap_dynamic_merges_with_risk() -> None:
    cfg = {
        "portfolio": {
            "symbol_allocation_cap": "dynamic",
            "symbol_allocation_cap_dynamic": {
                "max_pct": 30,
                "min_trade_size_usd": 500,
                "floor_pct": 10,
            },
        },
        "risk": {"max_symbol_allocation_pct": 0.18},
    }
    assert effective_symbol_allocation_cap_pct(cfg, account_equity=200_000.0) == pytest.approx(10.0)


def test_sum_long_stock_mv_in_bucket_tier_fallback() -> None:
    se = sector_etf_symbol_frozenset({})
    cfg: dict = {}
    positions = [
        {"symbol": "SPY", "qty": 1, "market_value": 5000.0},
        {"symbol": "AAPL", "qty": 1, "market_value": 3000.0},
    ]
    assert sum_long_stock_mv_in_bucket(positions, "tier_0", cfg, se) == pytest.approx(5000.0)


def test_risk_bucket_key_named_then_tier_fallback() -> None:
    se = sector_etf_symbol_frozenset({})
    cfg = {
        "risk_buckets": {
            "mega_cap_beta": ["SPY", "AAPL"],
            "financials": ["XLF"],
        }
    }
    assert risk_bucket_key_for_symbol(cfg, "AAPL", se) == "mega_cap_beta"
    assert risk_bucket_key_for_symbol(cfg, "XLF", se) == "financials"
    assert risk_bucket_key_for_symbol(cfg, "CAT", se) == "tier_3"


def test_sum_long_stock_mv_named_bucket() -> None:
    se = sector_etf_symbol_frozenset({})
    cfg = {"risk_buckets": {"mega_cap_beta": ["NVDA", "MSFT"]}}
    positions = [
        {"symbol": "NVDA", "qty": 1, "market_value": 1000.0},
        {"symbol": "MSFT", "qty": 1, "market_value": 2000.0},
        {"symbol": "XLF", "qty": 1, "market_value": 5000.0},
    ]
    assert sum_long_stock_mv_in_bucket(positions, "mega_cap_beta", cfg, se) == pytest.approx(3000.0)


def test_risk_max_bucket_allocation_frac_for_bucket_mega_override() -> None:
    cfg = {"risk": {"max_bucket_allocation_pct": 0.30, "mega_cap_beta_cap": 45}}
    assert risk_max_bucket_allocation_frac_for_bucket(cfg, "mega_cap_beta") == pytest.approx(0.45)
    assert risk_max_bucket_allocation_frac_for_bucket(cfg, "tier_2") == pytest.approx(0.30)


def test_risk_max_bucket_allocation_frac_for_bucket_tier_n_cap() -> None:
    cfg = {"risk": {"max_bucket_allocation_pct": 0.30, "tier_3_cap": 0.50}}
    assert risk_max_bucket_allocation_frac_for_bucket(cfg, "tier_3") == pytest.approx(0.50)
    assert risk_max_bucket_allocation_frac_for_bucket(cfg, "tier_2") == pytest.approx(0.30)


def test_risk_max_bucket_allocation_frac_tier_uses_max_when_tier_cap_unset() -> None:
    cfg = {"risk": {"max_bucket_allocation_pct": 0.30}}
    assert risk_max_bucket_allocation_frac_for_bucket(cfg, "tier_3") == pytest.approx(0.30)


def test_risk_effective_max_bucket_regime_scales() -> None:
    cfg: dict = {
        "risk": {"max_bucket_allocation_pct": 0.30},
        "adaptive": {
            "bucket_cap_multiplier": {"neutral": 1.2, "bullish": 1.0, "bearish": 1.0}
        },
    }
    assert risk_effective_max_bucket_allocation_frac_for_bucket(
        cfg, "mega_cap_beta", regime_score=3
    ) == pytest.approx(0.36)
    assert risk_effective_max_bucket_allocation_frac_for_bucket(
        cfg, "mega_cap_beta", regime_condition="neutral", regime_score=4
    ) == pytest.approx(0.36)


def test_bucket_allocation_allows_uses_regime_effective_cap() -> None:
    se = sector_etf_symbol_frozenset({})
    cfg: dict = {
        "risk": {"max_bucket_allocation_pct": 0.30, "max_symbol_allocation_pct": 0.5},
        "adaptive": {
            "bucket_cap_multiplier": {
                "neutral": 1.2,
                "bullish": 1.0,
                "bearish": 1.0,
            }
        },
    }
    ok, _r = bucket_allocation_allows(
        positions=[{"symbol": "AAPL", "qty": 1, "market_value": 0.0}],
        equity=100_000.0,
        sym_upper="AAPL",
        proposed_notional=40_000.0,
        sector_etfs=se,
        config=cfg,
        regime_score=2,
    )
    assert not ok
    # 40% long MV+prop vs 0.30 * 1.2 = 36% cap (regime score 2–3 → neutral)
    assert _r is not None and "36.0%" in str(_r)

    ok2, r2 = bucket_allocation_allows(
        positions=[{"symbol": "AAPL", "qty": 1, "market_value": 0.0}],
        equity=100_000.0,
        sym_upper="AAPL",
        proposed_notional=35_000.0,
        sector_etfs=se,
        config=cfg,
        regime_score=2,
    )
    assert ok2, r2


def test_bucket_allocation_allows_top_signal_override() -> None:
    se = sector_etf_symbol_frozenset({})
    cfg: dict = {
        "risk": {"max_bucket_allocation_pct": 0.30, "max_symbol_allocation_pct": 0.5},
        "execution": {
            "allow_bucket_override_for_top_signals": True,
            "top_signal_percentile": 0.2,
        },
    }
    ok, _r = bucket_allocation_allows(
        positions=[{"symbol": "AAPL", "qty": 1, "market_value": 0.0}],
        equity=100_000.0,
        sym_upper="AAPL",
        proposed_notional=40_000.0,
        sector_etfs=se,
        config=cfg,
        entry_strength=0.9,
        strength_cohort=[0.5, 0.6, 0.7, 0.8, 0.9],
    )
    assert ok
    assert _r is None


def test_bucket_allocation_allows_strict_disables_top_signal_override() -> None:
    se = sector_etf_symbol_frozenset({})
    cfg: dict = {
        "risk": {"max_bucket_allocation_pct": 0.30, "max_symbol_allocation_pct": 0.5},
        "execution": {
            "allow_bucket_override_for_top_signals": True,
            "top_signal_percentile": 0.2,
        },
    }
    ok, reason = bucket_allocation_allows(
        positions=[{"symbol": "AAPL", "qty": 1, "market_value": 0.0}],
        equity=100_000.0,
        sym_upper="AAPL",
        proposed_notional=40_000.0,
        sector_etfs=se,
        config=cfg,
        entry_strength=0.9,
        strength_cohort=[0.5, 0.6, 0.7, 0.8, 0.9],
        allow_top_signal_bucket_override=False,
        allow_cross_bucket_rebalance_headroom=False,
    )
    assert ok is False
    assert reason is not None


def test_bucket_allocation_allows_cross_bucket_rebalance_borrows() -> None:
    se = sector_etf_symbol_frozenset({})
    cfg: dict = {
        "risk": {"max_bucket_allocation_pct": 0.30, "max_symbol_allocation_pct": 0.5, "mega_cap_beta_cap": 45},
        "risk_buckets": {
            "mega_cap_beta": [
                "MSFT",
                "AAPL",
                "SPY",
                "NVDA",
                "AMZN",
                "GOOGL",
                "META",
                "XLF",
            ],
        },
        "portfolio": {
            "allocator": {"allow_cross_bucket_rebalance": True},
        },
    }
    positions = [{"symbol": "CAT", "qty": 1, "market_value": 30_000.0}]
    ok, _r = bucket_allocation_allows(
        positions=positions,
        equity=100_000.0,
        sym_upper="CAT",
        proposed_notional=5_000.0,
        sector_etfs=se,
        config=cfg,
    )
    assert ok, _r


def test_bucket_allocation_allows_no_cross_without_flag() -> None:
    se = sector_etf_symbol_frozenset({})
    cfg: dict = {
        "risk": {"max_bucket_allocation_pct": 0.30, "max_symbol_allocation_pct": 0.5, "mega_cap_beta_cap": 45},
        "risk_buckets": {
            "mega_cap_beta": [
                "MSFT",
                "AAPL",
                "SPY",
                "NVDA",
                "AMZN",
                "GOOGL",
                "META",
                "XLF",
            ],
        },
        "portfolio": {
            "allocator": {"allow_cross_bucket_rebalance": False},
        },
    }
    positions = [{"symbol": "CAT", "qty": 1, "market_value": 30_000.0}]
    ok, _r = bucket_allocation_allows(
        positions=positions,
        equity=100_000.0,
        sym_upper="CAT",
        proposed_notional=5_000.0,
        sector_etfs=se,
        config=cfg,
    )
    assert not ok, _r


def test_other_sleeves_dollar_headroom_excludes_target() -> None:
    se = sector_etf_symbol_frozenset({})
    cfg: dict = {
        "risk": {"max_bucket_allocation_pct": 0.30, "mega_cap_beta_cap": 45},
        "risk_buckets": {"mega_cap_beta": ["SPY"]},
    }
    o = other_sleeves_dollar_headroom(
        cfg,
        [{"symbol": "CAT", "qty": 1, "market_value": 10_000.0}],
        se,
        100_000.0,
        "tier_3",
    )
    assert o > 40_000.0
    assert not allow_cross_bucket_rebalance({})


def test_bucket_allocation_mega_cap_beta_cap_loosens_vs_global() -> None:
    """``mega_cap_beta_cap`` can exceed ``max_bucket_allocation_pct`` for that bucket only."""
    se = sector_etf_symbol_frozenset({})
    cfg = {
        "risk": {"max_bucket_allocation_pct": 0.30, "mega_cap_beta_cap": 45},
        "risk_buckets": {"mega_cap_beta": ["NVDA", "MSFT"]},
    }
    ok, reason = bucket_allocation_allows(
        positions=[{"symbol": "NVDA", "qty": 1, "market_value": 40_000.0}],
        equity=100_000.0,
        sym_upper="MSFT",
        proposed_notional=4000.0,
        sector_etfs=se,
        config=cfg,
    )
    assert ok is True
    assert reason is None


def test_bucket_allocation_mega_cap_beta_cap_tightens() -> None:
    se = sector_etf_symbol_frozenset({})
    cfg = {
        "risk": {"max_bucket_allocation_pct": 0.50, "mega_cap_beta_cap": 20},
        "risk_buckets": {"mega_cap_beta": ["NVDA", "MSFT"]},
    }
    ok, reason = bucket_allocation_allows(
        positions=[{"symbol": "NVDA", "qty": 1, "market_value": 18_000.0}],
        equity=100_000.0,
        sym_upper="MSFT",
        proposed_notional=5000.0,
        sector_etfs=se,
        config=cfg,
    )
    assert ok is False
    assert reason is not None
    assert "cap 20.0%" in reason


def test_bucket_allocation_named_bucket_message() -> None:
    se = sector_etf_symbol_frozenset({})
    cfg = {
        "risk": {"max_bucket_allocation_pct": 0.30},
        "risk_buckets": {"mega_cap_beta": ["NVDA", "MSFT"]},
    }
    ok, reason = bucket_allocation_allows(
        positions=[{"symbol": "NVDA", "qty": 1, "market_value": 29000.0}],
        equity=100_000.0,
        sym_upper="MSFT",
        proposed_notional=5000.0,
        sector_etfs=se,
        config=cfg,
    )
    assert ok is False
    assert reason is not None
    assert "bucket mega_cap_beta" in reason
    assert ">=" in reason


def test_bucket_allocation_blocks() -> None:
    se = sector_etf_symbol_frozenset({})
    cfg = {"risk": {"max_bucket_allocation_pct": 0.30}}
    equity = 100_000.0
    positions = [{"symbol": "XLF", "qty": 1, "market_value": 29000.0}]
    ok, reason = bucket_allocation_allows(
        positions=positions,
        equity=equity,
        sym_upper="XLK",
        proposed_notional=5000.0,
        sector_etfs=se,
        config=cfg,
    )
    assert ok is False
    assert reason is not None
    assert "bucket tier_2" in reason
    assert ">=" in reason


def test_tracked_add_on_count_for_et_day() -> None:
    tr = {"AAPL": {"adds_et_date": "2026-04-14", "adds_et_date_count": 1}}
    assert tracked_add_on_count_for_et_day(tr, "AAPL", "2026-04-14") == 1
    assert tracked_add_on_count_for_et_day(tr, "AAPL", "2026-04-13") == 0


def test_add_on_daily_cap() -> None:
    tr = {"AAPL": {"adds_et_date": "2026-04-14", "adds_et_date_count": 1}}
    ok, msg = add_on_allowed_for_daily_cap(tr, "AAPL", "2026-04-14", 1)
    assert ok is False
    assert msg is not None
    assert "add-ons today" in msg


def test_add_on_min_minutes() -> None:
    now = datetime(2026, 4, 14, 16, 0, 0, tzinfo=timezone.utc)
    tr = {"AAPL": {"last_add_time": "2026-04-14T15:30:00+00:00"}}
    ok, msg = add_on_allowed_for_min_minutes(tr, "AAPL", now, 60.0)
    assert ok is False
    assert msg is not None
    assert "last add" in msg
    assert "cooldown" in msg


def test_risk_max_adds_parser() -> None:
    assert risk_max_adds_per_symbol_per_day({}) == 0
    assert risk_max_adds_per_symbol_per_day({"risk": {"max_adds_per_symbol_per_day": 2}}) == 2


def test_risk_max_addons_per_day_alias() -> None:
    assert risk_max_adds_per_symbol_per_day({"risk": {"max_addons_per_day": 3}}) == 3


def test_risk_max_adds_canonical_wins_over_addons_alias() -> None:
    cfg = {"risk": {"max_adds_per_symbol_per_day": 1, "max_addons_per_day": 9}}
    assert risk_max_adds_per_symbol_per_day(cfg) == 1


def test_risk_hold_rebalance_flags_default_off() -> None:
    assert risk_enforce_position_caps_on_hold({}) is False
    assert risk_rebalance_on_breach({}) is False
    assert risk_rebalance_threshold_pct({}) == pytest.approx(0.0)


def test_risk_hold_rebalance_flags_read_yaml_style() -> None:
    cfg = {
        "risk": {
            "enforce_position_caps_on_hold": True,
            "rebalance_on_breach": True,
            "rebalance_threshold_pct": 1.5,
        }
    }
    assert risk_enforce_position_caps_on_hold(cfg) is True
    assert risk_rebalance_on_breach(cfg) is True
    assert risk_rebalance_threshold_pct(cfg) == pytest.approx(1.5)


def test_symbol_allocation_breach_trim_shares_zero_when_no_breach() -> None:
    # 8% of 100k = 8k; 9% threshold line at 8+1=9% → 9k MV is not a breach
    assert (
        symbol_allocation_breach_trim_shares(
            equity=100_000.0,
            position_market_value_usd=9000.0,
            qty=100,
            mid_price=90.0,
            cap_pct=8.0,
            rebalance_threshold_pct=1.0,
        )
        == 0
    )


def test_symbol_allocation_breach_trim_shares_sells_excess() -> None:
    # 10.1% of 100k = 10.1k > 9% trigger; cap_usd 8k → excess 2.1k / 100 = 21 shares
    n = symbol_allocation_breach_trim_shares(
        equity=100_000.0,
        position_market_value_usd=10_100.0,
        qty=200,
        mid_price=100.0,
        cap_pct=8.0,
        rebalance_threshold_pct=1.0,
    )
    assert n == 21


def test_symbol_allocation_breach_trim_shares_caps_at_qty() -> None:
    n = symbol_allocation_breach_trim_shares(
        equity=10_000.0,
        position_market_value_usd=5000.0,
        qty=3,
        mid_price=100.0,
        cap_pct=8.0,
        rebalance_threshold_pct=0.0,
    )
    assert n == 3


def test_symbol_allocation_breach_trim_shares_off_cap() -> None:
    assert (
        symbol_allocation_breach_trim_shares(
            equity=100_000.0,
            position_market_value_usd=9000.0,
            qty=10,
            mid_price=100.0,
            cap_pct=0.0,
            rebalance_threshold_pct=1.0,
        )
        == 0
    )


def test_parse_risk_emergency_deleverage() -> None:
    cfg = {
        "risk": {
            "emergency_deleverage_trigger": 1.2,
            "emergency_deleverage_pct": 30,
            "bulk_trim_priority": ["highest_weight", "weakest_pnl"],
        }
    }
    d = parse_risk_emergency_deleverage(cfg)
    assert d["emergency_deleverage_trigger"] == pytest.approx(1.2)
    assert d["emergency_deleverage_pct"] == pytest.approx(0.30)
    assert d["bulk_trim_priority"] == ["highest_weight", "weakest_pnl"]


def test_parse_risk_emergency_cancel_all_open_orders() -> None:
    d0 = parse_risk_emergency_cancel_all_open_orders({})
    assert d0["enabled"] is False
    assert d0["gross_threshold"] == pytest.approx(1.2)
    d1 = parse_risk_emergency_cancel_all_open_orders(
        {
            "risk": {
                "emergency_cancel_all_open_orders": True,
                "emergency_cancel_all_open_orders_gross": 1.35,
            }
        }
    )
    assert d1["enabled"] is True
    assert d1["gross_threshold"] == pytest.approx(1.35)
    d2 = parse_risk_emergency_cancel_all_open_orders(
        {"risk": {"emergency_cancel_all_open_orders": "yes"}}
    )
    assert d2["enabled"] is True
