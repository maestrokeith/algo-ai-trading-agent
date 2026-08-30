"""Tests for premarket provider execution (implemented in premarket_intelligence)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pytest

import src.premarket_intelligence as pm
import src.news_catalyst as nc
from src.news_catalyst import load_premarket_artifacts
from src.premarket_intelligence import (
    NewsEvent,
    PremarketRankEntry,
    ProviderExecResult,
    build_newsapi_queries,
    build_newsapi_query_batches,
    default_premarket_artifacts_dir,
    build_overnight_earnings_query,
    build_overnight_earnings_query_batches,
    build_premarket_rankings,
    default_premarket_catalysts_path,
    default_premarket_event_feed_path,
    default_premarket_rank_path,
    default_premarket_rankings_path,
    execute_premarket_providers,
    fetch_newsapi_articles,
    fetch_overnight_earnings_events,
    fetch_sec_filings,
    log_premarket_rankings,
    merge_premarket_events,
    write_premarket_artifacts,
    write_premarket_rank_json,
)


def test_fetch_newsapi_articles_uses_newsapi_client(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def _fake_query(query, api_key, **kwargs):
        calls.append({"query": query, "api_key": api_key, "kwargs": kwargs})
        meta = kwargs.get("meta")
        if meta is not None:
            meta["request_sent"] = True
            meta["http_status"] = 200
        return [
            {"title": "AAPL beats", "symbols": ["AAPL"], "publishedAt": "2026-06-01T10:00:00Z"},
            {"title": "AAPL guidance", "symbols": ["AAPL"], "publishedAt": "2026-06-01T11:00:00Z"},
        ]

    monkeypatch.setenv("NEWSAPI_KEY", "secret-key")
    monkeypatch.setattr(pm, "fetch_articles_query", _fake_query)

    result = fetch_newsapi_articles(
        ["AAPL"],
        {"premarket_intelligence": {"newsapi_enabled": True}, "news_sentiment": {"enabled": True}},
        10.0,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert calls
    assert len(calls) == 1
    assert calls[0]["api_key"] == "secret-key"
    assert calls[0]["query"] == "(Apple OR AAPL)"
    assert calls[0]["kwargs"]["lookback_hours"] == 48
    assert result.request_sent is True
    assert result.http_status == 200
    assert result.articles == 2
    assert result.raw_articles_before_filter == 2
    assert result.articles_after_filter == 2
    assert result.request_symbol_count == 1
    assert result.returned_symbol_count == 1
    assert result.sample_article_titles == ["AAPL beats", "AAPL guidance"]
    assert result.events[0].rank_source == "unknown"


def test_fetch_newsapi_articles_includes_30_hour_headline_in_premarket(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO)
    now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)
    calls: list[dict] = []

    def _fake_query(query, api_key, **kwargs):
        calls.append({"query": query, "kwargs": kwargs})
        meta = kwargs.get("meta")
        if meta is not None:
            meta["request_sent"] = True
            meta["http_status"] = 200
            meta["from"] = "2026-06-09T12:00:00Z"
            meta["to"] = "2026-06-11T12:00:00Z"
        assert kwargs["lookback_hours"] >= 30
        assert kwargs["now"] == now
        return [
            {
                "title": "Apple raises guidance",
                "description": "Apple lifted its revenue outlook.",
                "symbols": ["AAPL"],
                "publishedAt": "2026-06-10T06:00:00Z",
            }
        ]

    monkeypatch.setenv("NEWSAPI_KEY", "secret-key")
    monkeypatch.setattr(pm, "fetch_articles_query", _fake_query)
    pm._NEWSAPI_QUERY_CACHE.clear()
    pm._NEWSAPI_DAILY_CALLS.clear()

    result = fetch_newsapi_articles(
        ["AAPL"],
        {
            "premarket_intelligence": {"newsapi_enabled": True},
            "news_sentiment": {"enabled": True, "headline_lookback_hours": 24},
        },
        10.0,
        now=now,
    )

    assert calls
    assert calls[0]["kwargs"]["lookback_hours"] == 48
    assert result.articles == 1
    assert result.events[0].symbol == "AAPL"
    assert "effective_lookback_hours=48" in caplog.text
    assert "NEWSAPI_LOOKBACK_WINDOW provider=NewsAPI endpoint=everything lookback_hours=48" in caplog.text
    assert "older_than_24h_included=1" in caplog.text


def test_fetch_newsapi_missing_key_logs_request_sent_false(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import logging

    monkeypatch.delenv("NEWSAPI_KEY", raising=False)
    caplog.set_level(logging.INFO)
    result = fetch_newsapi_articles(
        ["AAPL"],
        {"premarket_intelligence": {"newsapi_enabled": True}, "news_sentiment": {"enabled": True}},
        10.0,
    )
    assert result.request_sent is False
    assert result.skip_reason == "missing_api_key"
    assert "request_sent=false reason=missing_api_key" in caplog.text


def test_fetch_newsapi_disabled_nested_config_skips_cleanly(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import logging

    caplog.set_level(logging.INFO)
    monkeypatch.setenv("NEWSAPI_KEY", "secret-key")
    monkeypatch.setattr(
        pm,
        "fetch_articles_query",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("NewsAPI should be skipped")),
    )

    result = fetch_newsapi_articles(
        ["AAPL"],
        {
            "premarket_intelligence": {
                "newsapi": {"enabled": False},
                "newsapi_enabled": True,
            },
            "news_sentiment": {"enabled": True},
        },
        10.0,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert result.enabled is False
    assert result.request_sent is False
    assert result.skip_reason == "newsapi_disabled"
    assert "provider=newsapi enabled=false" in caplog.text
    assert "request_sent=false reason=newsapi_disabled" in caplog.text


def test_execute_premarket_providers_merges_newsapi_and_alpaca(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _newsapi(*_a, **_k):
        return ProviderExecResult(
            provider="newsapi",
            request_sent=True,
            duration_ms=5.0,
            articles=2,
            events=[
                NewsEvent("NVDA", "NVDA up", "newsapi"),
                NewsEvent("NVDA", "NVDA up", "newsapi"),
            ],
        )

    def _alpaca(*_a, **_k):
        return ProviderExecResult(
            provider="alpaca",
            request_sent=True,
            duration_ms=7.0,
            articles=1,
            events=[NewsEvent("NVDA", "NVDA headline", "alpaca")],
        )

    def _sec(*_a, **_k):
        return ProviderExecResult(provider="sec", request_sent=True, duration_ms=3.0, filings=0)

    def _overnight(*_a, **_k):
        return ProviderExecResult(provider="earnings_overnight", request_sent=True, duration_ms=4.0, articles=0)

    monkeypatch.setattr(pm, "fetch_newsapi_articles", _newsapi)
    monkeypatch.setattr(pm, "fetch_alpaca_news_events", _alpaca)
    monkeypatch.setattr(pm, "fetch_sec_filings", _sec)
    monkeypatch.setattr(pm, "fetch_overnight_earnings_events", _overnight)
    pm._NEWSAPI_QUERY_CACHE.clear()
    pm._NEWSAPI_DAILY_CALLS.clear()
    pm._PREMARKET_PROVIDER_CACHE.clear()

    bundle = execute_premarket_providers({}, ["NVDA"], now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc))
    assert bundle.news_article_count == 3
    assert len(bundle.events) == 2


def test_execute_premarket_providers_newsapi_disabled_keeps_alpaca_and_sec_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEWSAPI_KEY", "secret-key")
    monkeypatch.setattr(
        pm,
        "fetch_articles_query",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("NewsAPI should be skipped")),
    )
    monkeypatch.setattr(
        pm,
        "fetch_alpaca_news_events",
        lambda *a, **k: ProviderExecResult(
            provider="alpaca",
            enabled=True,
            request_sent=True,
            articles=1,
            events=[NewsEvent("AAPL", "AAPL product launch", "alpaca", score=4.0)],
        ),
    )
    monkeypatch.setattr(
        pm,
        "fetch_sec_filings",
        lambda *a, **k: ProviderExecResult(provider="sec", enabled=True, request_sent=True, filings=1),
    )
    monkeypatch.setattr(pm, "fetch_benzinga_events", lambda *a, **k: ProviderExecResult(provider="benzinga"))
    monkeypatch.setattr(pm, "fetch_twitter_trusted_events", lambda *a, **k: ProviderExecResult(provider="twitter"))
    pm._NEWSAPI_QUERY_CACHE.clear()
    pm._NEWSAPI_DAILY_CALLS.clear()
    pm._PREMARKET_PROVIDER_CACHE.clear()

    bundle = execute_premarket_providers(
        {
            "premarket_intelligence": {
                "newsapi": {"enabled": False},
                "newsapi_enabled": True,
                "alpaca_news_enabled": True,
                "sec_filings_enabled": True,
                "overnight_earnings_enabled": True,
            },
            "news_sentiment": {"enabled": True},
        },
        ["AAPL"],
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert bundle.newsapi.enabled is False
    assert bundle.newsapi.skip_reason == "newsapi_disabled"
    assert bundle.overnight_earnings is not None
    assert bundle.overnight_earnings.enabled is True
    assert bundle.overnight_earnings.skip_reason == "depends_on_newsapi_disabled"
    assert bundle.alpaca.articles == 1
    assert bundle.sec.filings == 1


def test_execute_premarket_providers_newsapi_429_continues_fallback_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pm, "fetch_alpaca_news_events", lambda *a, **k: ProviderExecResult(provider="alpaca"))
    monkeypatch.setattr(pm, "fetch_sec_filings", lambda *a, **k: ProviderExecResult(provider="sec"))
    monkeypatch.setattr(pm, "fetch_benzinga_events", lambda *a, **k: ProviderExecResult(provider="benzinga"))
    monkeypatch.setattr(pm, "fetch_twitter_trusted_events", lambda *a, **k: ProviderExecResult(provider="twitter"))
    monkeypatch.setattr(pm, "fetch_reddit_social_sentiment", lambda *a, **k: ProviderExecResult(provider="reddit"))
    monkeypatch.setattr(
        pm,
        "fetch_newsapi_articles",
        lambda *a, **k: ProviderExecResult(
            provider="newsapi",
            request_sent=True,
            http_status=429,
            skip_reason="rate_limited",
        ),
    )
    monkeypatch.setattr(
        pm,
        "fetch_finnhub_events",
        lambda *a, **k: ProviderExecResult(
            provider="finnhub",
            enabled=True,
            request_sent=True,
            articles=1,
            events=[NewsEvent("NVDA", "Nvidia announces AI platform", "finnhub", score=6.0)],
        ),
    )
    monkeypatch.setattr(
        pm,
        "fetch_marketaux_events",
        lambda *a, **k: ProviderExecResult(
            provider="marketaux",
            enabled=True,
            request_sent=True,
            articles=1,
            events=[NewsEvent("AMD", "AMD raises guidance", "marketaux", score=6.0)],
        ),
    )
    monkeypatch.setattr(
        pm,
        "fetch_overnight_earnings_events",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("rate-limited NewsAPI should skip overnight earnings")),
    )

    bundle = execute_premarket_providers(
        {"premarket_intelligence": {"finnhub_enabled": True, "marketaux_enabled": True}},
        ["NVDA", "AMD"],
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert bundle.newsapi.http_status == 429
    assert bundle.overnight_earnings is not None
    assert bundle.overnight_earnings.skip_reason == "depends_on_newsapi_rate_limited"
    assert bundle.finnhub is not None
    assert bundle.marketaux is not None
    assert {ev.source for ev in bundle.events} == {"finnhub", "marketaux"}
    assert bundle.news_article_count == 2


def test_finnhub_and_marketaux_config_events_normalize_to_provider_events() -> None:
    pm._PREMARKET_PROVIDER_CACHE.clear()
    cfg = {
        "premarket_intelligence": {
            "finnhub_enabled": True,
            "finnhub_events": [
                {
                    "symbols": ["AAPL"],
                    "headline": "Apple signs AI partnership",
                    "source": "Finnhub Wire",
                    "datetime": "2026-06-01T09:30:00Z",
                    "url": "https://example.com/aapl",
                }
            ],
            "marketaux_enabled": True,
            "marketaux_events": [
                {
                    "symbol": "MSFT",
                    "title": "Microsoft lifts cloud outlook",
                    "source": {"name": "Marketaux"},
                    "published_at": "2026-06-01T09:31:00Z",
                }
            ],
        }
    }

    finnhub = pm.fetch_finnhub_events(
        ["AAPL", "MSFT"],
        cfg,
        10.0,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )
    marketaux = pm.fetch_marketaux_events(
        ["AAPL", "MSFT"],
        cfg,
        10.0,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert finnhub.articles == 1
    assert finnhub.events[0].symbol == "AAPL"
    assert finnhub.events[0].publisher == "Finnhub Wire"
    assert marketaux.articles == 1
    assert marketaux.events[0].symbol == "MSFT"
    assert marketaux.events[0].publisher == "Marketaux"


def test_low_coverage_premarket_write_does_not_overwrite_richer_existing_artifacts(
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.WARNING)
    artifact_dir = default_premarket_artifacts_dir(tmp_path)
    artifact_dir.mkdir(parents=True)
    rich_payload = {
        "generated_at": "2026-06-01T08:00:00+00:00",
        "source": "rich_existing",
        "ttl_minutes": 390,
        "symbols": [f"SYM{i}" for i in range(12)],
        "candidate_symbols": [f"SYM{i}" for i in range(12)],
        "events": [{"symbol": f"SYM{i % 12}", "headline": f"headline {i}"} for i in range(36)],
        "catalysts": [{"symbol": f"SYM{i}", "headline": f"catalyst {i}", "score": 6.0} for i in range(12)],
        "rankings": [{"symbol": f"SYM{i}", "score": 6.0, "source": "newsapi"} for i in range(12)],
    }
    default_premarket_event_feed_path(tmp_path).write_text(json.dumps(rich_payload), encoding="utf-8")

    write_premarket_artifacts(
        tmp_path,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        source="thin_rate_limited_fallback",
        events=[NewsEvent("AAPL", "Apple isolated headline", "finnhub", score=1.0)],
        catalysts={},
        rankings=[],
        candidate_symbols=["AAPL"],
    )

    preserved = json.loads(default_premarket_event_feed_path(tmp_path).read_text(encoding="utf-8"))
    assert preserved["source"] == "rich_existing"
    assert len(preserved["events"]) == 36
    assert "PREMARKET_ARTIFACT_PRESERVED reason=low_coverage_or_rate_limited" in caplog.text


def test_execute_premarket_providers_social_reddit_diagnostics_not_merged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(pm, "fetch_alpaca_news_events", lambda *a, **k: ProviderExecResult(provider="alpaca"))
    monkeypatch.setattr(pm, "fetch_sec_filings", lambda *a, **k: ProviderExecResult(provider="sec"))
    monkeypatch.setattr(pm, "fetch_benzinga_events", lambda *a, **k: ProviderExecResult(provider="benzinga"))
    monkeypatch.setattr(pm, "fetch_twitter_trusted_events", lambda *a, **k: ProviderExecResult(provider="twitter", enabled=False, skip_reason="twitter_disabled"))
    monkeypatch.setattr(pm, "fetch_newsapi_articles", lambda *a, **k: ProviderExecResult(provider="newsapi", enabled=False, skip_reason="newsapi_disabled"))
    monkeypatch.setattr(pm, "fetch_overnight_earnings_events", lambda *a, **k: ProviderExecResult(provider="earnings_overnight"))
    monkeypatch.setattr(
        pm,
        "fetch_reddit_social_sentiment",
        lambda *a, **k: ProviderExecResult(
            provider="reddit",
            enabled=True,
            request_sent=True,
            raw_articles_before_filter=4,
            articles_after_filter=2,
            articles=2,
        ),
    )

    bundle = execute_premarket_providers(
        {"premarket_intelligence": {"social": {"enabled": True}}},
        ["AAPL"],
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        project_root=tmp_path,
    )

    assert bundle.reddit is not None
    assert bundle.reddit.articles_after_filter == 2
    assert bundle.events == []
    assert bundle.catalysts == {}
    assert bundle.rankings == []


def test_execute_premarket_providers_includes_top_gainer_in_query_universe(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO)

    class FakeMarket:
        def get_top_movers(self):
            return [{"symbol": "DXST"}, {"symbol": "AAPL"}]

    def _newsapi(*_a, **_k):
        return ProviderExecResult(provider="newsapi", request_sent=True, duration_ms=5.0, articles=0, requests_made=1)

    def _alpaca(*_a, **_k):
        return ProviderExecResult(provider="alpaca", request_sent=False, duration_ms=1.0, articles=0)

    def _sec(*_a, **_k):
        return ProviderExecResult(provider="sec", request_sent=False, duration_ms=1.0, filings=0)

    def _overnight(*_a, **_k):
        return ProviderExecResult(provider="earnings_overnight", request_sent=False, duration_ms=1.0, articles=0)

    monkeypatch.setenv("NEWSAPI_KEY", "secret-key")
    monkeypatch.setattr(pm, "fetch_articles_query", lambda *a, **k: [])
    monkeypatch.setattr(pm, "fetch_alpaca_news_events", _alpaca)
    monkeypatch.setattr(pm, "fetch_sec_filings", _sec)
    monkeypatch.setattr(pm, "fetch_overnight_earnings_events", _overnight)
    pm._NEWSAPI_QUERY_CACHE.clear()
    pm._NEWSAPI_DAILY_CALLS.clear()
    pm._PREMARKET_PROVIDER_CACHE.clear()

    execute_premarket_providers(
        {"premarket_intelligence": {"newsapi_enabled": True}},
        ["AAPL"],
        market_client=FakeMarket(),
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert "PREMARKET_CANDIDATE_UNIVERSE count=" in caplog.text
    assert "sample=[AAPL,DXST]" in caplog.text
    assert "PREMARKET_NEWS_QUERY symbol=DXST query=" in caplog.text


def test_premarket_artifacts_include_raw_dynamic_movers_without_catalyst_bypass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO)

    class FakeMarket:
        def get_top_movers(self):
            return [
                {"symbol": "SUNE"},
                {"symbol": "INHD"},
                {"symbol": "ABAT"},
            ]

    def _empty(provider: str):
        return ProviderExecResult(provider=provider, request_sent=False, duration_ms=1.0)

    monkeypatch.setattr(pm, "fetch_alpaca_news_events", lambda *_a, **_k: _empty("alpaca"))
    monkeypatch.setattr(pm, "fetch_sec_filings", lambda *_a, **_k: _empty("sec"))
    monkeypatch.setattr(pm, "fetch_benzinga_events", lambda *_a, **_k: _empty("benzinga"))
    monkeypatch.setattr(pm, "fetch_twitter_trusted_events", lambda *_a, **_k: _empty("twitter"))
    monkeypatch.setattr(pm, "fetch_reddit_social_sentiment", lambda *_a, **_k: _empty("reddit"))
    monkeypatch.setattr(pm, "fetch_newsapi_articles", lambda *_a, **_k: _empty("newsapi"))
    monkeypatch.setattr(pm, "fetch_overnight_earnings_events", lambda *_a, **_k: _empty("earnings_overnight"))
    nc._NEWS_CACHE.clear()

    stats = pm._run_news_5am_job(
        {"universe": {"symbols": ["AAPL"]}},
        datetime(2026, 6, 1, 5, 15, tzinfo=ZoneInfo("America/New_York")),
        project_root=tmp_path,
        market_client=FakeMarket(),
    )

    rankings_payload = json.loads(default_premarket_rankings_path(tmp_path).read_text())
    catalysts_payload = json.loads(default_premarket_catalysts_path(tmp_path).read_text())
    for sym in ("SUNE", "INHD", "ABAT"):
        assert sym in rankings_payload["symbols"]
        assert sym in rankings_payload["candidate_symbols"]
        assert sym in catalysts_payload["symbols"]
        assert sym in catalysts_payload["candidate_symbols"]
    assert rankings_payload["rankings"] == []
    assert catalysts_payload["catalysts"] == []
    loaded = load_premarket_artifacts(
        tmp_path,
        now=datetime(2026, 6, 1, 5, 20, tzinfo=ZoneInfo("America/New_York")),
        emit_log=False,
    )
    assert "SUNE" not in loaded
    assert stats.ranked == 0
    assert "PREMARKET_SOURCE_COUNTS none=0" in caplog.text
    assert "PREMARKET_RANKED_SYMBOLS count=0 symbols=none" in caplog.text
    assert "PREMARKET_CATALYST_SYMBOLS count=0 symbols=none" in caplog.text
    assert "PREMARKET_ARTIFACT_WRITE_COUNT path=" in caplog.text
    assert "PREMARKET_ARTIFACT_WRITTEN path=" in caplog.text
    assert "PREMARKET_LOW_COVERAGE catalyst_ranked_symbols=0 total_events=0" in caplog.text


def test_execute_premarket_providers_writes_catalyst_for_non_core_mover(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO)

    class FakeMarket:
        def get_top_movers(self):
            return [{"symbol": "DXST"}]

    def _newsapi(*_a, **_k):
        return ProviderExecResult(provider="newsapi", request_sent=False, duration_ms=1.0, articles=0)

    def _alpaca(*_a, **_k):
        return ProviderExecResult(
            provider="alpaca",
            request_sent=True,
            duration_ms=1.0,
            articles=1,
            events=[NewsEvent("DXST", "DXST wins cloud deal", "alpaca", score=6.5)],
        )

    def _sec(*_a, **_k):
        return ProviderExecResult(provider="sec", request_sent=False, duration_ms=1.0, filings=0)

    def _overnight(*_a, **_k):
        return ProviderExecResult(provider="earnings_overnight", request_sent=False, duration_ms=1.0, articles=0)

    monkeypatch.setattr(pm, "fetch_newsapi_articles", _newsapi)
    monkeypatch.setattr(pm, "fetch_alpaca_news_events", _alpaca)
    monkeypatch.setattr(pm, "fetch_sec_filings", _sec)
    monkeypatch.setattr(pm, "fetch_overnight_earnings_events", _overnight)
    pm._NEWSAPI_QUERY_CACHE.clear()
    pm._NEWSAPI_DAILY_CALLS.clear()
    pm._PREMARKET_PROVIDER_CACHE.clear()

    bundle = execute_premarket_providers(
        {"premarket_intelligence": {"alpaca_news_enabled": True}},
        ["AAPL"],
        market_client=FakeMarket(),
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert "DXST" in bundle.catalysts
    assert any(row.symbol == "DXST" for row in bundle.rankings)
    assert "PREMARKET_CATALYST_WRITTEN symbol=DXST score=" in caplog.text


def test_execute_premarket_providers_emits_event_feed_and_source_summaries(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO)

    def _newsapi(*_a, **_k):
        return ProviderExecResult(provider="newsapi", request_sent=False, articles=0, requests_made=0)

    def _alpaca(*_a, **_k):
        return ProviderExecResult(provider="alpaca", request_sent=False, articles=0)

    def _sec(*_a, **_k):
        return ProviderExecResult(provider="sec", request_sent=False, filings=0, cik_mapped=0)

    def _overnight(*_a, **_k):
        return ProviderExecResult(provider="earnings_overnight", request_sent=False, articles=0)

    monkeypatch.setattr(pm, "fetch_newsapi_articles", _newsapi)
    monkeypatch.setattr(pm, "fetch_alpaca_news_events", _alpaca)
    monkeypatch.setattr(pm, "fetch_sec_filings", _sec)
    monkeypatch.setattr(pm, "fetch_overnight_earnings_events", _overnight)

    bundle = execute_premarket_providers(
        {
            "premarket_intelligence": {
                "benzinga_enabled": True,
                "benzinga_events": [
                    {
                        "symbol": "AAPL",
                        "title": "Benzinga headline",
                        "url": "http://benzinga.example/aapl",
                        "published_at": "2026-06-01T09:30:00Z",
                        "score": 2.0,
                        "sentiment": 0.2,
                    }
                ],
                "twitter_trusted_enabled": True,
                "twitter_trusted_events": [
                    {
                        "symbol": "AAPL",
                        "title": "Trusted tweet",
                        "url": "http://twitter.example/aapl",
                        "published_at": "2026-06-01T09:31:00Z",
                        "score": 0.5,
                        "sentiment": 0.25,
                    }
                ],
            }
        },
        ["AAPL"],
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert bundle.events
    assert "NEWS_PIPELINE source=benzinga articles=1" in caplog.text
    assert "NEWS_PIPELINE source=twitter posts=1 trusted_posts=1" in caplog.text
    assert "NEWS_PIPELINE source=newsapi calls=0 remaining_budget=" in caplog.text
    assert "NEWS_PIPELINE source=earnings_overnight calls=0 articles=0 status=ok" in caplog.text
    assert "EVENT_FEED symbol=AAPL source=benzinga" in caplog.text
    assert "EVENT_FEED symbol=AAPL source=twitter" in caplog.text


def test_fetch_alpaca_news_events_logs_request_and_result(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO)

    broker = MagicMock()
    broker.paper = False
    broker._news = object()
    broker.get_recent_news.return_value = [
        {"headline": "AAPL beats estimates", "symbols": ["AAPL"], "created_at": "2026-06-01T09:30:00Z"},
        {"headline": "MSFT guidance raised", "symbols": ["MSFT"], "created_at": "2026-06-01T09:31:00Z"},
        {"headline": "NVDA chip demand", "symbols": ["NVDA"], "created_at": "2026-06-01T09:32:00Z"},
        {"headline": "GOOGL ad growth", "symbols": ["GOOGL"], "created_at": "2026-06-01T09:33:00Z"},
        {"headline": "AMZN margin story", "symbols": ["AMZN"], "created_at": "2026-06-01T09:34:00Z"},
    ]

    result = pm.fetch_alpaca_news_events(
        ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN"],
        {"premarket_intelligence": {"alpaca_news_enabled": True}, "broker": {"paper": False}},
        10.0,
        market_client=broker,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert result.articles == 5
    assert "endpoint=broker.get_recent_news" in caplog.text
    assert "request_params=symbols=AAPL,MSFT,NVDA,GOOGL,AMZN" in caplog.text
    assert "http_status=200" in caplog.text
    assert "request_symbol_count=5" in caplog.text
    assert "returned_symbol_count=5" in caplog.text
    assert "raw_articles=5" in caplog.text
    assert "filtered_articles=5" in caplog.text
    assert "sample_titles=AAPL beats estimates | MSFT guidance raised | NVDA chip demand" in caplog.text
    assert "sample_symbols=AAPL,MSFT,NVDA,GOOGL,AMZN" in caplog.text
    assert "ALPACA_NEWS_RESULT status_code=200 raw_articles=5 filtered_articles=5" in caplog.text
    assert "ALPACA_NEWS_RAW_KEYS keys=" in caplog.text
    assert "ALPACA_NEWS_RAW title=AAPL beats estimates" in caplog.text


def test_fetch_alpaca_news_events_logs_empty_200_symbol_diagnostics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO)

    broker = MagicMock()
    broker.paper = False
    broker._news = object()
    broker.get_recent_news.return_value = []
    pm._PREMARKET_PROVIDER_CACHE.clear()

    result = pm.fetch_alpaca_news_events(
        [f"SYM{i}" for i in range(35)],
        {"premarket_intelligence": {"alpaca_news_enabled": True}, "broker": {"paper": False}},
        10.0,
        market_client=broker,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert result.http_status == 200
    assert result.raw_articles_before_filter == 0
    assert result.request_symbol_count == 35
    assert result.returned_symbol_count == 0
    assert result.sample_article_titles == []
    assert "request_symbol_count=35" in caplog.text
    assert "returned_symbol_count=0" in caplog.text
    assert "sample_titles=none" in caplog.text
    assert "ALPACA_NEWS_RESULT status_code=200 raw_articles=0 filtered_articles=0" in caplog.text


def test_fetch_alpaca_news_events_keeps_headline_only_symbol_match(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO)

    broker = MagicMock()
    broker.paper = False
    broker._news = object()
    broker.get_recent_news.return_value = [
        {"headline": "Apple rallies after guidance", "created_at": "2026-06-01T09:30:00Z"},
        {"title": "MSFT beats estimates", "created_at": "2026-06-01T09:31:00Z", "symbols": []},
    ]

    result = pm.fetch_alpaca_news_events(
        ["AAPL", "MSFT"],
        {"premarket_intelligence": {"alpaca_news_enabled": True}, "broker": {"paper": False}},
        10.0,
        market_client=broker,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert result.articles == 2
    assert "sample_symbols=AAPL,MSFT" in caplog.text
    assert "filtered_articles=2" in caplog.text


def test_fetch_alpaca_news_events_handles_multiple_response_shapes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO)

    @dataclass
    class ArticleData:
        title: str
        symbols: list[str]
        created_at: str

    class ModelDumpArticle:
        def __init__(self) -> None:
            self.title = "NVDA event"
            self.tickers = ["NVDA"]
            self.created_at = "2026-06-01T09:32:00Z"

        def model_dump(self):
            return {"title": self.title, "tickers": self.tickers, "created_at": self.created_at}

    class DictArticle:
        def __init__(self) -> None:
            self.title = "GOOGL event"
            self.symbols = ["GOOGL"]
            self.created_at = "2026-06-01T09:33:00Z"

        def dict(self):
            return {"title": self.title, "symbols": self.symbols, "created_at": self.created_at}

    class PlainArticle:
        def __init__(self) -> None:
            self.title = "AMZN event"
            self.entities = [{"symbol": "AMZN"}]
            self.created_at = "2026-06-01T09:34:00Z"

    broker = MagicMock()
    broker.paper = False
    broker._news = object()
    broker.get_recent_news.return_value = [
        ModelDumpArticle(),
        {"title": "AAPL event", "symbols": ["AAPL"], "created_at": "2026-06-01T09:30:00Z"},
        ArticleData(title="MSFT event", symbols=["MSFT"], created_at="2026-06-01T09:31:00Z"),
        DictArticle(),
        PlainArticle(),
    ]
    pm._PREMARKET_PROVIDER_CACHE.clear()

    result = pm.fetch_alpaca_news_events(
        ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN"],
        {"premarket_intelligence": {"alpaca_news_enabled": True}, "broker": {"paper": False}},
        10.0,
        market_client=broker,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert result.articles == 5
    assert "ALPACA_ARTICLE_TYPE type=ModelDumpArticle" in caplog.text
    assert "ALPACA_ARTICLE_TYPE type=dict" in caplog.text
    assert "ALPACA_ARTICLE_DICT_KEYS keys=created_at,tickers,title" in caplog.text
    assert "ALPACA_ARTICLE_DICT_KEYS keys=created_at,symbols,title" in caplog.text
    assert "sample_symbols=NVDA,AAPL,MSFT,GOOGL,AMZN" in caplog.text
    assert {ev.symbol for ev in result.events} == {"AAPL", "MSFT", "NVDA", "GOOGL", "AMZN"}


def test_fetch_alpaca_news_events_unwraps_tuple_response_container(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import logging

    caplog.set_level(logging.INFO)

    broker = MagicMock()
    broker.paper = False
    broker._news = object()
    broker.get_recent_news.return_value = (
        "data",
        {
            "news": [
                {"headline": "AAPL beats estimates", "symbols": ["AAPL"], "created_at": "2026-06-01T09:30:00Z"},
                {"headline": "MSFT guidance raised", "symbols": ["MSFT"], "created_at": "2026-06-01T09:31:00Z"},
                {"headline": "NVDA chip demand", "symbols": ["NVDA"], "created_at": "2026-06-01T09:32:00Z"},
                {"headline": "GOOGL ad growth", "symbols": ["GOOGL"], "created_at": "2026-06-01T09:33:00Z"},
                {"headline": "AMZN margin story", "symbols": ["AMZN"], "created_at": "2026-06-01T09:34:00Z"},
            ]
        },
    )
    monkeypatch.setattr(pm, "_provider_cache_get", lambda *_a, **_kw: None)
    monkeypatch.setattr(pm, "_provider_cache_set", lambda *_a, **_kw: None)

    result = pm.fetch_alpaca_news_events(
        ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN"],
        {"premarket_intelligence": {"alpaca_news_enabled": True}, "broker": {"paper": False}},
        10.0,
        market_client=broker,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert result.articles == 5
    assert "ALPACA_RESPONSE_TYPE type=tuple" in caplog.text
    assert "ALPACA_RESPONSE_KEYS keys=data,news" in caplog.text
    assert "ALPACA_NEWS_NORMALIZED_COUNT count=5" in caplog.text
    assert "ALPACA_NEWS_COUNT count=5" in caplog.text
    assert "ALPACA_NEWS_RESULT status_code=200 raw_articles=5 filtered_articles=5" in caplog.text


def test_fetch_alpaca_news_events_unwraps_list_of_tuple_response_container(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO)

    broker = MagicMock()
    broker.paper = False
    broker._news = object()
    broker.get_recent_news.return_value = [
        (
            "data",
            {
                "news": [
                    {
                        "headline": "HPE wins AI infrastructure deal",
                        "symbols": ["HPE"],
                        "created_at": "2026-06-01T09:30:00Z",
                    }
                ]
            },
        ),
        ("next_page_token", None),
    ]
    pm._PREMARKET_PROVIDER_CACHE.clear()

    result = pm.fetch_alpaca_news_events(
        ["HPE"],
        {"premarket_intelligence": {"alpaca_news_enabled": True}, "broker": {"paper": False}},
        10.0,
        market_client=broker,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert result.articles == 1
    assert result.events[0].symbol == "HPE"
    assert "ALPACA_RESPONSE_TYPE type=list" in caplog.text
    assert "ALPACA_RESPONSE_KEYS keys=0,1" in caplog.text
    assert "ALPACA_NEWS_NORMALIZED_COUNT count=1" in caplog.text
    assert "filtered_articles=1" in caplog.text


def test_fetch_newsapi_articles_limits_fallback_to_top_five_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _fake_query(query, api_key, **kwargs):
        calls.append(query)
        meta = kwargs.get("meta")
        if meta is not None:
            meta["request_sent"] = True
            meta["http_status"] = 200
        return []

    def _fake_top(query, api_key, **kwargs):
        calls.append(query)
        meta = kwargs.get("meta")
        if meta is not None:
            meta["request_sent"] = True
            meta["http_status"] = 200
        return []

    monkeypatch.setenv("NEWSAPI_KEY", "secret-key")
    monkeypatch.setattr(pm, "fetch_articles_query", _fake_query)
    monkeypatch.setattr(pm, "fetch_top_headlines_query", _fake_top)
    pm._NEWSAPI_QUERY_CACHE.clear()
    pm._NEWSAPI_DAILY_CALLS.clear()

    result = fetch_newsapi_articles(
        ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA"],
        {"premarket_intelligence": {"newsapi_enabled": True}, "news_sentiment": {"enabled": True}},
        10.0,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert result.requests_made <= 5
    joined = " ".join(calls)
    assert "Meta" not in joined
    assert "Tesla" not in joined
    assert "AI OR earnings OR guidance OR acquisition OR partnership OR FDA OR contract" in joined


def test_merge_premarket_events_dedupes_headline() -> None:
    events = merge_premarket_events(
        [
            NewsEvent("AAPL", "Beat", "newsapi", url="http://a"),
            NewsEvent("AAPL", "Beat", "alpaca", url="http://a"),
        ]
    )
    assert len(events) == 1


def test_build_newsapi_queries_skips_etfs_and_uses_company_names() -> None:
    batches = build_newsapi_query_batches(["SPY", "NVDA", "AAPL"], {})
    assert len(batches) == 1
    assert all("SPY" not in batch.symbols for batch in batches)
    assert batches[0].symbols == ("NVDA", "AAPL")
    assert batches[0].query == "(Nvidia OR NVDA) OR (Apple OR AAPL)"


def test_operating_company_symbols_excludes_etfs_and_keeps_core_equities() -> None:
    symbols = [
        "SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLY", "SMH",
        "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "AVGO", "AMD",
        "ORCL", "NFLX", "PLTR", "CRWD", "DDOG", "NOW", "SNOW", "UBER",
        "SHOP", "SQ", "ARM", "MU", "ANET", "MRVL", "SMCI", "TSM", "JPM",
        "GS", "LLY",
    ]

    filtered = pm._operating_company_symbols(symbols)

    assert filtered == [
        "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "AVGO", "AMD",
        "ORCL", "NFLX", "PLTR", "CRWD", "DDOG", "NOW", "SNOW", "UBER",
        "SHOP", "SQ", "ARM", "MU", "ANET", "MRVL", "SMCI", "TSM", "JPM",
        "GS", "LLY",
    ]


def test_execute_premarket_newsapi_filters_etfs_before_fallback_cap(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO)
    captured: dict[str, tuple[str, ...]] = {}

    def _newsapi(symbols, *_args, **_kwargs):
        captured["symbols"] = tuple(symbols)
        return ProviderExecResult(
            provider="newsapi",
            request_sent=True,
            request_symbol_count=len(symbols),
            returned_symbol_count=0,
            articles=0,
        )

    monkeypatch.setattr(pm, "fetch_newsapi_articles", _newsapi)
    monkeypatch.setattr(pm, "fetch_alpaca_news_events", lambda *a, **k: ProviderExecResult(provider="alpaca"))
    monkeypatch.setattr(pm, "fetch_sec_filings", lambda *a, **k: ProviderExecResult(provider="sec"))
    monkeypatch.setattr(pm, "fetch_benzinga_events", lambda *a, **k: ProviderExecResult(provider="benzinga"))
    monkeypatch.setattr(pm, "fetch_twitter_trusted_events", lambda *a, **k: ProviderExecResult(provider="twitter"))
    monkeypatch.setattr(pm, "fetch_overnight_earnings_events", lambda *a, **k: ProviderExecResult(provider="earnings_overnight"))

    execute_premarket_providers(
        {"premarket_intelligence": {"newsapi_fallback_top_n": 5}},
        ["SPY", "QQQ", "IWM", "XLK", "XLF", "AAPL", "MSFT", "NVDA", "AMZN", "META"],
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert captured["symbols"] == ("AAPL", "MSFT", "NVDA", "AMZN", "META")
    assert "PREMARKET_UNIVERSE base_symbols=10 candidate_symbols=10" in caplog.text


def test_build_newsapi_query_batches_groups_symbols() -> None:
    symbols = [
        "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AMD", "AVGO", "MU", "SMCI", "ARM",
    ]
    batches = build_newsapi_query_batches(symbols, {})
    assert len(batches) == 6
    for batch in batches:
        assert 1 <= len(batch.symbols) <= 2
        assert len(batch.query) < 200
        assert " OR " in batch.query


def test_fetch_newsapi_batches_broad_queries_and_stops_on_429(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO)
    calls: list[str] = []

    def _fake_query(query, api_key, **kwargs):
        calls.append(query)
        meta = kwargs.get("meta")
        if meta is not None:
            meta["request_sent"] = True
            meta["http_status"] = 429 if len(calls) == 1 else 200
            if len(calls) == 1:
                meta["error"] = "429 Too Many Requests"
                raise pm.NewsAPIRateLimitError("rate limited")
        return [
            {
                "title": "NVDA beats estimates",
                "symbols": ["NVDA"],
                "publishedAt": "2026-06-01T10:00:00Z",
            }
        ]

    monkeypatch.setenv("NEWSAPI_KEY", "secret-key")
    monkeypatch.setattr(pm, "fetch_articles_query", _fake_query)
    pm._NEWSAPI_QUERY_CACHE.clear()
    pm._NEWSAPI_DAILY_CALLS.clear()

    result = fetch_newsapi_articles(
        ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META"],
        {"premarket_intelligence": {"newsapi_enabled": True}, "news_sentiment": {"enabled": True}},
        10.0,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert len(calls) == 1
    assert result.request_sent is True
    assert result.articles == 0
    assert "NEWS_FETCH_START provider=NewsAPI" in caplog.text
    assert caplog.text.count("NEWSAPI_RATE_LIMITED_ONCE") == 1
    assert "NEWS_FETCH_ERROR exception=" not in caplog.text
    assert "NEWS_FETCH_RESULT provider=NewsAPI status_code=429" in caplog.text
    assert "NEWS_FETCH_FILTER before_count=0 after_count=0" in caplog.text


def test_fetch_newsapi_articles_with_three_results_produces_pipeline_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _fake_query(query, api_key, **kwargs):
        calls.append(query)
        meta = kwargs.get("meta")
        if meta is not None:
            meta["request_sent"] = True
            meta["http_status"] = 200
        if len(calls) > 1:
            return []
        return [
            {"title": "Apple raises guidance", "symbols": ["AAPL"], "publishedAt": "2026-06-01T10:00:00Z"},
            {"title": "Microsoft beats estimates", "symbols": ["MSFT"], "publishedAt": "2026-06-01T10:05:00Z"},
            {"title": "Nvidia acquisition chatter", "symbols": ["NVDA"], "publishedAt": "2026-06-01T10:10:00Z"},
        ]

    monkeypatch.setenv("NEWSAPI_KEY", "secret-key")
    monkeypatch.setattr(pm, "fetch_articles_query", _fake_query)

    result = fetch_newsapi_articles(
        ["AAPL", "MSFT", "NVDA"],
        {"premarket_intelligence": {"newsapi_enabled": True}, "news_sentiment": {"enabled": True}},
        10.0,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert calls
    assert result.raw_articles_before_filter == 3
    assert result.articles_after_filter > 0
    assert result.articles > 0
    assert result.request_symbol_count == 3
    assert result.returned_symbol_count == 3
    assert result.sample_article_titles == [
        "Apple raises guidance",
        "Microsoft beats estimates",
        "Nvidia acquisition chatter",
    ]


def test_fetch_newsapi_articles_uses_broad_fallback_when_symbol_batches_empty(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO)
    calls: list[str] = []

    def _fake_query(query, api_key, **kwargs):
        calls.append(query)
        meta = kwargs.get("meta")
        if meta is not None:
            meta["request_sent"] = True
            meta["http_status"] = 200
        if query == pm.NEWSAPI_CATALYST_FALLBACK_QUERY:
            return [
                {
                    "title": "Nvidia announces AI contract",
                    "description": "Nvidia wins a new enterprise AI contract.",
                    "publishedAt": "2026-06-01T10:00:00Z",
                }
            ]
        return []

    monkeypatch.setenv("NEWSAPI_KEY", "secret-key")
    monkeypatch.setattr(pm, "fetch_articles_query", _fake_query)
    pm._NEWSAPI_QUERY_CACHE.clear()
    pm._NEWSAPI_DAILY_CALLS.clear()

    result = fetch_newsapi_articles(
        ["NVDA"],
        {"premarket_intelligence": {"newsapi_enabled": True}, "news_sentiment": {"enabled": True}},
        10.0,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert calls[-1] == pm.NEWSAPI_CATALYST_FALLBACK_QUERY
    assert result.articles == 1
    assert result.events[0].symbol == "NVDA"
    assert "NEWS_FETCH_FALLBACK_START provider=NewsAPI route=broad_catalyst_everything" in caplog.text
    assert "NEWSAPI_RAW_ARTICLES provider=NewsAPI route=broad_catalyst_everything" in caplog.text


def test_fetch_newsapi_articles_uses_top_headlines_when_symbol_search_zero(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO)
    everything_calls: list[str] = []
    top_calls: list[str] = []

    def _fake_everything(query, api_key, **kwargs):
        everything_calls.append(query)
        meta = kwargs.get("meta")
        if meta is not None:
            meta["request_sent"] = True
            meta["http_status"] = 200
        return []

    def _fake_top(query, api_key, **kwargs):
        top_calls.append(query)
        assert "lookback_hours" not in kwargs
        assert "from" not in kwargs
        assert "to" not in kwargs
        meta = kwargs.get("meta")
        if meta is not None:
            meta["request_sent"] = True
            meta["http_status"] = 200
            meta["endpoint"] = "https://newsapi.org/v2/top-headlines"
        return [
            {
                "title": "Apple raises guidance",
                "description": "Apple lifted its guidance.",
                "publishedAt": "2026-06-01T10:00:00Z",
            }
        ]

    monkeypatch.setenv("NEWSAPI_KEY", "secret-key")
    monkeypatch.setattr(pm, "fetch_articles_query", _fake_everything)
    monkeypatch.setattr(pm, "fetch_top_headlines_query", _fake_top)
    pm._NEWSAPI_QUERY_CACHE.clear()
    pm._NEWSAPI_DAILY_CALLS.clear()

    result = fetch_newsapi_articles(
        ["AAPL"],
        {"premarket_intelligence": {"newsapi_enabled": True}, "news_sentiment": {"enabled": True}},
        10.0,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert everything_calls
    assert top_calls == [pm.NEWSAPI_CATALYST_FALLBACK_QUERY]
    assert result.articles == 1
    assert result.events[0].symbol == "AAPL"
    assert "NEWS_FETCH_FALLBACK_START provider=NewsAPI route=top_headlines endpoint=top-headlines" in caplog.text


def test_build_overnight_earnings_query_batches_per_symbol() -> None:
    batches = build_overnight_earnings_query_batches(["NVDA", "AAPL", "MSFT", "GOOGL"], {})
    assert len(batches) == 4
    assert batches[0].query == '"Nvidia" earnings'
    assert batches[1].query == '"Apple" earnings'
    for batch in batches:
        assert len(batch.symbols) == 1
        assert len(batch.query) < 200


def test_build_overnight_earnings_query_includes_keywords() -> None:
    query = build_overnight_earnings_query(["NVDA", "AAPL"], {})
    assert query == '"Nvidia" earnings'


def test_fetch_overnight_earnings_batched_continues_on_400(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO)
    calls: list[str] = []

    def _fake_query(query, api_key, **kwargs):
        calls.append(query)
        meta = kwargs.get("meta")
        if meta is not None:
            meta["request_sent"] = True
            meta["http_status"] = 400 if len(calls) == 1 else 200
            if len(calls) == 1:
                meta["error"] = "400 Client Error: Bad Request"
        if len(calls) == 1:
            return []
        return [
            {
                "title": "MSFT beats after hours",
                "url": "http://example.com/msft-earnings",
                "publishedAt": "2026-06-01T10:00:00Z",
            }
        ]

    monkeypatch.setenv("NEWSAPI_KEY", "secret-key")
    monkeypatch.setattr(pm, "fetch_articles_query", _fake_query)

    result = fetch_overnight_earnings_events(
        ["AAPL", "MSFT", "NVDA", "GOOGL"],
        {
            "premarket_intelligence": {
                "overnight_earnings_enabled": True,
                "overnight_earnings_batch_size": 3,
            },
            "news_sentiment": {"enabled": True},
        },
        10.0,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert len(calls) == 4
    assert result.request_sent is True
    assert result.articles >= 1
    assert "PREMARKET_NEWS_QUERY symbol=AAPL query=" in caplog.text
    assert "provider=earnings_overnight batch=1" in caplog.text
    assert "query_length=" in caplog.text
    assert "error=bad_request" in caplog.text
    assert "provider=earnings_overnight batch=2" in caplog.text
    assert "company_name_used=true" in caplog.text


def test_fetch_earnings_overnight_caps_symbols_and_stops_after_429(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO)
    calls: list[str] = []

    def _fake_query(query, api_key, **kwargs):
        calls.append(query)
        meta = kwargs.get("meta")
        if meta is not None:
            meta["request_sent"] = True
            meta["http_status"] = 429
            meta["error"] = "429 Too Many Requests"
            meta["rate_limit_headers"] = {"Retry-After": "60", "X-RateLimit-Remaining": "0"}
        raise pm.NewsAPIRateLimitError("rate limited")

    monkeypatch.setenv("NEWSAPI_KEY", "secret-key")
    monkeypatch.setattr(pm, "fetch_articles_query", _fake_query)
    pm._NEWSAPI_QUERY_CACHE.clear()
    pm._NEWSAPI_DAILY_CALLS.clear()
    pm._PREMARKET_PROVIDER_CACHE.clear()

    result = fetch_overnight_earnings_events(
        ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA"],
        {
            "premarket_intelligence": {
                "overnight_earnings_enabled": True,
                "overnight_earnings_lookback_hours": 14,
            },
            "news_sentiment": {"enabled": True},
        },
        10.0,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert len(calls) == 1
    assert result.http_status == 429
    assert result.requests_made == 1
    assert result.rate_limit_headers == {"Retry-After": "60", "X-RateLimit-Remaining": "0"}
    assert caplog.text.count("NEWSAPI_RATE_LIMITED_ONCE") == 1
    assert "rate_limit_headers=Retry-After:60,X-RateLimit-Remaining:0" in caplog.text
    assert "status=rate_limited" in caplog.text


def test_execute_providers_skips_remaining_newsapi_calls_after_429(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO)
    newsapi_calls: list[str] = []
    overnight_calls = {"n": 0}

    def _fake_query(query, api_key, **kwargs):
        newsapi_calls.append(query)
        meta = kwargs.get("meta")
        if meta is not None:
            meta["request_sent"] = True
            meta["http_status"] = 429
            meta["error"] = "429 Too Many Requests"
        raise pm.NewsAPIRateLimitError("rate limited")

    def _overnight(*args, **kwargs):
        overnight_calls["n"] += 1
        raise AssertionError("overnight NewsAPI calls should be skipped after 429")

    monkeypatch.setenv("NEWSAPI_KEY", "secret-key")
    monkeypatch.setattr(pm, "fetch_articles_query", _fake_query)
    monkeypatch.setattr(
        pm,
        "fetch_alpaca_news_events",
        lambda *a, **k: ProviderExecResult(provider="alpaca", articles=1, events=[
            NewsEvent(symbol="AAPL", headline="AAPL product launch", source="alpaca", score=4.0)
        ]),
    )
    monkeypatch.setattr(
        pm,
        "fetch_sec_filings",
        lambda *a, **k: ProviderExecResult(provider="sec", filings=0),
    )
    monkeypatch.setattr(
        pm,
        "fetch_benzinga_events",
        lambda *a, **k: ProviderExecResult(provider="benzinga"),
    )
    monkeypatch.setattr(
        pm,
        "fetch_twitter_trusted_events",
        lambda *a, **k: ProviderExecResult(provider="twitter"),
    )
    monkeypatch.setattr(pm, "fetch_overnight_earnings_events", _overnight)
    pm._NEWSAPI_QUERY_CACHE.clear()
    pm._NEWSAPI_DAILY_CALLS.clear()
    pm._PREMARKET_PROVIDER_CACHE.clear()

    result = execute_premarket_providers(
        {
            "premarket_intelligence": {"newsapi_enabled": True},
            "news_sentiment": {"enabled": True},
        },
        ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META"],
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert len(newsapi_calls) == 1
    assert overnight_calls["n"] == 0
    assert result.newsapi.http_status == 429
    assert result.overnight_earnings is not None
    assert result.overnight_earnings.skip_reason == "depends_on_newsapi_rate_limited"
    assert result.alpaca.articles == 1
    assert caplog.text.count("NEWSAPI_RATE_LIMITED_ONCE") == 1


def test_job_scoped_newsapi_rate_limit_logs_once_across_code_paths(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO)
    calls: list[str] = []
    job_state: dict[str, bool] = {}

    def _fake_query(query, api_key, **kwargs):
        calls.append(query)
        meta = kwargs.get("meta")
        if meta is not None:
            meta["request_sent"] = True
            meta["http_status"] = 429
            meta["error"] = "429 Too Many Requests"
        raise pm.NewsAPIRateLimitError("rate limited")

    monkeypatch.setenv("NEWSAPI_KEY", "secret-key")
    monkeypatch.setattr(pm, "fetch_articles_query", _fake_query)
    pm._NEWSAPI_QUERY_CACHE.clear()
    pm._NEWSAPI_DAILY_CALLS.clear()
    pm._PREMARKET_PROVIDER_CACHE.clear()

    newsapi = fetch_newsapi_articles(
        ["AAPL", "MSFT", "NVDA", "GOOGL"],
        {"premarket_intelligence": {"newsapi_enabled": True}, "news_sentiment": {"enabled": True}},
        10.0,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        rate_limit_log_state=job_state,
    )
    overnight = fetch_overnight_earnings_events(
        ["AAPL", "MSFT", "NVDA", "GOOGL"],
        {
            "premarket_intelligence": {
                "newsapi_enabled": True,
                "overnight_earnings_enabled": True,
            },
            "news_sentiment": {"enabled": True},
        },
        10.0,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        rate_limit_log_state=job_state,
    )

    assert newsapi.http_status == 429
    assert overnight.request_sent is False
    assert overnight.skip_reason == "depends_on_newsapi_rate_limited"
    assert job_state["newsapi_rate_limited"] is True
    assert len(calls) == 1
    assert caplog.text.count("NEWSAPI_RATE_LIMITED_ONCE") == 1
    assert caplog.text.count("http_status=429") == 1


def test_earnings_overnight_rate_limit_dependency_reason_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEWSAPI_KEY", "secret-key")
    overnight = fetch_overnight_earnings_events(
        ["AAPL", "MSFT"],
        {
            "premarket_intelligence": {
                "newsapi_enabled": True,
                "overnight_earnings_enabled": True,
            },
            "news_sentiment": {"enabled": True},
        },
        10.0,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        rate_limit_log_state={"newsapi_rate_limited": True},
    )

    assert overnight.request_sent is False
    assert overnight.skip_reason == "depends_on_newsapi_rate_limited"


def test_earnings_overnight_disabled_when_newsapi_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEWSAPI_KEY", "secret-key")
    monkeypatch.setattr(
        pm,
        "fetch_articles_query",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("NewsAPI should be skipped")),
    )

    overnight = fetch_overnight_earnings_events(
        ["AAPL", "MSFT"],
        {
            "premarket_intelligence": {
                "newsapi": {"enabled": False},
                "newsapi_enabled": True,
                "overnight_earnings_enabled": True,
            },
            "news_sentiment": {"enabled": True},
        },
        10.0,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert overnight.enabled is True
    assert overnight.request_sent is False
    assert overnight.skip_reason == "depends_on_newsapi_disabled"


def test_fetch_earnings_overnight_caps_to_five_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _fake_query(query, api_key, **kwargs):
        calls.append(query)
        meta = kwargs.get("meta")
        if meta is not None:
            meta["request_sent"] = True
            meta["http_status"] = 200
        return []

    monkeypatch.setenv("NEWSAPI_KEY", "secret-key")
    monkeypatch.setattr(pm, "fetch_articles_query", _fake_query)
    pm._NEWSAPI_QUERY_CACHE.clear()
    pm._NEWSAPI_DAILY_CALLS.clear()
    pm._PREMARKET_PROVIDER_CACHE.clear()

    result = fetch_overnight_earnings_events(
        ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA"],
        {
            "premarket_intelligence": {
                "overnight_earnings_enabled": True,
                "overnight_earnings_lookback_hours": 14,
            },
            "news_sentiment": {"enabled": True},
        },
        10.0,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert len(calls) == 5
    assert result.requests_made == 5


def test_build_symbol_search_query_uses_company_name_only() -> None:
    from src.premarket_intelligence import _build_symbol_search_query

    api_query, display, used = _build_symbol_search_query("NVDA", "guidance", {})
    assert api_query == '"Nvidia" guidance'
    assert display == "Nvidia guidance"
    assert used is True


def test_execute_premarket_providers_ranks_sec_only_events_without_articles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _newsapi(*_a, **_k):
        return ProviderExecResult(provider="newsapi", request_sent=True, articles=0)

    def _alpaca(*_a, **_k):
        return ProviderExecResult(provider="alpaca", request_sent=True, articles=0)

    def _sec(*_a, **_k):
        return ProviderExecResult(
            provider="sec",
            request_sent=True,
            filings=3,
            events=[
                NewsEvent(
                    "GOOGL",
                    "SEC filing 8-K",
                    "sec",
                    form="8-K",
                    published_at="2026-06-01",
                    accession="0001652044-26-000001",
                    url="https://www.sec.gov/Archives/edgar/data/1652044/example-8k.htm",
                    rankable=True,
                    score=7.2,
                ),
                NewsEvent(
                    "GOOGL",
                    "SEC filing CERT",
                    "sec",
                    form="CERT",
                    published_at="2026-06-01",
                    accession="0001652044-26-000002",
                    url="https://www.sec.gov/Archives/edgar/data/1652044/example-cert.htm",
                    rankable=True,
                    score=4.0,
                ),
                NewsEvent(
                    "GOOGL",
                    "SEC filing 424B5",
                    "sec",
                    form="424B5",
                    published_at="2026-06-01",
                    accession="0001652044-26-000003",
                    url="https://www.sec.gov/Archives/edgar/data/1652044/example-424b5.htm",
                    rankable=True,
                    score=1.5,
                ),
            ],
        )

    def _overnight(*_a, **_k):
        return ProviderExecResult(provider="earnings_overnight", request_sent=True, articles=0)

    monkeypatch.setattr(pm, "fetch_newsapi_articles", _newsapi)
    monkeypatch.setattr(pm, "fetch_alpaca_news_events", _alpaca)
    monkeypatch.setattr(pm, "fetch_sec_filings", _sec)
    monkeypatch.setattr(pm, "fetch_overnight_earnings_events", _overnight)

    bundle = execute_premarket_providers({}, ["GOOGL"], now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc))
    assert bundle.news_article_count == 0
    assert bundle.filings_count == 3
    assert "GOOGL" in bundle.catalysts
    assert bundle.catalysts["GOOGL"].source == "sec"
    assert bundle.catalysts["GOOGL"].catalyst_type == "sec_filing"
    assert bundle.rankings
    assert bundle.rankings[0].symbol == "GOOGL"
    assert bundle.rankings[0].source == "sec_filing"
    assert bundle.rankings[0].form == "8-K"


def test_fetch_sec_filings_logs_per_filing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO)
    submissions = {
        "filings": {
            "recent": {
                "form": ["8-K"],
                "filingDate": ["2026-06-01"],
                "accessionNumber": ["0000320193-26-000001"],
                "primaryDocument": ["aapl-8k.htm"],
            }
        }
    }
    tickers = {"0": {"cik_str": 320193, "ticker": "AAPL"}}

    class FakeResp:
        def __init__(self, data: dict, status: int = 200) -> None:
            self._data = data
            self.status_code = status

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._data

    def fake_get(url: str, **kwargs):
        if "company_tickers" in url:
            return FakeResp(tickers)
        if "submissions" in url:
            return FakeResp(submissions)
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(pm.requests, "get", fake_get)

    result = fetch_sec_filings(
        ["AAPL"],
        {
            "premarket_intelligence": {"sec_filings_enabled": True},
            "news_sentiment": {"headline_lookback_hours": 24},
        },
        10.0,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert result.filings == 1
    assert result.events[0].form == "8-K"
    assert result.events[0].primary_doc == "aapl-8k.htm"
    assert "sec.gov" in result.events[0].url
    assert "PREMARKET_SEC_FILING symbol=AAPL" in caplog.text
    assert "cik=0000320193" in caplog.text
    assert "form=8-K" in caplog.text
    assert "filing_date=2026-06-01" in caplog.text
    assert "accession=0000320193-26-000001" in caplog.text
    assert "primary_doc=aapl-8k.htm" in caplog.text
    assert "url=https://www.sec.gov/Archives/edgar/data/" in caplog.text
    assert "routine=false rankable=true" in caplog.text


def test_fetch_sec_filings_sd_logged_not_ranked(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO)
    submissions = {
        "filings": {
            "recent": {
                "form": ["SD"],
                "filingDate": ["2026-06-01"],
                "accessionNumber": ["0001652044-26-000001"],
                "primaryDocument": ["goog-sd.htm"],
            }
        }
    }
    tickers = {"0": {"cik_str": 1652044, "ticker": "GOOGL"}}

    class FakeResp:
        def __init__(self, data: dict, status: int = 200) -> None:
            self._data = data
            self.status_code = status

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._data

    def fake_get(url: str, **kwargs):
        if "company_tickers" in url:
            return FakeResp(tickers)
        if "submissions" in url:
            return FakeResp(submissions)
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(pm.requests, "get", fake_get)

    cfg = {
        "premarket_intelligence": {
            "sec_filings_enabled": True,
            "include_routine_sec_filings": False,
        },
        "news_sentiment": {"headline_lookback_hours": 24},
    }
    result = fetch_sec_filings(
        ["GOOGL"],
        cfg,
        10.0,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert result.filings == 1
    assert result.events[0].form == "SD"
    assert result.events[0].rankable is False
    assert "PREMARKET_SEC_FILING symbol=GOOGL cik=0001652044 form=SD" in caplog.text
    assert "routine=true rankable=false" in caplog.text
    rankings = build_premarket_rankings(["GOOGL"], catalysts={}, events=result.events, cfg=cfg)
    assert rankings == []


def test_build_premarket_rankings_sec_entry_has_filing_fields() -> None:
    events = [
        NewsEvent(
            "AAPL",
            "SEC filing 8-K",
            "sec",
            form="8-K",
            accession="0000320193-26-000001",
            published_at="2026-06-01",
            url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/aapl-8k.htm",
            rank_reason="sec_filing",
        ),
    ]
    rankings = build_premarket_rankings(["AAPL"], catalysts={}, events=events, cfg={})
    assert len(rankings) == 1
    row = rankings[0]
    assert row.symbol == "AAPL"
    assert row.score == 2.5
    assert row.catalyst_type == "sec_filing"
    assert row.source == "sec_filing"
    assert row.form == "8-K"
    assert row.reason == "SEC material event filing"
    assert row.filing_date == "2026-06-01"
    assert row.accession == "0000320193-26-000001"
    assert "sec.gov" in row.url


def test_build_premarket_rankings_merges_sec_news_and_earnings() -> None:
    events = [
        NewsEvent(
            "NVDA",
            "SEC filing 8-K",
            "sec",
            form="8-K",
            accession="0001",
            published_at="2026-06-01",
            rank_reason="sec_filing",
        ),
        NewsEvent(
            "AAPL",
            "AAPL beats after hours",
            "earnings_overnight",
            catalyst_type="earnings",
            rank_reason="earnings_overnight",
        ),
    ]
    cat = MagicMock(score=3.5, headline="NVDA analyst upgrade", catalyst_type="upgrade", source="newsapi")
    rankings = build_premarket_rankings(["NVDA", "AAPL"], catalysts={"NVDA": cat}, events=events, cfg={})
    assert len(rankings) == 2
    nvda = next(row for row in rankings if row.symbol == "NVDA")
    assert nvda.reason == "analyst headline"
    assert nvda.catalyst_type == "analyst"
    assert any(row.symbol == "AAPL" and row.source == "earnings" for row in rankings)


def test_build_premarket_rankings_classifies_headlines_explicitly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO)
    events = [
        NewsEvent("AAPL", "Apple raises guidance after quarter", "newsapi"),
        NewsEvent("MSFT", "Microsoft analyst upgrades target", "alpaca"),
        NewsEvent("AMD", "AMD and OpenAI announce partnership", "alpaca"),
        NewsEvent("NVDA", "Nvidia unveils AI product line", "alpaca"),
        NewsEvent("BABA", "Alibaba says hello", "alpaca"),
    ]

    rankings = build_premarket_rankings(["AAPL", "MSFT", "AMD", "NVDA", "BABA"], catalysts={}, events=events)
    mapping = {row.symbol: row for row in rankings}

    assert mapping["AAPL"].source == "guidance"
    assert mapping["MSFT"].source == "analyst"
    assert mapping["AMD"].source == "ai"
    assert mapping["NVDA"].source == "ai"
    assert mapping["BABA"].source == "unknown"
    assert "CATALYST_CLASSIFIED symbol=AAPL headline=Apple raises guidance after quarter type=guidance" in caplog.text
    assert "CATALYST_CLASSIFIED symbol=MSFT headline=Microsoft analyst upgrades target type=analyst" in caplog.text
    assert "CATALYST_CLASSIFIED symbol=AMD headline=AMD and OpenAI announce partnership type=ai" in caplog.text
    assert "CATALYST_CLASSIFIED symbol=NVDA headline=Nvidia unveils AI product line type=ai" in caplog.text
    assert "CATALYST_CLASSIFIED symbol=BABA headline=Alibaba says hello type=unknown" in caplog.text


def test_avgo_earnings_outranks_amd_form_144() -> None:
    events = [
        NewsEvent("AVGO", "Broadcom beats earnings and raises outlook", "newsapi"),
        NewsEvent("AMD", "SEC filing 144", "sec", form="144", rankable=True),
    ]

    rankings = build_premarket_rankings(["AVGO", "AMD"], catalysts={}, events=events, cfg={})

    assert [row.symbol for row in rankings[:2]] == ["AVGO", "AMD"]
    amd = next(row for row in rankings if row.symbol == "AMD")
    assert amd.score == 1.0
    assert amd.confidence < 0
    assert amd.catalyst_type == "form_144"


def test_crwd_analyst_outranks_googl_424b5() -> None:
    events = [
        NewsEvent("CRWD", "CrowdStrike upgraded by analyst after channel checks", "alpaca"),
        NewsEvent("GOOGL", "SEC filing 424B5", "sec", form="424B5", rankable=True),
    ]

    rankings = build_premarket_rankings(["CRWD", "GOOGL"], catalysts={}, events=events, cfg={})

    assert [row.symbol for row in rankings[:2]] == ["CRWD", "GOOGL"]
    googl = next(row for row in rankings if row.symbol == "GOOGL")
    assert googl.score == 1.5
    assert googl.catalyst_type == "dilution_risk"


def test_msft_deal_outranks_fwp() -> None:
    events = [
        NewsEvent("MSFT", "Microsoft announces new Azure AI partnership deal", "newsapi"),
        NewsEvent("GOOGL", "SEC filing FWP", "sec", form="FWP", rankable=True),
    ]

    rankings = build_premarket_rankings(["MSFT", "GOOGL"], catalysts={}, events=events, cfg={})

    assert [row.symbol for row in rankings[:2]] == ["MSFT", "GOOGL"]
    fwp = next(row for row in rankings if row.symbol == "GOOGL")
    assert fwp.score == 1.0
    assert fwp.catalyst_type == "informational"


def test_form_4_is_not_rankable() -> None:
    events = [
        NewsEvent("MSFT", "SEC filing 4", "sec", form="4", rankable=True),
    ]

    assert build_premarket_rankings(["MSFT"], catalysts={}, events=events, cfg={}) == []


def test_premarket_rankings_use_tradability_gap_volume_and_recency() -> None:
    now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    events = [
        NewsEvent(
            "AAPL",
            "Apple beats earnings estimates",
            "newsapi",
            published_at="2026-05-31T20:00:00Z",
            gap_pct=1.0,
            volume_surge_pct=20.0,
        ),
        NewsEvent(
            "MSFT",
            "Microsoft analyst upgrades stock",
            "alpaca",
            published_at="2026-06-01T09:45:00Z",
            gap_pct=12.0,
            volume_surge_pct=350.0,
        ),
    ]

    rankings = build_premarket_rankings(["AAPL", "MSFT"], catalysts={}, events=events, now=now)

    assert [row.symbol for row in rankings] == ["MSFT", "AAPL"]
    assert rankings[0].score > rankings[1].score


def test_top_rankings_are_actionable_not_administrative_sec_filings() -> None:
    now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    events = [
        NewsEvent("AVGO", "Broadcom beats earnings estimates", "newsapi", published_at="2026-06-01T09:50:00Z", gap_pct=6, volume_surge_pct=250),
        NewsEvent("CRWD", "CrowdStrike analyst upgrade after strong checks", "alpaca", published_at="2026-06-01T09:45:00Z", gap_pct=5, volume_surge_pct=200),
        NewsEvent("MSFT", "Microsoft announces OpenAI partnership", "newsapi", published_at="2026-06-01T09:40:00Z", gap_pct=4, volume_surge_pct=180),
        NewsEvent("AMD", "SEC filing 144", "sec", form="144", rankable=True),
        NewsEvent("GOOGL", "SEC filing 424B5", "sec", form="424B5", rankable=True),
        NewsEvent("META", "SEC filing FWP", "sec", form="FWP", rankable=True),
        NewsEvent("AAPL", "SEC filing 8-K", "sec", form="8-K", rankable=True),
    ]

    rankings = build_premarket_rankings(
        ["AVGO", "CRWD", "MSFT", "AMD", "GOOGL", "META", "AAPL"],
        catalysts={},
        events=events,
        now=now,
    )

    assert [row.symbol for row in rankings[:3]] == ["AVGO", "CRWD", "MSFT"]
    assert all(row.source != "sec_filing" for row in rankings[:3])
    assert all(row.score < rankings[2].score for row in rankings if row.source == "sec_filing")


def test_etf_ai_news_ranks_below_single_stock_ai_news(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    caplog.set_level(logging.INFO)
    now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    events = [
        NewsEvent("DIA", "AI partnership expands enterprise platform", "newsapi", published_at="2026-06-01T09:50:00Z", gap_pct=5, volume_surge_pct=200),
        NewsEvent("GOOG", "AI partnership expands enterprise platform", "newsapi", published_at="2026-06-01T09:50:00Z", gap_pct=5, volume_surge_pct=200),
        NewsEvent("NVDA", "AI partnership expands enterprise platform", "alpaca", published_at="2026-06-01T09:50:00Z", gap_pct=5, volume_surge_pct=200),
    ]

    rankings = build_premarket_rankings(["DIA", "GOOG", "NVDA"], catalysts={}, events=events, now=now)
    symbols = [row.symbol for row in rankings]

    assert symbols.index("DIA") > symbols.index("GOOG")
    assert symbols.index("DIA") > symbols.index("NVDA")
    dia = next(row for row in rankings if row.symbol == "DIA")
    goog = next(row for row in rankings if row.symbol == "GOOG")
    assert dia.score < goog.score
    assert dia.score == pytest.approx(goog.score * 0.5)
    assert "PREMARKET_RANK_ADJUSTMENT symbol=DIA" in caplog.text
    assert "etf_multiplier=0.5" in caplog.text


def test_ai_news_ranking_boosts_high_quality_catalyst() -> None:
    now = datetime(2026, 6, 5, 12, tzinfo=timezone.utc)
    events = [
        NewsEvent(
            "AAAA",
            "AAAA signs AI infrastructure partnership worth $2 billion",
            "alpaca",
            rank_source="deal",
            sentiment=0.6,
            published_at="2026-06-05T11:00:00Z",
        ),
        NewsEvent(
            "BBBB",
            "BBBB stock moves on vague AI rumor",
            "newsapi",
            rank_source="ai",
            sentiment=0.0,
            published_at="2026-06-03T11:00:00Z",
        ),
    ]

    rankings = build_premarket_rankings(
        ["AAAA", "BBBB"],
        catalysts={},
        events=events,
        cfg={"ai_news_ranking": {"enabled": True, "score_weight": 2.0}},
        now=now,
    )

    assert [row.symbol for row in rankings] == ["AAAA", "BBBB"]
    assert rankings[0].news_quality is not None
    assert rankings[0].catalyst_strength is not None
    assert rankings[0].ai_confidence is not None
    assert rankings[0].score > rankings[1].score


def test_premarket_rank_json_includes_ai_news_scores(tmp_path) -> None:
    path = tmp_path / "rankings.json"
    rows = [
        PremarketRankEntry(
            "AAAA",
            8.5,
            "deal",
            "deal",
            0.88,
            "deal headline",
            news_quality=0.91,
            catalyst_strength=0.84,
            ai_confidence=0.86,
        )
    ]

    pm.write_premarket_rank_json(path, rows, now=datetime(2026, 6, 5, tzinfo=timezone.utc))

    payload = json.loads(path.read_text(encoding="utf-8"))
    item = payload["items"][0]
    assert item["news_quality"] == 0.91
    assert item["catalyst_strength"] == 0.84
    assert item["ai_confidence"] == 0.86


def test_log_premarket_rankings_emits_sec_rank_and_top10(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    caplog.set_level(logging.INFO)
    rows = [
        PremarketRankEntry(
            "AAPL",
            7.2,
            "sec_filing",
            "sec_filing",
            0.72,
            "SEC material event filing",
            form="8-K",
        ),
        PremarketRankEntry("NVDA", 3.5, "analyst", "analyst", 0.87, "analyst headline"),
    ]
    log_premarket_rankings(rows, top_n=10)
    assert "PREMARKET_RANK symbol=AAPL score=7.20 source=sec_filing catalyst_type=sec_filing form=8-K" in caplog.text
    assert "PREMARKET_RANK symbol=NVDA score=3.50 source=analyst" in caplog.text
    assert "PREMARKET_TOP10 rank=1 symbol=AAPL" in caplog.text


def test_log_premarket_rankings_emits_rank_and_top10(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    caplog.set_level(logging.INFO)
    rows = [
        PremarketRankEntry("NVDA", 3.5, "analyst", "analyst", 0.87, "analyst headline"),
        PremarketRankEntry("AAPL", 2.5, "earnings", "earnings", 0.82, "earnings headline"),
    ]
    log_premarket_rankings(rows, top_n=10)
    assert "PREMARKET_RANK symbol=NVDA score=3.50 source=analyst" in caplog.text
    assert "PREMARKET_TOP10 rank=1 symbol=NVDA" in caplog.text
    assert "PREMARKET_TOP10 rank=2 symbol=AAPL" in caplog.text


def test_write_premarket_rank_json(tmp_path) -> None:
    path = tmp_path / "premarket_rank.json"
    rows = [
        PremarketRankEntry(
            "AAPL",
            7.2,
            "sec_filing",
            "sec_filing",
            0.72,
            "SEC material event filing",
            form="8-K",
            filing_date="2026-06-01",
            accession="0000320193-26-000001",
            url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/aapl-8k.htm",
        )
    ]
    now = datetime(2026, 6, 1, 5, 0, tzinfo=timezone.utc)
    write_premarket_rank_json(path, rows, now=now)
    payload = json.loads(path.read_text())
    assert payload["date"] == "2026-06-01"
    item = payload["items"][0]
    assert item["symbol"] == "AAPL"
    assert item["form"] == "8-K"
    assert item["filing_date"] == "2026-06-01"
    assert item["accession"] == "0000320193-26-000001"
    assert "sec.gov" in item["url"]
    assert item["reason"] == "SEC material event filing"
    assert item["source"] == "sec_filing"
    assert item["catalyst_type"] == "sec_filing"
    assert payload["top_catalyst"]["symbol"] == "AAPL"


def test_write_and_load_premarket_artifacts_seeds_cache(tmp_path, caplog) -> None:
    import logging

    caplog.set_level(logging.INFO)
    project_root = tmp_path
    now = datetime(2026, 6, 1, 5, 0, tzinfo=timezone.utc)
    events = [
        NewsEvent(
            symbol="GOOGL",
            headline="Google wins new cloud deal",
            source="alpaca",
            url="http://example.com/googl",
            published_at="2026-06-01T04:55:00Z",
            score=6.5,
        )
    ]
    catalysts = {
        "GOOGL": nc.NewsCatalyst(
            "GOOGL",
            6,
            "Google wins new cloud deal",
            source="alpaca",
            catalyst_type="deal",
            article_count=2,
            sentiment=0.65,
        )
    }
    rankings = [
        PremarketRankEntry("GOOGL", 6.5, "deal", "alpaca", 0.92, "deal headline")
    ]
    nc._NEWS_CACHE.clear()

    write_premarket_artifacts(
        project_root,
        now=now,
        source="news_5am",
        events=events,
        catalysts=catalysts,
        rankings=rankings,
        ttl_minutes=60,
    )

    assert default_premarket_event_feed_path(project_root).exists()
    assert default_premarket_rankings_path(project_root).exists()
    assert default_premarket_catalysts_path(project_root).exists()

    loaded = load_premarket_artifacts(project_root, now=now, emit_log=True)
    assert "GOOGL" in loaded
    meta = nc.get_cached_news_metadata("GOOGL", now=now, emit_log=False)
    assert meta is not None
    assert meta["score"] == 7
    assert meta["event_score"] == 6.5
    assert meta["article_count"] == 2
    assert "PREMARKET_ARTIFACT_LOADED path=" in caplog.text
    assert "CATALYST_MATCH_DEBUG symbol=GOOGL source=alpaca" in caplog.text
    assert "PREMARKET_CATALYST_APPLIED symbol=GOOGL score=6.50 source=alpaca" in caplog.text


def test_load_premarket_artifacts_maps_catalyst_score_only_item(
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO)
    project_root = tmp_path
    path = default_premarket_rankings_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-06-01T09:20:00-04:00",
                "source": "sec",
                "ttl_minutes": 390,
                "symbols": ["ABAT"],
                "rankings": [
                    {
                        "symbol": "ABAT",
                        "headline": "ABAT files material update",
                        "source": "sec",
                        "catalyst_type": "sec_filing",
                        "catalyst_score": 0.4,
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    nc._NEWS_CACHE.clear()

    loaded = load_premarket_artifacts(
        project_root,
        now=datetime(2026, 6, 1, 9, 25, tzinfo=ZoneInfo("America/New_York")),
        emit_log=True,
    )

    assert loaded["ABAT"]["news_score"] == 4
    assert loaded["ABAT"]["catalyst_score"] == pytest.approx(0.4)
    assert "CATALYST_MATCH_DEBUG symbol=ABAT source=sec" in caplog.text
    assert "raw_score=0.00 mapped_score=4.00" in caplog.text


def test_premarket_artifacts_remain_valid_through_regular_session(tmp_path) -> None:
    project_root = tmp_path
    generated_at = datetime(2026, 6, 1, 9, 32, tzinfo=ZoneInfo("America/New_York"))
    write_premarket_artifacts(
        project_root,
        now=generated_at,
        source="news_5am",
        events=[
            NewsEvent(
                symbol="AMD",
                headline="AMD announces AI accelerator deal",
                source="alpaca",
                score=7.0,
            )
        ],
        catalysts={
            "AMD": nc.NewsCatalyst(
                "AMD",
                7,
                "AMD announces AI accelerator deal",
                source="alpaca",
                catalyst_type="ai",
            )
        },
        rankings=[PremarketRankEntry("AMD", 7.0, "ai", "alpaca", 0.85, "ai headline")],
        ttl_minutes=60,
    )

    loaded = load_premarket_artifacts(
        project_root,
        now=datetime(2026, 6, 1, 13, 0, tzinfo=ZoneInfo("America/New_York")),
        emit_log=False,
    )

    assert "AMD" in loaded
    assert loaded["AMD"]["news_score"] >= 7


def test_stale_premarket_artifact_ignored(tmp_path, caplog) -> None:
    import logging
    import json

    caplog.set_level(logging.INFO)
    project_root = tmp_path
    path = default_premarket_event_feed_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-06-01T03:00:00+00:00",
                "source": "news_5am",
                "ttl_minutes": 30,
                "symbols": ["GOOGL"],
                "events": [
                    {
                        "symbol": "GOOGL",
                        "headline": "Old headline",
                        "source": "alpaca",
                        "score": 6.5,
                    }
                ],
                "catalysts": [],
                "rankings": [],
            }
        )
        + "\n"
    )
    nc._NEWS_CACHE.clear()
    loaded = load_premarket_artifacts(
        project_root,
        now=datetime(2026, 6, 1, 5, 0, tzinfo=timezone.utc),
        emit_log=True,
    )
    assert loaded == {}
    assert nc.get_cached_news_metadata("GOOGL", emit_log=False) is None
    assert "PREMARKET_ARTIFACT_STALE path=" in caplog.text


def test_write_premarket_artifacts_keep_rankings_and_catalysts_linked(
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO)
    project_root = tmp_path
    now = datetime(2026, 6, 1, 5, 0, tzinfo=timezone.utc)
    events = [NewsEvent(symbol="DXST", headline="DXST wins contract", source="alpaca", score=6.5)]
    catalysts = {
        "DXST": nc.NewsCatalyst(
            "DXST",
            7,
            "DXST wins contract",
            source="alpaca",
            catalyst_type="deal",
            article_count=1,
            sentiment=0.7,
        )
    }
    rankings = [PremarketRankEntry("DXST", 6.5, "deal", "deal", 0.91, "deal headline")]

    write_premarket_artifacts(
        project_root,
        now=now,
        source="news_5am",
        events=events,
        catalysts=catalysts,
        rankings=rankings,
    )

    rankings_payload = json.loads(default_premarket_rankings_path(project_root).read_text())
    assert rankings_payload["events"]
    assert rankings_payload["catalysts"]
    assert rankings_payload["rankings"]
    assert rankings_payload["catalysts"][0]["symbol"] == "DXST"
    assert rankings_payload["events"][0]["symbol"] == "DXST"
    assert (
        "PREMARKET_ARTIFACT_COUNTS event_count=1 rankable_event_count=1 catalyst_count=1 ranking_count=1"
        in caplog.text
    )


def test_run_news_job_writes_rank_json_and_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import json
    import logging
    from zoneinfo import ZoneInfo

    caplog.set_level(logging.INFO)
    rankings = [
        PremarketRankEntry("AAPL", 3.0, "earnings", "earnings", 0.81, "earnings headline"),
        PremarketRankEntry(
            "NVDA",
            7.2,
            "sec_filing",
            "sec_filing",
            0.72,
            "SEC material event filing",
            form="8-K",
            filing_date="2026-06-01",
            accession="0001045810-26-000001",
            url="https://www.sec.gov/Archives/edgar/data/1045810/example.htm",
        ),
    ]
    bundle = pm.PremarketProviderResults(
        newsapi=ProviderExecResult("newsapi", request_sent=True, duration_ms=10.0, articles=2),
        alpaca=ProviderExecResult("alpaca", request_sent=True, duration_ms=20.0, articles=1),
        sec=ProviderExecResult("sec", request_sent=True, duration_ms=30.0, cik_mapped=2, filings=1),
        catalysts={"AAPL": MagicMock(score=3, headline="beat", catalyst_type="earnings")},
        rankings=rankings,
    )
    monkeypatch.setattr(pm, "execute_premarket_providers", lambda *a, **k: bundle)

    stats = pm._run_news_5am_job(
        {
            "universe": {"symbols": ["AAPL", "NVDA"]},
            "premarket_intelligence": {"enabled": True},
        },
        datetime(2026, 6, 1, 6, 0, tzinfo=ZoneInfo("America/New_York")),
        project_root=tmp_path,
        dry_run=False,
    )

    assert stats.ranked == 2
    assert "PREMARKET_RANK symbol=NVDA score=7.20 source=sec_filing catalyst_type=sec_filing form=8-K" in caplog.text
    assert "PREMARKET_TOP10 rank=1 symbol=AAPL" in caplog.text
    rank_path = default_premarket_rank_path(tmp_path)
    assert rank_path.exists()
    payload = json.loads(rank_path.read_text())
    assert len(payload["items"]) == 2
    sec_item = next(item for item in payload["items"] if item["symbol"] == "NVDA")
    assert sec_item["form"] == "8-K"
    assert "sec.gov" in sec_item["url"]


def test_run_news_job_uses_provider_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    from zoneinfo import ZoneInfo

    bundle = pm.PremarketProviderResults(
        newsapi=ProviderExecResult("newsapi", request_sent=True, duration_ms=10.0, articles=2),
        alpaca=ProviderExecResult("alpaca", request_sent=True, duration_ms=20.0, articles=1),
        sec=ProviderExecResult("sec", request_sent=True, duration_ms=30.0, cik_mapped=2, filings=1),
        catalysts={"AAPL": MagicMock(score=2, headline="beat", catalyst_type="earnings")},
        rankings=[PremarketRankEntry("AAPL", 2.0, "earnings", "earnings", 0.69, "earnings headline")],
    )
    monkeypatch.setattr(pm, "execute_premarket_providers", lambda *a, **k: bundle)

    stats = pm._run_news_5am_job(
        {
            "universe": {"symbols": ["AAPL", "NVDA"]},
            "premarket_intelligence": {"enabled": True},
        },
        datetime(2026, 6, 1, 6, 0, tzinfo=ZoneInfo("America/New_York")),
        dry_run=False,
    )

    assert stats.symbols == 2
    assert stats.news == 3
    assert stats.filings == 1
    assert stats.ranked == 1
