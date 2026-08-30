"""Tests for signal tier ranking (trend-long live loop)."""

from __future__ import annotations

import pytest

from src.signal_ranking import (
    SIGNAL_RANKING_MODE_COMPOSITE,
    SIGNAL_RANKING_MODE_MRV,
    SIGNAL_RANKING_MODE_SIGNAL_PRIORITY,
    SIGNAL_RANKING_MODE_STRENGTH,
    SIGNAL_RANKING_MODE_TIER,
    apply_recent_add_rank_penalty,
    canonical_signal_ranking_mode,
    max_signals_per_loop_from_portfolio,
    parse_composite_weights_from_portfolio,
    parse_recent_add_priority_cfg,
    pnl_potential_from_vol_and_volume,
    rank_trend_long_candidate_rows,
    row_composite_score,
    row_momentum_rs_volume_score,
    row_signal_priority_score,
    sector_etf_symbol_frozenset,
    symbol_signal_priority_tier,
    top_trend_long_candidates_by_composite_score,
)


def test_max_signals_per_loop_from_signal_ranking() -> None:
    cfg = {"portfolio": {"signal_ranking": {"max_signals_per_loop": 7}}}
    assert max_signals_per_loop_from_portfolio(cfg["portfolio"]) == 7


def test_max_signals_per_loop_from_rank_based_allocator_fallback() -> None:
    port = {"allocator": {"type": "rank_based", "top_n": 12}}
    assert max_signals_per_loop_from_portfolio(port) == 12


def test_max_signals_per_loop_from_rank_based_allocator_max_positions_only() -> None:
    port = {"allocator": {"type": "rank_based", "max_positions": 8}}
    assert max_signals_per_loop_from_portfolio(port) == 8


def test_max_signals_per_loop_rank_based_top_n_over_max_positions() -> None:
    port = {"allocator": {"type": "rank_based", "top_n": 5, "max_positions": 9}}
    assert max_signals_per_loop_from_portfolio(port) == 5


def test_max_signals_per_loop_ranked_type_alias() -> None:
    port = {"allocator": {"type": "ranked", "top_n": 6}}
    assert max_signals_per_loop_from_portfolio(port) == 6


def test_max_signals_per_loop_default_without_allocator() -> None:
    assert max_signals_per_loop_from_portfolio({}) == 3


def test_tier_spy_qqq_best() -> None:
    se = sector_etf_symbol_frozenset({})
    assert symbol_signal_priority_tier("SPY", se) == 0
    assert symbol_signal_priority_tier("QQQ", se) == 0


def test_tier_nvda_msft() -> None:
    se = sector_etf_symbol_frozenset({})
    assert symbol_signal_priority_tier("NVDA", se) == 1
    assert symbol_signal_priority_tier("MSFT", se) == 1


def test_tier_sector_etf() -> None:
    se = sector_etf_symbol_frozenset({})
    assert symbol_signal_priority_tier("XLK", se) == 2


def test_tier_other() -> None:
    se = sector_etf_symbol_frozenset({})
    assert symbol_signal_priority_tier("AAPL", se) == 3


def test_sector_etf_symbol_frozenset_merges_yaml() -> None:
    cfg = {"portfolio": {"signal_ranking": {"sector_etf_symbols": ["XYZ1"]}}}
    s = sector_etf_symbol_frozenset(cfg)
    assert "XLK" in s
    assert "XYZ1" in s


def test_rank_trend_long_candidate_rows_tier_then_strength() -> None:
    se = sector_etf_symbol_frozenset({})
    rows = [
        {"sym_u": "AAPL", "strength_eff": 2.0},
        {"sym_u": "NVDA", "strength_eff": 0.1},
        {"sym_u": "SPY", "strength_eff": 0.05},
    ]
    chosen, dropped = rank_trend_long_candidate_rows(rows, max_take=2, sector_etfs=se)
    assert [r["sym_u"] for r in chosen] == ["SPY", "NVDA"]
    assert set(dropped) == {"AAPL"}


def test_pnl_potential_is_avg_of_vol_quality_and_volume() -> None:
    assert pnl_potential_from_vol_and_volume(volatility_quality=1.0, volume=0.0) == pytest.approx(0.5)
    assert pnl_potential_from_vol_and_volume(volatility_quality=0.0, volume=1.0) == pytest.approx(0.5)
    assert pnl_potential_from_vol_and_volume(volatility_quality=1.0, volume=1.0) == pytest.approx(1.0)


