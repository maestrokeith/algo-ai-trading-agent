"""Best-effort news catalyst scoring for dynamic momentum entries."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from src.news_catalyst import get_cached_news_catalyst

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CatalystScore:
    symbol: str
    score: int
    summary: str


_CACHE: dict[str, tuple[float, CatalystScore]] = {}

_STRONG_TERMS = (
    "earnings",
    "revenue",
    "profit",
    "eps",
    "guidance",
    "raises outlook",
    "raises forecast",
    "fda",
    "approval",
    "approved",
    "clinical",
    "trial",
    "contract",
    "deal",
    "order",
    "partnership",
    "partner",
    "collaboration",
    "upgrade",
    "upgraded",
    "price target",
    "sector",
    "tariff",
    "chip",
    "ai demand",
)
_WEAK_TERMS = (
    "penny stock",
    "sponsored",
    "promotion",
    "promotional",
    "newsletter",
    "watchlist",
    "rumor",
    "meme",
    "could",
    "might",
    "why is",
    "what's going on",
    "stock moves",
    "shares move",
)


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


def _clean_summary(text: str) -> str:
    one_line = re.sub(r"\s+", " ", str(text or "")).strip()
    return one_line[:160] if one_line else "neutral/no catalyst"


def _score_articles(
    symbol: str,
    articles: list[Mapping[str, Any]],
    *,
    now: datetime,
    fresh_hours: float,
    stale_hours: float,
) -> CatalystScore:
    sym = str(symbol or "").strip().upper()
    if not articles:
        return CatalystScore(sym, 50, "no recent news found")

    best_score = 40
    best_summary = "news vague or unrelated"
    for article in articles:
        text = _article_text(article)
        low = text.lower()
        if not text:
            continue

        published = _parse_dt(article.get("publishedAt"))
        age_h = None
        if published is not None:
            age_h = max(0.0, (now - published.astimezone(timezone.utc)).total_seconds() / 3600.0)

        score = 50
        if sym and sym.lower() not in low:
            score -= 10
        if any(term in low for term in _STRONG_TERMS):
            score = max(score, 72)
        if any(term in low for term in _WEAK_TERMS):
            score = min(score, 40)
        if age_h is not None:
            if age_h <= fresh_hours:
                score += 5
            elif age_h > stale_hours:
                score = min(score, 40)
        score = int(max(0, min(100, score)))
        if score > best_score:
            best_score = score
            best_summary = _clean_summary(text)

    return CatalystScore(sym, int(best_score), best_summary)


def score_ai_catalyst(
    symbol: str,
    config: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> CatalystScore:
    """Return catalyst score in [0, 100], neutral on missing config, no news, or failures."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return CatalystScore("?", 50, "no symbol")
    dme = config.get("dynamic_momentum_entry") if isinstance(config, Mapping) else {}
    ai_cfg = dme.get("ai_catalyst") if isinstance(dme, Mapping) else {}
    ai_cfg = ai_cfg if isinstance(ai_cfg, Mapping) else {}
    enabled = bool(ai_cfg.get("enabled", True))
    if not enabled:
        return CatalystScore(sym, 50, "ai catalyst disabled")

    try:
        ttl = float(ai_cfg.get("cache_ttl_seconds", 900) or 900)
    except (TypeError, ValueError):
        ttl = 900.0
    now_ts = time.time()
    cached = _CACHE.get(sym)
    if cached and now_ts - cached[0] < max(1.0, ttl):
        return cached[1]

    try:
        lookback_h = int(ai_cfg.get("lookback_hours", 24) or 24)
    except (TypeError, ValueError):
        lookback_h = 24
    try:
        page_size = int(ai_cfg.get("max_articles", 10) or 10)
    except (TypeError, ValueError):
        page_size = 10
    try:
        fresh_h = float(ai_cfg.get("fresh_hours", 12) or 12)
    except (TypeError, ValueError):
        fresh_h = 12.0
    try:
        stale_h = float(ai_cfg.get("stale_hours", 48) or 48)
    except (TypeError, ValueError):
        stale_h = 48.0

    cached_news = get_cached_news_catalyst(
        sym,
        now=now,
        max_age_seconds=max(1.0, ttl),
        emit_log=False,
    )
    if cached_news is None:
        result = CatalystScore(sym, 50, "news catalyst cache empty")
        _CACHE[sym] = (now_ts, result)
        return result
    raw_score = int(cached_news.score)
    if raw_score >= 3:
        score = 78
    elif raw_score > 0:
        score = 62
    elif raw_score <= -3:
        score = 25
    elif raw_score < 0:
        score = 40
    else:
        score = 50
    result = CatalystScore(sym, score, cached_news.headline or "cached news catalyst")
    _CACHE[sym] = (now_ts, result)
    return result


__all__ = ["CatalystScore", "score_ai_catalyst"]
