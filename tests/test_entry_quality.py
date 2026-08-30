from __future__ import annotations

import pandas as pd
import pytest

from src.entry_quality import (
    adaptive_sleeve_size_multiplier,
    aggressive_dynamic_cooldown_minutes,
    compute_aggressive_dynamic_entry_score,
    evaluate_entry_quality,
    no_chase_ok,
    price_above_session_vwap,
    relative_strength_rank,
    sector_confirmation_symbol,
    strategy_quality_points,
    trend_long_quality_score,
)
from src.dynamic_universe import adaptive_dynamic_reentry_cooldown_minutes


def _df(last: float = 105.0) -> pd.DataFrame:
    closes = [100 + i * 0.1 for i in range(60)]
    closes[-1] = last
    return pd.DataFrame(
        {
            "open": closes,
            "high": [value + 0.3 for value in closes],
            "low": [value - 0.3 for value in closes],
            "close": closes,
            "volume": [1000 + i for i in range(60)],
        }
    )


def test_trend_long_quality_score_improves_with_confirmations() -> None:
    df = _df()

    weak = trend_long_quality_score(
        df,
        atr_pct=1.0,
        max_atr_pct=8.0,
        market_vwap_confirmed=False,
        symbol_vwap_confirmed=False,
        sector_confirmed=False,
        no_chase_passed=False,
    )
    strong = trend_long_quality_score(
        df,
        atr_pct=1.0,
        max_atr_pct=8.0,
        market_vwap_confirmed=True,
        symbol_vwap_confirmed=True,
        sector_confirmed=True,
        no_chase_passed=True,
    )

    assert strong > weak
    assert 0.0 <= weak <= 1.0
    assert 0.0 <= strong <= 1.0


def test_vwap_and_no_chase_rules() -> None:
    bars = pd.DataFrame(
        {
            "high": [99.2, 100.2, 101.2],
            "low": [98.8, 99.8, 100.8],
            "close": [99.0, 100.0, 101.0],
            "volume": [100, 100, 100],
        }
    )

    assert price_above_session_vwap(bars, price=101.0) is True
    assert price_above_session_vwap(bars, price=99.0) is False
    assert no_chase_ok(price=104, vwap=100, atr=2, max_vwap_distance_pct=2.0)[0] is False
    assert no_chase_ok(price=101, vwap=100, atr=0.5, max_atr_extension_pct=100.0)[0] is False


def test_sector_confirmation_mapping() -> None:
    assert sector_confirmation_symbol("MSFT") == "XLK"
    assert sector_confirmation_symbol("XLF") == "XLF"


def test_evaluate_entry_quality_blocks_regime_lte_2() -> None:
    decision = evaluate_entry_quality(
        route="trend_long",
        symbol="MSFT",
        df=_df(),
        price=101.0,
        symbol_vwap=100.0,
        market_vwap_confirmed=True,
        qqq_above_vwap=True,
        trend_5m_positive=False,
        trend_15m_positive=False,
        sector_confirmed=True,
        regime_score=2,
    )

    assert decision.allowed is False
    assert decision.reason == "regime_lte_2"
    assert decision.quality_score < 8


def test_evaluate_entry_quality_requires_market_symbol_and_sector_vwap() -> None:
    base = dict(
        route="trend_long",
        symbol="MSFT",
        df=_df(),
        price=101.0,
        symbol_vwap=100.0,
        regime_score=4,
    )

    assert (
        evaluate_entry_quality(**base, market_vwap_confirmed=False, sector_confirmed=True).reason
        == "market_vwap_not_confirmed"
    )
    below_vwap = {**base, "price": 99.0}
    assert (
        evaluate_entry_quality(**below_vwap, market_vwap_confirmed=True, sector_confirmed=True).reason
        == "symbol_vwap_not_confirmed"
    )
    assert (
        evaluate_entry_quality(**base, market_vwap_confirmed=True, sector_confirmed=False).reason
        == "sector_not_confirmed"
    )


