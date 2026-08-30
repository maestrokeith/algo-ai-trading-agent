"""Read-only social sentiment collection for premarket diagnostics."""

from __future__ import annotations

import json
import os
import re
import time as time_module
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import requests


ARTIFACT_PATH = Path("data") / "premarket" / "social_sentiment_latest.json"
REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_SEARCH_URL_TEMPLATE = "https://oauth.reddit.com/r/{subreddit}/search"
DEFAULT_SUBREDDITS = ["stocks", "wallstreetbets", "investing", "SecurityAnalysis"]
PUMP_WORDS = {
    "moon",
    "mooning",
    "rocket",
    "rockets",
    "squeeze",
    "short squeeze",
    "pump",
    "pump it",
    "guaranteed",
    "can't lose",
    "cant lose",
    "to the moon",
}
BULLISH_WORDS = {
    "beat",
    "beats",
    "breakout",
    "bull",
    "bullish",
    "buy",
    "calls",
    "growth",
    "long",
    "upgrade",
    "upside",
}
BEARISH_WORDS = {
    "bear",
    "bearish",
    "downgrade",
    "fraud",
    "miss",
    "puts",
    "sell",
    "short",
    "warning",
    "weak",
}


@dataclass(frozen=True)
class RedditCredentials:
    """Resolved Reddit OAuth client credentials."""

    client_id: str
    client_secret: str
    user_agent: str

    @property
    def present(self) -> bool:
        return bool(self.client_id and self.client_secret and self.user_agent)


@dataclass
class SocialProviderDiagnostic:
    """Provider-level diagnostics for the social sentiment collector."""

    enabled: bool
    request_sent: bool = False
    http_status: int | None = None
    raw_count: int = 0
    filtered_count: int = 0
    rate_limited: bool = False
    duration_ms: float = 0.0
    reason: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "request_sent": bool(self.request_sent),
            "http_status": self.http_status,
            "raw_count": int(self.raw_count),
            "filtered_count": int(self.filtered_count),
            "rate_limited": bool(self.rate_limited),
            "duration_ms": round(float(self.duration_ms or 0.0), 1),
            "reason": self.reason,
        }


@dataclass
class SocialPost:
    """Normalized social mention candidate."""

    symbol: str
    title: str
    author: str
    source: str
    created_utc: float
    score: int = 0
    url: str = ""
    author_created_utc: float | None = None


@dataclass
class SymbolSocialSentiment:
    """Aggregated social sentiment for one symbol."""

    symbol: str
    mention_count: int = 0
    unique_author_count: int = 0
    bullish_count: int = 0
    bearish_count: int = 0
    neutral_count: int = 0
    sentiment_score: float = 0.0
    mention_velocity_score: float = 0.0
    top_post_titles: list[str] = field(default_factory=list)
    source_breakdown: dict[str, int] = field(default_factory=dict)
    passed_min_unique_authors: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "mention_count": int(self.mention_count),
            "unique_author_count": int(self.unique_author_count),
            "bullish_count": int(self.bullish_count),
            "bearish_count": int(self.bearish_count),
            "neutral_count": int(self.neutral_count),
            "sentiment_score": round(float(self.sentiment_score), 4),
            "mention_velocity_score": round(float(self.mention_velocity_score), 4),
            "top_post_titles": list(self.top_post_titles[:5]),
            "source_breakdown": dict(self.source_breakdown),
            "passed_min_unique_authors": bool(self.passed_min_unique_authors),
        }


def social_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    root = config or {}
    pm = root.get("premarket_intelligence")
    if not isinstance(pm, Mapping):
        pm = {}
    raw_social = pm.get("social")
    social = dict(raw_social) if isinstance(raw_social, Mapping) else {}
    raw_reddit = social.get("reddit")
    raw_twitter = social.get("twitter")
    social["enabled"] = _as_bool(social.get("enabled"), False)
    social["reddit"] = dict(raw_reddit) if isinstance(raw_reddit, Mapping) else {}
    social["twitter"] = dict(raw_twitter) if isinstance(raw_twitter, Mapping) else {}
    social["reddit"]["enabled"] = _as_bool(social["reddit"].get("enabled"), True)
    social["twitter"]["enabled"] = _as_bool(social["twitter"].get("enabled"), False)
    if not isinstance(social.get("subreddits"), (list, tuple)):
        social["subreddits"] = list(DEFAULT_SUBREDDITS)
    if not isinstance(social.get("trusted_twitter_accounts"), (list, tuple)):
        social["trusted_twitter_accounts"] = []
    return social


