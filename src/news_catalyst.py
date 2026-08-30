"""Batch NewsAPI catalyst scoring for dynamic-universe scan and live entry bypass."""

from __future__ import annotations

import logging
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from src.news_sentiment.newsapi_client import (
    NewsAPIRateLimitError,
    fetch_articles_query,
    newsapi_key_from_config,
)
from src.ai_news_ranking import is_software_package_spam

log = logging.getLogger(__name__)

_UNSTABLE_QUOTE_SPREAD_PCT = 15.0
_EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class NewsCatalyst:
    """Scored headline catalyst for one symbol."""

    symbol: str
    score: int
    headline: str
    article_symbols: tuple[str, ...] = ()
    published_at: datetime | None = None
    source: str = ""
    catalyst_type: str | None = None
    article_count: int = 0
    sentiment: float = 0.0
    publisher: str = ""
    url: str = ""


@dataclass
class _NewsCacheEntry:
    score: int
    headline: str
    fetched_at: datetime
    catalyst: NewsCatalyst | None = None
    article_count: int = 0
    sentiment: float = 0.0
    event_score: float = 0.0


_NEWS_CACHE: dict[str, _NewsCacheEntry] = {}
_NEWS_LAST_FETCH_AT: datetime | None = None
_NEWS_RATE_LIMIT_UNTIL: datetime | None = None
_NEWS_LAST_ARTICLES_FETCHED = 0
_NEWS_LAST_ARTICLES_AFTER_FILTER = 0
_ALPACA_NEWS_LAST_EVENTS = 0

PREMARKET_ARTIFACT_TTL_MINUTES = 390
PREMARKET_ARTIFACT_DIRNAME = "premarket"
PREMARKET_ARTIFACT_FILENAMES = {
    "events": "latest_event_feed.json",
    "rankings": "latest_rankings.json",
    "catalysts": "latest_catalysts.json",
}

_CATALYST_RULES: tuple[tuple[tuple[str, ...], int, str], ...] = (
    (("fda approval", "fda clears", "fda grants"), 4, "fda"),
    (("fda", "clinical trial", "phase 3", "phase iii"), 3, "fda"),
    (("earnings beat", "beats estimates", "raises guidance", "raises outlook"), 4, "earnings"),
    (("earnings", "eps beat", "revenue beat", "guidance raise"), 3, "earnings"),
    (("upgrade", "upgraded", "price target raised"), 3, "upgrade"),
    (("contract win", "wins contract", "partnership", "collaboration"), 3, "deal"),
    (("offering", "dilution", "secondary offering"), -4, "dilution"),
    (("investigation", "sec probe", "lawsuit"), -3, "legal"),
)

_TICKER_IN_TEXT = re.compile(r"\b[A-Z]{1,5}\b")


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _artifact_path(project_root: Path, filename: str) -> Path:
    return project_root / "data" / PREMARKET_ARTIFACT_DIRNAME / filename


def premarket_artifact_paths(project_root: Path) -> dict[str, Path]:
    return {kind: _artifact_path(project_root, filename) for kind, filename in PREMARKET_ARTIFACT_FILENAMES.items()}


def _artifact_symbol_list(payload: Mapping[str, Any]) -> list[str]:
    raw = payload.get("symbols")
    symbols: list[str] = []
    if isinstance(raw, (list, tuple, set)):
        for item in raw:
            sym = str(item or "").strip().upper()
            if sym and sym not in symbols:
                symbols.append(sym)
    return symbols


def _artifact_items(payload: Mapping[str, Any], kind: str) -> list[Mapping[str, Any]]:
    primary = payload.get(kind)
    if isinstance(primary, list):
        return [item for item in primary if isinstance(item, Mapping)]
    for fallback in ("events", "rankings", "catalysts"):
        raw = payload.get(fallback)
        if isinstance(raw, list) and raw:
            return [item for item in raw if isinstance(item, Mapping)]
    return []


def _artifact_item_symbol(raw: Mapping[str, Any]) -> str:
    for key in ("symbol", "ticker"):
        sym = str(raw.get(key) or "").strip().upper()
        if sym:
            return sym
    symbols = raw.get("symbols")
    if isinstance(symbols, (list, tuple)) and symbols:
        sym = str(symbols[0] or "").strip().upper()
        if sym:
            return sym
    return ""


def _artifact_item_raw_score(raw: Mapping[str, Any]) -> float:
    for key in ("score", "news_score", "event_score", "raw_score", "mapped_score", "rank_score"):
        try:
            val = float(raw.get(key) or 0.0)
        except (TypeError, ValueError):
            continue
        if val != 0.0:
            return val
    return 0.0