def test_row_composite_score_prefers_field_then_breakdown() -> None:
    assert row_composite_score({"composite_score": 3.5}) == pytest.approx(3.5)
    rb = {"trend_strength": 1.0, "momentum": 0.5, "pnl_potential": 0.5}
    assert row_composite_score({"rank_breakdown": rb}) == pytest.approx(2.0)


def test_row_composite_score_four_pillar_from_breakdown() -> None:
    rb = {
        "trend_strength": 1.0,
        "momentum": 0.0,
        "volatility_expansion": 0.0,
        "relative_strength": 0.0,
    }
    # Default weights: 0.35 * 1.0 = 0.35 → *3 = 1.05
    assert row_composite_score({"rank_breakdown": rb}) == pytest.approx(1.05)


def test_parse_composite_weights_renormalizes() -> None:
    c = parse_composite_weights_from_portfolio(
        {
            "signal_ranking": {
                "composite_weights": {
                    "trend_strength": 1.0,
                    "momentum": 1.0,
                    "volatility_expansion": 0.0,
                    "relative_strength": 0.0,
                }
            }
        }
    )
    assert c["trend_strength"] == pytest.approx(0.5)
    assert c["momentum"] == pytest.approx(0.5)


def test_rank_signal_strength_mode_ignores_tier() -> None:
    se = sector_etf_symbol_frozenset({})
    rows = [
        {"sym_u": "SPY", "strength_eff": 0.1},
        {"sym_u": "AAPL", "strength_eff": 0.9},
    ]
    chosen, dropped = rank_trend_long_candidate_rows(
        rows, max_take=1, sector_etfs=se, ranking_mode=SIGNAL_RANKING_MODE_STRENGTH
    )
    assert [r["sym_u"] for r in chosen] == ["AAPL"]
    assert set(dropped) == {"SPY"}


def test_rank_composite_score_sorts_by_score_not_tier() -> None:
    se = sector_etf_symbol_frozenset({})
    rows = [
        {"sym_u": "SPY", "composite_score": 1.0, "strength_eff": 0.9},
        {"sym_u": "AAPL", "composite_score": 3.8, "strength_eff": 0.2},
        {"sym_u": "NVDA", "composite_score": 2.0, "strength_eff": 0.5},
    ]
    chosen, dropped = rank_trend_long_candidate_rows(
        rows, max_take=2, sector_etfs=se, ranking_mode=SIGNAL_RANKING_MODE_COMPOSITE
    )
    assert [r["sym_u"] for r in chosen] == ["AAPL", "NVDA"]
    assert set(dropped) == {"SPY"}


def test_top_trend_long_candidates_by_composite_score_alias() -> None:
    se = sector_etf_symbol_frozenset({})
    rows = [{"sym_u": "X", "composite_score": 2.0}, {"sym_u": "Y", "composite_score": 1.0}]
    chosen, dropped = top_trend_long_candidates_by_composite_score(rows, max_take=1, sector_etfs=se)
    assert [r["sym_u"] for r in chosen] == ["X"]
    assert dropped == ["Y"]


def test_parse_recent_add_priority_defaults() -> None:
    d = parse_recent_add_priority_cfg({})
    assert d["enabled"] is False


def test_parse_recent_add_priority_reads_yaml() -> None:
    cfg = {
        "portfolio": {
            "signal_ranking": {
                "recent_add_priority": {
                    "enabled": True,
                    "recent_minutes": 120,
                    "strength_eff_multiplier": 0.5,
                    "composite_score_multiplier": 0.6,
                    "extra_priority_tier": 2,
                }
            }
        }
    }
    d = parse_recent_add_priority_cfg(cfg["portfolio"])
    assert d["enabled"] is True
    assert d["recent_minutes"] == pytest.approx(120)
    assert d["strength_eff_multiplier"] == pytest.approx(0.5)
    assert d["composite_score_multiplier"] == pytest.approx(0.6)
    assert d["extra_priority_tier"] == 2


def test_apply_recent_add_rank_penalty_scales_and_bumps_tier() -> None:
    row = {"sym_u": "AAA", "tier": 1, "strength_eff": 1.0, "composite_score": 4.0}
    apply_recent_add_rank_penalty(
        row,
        is_recent_add=True,
        strength_eff_multiplier=0.5,
        composite_score_multiplier=0.25,
        extra_priority_tier=1,
    )
    assert row["strength_eff"] == pytest.approx(0.5)
    assert row["composite_score"] == pytest.approx(1.0)
    assert row["tier"] == 2
    assert row["recent_add_penalized"] is True


