"""Fetch recent headlines from NewsAPI (https://newsapi.org/)."""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

log = logging.getLogger(__name__)

NEWSAPI_EVERYTHING = "https://newsapi.org/v2/everything"
NEWSAPI_TOP_HEADLINES = "https://newsapi.org/v2/top-headlines"
RATE_LIMIT_HEADER_PREFIXES = ("x-ratelimit", "ratelimit")
RATE_LIMIT_HEADER_NAMES = {
    "retry-after",
    "x-newsapi-ratelimit-remaining",
    "x-newsapi-ratelimit-limit",
    "x-newsapi-ratelimit-reset",
}


class NewsAPIRateLimitError(RuntimeError):
    """Raised when NewsAPI returns HTTP 429."""


def redact_newsapi_secret(text: str) -> str:
    """Redact apiKey values from URLs/messages before logging."""
    raw = str(text or "")
    raw = re.sub(r"(?i)(apiKey=)[^&\s]+", r"\1<redacted>", raw)
    raw = re.sub(r"(?i)(api_key=)[^&\s]+", r"\1<redacted>", raw)
    return raw


def _redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        pairs = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            pairs.append((key, "<redacted>" if key.lower() in {"apikey", "api_key"} else value))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), parts.fragment))
    except Exception:
        return redact_newsapi_secret(url)


