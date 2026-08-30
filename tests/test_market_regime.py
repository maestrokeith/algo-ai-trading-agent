"""Tests for multi-factor market regime scoring."""

from __future__ import annotations

import pandas as pd
import pytest

from src.market_regime import MarketRegimeScorer


def _cfg(**overrides):
    base = {
        "market_regime": {
            "enabled": True,
            "symbols": {
                "spy": "SPY",
                "qqq": "QQQ",
                "vix": "VIXY",
                "hyg": "HYG",
                "tlt": "TLT",
            },
            "ma_period_trend": 50,
            "ma_period_rising_falling": 20,
            "vix_threshold": 20.0,
        }
    }
    if overrides:
        base["market_regime"] = {**base["market_regime"], **overrides}
    return base


def _df_closes(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


def test_regime_score_is_sum_of_factor_scores():
    spy = _df_closes([100.0] * 50 + [150.0])
    qqq = _df_closes([100.0] * 50 + [150.0])
    vixy = _df_closes([15.0])
    hyg = _df_closes([50.0] * 19 + [100.0])
    tlt = _df_closes([150.0] * 19 + [50.0])
    scorer = MarketRegimeScorer(_cfg())
    r = scorer.compute({"SPY": spy, "QQQ": qqq, "VIXY": vixy, "HYG": hyg, "TLT": tlt})
    assert r.factor_scores["trend_score"] == 2
    assert r.factor_scores["volatility_score"] == 1
    assert r.factor_scores["breadth_score"] == 1
    assert r.factor_scores["macro_score"] == 1
    assert r.score == 5
    assert r.score == sum(r.factor_scores.values())
    assert r.condition == "bullish"


def test_regime_neutral_when_only_trend_other_factors_off():
    spy = _df_closes([100.0] * 50 + [150.0])
    qqq = _df_closes([100.0] * 50 + [150.0])
    vixy = _df_closes([25.0])
    hyg = _df_closes([100.0] * 20)
    tlt = _df_closes([100.0] * 20)
    scorer = MarketRegimeScorer(_cfg())
    r = scorer.compute({"SPY": spy, "QQQ": qqq, "VIXY": vixy, "HYG": hyg, "TLT": tlt})
    assert r.factor_scores["trend_score"] == 2
    assert r.factor_scores["volatility_score"] == 0
    assert r.factor_scores["breadth_score"] == 0
    assert r.factor_scores["macro_score"] == 0
    assert r.score == 2
    assert r.condition == "neutral"


def test_top_level_regime_overrides_bullish_neutral_mults():
    cfg = _cfg(
        size_multipliers={"bullish": 1.0, "neutral": 0.85, "defensive": 0.5},
    )
    cfg["regime"] = {"bullish_size_mult": 1.25, "neutral_size_mult": 1.0}
    spy = _df_closes([100.0] * 50 + [150.0])
    qqq = _df_closes([100.0] * 50 + [150.0])
    vixy = _df_closes([15.0])
    hyg = _df_closes([50.0] * 19 + [100.0])
    tlt = _df_closes([150.0] * 19 + [50.0])
    bullish = MarketRegimeScorer(cfg).compute(
        {"SPY": spy, "QQQ": qqq, "VIXY": vixy, "HYG": hyg, "TLT": tlt}
    )
    assert bullish.condition == "bullish"
    assert bullish.size_multiplier == pytest.approx(1.25)

    cfg_n = _cfg(
        size_multipliers={"bullish": 1.0, "neutral": 0.85, "defensive": 0.5},
    )
    cfg_n["regime"] = {"bullish_size_mult": 1.25, "neutral_size_mult": 1.0}
    vix_high = _df_closes([25.0])
    flat_hyg = _df_closes([100.0] * 20)
    flat_tlt = _df_closes([100.0] * 20)
    neutral = MarketRegimeScorer(cfg_n).compute(
        {"SPY": spy, "QQQ": qqq, "VIXY": vix_high, "HYG": flat_hyg, "TLT": flat_tlt}
    )
    assert neutral.condition == "neutral"
    assert neutral.size_multiplier == pytest.approx(1.0)


def test_regime_missing_symbol_drops_that_factor_only():
    spy = _df_closes([100.0] * 50 + [150.0])
    qqq = _df_closes([100.0] * 50 + [150.0])
    hyg = _df_closes([50.0] * 19 + [100.0])
    tlt = _df_closes([150.0] * 19 + [50.0])
    scorer = MarketRegimeScorer(_cfg())
    r = scorer.compute({"SPY": spy, "QQQ": qqq, "HYG": hyg, "TLT": tlt})
    assert r.factor_scores["volatility_score"] == 0
    assert r.score == 4
    assert isinstance(r.details["VIXY<20.0"], str)
