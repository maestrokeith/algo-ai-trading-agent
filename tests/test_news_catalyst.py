"""Tests for :mod:`src.news_catalyst` — attribution, bypass gating, starter sizing."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src import dynamic_universe as du
from src import news_catalyst as nc


def setup_function() -> None:
    nc._NEWS_CACHE.clear()
    nc._NEWS_LAST_FETCH_AT = None
    nc._NEWS_RATE_LIMIT_UNTIL = None
    nc._NEWS_LAST_ARTICLES_FETCHED = 0
    nc._NEWS_LAST_ARTICLES_AFTER_FILTER = 0
    nc._ALPACA_NEWS_LAST_EVENTS = 0


def test_dell_article_does_not_score_unrelated_symbols() -> None:
    article = {
        "title": "Dell raises AI server guidance after strong demand",
        "description": "DELL shares rise on data-center outlook.",
        "symbols": ["DELL"],
    }
    assert nc.article_applies_to_symbol(article, "DELL")
    assert not nc.article_applies_to_symbol(article, "HUBC")
    assert not nc.article_applies_to_symbol(article, "PRFX")


def test_headline_ticker_match_without_explicit_symbols() -> None:
    article = {"title": "REPL wins FDA approval for lead drug candidate"}
    assert nc.article_applies_to_symbol(article, "REPL")
    assert not nc.article_applies_to_symbol(article, "PD")


def test_fetch_maps_scores_only_to_attributed_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    articles = [
        {
            "title": "Dell raises guidance on AI server demand",
            "description": "Enterprise demand lifts outlook.",
            "symbols": ["DELL"],
            "publishedAt": "2026-05-29T14:00:00Z",
        }
    ]

    monkeypatch.setattr(nc, "_fetch_batch_articles", lambda *_a, **_kw: articles)
    out = nc.fetch_recent_news_catalysts(None, ["DELL", "HUBC", "PRFX"], config={})
    assert "DELL" in out
    assert "HUBC" not in out
    assert "PRFX" not in out
    assert out["DELL"].article_count == 1


def test_news_pipeline_summary_counts_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    articles = [
        {
            "title": "Dell raises guidance on AI server demand",
            "description": "DELL shares rise.",
            "symbols": ["DELL"],
        },
        {
            "title": "OKTA beats earnings estimates",
            "description": "OKTA shares rise.",
            "symbols": ["OKTA"],
        },
    ]

    monkeypatch.setattr(nc, "_fetch_batch_articles", lambda *_a, **_kw: articles)
    nc.fetch_recent_news_catalysts(None, ["DELL", "OKTA", "HUBC"], config={})

    assert nc.news_pipeline_summary() == {
        "articles_fetched": 2,
        "articles_after_filter": 2,
        "symbols_scored": 3,
    }


def test_fetch_logs_reason_when_articles_exist_but_score_is_zero(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    articles = [
        {
            "title": "Alphabet corporation announcement",
            "description": "Routine disclosure communication pending.",
        }
    ]

    monkeypatch.setattr(nc, "_fetch_batch_articles", lambda *_a, **_kw: articles)
    caplog.set_level("INFO", logger="src.news_catalyst")

    out = nc.fetch_recent_news_catalysts(None, ["AAPL"], config={})

    assert out == {}
    assert "NEWS_LOOKUP symbol=AAPL matched_articles=0 cache_hit=false sentiment_score=0.00 reason=no symbol match" in caplog.text


def test_fetch_logs_lookup_and_positive_sentiment(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    articles = [
        {
            "title": "Apple raises guidance after strong earnings beat",
            "description": "Apple shares rise on outlook.",
            "symbols": ["AAPL"],
        }
    ]

    monkeypatch.setattr(nc, "_fetch_batch_articles", lambda *_a, **_kw: articles)
    caplog.set_level("INFO", logger="src.news_catalyst")

    out = nc.fetch_recent_news_catalysts(None, ["AAPL"], config={})

    assert "AAPL" in out
    assert out["AAPL"].article_count == 1
    assert out["AAPL"].sentiment > 0
    assert "NEWS_LOOKUP symbol=AAPL matched_articles=1 cache_hit=false sentiment_score=" in caplog.text


def test_pypi_package_release_does_not_become_news_catalyst(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    articles = [
        {
            "title": "NVDA 0.4.1 package released on PyPI",
            "description": "Python package metadata update.",
            "symbols": ["NVDA"],
            "source": {"name": "PyPI"},
            "url": "https://pypi.org/project/nvda/",
        }
    ]

    monkeypatch.setattr(nc, "_fetch_batch_articles", lambda *_a, **_kw: articles)
    caplog.set_level("INFO", logger="src.news_catalyst")

    out = nc.fetch_recent_news_catalysts(None, ["NVDA"], config={})

    assert out == {}
    assert "NEWS_PACKAGE_SPAM_FILTERED symbol=NVDA" in caplog.text


def test_news_dynamic_entry_bypass_repl_like_candidate() -> None:
    ok, reason = nc.news_dynamic_entry_bypass_passes(
        symbol="REPL",
        news_score=2,
        relative_volume=2.0,
        price_above_vwap=True,
        spread_pct=0.2,
        is_dynamic=True,
    )
    assert ok
    assert reason == "news_catalyst"


def test_news_dynamic_entry_bypass_blocks_wide_spread() -> None:
    ok, reason = nc.news_dynamic_entry_bypass_passes(
        symbol="REPL",
        news_score=2,
        relative_volume=2.0,
        price_above_vwap=True,
        spread_pct=2.0,
        is_dynamic=True,
    )
    assert not ok
    assert "spread" in reason


def test_news_dynamic_entry_bypass_blocks_without_news_score() -> None:
    ok, reason = nc.news_dynamic_entry_bypass_passes(
        symbol="REPL",
        news_score=0,
        relative_volume=2.0,
        price_above_vwap=True,
        spread_pct=0.2,
        is_dynamic=True,
    )
    assert not ok
    assert "news_score" in reason


def test_news_dynamic_entry_bypass_not_for_core_symbol() -> None:
    ok, reason = nc.news_dynamic_entry_bypass_passes(
        symbol="NVDA",
        news_score=3,
        relative_volume=2.0,
        price_above_vwap=True,
        spread_pct=0.2,
        is_dynamic=False,
    )
    assert not ok
    assert "core" in reason


def test_get_news_score_disabled_returns_safe_default() -> None:
    score, reason = nc.get_news_score("OKTA", config={"news_ai": {"enabled": False}})
    assert score == 0
    assert reason == "disabled"


def test_news_refresh_phase_for_et() -> None:
    et = ZoneInfo("America/New_York")
    assert nc.news_refresh_phase_for_et(datetime(2026, 5, 29, 8, 30, tzinfo=et)) == (
        "premarket",
        15.0 * 60.0,
    )
    assert nc.news_refresh_phase_for_et(datetime(2026, 5, 29, 9, 35, tzinfo=et)) == (
        "market_open",
        5.0 * 60.0,
    )
    assert nc.news_refresh_phase_for_et(datetime(2026, 5, 29, 10, 30, tzinfo=et)) == (
        "intraday",
        15.0 * 60.0,
    )
    assert nc.news_refresh_phase_for_et(datetime(2026, 5, 29, 15, 45, tzinfo=et)) == (
        "eod",
        0.0,
    )
    assert nc.news_refresh_phase_for_et(datetime(2026, 5, 29, 16, 5, tzinfo=et)) == (None, None)


def test_fetch_recent_news_catalysts_force_refresh_overrides_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def fake_fetch(*_args, **_kwargs):
        calls.append(1)
        return [{"title": "Okta beats earnings estimates", "symbols": ["OKTA"]}]

    monkeypatch.setattr(nc, "_fetch_batch_articles", fake_fetch)
    nc._NEWS_CACHE["OKTA"] = nc._NewsCacheEntry(
        score=0,
        headline="",
        fetched_at=datetime.now(timezone.utc),
        catalyst=None,
    )

    nc.fetch_recent_news_catalysts(None, ["OKTA"], config={}, now=datetime.now(timezone.utc), force_refresh=True)
    assert len(calls) == 1


def test_alpaca_realtime_news_persists_and_scores_event(
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime(2026, 6, 18, 14, 0, tzinfo=timezone.utc)

    class FakeMarket:
        def get_recent_news(self, symbols, **_kwargs):
            assert "ABCD" in symbols
            return [
                {
                    "id": 123,
                    "headline": "ABCD wins contract with major customer",
                    "summary": "ABCD shares rise after a new partnership.",
                    "symbols": ["ABCD"],
                    "created_at": now,
                    "url": "https://example.test/news/abcd",
                }
            ]

    config = {
        "news_sentiment": {
            "alpaca_realtime": {
                "enabled": True,
                "symbols": ["ABCD"],
                "persist_dir": str(tmp_path),
                "min_score": 3,
            }
        }
    }
    caplog.set_level("INFO", logger="src.news_catalyst")

    out = nc.fetch_recent_news_catalysts(FakeMarket(), [], config=config, now=now)

    assert "ABCD" in out
    assert out["ABCD"].source == "alpaca"
    assert out["ABCD"].score >= 3
    persisted = tmp_path / "2026-06-18.jsonl"
    assert persisted.exists()
    assert "ABCD wins contract" in persisted.read_text()
    assert "ALPACA_NEWS_INGEST_START" in caplog.text
    assert "ALPACA_NEWS_EVENT" in caplog.text
    assert "ALPACA_NEWS_SCORE symbol=ABCD" in caplog.text
    assert "ALPACA_NEWS_LATENCY symbol=ABCD" in caplog.text


def test_alpaca_realtime_news_ignores_stale_or_low_quality_news(tmp_path) -> None:
    now = datetime(2026, 6, 18, 14, 0, tzinfo=timezone.utc)

    class FakeMarket:
        def get_recent_news(self, *_args, **_kwargs):
            return [
                {
                    "headline": "WXYZ routine corporate update",
                    "summary": "No catalyst language.",
                    "symbols": ["WXYZ"],
                    "created_at": now,
                },
                {
                    "headline": "ABCD wins contract with major customer",
                    "summary": "Stale but otherwise relevant.",
                    "symbols": ["ABCD"],
                    "created_at": now.replace(hour=13, minute=0),
                },
            ]

    config = {
        "news_sentiment": {
            "alpaca_realtime": {
                "enabled": True,
                "symbols": ["ABCD", "WXYZ"],
                "persist_dir": str(tmp_path),
                "max_age_seconds": 60,
                "min_score": 3,
            }
        }
    }

    out = nc.fetch_recent_news_catalysts(FakeMarket(), [], config=config, now=now)

    assert out == {}
    assert nc.get_cached_news_catalyst("ABCD", now=now) is None
    assert nc.get_cached_news_catalyst("WXYZ", now=now) is None


def test_alpaca_realtime_news_failure_falls_back_safely(caplog: pytest.LogCaptureFixture) -> None:
    class FakeMarket:
        def get_recent_news(self, *_args, **_kwargs):
            raise RuntimeError("rate limited")

    config = {"news_sentiment": {"alpaca_realtime": {"enabled": True, "symbols": ["ABCD"]}}}
    caplog.set_level("INFO", logger="src.news_catalyst")

    out = nc.fetch_recent_news_catalysts(
        FakeMarket(),
        [],
        config=config,
        now=datetime(2026, 6, 18, 14, 0, tzinfo=timezone.utc),
    )

    assert out == {}
    assert "ALPACA_NEWS_FALLBACK reason=fetch_failed" in caplog.text


def test_dynamic_momentum_entry_passes_news_bypass_skips_breakout() -> None:
    bars_1m = pd.DataFrame(
        {
            "high": [100.0, 100.0],
            "low": [99.0, 99.0],
            "open": [99.5, 99.5],
            "close": [99.8, 99.9],
            "volume": [10_000.0, 10_000.0],
        }
    )
    bars_5m = pd.DataFrame({"high": [150.0, 151.0]})
    ok, msg = du.dynamic_momentum_entry_passes(
        gain_pct=8.0,
        relative_volume=2.0,
        vwap_above=True,
        spread_pct=0.2,
        bars_1m=bars_1m,
        bars_5m=bars_5m,
        ref_price=98.0,
        symbol="REPL",
        news_score=2,
        is_dynamic=True,
        cfg={
            "min_day_gain_pct": 5.0,
            "min_relative_volume": 1.3,
            "max_entry_spread_pct": 3.0,
            "opening_range_breakout": {"enabled": False},
            "news_dynamic_entry": {
                "min_news_score": 2,
                "min_relative_volume": 1.5,
                "max_spread_pct": 1.0,
            },
        },
    )
    assert ok, msg
    assert msg == "ok news_catalyst"


def test_dynamic_momentum_entry_passes_no_bypass_when_spread_wide() -> None:
    bars_1m = pd.DataFrame(
        {
            "high": [98.0, 98.5],
            "low": [97.0, 97.5],
            "open": [97.5, 98.0],
            "close": [98.0, 98.2],
            "volume": [10_000.0, 10_000.0],
        }
    )
    ok, msg = du.dynamic_momentum_entry_passes(
        gain_pct=8.0,
        relative_volume=2.0,
        vwap_above=True,
        spread_pct=2.0,
        bars_1m=bars_1m,
        bars_5m=pd.DataFrame({"high": [95.0, 96.0]}),
        ref_price=94.0,
        symbol="REPL",
        news_score=2,
        is_dynamic=True,
        cfg={
            "min_day_gain_pct": 5.0,
            "min_relative_volume": 1.3,
            "max_entry_spread_pct": 3.0,
            "opening_range_breakout": {"enabled": False},
            "news_dynamic_entry": {"max_spread_pct": 1.0},
        },
    )
    assert not ok
    assert "breakout" in msg or "nh=" in msg


def test_dynamic_momentum_entry_passes_core_symbol_no_news_bypass() -> None:
    bars_1m = pd.DataFrame(
        {
            "high": [98.0, 98.5],
            "low": [97.0, 97.5],
            "open": [97.5, 98.0],
            "close": [98.0, 98.2],
            "volume": [10_000.0, 10_000.0],
        }
    )
    ok, msg = du.dynamic_momentum_entry_passes(
        gain_pct=8.0,
        relative_volume=2.0,
        vwap_above=True,
        spread_pct=0.2,
        bars_1m=bars_1m,
        bars_5m=pd.DataFrame({"high": [95.0, 96.0]}),
        ref_price=94.0,
        symbol="NVDA",
        news_score=3,
        is_dynamic=False,
        cfg={
            "min_day_gain_pct": 5.0,
            "min_relative_volume": 1.3,
            "max_entry_spread_pct": 3.0,
            "opening_range_breakout": {"enabled": False},
        },
    )
    assert not ok
    assert "breakout" in msg or "nh=" in msg


def test_news_dynamic_starter_notional_clamped() -> None:
    assert nc.news_dynamic_starter_notional_usd({}) == 750.0
    assert nc.news_dynamic_starter_notional_usd(
        {"starter_notional_usd": 900, "starter_notional_fraction_of_normal": 0.25},
        normal_notional=4000.0,
    ) == 750.0
    assert nc.news_dynamic_starter_notional_usd(
        {"starter_notional_usd": 750, "starter_notional_fraction_of_normal": 0.25},
        normal_notional=2000.0,
    ) == 500.0