def test_adaptive_market_vwap_penalty_allows_strong_dynamic_candidate() -> None:
    decision = evaluate_entry_quality(
        route="dynamic_momentum_override",
        symbol="LCID",
        df=_df(),
        price=101.0,
        symbol_vwap=100.0,
        market_vwap_confirmed=False,
        qqq_above_vwap=False,
        trend_5m_positive=True,
        trend_15m_positive=True,
        sector_confirmed=True,
        regime_score=4,
        relative_volume=3.5,
        spread_pct=0.05,
        has_strong_catalyst=False,
        news_score=3,
        catalyst_score=0.30,
        event_score=1,
        momentum_confirmed=True,
        config={
            "entry_quality": {
                "enabled": True,
                "scoring_enabled": True,
                "threshold": 75,
                "market_vwap_penalty": 8,
                "dynamic_no_catalyst_min_quality_score": 75,
            }
        },
    )

    assert decision.allowed is True
    assert decision.reason == "quality_score_passed_with_market_vwap_penalty"
    assert decision.entry_quality_score == pytest.approx(90.0)
    assert decision.zero_score_factors == ("market_vwap",)
    assert decision.features["market_vwap_state"] == "deteriorating"


def test_weighted_entry_score_rejects_below_threshold() -> None:
    decision = evaluate_entry_quality(
        route="dynamic_momentum_override",
        symbol="PYPL",
        df=_df(),
        price=101.0,
        symbol_vwap=100.0,
        market_vwap_confirmed=False,
        trend_5m_positive=False,
        trend_15m_positive=False,
        sector_confirmed=True,
        regime_score=4,
        relative_volume=0.5,
        spread_pct=0.05,
        news_score=1,
        catalyst_score=0.0,
        event_score=0,
        momentum_confirmed=False,
        config={"entry_quality": {"enabled": True, "scoring_enabled": True, "threshold": 75}},
    )

    assert decision.allowed is False
    assert decision.reason in {"market_vwap_not_confirmed", "quality_score_below_min"}
    assert decision.adaptive_scoring_used is True
    assert decision.quality_score < 75


def test_strong_catalyst_override_allows_one_noncritical_failure() -> None:
    decision = evaluate_entry_quality(
        route="dynamic_momentum_override",
        symbol="PYPL",
        df=_df(),
        price=101.0,
        symbol_vwap=100.0,
        market_vwap_confirmed=True,
        trend_5m_positive=True,
        trend_15m_positive=True,
        sector_confirmed=False,
        regime_score=4,
        relative_volume=3.0,
        spread_pct=0.05,
        news_score=5,
        catalyst_score=0.65,
        event_score=3,
        momentum_confirmed=True,
        config={"entry_quality": {"enabled": True, "scoring_enabled": True, "threshold": 75, "strong_news_override": True}},
    )

    assert decision.allowed is True
    assert decision.reason == "strong_catalyst_override"
    assert any("strong_catalyst_override=sector_not_confirmed" == item for item in decision.entry_quality_penalties)


def test_strong_catalyst_override_never_bypasses_spread() -> None:
    decision = evaluate_entry_quality(
        route="dynamic_momentum_override",
        symbol="PYPL",
        df=_df(),
        price=101.0,
        symbol_vwap=100.0,
        market_vwap_confirmed=True,
        trend_5m_positive=True,
        trend_15m_positive=True,
        sector_confirmed=True,
        regime_score=4,
        relative_volume=3.0,
        spread_pct=1.0,
        news_score=5,
        catalyst_score=0.65,
        event_score=3,
        momentum_confirmed=True,
        config={"entry_quality": {"enabled": True, "scoring_enabled": True, "threshold": 75, "strong_news_override": True}},
    )

    assert decision.allowed is False
    assert "wide_spread" in decision.rejected_rules


