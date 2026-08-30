"""Backward-compatible re-exports — provider execution lives in premarket_intelligence."""

from __future__ import annotations

from src.premarket_intelligence import (
    NewsEvent,
    PremarketProviderResults,
    ProviderExecResult,
    execute_premarket_providers,
    fetch_alpaca_news_events,
    fetch_finnhub_events,
    fetch_marketaux_events,
    fetch_newsapi_articles,
    fetch_sec_filings,
    merge_premarket_events,
    score_premarket_catalysts,
)

__all__ = [
    "NewsEvent",
    "PremarketProviderResults",
    "ProviderExecResult",
    "execute_premarket_providers",
    "execute_newsapi_provider",
    "execute_alpaca_news_provider",
    "execute_sec_provider",
    "fetch_newsapi_articles",
    "fetch_alpaca_news_events",
    "fetch_finnhub_events",
    "fetch_marketaux_events",
    "fetch_sec_filings",
    "merge_premarket_events",
    "score_premarket_catalysts",
]


def execute_newsapi_provider(
    config,
    symbols,
    *,
    now=None,
):
    from datetime import datetime, timezone

    from src.premarket_intelligence import _timeout_seconds

    return fetch_newsapi_articles(
        symbols,
        config,
        _timeout_seconds(config, "news_timeout_seconds", 10.0),
        now=now or datetime.now(timezone.utc),
    )


def execute_alpaca_news_provider(
    config,
    symbols,
    *,
    market_client=None,
    now=None,
):
    from datetime import datetime, timezone

    from src.premarket_intelligence import _timeout_seconds

    return fetch_alpaca_news_events(
        symbols,
        config,
        _timeout_seconds(config, "alpaca_news_timeout_seconds", 10.0),
        market_client=market_client,
        now=now or datetime.now(timezone.utc),
    )


def execute_sec_provider(
    config,
    symbols,
    *,
    now=None,
):
    from datetime import datetime, timezone

    from src.premarket_intelligence import _timeout_seconds

    return fetch_sec_filings(
        symbols,
        config,
        _timeout_seconds(config, "sec_timeout_seconds", 10.0),
        now=now or datetime.now(timezone.utc),
    )