def test_same_tier_penalized_row_loses_rank_slot() -> None:
    """Simulate live loop: non-recent keeps keys; penalized row matches weaker competitor."""
    se = sector_etf_symbol_frozenset({})
    winner = {"sym_u": "AAA", "tier": 3, "strength_eff": 0.85, "composite_score": 3.2}
    loser = {"sym_u": "BBB", "tier": 3, "strength_eff": 0.90, "composite_score": 3.4}
    apply_recent_add_rank_penalty(
        loser,
        is_recent_add=True,
        strength_eff_multiplier=0.5,
        composite_score_multiplier=0.5,
        extra_priority_tier=0,
    )
    chosen, _ = rank_trend_long_candidate_rows(
        [winner, loser], max_take=1, sector_etfs=se, ranking_mode=SIGNAL_RANKING_MODE_TIER
    )
    assert chosen[0]["sym_u"] == "AAA"
    chosen_c, _ = rank_trend_long_candidate_rows(
        [winner, loser], max_take=1, sector_etfs=se, ranking_mode=SIGNAL_RANKING_MODE_COMPOSITE
    )
    assert chosen_c[0]["sym_u"] == "AAA"


def test_row_signal_priority_score_four_pillar_sum() -> None:
    rb = {
        "trend_strength": 1.0,
        "momentum": 0.5,
        "volatility_expansion": 0.25,
        "relative_strength": 0.25,
    }
    assert row_signal_priority_score({"priority_score": 9.9}) == pytest.approx(9.9)
    assert row_signal_priority_score({"rank_breakdown": rb}) == pytest.approx(2.0)


def test_rank_signal_priority_sorts_by_unweighted_sum() -> None:
    se = sector_etf_symbol_frozenset({})
    rows = [
        {
            "sym_u": "SPY",
            "rank_breakdown": {
                "trend_strength": 1.0,
                "momentum": 0.0,
                "volatility_expansion": 0.0,
                "relative_strength": 0.0,
            },
        },
        {
            "sym_u": "AAPL",
            "rank_breakdown": {
                "trend_strength": 0.5,
                "momentum": 0.5,
                "volatility_expansion": 0.5,
                "relative_strength": 0.5,
            },
        },
    ]
    chosen, dropped = rank_trend_long_candidate_rows(
        rows,
        max_take=1,
        sector_etfs=se,
        ranking_mode=SIGNAL_RANKING_MODE_SIGNAL_PRIORITY,
    )
    assert [r["sym_u"] for r in chosen] == ["AAPL"]
    assert dropped == ["SPY"]


def test_canonical_signal_ranking_mode() -> None:
    assert canonical_signal_ranking_mode("signal_priority") == SIGNAL_RANKING_MODE_SIGNAL_PRIORITY
    assert canonical_signal_ranking_mode("priority") == SIGNAL_RANKING_MODE_SIGNAL_PRIORITY
    assert canonical_signal_ranking_mode(
        "tier_then_strength", allocation_rank_by_strength=True
    ) == SIGNAL_RANKING_MODE_STRENGTH
    assert (
        canonical_signal_ranking_mode(
            "tier_then_strength",
            allocation_rank_by_strength=True,
            allocation_rank_top_k_by="momentum_rs_volume",
        )
        == SIGNAL_RANKING_MODE_MRV
    )


def test_rank_mrv_keeps_best_momentum_rs_volume_not_tier_bias() -> None:
    """Top-K by MRV: SPY can lose to names with stronger momentum+RS+volume."""
    se = sector_etf_symbol_frozenset(
        {"portfolio": {"signal_ranking": {"sector_etf_symbols": ["SMH", "SOXX"]}}}
    )
    rows = [
        {
            "sym_u": "IWM",
            "strength_eff": 0.55,
            "rank_breakdown": {
                "momentum": 0.9,
                "relative_strength": 0.85,
                "volume_signal": 0.8,
            },
        },
        {
            "sym_u": "SPY",
            "strength_eff": 0.92,
            "rank_breakdown": {
                "momentum": 0.35,
                "relative_strength": 0.3,
                "volume_signal": 0.25,
            },
        },
    ]
    chosen, dropped = rank_trend_long_candidate_rows(
        rows,
        max_take=1,
        sector_etfs=se,
        ranking_mode=SIGNAL_RANKING_MODE_MRV,
    )
    assert [r["sym_u"] for r in chosen] == ["IWM"]
    assert dropped == ["SPY"]
    assert row_momentum_rs_volume_score(rows[0]) == pytest.approx(2.55)


def test_row_momentum_rs_volume_score_explicit() -> None:
    assert row_momentum_rs_volume_score({"momentum_rs_volume_score": 2.2}) == pytest.approx(2.2)