def test_weighted_entry_quality_full_score() -> None:
    decision = evaluate_entry_quality(
        route="trend_long",
        symbol="SPY",
        df=_df(),
        price=101.0,
        symbol_vwap=100.0,
        market_vwap_confirmed=True,
        qqq_above_vwap=True,
        trend_5m_positive=True,
        trend_15m_positive=True,
        pullback_confirmed=True,
        volume_confirmed=True,
        sector_confirmed=True,
        regime_score=4,
        spread_pct=0.05,
        config={"entry_quality": {"enabled": True, "scoring_enabled": True}},
    )

    assert decision.allowed is True
    assert decision.quality_score == pytest.approx(100.0)
    assert decision.score_threshold == pytest.approx(80.0)
    assert decision.sizing_multiplier == pytest.approx(1.0)
    assert decision.score_components["trend"] == {"score": 20.0, "max": 20.0}


def test_partial_market_vwap_scoring() -> None:
    decision = evaluate_entry_quality(
        route="trend_long",
        symbol="IWM",
        df=_df(),
        price=101.0,
        symbol_vwap=100.0,
        market_vwap_confirmed=False,
        qqq_above_vwap=False,
        trend_5m_positive=True,
        trend_15m_positive=True,
        pullback_confirmed=True,
        volume_confirmed=True,
        sector_confirmed=True,
        regime_score=4,
        spread_pct=0.05,
        market_vwap_distance_pct=-0.10,
        market_vwap_slope=0.05,
        market_vwap_data_available=True,
        config={"entry_quality": {"enabled": True, "scoring_enabled": True}},
    )

    assert decision.allowed is True
    assert decision.market_vwap_state == "recovering"
    assert decision.market_vwap_score == pytest.approx(5.0)
    assert decision.quality_score == pytest.approx(95.0)


def test_market_vwap_unavailable_is_data_quality_state() -> None:
    decision = evaluate_entry_quality(
        route="trend_long",
        symbol="AAPL",
        df=_df(),
        price=101.0,
        symbol_vwap=100.0,
        market_vwap_confirmed=False,
        qqq_above_vwap=False,
        trend_5m_positive=True,
        trend_15m_positive=True,
        pullback_confirmed=True,
        volume_confirmed=True,
        sector_confirmed=True,
        regime_score=4,
        spread_pct=0.05,
        market_vwap_data_available=False,
        config={"entry_quality": {"enabled": True, "scoring_enabled": True}},
    )

    assert decision.allowed is True
    assert decision.market_vwap_state == "unavailable"
    assert decision.market_vwap_data_available is False
    assert decision.zero_score_factors == ("market_vwap",)
    assert decision.features["market_vwap_state"] == "unavailable"
    assert decision.features["market_vwap_data_available"] is False


def test_dynamic_momentum_market_vwap_unavailable_applies_policy_without_exception() -> None:
    decision = evaluate_entry_quality(
        route="dynamic_momentum_override",
        symbol="PYPL",
        df=_df(),
        price=101.0,
        symbol_vwap=100.0,
        market_vwap_confirmed=False,
        qqq_above_vwap=False,
        trend_5m_positive=True,
        trend_15m_positive=True,
        pullback_confirmed=True,
        volume_confirmed=True,
        sector_confirmed=True,
        regime_score=4,
        spread_pct=0.05,
        market_vwap_data_available=False,
        news_score=4,
        catalyst_score=0.3,
        event_score=1,
        momentum_confirmed=True,
        config={
            "entry_quality": {
                "enabled": True,
                "scoring_enabled": True,
                "dynamic_no_catalyst_min_quality_score": 80,
                "unavailable_feature_policy": "conservative",
            }
        },
    )

    assert decision.allowed is True
    assert decision.market_vwap_state == "unavailable"
    assert decision.market_vwap_data_available is False
    assert decision.quality_score == pytest.approx(90.0)


