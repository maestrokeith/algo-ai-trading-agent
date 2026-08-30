"""Tests for premarket provider diagnostics logging."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.brokers.alpaca_client import resolve_alpaca_credentials
from src.premarket_diagnostics import (
    log_alpaca_news_provider,
    log_newsapi_fetch_result,
    log_newsapi_preflight,
    log_premarket_provider_creds,
    log_premarket_sec_provider,
    log_premarket_universe,
)


def test_log_premarket_universe_sample(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    caplog.set_level(logging.INFO)
    log_premarket_universe(["AAPL", "NVDA", "AMZN", "MSFT", "GOOGL", "TSLA"])
    assert "PREMARKET_UNIVERSE symbols=6 sample=AAPL,NVDA,AMZN,MSFT,GOOGL" in caplog.text


def test_log_newsapi_preflight_missing_key(caplog: pytest.LogCaptureFixture, monkeypatch) -> None:
    import logging

    monkeypatch.setenv("NEWSAPI_KEY", "")
    caplog.set_level(logging.INFO)
    assert log_newsapi_preflight(
        {"premarket_intelligence": {"newsapi_enabled": True}, "news_sentiment": {"enabled": True}}
    ) is False
    assert "PREMARKET_NEWS_PROVIDER provider=newsapi enabled=true" in caplog.text
    assert "api_key_present=false" in caplog.text
    assert "missing_env_NEWSAPI_KEY" in caplog.text


def test_log_newsapi_fetch_result_from_meta(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    caplog.set_level(logging.INFO)
    log_newsapi_fetch_result(
        {"request_sent": True, "http_status": 200, "articles": 12},
        articles=12,
    )
    assert "request_sent=true" in caplog.text
    assert "http_status=200" in caplog.text
    assert "articles=12" in caplog.text


def test_log_newsapi_fetch_result_zero_articles_logs_query(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    caplog.set_level(logging.INFO)
    log_newsapi_fetch_result(
        {
            "request_sent": True,
            "http_status": 200,
            "query": '"AAPL" OR AAPL',
            "from": "2026-06-01T08:00:00Z",
            "to": "2026-06-01T10:00:00Z",
        },
        articles=0,
    )
    assert "articles=0" in caplog.text
    assert 'query="AAPL" OR AAPL' in caplog.text
    assert "from_date=2026-06-01T08:00:00Z" in caplog.text
    assert "to_date=2026-06-01T10:00:00Z" in caplog.text


def test_resolve_alpaca_credentials_live_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    monkeypatch.setenv("ALPACA_LIVE_API_KEY_ID", "live-key")
    monkeypatch.setenv("ALPACA_LIVE_API_SECRET_KEY", "live-secret")
    monkeypatch.setenv("ALPACA_LIVE", "true")

    creds = resolve_alpaca_credentials({"broker": {"paper": False}})
    assert creds.mode == "live"
    assert creds.selected == "live"
    assert creds.live_key_present is True
    assert creds.paper_key_present is False
    assert creds.api_key == "live-key"


def test_resolve_alpaca_credentials_live_falls_back_to_paper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APCA_API_KEY_ID", "paper-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "paper-secret")
    monkeypatch.delenv("ALPACA_LIVE_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_LIVE_API_SECRET_KEY", raising=False)
    monkeypatch.setenv("ALPACA_LIVE", "true")

    creds = resolve_alpaca_credentials({"broker": {"paper": False}})
    assert creds.mode == "live"
    assert creds.selected == "paper"
    assert creds.paper_key_present is True


def test_log_premarket_provider_creds_live_mode(caplog: pytest.LogCaptureFixture, monkeypatch) -> None:
    import logging

    monkeypatch.setenv("ALPACA_LIVE_API_KEY_ID", "live-key")
    monkeypatch.setenv("ALPACA_LIVE_API_SECRET_KEY", "live-secret")
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    monkeypatch.setenv("ALPACA_LIVE", "true")
    caplog.set_level(logging.INFO)

    broker = MagicMock()
    broker.paper = False
    creds = log_premarket_provider_creds({"broker": {"paper": False}}, broker)
    assert creds.selected == "live"
    assert "PREMARKET_PROVIDER_CREDS provider=alpaca mode=live live_key_present=true paper_key_present=false selected=live" in caplog.text


def test_log_alpaca_news_provider_uses_resolved_live_creds(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import logging

    monkeypatch.setenv("ALPACA_LIVE_API_KEY_ID", "live-key")
    monkeypatch.setenv("ALPACA_LIVE_API_SECRET_KEY", "live-secret")
    monkeypatch.setenv("ALPACA_LIVE", "true")
    caplog.set_level(logging.INFO)

    with patch(
        "src.premarket_diagnostics.fetch_alpaca_news_with_credentials",
        return_value=[{"id": 1}],
    ) as fetch_mock:
        count = log_alpaca_news_provider(
            None,
            ["AAPL"],
            config={"broker": {"paper": False}},
            now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        )

    assert count == 1
    fetch_mock.assert_called_once()
    assert "selected=live" in caplog.text
    assert "credentials_present=true" in caplog.text
    assert "request_sent=true" in caplog.text
    assert "articles=1" in caplog.text


def test_log_alpaca_news_provider_with_broker_client(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    broker = MagicMock()
    broker.paper = False
    broker._news = object()
    broker.get_recent_news.return_value = [{"id": 1}, {"id": 2}]
    caplog.set_level(logging.INFO)
    count = log_alpaca_news_provider(
        broker,
        ["AAPL"],
        config={"premarket_intelligence": {"alpaca_news_enabled": True}, "broker": {"paper": False}},
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )
    assert count == 2
    assert "request_sent=true" in caplog.text
    assert "articles=2" in caplog.text


def test_log_premarket_sec_provider_disabled(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    caplog.set_level(logging.INFO)
    filings, cik = log_premarket_sec_provider({"premarket_intelligence": {}}, ["AAPL"])
    assert filings == 0
    assert cik == 0
    assert "PREMARKET_SEC_PROVIDER enabled=false" in caplog.text
    assert "sec_filings_disabled_in_config" in caplog.text
