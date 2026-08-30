"""AI/news catalyst scoring for dynamic momentum entries."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

import src.ai_catalyst as ai
import src.news_catalyst as nc
from src.dynamic_universe import dynamic_momentum_entry_passes


def setup_function() -> None:
    ai._CACHE.clear()
    nc._NEWS_CACHE.clear()
    nc._NEWS_LAST_FETCH_AT = None
    nc._NEWS_RATE_LIMIT_UNTIL = None


def test_ai_catalyst_empty_batch_cache_is_neutral() -> None:
    out = ai.score_ai_catalyst("APPS", {"dynamic_momentum_entry": {"ai_catalyst": {}}})
    assert out.score == 50
    assert "cache empty" in out.summary


def test_ai_catalyst_cached_positive_news_scores_strong() -> None:
    now = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    nc._NEWS_CACHE["APPS"] = nc._NewsCacheEntry(
        score=4,
        headline="APPS earnings beat estimates and raises guidance",
        fetched_at=now,
        catalyst=nc.NewsCatalyst(
            "APPS",
            4,
            "APPS earnings beat estimates and raises guidance",
            ("APPS",),
            now,
            "NewsAPI",
        ),
    )
    out = ai.score_ai_catalyst(
        "APPS",
        {"dynamic_momentum_entry": {"ai_catalyst": {}}},
        now=now,
    )
    assert out.score >= 70
    assert "earnings" in out.summary.lower()


def test_ai_catalyst_cached_negative_news_scores_weak() -> None:
    now = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    nc._NEWS_CACHE["XYZ"] = nc._NewsCacheEntry(
        score=-4,
        headline="XYZ announces offering and dilution",
        fetched_at=now,
        catalyst=nc.NewsCatalyst(
            "XYZ",
            -4,
            "XYZ announces offering and dilution",
            ("XYZ",),
            now,
            "NewsAPI",
        ),
    )
    out = ai.score_ai_catalyst(
        "XYZ",
        {"dynamic_momentum_entry": {"ai_catalyst": {}}},
        now=now,
    )
    assert out.score <= 40


def test_ai_catalyst_does_not_fetch_newsapi_when_cache_empty() -> None:
    out = ai.score_ai_catalyst("NIO", {"dynamic_momentum_entry": {"ai_catalyst": {}}})
    assert out.score == 50


def test_dynamic_entry_blocks_low_ai_catalyst_score() -> None:
    ok, reason = dynamic_momentum_entry_passes(
        gain_pct=20.0,
        relative_volume=5.0,
        vwap_above=True,
        spread_pct=0.1,
        bars_1m=pd.DataFrame(),
        bars_5m=pd.DataFrame({"high": [10.0, 10.5]}),
        ref_price=11.0,
        cfg={"ai_catalyst": {"block_below_score": 45}},
        ai_catalyst_score=40,
    )
    assert not ok
    assert "ai_catalyst_score" in reason


def test_dynamic_entry_strong_ai_catalyst_can_boost_thresholds() -> None:
    ok, reason = dynamic_momentum_entry_passes(
        gain_pct=4.7,
        relative_volume=1.2,
        vwap_above=True,
        spread_pct=0.1,
        bars_1m=pd.DataFrame(),
        bars_5m=pd.DataFrame({"high": [10.0, 10.5]}),
        ref_price=11.0,
        cfg={
            "min_day_gain_pct": 5.0,
            "min_relative_volume": 1.3,
            "ai_catalyst": {"boost_at_score": 70, "boost_threshold_factor": 0.90},
        },
        ai_catalyst_score=75,
    )
    assert ok, reason