def test_score_exactly_at_threshold_passes_and_uses_size_band() -> None:
    decision = evaluate_entry_quality(
        route="trend_long",
        symbol="XLF",
        df=_df(),
        price=101.0,
        symbol_vwap=100.0,
        market_vwap_confirmed=False,
        qqq_above_vwap=False,
        trend_5m_positive=True,
        trend_15m_positive=True,
        pullback_confirmed=True,
        volume_confirmed=True,
        sector_confirmed=True,
        regime_score=4,
        spread_pct=0.05,
        config={
            "entry_quality": {
                "enabled": True,
                "scoring_enabled": True,
                "threshold": {"strong_regime": 90, "default": 90},
            }
        },
    )

    assert decision.allowed is True
    assert decision.quality_score == pytest.approx(90.0)
    assert decision.sizing_multiplier == pytest.approx(1.0)


def test_regime_specific_threshold_and_reduced_sizing() -> None:
    decision = evaluate_entry_quality(
        route="trend_long",
        symbol="SPY",
        df=_df(),
        price=101.0,
        symbol_vwap=100.0,
        market_vwap_confirmed=False,
        qqq_above_vwap=False,
        trend_5m_positive=True,
        trend_15m_positive=True,
        pullback_confirmed=True,
        volume_confirmed=True,
        sector_confirmed=True,
        regime_score=3,
        spread_pct=0.05,
        config={
            "entry_quality": {
                "enabled": True,
                "scoring_enabled": True,
                "threshold": {"neutral_regime": 80, "default": 80},
            }
        },
    )

    assert decision.allowed is True
    assert decision.score_threshold == pytest.approx(80.0)
    assert decision.quality_score == pytest.approx(90.0)
    assert decision.sizing_multiplier == pytest.approx(1.0)


def test_more_than_one_zero_noncritical_factor_rejects() -> None:
    decision = evaluate_entry_quality(
        route="trend_long",
        symbol="SPY",
        df=_df(),
        price=101.0,
        symbol_vwap=100.0,
        market_vwap_confirmed=False,
        qqq_above_vwap=False,
        trend_5m_positive=True,
        trend_15m_positive=True,
        pullback_confirmed=True,
        volume_confirmed=False,
        sector_confirmed=True,
        regime_score=4,
        spread_pct=0.05,
        config={"entry_quality": {"enabled": True, "scoring_enabled": True}},
    )

    assert decision.allowed is False
    assert decision.reason == "quality_score_below_min"
    assert set(decision.zero_score_factors) >= {"volume", "market_vwap"}


def test_july_16_market_vwap_only_trend_long_regression() -> None:
    decision = evaluate_entry_quality(
        route="trend_long",
        symbol="SPY",
        df=_df(),
        price=101.0,
        symbol_vwap=100.0,
        market_vwap_confirmed=False,
        qqq_above_vwap=False,
        trend_5m_positive=True,
        trend_15m_positive=True,
        pullback_confirmed=True,
        volume_confirmed=True,
        sector_confirmed=True,
        regime_score=4,
        spread_pct=0.05,
        config={"entry_quality": {"enabled": True, "scoring_enabled": True}},
    )

    assert decision.allowed is True
    assert decision.reason == "quality_score_passed_with_market_vwap_penalty"
    assert decision.quality_score == pytest.approx(90.0)
    assert decision.sizing_multiplier == pytest.approx(1.0)
    assert decision.market_vwap_score == pytest.approx(0.0)
    assert decision.score_components["market_vwap"] == {"score": 0.0, "max": 10.0}


def test_scoring_disabled_preserves_binary_market_vwap_gate() -> None:
    decision = evaluate_entry_quality(
        route="trend_long",
        symbol="SPY",
        df=_df(),
        price=101.0,
        symbol_vwap=100.0,
        market_vwap_confirmed=False,
        qqq_above_vwap=False,
        trend_5m_positive=True,
        trend_15m_positive=True,
        pullback_confirmed=True,
        volume_confirmed=True,
        sector_confirmed=True,
        regime_score=4,
        spread_pct=0.05,
        config={"entry_quality": {"enabled": True, "scoring_enabled": False}},
    )

    assert decision.allowed is False
    assert decision.reason == "market_vwap_not_confirmed"


