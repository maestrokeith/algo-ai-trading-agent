"""Provider-level diagnostics for the premarket intelligence job."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from src.brokers.alpaca_client import (
    AlpacaCredentialResolution,
    fetch_alpaca_news_with_credentials,
    resolve_alpaca_credentials,
)
from src.news_sentiment.newsapi_client import newsapi_key_from_config

log = logging.getLogger(__name__)


def _emit(msg: str, *, level: int = logging.INFO) -> None:
    log.log(level, msg)
    print(msg, flush=True)


def _news_provider_line(provider: str, **fields: Any) -> None:
    parts = [f"PREMARKET_NEWS_PROVIDER provider={provider}"]
    for key, value in fields.items():
        parts.append(f"{key}={value}")
    _emit(" ".join(parts))


def _sec_provider_line(**fields: Any) -> None:
    parts = ["PREMARKET_SEC_PROVIDER"]
    for key, value in fields.items():
        parts.append(f"{key}={value}")
    _emit(" ".join(parts))


def _provider_creds_line(provider: str, **fields: Any) -> None:
    parts = [f"PREMARKET_PROVIDER_CREDS provider={provider}"]
    for key, value in fields.items():
        parts.append(f"{key}={value}")
    _emit(" ".join(parts))


def _premarket_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    root = config or {}
    raw = root.get("premarket_intelligence")
    if isinstance(raw, Mapping):
        return dict(raw)
    return {}


def _news_sentiment_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    root = config or {}
    raw = root.get("news_sentiment")
    if isinstance(raw, Mapping):
        return dict(raw)
    return {}


def _newsapi_enabled(config: Mapping[str, Any] | None) -> bool:
    root = config or {}
    raw_pm = root.get("premarket_intelligence")
    if isinstance(raw_pm, Mapping):
        raw_nested = raw_pm.get("newsapi")
        if isinstance(raw_nested, Mapping) and "enabled" in raw_nested:
            return _as_bool(raw_nested.get("enabled"), default=False)
        if "newsapi_enabled" in raw_pm:
            return _as_bool(raw_pm.get("newsapi_enabled"), default=False)
    ns = _news_sentiment_config(config)
    return _as_bool(ns.get("enabled"), default=False)


def _as_bool(raw: Any, *, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in {"1", "true", "yes", "on", "y"}:
            return True
        if text in {"0", "false", "no", "off", "n", ""}:
            return False
    return bool(raw)


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def resolve_premarket_alpaca_credentials(
    config: Mapping[str, Any] | None,
    market_client: Any = None,
) -> AlpacaCredentialResolution:
    """Resolve Alpaca credentials honoring broker paper/live mode when available."""
    paper = getattr(market_client, "paper", None)
    return resolve_alpaca_credentials(config, paper=paper, paper_fallback_on_live=True)


def log_premarket_provider_creds(
    config: Mapping[str, Any] | None,
    market_client: Any = None,
) -> AlpacaCredentialResolution:
    """Emit ``PREMARKET_PROVIDER_CREDS`` for Alpaca with live/paper env detection."""
    paper = getattr(market_client, "paper", None)
    creds = resolve_alpaca_credentials(config, paper=paper, paper_fallback_on_live=True)
    _provider_creds_line(
        "alpaca",
        mode=creds.mode,
        live_key_present=_bool_text(creds.live_key_present),
        paper_key_present=_bool_text(creds.paper_key_present),
        selected=creds.selected,
    )
    if not creds.credentials_present:
        _provider_creds_line(
            "alpaca",
            reason=f"missing_{creds.mode}_credentials",
        )
    return creds


def log_premarket_universe(symbols: Sequence[str]) -> None:
    uniq = [str(s).strip().upper() for s in symbols if str(s).strip()]
    uniq = list(dict.fromkeys(uniq))
    sample = ",".join(uniq[:5])
    _emit(f"PREMARKET_UNIVERSE symbols={len(uniq)} sample={sample}")


def log_newsapi_preflight(config: Mapping[str, Any] | None) -> bool:
    """
    Emit NewsAPI enabled / api_key lines before the batch fetch.

    Returns ``True`` when a fetch should be attempted.
    """
    enabled = _newsapi_enabled(config)
    _news_provider_line("newsapi", enabled=_bool_text(enabled))
    if not enabled:
        _news_provider_line("newsapi", reason="newsapi_disabled")
        return False

    api_key = newsapi_key_from_config(dict(config or {}))
    key_present = bool(api_key)
    _news_provider_line("newsapi", api_key_present=_bool_text(key_present))
    if not key_present:
        ns = _news_sentiment_config(config)
        env_name = str(ns.get("newsapi_key_env") or "NEWSAPI_KEY")
        _news_provider_line("newsapi", reason=f"missing_env_{env_name}")
        return False
    return True


def log_newsapi_fetch_result(
    meta: Mapping[str, Any] | None,
    *,
    articles: int,
    duration_ms: float | None = None,
) -> None:
    """Emit NewsAPI request/response lines after :func:`fetch_articles_query` runs."""
    m = dict(meta or {})
    request_sent = bool(m.get("request_sent"))
    _news_provider_line("newsapi", request_sent=_bool_text(request_sent))
    if not request_sent and m.get("skip_reason"):
        _news_provider_line("newsapi", reason=str(m["skip_reason"]))
    if m.get("http_status") is not None:
        _news_provider_line("newsapi", http_status=int(m["http_status"]))
    if request_sent and m.get("skip_reason"):
        _news_provider_line("newsapi", reason=str(m["skip_reason"]))
    if m.get("error"):
        _news_provider_line("newsapi", error=str(m["error"])[:240])
    _news_provider_line("newsapi", articles=int(articles))
    if duration_ms is not None:
        _news_provider_line("newsapi", duration_ms=f"{float(duration_ms):.1f}")
    if int(articles) == 0 and request_sent:
        if m.get("query"):
            _news_provider_line("newsapi", query=str(m["query"])[:500])
        if m.get("from"):
            _news_provider_line("newsapi", from_date=str(m["from"]))
        if m.get("to"):
            _news_provider_line("newsapi", to_date=str(m["to"]))


def _fetch_alpaca_news_articles(
    market_client: Any,
    creds: AlpacaCredentialResolution,
    symbols: Sequence[str],
    *,
    start: datetime,
    end: datetime,
    limit: int,
) -> tuple[list[Any], str]:
    """Return ``(articles, skip_reason)`` using broker client or resolved credentials."""
    if market_client is not None and getattr(market_client, "_news", None) is not None:
        if callable(getattr(market_client, "get_recent_news", None)):
            try:
                articles = market_client.get_recent_news(
                    list(symbols),
                    start=start,
                    end=end,
                    limit=limit,
                )
                return list(articles or []), ""
            except Exception as exc:
                return [], f"{exc.__class__.__name__}:{str(exc)[:200]}"

    if creds.credentials_present:
        try:
            articles = fetch_alpaca_news_with_credentials(
                creds.api_key,
                creds.secret,
                symbols,
                start=start,
                end=end,
                limit=limit,
            )
            return list(articles or []), ""
        except Exception as exc:
            return [], f"{exc.__class__.__name__}:{str(exc)[:200]}"

    return [], "missing_alpaca_credentials"


def log_alpaca_news_provider(
    market_client: Any,
    symbols: Sequence[str],
    *,
    config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> int:
    """Probe Alpaca news and emit provider diagnostics. Returns article count."""
    pm = _premarket_config(config)
    provider_enabled = _as_bool(pm.get("alpaca_news_enabled"), default=True)
    _news_provider_line("alpaca", enabled=_bool_text(provider_enabled))
    if not provider_enabled:
        _news_provider_line("alpaca", reason="alpaca_news_disabled_in_config")
        return 0

    creds = log_premarket_provider_creds(config, market_client)
    credentials_present = creds.credentials_present or (
        market_client is not None
        and getattr(market_client, "_news", None) is not None
        and callable(getattr(market_client, "get_recent_news", None))
    )
    _news_provider_line("alpaca", credentials_present=_bool_text(credentials_present))
    if not credentials_present:
        _news_provider_line("alpaca", reason=f"missing_{creds.mode}_credentials")
        return 0

    uniq = [str(s).strip().upper() for s in symbols if str(s).strip()]
    uniq = list(dict.fromkeys(uniq))
    if not uniq:
        _news_provider_line("alpaca", reason="empty_symbol_universe")
        return 0

    if now is None:
        now = datetime.now(timezone.utc)
    try:
        lookback_h = int(_news_sentiment_config(config).get("headline_lookback_hours", 24) or 24)
    except (TypeError, ValueError):
        lookback_h = 24
    start = now - timedelta(hours=max(1, lookback_h))
    limit = min(100, max(50, len(uniq) * 3))

    articles, skip_reason = _fetch_alpaca_news_articles(
        market_client,
        creds,
        uniq,
        start=start,
        end=now,
        limit=limit,
    )
    count = len(articles)
    _news_provider_line("alpaca", request_sent=_bool_text(True))
    if skip_reason:
        _news_provider_line("alpaca", reason=skip_reason)
    _news_provider_line("alpaca", articles=count)
    return count


def log_newsapi_rate_limit_result() -> None:
    """Emit NewsAPI lines when batch fetch hits HTTP 429."""
    _news_provider_line("newsapi", request_sent=_bool_text(True))
    _news_provider_line("newsapi", http_status=429)
    _news_provider_line("newsapi", reason="rate_limited")
    _news_provider_line("newsapi", articles=0)


def log_premarket_sec_provider(
    config: Mapping[str, Any] | None,
    symbols: Sequence[str],
) -> tuple[int, int]:
    """
    Emit SEC provider diagnostics.

    Returns ``(filings_count, cik_mapped_count)``. SEC fetch is optional; when disabled or
    unimplemented, counts are zero with an explicit reason in logs.
    """
    pm = _premarket_config(config)
    enabled = _as_bool(pm.get("sec_filings_enabled"), default=False)
    _sec_provider_line(enabled=_bool_text(enabled))
    if not enabled:
        _sec_provider_line(reason="sec_filings_disabled_in_config")
        _sec_provider_line(cik_mapped=0)
        _sec_provider_line(filings=0)
        return 0, 0

    uniq = [str(s).strip().upper() for s in symbols if str(s).strip()]
    uniq = list(dict.fromkeys(uniq))
    cik_map = pm.get("symbol_cik")
    cik_mapped = 0
    if isinstance(cik_map, Mapping):
        for sym in uniq:
            if str(cik_map.get(sym) or cik_map.get(sym.upper()) or "").strip():
                cik_mapped += 1

    _sec_provider_line(cik_mapped=cik_mapped)
    if cik_mapped == 0:
        _sec_provider_line(reason="no_symbol_cik_map_configured")
        _sec_provider_line(filings=0)
        return 0, 0

    _sec_provider_line(reason="sec_fetch_not_implemented")
    _sec_provider_line(filings=0)
    return 0, cik_mapped
