"""AI-style news ranking service for catalyst-driven symbol selection."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlparse


@dataclass(frozen=True)
class AINewsRank:
    """Quality, catalyst, and confidence scores for one news item."""

    symbol: str
    news_quality: float
    catalyst_strength: float
    llm_confidence: float
    combined_score: float
    catalyst_type: str
    rationale: str


_HIGH_QUALITY_SOURCES = {"alpaca", "benzinga", "sec", "earnings_overnight", "newsapi"}
_FINANCE_NEWS_SOURCE_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("reuters", 0.24),
    ("the information", 0.22),
    ("bloomberg", 0.22),
    ("wall street journal", 0.20),
    ("wsj", 0.20),
    ("cnbc", 0.18),
    ("barron's", 0.16),
    ("barrons", 0.16),
    ("marketwatch", 0.14),
    ("financial times", 0.14),
    ("ft.com", 0.14),
    ("benzinga", 0.12),
    ("seeking alpha", 0.10),
)
_SOFTWARE_PACKAGE_SOURCE_TERMS = (
    "pypi",
    "python package index",
    "npm",
    "npmjs",
    "rubygems",
    "nuget",
    "crates.io",
    "packagist",
    "github releases",
)
_SOFTWARE_PACKAGE_TERMS = (
    "pypi",
    "python package index",
    "python package",
    "package release",
    "software package",
    "pip install",
    "npm package",
    "package version",
    "package versions",
    "released on pypi",
    "wheel distribution",
    "source distribution",
    "package metadata",
)
_LOW_QUALITY_TERMS = (
    "rumor",
    "meme",
    "why is",
    "what's going on",
    "stock moves",
    "shares move",
    "watchlist",
    "sponsored",
    "promotion",
)
_CATALYST_STRENGTH = {
    "guidance": 0.94,
    "earnings": 0.90,
    "fda": 0.88,
    "deal": 0.84,
    "acquisition": 0.84,
    "analyst": 0.78,
    "ai": 0.76,
    "upgrade": 0.74,
    "product": 0.58,
    "sec_filing": 0.34,
    "unknown": 0.30,
    "news": 0.45,
}
_CATALYST_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bguidance\b|\braises? (?:outlook|forecast|guidance)\b", "guidance"),
    (r"\bearnings\b|\beps\b|\brevenue\b|\bquarterly results?\b", "earnings"),
    (r"\bfda\b|\bclinical\b|\btrial\b|\bapproval\b", "fda"),
    (r"\bvera\b|\brubin\b|\bcpu\b|\bgpu\b|\bprocessor\b|\bchip\b|\bsemiconductor\b", "product"),
    (r"\bartificial intelligence\b|\bgenerative ai\b|\bopenai\b|\bai\b", "ai"),
    (r"\bpartnership\b|\bcontract\b|\bcollaboration\b|\bacqui(?:res|sition)\b|\bdeal\b", "deal"),
    (r"\bupgrade(?:d|s)?\b|\bprice target\b|\banalyst\b|\boutperform\b", "analyst"),
    (r"\blaunch(?:es|ed)?\b|\bunveils?\b|\bproduct\b", "product"),
)


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if not math.isfinite(value):
        return lo
    return max(lo, min(hi, value))


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


def _source_blob(source: str = "", publisher: str = "", url: str = "") -> str:
    parts = [str(source or ""), str(publisher or "")]
    parsed = urlparse(str(url or ""))
    if parsed.netloc:
        parts.append(parsed.netloc)
    return " ".join(parts).casefold()


def finance_news_source_weight(source: str = "", publisher: str = "", url: str = "") -> float:
    """Return a positive quality adjustment for known finance/news publishers."""
    blob = _source_blob(source, publisher, url)
    for term, weight in _FINANCE_NEWS_SOURCE_WEIGHTS:
        if term in blob:
            return float(weight)
    return 0.0


def is_software_package_spam(text: str, source: str = "", publisher: str = "", url: str = "") -> bool:
    """True for package-release feed noise that should not become equity catalysts."""
    blob = " ".join((str(text or ""), str(source or ""), str(publisher or ""), str(url or ""))).casefold()
    if any(term in blob for term in _SOFTWARE_PACKAGE_SOURCE_TERMS):
        return True
    if any(term in blob for term in _SOFTWARE_PACKAGE_TERMS):
        return True
    if re.search(r"\b(?:version|v)\s*\d+\.\d+(?:\.\d+)?\b", blob) and "package" in blob:
        return True
    return False


def infer_catalyst_type(headline: str, catalyst_type: str = "") -> str:
    """Infer a normalized catalyst type from explicit metadata and headline text."""
    explicit = str(catalyst_type or "").strip().lower()
    if explicit and explicit not in {"news", "unknown"}:
        if explicit in {"upgrade", "downgrade"}:
            return "analyst"
        if explicit in {"ai_partnership"}:
            return "ai"
        return explicit
    low = str(headline or "").lower()
    for pattern, resolved in _CATALYST_PATTERNS:
        if re.search(pattern, low):
            return resolved
    return explicit or "unknown"


def score_news_quality(
    *,
    symbol: str,
    headline: str,
    source: str = "",
    publisher: str = "",
    url: str = "",
    published_at: Any = None,
    now: datetime | None = None,
) -> float:
    """Score source quality, specificity, freshness, and headline usefulness in [0, 1]."""
    sym = str(symbol or "").strip().upper()
    text = str(headline or "").strip()
    low = text.lower()
    score = 0.48
    if str(source or "").strip().lower() in _HIGH_QUALITY_SOURCES:
        score += 0.12
    score += finance_news_source_weight(source, publisher, url)
    if sym and re.search(rf"\b{re.escape(sym)}\b", text.upper()):
        score += 0.10
    if len(text) >= 45:
        score += 0.06
    if re.search(r"\b\d+(?:\.\d+)?%?\b", text):
        score += 0.06
    if any(term in low for term in _LOW_QUALITY_TERMS):
        score -= 0.22
    if is_software_package_spam(text, source=source, publisher=publisher, url=url):
        score -= 0.55
    published = _parse_dt(published_at)
    if published is not None:
        ref = now or datetime.now(timezone.utc)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        age_hours = max(0.0, (ref.astimezone(timezone.utc) - published.astimezone(timezone.utc)).total_seconds() / 3600.0)
        if age_hours <= 3:
            score += 0.08
        elif age_hours > 24:
            score -= 0.12
    return _clamp(score)


def score_catalyst_strength(*, headline: str, catalyst_type: str = "") -> tuple[str, float]:
    """Return normalized catalyst type and strength in [0, 1]."""
    resolved = infer_catalyst_type(headline, catalyst_type)
    return resolved, _clamp(float(_CATALYST_STRENGTH.get(resolved, 0.45)))


def _fallback_llm_confidence(
    *,
    news_quality: float,
    catalyst_strength: float,
    sentiment: float = 0.0,
) -> float:
    sentiment_component = _clamp((float(sentiment or 0.0) + 1.0) / 2.0)
    return _clamp(news_quality * 0.35 + catalyst_strength * 0.45 + sentiment_component * 0.20)


def rank_news_item(
    *,
    symbol: str,
    headline: str,
    source: str = "",
    publisher: str = "",
    url: str = "",
    catalyst_type: str = "",
    sentiment: float = 0.0,
    published_at: Any = None,
    now: datetime | None = None,
    llm_confidence: float | None = None,
) -> AINewsRank:
    """Rank one news item using deterministic AI-style scoring features."""
    quality = score_news_quality(
        symbol=symbol,
        headline=headline,
        source=source,
        publisher=publisher,
        url=url,
        published_at=published_at,
        now=now,
    )
    resolved_type, strength = score_catalyst_strength(
        headline=headline,
        catalyst_type=catalyst_type,
    )
    confidence = _clamp(float(llm_confidence)) if llm_confidence is not None else _fallback_llm_confidence(
        news_quality=quality,
        catalyst_strength=strength,
        sentiment=sentiment,
    )
    combined = _clamp(quality * 0.30 + strength * 0.40 + confidence * 0.30)
    rationale = (
        f"quality={quality:.2f} catalyst_strength={strength:.2f} "
        f"llm_confidence={confidence:.2f} catalyst_type={resolved_type} "
        f"source_weight={finance_news_source_weight(source, publisher, url):.2f} "
        f"package_spam={str(is_software_package_spam(headline, source=source, publisher=publisher, url=url)).lower()}"
    )
    return AINewsRank(
        symbol=str(symbol or "").strip().upper(),
        news_quality=quality,
        catalyst_strength=strength,
        llm_confidence=confidence,
        combined_score=combined,
        catalyst_type=resolved_type,
        rationale=rationale,
    )


def score_adjustment(rank: AINewsRank, *, weight: float = 2.0) -> float:
    """Convert an AI news rank into a bounded premarket ranking score adjustment."""
    return round((float(rank.combined_score) - 0.50) * float(weight), 4)


def ai_news_ranking_enabled(config: Mapping[str, Any] | None) -> bool:
    raw = config.get("ai_news_ranking") if isinstance(config, Mapping) else None
    if not isinstance(raw, Mapping):
        return False
    return bool(raw.get("enabled", False))


def ai_news_ranking_weight(config: Mapping[str, Any] | None) -> float:
    raw = config.get("ai_news_ranking") if isinstance(config, Mapping) else None
    if not isinstance(raw, Mapping):
        return 2.0
    try:
        return max(0.0, min(5.0, float(raw.get("score_weight", 2.0) or 2.0)))
    except (TypeError, ValueError):
        return 2.0


__all__ = [
    "AINewsRank",
    "ai_news_ranking_enabled",
    "ai_news_ranking_weight",
    "finance_news_source_weight",
    "infer_catalyst_type",
    "is_software_package_spam",
    "rank_news_item",
    "score_adjustment",
    "score_catalyst_strength",
    "score_news_quality",
]