def test_dynamic_cooldown_varies_by_exit_quality() -> None:
    cfg = {"entry_quality": {"dynamic_cooldown": True, "dynamic_cooldown_minutes": {"loss_min": 60, "loss_max": 90, "small_winner": 30, "large_winner": 15, "fresh_catalyst_reduction": 15, "fresh_catalyst_window_minutes": 120}}}

    assert adaptive_dynamic_reentry_cooldown_minutes(pnl_pct=-2.0, config=cfg) == (90, "loss_exit")
    assert adaptive_dynamic_reentry_cooldown_minutes(pnl_pct=-0.2, config=cfg) == (60, "loss_exit")
    assert adaptive_dynamic_reentry_cooldown_minutes(pnl_pct=0.2, config=cfg) == (30, "small_winner")
    assert adaptive_dynamic_reentry_cooldown_minutes(pnl_pct=1.2, config=cfg) == (15, "large_winner")
    assert adaptive_dynamic_reentry_cooldown_minutes(pnl_pct=0.2, news_score=4, catalyst_age_minutes=30, config=cfg) == (15, "small_winner_fresh_catalyst")


def test_evaluate_entry_quality_starter_and_confirmation_sizing() -> None:
    cfg = {"entry_quality": {"live_min_quality_score": 6, "confirmation_quality_score": 13, "starter_size_fraction": 0.25}}
    starter = evaluate_entry_quality(
        route="trend_long",
        symbol="MSFT",
        df=_df(),
        price=101.0,
        symbol_vwap=100.0,
        market_vwap_confirmed=True,
        qqq_above_vwap=True,
        sector_confirmed=True,
        regime_score=4,
        config=cfg,
    )
    confirmed = evaluate_entry_quality(
        route="trend_long",
        symbol="MSFT",
        df=_df(),
        price=101.0,
        symbol_vwap=100.0,
        market_vwap_confirmed=True,
        qqq_above_vwap=True,
        sector_confirmed=True,
        regime_score=4,
        config={"entry_quality": {"live_min_quality_score": 6, "confirmation_quality_score": 6}},
    )

    assert starter.allowed is True
    assert starter.starter is True
    assert starter.sizing_multiplier == pytest.approx(0.25)
    assert confirmed.allowed is True
    assert confirmed.starter is False
    assert confirmed.sizing_multiplier == pytest.approx(1.0)


def test_strategy_quality_points_uses_required_weights() -> None:
    score = strategy_quality_points(
        symbol_above_vwap=True,
        spy_above_vwap=True,
        qqq_above_vwap=True,
        trend_5m_positive=True,
        trend_15m_positive=True,
        sector_confirmed=True,
        relative_volume_ok=True,
        spread_tight=True,
        regime_score=4,
        extended_above_vwap=False,
        wide_spread=False,
    )

    assert score == 12


def test_no_chase_blocks_vwap_and_atr_extension() -> None:
    decision = evaluate_entry_quality(
        route="trend_long",
        symbol="MSFT",
        df=_df(),
        price=103.0,
        symbol_vwap=100.0,
        market_vwap_confirmed=True,
        qqq_above_vwap=True,
        sector_confirmed=True,
        regime_score=4,
        atr=1.0,
    )

    assert decision.allowed is False
    assert decision.reason in {"vwap_distance_chase", "atr_extension_chase"}


def test_sector_confirmation_missing_blocks_weak_regime_and_sizes_otherwise() -> None:
    weak = evaluate_entry_quality(
        route="trend_long",
        symbol="MSFT",
        df=_df(),
        price=101.0,
        symbol_vwap=100.0,
        market_vwap_confirmed=True,
        qqq_above_vwap=True,
        sector_confirmed=False,
        regime_score=2,
    )

    assert weak.allowed is False
    assert "sector_not_confirmed" in weak.rejected_rules