def resolve_reddit_credentials(config: Mapping[str, Any] | None = None) -> RedditCredentials:
    cfg = social_config(config)
    reddit_cfg = cfg.get("reddit") if isinstance(cfg.get("reddit"), Mapping) else {}
    client_id_env = str(reddit_cfg.get("client_id_env") or "REDDIT_CLIENT_ID")
    client_secret_env = str(reddit_cfg.get("client_secret_env") or "REDDIT_CLIENT_SECRET")
    user_agent_env = str(reddit_cfg.get("user_agent_env") or "REDDIT_USER_AGENT")
    return RedditCredentials(
        client_id=str(os.environ.get(client_id_env) or ""),
        client_secret=str(os.environ.get(client_secret_env) or ""),
        user_agent=str(os.environ.get(user_agent_env) or ""),
    )


def collect_social_sentiment(
    *,
    symbols: Sequence[str],
    config: Mapping[str, Any] | None = None,
    project_root: Path | str = ".",
    hours: int = 24,
    limit: int = 10,
    now: datetime | None = None,
    timeout_seconds: float = 10.0,
    confirmed_symbols: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Collect read-only social sentiment and persist the latest diagnostics artifact."""
    started = time_module.monotonic()
    when = _utc_now(now)
    hours_i = max(1, int(hours or 24))
    limit_i = max(1, min(100, int(limit or 10)))
    symbol_list = _clean_symbols(symbols)
    cfg = social_config(config)
    diagnostics: dict[str, SocialProviderDiagnostic] = {
        "twitter": SocialProviderDiagnostic(
            enabled=bool((cfg.get("twitter") or {}).get("enabled")),
            request_sent=False,
            reason="twitter_disabled" if not bool((cfg.get("twitter") or {}).get("enabled")) else "not_implemented",
        )
    }
    reddit_posts: list[SocialPost] = []
    if not bool(cfg.get("enabled")):
        diagnostics["reddit"] = SocialProviderDiagnostic(
            enabled=False,
            request_sent=False,
            reason="social_disabled",
            duration_ms=(time_module.monotonic() - started) * 1000.0,
        )
    elif not bool((cfg.get("reddit") or {}).get("enabled")):
        diagnostics["reddit"] = SocialProviderDiagnostic(
            enabled=False,
            request_sent=False,
            reason="reddit_disabled",
            duration_ms=(time_module.monotonic() - started) * 1000.0,
        )
    else:
        reddit_diag, reddit_posts = fetch_reddit_mentions(
            symbol_list,
            config=config,
            hours=hours_i,
            limit=limit_i,
            now=when,
            timeout_seconds=timeout_seconds,
            confirmed_symbols=confirmed_symbols,
        )
        diagnostics["reddit"] = reddit_diag
    rows = aggregate_social_posts(
        symbol_list,
        reddit_posts,
        hours=hours_i,
        config=config,
    )
    payload = {
        "generated_at": when.isoformat(),
        "symbols": symbol_list,
        "hours": hours_i,
        "providers": {name: diag.to_dict() for name, diag in sorted(diagnostics.items())},
        "items": [rows[sym].to_dict() for sym in symbol_list],
        "summary": {
            "symbol_count": len(symbol_list),
            "total_mentions": sum(row.mention_count for row in rows.values()),
            "total_unique_authors": len({post.author for post in reddit_posts if post.author}),
        },
    }
    path = Path(project_root) / ARTIFACT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    payload["artifact_path"] = str(path)
    return payload


def fetch_reddit_mentions(
    symbols: Sequence[str],
    *,
    config: Mapping[str, Any] | None = None,
    hours: int = 24,
    limit: int = 10,
    now: datetime | None = None,
    timeout_seconds: float = 10.0,
    confirmed_symbols: Sequence[str] | None = None,
) -> tuple[SocialProviderDiagnostic, list[SocialPost]]:
    """Fetch Reddit mentions through the official OAuth API."""
    started = time_module.monotonic()
    cfg = social_config(config)
    reddit_cfg = cfg.get("reddit") if isinstance(cfg.get("reddit"), Mapping) else {}
    diag = SocialProviderDiagnostic(enabled=True)
    creds = resolve_reddit_credentials(config)
    if not creds.present:
        diag.reason = "reddit_credentials_missing"
        diag.duration_ms = (time_module.monotonic() - started) * 1000.0
        return diag, []
    token_resp = requests.post(
        REDDIT_TOKEN_URL,
        auth=(creds.client_id, creds.client_secret),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": creds.user_agent},
        timeout=timeout_seconds,
    )
    diag.request_sent = True
    diag.http_status = int(token_resp.status_code)
    if token_resp.status_code == 429:
        diag.rate_limited = True
        diag.reason = "rate_limited"
        diag.duration_ms = (time_module.monotonic() - started) * 1000.0
        return diag, []
    if token_resp.status_code >= 400:
        diag.reason = f"reddit_http_{token_resp.status_code}"
        diag.duration_ms = (time_module.monotonic() - started) * 1000.0
        return diag, []
    token = str((token_resp.json() or {}).get("access_token") or "")
    if not token:
        diag.reason = "reddit_token_missing"
        diag.duration_ms = (time_module.monotonic() - started) * 1000.0
        return diag, []
    subreddits = [str(item).strip() for item in cfg.get("subreddits", DEFAULT_SUBREDDITS) if str(item).strip()]
    posts: list[SocialPost] = []
    raw_count = 0
    confirmed = {str(sym).strip().upper() for sym in confirmed_symbols or [] if str(sym).strip()}
    for subreddit in subreddits:
        for symbol in symbols:
            query = f"${symbol} OR {symbol}"
            resp = requests.get(
                REDDIT_SEARCH_URL_TEMPLATE.format(subreddit=subreddit),
                headers={"Authorization": f"bearer {token}", "User-Agent": creds.user_agent},
                params={
                    "q": query,
                    "restrict_sr": "1",
                    "sort": "new",
                    "t": "day",
                    "limit": max(1, min(100, int(limit))),
                },
                timeout=timeout_seconds,
            )
            diag.request_sent = True
            diag.http_status = int(resp.status_code)
            if resp.status_code == 429:
                diag.rate_limited = True
                diag.reason = "rate_limited"
                diag.duration_ms = (time_module.monotonic() - started) * 1000.0
                return diag, posts
            if resp.status_code >= 400:
                diag.reason = f"reddit_http_{resp.status_code}"
                continue
            children = ((resp.json() or {}).get("data") or {}).get("children") or []
            raw_count += len(children)
            for child in children:
                data = child.get("data") if isinstance(child, Mapping) else None
                if not isinstance(data, Mapping):
                    continue
                post = _post_from_reddit(data, symbol=symbol, subreddit=subreddit)
                if post is None:
                    continue
                if not _post_passes_filters(post, config=config, now=_utc_now(now), approved_symbols=symbols, confirmed_symbols=confirmed):
                    continue
                posts.append(post)
    diag.raw_count = raw_count
    diag.filtered_count = len(posts)
    diag.reason = "ok" if posts else ("no_filtered_mentions" if raw_count else "no_mentions")
    diag.duration_ms = (time_module.monotonic() - started) * 1000.0
    return diag, posts


def aggregate_social_posts(
    symbols: Sequence[str],
    posts: Sequence[SocialPost],
    *,
    hours: int,
    config: Mapping[str, Any] | None = None,
) -> dict[str, SymbolSocialSentiment]:
    cfg = social_config(config)
    min_unique = _int_cfg(cfg, "min_unique_authors", 2)
    author_cap = _int_cfg(cfg, "max_mentions_per_author", 2)
    out = {sym: SymbolSocialSentiment(symbol=sym) for sym in _clean_symbols(symbols)}
    by_symbol: dict[str, list[SocialPost]] = {sym: [] for sym in out}
    author_counts: dict[tuple[str, str], int] = {}
    for post in sorted(posts, key=lambda row: row.score, reverse=True):
        if post.symbol not in out:
            continue
        key = (post.symbol, post.author.lower())
        if author_counts.get(key, 0) >= author_cap:
            continue
        author_counts[key] = author_counts.get(key, 0) + 1
        by_symbol[post.symbol].append(post)
    for symbol, rows in by_symbol.items():
        agg = out[symbol]
        authors = {row.author for row in rows if row.author}
        bullish = bearish = neutral = 0
        sources: dict[str, int] = {}
        for row in rows:
            label = classify_social_sentiment(row.title)
            if label == "bullish":
                bullish += 1
            elif label == "bearish":
                bearish += 1
            else:
                neutral += 1
            sources[row.source] = sources.get(row.source, 0) + 1
        agg.mention_count = len(rows)
        agg.unique_author_count = len(authors)
        agg.bullish_count = bullish
        agg.bearish_count = bearish
        agg.neutral_count = neutral
        agg.passed_min_unique_authors = len(authors) >= min_unique
        agg.sentiment_score = ((bullish - bearish) / max(1, len(rows))) if agg.passed_min_unique_authors else 0.0
        agg.mention_velocity_score = min(1.0, len(rows) / max(1.0, float(hours))) if agg.passed_min_unique_authors else 0.0
        agg.top_post_titles = [row.title for row in rows[:5]]
        agg.source_breakdown = sources
    return out


def classify_social_sentiment(text: str) -> str:
    lowered = str(text or "").lower()
    bull = sum(1 for word in BULLISH_WORDS if word in lowered)
    bear = sum(1 for word in BEARISH_WORDS if word in lowered)
    if bull > bear:
        return "bullish"
    if bear > bull:
        return "bearish"
    return "neutral"


def format_social_sentiment(payload: Mapping[str, Any]) -> str:
    lines = [
        "SOCIAL_SENTIMENT symbols=%s hours=%s artifact=%s"
        % (",".join(payload.get("symbols") or []), payload.get("hours"), payload.get("artifact_path", "none")),
    ]
    providers = payload.get("providers") if isinstance(payload.get("providers"), Mapping) else {}
    for name, row in sorted(providers.items()):
        if not isinstance(row, Mapping):
            continue
        lines.append(
            "SOCIAL_PROVIDER provider=%s enabled=%s request_sent=%s http_status=%s raw_count=%s filtered_count=%s rate_limited=%s reason=%s"
            % (
                name,
                str(bool(row.get("enabled"))).lower(),
                str(bool(row.get("request_sent"))).lower(),
                row.get("http_status") if row.get("http_status") is not None else "none",
                int(row.get("raw_count") or 0),
                int(row.get("filtered_count") or 0),
                str(bool(row.get("rate_limited"))).lower(),
                row.get("reason") or "ok",
            )
        )
    for item in payload.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        lines.append(
            "SOCIAL_SYMBOL symbol=%s mentions=%d unique_authors=%d bullish=%d bearish=%d neutral=%d sentiment=%.4f velocity=%.4f"
            % (
                item.get("symbol"),
                int(item.get("mention_count") or 0),
                int(item.get("unique_author_count") or 0),
                int(item.get("bullish_count") or 0),
                int(item.get("bearish_count") or 0),
                int(item.get("neutral_count") or 0),
                float(item.get("sentiment_score") or 0.0),
                float(item.get("mention_velocity_score") or 0.0),
            )
        )
    return "\n".join(lines)


def _post_from_reddit(data: Mapping[str, Any], *, symbol: str, subreddit: str) -> SocialPost | None:
    title = str(data.get("title") or "").strip()
    if not title:
        return None
    return SocialPost(
        symbol=str(symbol or "").strip().upper(),
        title=title,
        author=str(data.get("author") or "").strip(),
        source=f"reddit:{subreddit}",
        created_utc=float(data.get("created_utc") or 0.0),
        score=int(data.get("score") or 0),
        url=str(data.get("url") or data.get("permalink") or ""),
        author_created_utc=_optional_float(data.get("author_created_utc")),
    )


def _post_passes_filters(
    post: SocialPost,
    *,
    config: Mapping[str, Any] | None,
    now: datetime,
    approved_symbols: Sequence[str],
    confirmed_symbols: set[str],
) -> bool:
    cfg = social_config(config)
    approved = set(_clean_symbols(approved_symbols))
    if post.symbol not in approved and _looks_like_penny_stock(post.symbol):
        return False
    min_account_age_days = _int_cfg(cfg, "min_account_age_days", 7)
    if post.author_created_utc:
        account_created = datetime.fromtimestamp(post.author_created_utc, tz=timezone.utc)
        if now - account_created < timedelta(days=min_account_age_days):
            return False
    if _contains_pump_language(post.title) and post.symbol not in confirmed_symbols:
        return False
    return _symbol_mentioned(post.symbol, post.title)


def _contains_pump_language(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(word in lowered for word in PUMP_WORDS)


def _symbol_mentioned(symbol: str, text: str) -> bool:
    sym = re.escape(str(symbol or "").strip().upper())
    if not sym:
        return False
    return bool(re.search(rf"(?<![A-Z0-9])\$?{sym}(?![A-Z0-9])", str(text or "").upper()))


def _looks_like_penny_stock(symbol: str) -> bool:
    return len(str(symbol or "").strip()) >= 5


def _clean_symbols(symbols: Sequence[str]) -> list[str]:
    out: list[str] = []
    for item in symbols:
        sym = str(item or "").strip().upper()
        if sym and sym not in out:
            out.append(sym)
    return out


def _utc_now(now: datetime | None = None) -> datetime:
    when = now or datetime.now(timezone.utc)
    if when.tzinfo is None:
        return when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _as_bool(raw: Any, default: bool = False) -> bool:
    if raw is None:
        return bool(default)
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in {"1", "true", "yes", "on", "y"}:
            return True
        if text in {"0", "false", "no", "off", "n", ""}:
            return False
    return bool(raw)


def _int_cfg(cfg: Mapping[str, Any], key: str, default: int) -> int:
    try:
        return int(cfg.get(key, default) or default)
    except (TypeError, ValueError):
        return int(default)


def _optional_float(raw: Any) -> float | None:
    try:
        if raw is None or str(raw).strip() == "":
            return None
        return float(raw)
    except (TypeError, ValueError):
        return None


__all__ = [
    "ARTIFACT_PATH",
    "SocialProviderDiagnostic",
    "SocialPost",
    "aggregate_social_posts",
    "collect_social_sentiment",
    "fetch_reddit_mentions",
    "format_social_sentiment",
]