def _rate_limit_headers(headers: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        items = headers.items()
    except Exception:
        return out
    for key, value in items:
        name = str(key or "").strip()
        lower = name.lower()
        if lower in RATE_LIMIT_HEADER_NAMES or any(lower.startswith(prefix) for prefix in RATE_LIMIT_HEADER_PREFIXES):
            out[name] = str(value)
    return out


def fetch_headlines(
    symbol: str,
    api_key: str,
    *,
    lookback_hours: int = 24,
    page_size: int = 15,
    language: str = "en",
    timeout_sec: float = 15.0,
) -> list[str]:
    """
    Query NewsAPI everything endpoint for articles mentioning the ticker/symbol.
    Returns list of headline strings (title + optional description snippet).
    """
    if not api_key:
        return []
    articles = fetch_articles(
        symbol,
        api_key,
        lookback_hours=lookback_hours,
        page_size=page_size,
        language=language,
        timeout_sec=timeout_sec,
    )
    out: list[str] = []
    for art in articles:
        title = str(art.get("title") or "").strip()
        desc = str(art.get("description") or "").strip()
        if title:
            out.append(f"{title}. {desc}" if desc else title)
    return out


def fetch_articles(
    symbol: str,
    api_key: str,
    *,
    lookback_hours: int = 24,
    page_size: int = 15,
    language: str = "en",
    timeout_sec: float = 15.0,
    raise_on_rate_limit: bool = False,
) -> list[dict[str, Any]]:
    """Query NewsAPI and return raw article dictionaries for catalyst scoring."""
    if not api_key:
        return []
    q = f'"{symbol}" OR {symbol} stock'
    return fetch_articles_query(
        q,
        api_key,
        lookback_hours=lookback_hours,
        page_size=page_size,
        language=language,
        timeout_sec=timeout_sec,
        raise_on_rate_limit=raise_on_rate_limit,
    )


def fetch_articles_query(
    query: str,
    api_key: str,
    *,
    lookback_hours: int = 24,
    now: datetime | None = None,
    page_size: int = 15,
    language: str = "en",
    timeout_sec: float = 15.0,
    raise_on_rate_limit: bool = False,
    meta: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Query NewsAPI with a caller-provided query and return raw articles."""
    if meta is not None:
        meta.clear()
        meta["request_sent"] = False
        meta["http_status"] = None
        meta["articles"] = 0
        meta["skip_reason"] = ""
        meta["error"] = ""
        meta["query"] = ""
        meta["from"] = ""
        meta["to"] = ""
        meta["endpoint"] = NEWSAPI_EVERYTHING
        meta["rate_limit_headers"] = {}
    if not api_key:
        if meta is not None:
            meta["skip_reason"] = "missing_api_key"
        return []
    to_dt = now if now is not None else datetime.now(timezone.utc)
    if to_dt.tzinfo is None:
        to_dt = to_dt.replace(tzinfo=timezone.utc)
    else:
        to_dt = to_dt.astimezone(timezone.utc)
    from_dt = to_dt - timedelta(hours=max(1, lookback_hours))
    params: dict[str, Any] = {
        "q": str(query or "").strip(),
        "language": language,
        "sortBy": "publishedAt",
        "pageSize": min(100, max(1, page_size)),
        "from": from_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": to_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "apiKey": api_key,
    }
    if meta is not None:
        meta["query"] = str(params["q"])
        meta["from"] = str(params["from"])
        meta["to"] = str(params["to"])
    try:
        if meta is not None:
            meta["request_sent"] = True
        r = requests.get(NEWSAPI_EVERYTHING, params=params, timeout=timeout_sec)
        if meta is not None:
            meta["http_status"] = int(r.status_code)
            meta["rate_limit_headers"] = _rate_limit_headers(r.headers)
        if r.status_code == 429:
            msg = f"NewsAPI rate limited: status=429 url={_redact_url(r.url)}"
            if meta is not None:
                meta["skip_reason"] = "rate_limited"
                meta["error"] = redact_newsapi_secret(msg)[:240]
            if raise_on_rate_limit:
                raise NewsAPIRateLimitError(msg)
            log.warning("%s", msg)
            log.info("NEWS_FETCH_ERROR error=%s", redact_newsapi_secret(msg)[:240])
            return []
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "ok":
            msg = redact_newsapi_secret(str(data.get("message", data)))
            if meta is not None:
                meta["skip_reason"] = "api_status_not_ok"
                meta["error"] = msg[:240]
            log.warning("NewsAPI non-ok: %s", msg)
            log.info("NEWS_FETCH_ERROR error=%s", msg[:240])
            return []
        articles = [art for art in data.get("articles") or [] if isinstance(art, dict)]
        if meta is not None:
            meta["articles"] = len(articles)
        return articles
    except Exception as e:
        if isinstance(e, NewsAPIRateLimitError):
            raise
        safe_error = redact_newsapi_secret(str(e))
        if meta is not None:
            meta["error"] = safe_error[:240]
            if not meta.get("skip_reason"):
                meta["skip_reason"] = e.__class__.__name__
        log.warning("NewsAPI fetch failed: %s", safe_error)
        log.info("NEWS_FETCH_ERROR error=%s", safe_error[:240])
        return []


def fetch_top_headlines_query(
    query: str,
    api_key: str,
    *,
    page_size: int = 20,
    language: str = "en",
    country: str = "us",
    timeout_sec: float = 15.0,
    raise_on_rate_limit: bool = False,
    meta: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Query NewsAPI top-headlines endpoint and return raw article dictionaries."""
    if meta is not None:
        meta.clear()
        meta["request_sent"] = False
        meta["http_status"] = None
        meta["articles"] = 0
        meta["skip_reason"] = ""
        meta["error"] = ""
        meta["query"] = ""
        meta["from"] = ""
        meta["to"] = ""
        meta["endpoint"] = NEWSAPI_TOP_HEADLINES
        meta["rate_limit_headers"] = {}
    if not api_key:
        if meta is not None:
            meta["skip_reason"] = "missing_api_key"
        return []
    params: dict[str, Any] = {
        "q": str(query or "").strip(),
        "language": language,
        "country": country,
        "pageSize": min(100, max(1, page_size)),
        "apiKey": api_key,
    }
    if meta is not None:
        meta["query"] = str(params["q"])
    try:
        if meta is not None:
            meta["request_sent"] = True
        r = requests.get(NEWSAPI_TOP_HEADLINES, params=params, timeout=timeout_sec)
        if meta is not None:
            meta["http_status"] = int(r.status_code)
            meta["rate_limit_headers"] = _rate_limit_headers(r.headers)
        if r.status_code == 429:
            msg = f"NewsAPI rate limited: status=429 url={_redact_url(r.url)}"
            if meta is not None:
                meta["skip_reason"] = "rate_limited"
                meta["error"] = redact_newsapi_secret(msg)[:240]
            if raise_on_rate_limit:
                raise NewsAPIRateLimitError(msg)
            log.warning("%s", msg)
            log.info("NEWS_FETCH_ERROR error=%s", redact_newsapi_secret(msg)[:240])
            return []
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "ok":
            msg = redact_newsapi_secret(str(data.get("message", data)))
            if meta is not None:
                meta["skip_reason"] = "api_status_not_ok"
                meta["error"] = msg[:240]
            log.warning("NewsAPI top-headlines non-ok: %s", msg)
            log.info("NEWS_FETCH_ERROR error=%s", msg[:240])
            return []
        articles = [art for art in data.get("articles") or [] if isinstance(art, dict)]
        if meta is not None:
            meta["articles"] = len(articles)
        return articles
    except Exception as e:
        if isinstance(e, NewsAPIRateLimitError):
            raise
        safe_error = redact_newsapi_secret(str(e))
        if meta is not None:
            meta["error"] = safe_error[:240]
            if not meta.get("skip_reason"):
                meta["skip_reason"] = e.__class__.__name__
        log.warning("NewsAPI top-headlines fetch failed: %s", safe_error)
        log.info("NEWS_FETCH_ERROR error=%s", safe_error[:240])
        return []


def newsapi_key_from_config(config: dict) -> str:
    ns = config.get("news_sentiment") or {}
    env_name = ns.get("newsapi_key_env") or "NEWSAPI_KEY"
    return (os.environ.get(env_name) or "").strip()