def test_dynamic_no_catalyst_requires_stronger_quality_and_starter_size() -> None:
    decision = evaluate_entry_quality(
        route="dynamic_momentum_override",
        symbol="FHTX",
        df=_df(),
        price=101.0,
        symbol_vwap=100.0,
        market_vwap_confirmed=True,
        qqq_above_vwap=True,
        sector_confirmed=True,
        regime_score=4,
        has_strong_catalyst=False,
        config={"entry_quality": {"dynamic_no_catalyst_min_quality_score": 7, "starter_size_fraction": 0.25}},
    )

    assert decision.allowed is True
    assert decision.starter is True
    assert decision.sizing_multiplier == pytest.approx(0.25)


def test_adaptive_sleeve_sizing_blocks_after_three_weak_exits() -> None:
    exits = [
        {"entry_route": "trend_long", "pnl": -1.0},
        {"entry_route": "trend_long", "exit_reason": "signal_flip"},
        {"entry_route": "trend_long", "pnl_pct": -0.2},
    ]

    mult, blocked, count = adaptive_sleeve_size_multiplier(exits, sleeve="trend_long")

    assert mult == 0.0
    assert blocked is True
    assert count == 3


def test_relative_strength_top_n_selection() -> None:
    ranked = relative_strength_rank(
        [
            {"symbol": "A", "day_gain_pct": 1, "relative_volume": 1, "catalyst_score": 1},
            {"symbol": "B", "day_gain_pct": 3, "relative_volume": 4, "vwap_above": True},
            {"symbol": "C", "day_gain_pct": 2, "relative_volume": 2},
        ],
        top_n=2,
    )

    assert [row["symbol"] for row in ranked] == ["B", "C"]


def _aggressive_cfg(**overrides):
    base = {
        "dynamic_entry": {
            "aggressive_mode": {
                "enabled": True,
                "normal_threshold": 60,
                "fast_lane_threshold": 50,
                "max_noncritical_failures": 3,
                "minimum_price": 2.0,
                "size_by_score": {80: 1.0, 70: 0.65, 60: 0.40, 55: 0.25, 50: 0.15},
                "price_tier_size": {"above_20": 1.0, "five_to_20": 0.75, "two_to_5": 0.35},
                "fast_lane": {"news_score": 3, "catalyst_score": 0.25, "event_score": 2.0, "gap_or_gain_pct": 8.0},
            }
        }
    }
    base["dynamic_entry"]["aggressive_mode"].update(overrides)
    return base


def test_aggressive_mode_disabled_preserves_current_dynamic_behavior() -> None:
    decision = evaluate_entry_quality(
        route="dynamic_momentum_override",
        symbol="LCID",
        df=_df(),
        price=101.0,
        symbol_vwap=100.0,
        market_vwap_confirmed=False,
        sector_confirmed=False,
        regime_score=4,
        trend_5m_positive=True,
        momentum_confirmed=True,
        relative_volume=2.5,
        day_gain_pct=5.0,
        spread_pct=0.05,
        config={"dynamic_entry": {"aggressive_mode": {"enabled": False}}, "entry_quality": {"scoring_enabled": False}},
    )

    assert decision.allowed is False
    assert decision.reason == "market_vwap_not_confirmed"


def test_aggressive_dynamic_market_and_sector_failures_do_not_block() -> None:
    decision = evaluate_entry_quality(
        route="dynamic_momentum_override",
        symbol="LCID",
        df=_df(),
        price=101.0,
        symbol_vwap=100.0,
        market_vwap_confirmed=False,
        market_vwap_data_available=False,
        sector_confirmed=False,
        regime_score=4,
        trend_5m_positive=True,
        momentum_confirmed=True,
        relative_volume=3.0,
        day_gain_pct=5.0,
        news_score=3,
        catalyst_score=0.25,
        spread_pct=0.05,
        config=_aggressive_cfg(),
    )

    assert decision.allowed is True
    assert decision.reason == "aggressive_dynamic_fast_lane"
    assert decision.features["aggressive_fast_lane"] is True
    assert decision.features["fast_lane_trigger"] == "news_score"
    assert decision.sizing_multiplier > 0


