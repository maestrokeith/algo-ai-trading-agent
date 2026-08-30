"""Read-only news provider diagnostics for premarket troubleshooting."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.brokers.alpaca_client import resolve_alpaca_credentials
from src.news_sentiment.newsapi_client import newsapi_key_from_config
from src.premarket_intelligence import (
    ProviderExecResult,
    fetch_alpaca_news_events,
    fetch_newsapi_articles,
    fetch_sec_filings,
)


ARTIFACT_PATH = Path("data") / "premarket" / "news_diagnostics_latest.json"


@dataclass(frozen=True)
class NewsDiagnosticRequest:
    """Request parameters for one read-only provider diagnostic."""

    provider: str
    symbol: str
    hours: int = 24
    limit: int = 10


def _provider_config(base_config: Mapping[str, Any] | None, request: NewsDiagnosticRequest) -> dict[str, Any]:
    cfg = dict(base_config or {})
    pm = dict(cfg.get("premarket_intelligence") or {})
    ns = dict(cfg.get("news_sentiment") or {})
    pm.setdefault("enabled", True)
    pm.setdefault("newsapi_enabled", True)
    pm.setdefault("alpaca_news_enabled", True)
    pm.setdefault("sec_filings_enabled", True)
    ns["enabled"] = True
    ns["headline_lookback_hours"] = max(1, int(request.hours))
    ns["max_headlines"] = max(1, int(request.limit))
    cfg["premarket_intelligence"] = pm
    cfg["news_sentiment"] = ns
    return cfg


def _credentials_present(provider: str, cfg: Mapping[str, Any]) -> bool:
    if provider == "newsapi":
        return bool(newsapi_key_from_config(dict(cfg)))
    if provider == "alpaca":
        return bool(resolve_alpaca_credentials(cfg, paper=None, paper_fallback_on_live=True).credentials_present)
    if provider == "sec":
        return True
    return False


def _filtered_titles(result: ProviderExecResult) -> list[str]:
    titles = [str(ev.headline or "").strip() for ev in result.events if str(ev.headline or "").strip()]
    if not titles:
        titles = [str(title).strip() for title in result.sample_article_titles if str(title).strip()]
    return titles[:5]


def _raw_titles(result: ProviderExecResult) -> list[str]:
    # ProviderExecResult stores sample_article_titles before filtering for NewsAPI/Alpaca.
    return [str(title).strip() for title in result.sample_article_titles if str(title).strip()][:5]


def _reason(result: ProviderExecResult) -> str:
    if result.skip_reason:
        return result.skip_reason
    if result.error:
        return result.error
    raw_count = int(result.raw_articles_before_filter or result.articles or result.filings or 0)
    filtered_count = int(result.articles_after_filter or result.articles or result.filings or 0)
    if raw_count == 0:
        return "no_raw_results"
    if filtered_count == 0:
        return "no_filtered_results"
    return "ok"


def _result_payload(
    *,
    request: NewsDiagnosticRequest,
    result: ProviderExecResult,
    cfg: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    raw_count = int(result.raw_articles_before_filter or result.articles or result.filings or 0)
    filtered_count = int(result.articles_after_filter or result.articles or result.filings or 0)
    return {
        "generated_at": now.isoformat(),
        "provider": request.provider,
        "symbol": request.symbol,
        "request_parameters": {
            "symbol": request.symbol,
            "hours": int(request.hours),
            "limit": int(request.limit),
        },
        "credentials_present": _credentials_present(request.provider, cfg),
        "request_sent": bool(result.request_sent),
        "http_status": result.http_status,
        "raw_count": raw_count,
        "filtered_count": filtered_count,
        "raw_headlines": _raw_titles(result),
        "filtered_headlines": _filtered_titles(result),
        "reason": _reason(result),
    }


def run_news_diagnostic(
    *,
    provider: str,
    symbol: str,
    hours: int = 24,
    limit: int = 10,
    config: Mapping[str, Any] | None = None,
    project_root: Path | str = ".",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run one read-only news provider diagnostic and persist the latest artifact."""
    provider_s = str(provider or "").strip().lower()
    if provider_s not in {"alpaca", "newsapi", "sec"}:
        raise ValueError("provider must be one of: alpaca, newsapi, sec")
    symbol_s = str(symbol or "").strip().upper()
    if not symbol_s:
        raise ValueError("symbol is required")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    request = NewsDiagnosticRequest(provider=provider_s, symbol=symbol_s, hours=max(1, int(hours)), limit=max(1, int(limit)))
    cfg = _provider_config(config, request)
    timeout_seconds = 10.0
    if provider_s == "alpaca":
        result = fetch_alpaca_news_events([symbol_s], cfg, timeout_seconds, now=now)
    elif provider_s == "newsapi":
        result = fetch_newsapi_articles([symbol_s], cfg, timeout_seconds, now=now, rate_limit_log_state={})
    else:
        result = fetch_sec_filings([symbol_s], cfg, timeout_seconds, now=now)
    payload = _result_payload(request=request, result=result, cfg=cfg, now=now)
    path = Path(project_root) / ARTIFACT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    payload["artifact_path"] = str(path)
    return payload


def format_news_diagnostic(payload: Mapping[str, Any]) -> str:
    """Render a concise diagnostics report for CLI use."""
    params = payload.get("request_parameters") if isinstance(payload.get("request_parameters"), Mapping) else {}
    lines = [
        f"NEWS_DIAGNOSTICS provider={payload.get('provider')} symbol={payload.get('symbol')}",
        (
            "request_parameters "
            f"symbol={params.get('symbol')} hours={params.get('hours')} limit={params.get('limit')}"
        ),
        (
            "status "
            f"credentials_present={str(bool(payload.get('credentials_present'))).lower()} "
            f"request_sent={str(bool(payload.get('request_sent'))).lower()} "
            f"http_status={payload.get('http_status') if payload.get('http_status') is not None else 'none'} "
            f"raw_count={int(payload.get('raw_count') or 0)} "
            f"filtered_count={int(payload.get('filtered_count') or 0)} "
            f"reason={payload.get('reason') or 'ok'}"
        ),
        "raw_headlines:",
    ]
    raw = payload.get("raw_headlines") if isinstance(payload.get("raw_headlines"), list) else []
    lines.extend(f"  - {title}" for title in raw[:5])
    if not raw:
        lines.append("  none")
    lines.append("filtered_headlines:")
    filtered = payload.get("filtered_headlines") if isinstance(payload.get("filtered_headlines"), list) else []
    lines.extend(f"  - {title}" for title in filtered[:5])
    if not filtered:
        lines.append("  none")
    if payload.get("artifact_path"):
        lines.append(f"artifact={payload.get('artifact_path')}")
    return "\n".join(lines)


__all__ = ["format_news_diagnostic", "run_news_diagnostic"]