def _artifact_item_catalyst_score(raw: Mapping[str, Any]) -> float:
    try:
        val = float(raw.get("catalyst_score") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if val <= 0.0:
        return 0.0
    return val / 10.0 if val > 1.0 else val


def _artifact_item_score(raw: Mapping[str, Any]) -> float:
    raw_score = _artifact_item_raw_score(raw)
    if raw_score != 0.0:
        return raw_score
    catalyst_score = _artifact_item_catalyst_score(raw)
    if catalyst_score > 0.0:
        return catalyst_score * 10.0
    return 0.0


def _artifact_item_event_score(raw: Mapping[str, Any]) -> float:
    try:
        return float(raw.get("event_score") or raw.get("score") or raw.get("news_score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _artifact_item_article_count(raw: Mapping[str, Any]) -> int:
    for key in ("article_count", "articles", "count"):
        try:
            val = int(raw.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if val >= 0:
            return val
    return 0


def _artifact_item_sentiment(raw: Mapping[str, Any]) -> float:
    for key in ("sentiment", "sentiment_score"):
        try:
            val = float(raw.get(key) or 0.0)
        except (TypeError, ValueError):
            continue
        return max(-1.0, min(1.0, val))
    return 0.0


def _artifact_item_headline(raw: Mapping[str, Any]) -> str:
    return str(raw.get("headline") or raw.get("title") or raw.get("text") or "").strip()


def _artifact_item_source(raw: Mapping[str, Any], fallback: str) -> str:
    source = str(raw.get("source") or raw.get("provider") or fallback or "").strip().lower()
    if source in {"sec", "alpaca", "newsapi", "twitter"}:
        return source
    if source == "sec_filing":
        return "sec"
    return source or fallback


def _parse_dt(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _article_text(article: Mapping[str, Any]) -> str:
    title = str(article.get("title") or "").strip()
    desc = str(article.get("description") or "").strip()
    return f"{title}. {desc}".strip(". ")


def _article_publisher(article: Mapping[str, Any]) -> str:
    source = article.get("source")
    if isinstance(source, Mapping):
        for key in ("name", "id"):
            text = str(source.get(key) or "").strip()
            if text:
                return text
    for key in ("publisher", "provider", "source_name", "source"):
        text = str(article.get(key) or "").strip()
        if text and not text.startswith("{"):
            return text
    return ""


def extract_article_symbols(article: Mapping[str, Any]) -> tuple[str, ...]:
    """Explicit tickers attached to an article (when provided by the feed)."""
    raw = article.get("symbols")
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[str] = []
    for item in raw:
        sym = str(item or "").strip().upper()
        if sym and sym not in out:
            out.append(sym)
    return tuple(out)


def headline_mentions_symbol(text: str, symbol: str) -> bool:
    """True when *symbol* appears as a standalone token in headline/description text."""
    sym = str(symbol or "").strip().upper()
    if not sym or not text:
        return False
    return bool(re.search(rf"\b{re.escape(sym)}\b", str(text).upper()))


def article_applies_to_symbol(article: Mapping[str, Any], symbol: str) -> bool:
    """
    Only attribute an article to *symbol* when it is explicitly tagged or mentioned in text.

    Prevents unrelated movers (e.g. HUBC/PRFX) from inheriting a DELL headline score.
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        return False
    explicit = extract_article_symbols(article)
    if explicit:
        return sym in explicit
    return headline_mentions_symbol(_article_text(article), sym)


def _article_count_for_symbol(
    symbol: str,
    articles: Sequence[Mapping[str, Any]],
) -> int:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return 0
    return sum(1 for article in articles if article_applies_to_symbol(article, sym))


def _sentiment_from_score(score: int) -> float:
    try:
        val = float(score) / 10.0
    except (TypeError, ValueError):
        val = 0.0
    return max(-1.0, min(1.0, val))


def _log_news_lookup(
    symbol: str,
    *,
    matched_articles: int,
    cache_hit: bool,
    sentiment_score: float,
    reason: str | None = None,
) -> None:
    msg = (
        "NEWS_LOOKUP symbol=%s matched_articles=%d cache_hit=%s sentiment_score=%.2f"
        % (
            symbol,
            int(matched_articles),
            str(bool(cache_hit)).lower(),
            float(sentiment_score),
        )
    )
    if reason:
        msg += f" reason={reason}"
    log.info(msg)


def score_article_text(text: str) -> tuple[int, str | None]:
    """Return integer catalyst score and optional catalyst_type label."""
    low = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    if not low:
        return 0, None
    best_score = 0
    best_type: str | None = None
    for phrases, score, ctype in _CATALYST_RULES:
        if any(p in low for p in phrases):
            if abs(score) > abs(best_score) or (score > 0 and score > best_score):
                best_score = score
                best_type = ctype
    if best_score == 0 and _TICKER_IN_TEXT.search(str(text or "").upper()):
        best_score = 1
    return int(best_score), best_type


def _score_article_for_symbol(article: Mapping[str, Any], symbol: str) -> tuple[int, str | None, str]:
    text = _article_text(article)
    publisher = _article_publisher(article)
    url = str(article.get("url") or "").strip()
    source = str(article.get("source") or "").strip()
    if is_software_package_spam(text, source=source, publisher=publisher, url=url):
        log.info(
            "NEWS_PACKAGE_SPAM_FILTERED symbol=%s source=%s publisher=%s title=%s",
            str(symbol or "").strip().upper() or "?",
            source or "unknown",
            publisher or "unknown",
            text.replace("\n", " ")[:180],
        )
        return 0, None, text
    if not article_applies_to_symbol(article, symbol):
        return 0, None, text
    score, ctype = score_article_text(text)
    return score, ctype, text


def _news_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    root = dict(config or {})
    ns = root.get("news_sentiment")
    if isinstance(ns, Mapping):
        return dict(ns)
    return {}


def _news_ai_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    root = dict(config or {})
    ns = root.get("news_ai")
    if isinstance(ns, Mapping):
        return dict(ns)
    return {}


def _alpaca_realtime_news_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    ns = _news_config(config)
    raw = ns.get("alpaca_realtime") if isinstance(ns, Mapping) else None
    return dict(raw) if isinstance(raw, Mapping) else {}


def _alpaca_realtime_news_enabled(config: Mapping[str, Any] | None) -> bool:
    cfg = _alpaca_realtime_news_config(config)
    return str(cfg.get("enabled", False)).strip().lower() in {"1", "true", "yes", "on"}


def _alpaca_realtime_extra_symbols(config: Mapping[str, Any] | None) -> list[str]:
    cfg = _alpaca_realtime_news_config(config)
    raw = cfg.get("symbols") or cfg.get("watchlist_symbols") or []
    out: list[str] = []
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",")]
    if isinstance(raw, (list, tuple, set)):
        for item in raw:
            sym = str(item or "").strip().upper()
            if sym and sym not in out:
                out.append(sym)
    return out


def _alpaca_article_value(article: Any, key: str, default: Any = None) -> Any:
    if isinstance(article, Mapping):
        return article.get(key, default)
    return getattr(article, key, default)


def _normalize_alpaca_news_article(article: Any) -> dict[str, Any]:
    headline = str(
        _alpaca_article_value(article, "headline")
        or _alpaca_article_value(article, "title")
        or ""
    ).strip()
    summary = str(
        _alpaca_article_value(article, "summary")
        or _alpaca_article_value(article, "description")
        or ""
    ).strip()
    raw_symbols = _alpaca_article_value(article, "symbols", []) or []
    symbols: list[str] = []
    if isinstance(raw_symbols, str):
        raw_symbols = [part.strip() for part in raw_symbols.split(",")]
    if isinstance(raw_symbols, (list, tuple, set)):
        for item in raw_symbols:
            sym = str(item or "").strip().upper()
            if sym and sym not in symbols:
                symbols.append(sym)
    published_raw = (
        _alpaca_article_value(article, "created_at")
        or _alpaca_article_value(article, "updated_at")
        or _alpaca_article_value(article, "published_at")
        or _alpaca_article_value(article, "publishedAt")
    )
    return {
        "id": str(_alpaca_article_value(article, "id", "") or ""),
        "title": headline,
        "description": summary,
        "symbols": symbols,
        "publishedAt": published_raw.isoformat() if hasattr(published_raw, "isoformat") else str(published_raw or ""),
        "source": "alpaca",
        "publisher": str(_alpaca_article_value(article, "author") or _alpaca_article_value(article, "source") or "alpaca"),
        "url": str(_alpaca_article_value(article, "url", "") or ""),
        "provider": "alpaca",
    }


def _persist_alpaca_news_event(
    article: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None,
    now: datetime,
) -> None:
    cfg = _alpaca_realtime_news_config(config)
    if str(cfg.get("persist", True)).strip().lower() in {"0", "false", "no", "off"}:
        return
    persist_dir = Path(str(cfg.get("persist_dir") or "data/realtime_alpaca_news"))
    try:
        persist_dir.mkdir(parents=True, exist_ok=True)
        path = persist_dir / f"{now.date().isoformat()}.jsonl"
        payload = {
            "ingested_at": now.isoformat(),
            "provider": "alpaca",
            "id": article.get("id"),
            "symbols": article.get("symbols") or [],
            "headline": article.get("title") or "",
            "summary": article.get("description") or "",
            "published_at": article.get("publishedAt") or "",
            "url": article.get("url") or "",
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, sort_keys=True) + "\n")
    except Exception:
        log.debug("Alpaca news event persistence failed", exc_info=True)


def _fetch_alpaca_realtime_articles(
    market_client: Any,
    symbols: Sequence[str],
    config: Mapping[str, Any] | None,
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    global _ALPACA_NEWS_LAST_EVENTS
    if not _alpaca_realtime_news_enabled(config):
        return []
    cfg = _alpaca_realtime_news_config(config)
    if market_client is None or not hasattr(market_client, "get_recent_news"):
        log.info("ALPACA_NEWS_FALLBACK reason=client_missing")
        return []
    uniq = list(dict.fromkeys(str(s or "").strip().upper() for s in symbols if str(s or "").strip()))
    for sym in _alpaca_realtime_extra_symbols(config):
        if sym not in uniq:
            uniq.append(sym)
    if not uniq:
        return []
    try:
        lookback_minutes = max(1, int(cfg.get("lookback_minutes", 30) or 30))
    except (TypeError, ValueError):
        lookback_minutes = 30
    try:
        limit = max(1, int(cfg.get("limit", 50) or 50))
    except (TypeError, ValueError):
        limit = 50
    start = now - timedelta(minutes=lookback_minutes)
    log.info("ALPACA_NEWS_INGEST_START symbols=%d lookback_minutes=%d limit=%d", len(uniq), lookback_minutes, limit)
    try:
        raw_articles = market_client.get_recent_news(
            uniq,
            start=start,
            end=now,
            limit=limit,
            exclude_contentless=False,
        )
    except Exception as exc:
        log.info("ALPACA_NEWS_FALLBACK reason=fetch_failed error=%s", str(exc)[:180])
        return []
    articles = [_normalize_alpaca_news_article(article) for article in raw_articles or []]
    _ALPACA_NEWS_LAST_EVENTS = len(articles)
    for article in articles:
        published = _parse_dt(article.get("publishedAt"))
        age_seconds = (now - published).total_seconds() if published is not None else None
        log.info(
            "ALPACA_NEWS_EVENT symbols=%s headline=%s published_at=%s age_seconds=%s",
            ",".join(article.get("symbols") or []) or "none",
            str(article.get("title") or "")[:180],
            article.get("publishedAt") or "",
            "n/a" if age_seconds is None else "%.1f" % max(0.0, age_seconds),
        )
        _persist_alpaca_news_event(article, config=config, now=now)
    return articles


def _score_alpaca_realtime_catalysts(
    articles: Sequence[Mapping[str, Any]],
    symbols: Sequence[str],
    config: Mapping[str, Any] | None,
    *,
    now: datetime,
) -> dict[str, NewsCatalyst]:
    if not articles:
        return {}
    cfg = _alpaca_realtime_news_config(config)
    try:
        max_age_seconds = max(1.0, float(cfg.get("max_age_seconds", 900) or 900))
    except (TypeError, ValueError):
        max_age_seconds = 900.0
    try:
        min_score = int(cfg.get("min_score", 3) or 3)
    except (TypeError, ValueError):
        min_score = 3
    eligible_symbols = set(str(s or "").strip().upper() for s in symbols if str(s or "").strip())
    eligible_symbols.update(_alpaca_realtime_extra_symbols(config))
    for article in articles:
        for sym in extract_article_symbols(article):
            eligible_symbols.add(sym)

    out: dict[str, NewsCatalyst] = {}
    for sym in sorted(eligible_symbols):
        filtered: list[Mapping[str, Any]] = []
        for article in articles:
            published = _parse_dt(article.get("publishedAt"))
            if published is not None and (now - published).total_seconds() > max_age_seconds:
                continue
            if article_applies_to_symbol(article, sym):
                filtered.append(article)
        cat = _best_catalyst_for_symbol(sym, filtered, source="alpaca")
        if cat is None:
            continue
        age_seconds = (now - cat.published_at).total_seconds() if cat.published_at is not None else None
        log.info(
            "ALPACA_NEWS_SCORE symbol=%s score=%d catalyst_type=%s article_count=%d headline=%s",
            sym,
            int(cat.score),
            cat.catalyst_type or "unknown",
            int(cat.article_count),
            cat.headline[:180],
        )
        if int(cat.score) < min_score:
            continue
        out[sym] = cat
        _NEWS_CACHE[sym] = _NewsCacheEntry(
            score=int(cat.score),
            headline=cat.headline,
            fetched_at=now,
            catalyst=cat,
            article_count=int(cat.article_count or 0),
            sentiment=float(cat.sentiment or 0.0),
            event_score=float(cat.score),
        )
        if age_seconds is not None:
            log.info(
                "ALPACA_NEWS_LATENCY symbol=%s latency_seconds=%.1f published_at=%s available_at=%s",
                sym,
                max(0.0, age_seconds),
                cat.published_at.isoformat(),
                now.isoformat(),
            )
    return out


def _ensure_et(now: datetime | None) -> datetime:
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_EASTERN)


def news_refresh_phase_for_et(now_et: datetime) -> tuple[str | None, float | None]:
    """Return the active news-refresh phase and age limit in seconds for the ET timestamp."""
    dt = now_et
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_EASTERN)
    else:
        dt = dt.astimezone(_EASTERN)
    tm = dt.timetz()
    hhmm = (tm.hour, tm.minute)
    if (8, 30) <= hhmm < (9, 20):
        return "premarket", 15.0 * 60.0
    if (9, 35) <= hhmm < (10, 30):
        return "market_open", 5.0 * 60.0
    if (10, 30) <= hhmm < (15, 30):
        return "intraday", 15.0 * 60.0
    if (15, 45) <= hhmm < (16, 0):
        return "eod", 0.0
    return None, None


def _cache_ttl_seconds(config: Mapping[str, Any] | None) -> float:
    ns = _news_config(config)
    try:
        return max(60.0, float(ns.get("cache_ttl_seconds", 900) or 900))
    except (TypeError, ValueError):
        return 900.0


def _fetch_batch_articles(
    symbols: Sequence[str],
    config: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
    newsapi_meta: dict[str, Any] | None = None,
    premarket: bool = False,
    timeout_sec: float = 10.0,
) -> list[dict[str, Any]]:
    global _NEWS_LAST_FETCH_AT, _NEWS_RATE_LIMIT_UNTIL

    if now is None:
        now = datetime.now(timezone.utc)
    ns = _news_config(config)
    if not premarket:
        enabled_raw = ns.get("enabled")
        if enabled_raw is not None and str(enabled_raw).strip().lower() in {"0", "false", "no", "off", "n", ""}:
            if newsapi_meta is not None:
                newsapi_meta["skip_reason"] = "news_sentiment_disabled"
            return []
    if _NEWS_RATE_LIMIT_UNTIL is not None and now < _NEWS_RATE_LIMIT_UNTIL:
        log.info("NEWS_FETCH skipped — rate limited until %s", _NEWS_RATE_LIMIT_UNTIL.isoformat())
        if newsapi_meta is not None:
            newsapi_meta["skip_reason"] = "rate_limit_backoff"
        return []

    api_key = newsapi_key_from_config(dict(config or {}))
    if not api_key:
        if newsapi_meta is not None:
            newsapi_meta["skip_reason"] = "missing_api_key"
        return []

    try:
        lookback_h = int(ns.get("headline_lookback_hours", 24) or 24)
    except (TypeError, ValueError):
        lookback_h = 24
    try:
        page_size = int(ns.get("max_headlines", 30) or 30)
    except (TypeError, ValueError):
        page_size = 30

    uniq = [str(s).strip().upper() for s in symbols if str(s).strip()]
    uniq = list(dict.fromkeys(uniq))
    if not uniq:
        return []

    query_parts = [f'"{sym}" OR {sym}' for sym in uniq[:12]]
    query = " OR ".join(query_parts)
    if newsapi_meta is not None:
        to_dt = now
        from_dt = now - timedelta(hours=max(1, lookback_h))
        newsapi_meta["query"] = query
        newsapi_meta["from"] = from_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        newsapi_meta["to"] = to_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        articles = fetch_articles_query(
            query,
            api_key,
            lookback_hours=lookback_h,
            page_size=min(100, max(page_size, len(uniq) * 3)),
            timeout_sec=float(timeout_sec),
            raise_on_rate_limit=True,
            meta=newsapi_meta,
        )
        _NEWS_LAST_FETCH_AT = now
        if newsapi_meta is not None:
            newsapi_meta["articles"] = len(articles)
        return articles
    except NewsAPIRateLimitError:
        _NEWS_RATE_LIMIT_UNTIL = now + timedelta(minutes=15)
        log.info("NEWS_FETCH rate limited — backing off")
        if newsapi_meta is not None:
            newsapi_meta["http_status"] = 429
            newsapi_meta["skip_reason"] = "rate_limited"
            newsapi_meta["articles"] = 0
        return []
    except Exception as exc:
        log.info("NEWS_FETCH_ERROR error=%s", str(exc)[:240])
        if newsapi_meta is not None:
            newsapi_meta["error"] = str(exc)[:240]
            if not newsapi_meta.get("skip_reason"):
                newsapi_meta["skip_reason"] = exc.__class__.__name__
        return []


def _best_catalyst_for_symbol(
    symbol: str,
    articles: Sequence[Mapping[str, Any]],
    *,
    source: str = "NewsAPI",
) -> NewsCatalyst | None:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    best_score = 0
    best_headline = ""
    best_type: str | None = None
    best_published: datetime | None = None
    best_article_symbols: tuple[str, ...] = ()
    best_publisher = ""
    best_url = ""
    article_count = _article_count_for_symbol(sym, articles)
    for article in articles:
        score, ctype, text = _score_article_for_symbol(article, sym)
        if score == 0 or not text:
            continue
        published = _parse_dt(article.get("publishedAt"))
        if score > best_score or (
            score == best_score and published is not None and (best_published is None or published > best_published)
        ):
            best_score = score
            best_headline = text[:240]
            best_type = ctype
            best_published = published
            best_article_symbols = extract_article_symbols(article)
            best_publisher = _article_publisher(article)
            best_url = str(article.get("url") or "").strip()
    if best_score == 0 or not best_headline:
        return None
    sentiment = _sentiment_from_score(int(best_score))
    return NewsCatalyst(
        sym,
        int(best_score),
        best_headline,
        best_article_symbols,
        best_published,
        source,
        best_type,
        article_count,
        sentiment,
        best_publisher,
        best_url,
    )


def fetch_recent_news_catalysts(
    market_client: Any,
    symbols: Sequence[str],
    *,
    config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    max_age_seconds: float | None = None,
    force_refresh: bool = False,
    newsapi_meta: dict[str, Any] | None = None,
) -> dict[str, NewsCatalyst]:
    """
    Score recent headlines for *symbols* and populate the in-process cache.

    *market_client* is reserved for future broker-native news feeds; NewsAPI is used today.
    """
    global _NEWS_LAST_ARTICLES_FETCHED, _NEWS_LAST_ARTICLES_AFTER_FILTER
    if now is None:
        now = datetime.now(timezone.utc)
    ttl = _cache_ttl_seconds(config) if max_age_seconds is None else max(0.0, float(max_age_seconds))
    if force_refresh:
        ttl = 0.0
    out: dict[str, NewsCatalyst] = {}
    stale: list[str] = []
    for sym in symbols:
        su = str(sym or "").strip().upper()
        if not su:
            continue
        cached = _NEWS_CACHE.get(su)
        if cached is not None and (now - cached.fetched_at).total_seconds() < ttl:
            if cached.catalyst is not None:
                out[su] = cached.catalyst
            continue
        stale.append(su)

    articles: list[dict[str, Any]] = []
    if stale or _alpaca_realtime_extra_symbols(config):
        alpaca_articles = _fetch_alpaca_realtime_articles(market_client, stale, config, now=now)
        alpaca_cats = _score_alpaca_realtime_catalysts(alpaca_articles, stale, config, now=now)
        out.update(alpaca_cats)
        stale = [sym for sym in stale if sym not in alpaca_cats]

    if stale:
        articles = _fetch_batch_articles(stale, config, now=now, newsapi_meta=newsapi_meta)
        _NEWS_LAST_ARTICLES_FETCHED = len(articles)
        if articles:
            log.info("NEWS_FETCH articles=%d symbols=%d", len(articles), len(stale))
    matched_any: set[int] = set()

    for sym in stale:
        matched_articles = _article_count_for_symbol(sym, articles)
        if matched_articles > 0:
            for idx, article in enumerate(articles):
                if article_applies_to_symbol(article, sym):
                    matched_any.add(idx)
        cat = _best_catalyst_for_symbol(sym, articles)
        article_count = int(cat.article_count) if cat is not None else _article_count_for_symbol(sym, articles)
        sentiment = float(cat.sentiment) if cat is not None else 0.0
        lookup_reason = None
        if matched_articles <= 0:
            if newsapi_meta and (newsapi_meta.get("skip_reason") or newsapi_meta.get("error")):
                lookup_reason = "fetch_failure"
            elif not articles:
                lookup_reason = "fetch_failure"
            else:
                lookup_reason = "no symbol match"
        _log_news_lookup(
            sym,
            matched_articles=matched_articles,
            cache_hit=False,
            sentiment_score=sentiment,
            reason=lookup_reason,
        )
        _NEWS_CACHE[sym] = _NewsCacheEntry(
            score=int(cat.score) if cat is not None else 0,
            headline=cat.headline if cat is not None else "",
            fetched_at=now,
            catalyst=cat,
            article_count=article_count,
            sentiment=sentiment,
            event_score=float(cat.score) if cat is not None else 0.0,
        )
        if cat is not None:
            out[sym] = cat
    _NEWS_LAST_ARTICLES_AFTER_FILTER = len(matched_any)
    return out


def news_pipeline_summary() -> dict[str, int]:
    """Current in-process news pipeline counters for startup diagnostics."""
    symbols_scored = len(_NEWS_CACHE)
    return {
        "articles_fetched": int(_NEWS_LAST_ARTICLES_FETCHED),
        "articles_after_filter": int(_NEWS_LAST_ARTICLES_AFTER_FILTER),
        "symbols_scored": int(symbols_scored),
    }


def load_premarket_artifacts(
    project_root: Path,
    *,
    now: datetime | None = None,
    emit_log: bool = True,
) -> dict[str, dict[str, Any]]:
    """
    Load the latest premarket artifact files and seed the in-process news cache.

    Returns a per-symbol summary keyed by symbol. Stale artifact files are ignored.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    summary: dict[str, dict[str, Any]] = {}
    for kind, path in premarket_artifact_paths(project_root).items():
        if not path.exists():
            if emit_log:
                log.info("PREMARKET_ARTIFACT_MISSING path=%s kind=%s", str(path), kind)
            continue
        try:
            payload = json.loads(path.read_text())
        except Exception:
            continue
        if not isinstance(payload, Mapping):
            continue
        generated_at = _parse_dt(payload.get("generated_at"))
        if generated_at is None:
            try:
                generated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except Exception:
                generated_at = now
        try:
            ttl_minutes = int(payload.get("ttl_minutes") or PREMARKET_ARTIFACT_TTL_MINUTES)
        except (TypeError, ValueError):
            ttl_minutes = PREMARKET_ARTIFACT_TTL_MINUTES
        if int(ttl_minutes) == 60 and str(payload.get("source") or "").strip().lower() == "news_5am":
            ttl_minutes = PREMARKET_ARTIFACT_TTL_MINUTES
        age_minutes = max(0.0, (now - generated_at).total_seconds() / 60.0)
        age_seconds = age_minutes * 60.0
        symbols = _artifact_symbol_list(payload)
        if age_minutes > float(ttl_minutes):
            if emit_log:
                log.info(
                    "PREMARKET_ARTIFACT_STALE path=%s age_minutes=%.1f ttl=%d",
                    str(path),
                    age_minutes,
                    ttl_minutes,
                )
            continue
        if emit_log:
            log.info(
                "PREMARKET_ARTIFACT_LOADED path=%s count_symbols=%d age_seconds=%.1f",
                str(path),
                len(symbols),
                age_seconds,
            )
        items = _artifact_items(payload, kind)
        matched_symbols: set[str] = set()
        for item_index, raw in enumerate(items, start=1):
            if not isinstance(raw, Mapping):
                continue
            symbol = _artifact_item_symbol(raw)
            if not symbol:
                continue
            matched_symbols.add(symbol)
            headline = _artifact_item_headline(raw)
            source = _artifact_item_source(raw, str(payload.get("source") or kind))
            raw_score = _artifact_item_raw_score(raw)
            score = _artifact_item_score(raw)
            event_score = _artifact_item_event_score(raw)
            raw_catalyst_score = _artifact_item_catalyst_score(raw)
            article_count = _artifact_item_article_count(raw)
            sentiment = _artifact_item_sentiment(raw)
            catalyst_type = str(raw.get("catalyst_type") or raw.get("rank_source") or raw.get("source") or "").strip() or None
            premarket_rank: int | None = None
            if kind == "rankings":
                try:
                    premarket_rank = max(1, int(float(raw.get("rank") or item_index)))
                except (TypeError, ValueError):
                    premarket_rank = item_index
            prev_news_score = 0
            prev_event_score = 0.0
            prev = summary.get(symbol)
            if prev is None:
                prev = {
                    "symbol": symbol,
                    "news_score": 0,
                    "event_score": 0.0,
                    "catalyst_score": 0.0,
                    "article_count": 0,
                    "sentiment": 0.0,
                    "headline": "",
                    "source": source,
                    "catalyst_type": catalyst_type,
                    "generated_at": generated_at,
                    "age_seconds": age_seconds,
                    "age_minutes": age_minutes,
                    "artifact_kind": kind,
                    "premarket_rank": premarket_rank,
                }
                summary[symbol] = prev
            else:
                prev_news_score = int(prev.get("news_score", 0) or 0)
                prev_event_score = float(prev.get("event_score", 0.0) or 0.0)
            prev["news_score"] = int(max(float(prev["news_score"]), score))
            prev["event_score"] = max(float(prev["event_score"]), event_score)
            prev["catalyst_score"] = max(
                float(prev.get("catalyst_score", 0.0) or 0.0),
                float(raw_catalyst_score),
                max(float(score), float(event_score)) / 10.0,
            )
            prev["article_count"] = max(int(prev["article_count"]), article_count)
            prev["sentiment"] = max(float(prev["sentiment"]), sentiment)
            if headline and (not prev["headline"] or score >= float(prev_news_score) or event_score >= float(prev_event_score)):
                prev["headline"] = headline
            if catalyst_type:
                prev["catalyst_type"] = catalyst_type
            if source:
                prev["source"] = source
            if premarket_rank is not None:
                try:
                    existing_rank = int(float(prev.get("premarket_rank") or premarket_rank))
                except (TypeError, ValueError):
                    existing_rank = premarket_rank
                prev["premarket_rank"] = min(existing_rank, premarket_rank)
            prev["generated_at"] = generated_at
            if emit_log:
                log.info(
                    "CATALYST_MATCH_DEBUG symbol=%s source=%s headline=%s age_minutes=%.1f raw_score=%.2f mapped_score=%.2f",
                    symbol,
                    source or "unknown",
                    (headline or "")[:180],
                    age_minutes,
                    float(raw_score),
                    float(score),
                )
                log.info(
                    "PREMARKET_ARTIFACT_SCORE_TRACE symbol=%s kind=%s source=%s ranking_score=%.2f "
                    "event_score=%.2f news_score=%.2f catalyst_score=%.2f catalyst_type=%s "
                    "summary_news_score=%d summary_event_score=%.2f summary_catalyst_score=%.2f",
                    symbol,
                    kind,
                    source or "unknown",
                    float(raw_score),
                    float(event_score),
                    float(score),
                    float(raw_catalyst_score),
                    str(catalyst_type or "unknown"),
                    int(prev["news_score"] or 0),
                    float(prev["event_score"] or 0.0),
                    float(prev["catalyst_score"] or 0.0),
                )
            if emit_log and (score > 0 or event_score > 0) and source in {"sec", "alpaca", "newsapi", "twitter"}:
                log.info(
                    "PREMARKET_CATALYST_APPLIED symbol=%s score=%.2f source=%s catalyst_type=%s headline=%s age_minutes=%.1f",
                    symbol,
                    float(max(score, event_score)),
                    source or "unknown",
                    str(prev["catalyst_type"] or "unknown"),
                    (headline or "")[:180],
                    age_minutes,
                )
            if emit_log and score <= 0 and event_score <= 0:
                log.info(
                    "PREMARKET_CATALYST_MISS symbol=%s reason=no_catalyst_score",
                    symbol,
                )
            cached_score = int(math.ceil(max(float(prev["news_score"]), float(prev["event_score"]))))
            cached_headline = str(prev["headline"] or headline or "")
            cached_sentiment = float(prev["sentiment"] or sentiment or 0.0)
            cached_catalyst = NewsCatalyst(
                symbol=symbol,
                score=cached_score,
                headline=cached_headline,
                source=source or str(payload.get("source") or kind),
                catalyst_type=prev["catalyst_type"],
                article_count=int(prev["article_count"]),
                sentiment=cached_sentiment,
            )
            _NEWS_CACHE[symbol] = _NewsCacheEntry(
                score=cached_score,
                headline=cached_headline,
                fetched_at=generated_at,
                catalyst=cached_catalyst,
                article_count=int(prev["article_count"]),
                sentiment=cached_sentiment,
                event_score=float(prev["event_score"]),
            )
        if emit_log:
            for sym in symbols:
                sym_u = str(sym or "").strip().upper()
                if sym_u and sym_u not in matched_symbols:
                    log.info(
                        "PREMARKET_CATALYST_MISS symbol=%s reason=no_matching_item",
                        sym_u,
                    )
    if emit_log:
        log.info("PREMARKET_ARTIFACT_LOADED count=%d", len(summary))
    return summary


def get_cached_news_metadata(
    symbol: str,
    *,
    now: datetime | None = None,
    max_age_seconds: float = 900.0,
    emit_log: bool = False,
) -> dict[str, Any] | None:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    entry = _NEWS_CACHE.get(sym)
    if entry is None:
        _log_news_lookup(sym, matched_articles=0, cache_hit=False, sentiment_score=0.0, reason="fetch_failure")
        return None
    if now is None:
        now = datetime.now(timezone.utc)
    age = (now - entry.fetched_at).total_seconds()
    if age > max(1.0, float(max_age_seconds)):
        if emit_log:
            log.debug("NEWS_CACHE stale metadata symbol=%s age=%.0fs", sym, age)
        _log_news_lookup(sym, matched_articles=0, cache_hit=True, sentiment_score=0.0, reason="stale cache")
        return None
    cat = entry.catalyst
    _log_news_lookup(
        sym,
        matched_articles=int(entry.article_count or 0),
        cache_hit=True,
        sentiment_score=float(entry.sentiment or 0.0),
    )
    return {
        "score": int(entry.score or 0),
        "headline": entry.headline or (cat.headline if cat is not None else ""),
        "article_count": int(entry.article_count or 0),
        "sentiment": float(entry.sentiment or 0.0),
        "event_score": float(entry.event_score or 0.0),
        "source": str(cat.source or "none") if cat is not None else "none",
        "catalyst_type": cat.catalyst_type if cat is not None else None,
    }


def get_cached_news_catalyst(
    symbol: str,
    *,
    now: datetime | None = None,
    max_age_seconds: float = 900.0,
    emit_log: bool = True,
) -> NewsCatalyst | None:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    entry = _NEWS_CACHE.get(sym)
    if entry is None or entry.catalyst is None:
        if emit_log:
            log.debug("NEWS_CACHE miss symbol=%s", sym)
        return None
    if now is None:
        now = datetime.now(timezone.utc)
    age = (now - entry.fetched_at).total_seconds()
    if age > max(1.0, float(max_age_seconds)):
        if emit_log:
            log.debug("NEWS_CACHE stale symbol=%s age=%.0fs", sym, age)
        return None
    return entry.catalyst


def get_cached_news_score(
    symbol: str,
    *,
    now: datetime | None = None,
    max_age_seconds: float = 900.0,
) -> tuple[int, str]:
    """Return the cached catalyst score for *symbol* if it is fresh enough."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return 0, "invalid_symbol"
    entry = _NEWS_CACHE.get(sym)
    if entry is None:
        _log_news_lookup(sym, matched_articles=0, cache_hit=False, sentiment_score=0.0, reason="fetch_failure")
        return 0, "cache_miss"
    if now is None:
        now = datetime.now(timezone.utc)
    age = (now - entry.fetched_at).total_seconds()
    if age > max(1.0, float(max_age_seconds)):
        _log_news_lookup(sym, matched_articles=0, cache_hit=True, sentiment_score=0.0, reason="stale cache")
        return 0, "cache_stale"
    _log_news_lookup(
        sym,
        matched_articles=int(entry.article_count or 0),
        cache_hit=True,
        sentiment_score=float(entry.sentiment or 0.0),
    )
    return int(entry.score or 0), "cache"


def news_cache_age_seconds(symbol: str, *, now: datetime | None = None) -> float | None:
    """Return cache age in seconds for *symbol*, or ``None`` if it is not cached."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    entry = _NEWS_CACHE.get(sym)
    if entry is None:
        return None
    now = now or datetime.now(timezone.utc)
    return float((now - entry.fetched_at).total_seconds())


def _news_dynamic_cfg(cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    root = dict(cfg or {})
    nested = root.get("news_dynamic_entry")
    if isinstance(nested, Mapping):
        return dict(nested)
    return root


def news_dynamic_starter_notional_usd(
    cfg: Mapping[str, Any] | None = None,
    *,
    normal_notional: float | None = None,
) -> float:
    """Starter BUY size for news-catalyst dynamic entries ($500–$750 band, optional %% of normal)."""
    nde = _news_dynamic_cfg(cfg)
    try:
        cap = float(nde.get("starter_notional_usd", 750.0) or 750.0)
    except (TypeError, ValueError):
        cap = 750.0
    cap = max(500.0, min(750.0, cap))
    if normal_notional is not None:
        try:
            frac = float(nde.get("starter_notional_fraction_of_normal", 0.25) or 0.25)
        except (TypeError, ValueError):
            frac = 0.25
        frac = max(0.05, min(1.0, frac))
        scaled = float(normal_notional) * frac
        return max(500.0, min(cap, scaled))
    return cap


def news_dynamic_entry_bypass_passes(
    *,
    symbol: str,
    news_score: int,
    relative_volume: float | None,
    price_above_vwap: bool,
    spread_pct: float | None,
    quote_unstable: bool = False,
    is_dynamic: bool = True,
    min_relative_volume: float | None = None,
    cfg: Mapping[str, Any] | None = None,
) -> tuple[bool, str]:
    """
    Allow dynamic scanner names to skip breakout confirmation when news + tape quality are strong.

    Logs ``NEWS_DYNAMIC_ENTRY_BYPASS`` on success and ``NEWS_DYNAMIC_ENTRY_BLOCKED`` on failure
    when *news_score* >= min threshold (candidate had news context).
    """
    sym = str(symbol or "").strip().upper()
    nde = _news_dynamic_cfg(cfg)
    try:
        min_score = int(nde.get("min_news_score", 2) or 2)
    except (TypeError, ValueError):
        min_score = 2
    try:
        min_rel = float(nde.get("min_relative_volume", 1.5) or 1.5)
    except (TypeError, ValueError):
        min_rel = 1.5
    if min_relative_volume is not None:
        try:
            min_rel = float(min_relative_volume)
        except (TypeError, ValueError):
            pass
    try:
        max_spread = float(nde.get("max_spread_pct", 1.0) or 1.0)
    except (TypeError, ValueError):
        max_spread = 1.0

    def _block(reason: str) -> tuple[bool, str]:
        if int(news_score or 0) >= min_score:
            log.info(
                "NEWS_DYNAMIC_ENTRY_BLOCKED symbol=%s reason=%s",
                sym or "?",
                reason,
            )
        return False, reason

    if not is_dynamic:
        return _block("core symbol")
    if quote_unstable:
        return _block("unstable quote")
    if int(news_score or 0) < min_score:
        return _block("news_score %d < %d" % (int(news_score or 0), min_score))
    if not price_above_vwap:
        return _block("price not above session VWAP")
    if relative_volume is None or not math.isfinite(float(relative_volume)):
        return _block("relative_volume unavailable")
    if float(relative_volume) < min_rel:
        return _block("relative_volume %.2f < %.2f" % (float(relative_volume), min_rel))
    if spread_pct is None or not math.isfinite(float(spread_pct)):
        return _block("spread_pct unavailable")
    if float(spread_pct) > max_spread:
        return _block("spread %.3f%% > %.3f%%" % (float(spread_pct), max_spread))

    log.info(
        "NEWS_DYNAMIC_ENTRY_BYPASS symbol=%s news_score=%d rel_volume=%.2f spread=%.3f%% reason=news_catalyst",
        sym,
        int(news_score),
        float(relative_volume),
        float(spread_pct),
    )
    return True, "news_catalyst"


def news_early_entry_passes(
    *,
    news_score: int,
    relative_volume: float | None,
    price_above_vwap: bool | None,
    spread_pct: float | None,
    bars_1m: pd.DataFrame | None = None,
    cfg: Mapping[str, Any] | None = None,
) -> tuple[bool, str]:
    """
    Scanner-side early accept for strong news (``news_score >= 3``) before gain/volume filters.

    Used during dynamic scan row evaluation — not the live breakout bypass (see
    :func:`news_dynamic_entry_bypass_passes`).
    """
    _ = bars_1m
    nde = _news_dynamic_cfg(cfg)
    try:
        min_score = int(nde.get("early_min_news_score", 3) or 3)
    except (TypeError, ValueError):
        min_score = 3
    try:
        min_rel = float(nde.get("early_min_relative_volume", 1.5) or 1.5)
    except (TypeError, ValueError):
        min_rel = 1.5
    try:
        max_spread = float(nde.get("early_max_spread_pct", 1.5) or 1.5)
    except (TypeError, ValueError):
        max_spread = 1.5

    if int(news_score or 0) < min_score:
        return False, "news_score %d < %d" % (int(news_score or 0), min_score)
    if price_above_vwap is not True:
        return False, "price not above session VWAP"
    if relative_volume is None or not math.isfinite(float(relative_volume)):
        return False, "relative_volume unavailable"
    if float(relative_volume) < min_rel:
        return False, "relative_volume %.2f < %.2f" % (float(relative_volume), min_rel)
    if spread_pct is None or not math.isfinite(float(spread_pct)):
        return False, "spread_pct unavailable"
    if float(spread_pct) > max_spread:
        return False, "spread %.3f%% > %.3f%%" % (float(spread_pct), max_spread)
    return True, "news_early_entry"


def get_news_score(
    symbol: str,
    *,
    config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[int, str]:
    """
    Return cached news score for *symbol*.

    Safe fallback: when ``news_ai.enabled`` is false, or there is no cached catalyst, returns ``(0, ...)``.
    No live network call is performed here.
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        return 0, "invalid_symbol"
    ai_cfg = _news_ai_config(config)
    enabled = bool(ai_cfg.get("enabled", False))
    if not enabled:
        log.info("NEWS_SCORE symbol=%s score=0 reason=disabled", sym)
        return 0, "disabled"
    try:
        max_age = float(ai_cfg.get("cache_max_age_seconds", 3600) or 3600)
    except (TypeError, ValueError):
        max_age = 3600.0
    cat = get_cached_news_catalyst(sym, now=now, max_age_seconds=max_age, emit_log=False)
    if cat is None:
        log.info("NEWS_SCORE symbol=%s score=0 reason=safe_default", sym)
        return 0, "safe_default"
    reason = cat.catalyst_type or "cache"
    log.info("NEWS_SCORE symbol=%s score=%d reason=%s", sym, int(cat.score), reason)
    return int(cat.score), reason


__all__ = [
    "NewsCatalyst",
    "article_applies_to_symbol",
    "extract_article_symbols",
    "fetch_recent_news_catalysts",
    "get_cached_news_catalyst",
    "get_cached_news_score",
    "headline_mentions_symbol",
    "load_premarket_artifacts",
    "premarket_artifact_paths",
    "news_dynamic_entry_bypass_passes",
    "news_dynamic_starter_notional_usd",
    "news_early_entry_passes",
    "get_cached_news_metadata",
    "news_pipeline_summary",
    "get_news_score",
    "news_cache_age_seconds",
    "news_refresh_phase_for_et",
    "score_article_text",
]