def test_aggressive_dynamic_four_noncritical_failures_rejects() -> None:
    result = compute_aggressive_dynamic_entry_score(
        config=_aggressive_cfg(),
        price=12,
        news_score=3,
        catalyst_score=0,
        event_score=0,
        relative_volume=0.1,
        gain_pct=0.1,
        trend_confirmed=False,
        momentum_confirmed=False,
        breakout_confirmed=False,
        symbol_vwap_confirmed=False,
        market_vwap_state="deteriorating",
        sector_confirmed=False,
        regime_score=4,
    )

    assert result["allowed"] is False
    assert result["reason"] == "too_many_noncritical_failures"


def test_aggressive_dynamic_starter_entry_at_score_50() -> None:
    result = compute_aggressive_dynamic_entry_score(
        config=_aggressive_cfg(),
        price=25,
        news_score=3,
        catalyst_score=0.25,
        event_score=0,
        relative_volume=1.5,
        gain_pct=2.0,
        trend_confirmed=None,
        momentum_confirmed=True,
        breakout_confirmed=False,
        symbol_vwap_confirmed=False,
        market_vwap_state="unavailable",
        sector_confirmed=False,
        regime_score=4,
    )

    assert result["allowed"] is True
    assert result["score"] >= 50
    assert result["size_multiplier"] == pytest.approx(0.15)


def test_aggressive_dynamic_low_price_tier_and_sub_two_reject() -> None:
    low = compute_aggressive_dynamic_entry_score(
        config=_aggressive_cfg(),
        price=3.5,
        news_score=5,
        relative_volume=5,
        gain_pct=10,
        trend_confirmed=True,
        momentum_confirmed=True,
        breakout_confirmed=True,
        symbol_vwap_confirmed=True,
        market_vwap_state="confirmed",
        sector_confirmed=True,
        regime_score=4,
    )
    sub_two = compute_aggressive_dynamic_entry_score(
        config=_aggressive_cfg(),
        price=1.8,
        news_score=5,
        relative_volume=5,
        gain_pct=10,
        trend_confirmed=True,
        momentum_confirmed=True,
        breakout_confirmed=True,
        symbol_vwap_confirmed=True,
        market_vwap_state="confirmed",
        sector_confirmed=True,
        regime_score=4,
    )

    assert low["allowed"] is True
    assert low["price_tier"] == "two_to_5"
    assert low["size_multiplier"] <= 0.35
    assert sub_two["allowed"] is False
    assert "price_below_aggressive_minimum" in sub_two["hard_reasons"]


def test_aggressive_dynamic_cooldowns() -> None:
    cfg = _aggressive_cfg(cooldown_minutes={"profitable_exit": 10, "scratch_exit": 15, "small_loss": 30, "large_loss": 60, "material_catalyst_reset": True})

    assert aggressive_dynamic_cooldown_minutes(pnl_pct=0.5, config=cfg) == (10, "profitable_exit")
    assert aggressive_dynamic_cooldown_minutes(pnl_pct=0.0, config=cfg) == (15, "scratch_exit")
    assert aggressive_dynamic_cooldown_minutes(pnl_pct=-0.5, config=cfg) == (30, "small_loss")
    assert aggressive_dynamic_cooldown_minutes(pnl_pct=-2.0, config=cfg) == (60, "large_loss")
    assert aggressive_dynamic_cooldown_minutes(pnl_pct=0.1, material_catalyst=True, config=cfg) == (0, "material_catalyst_reset")
