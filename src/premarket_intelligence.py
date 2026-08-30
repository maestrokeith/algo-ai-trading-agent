"""Premarket intelligence scheduler hooks for the live loop."""

from __future__ import annotations

import json
import logging
import math
import re
import signal
import threading
import time as time_module
import traceback
from dataclasses import asdict, is_dataclass
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import requests

from src.brokers.alpaca_client import (
    fetch_alpaca_news_with_credentials,
    resolve_alpaca_credentials,
)
from src.news_catalyst import NewsCatalyst, _best_catalyst_for_symbol, score_article_text
from src.ai_news_ranking import (
    ai_news_ranking_enabled,
    ai_news_ranking_weight,
    finance_news_source_weight,
    is_software_package_spam,
    rank_news_item,
    score_adjustment,
)
from src.news_sentiment.newsapi_client import (
    NewsAPIRateLimitError,
    fetch_articles_query,
    fetch_top_headlines_query,
    newsapi_key_from_config,
    redact_newsapi_secret,
)
from src.social_sentiment import collect_social_sentiment

log = logging.getLogger(__name__)

PREMARKET_ENGINE_VERSION = 1
EASTERN_TZ = ZoneInfo("America/New_York")
PREMARKET_RECOVERY_END = time(hour=9, minute=25)

DEFAULT_PREMARKET_CONFIG: dict[str, Any] = {
    "enabled": False,
    "keep_alive_overnight": False,
    "allow_trading": False,
    "news_scan_time": "05:15",
    "collection_start_time": "05:15",
    "collection_end_time": "09:25",
    "refresh_interval_minutes": 12,
    "artifact_ttl_minutes": 390,
    "min_events_to_overwrite": 30,
    "min_rankings_to_overwrite": 10,
    "preserve_on_provider_rate_limit": True,
    "preserve_existing_if_richer": True,
    "job_timeout_seconds": 120,
    "finbert_timeout_seconds": 60,
    "news_timeout_seconds": 10,
    "alpaca_news_timeout_seconds": 10,
    "sec_timeout_seconds": 10,
    "sec_filings_enabled": True,
    "newsapi": {"enabled": False},
    "newsapi_enabled": False,
    "alpaca_news_enabled": True,
    "social": {
        "enabled": False,
        "reddit": {"enabled": True},
        "twitter": {"enabled": False},
        "subreddits": ["stocks", "wallstreetbets", "investing", "SecurityAnalysis"],
        "trusted_twitter_accounts": [],
    },
    "overnight_earnings_enabled": True,
    "rank_top_n": 10,
    "newsapi_symbol_batch_size": 2,
    "newsapi_max_query_length": 200,
    "overnight_earnings_batch_size": 3,
    "include_routine_sec_filings": False,
    "ignored_sec_forms": ["SD", "13F-HR", "DEF 14A", "PX14A6G"],
}

_ETF_SYMBOLS = frozenset(
    {
        "SPY", "QQQ", "IWM", "DIA", "SMH", "XLK", "XLF", "XLE", "XLV", "XLI",
        "XLY", "XLP", "XLU", "XLB", "ARKK", "FXI", "KWEB", "EWZ", "EEM",
    }
)

_DEFAULT_SYMBOL_NAMES: dict[str, str] = {
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "NVDA": "Nvidia",
    "GOOGL": "Google",
    "GOOG": "Google",
    "AMZN": "Amazon",
    "META": "Meta",
    "TSLA": "Tesla",
    "AMD": "Advanced Micro Devices AMD",
    "AVGO": "Broadcom",
    "MU": "Micron",
    "SMCI": "Super Micro",
    "ARM": "Arm Holdings",
    "MRVL": "Marvell",
    "ANET": "Arista",
    "PLTR": "Palantir",
    "CRM": "Salesforce",
    "ORCL": "Oracle",
    "NFLX": "Netflix",
    "COIN": "Coinbase",
    "TSM": "Taiwan Semiconductor",
    "INTC": "Intel",
    "QCOM": "Qualcomm",
    "UBER": "Uber",
    "SHOP": "Shopify",
    "SNOW": "Snowflake",
    "PANW": "Palo Alto",
    "CRWD": "CrowdStrike",
    "DELL": "Dell",
    "BABA": "Alibaba",
    "WMT": "Walmart",
    "JPM": "JPMorgan",
    "BAC": "Bank of America",
    "XOM": "Exxon",
    "LLY": "Eli Lilly",
}

_NEWS_THEME_KEYWORDS: tuple[str, ...] = (
    "earnings",
    "analyst upgrade",
    "analyst downgrade",
    "guidance",
    "buyback",
    "acquisition",
    "artificial intelligence",
    "semiconductor",
)

_NEWSAPI_QUERY_TIERS: tuple[tuple[int, str, str], ...] = (
    (1, "earnings", "earnings"),
    (2, "guidance", "guidance"),
    (3, "acquisition", "acquisition"),
)

_PREMARKET_RANK_SOURCES: frozenset[str] = frozenset(
    {"earnings", "guidance", "analyst", "acquisition", "sec_filing"}
)

_DEFAULT_IGNORED_SEC_FORMS: tuple[str, ...] = (
    "SD",
    "13F-HR",
    "13F",
    "DEF 14A",
    "PX14A6G",
    "DEFA14A",
)

RUNNING_STALE_SECONDS = 120.0
NEWSAPI_REQUEST_CAP_PER_RUN = 25
NEWSAPI_CACHE_TTL_SECONDS = 1800
NEWSAPI_DAILY_CALL_CAP_DEFAULT = 10
NEWSAPI_DAILY_CALL_CAP_MIN = 5
NEWSAPI_DAILY_CALL_CAP_MAX = 10
PREMARKET_EVENT_CACHE_TTL_SECONDS = 3600

SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_USER_AGENT = "AlgoSphere premarket/1.0 (contact: support@algosphere.local)"


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


@dataclass(frozen=True)
class PremarketConfig:
    enabled: bool
    keep_alive_overnight: bool
    allow_trading: bool
    news_scan_time: str
    missing: bool = False


@dataclass(frozen=True)
class PremarketJobResult:
    job: str
    due: bool
    ran: bool
    skipped_reason: str = ""
    reason: str = ""
    symbols: int = 0
    news: int = 0
    filings: int = 0
    ranked: int = 0
    error: str = ""


@dataclass(frozen=True)
class _PremarketJobStats:
    symbols: int = 0
    news: int = 0
    filings: int = 0
    ranked: int = 0


class PremarketJobTimeout(TimeoutError):
    """Raised when a premarket job exceeds its whole-job timeout."""


@dataclass(frozen=True)
class NewsapiQueryBatch:
    batch: int
    symbols: tuple[str, ...]
    query: str
    tier: int = 1
    rank_source: str = "earnings"
    kind: str = "tier"


@dataclass(frozen=True)
class SecFilingClassification:
    routine: bool
    rankable: bool
    score: float
    catalyst_type: str
    reason: str


@dataclass(frozen=True)
class NewsEvent:
    """Normalized headline or filing event from a premarket provider."""

    symbol: str
    headline: str
    source: str
    publisher: str = ""
    url: str = ""
    published_at: str = ""
    form: str = ""
    accession: str = ""
    cik: str = ""
    primary_doc: str = ""
    catalyst_type: str = ""
    rank_reason: str = ""
    rank_source: str = ""
    sec_routine: bool = False
    rankable: bool = True
    sentiment: float = 0.0
    score: float = 0.0
    gap_pct: float = 0.0
    volume_surge_pct: float = 0.0


@dataclass(frozen=True)
class PremarketRankEntry:
    symbol: str
    score: float
    catalyst_type: str
    source: str
    confidence: float
    reason: str
    form: str = ""
    filing_date: str = ""
    accession: str = ""
    url: str = ""
    news_quality: float | None = None
    catalyst_strength: float | None = None
    ai_confidence: float | None = None
    publisher: str = ""
    source_quality_weight: float | None = None


@dataclass
class ProviderExecResult:
    provider: str
    enabled: bool = True
    request_sent: bool = False
    duration_ms: float = 0.0
    requests_made: int = 0
    articles: int = 0
    filings: int = 0
    cik_mapped: int = 0
    cik_missing: int = 0
    http_status: int | None = None
    skip_reason: str = ""
    error: str = ""
    events: list[NewsEvent] = field(default_factory=list)
    raw_articles_before_filter: int = 0
    articles_after_filter: int = 0
    request_symbol_count: int = 0
    returned_symbol_count: int = 0
    sample_article_titles: list[str] = field(default_factory=list)
    rate_limit_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class _NewsapiBatchCacheEntry:
    fetched_at: datetime
    articles: list[dict[str, Any]]
    http_status: int | None
    skip_reason: str = ""
    error: str = ""


@dataclass
class _ProviderEventCacheEntry:
    fetched_at: datetime
    result: ProviderExecResult


@dataclass
class PremarketProviderResults:
    newsapi: ProviderExecResult
    alpaca: ProviderExecResult
    sec: ProviderExecResult
    benzinga: ProviderExecResult | None = None
    finnhub: ProviderExecResult | None = None
    marketaux: ProviderExecResult | None = None
    twitter: ProviderExecResult | None = None
    reddit: ProviderExecResult | None = None
    overnight_earnings: ProviderExecResult | None = None
    events: list[NewsEvent] = field(default_factory=list)
    catalysts: dict[str, NewsCatalyst] = field(default_factory=dict)
    rankings: list[PremarketRankEntry] = field(default_factory=list)
    candidate_symbols: list[str] = field(default_factory=list)

    @property
    def news_article_count(self) -> int:
        base = int(self.newsapi.articles) + int(self.alpaca.articles)
        if self.benzinga is not None:
            base += int(self.benzinga.articles)
        if self.finnhub is not None:
            base += int(self.finnhub.articles)
        if self.marketaux is not None:
            base += int(self.marketaux.articles)
        if self.overnight_earnings is not None:
            base += int(self.overnight_earnings.articles)
        return base

    @property
    def filings_count(self) -> int:
        return int(self.sec.filings)

    @property
    def provider_results(self) -> list[ProviderExecResult]:
        rows = [
            self.alpaca,
            self.sec,
            self.benzinga,
            self.finnhub,
            self.marketaux,
            self.twitter,
            self.reddit,
            self.newsapi,
            self.overnight_earnings,
        ]
        return [row for row in rows if row is not None]


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


def resolve_premarket_config(config: Mapping[str, Any] | None) -> PremarketConfig:
    root = config or {}
    raw = root.get("premarket_intelligence")
    missing = not isinstance(raw, Mapping)
    section = dict(DEFAULT_PREMARKET_CONFIG)
    if isinstance(raw, Mapping):
        section.update(dict(raw))
    return PremarketConfig(
        enabled=_as_bool(section.get("enabled"), False),
        keep_alive_overnight=_as_bool(section.get("keep_alive_overnight"), False),
        allow_trading=_as_bool(section.get("allow_trading"), False),
        news_scan_time=str(section.get("news_scan_time") or "05:15").strip() or "05:15",
        missing=missing,
    )


def log_premarket_startup_config(config: Mapping[str, Any] | None) -> PremarketConfig:
    pm = resolve_premarket_config(config)
    if pm.missing:
        msg = f"PREMARKET_CONFIG_MISSING using_defaults={DEFAULT_PREMARKET_CONFIG}"
        log.warning(msg)
        print(msg, flush=True)
    msg = (
        "PREMARKET_CONFIG enabled=%s keep_alive_overnight=%s allow_trading=%s news_scan_time=%s"
        % (
            _bool_text(pm.enabled),
            _bool_text(pm.keep_alive_overnight),
            _bool_text(pm.allow_trading),
            pm.news_scan_time,
        )
    )
    log.info(
        msg,
    )
    print(msg, flush=True)
    if pm.enabled:
        log.info("PREMARKET_INTELLIGENCE_ENABLED")
        print("PREMARKET_INTELLIGENCE_ENABLED", flush=True)
        version_msg = "PREMARKET_PIPELINE_VERSION widened_universe=true"
        log.info(version_msg)
        print(version_msg, flush=True)
        root = config or {}
        nfl = root.get("news_fast_lane")
        if isinstance(nfl, Mapping):
            fast_enabled = _as_bool(nfl.get("enabled"), False)
            try:
                fast_interval = int(float(nfl.get("scan_interval_seconds", 60) or 60))
            except (TypeError, ValueError):
                fast_interval = 60
            msg = f"NEWS_FAST_LANE enabled={_bool_text(fast_enabled)} scan_interval_seconds={fast_interval}"
            log.info(msg)
            print(msg, flush=True)
    return pm


def parse_news_scan_time(raw: str) -> time:
    try:
        hour_s, minute_s = str(raw).strip().split(":", 1)
        return time(hour=max(0, min(23, int(hour_s))), minute=max(0, min(59, int(minute_s))))
    except (TypeError, ValueError):
        return time(hour=5, minute=15)


def default_state_path(project_root: Path) -> Path:
    return project_root / "data" / "premarket_intelligence_state.json"


def default_premarket_rank_path(project_root: Path) -> Path:
    return project_root / "data" / "premarket_rank.json"


def default_premarket_artifacts_dir(project_root: Path) -> Path:
    return project_root / "data" / "premarket"


def default_premarket_event_feed_path(project_root: Path) -> Path:
    return default_premarket_artifacts_dir(project_root) / "latest_event_feed.json"


def default_premarket_rankings_path(project_root: Path) -> Path:
    return default_premarket_artifacts_dir(project_root) / "latest_rankings.json"


def default_premarket_catalysts_path(project_root: Path) -> Path:
    return default_premarket_artifacts_dir(project_root) / "latest_catalysts.json"


def default_premarket_provider_diagnostics_path(project_root: Path) -> Path:
    return default_premarket_artifacts_dir(project_root) / "provider_diagnostics_latest.json"


def _as_eastern(now: datetime) -> datetime:
    """Return a timezone-aware America/New_York datetime."""
    if now.tzinfo is None:
        return now.replace(tzinfo=EASTERN_TZ)
    return now.astimezone(EASTERN_TZ)


def _emit(msg: str, *, level: int = logging.INFO) -> None:
    log.log(level, msg)
    print(msg, flush=True)


def _step(job: str, step: str, **fields: Any) -> None:
    suffix = "".join(f" {key}={value}" for key, value in fields.items())
    _emit(f"PREMARKET_JOB_STEP job={job} step={step}{suffix}")


def _timeout_seconds(config: Mapping[str, Any] | None, key: str, default: float) -> float:
    root = config or {}
    pm = root.get("premarket_intelligence")
    raw = pm.get(key) if isinstance(pm, Mapping) else None
    try:
        return max(1.0, float(raw if raw is not None else default))
    except (TypeError, ValueError):
        return float(default)


def _load_state(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        log.warning("PREMARKET_STATE_READ_FAILED path=%s", path)
    return {}


def _save_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(state), indent=2, sort_keys=True) + "\n")


def _job_state_key(job: str, run_date: date) -> str:
    return f"{job}:{run_date.isoformat()}"


def _parse_state_datetime(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=EASTERN_TZ)
    return dt.astimezone(EASTERN_TZ)


def _job_state_is_complete(row: Mapping[str, Any]) -> bool:
    """True only when a prior run finished successfully."""
    status = str(row.get("status") or "").strip().lower()
    return status == "done" and bool(row.get("finished_at"))


def _is_legacy_incomplete_row(row: Mapping[str, Any]) -> bool:
    """
    Legacy rows wrote ``ran_at`` (and sometimes ``symbols``) before the job finished.

    Those must be rerun — they are not a successful completion marker.
    """
    status = str(row.get("status") or "").strip().lower()
    if status:
        return False
    return bool(row.get("ran_at")) and not row.get("finished_at")


def _mark_job_state(
    path: Path,
    *,
    job: str,
    run_date: date,
    status: str,
    now: datetime,
    reason: str,
    **fields: Any,
) -> None:
    now = _as_eastern(now)
    state = _load_state(path)
    key = _job_state_key(job, run_date)
    existing = state.get(key)
    row: dict[str, Any] = dict(existing) if isinstance(existing, Mapping) else {}
    status_raw = str(status).strip().lower()
    status_text = "failed" if status_raw == "error" else status_raw
    row["status"] = status_text
    row["reason"] = reason
    row["updated_at"] = now.isoformat()
    for legacy_key in ("ran_at", "error_at", "news", "filings"):
        row.pop(legacy_key, None)
    row.update(fields)
    if status_text == "running":
        row["started_at"] = now.isoformat()
        row.pop("finished_at", None)
        row.pop("error", None)
        row.pop("duration_sec", None)
        row.pop("news_count", None)
        row.pop("filings_count", None)
    elif status_text == "done":
        row.setdefault("started_at", now.isoformat())
        row["finished_at"] = now.isoformat()
        row.pop("error", None)
    elif status_text == "failed":
        row.setdefault("started_at", now.isoformat())
        row["finished_at"] = now.isoformat()
    state[key] = row
    _save_state(path, state)


def next_premarket_job(config: Mapping[str, Any] | None, now: datetime) -> str:
    now = _as_eastern(now)
    pm = resolve_premarket_config(config)
    scan_time = parse_news_scan_time(pm.news_scan_time)
    candidate = datetime.combine(now.date(), scan_time, tzinfo=EASTERN_TZ)
    if now >= candidate:
        candidate = datetime.combine(now.date() + timedelta(days=1), scan_time, tzinfo=EASTERN_TZ)
    return f"news_5am@{candidate.strftime('%Y-%m-%d %H:%M %Z').strip()}"


def _artifact_generated_today(path: Path, now: datetime) -> bool:
    try:
        payload = json.loads(path.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        return False
    generated_at = _parse_state_datetime(payload.get("generated_at"))
    if generated_at is None:
        return False
    return generated_at.astimezone(EASTERN_TZ).date() == now.date()


def _today_premarket_artifacts_present(project_root: Path, now: datetime) -> bool:
    now = _as_eastern(now)
    return all(
        _artifact_generated_today(path, now)
        for path in (
            default_premarket_event_feed_path(project_root),
            default_premarket_rankings_path(project_root),
            default_premarket_catalysts_path(project_root),
        )
    )


def _premarket_recovery_window(config: Mapping[str, Any] | None, now: datetime) -> bool:
    pm = resolve_premarket_config(config)
    scan_time = parse_news_scan_time(pm.news_scan_time)
    now_time = now.timetz().replace(tzinfo=None)
    return scan_time <= now_time < PREMARKET_RECOVERY_END


def due_premarket_jobs(
    config: Mapping[str, Any] | None,
    now: datetime,
    *,
    state_path: Path,
    project_root: Path | None = None,
) -> list[str]:
    now = _as_eastern(now)
    pm = resolve_premarket_config(config)
    if not pm.enabled:
        return []
    scan_time = parse_news_scan_time(pm.news_scan_time)
    if now.timetz().replace(tzinfo=None) < scan_time:
        return []
    state = _load_state(state_path)
    key = _job_state_key("news_5am", now.date())
    row = state.get(key)
    if not isinstance(row, Mapping):
        if row:
            return []
        if (
            project_root is not None
            and _premarket_recovery_window(config, now)
            and not _today_premarket_artifacts_present(project_root, now)
        ):
            _emit("PREMARKET_JOB_DUE job=news_5am now=%s" % now.isoformat())
            return ["news_5am"]
        _emit("PREMARKET_JOB_DUE job=news_5am now=%s" % now.isoformat())
        return ["news_5am"]
    if _job_state_is_complete(row):
        return []
    if (
        project_root is not None
        and _premarket_recovery_window(config, now)
        and not _today_premarket_artifacts_present(project_root, now)
    ):
        _emit(
            "PREMARKET_JOB_DUE job=news_5am now=%s reason=missing_artifacts"
            % now.isoformat()
        )
        return ["news_5am"]
    if _is_legacy_incomplete_row(row):
        _emit(
            "PREMARKET_JOB_STALE_RERUN job=news_5am reason=legacy_ran_at_without_done",
            level=logging.WARNING,
        )
        return ["news_5am"]
    status = str(row.get("status") or "").strip().lower()
    if status in {"failed", "error", "stale"}:
        return ["news_5am"]
    if status == "running":
        started_at = _parse_state_datetime(row.get("started_at") or row.get("updated_at"))
        stale = started_at is None or abs((now - started_at).total_seconds()) > RUNNING_STALE_SECONDS
        if not stale:
            return []
        state[key] = {
            **dict(row),
            "status": "stale",
            "stale_at": now.isoformat(),
            "stale_reason": "running_older_than_120s",
        }
        _save_state(state_path, state)
        _emit(
            "PREMARKET_JOB_INTERRUPTED_OR_STALE job=news_5am status=running age_sec=%.1f action=rerun"
            % (abs((now - started_at).total_seconds()) if started_at is not None else -1.0),
            level=logging.WARNING,
        )
    return ["news_5am"]


def _news_symbols(config: Mapping[str, Any] | None) -> list[str]:
    root = config or {}
    universe = root.get("universe")
    raw = universe.get("symbols") if isinstance(universe, Mapping) else None
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        sym = str(item or "").strip().upper()
        if sym and sym not in out:
            out.append(sym)
    return out


def _premarket_candidate_universe(
    config: Mapping[str, Any] | None,
    *,
    market_client: Any = None,
    base_symbols: Sequence[str] | None = None,
) -> list[str]:
    core = _news_symbols(config)
    seed: list[str] = []
    for sym in list(base_symbols or []) + core:
        su = str(sym or "").strip().upper()
        if su and su not in seed:
            seed.append(su)

    mover_symbols: list[str] = []
    try:
        if market_client is not None and callable(getattr(market_client, "get_top_movers", None)):
            for row in market_client.get_top_movers() or []:
                sym = str((row or {}).get("symbol") or "").strip().upper()
                if sym and sym not in mover_symbols:
                    mover_symbols.append(sym)
    except Exception as exc:
        _emit("PREMARKET_CANDIDATE_UNIVERSE source=market_client error=%s" % str(exc)[:200])

    combined: list[str] = []
    for sym in seed + mover_symbols:
        if sym and sym not in combined:
            combined.append(sym)

    sample = ",".join(combined[:10])
    _emit("PREMARKET_CANDIDATE_UNIVERSE count=%d sample=[%s]" % (len(combined), sample))
    return combined


def _pm_section(config: Mapping[str, Any] | None) -> dict[str, Any]:
    root = config or {}
    raw = root.get("premarket_intelligence")
    section = dict(DEFAULT_PREMARKET_CONFIG)
    if isinstance(raw, Mapping):
        section.update(dict(raw))
    return section


def _news_sentiment_section(config: Mapping[str, Any] | None) -> dict[str, Any]:
    root = config or {}
    raw = root.get("news_sentiment")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _news_provider_line(provider: str, **fields: Any) -> None:
    parts = [f"PREMARKET_NEWS_PROVIDER provider={provider}"]
    for key, value in fields.items():
        parts.append(f"{key}={value}")
    _emit(" ".join(parts))


def _sample_article_titles(articles: Sequence[Any], *, limit: int = 3) -> list[str]:
    titles: list[str] = []
    for item in articles:
        data = _article_like_mapping(item)
        title = str(
            data.get("title")
            or data.get("headline")
            or getattr(item, "title", "")
            or getattr(item, "headline", "")
            or ""
        ).strip()
        if not title:
            continue
        title = title.replace("\n", " ")[:160]
        if title not in titles:
            titles.append(title)
        if len(titles) >= max(1, int(limit)):
            break
    return titles


def _article_like_mapping(item: Any) -> dict[str, Any]:
    if isinstance(item, Mapping):
        return dict(item)
    if hasattr(item, "model_dump") and callable(getattr(item, "model_dump")):
        try:
            dumped = item.model_dump()
            if isinstance(dumped, Mapping):
                return dict(dumped)
        except Exception:
            pass
    if hasattr(item, "dict") and callable(getattr(item, "dict")):
        try:
            dumped = item.dict()
            if isinstance(dumped, Mapping):
                return dict(dumped)
        except Exception:
            pass
    if is_dataclass(item):
        try:
            dumped = asdict(item)
            if isinstance(dumped, Mapping):
                return dict(dumped)
        except Exception:
            pass
    if hasattr(item, "__dict__"):
        try:
            return dict(vars(item))
        except Exception:
            pass
    return {}


def _format_sample_titles(titles: Sequence[str]) -> str:
    clean = [str(title or "").replace("|", "/").replace("\n", " ")[:160] for title in titles if str(title or "").strip()]
    return " | ".join(clean) if clean else "none"


def _format_rate_limit_headers(headers: Mapping[str, Any] | None) -> str:
    if not headers:
        return "none"
    parts: list[str] = []
    for key in sorted(headers):
        value = str(headers.get(key) or "").strip()
        if value:
            parts.append(f"{key}:{value}")
    return ",".join(parts) if parts else "none"


def _symbols_from_newsapi_articles(articles: Sequence[Mapping[str, Any]]) -> list[str]:
    out: list[str] = []
    for art in articles:
        if not isinstance(art, Mapping):
            continue
        raw = art.get("symbols")
        if isinstance(raw, (list, tuple, set)):
            for item in raw:
                su = str(item or "").strip().upper()
                if su and su not in out:
                    out.append(su)
    return out


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


def _premarket_newsapi_enabled(config: Mapping[str, Any] | None) -> bool:
    root = config or {}
    raw_pm = root.get("premarket_intelligence")
    if isinstance(raw_pm, Mapping):
        raw_nested = raw_pm.get("newsapi")
        if isinstance(raw_nested, Mapping) and "enabled" in raw_nested:
            return _as_bool(raw_nested.get("enabled"), False)
        if "newsapi_enabled" in raw_pm:
            return _as_bool(raw_pm.get("newsapi_enabled"), False)
    ns = _news_sentiment_section(config)
    return _as_bool(ns.get("enabled"), False)


def _symbol_names(cfg: Mapping[str, Any] | None) -> dict[str, str]:
    names = dict(_DEFAULT_SYMBOL_NAMES)
    pm = _pm_section(cfg)
    raw = pm.get("symbol_names")
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            sym = str(key or "").strip().upper()
            text = str(value or "").strip()
            if sym and text:
                names[sym] = text
    return names


def _operating_company_symbols(symbols: Sequence[str]) -> list[str]:
    """Return universe symbols excluding ETFs and index products."""
    uniq = [str(s).strip().upper() for s in symbols if str(s).strip()]
    uniq = list(dict.fromkeys(uniq))
    return [s for s in uniq if s not in _ETF_SYMBOLS]


def _ordered_equity_symbols(symbols: Sequence[str]) -> list[str]:
    """Backward-compatible alias — operating companies only (no ETFs)."""
    return _operating_company_symbols(symbols)


def _chunk_list(items: Sequence[str], size: int) -> list[list[str]]:
    n = max(1, int(size))
    seq = list(items)
    return [seq[i : i + n] for i in range(0, len(seq), n)]


def _newsapi_batch_size(cfg: Mapping[str, Any] | None) -> int:
    pm = _pm_section(cfg)
    try:
        return max(1, min(10, int(pm.get("newsapi_symbol_batch_size", 2) or 2)))
    except (TypeError, ValueError):
        return 2


def _newsapi_max_query_length(cfg: Mapping[str, Any] | None) -> int:
    pm = _pm_section(cfg)
    try:
        return max(20, min(200, int(pm.get("newsapi_max_query_length", 200) or 200)))
    except (TypeError, ValueError):
        return 200


def _newsapi_request_cap(cfg: Mapping[str, Any] | None) -> int:
    pm = _pm_section(cfg)
    try:
        return max(1, min(NEWSAPI_REQUEST_CAP_PER_RUN, int(pm.get("newsapi_request_cap_per_run", NEWSAPI_REQUEST_CAP_PER_RUN) or NEWSAPI_REQUEST_CAP_PER_RUN)))
    except (TypeError, ValueError):
        return NEWSAPI_REQUEST_CAP_PER_RUN


def _newsapi_fallback_top_n(cfg: Mapping[str, Any] | None) -> int:
    pm = _pm_section(cfg)
    try:
        raw = int(pm.get("newsapi_fallback_top_n", 5) or 5)
    except (TypeError, ValueError):
        raw = 5
    return max(1, min(5, raw))


def _newsapi_cache_ttl_seconds(cfg: Mapping[str, Any] | None) -> float:
    pm = _pm_section(cfg)
    try:
        raw = float(pm.get("newsapi_cache_ttl_seconds", NEWSAPI_CACHE_TTL_SECONDS) or NEWSAPI_CACHE_TTL_SECONDS)
    except (TypeError, ValueError):
        raw = float(NEWSAPI_CACHE_TTL_SECONDS)
    return max(1800.0, min(3600.0, raw))


def _newsapi_daily_call_cap(cfg: Mapping[str, Any] | None) -> int:
    pm = _pm_section(cfg)
    try:
        raw = int(pm.get("newsapi_daily_call_cap", NEWSAPI_DAILY_CALL_CAP_DEFAULT) or NEWSAPI_DAILY_CALL_CAP_DEFAULT)
    except (TypeError, ValueError):
        raw = NEWSAPI_DAILY_CALL_CAP_DEFAULT
    return max(NEWSAPI_DAILY_CALL_CAP_MIN, min(NEWSAPI_DAILY_CALL_CAP_MAX, raw))


def _newsapi_daily_budget_key(now: datetime | None = None) -> str:
    when = now or datetime.now(timezone.utc)
    return when.date().isoformat()


def _newsapi_daily_calls_used(now: datetime | None = None) -> int:
    return int(_NEWSAPI_DAILY_CALLS.get(_newsapi_daily_budget_key(now), 0))


def _newsapi_remaining_daily_budget(cfg: Mapping[str, Any] | None, now: datetime | None = None) -> int:
    return max(0, _newsapi_daily_call_cap(cfg) - _newsapi_daily_calls_used(now))


def _newsapi_consume_daily_budget(calls: int = 1, now: datetime | None = None) -> int:
    key = _newsapi_daily_budget_key(now)
    used = _NEWSAPI_DAILY_CALLS.get(key, 0) + max(0, int(calls))
    _NEWSAPI_DAILY_CALLS[key] = used
    return used


def _event_feed_cache_key(provider: str, symbols: Sequence[str], cfg: Mapping[str, Any] | None) -> str:
    pm = _pm_section(cfg)
    relevant = {
        "provider": provider,
        "symbols": [str(sym or "").strip().upper() for sym in symbols if str(sym or "").strip()],
        "benzinga_enabled": _as_bool(pm.get("benzinga_enabled"), False),
        "finnhub_enabled": _as_bool(pm.get("finnhub_enabled"), False),
        "marketaux_enabled": _as_bool(pm.get("marketaux_enabled"), False),
        "fmp_enabled": _as_bool(pm.get("fmp_enabled"), False),
        "twitter_trusted_enabled": _as_bool(pm.get("twitter_trusted_enabled"), False),
        "twitter_trusted_accounts": list(pm.get("twitter_trusted_accounts") or []) if isinstance(pm.get("twitter_trusted_accounts"), (list, tuple)) else [],
        "benzinga_feed_url": str(pm.get("benzinga_feed_url") or ""),
        "finnhub_feed_url": str(pm.get("finnhub_feed_url") or ""),
        "marketaux_feed_url": str(pm.get("marketaux_feed_url") or ""),
        "fmp_feed_url": str(pm.get("fmp_feed_url") or ""),
        "twitter_trusted_feed_url": str(pm.get("twitter_trusted_feed_url") or ""),
        "newsapi_fallback_top_n": int(pm.get("newsapi_fallback_top_n") or 5) if str(pm.get("newsapi_fallback_top_n") or "").strip() else 5,
    }
    return json.dumps(relevant, sort_keys=True, default=str)


def _event_feed_line(ev: NewsEvent) -> None:
    _emit(
        "EVENT_FEED symbol=%s source=%s title=%s url=%s published_at=%s sentiment=%.2f score=%.2f"
        % (
            ev.symbol or "",
            ev.source or "",
            str(ev.headline or "").replace("\n", " ")[:180],
            ev.url or "",
            ev.published_at or "",
            float(ev.sentiment or 0.0),
            float(ev.score or 0.0),
        )
    )


def _newsapi_symbol_clause(sym: str, cfg: Mapping[str, Any] | None) -> str:
    ticker = str(sym or "").strip().upper()
    if not ticker:
        return ""
    label, used_name = _resolve_search_label(ticker, cfg)
    if not label:
        return ticker
    company = f'"{label}"' if " " in label else label
    if used_name:
        return f"({company} OR {ticker})"
    return f"({ticker} OR {company})"


def _newsapi_query_for_symbols(symbols: Sequence[str], cfg: Mapping[str, Any] | None) -> str:
    clauses = [_newsapi_symbol_clause(sym, cfg) for sym in symbols if str(sym or "").strip()]
    clauses = [clause for clause in clauses if clause]
    return " OR ".join(clauses) if clauses else ""


NEWSAPI_CATALYST_FALLBACK_QUERY = "AI OR earnings OR guidance OR acquisition OR partnership OR FDA OR contract"
PREMARKET_NEWSAPI_MIN_LOOKBACK_HOURS = 48


_NEWSAPI_QUERY_CACHE: dict[str, _NewsapiBatchCacheEntry] = {}
_NEWSAPI_DAILY_CALLS: dict[str, int] = {}
_PREMARKET_PROVIDER_CACHE: dict[str, _ProviderEventCacheEntry] = {}


def _emit_newsapi_rate_limited_once(
    state: dict[str, bool] | None = None,
    *,
    provider: str = "newsapi",
) -> None:
    if state is not None:
        if state.get("newsapi_rate_limited"):
            return
        state["newsapi_rate_limited"] = True
    _emit(
        "NEWSAPI_RATE_LIMITED_ONCE provider=%s action=skip_remaining_newsapi_calls"
        % provider,
        level=logging.WARNING,
    )


def _newsapi_rate_limited_for_job(state: dict[str, bool] | None) -> bool:
    return bool(state is not None and state.get("newsapi_rate_limited"))


def _premarket_newsapi_lookback_hours(cfg: Mapping[str, Any] | None) -> int:
    ns = _news_sentiment_section(cfg)
    try:
        configured = int(ns.get("headline_lookback_hours", 24) or 24)
    except (TypeError, ValueError):
        configured = 24
    return max(PREMARKET_NEWSAPI_MIN_LOOKBACK_HOURS, configured)


def _parse_newsapi_published_at(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _log_newsapi_window_diagnostics(
    *,
    route: str,
    articles: Sequence[Mapping[str, Any]],
    now: datetime,
    lookback_hours: int,
) -> None:
    effective_cutoff = now.astimezone(timezone.utc) - timedelta(hours=max(1, lookback_hours))
    old_cutoff = now.astimezone(timezone.utc) - timedelta(hours=24)
    older_than_effective = 0
    older_than_24h_included = 0
    oldest: datetime | None = None
    for art in articles:
        if not isinstance(art, Mapping):
            continue
        published = _parse_newsapi_published_at(art.get("publishedAt") or art.get("published_at"))
        if published is None:
            continue
        if oldest is None or published < oldest:
            oldest = published
        if published < effective_cutoff:
            older_than_effective += 1
        elif published < old_cutoff:
            older_than_24h_included += 1
    _emit(
        "NEWSAPI_WINDOW_DIAGNOSTIC route=%s lookback_hours=%d old_lookback_hours=24 older_than_effective_window=%d older_than_24h_included=%d oldest_published_at=%s"
        % (
            route,
            int(lookback_hours),
            older_than_effective,
            older_than_24h_included,
            oldest.isoformat() if oldest is not None else "none",
        )
    )


def _resolve_search_label(sym: str, cfg: Mapping[str, Any] | None) -> tuple[str, bool]:
    su = str(sym or "").strip().upper()
    if not su:
        return "", False
    names = _symbol_names(cfg)
    company = str(names.get(su) or "").strip()
    if company:
        return company, True
    return su, False


def _build_symbol_search_query(
    sym: str,
    theme: str,
    cfg: Mapping[str, Any] | None,
) -> tuple[str, str, bool]:
    """
    Build a per-symbol NewsAPI query.

    API query uses quoted company names: ``"Apple" earnings``.
    Display query for logs: ``Apple earnings``.
    """
    label, used_name = _resolve_search_label(sym, cfg)
    if not label:
        return theme, theme, False
    display = f"{label} {theme}"
    if used_name:
        return f'"{label}" {theme}', display, True
    return display, display, False


def _log_news_query(symbol: str, query: str) -> None:
    _emit('PREMARKET_NEWS_QUERY symbol=%s query="%s"' % (symbol, query.replace('"', "")))


def build_newsapi_query_batches(
    symbols: Sequence[str],
    cfg: Mapping[str, Any] | None,
) -> list[NewsapiQueryBatch]:
    """Build batched broad NewsAPI queries for operating companies."""
    ordered = _operating_company_symbols(symbols)
    if not ordered:
        return []

    min_batch_size = _newsapi_batch_size(cfg)
    request_cap = _newsapi_request_cap(cfg)
    target_batch_size = max(min_batch_size, int(math.ceil(len(ordered) / float(request_cap))))
    max_query_length = _newsapi_max_query_length(cfg)

    batches: list[NewsapiQueryBatch] = []
    current: list[str] = []
    batch_num = 0

    def _flush() -> None:
        nonlocal batch_num, current
        if not current:
            return
        query = _newsapi_query_for_symbols(current, cfg)
        for sym in current:
            _log_news_query(sym, query)
        batch_num += 1
        batches.append(
            NewsapiQueryBatch(
                batch=batch_num,
                symbols=tuple(current),
                query=query,
                tier=1,
                rank_source="news",
                kind="batch",
            )
        )
        current = []

    for sym in ordered:
        candidate = current + [sym]
        candidate_query = _newsapi_query_for_symbols(candidate, cfg)
        if current and (len(candidate) > target_batch_size or len(candidate_query) > max_query_length):
            _flush()
        current.append(sym)
    _flush()
    return batches


def build_newsapi_queries(
    symbols: Sequence[str],
    cfg: Mapping[str, Any] | None,
    *,
    max_symbol_clauses: int = 10,
) -> list[str]:
    """Backward-compatible wrapper returning query strings from batched builder."""
    _ = max_symbol_clauses
    return [batch.query for batch in build_newsapi_query_batches(symbols, cfg)]


def _article_cache_key(art: Mapping[str, Any]) -> str:
    url = str(art.get("url") or "").strip()
    if url:
        return f"url:{url}"
    title = str(art.get("title") or "").strip().casefold()
    if title:
        return f"title:{title}"
    return ""


def _filter_new_articles(
    articles: Sequence[Mapping[str, Any]],
    seen: set[str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for art in articles:
        if not isinstance(art, Mapping):
            continue
        key = _article_cache_key(art)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(dict(art))
    return out


def _article_publisher(raw: Mapping[str, Any]) -> str:
    source = raw.get("source")
    if isinstance(source, Mapping):
        for key in ("name", "id"):
            text = str(source.get(key) or "").strip()
            if text:
                return text
    for key in ("publisher", "provider", "source_name", "source"):
        text = str(raw.get(key) or "").strip()
        if text and not text.startswith("{"):
            return text
    return ""


def _finance_source_score(ev: NewsEvent) -> float:
    return finance_news_source_weight(ev.source, ev.publisher, ev.url)


def _software_package_spam_event(ev: NewsEvent) -> bool:
    return is_software_package_spam(
        ev.headline,
        source=ev.source,
        publisher=ev.publisher,
        url=ev.url,
    )


def _classify_catalyst_headline(*, headline: str, catalyst_type: str = "") -> tuple[str, float]:
    hl = str(headline or "").casefold()
    ctype = str(catalyst_type or "").casefold()

    def _matches_any(patterns: Sequence[str]) -> bool:
        return any(re.search(pattern, hl) for pattern in patterns)

    earnings_patterns = (
        r"\bearnings\b",
        r"\beps\b",
        r"\brevenue\b",
        r"\bquarterly results?\b",
        r"\bq[1-4]\b",
        r"\breports results?\b",
        r"\bresults?\b",
    )
    guidance_patterns = (
        r"\bguidance\b",
        r"\braises? (?:its )?(?:full[- ]year )?(?:outlook|forecast|guidance)\b",
        r"\blifts? (?:its )?(?:full[- ]year )?(?:outlook|forecast|guidance)\b",
        r"\bboosts? (?:its )?(?:full[- ]year )?(?:outlook|forecast|guidance)\b",
        r"\bupbeat (?:outlook|forecast|guidance)\b",
    )
    analyst_patterns = (
        r"\bupgrade(?:d|s)?\b",
        r"\bdowngrade(?:d|s)?\b",
        r"\banalyst\b",
        r"\bprice target\b",
        r"\binitiat(?:e|es|ed|ing)\b",
        r"\boverweight\b",
        r"\bunderweight\b",
        r"\boutperform\b",
        r"\bneutral\b",
    )
    deal_patterns = (
        r"\bpartnership\b",
        r"\bpartner(?:s|ship|ing)?\b",
        r"\bacquisition\b",
        r"\bacquires?\b",
        r"\bmerger\b",
        r"\bm&a\b",
        r"\bdeal\b",
        r"\bcontract win\b",
        r"\bwins? contract\b",
        r"\bcollaboration\b",
    )
    product_patterns = (
        r"\blaunch(?:es|ed|ing)?\b",
        r"\bunveils?\b",
        r"\brelease(?:s|d|ing)?\b",
        r"\brollout\b",
        r"\bproduct\b",
        r"\bshipping\b",
    )
    semiconductor_product_patterns = (
        r"\bvera\b",
        r"\brubin\b",
        r"\bcpu\b",
        r"\bgpu\b",
        r"\bprocessor\b",
        r"\bchip\b",
        r"\bsemiconductor\b",
    )
    ai_patterns = (
        r"\bartificial intelligence\b",
        r"\bai\b",
        r"\bgenerative ai\b",
        r"\bgenai\b",
        r"\bmachine learning\b",
        r"\bopenai\b",
    )
    ai_partnership_patterns = (
        r"\bai\b.*\b(partnership|partner|deal|collaboration)\b",
        r"\b(partnership|partner|deal|collaboration)\b.*\bai\b",
        r"\bopenai\b.*\b(partnership|partner|deal|collaboration)\b",
        r"\b(partnership|partner|deal|collaboration)\b.*\bopenai\b",
        r"\bartificial intelligence\b.*\b(partnership|partner|deal|collaboration)\b",
        r"\b(partnership|partner|deal|collaboration)\b.*\bartificial intelligence\b",
    )

    if _matches_any(guidance_patterns) or ctype in {"guidance", "guidance_raise"}:
        return "guidance", 0.90
    if _matches_any(analyst_patterns) or ctype in {"upgrade", "downgrade", "analyst"}:
        return "analyst", 0.86
    if _matches_any(ai_partnership_patterns) or ctype in {"ai_partnership"}:
        return "ai", 0.87
    if _matches_any(deal_patterns) or ctype in {"deal", "acquisition"}:
        return "deal", 0.84
    if _matches_any(semiconductor_product_patterns) or ctype == "product":
        return "product", 0.82
    if _matches_any(ai_patterns) or ctype == "ai":
        return "ai", 0.83
    if _matches_any(product_patterns):
        return "product", 0.82
    if _matches_any(earnings_patterns) or ctype == "earnings":
        return "earnings", 0.88
    return "unknown", 0.35


def _log_catalyst_classified(symbol: str, headline: str, catalyst_type: str, confidence: float) -> None:
    _emit(
        "CATALYST_CLASSIFIED symbol=%s headline=%s type=%s confidence=%.2f"
        % (
            str(symbol or "").strip().upper() or "unknown",
            str(headline or "").replace("\n", " ")[:240],
            str(catalyst_type or "unknown"),
            float(confidence),
        )
    )


def _overnight_earnings_batch_size(cfg: Mapping[str, Any] | None) -> int:
    pm = _pm_section(cfg)
    try:
        return max(1, min(3, int(pm.get("overnight_earnings_batch_size", 3) or 3)))
    except (TypeError, ValueError):
        return 3


def build_overnight_earnings_query_batches(
    symbols: Sequence[str],
    cfg: Mapping[str, Any] | None,
) -> list[NewsapiQueryBatch]:
    """Build per-symbol overnight earnings queries for operating companies."""
    ordered = _operating_company_symbols(symbols)
    batches: list[NewsapiQueryBatch] = []
    for batch_num, sym in enumerate(ordered, start=1):
        api_query, _display, _used = _build_symbol_search_query(sym, "earnings", cfg)
        batches.append(
            NewsapiQueryBatch(
                batch=batch_num,
                symbols=(sym,),
                query=api_query,
                tier=1,
                rank_source="earnings",
                kind="earnings_overnight",
            )
        )
    return batches


def build_overnight_earnings_query(symbols: Sequence[str], cfg: Mapping[str, Any] | None) -> str:
    """NewsAPI query for the first operating-company overnight earnings lookup."""
    batches = build_overnight_earnings_query_batches(symbols, cfg)
    if not batches:
        return "earnings"
    return batches[0].query


def _dedupe_raw_articles(articles: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    return _filter_new_articles(articles, seen)


def _confidence_for_rank(score: float, reason: str, catalyst_type: str) -> float:
    if reason == "sec_filing":
        ctype = str(catalyst_type).upper()
        if ctype.startswith("8-K"):
            return 0.25
        if ctype.startswith("144") or ctype.startswith("424B5") or ctype.startswith("FWP"):
            return -0.10
        return 0.10
    if reason == "earnings_overnight":
        return min(0.92, 0.62 + score * 0.08)
    return min(0.95, 0.45 + score * 0.12)


def _headline_rank_score(rank_source: str) -> float:
    """Score headline catalysts by premarket trading priority."""
    return {
        "earnings": 10.0,
        "guidance": 9.0,
        "analyst": 7.5,
        "deal": 7.0,
        "ai": 6.5,
        "product": 5.8,
        "unknown": 1.0,
    }.get(str(rank_source or "").strip().lower(), 1.5)


def _float_from_any(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def _first_float(raw: Mapping[str, Any], keys: Sequence[str]) -> float:
    for key in keys:
        if key in raw:
            out = _float_from_any(raw.get(key))
            if out != 0.0:
                return out
    return 0.0


def _event_recency_score(ev: NewsEvent, now: datetime | None) -> float:
    if not ev.published_at:
        return 0.0
    ref = _as_eastern(now or datetime.now(EASTERN_TZ))
    event_dt = _parse_state_datetime(ev.published_at)
    if event_dt is None:
        return 0.0
    age_minutes = max(0.0, (ref - event_dt).total_seconds() / 60.0)
    if age_minutes <= 60:
        return 1.0
    if age_minutes <= 180:
        return 0.6
    if age_minutes <= 720:
        return 0.25
    return -0.5


def _tradability_score(base_score: float, ev: NewsEvent, now: datetime | None) -> float:
    gap = max(0.0, _float_from_any(ev.gap_pct))
    volume_surge = max(0.0, _float_from_any(ev.volume_surge_pct))
    gap_bonus = min(gap, 20.0) * 0.08
    volume_bonus = min(volume_surge, 500.0) / 500.0 * 1.4
    recency_bonus = _event_recency_score(ev, now)
    return round(float(base_score) + gap_bonus + volume_bonus + recency_bonus, 4)


def _rank_score_for_symbol(symbol: str, score: float) -> float:
    su = str(symbol or "").strip().upper()
    base_score = float(score)
    multiplier = 0.5 if su in _ETF_SYMBOLS else 1.0
    final_score = round(base_score * multiplier, 4)
    if multiplier != 1.0:
        _emit(
            "PREMARKET_RANK_ADJUSTMENT symbol=%s base_score=%.4f etf_multiplier=%.1f final_score=%.4f"
            % (su, base_score, multiplier, final_score)
        )
    return final_score


def _sec_filing_url(cik: str, accession: str, primary_doc: str) -> str:
    """Build an EDGAR archive URL for a filing primary document."""
    cik_text = str(cik or "").strip()
    accession_text = str(accession or "").strip()
    primary = str(primary_doc or "").strip()
    if not cik_text or not accession_text:
        return ""
    try:
        cik_int = str(int(cik_text))
    except ValueError:
        cik_int = cik_text.lstrip("0") or cik_text
    acc_nodash = accession_text.replace("-", "")
    if primary:
        return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{primary}"
    return (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        f"&CIK={cik_int}&type=&dateb=&owner=include&count=40"
    )


def _ignored_sec_forms(cfg: Mapping[str, Any] | None) -> frozenset[str]:
    pm = _pm_section(cfg)
    raw = pm.get("ignored_sec_forms")
    if isinstance(raw, (list, tuple)):
        values = [str(item or "").strip().upper() for item in raw if str(item or "").strip()]
        if values:
            return frozenset(values)
    return frozenset(_DEFAULT_IGNORED_SEC_FORMS)


def _sec_form_matches(form: str, pattern: str) -> bool:
    form_u = str(form or "").strip().upper()
    pattern_u = str(pattern or "").strip().upper()
    if not form_u or not pattern_u:
        return False
    return (
        form_u == pattern_u
        or form_u.startswith(f"{pattern_u} ")
        or form_u.startswith(f"{pattern_u}/")
        or form_u.startswith(pattern_u)
    )


def classify_sec_filing(form: str, cfg: Mapping[str, Any] | None) -> SecFilingClassification:
    """Classify SEC forms for routine vs rankable premarket catalyst scoring."""
    form_u = str(form or "").strip().upper()
    pm = _pm_section(cfg)
    include_routine = _as_bool(pm.get("include_routine_sec_filings"), False)

    for ignored in _ignored_sec_forms(cfg):
        if _sec_form_matches(form_u, ignored):
            return SecFilingClassification(
                routine=True,
                rankable=include_routine,
                score=1.0 if include_routine else 0.0,
                catalyst_type="sec_filing",
                reason="SEC routine filing",
            )

    if form_u.startswith("13F"):
        return SecFilingClassification(
            routine=True,
            rankable=include_routine,
            score=1.0 if include_routine else 0.0,
            catalyst_type="sec_filing",
            reason="SEC routine filing",
        )

    if "DEF 14A" in form_u or form_u.startswith("DEFA14A"):
        return SecFilingClassification(
            routine=True,
            rankable=include_routine,
            score=1.0 if include_routine else 0.0,
            catalyst_type="sec_filing",
            reason="SEC proxy filing",
        )

    if form_u.startswith("PX14A"):
        return SecFilingClassification(
            routine=True,
            rankable=include_routine,
            score=1.0 if include_routine else 0.0,
            catalyst_type="sec_filing",
            reason="SEC routine filing",
        )

    if form_u in {"3", "4", "5"} or form_u.startswith("4/") or form_u.startswith("3/") or form_u.startswith("5/"):
        return SecFilingClassification(
            routine=True,
            rankable=False,
            score=0.0,
            catalyst_type="insider_ownership",
            reason="SEC ownership filing",
        )

    if form_u.startswith("144"):
        return SecFilingClassification(
            routine=True,
            rankable=True,
            score=1.0,
            catalyst_type="form_144",
            reason="SEC Form 144 proposed sale filing",
        )

    if form_u.startswith("424B5"):
        return SecFilingClassification(
            routine=False,
            rankable=True,
            score=1.5,
            catalyst_type="dilution_risk",
            reason="SEC 424B5 dilution risk filing",
        )

    if form_u.startswith("FWP"):
        return SecFilingClassification(
            routine=True,
            rankable=True,
            score=1.0,
            catalyst_type="informational",
            reason="SEC free writing prospectus informational filing",
        )

    if form_u.startswith("S-1") or form_u.startswith("424B"):
        return SecFilingClassification(
            routine=False,
            rankable=True,
            score=2.0,
            catalyst_type="dilution_risk",
            reason="SEC offering/dilution filing",
        )

    if form_u.startswith("8-K"):
        return SecFilingClassification(
            routine=False,
            rankable=True,
            score=7.2,
            catalyst_type="sec_filing",
            reason="SEC material event filing",
        )

    if form_u.startswith("10-Q") or form_u.startswith("10-K"):
        return SecFilingClassification(
            routine=True,
            rankable=include_routine,
            score=3.0 if include_routine else 0.0,
            catalyst_type="sec_filing",
            reason="SEC periodic report",
        )

    return SecFilingClassification(
        routine=False,
        rankable=True,
        score=4.0,
        catalyst_type="sec_filing",
        reason="SEC filing detected",
    )


def build_premarket_rankings(
    symbols: Sequence[str],
    *,
    catalysts: Mapping[str, NewsCatalyst],
    events: Sequence[NewsEvent],
    cfg: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> list[PremarketRankEntry]:
    """Merge news catalysts and SEC/earnings events into ranked premarket entries."""
    by_sym: dict[str, PremarketRankEntry] = {}
    ai_rank_enabled = ai_news_ranking_enabled(cfg)
    ai_rank_weight = ai_news_ranking_weight(cfg)

    def _upsert(entry: PremarketRankEntry) -> None:
        su = entry.symbol.strip().upper()
        if not su:
            return
        prev = by_sym.get(su)
        if prev is None or entry.score > prev.score:
            by_sym[su] = entry

    for ev in events:
        su = str(ev.symbol or "").strip().upper()
        if not su:
            continue
        if ev.source == "sec":
            if not ev.rankable:
                continue
            form = str(ev.form or "").strip()
            classification = classify_sec_filing(form, cfg)
            score = min(classification.score, 2.5)
            if score <= 0:
                continue
            _upsert(
                PremarketRankEntry(
                    symbol=su,
                    score=_rank_score_for_symbol(su, score),
                    catalyst_type=classification.catalyst_type,
                    source="sec_filing",
                    confidence=_confidence_for_rank(score, "sec_filing", form),
                    reason=classification.reason,
                    form=form,
                    filing_date=str(ev.published_at or "").strip(),
                    accession=str(ev.accession or "").strip(),
                    url=str(ev.url or "").strip(),
                )
            )
        elif ev.source in {"newsapi", "alpaca", "benzinga"}:
            if _software_package_spam_event(ev):
                _emit(
                    "NEWS_PACKAGE_SPAM_FILTERED symbol=%s source=%s publisher=%s title=%s"
                    % (
                        su,
                        ev.source,
                        ev.publisher or "unknown",
                        ev.headline.replace("\n", " ")[:180],
                    )
                )
                continue
            rank_source = str(ev.rank_source or "").strip().lower()
            rank_source, confidence = _classify_catalyst_headline(
                headline=ev.headline,
                catalyst_type=rank_source or ev.catalyst_type or "",
            )
            _log_catalyst_classified(su, ev.headline, rank_source, confidence)
            base_score = _tradability_score(_headline_rank_score(rank_source), ev, now)
            source_weight = _finance_source_score(ev)
            if source_weight:
                base_score += source_weight * 10.0
                confidence = min(0.98, float(confidence) + source_weight * 0.35)
                _emit(
                    "NEWS_SOURCE_QUALITY symbol=%s provider=%s publisher=%s weight=%.2f score_bonus=%.2f"
                    % (su, ev.source, ev.publisher or "unknown", source_weight, source_weight * 10.0)
                )
            news_quality = None
            catalyst_strength = None
            ai_confidence = None
            if ai_rank_enabled:
                ai_rank = rank_news_item(
                    symbol=su,
                    headline=ev.headline,
                    source=ev.source,
                    publisher=ev.publisher,
                    url=ev.url,
                    catalyst_type=rank_source or ev.catalyst_type,
                    sentiment=float(ev.sentiment or 0.0),
                    published_at=ev.published_at,
                    now=now,
                )
                base_score += score_adjustment(ai_rank, weight=ai_rank_weight)
                rank_source = ai_rank.catalyst_type if rank_source == "unknown" else rank_source
                confidence = max(float(confidence), float(ai_rank.llm_confidence))
                news_quality = ai_rank.news_quality
                catalyst_strength = ai_rank.catalyst_strength
                ai_confidence = ai_rank.llm_confidence
                _emit(
                    "AI_NEWS_RANK symbol=%s quality=%.2f catalyst_strength=%.2f llm_confidence=%.2f combined=%.2f adjustment=%.2f rationale=%s"
                    % (
                        su,
                        ai_rank.news_quality,
                        ai_rank.catalyst_strength,
                        ai_rank.llm_confidence,
                        ai_rank.combined_score,
                        score_adjustment(ai_rank, weight=ai_rank_weight),
                        ai_rank.rationale,
                    )
                )
            score = _rank_score_for_symbol(
                su,
                base_score,
            )
            _upsert(
                PremarketRankEntry(
                    symbol=su,
                    score=score,
                    catalyst_type=rank_source,
                    source=rank_source,
                    confidence=confidence,
                    reason=f"{rank_source} headline" if rank_source != "unknown" else "unknown headline",
                    news_quality=news_quality,
                    catalyst_strength=catalyst_strength,
                    ai_confidence=ai_confidence,
                    publisher=ev.publisher,
                    source_quality_weight=source_weight,
                )
            )
        elif ev.source == "twitter":
            continue
        elif ev.source == "earnings_overnight":
            score = _rank_score_for_symbol(
                su,
                _tradability_score(_headline_rank_score("earnings"), ev, now),
            )
            _upsert(
                PremarketRankEntry(
                    symbol=su,
                    score=score,
                    catalyst_type="earnings",
                    source="earnings",
                    confidence=_confidence_for_rank(score, "earnings", "earnings"),
                    reason="overnight earnings headline",
                )
            )

    for sym, cat in catalysts.items():
        su = str(sym or "").strip().upper()
        if not su:
            continue
        if str(getattr(cat, "source", "") or "").strip().lower() == "sec":
            continue
        score = float(getattr(cat, "score", 0) or 0)
        twitter_boost = 0.0
        if score > 0:
            if any(str(ev.source or "").strip().lower() == "twitter" and str(ev.symbol or "").strip().upper() == su for ev in events):
                twitter_boost = 0.5
        score += twitter_boost
        if score <= 0:
            continue
        ctype = str(getattr(cat, "catalyst_type", None) or "news")
        headline = str(getattr(cat, "headline", "") or "")
        cat_publisher = str(getattr(cat, "publisher", "") or "")
        cat_url = str(getattr(cat, "url", "") or "")
        cat_source = str(getattr(cat, "source", "") or "")
        if is_software_package_spam(headline, source=cat_source, publisher=cat_publisher, url=cat_url):
            _emit(
                "NEWS_PACKAGE_SPAM_FILTERED symbol=%s source=%s publisher=%s title=%s"
                % (su, cat_source or "unknown", cat_publisher or "unknown", headline.replace("\n", " ")[:180])
            )
            continue
        rank_source, confidence = _classify_catalyst_headline(headline=headline, catalyst_type=ctype)
        _log_catalyst_classified(su, headline, rank_source, confidence)
        ev_for_score = next(
            (
                ev
                for ev in events
                if str(ev.symbol or "").strip().upper() == su
                and str(ev.source or "").strip().lower() in {"newsapi", "alpaca", "benzinga", "earnings_overnight", "twitter"}
            ),
            NewsEvent(su, headline, "catalyst"),
        )
        base_score = _tradability_score(max(score, _headline_rank_score(rank_source)), ev_for_score, now)
        source_weight = finance_news_source_weight(
            cat_source or ev_for_score.source,
            cat_publisher or ev_for_score.publisher,
            cat_url or ev_for_score.url,
        )
        if source_weight:
            base_score += source_weight * 10.0
            confidence = min(0.98, float(confidence) + source_weight * 0.35)
            _emit(
                "NEWS_SOURCE_QUALITY symbol=%s provider=%s publisher=%s weight=%.2f score_bonus=%.2f"
                % (
                    su,
                    cat_source or ev_for_score.source or "unknown",
                    cat_publisher or ev_for_score.publisher or "unknown",
                    source_weight,
                    source_weight * 10.0,
                )
            )
        news_quality = None
        catalyst_strength = None
        ai_confidence = None
        if ai_rank_enabled:
            ai_rank = rank_news_item(
                symbol=su,
                headline=headline,
                source=cat_source or ev_for_score.source or "catalyst",
                publisher=cat_publisher or ev_for_score.publisher,
                url=cat_url or ev_for_score.url,
                catalyst_type=rank_source or ctype,
                sentiment=float(getattr(cat, "sentiment", 0.0) or ev_for_score.sentiment or 0.0),
                published_at=getattr(cat, "published_at", None) or ev_for_score.published_at,
                now=now,
            )
            base_score += score_adjustment(ai_rank, weight=ai_rank_weight)
            rank_source = ai_rank.catalyst_type if rank_source == "unknown" else rank_source
            confidence = max(float(confidence), float(ai_rank.llm_confidence))
            news_quality = ai_rank.news_quality
            catalyst_strength = ai_rank.catalyst_strength
            ai_confidence = ai_rank.llm_confidence
            _emit(
                "AI_NEWS_RANK symbol=%s quality=%.2f catalyst_strength=%.2f llm_confidence=%.2f combined=%.2f adjustment=%.2f rationale=%s"
                % (
                    su,
                    ai_rank.news_quality,
                    ai_rank.catalyst_strength,
                    ai_rank.llm_confidence,
                    ai_rank.combined_score,
                    score_adjustment(ai_rank, weight=ai_rank_weight),
                    ai_rank.rationale,
                )
            )
        _upsert(
            PremarketRankEntry(
                symbol=su,
                score=_rank_score_for_symbol(
                    su,
                    base_score,
                ),
                catalyst_type=rank_source,
                source=rank_source,
                confidence=confidence,
                reason=f"{rank_source} headline" if rank_source != "unknown" else "unknown headline",
                news_quality=news_quality,
                catalyst_strength=catalyst_strength,
                ai_confidence=ai_confidence,
                publisher=cat_publisher or ev_for_score.publisher,
                source_quality_weight=source_weight,
            )
        )

    ranked = sorted(by_sym.values(), key=lambda row: (-row.score, -row.confidence, row.symbol))
    if not ranked:
        return []
    return ranked


def log_premarket_rankings(rankings: Sequence[PremarketRankEntry], *, top_n: int = 10) -> None:
    for row in rankings:
        if row.source == "sec_filing":
            _emit(
                "PREMARKET_RANK symbol=%s score=%.2f source=sec_filing catalyst_type=%s form=%s reason=%s"
                % (row.symbol, row.score, row.catalyst_type, row.form or "unknown", row.reason)
            )
        else:
            _emit(
                "PREMARKET_RANK symbol=%s score=%.2f source=%s catalyst_type=%s confidence=%.2f reason=%s"
                % (row.symbol, row.score, row.source, row.catalyst_type, row.confidence, row.reason)
            )
    for idx, row in enumerate(list(rankings)[: max(1, top_n)], start=1):
        _emit(
            "PREMARKET_TOP10 rank=%d symbol=%s score=%.2f catalyst_type=%s source=%s confidence=%.2f reason=%s"
            % (idx, row.symbol, row.score, row.catalyst_type, row.source, row.confidence, row.reason)
        )


def write_premarket_rank_json(
    path: Path,
    rankings: Sequence[PremarketRankEntry],
    *,
    now: datetime,
) -> None:
    items: list[dict[str, Any]] = []
    for row in rankings:
        item: dict[str, Any] = {
            "symbol": row.symbol,
            "score": row.score,
            "source": row.source,
            "catalyst_type": row.catalyst_type,
            "reason": row.reason,
        }
        if row.form:
            item["form"] = row.form
        if row.filing_date:
            item["filing_date"] = row.filing_date
        if row.accession:
            item["accession"] = row.accession
        if row.url:
            item["url"] = row.url
        if row.source != "sec_filing":
            item["confidence"] = round(row.confidence, 3)
        if row.news_quality is not None:
            item["news_quality"] = round(float(row.news_quality), 3)
        if row.catalyst_strength is not None:
            item["catalyst_strength"] = round(float(row.catalyst_strength), 3)
        if row.ai_confidence is not None:
            item["ai_confidence"] = round(float(row.ai_confidence), 3)
        items.append(item)
    payload: dict[str, Any] = {
        "date": now.date().isoformat(),
        "generated_at": now.isoformat(),
        "items": items,
    }
    if items:
        payload["top_catalyst"] = dict(items[0])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _serialize_event(ev: NewsEvent) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "symbol": ev.symbol,
        "headline": ev.headline,
        "source": ev.source,
        "publisher": ev.publisher,
        "url": ev.url,
        "published_at": ev.published_at,
        "form": ev.form,
        "accession": ev.accession,
        "cik": ev.cik,
        "primary_doc": ev.primary_doc,
        "catalyst_type": ev.catalyst_type,
        "rank_reason": ev.rank_reason,
        "rank_source": ev.rank_source,
        "sec_routine": ev.sec_routine,
        "rankable": ev.rankable,
        "sentiment": ev.sentiment,
        "score": ev.score,
        "gap_pct": ev.gap_pct,
        "volume_surge_pct": ev.volume_surge_pct,
    }
    def _keep(key: str, value: Any) -> bool:
        if key in {"sec_routine", "rankable", "sentiment", "score", "gap_pct", "volume_surge_pct"}:
            return True
        return value not in ("", None, False)

    return {key: value for key, value in payload.items() if _keep(key, value)}


def _serialize_catalyst(sym: str, cat: NewsCatalyst) -> dict[str, Any]:
    def _safe_text(value: Any) -> str:
        return str(value or "").strip()

    def _safe_float(value: Any, default: float = 0.0) -> float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        text = str(value or "").strip()
        if not text:
            return float(default)
        try:
            return float(text)
        except (TypeError, ValueError):
            return float(default)

    def _safe_int(value: Any, default: int = 0) -> int:
        if isinstance(value, int) and not isinstance(value, bool):
            return int(value)
        if isinstance(value, float):
            return int(value)
        text = str(value or "").strip()
        if not text:
            return int(default)
        try:
            return int(float(text))
        except (TypeError, ValueError):
            return int(default)

    article_symbols: list[str] = []
    raw_symbols = getattr(cat, "article_symbols", ())
    if isinstance(raw_symbols, (list, tuple, set)):
        for item in raw_symbols:
            text = _safe_text(item)
            if text:
                article_symbols.append(text)

    published_at_obj = getattr(cat, "published_at", None)
    if isinstance(published_at_obj, datetime):
        published_at_text = published_at_obj.isoformat()
    else:
        published_at_text = ""

    payload: dict[str, Any] = {
        "symbol": _safe_text(sym).upper(),
        "score": _safe_float(getattr(cat, "score", 0.0)),
        "headline": _safe_text(getattr(cat, "headline", "")),
        "article_symbols": article_symbols,
        "published_at": published_at_text,
        "source": _safe_text(getattr(cat, "source", "")),
        "publisher": _safe_text(getattr(cat, "publisher", "")),
        "url": _safe_text(getattr(cat, "url", "")),
        "catalyst_type": _safe_text(getattr(cat, "catalyst_type", "")),
        "article_count": _safe_int(getattr(cat, "article_count", 0)),
        "sentiment": _safe_float(getattr(cat, "sentiment", 0.0)),
        "event_score": _safe_float(getattr(cat, "score", 0.0)),
    }
    def _keep(key: str, value: Any) -> bool:
        if key in {"score", "article_count", "sentiment", "event_score"}:
            return True
        return value not in ("", None, [], 0)

    return {key: value for key, value in payload.items() if _keep(key, value)}


def _serialize_rank(row: PremarketRankEntry) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "symbol": row.symbol,
        "score": row.score,
        "source": row.source,
        "catalyst_type": row.catalyst_type,
        "reason": row.reason,
        "form": row.form,
        "filing_date": row.filing_date,
        "accession": row.accession,
        "url": row.url,
        "confidence": round(row.confidence, 3),
        "publisher": row.publisher,
    }
    if row.source_quality_weight is not None:
        payload["source_quality_weight"] = round(float(row.source_quality_weight), 3)
    def _keep(key: str, value: Any) -> bool:
        if key in {"score", "confidence"}:
            return True
        return value not in ("", None, 0)

    return {key: value for key, value in payload.items() if _keep(key, value)}


def _artifact_payload_counts(path: Path) -> dict[str, int]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"events": 0, "catalysts": 0, "rankings": 0, "catalyst_ranked_symbols": 0}
    if not isinstance(payload, Mapping):
        return {"events": 0, "catalysts": 0, "rankings": 0, "catalyst_ranked_symbols": 0}
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    catalysts = payload.get("catalysts") if isinstance(payload.get("catalysts"), list) else []
    rankings = payload.get("rankings") if isinstance(payload.get("rankings"), list) else []
    catalyst_symbols = {
        str(item.get("symbol") or "").strip().upper()
        for item in catalysts
        if isinstance(item, Mapping) and str(item.get("symbol") or "").strip()
    }
    ranked_symbols = {
        str(item.get("symbol") or "").strip().upper()
        for item in rankings
        if isinstance(item, Mapping) and str(item.get("symbol") or "").strip()
    }
    return {
        "events": len(events),
        "catalysts": len(catalysts),
        "rankings": len(rankings),
        "catalyst_ranked_symbols": len(catalyst_symbols | ranked_symbols),
    }


def _artifact_payload_fresh(path: Path, *, now: datetime) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(payload, Mapping):
        return False
    generated_at_raw = str(payload.get("generated_at") or "").strip()
    if not generated_at_raw:
        return False
    try:
        generated_at = datetime.fromisoformat(generated_at_raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    ttl_raw = payload.get("ttl_minutes")
    try:
        ttl_minutes = int(float(ttl_raw or _pm_section(None).get("artifact_ttl_minutes", 390) or 390))
    except (TypeError, ValueError):
        ttl_minutes = 390
    now_cmp = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    age_minutes = (now_cmp.astimezone(timezone.utc) - generated_at.astimezone(timezone.utc)).total_seconds() / 60.0
    return age_minutes <= float(max(1, ttl_minutes))


def _artifact_quality(counts: Mapping[str, int]) -> int:
    return (
        int(counts.get("events") or 0)
        + int(counts.get("catalysts") or 0) * 4
        + int(counts.get("rankings") or 0) * 4
        + int(counts.get("catalyst_ranked_symbols") or 0) * 6
    )


@dataclass(frozen=True)
class _PremarketArtifactPreservation:
    preserve: bool
    reason: str
    new_counts: dict[str, int]
    existing_counts: dict[str, int]
    rate_limited: bool = False
    existing_fresh: bool = False


def _int_pm_config(config: Mapping[str, Any] | None, key: str, default: int) -> int:
    try:
        return int(float(_pm_section(config).get(key, default) or default))
    except (TypeError, ValueError):
        return int(default)


def _bool_pm_config(config: Mapping[str, Any] | None, key: str, default: bool) -> bool:
    raw = _pm_section(config).get(key, default)
    if isinstance(raw, str):
        return raw.strip().lower() not in {"0", "false", "no", "off", "n", ""}
    return bool(raw)


def _provider_results_rate_limited(provider_results: "PremarketProviderResults | None") -> bool:
    if provider_results is None:
        return False
    for result in provider_results.provider_results:
        if int(getattr(result, "http_status", 0) or 0) == 429:
            return True
        if str(getattr(result, "skip_reason", "") or "").strip().lower() in {
            "rate_limited",
            "newsapi_rate_limited_for_job",
            "depends_on_newsapi_rate_limited",
        }:
            return True
    return False


def _premarket_artifact_preservation_decision(
    project_root: Path,
    *,
    now: datetime,
    config: Mapping[str, Any] | None,
    provider_rate_limited: bool,
    new_event_count: int,
    new_catalyst_count: int,
    new_ranking_count: int,
    new_catalyst_ranked_symbols: int,
) -> _PremarketArtifactPreservation:
    new_counts = {
        "events": int(new_event_count),
        "catalysts": int(new_catalyst_count),
        "rankings": int(new_ranking_count),
        "catalyst_ranked_symbols": int(new_catalyst_ranked_symbols),
    }
    existing_path = default_premarket_event_feed_path(project_root)
    existing = _artifact_payload_counts(existing_path)
    min_events = max(0, _int_pm_config(config, "min_events_to_overwrite", 30))
    min_rankings = max(0, _int_pm_config(config, "min_rankings_to_overwrite", 10))
    preserve_on_rate_limit = _bool_pm_config(config, "preserve_on_provider_rate_limit", True)
    preserve_if_richer = _bool_pm_config(config, "preserve_existing_if_richer", True)
    below_minimum = int(new_counts["events"]) < min_events or int(new_counts["rankings"]) < min_rankings
    triggered = below_minimum or (bool(provider_rate_limited) and preserve_on_rate_limit)
    if not triggered or not preserve_if_richer:
        return _PremarketArtifactPreservation(False, "not_applicable", new_counts, existing, bool(provider_rate_limited))
    existing_fresh = existing_path.exists() and _artifact_payload_fresh(existing_path, now=now)
    if not existing_fresh:
        return _PremarketArtifactPreservation(False, "existing_stale_or_missing", new_counts, existing, bool(provider_rate_limited), False)
    existing_richer = _artifact_quality(existing) > _artifact_quality(new_counts)
    if existing_richer:
        return _PremarketArtifactPreservation(
            True,
            "low_coverage_or_rate_limited",
            new_counts,
            existing,
            bool(provider_rate_limited),
            True,
        )
    return _PremarketArtifactPreservation(False, "existing_not_richer", new_counts, existing, bool(provider_rate_limited), True)


def _should_preserve_existing_premarket_artifacts(
    project_root: Path,
    *,
    now: datetime,
    config: Mapping[str, Any] | None,
    provider_rate_limited: bool = False,
    new_event_count: int,
    new_catalyst_count: int,
    new_ranking_count: int,
    new_catalyst_ranked_symbols: int,
) -> tuple[bool, dict[str, int]]:
    decision = _premarket_artifact_preservation_decision(
        project_root,
        now=now,
        config=config,
        provider_rate_limited=provider_rate_limited,
        new_event_count=new_event_count,
        new_catalyst_count=new_catalyst_count,
        new_ranking_count=new_ranking_count,
        new_catalyst_ranked_symbols=new_catalyst_ranked_symbols,
    )
    return decision.preserve, decision.existing_counts


def _log_premarket_artifact_preserved(decision: _PremarketArtifactPreservation) -> None:
    _emit(
        "PREMARKET_ARTIFACT_PRESERVED reason=low_coverage_or_rate_limited "
        "existing_events=%d new_events=%d existing_rankings=%d new_rankings=%d "
        "existing_catalysts=%d new_catalysts=%d existing_catalyst_ranked_symbols=%d "
        "new_catalyst_ranked_symbols=%d rate_limited=%s"
        % (
            int(decision.existing_counts.get("events") or 0),
            int(decision.new_counts.get("events") or 0),
            int(decision.existing_counts.get("rankings") or 0),
            int(decision.new_counts.get("rankings") or 0),
            int(decision.existing_counts.get("catalysts") or 0),
            int(decision.new_counts.get("catalysts") or 0),
            int(decision.existing_counts.get("catalyst_ranked_symbols") or 0),
            int(decision.new_counts.get("catalyst_ranked_symbols") or 0),
            _bool_text(decision.rate_limited),
        ),
        level=logging.WARNING,
    )


def write_premarket_artifacts(
    project_root: Path,
    *,
    now: datetime,
    source: str,
    events: Sequence[NewsEvent],
    catalysts: Mapping[str, NewsCatalyst],
    rankings: Sequence[PremarketRankEntry],
    candidate_symbols: Sequence[str] | None = None,
    ttl_minutes: int = 390,
    config: Mapping[str, Any] | None = None,
    provider_rate_limited: bool = False,
    preserve_existing: bool = True,
) -> None:
    """Write the latest premarket artifacts consumed by the live loop."""
    ttl_minutes = max(1, int(ttl_minutes or 390))
    artifact_dir = default_premarket_artifacts_dir(project_root)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    symbol_set: set[str] = set()
    for ev in events:
        sym = str(ev.symbol or "").strip().upper()
        if sym:
            symbol_set.add(sym)
    for sym in catalysts.keys():
        sym_u = str(sym or "").strip().upper()
        if sym_u:
            symbol_set.add(sym_u)
    for row in rankings:
        sym = str(row.symbol or "").strip().upper()
        if sym:
            symbol_set.add(sym)
    for sym in candidate_symbols or []:
        sym_u = str(sym or "").strip().upper()
        if sym_u:
            symbol_set.add(sym_u)
    symbols = sorted(symbol_set)
    serialized_events = [_serialize_event(ev) for ev in events]
    serialized_catalysts = [_serialize_catalyst(sym, cat) for sym, cat in catalysts.items()]
    serialized_rankings = [_serialize_rank(row) for row in rankings]
    catalyst_symbols = sorted(
        {
            str(item.get("symbol") or "").strip().upper()
            for item in serialized_catalysts
            if str(item.get("symbol") or "").strip()
        }
    )
    ranked_symbols = sorted(
        {
            str(item.get("symbol") or "").strip().upper()
            for item in serialized_rankings
            if str(item.get("symbol") or "").strip()
        }
    )
    catalyst_ranked_symbols = sorted(set(catalyst_symbols) | set(ranked_symbols))
    _emit(
        "PREMARKET_ARTIFACT_COUNTS event_count=%d rankable_event_count=%d catalyst_count=%d ranking_count=%d"
        % (
            len(serialized_events),
            sum(1 for ev in events if bool(getattr(ev, "rankable", False))),
            len(serialized_catalysts),
            len(serialized_rankings),
        )
    )
    _emit(
        "PREMARKET_RANKED_SYMBOLS count=%d symbols=%s"
        % (len(ranked_symbols), ",".join(ranked_symbols) or "none")
    )
    _emit(
        "PREMARKET_CATALYST_SYMBOLS count=%d symbols=%s"
        % (len(catalyst_symbols), ",".join(catalyst_symbols) or "none")
    )
    min_events = max(0, _int_pm_config(config, "min_events_to_overwrite", 30))
    min_rankings = max(0, _int_pm_config(config, "min_rankings_to_overwrite", 10))
    if len(serialized_events) < min_events or len(serialized_rankings) < min_rankings:
        _emit(
            "PREMARKET_LOW_COVERAGE catalyst_ranked_symbols=%d total_events=%d rankings=%d min_events=%d min_rankings=%d"
            % (len(catalyst_ranked_symbols), len(serialized_events), len(serialized_rankings), min_events, min_rankings),
            level=logging.WARNING,
        )
    decision = _premarket_artifact_preservation_decision(
        project_root,
        now=now,
        config=config,
        provider_rate_limited=provider_rate_limited,
        new_event_count=len(serialized_events),
        new_catalyst_count=len(serialized_catalysts),
        new_ranking_count=len(serialized_rankings),
        new_catalyst_ranked_symbols=len(catalyst_ranked_symbols),
    )
    if preserve_existing and decision.preserve:
        _log_premarket_artifact_preserved(decision)
        return
    payloads = {
        default_premarket_event_feed_path(project_root): {
            "generated_at": now.isoformat(),
            "source": source,
            "ttl_minutes": ttl_minutes,
            "symbols": symbols,
            "candidate_symbols": symbols,
            "events": serialized_events,
            "catalysts": serialized_catalysts,
            "rankings": serialized_rankings,
        },
        default_premarket_rankings_path(project_root): {
            "generated_at": now.isoformat(),
            "source": source,
            "ttl_minutes": ttl_minutes,
            "symbols": symbols,
            "candidate_symbols": symbols,
            "events": serialized_events,
            "catalysts": serialized_catalysts,
            "rankings": serialized_rankings,
        },
        default_premarket_catalysts_path(project_root): {
            "generated_at": now.isoformat(),
            "source": source,
            "ttl_minutes": ttl_minutes,
            "symbols": symbols,
            "candidate_symbols": symbols,
            "events": serialized_events,
            "catalysts": serialized_catalysts,
            "rankings": serialized_rankings,
        },
    }
    for path, payload in payloads.items():
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        _emit(
            "PREMARKET_ARTIFACT_WRITE_COUNT path=%s symbols=%d events=%d catalysts=%d rankings=%d"
            % (
                str(path),
                len(payload.get("symbols") or []),
                len(payload.get("events") or []),
                len(payload.get("catalysts") or []),
                len(payload.get("rankings") or []),
            )
        )
        _emit(
            "PREMARKET_ARTIFACT_WRITTEN path=%s symbols=%d events=%d rankings=%d catalysts=%d"
            % (
                str(path),
                len(payload.get("symbols") or []),
                len(payload.get("events") or []),
                len(payload.get("rankings") or []),
                len(payload.get("catalysts") or []),
            )
        )
    if serialized_rankings:
        previous_payloads = {
            artifact_dir / "previous_non_empty_event_feed.json": payloads[default_premarket_event_feed_path(project_root)],
            artifact_dir / "previous_non_empty_rankings.json": payloads[default_premarket_rankings_path(project_root)],
            artifact_dir / "previous_non_empty_catalysts.json": payloads[default_premarket_catalysts_path(project_root)],
        }
        for path, payload in previous_payloads.items():
            path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _provider_diagnostic_record(result: ProviderExecResult) -> dict[str, Any]:
    raw_count = int(result.raw_articles_before_filter or result.articles or result.filings or 0)
    filtered_count = int(result.articles_after_filter or result.articles or result.filings or 0)
    rate_limited = int(result.http_status or 0) == 429 or str(result.skip_reason or "").strip().lower() in {
        "rate_limited",
        "newsapi_rate_limited_for_job",
        "depends_on_newsapi_rate_limited",
    }
    reason = str(result.skip_reason or result.error or ("rate_limited" if rate_limited else "ok")).strip() or "ok"
    return {
        "enabled": bool(result.enabled),
        "request_sent": bool(result.request_sent),
        "http_status": result.http_status,
        "raw_count": raw_count,
        "filtered_count": filtered_count,
        "rate_limited": bool(rate_limited),
        "duration_ms": round(float(result.duration_ms or 0.0), 1),
        "reason": reason,
    }


def write_premarket_provider_diagnostics(
    project_root: Path,
    *,
    now: datetime,
    source: str,
    provider_results: PremarketProviderResults,
) -> Path:
    """Write a research/ops diagnostics snapshot for the latest provider run."""
    path = default_premarket_provider_diagnostics_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    providers = {
        result.provider: _provider_diagnostic_record(result)
        for result in provider_results.provider_results
    }
    payload = {
        "generated_at": now.isoformat(),
        "source": source,
        "providers": providers,
        "summary": {
            "provider_count": len(providers),
            "rate_limited": sorted(name for name, row in providers.items() if bool(row.get("rate_limited"))),
            "request_sent": sorted(name for name, row in providers.items() if bool(row.get("request_sent"))),
            "total_raw_count": sum(int(row.get("raw_count") or 0) for row in providers.values()),
            "total_filtered_count": sum(int(row.get("filtered_count") or 0) for row in providers.values()),
        },
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _log_sec_filing(ev: NewsEvent) -> None:
    _emit(
        "PREMARKET_SEC_FILING symbol=%s cik=%s form=%s filing_date=%s accession=%s primary_doc=%s url=%s routine=%s rankable=%s"
        % (
            ev.symbol,
            ev.cik or "unknown",
            ev.form or "unknown",
            ev.published_at or "unknown",
            ev.accession or "unknown",
            ev.primary_doc or "unknown",
            ev.url or "unknown",
            _bool_text(ev.sec_routine),
            _bool_text(ev.rankable),
        )
    )


def _newsapi_article_to_events(
    articles: Sequence[Mapping[str, Any]],
    universe: Sequence[str],
    cfg: Mapping[str, Any] | None = None,
) -> list[NewsEvent]:
    out: list[NewsEvent] = []
    uni = [str(s).strip().upper() for s in universe if str(s).strip()]
    symbol_names = _symbol_names(cfg)
    for art in articles:
        if not isinstance(art, Mapping):
            continue
        title = str(art.get("title") or "").strip()
        desc = str(art.get("description") or "").strip()
        if not title:
            continue
        symbols: list[str] = []
        publisher = _article_publisher(art)
        url = str(art.get("url") or "").strip()
        if is_software_package_spam(title, source="newsapi", publisher=publisher, url=url):
            _emit(
                "NEWS_PACKAGE_SPAM_FILTERED symbol=%s source=newsapi publisher=%s title=%s"
                % (
                    str(art.get("_premarket_symbol") or "").strip().upper() or "unknown",
                    publisher or "unknown",
                    title.replace("\n", " ")[:180],
                )
            )
            continue
        tagged_sym = str(art.get("_premarket_symbol") or "").strip().upper()
        if tagged_sym:
            symbols = [tagged_sym]
        raw_syms = art.get("symbols")
        if isinstance(raw_syms, (list, tuple)):
            for item in raw_syms:
                su = str(item or "").strip().upper()
                if su and su not in symbols:
                    symbols.append(su)
        if not symbols:
            blob = f"{title} {desc}".upper()
            for sym in uni:
                name = str(symbol_names.get(sym) or "").strip().upper()
                ticker_match = sym and (f" {sym} " in f" {blob} " or f'"{sym}"' in blob)
                name_match = bool(name and f" {name} " in f" {blob} ")
                if ticker_match or name_match:
                    symbols.append(sym)
                    break
        if not symbols:
            symbols = [""]
        published = str(art.get("publishedAt") or "").strip()
        rank_source = str(art.get("_premarket_rank_source") or "").strip().lower()
        rank_source, confidence = _classify_catalyst_headline(headline=title, catalyst_type=rank_source)
        _log_catalyst_classified(symbols[0] if symbols else "", title, rank_source, confidence)
        score, _ctype = score_article_text(title)
        for sym in symbols:
            out.append(
                NewsEvent(
                    symbol=sym,
                    headline=title,
                    source="newsapi",
                    publisher=publisher,
                    url=url,
                    published_at=published,
                    rank_reason=rank_source,
                    rank_source=rank_source,
                    catalyst_type=rank_source,
                    sentiment=max(-1.0, min(1.0, float(score) / 10.0)),
                    score=float(score),
                    gap_pct=_first_float(art, ("gap_pct", "premarket_gap_pct", "day_gain_pct", "gain_pct")),
                    volume_surge_pct=_first_float(art, ("volume_surge_pct", "relative_volume_pct", "rel_volume_pct")),
                )
            )
    return out


def _event_from_mapping(raw: Mapping[str, Any], *, source: str) -> NewsEvent | None:
    raw_symbol = raw.get("symbol") or raw.get("ticker") or raw.get("symbols") or ""
    if isinstance(raw_symbol, (list, tuple, set)):
        raw_symbol = next((str(item or "").strip() for item in raw_symbol if str(item or "").strip()), "")
    symbol = str(raw_symbol or "").strip().upper()
    title = str(raw.get("title") or raw.get("headline") or raw.get("text") or "").strip()
    if not title:
        return None
    url = str(raw.get("url") or raw.get("link") or "").strip()
    raw_source = raw.get("publisher") or raw.get("provider") or raw.get("source_name") or raw.get("source") or ""
    if isinstance(raw_source, Mapping):
        raw_source = raw_source.get("name") or raw_source.get("domain") or ""
    publisher = str(raw_source or "").strip()
    published_at = str(
        raw.get("published_at")
        or raw.get("publishedAt")
        or raw.get("created_at")
        or raw.get("datetime")
        or raw.get("publishedDate")
        or ""
    ).strip()
    try:
        sentiment = float(raw.get("sentiment") or raw.get("sentiment_score") or 0.0)
    except (TypeError, ValueError):
        sentiment = 0.0
    try:
        score = float(raw.get("score") or raw.get("news_score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    if not symbol:
        return None
    return NewsEvent(
        symbol=symbol,
        headline=title,
        source=source,
        publisher=publisher,
        url=url,
        published_at=published_at,
        sentiment=sentiment,
        score=score,
        gap_pct=_first_float(raw, ("gap_pct", "premarket_gap_pct", "day_gain_pct", "gain_pct")),
        volume_surge_pct=_first_float(raw, ("volume_surge_pct", "relative_volume_pct", "rel_volume_pct")),
    )


def _news_event_score(ev: NewsEvent) -> float:
    try:
        if float(ev.score or 0.0) != 0.0:
            return float(ev.score or 0.0)
    except (TypeError, ValueError):
        pass
    if ev.source == "sec":
        classification = classify_sec_filing(ev.form, None)
        return float(classification.score)
    if ev.source in {"newsapi", "alpaca", "benzinga", "finnhub", "marketaux", "fmp"}:
        score, _ctype = score_article_text(ev.headline)
        return float(score)
    if ev.source == "twitter":
        return max(0.0, float(ev.score or 0.0))
    return max(0.0, float(ev.score or 0.0))


def fetch_newsapi_articles(
    symbols: Sequence[str],
    cfg: Mapping[str, Any] | None,
    timeout_seconds: float,
    *,
    now: datetime | None = None,
    rate_limit_log_state: dict[str, bool] | None = None,
) -> ProviderExecResult:
    """
    Fetch recent headlines from NewsAPI for *symbols* using :mod:`newsapi_client`.

    Logs ``PREMARKET_NEWS_PROVIDER provider=newsapi ...`` lines with timing.
    """
    started = time_module.monotonic()
    result = ProviderExecResult(provider="newsapi")
    result.enabled = _premarket_newsapi_enabled(cfg)
    _news_provider_line("newsapi", enabled=_bool_text(result.enabled))
    if not result.enabled:
        result.skip_reason = "newsapi_disabled"
        result.duration_ms = (time_module.monotonic() - started) * 1000.0
        _news_provider_line("newsapi", request_sent=_bool_text(False), reason=result.skip_reason)
        _news_provider_line("newsapi", duration_ms=f"{result.duration_ms:.1f}")
        return result

    api_key = newsapi_key_from_config(dict(cfg or {}))
    key_present = bool(api_key)
    _emit(f"NEWSAPI_KEY_PRESENT={_bool_text(key_present)}")
    _news_provider_line("newsapi", api_key_present=_bool_text(key_present))
    if not key_present:
        result.skip_reason = "missing_api_key"
        result.duration_ms = (time_module.monotonic() - started) * 1000.0
        _news_provider_line("newsapi", request_sent=_bool_text(False), reason=result.skip_reason)
        _news_provider_line("newsapi", duration_ms=f"{result.duration_ms:.1f}")
        return result

    if _newsapi_rate_limited_for_job(rate_limit_log_state):
        result.skip_reason = "newsapi_rate_limited_for_job"
        result.duration_ms = (time_module.monotonic() - started) * 1000.0
        _news_provider_line("newsapi", request_sent=_bool_text(False), reason=result.skip_reason)
        _news_provider_line("newsapi", duration_ms=f"{result.duration_ms:.1f}")
        return result

    if now is None:
        now = datetime.now(timezone.utc)
    lookback_h = _premarket_newsapi_lookback_hours(cfg)
    try:
        ns = _news_sentiment_section(cfg)
        page_size = int(ns.get("max_headlines", 30) or 30)
    except (TypeError, ValueError):
        page_size = 30
    _news_provider_line("newsapi", effective_lookback_hours=lookback_h)

    uniq = [str(s).strip().upper() for s in symbols if str(s).strip()]
    uniq = list(dict.fromkeys(uniq))
    result.request_symbol_count = len(uniq)
    if not uniq:
        result.skip_reason = "empty_symbol_universe"
        result.duration_ms = (time_module.monotonic() - started) * 1000.0
        _news_provider_line("newsapi", request_symbol_count=result.request_symbol_count)
        _news_provider_line("newsapi", request_sent=_bool_text(False), reason=result.skip_reason)
        _news_provider_line("newsapi", duration_ms=f"{result.duration_ms:.1f}")
        return result

    ordered = _operating_company_symbols(uniq)[: _newsapi_fallback_top_n(cfg)]
    result.request_symbol_count = len(ordered)
    if not ordered:
        result.skip_reason = "no_operating_companies_after_etf_filter"
        result.duration_ms = (time_module.monotonic() - started) * 1000.0
        _news_provider_line("newsapi", request_symbol_count=result.request_symbol_count)
        _news_provider_line("newsapi", request_sent=_bool_text(False), reason=result.skip_reason)
        _news_provider_line("newsapi", duration_ms=f"{result.duration_ms:.1f}")
        return result

    batches = build_newsapi_query_batches(ordered, cfg)
    if not batches:
        result.skip_reason = "no_newsapi_batches"
        result.duration_ms = (time_module.monotonic() - started) * 1000.0
        _news_provider_line("newsapi", request_sent=_bool_text(False), reason=result.skip_reason)
        _news_provider_line("newsapi", duration_ms=f"{result.duration_ms:.1f}")
        return result

    _emit(
        "NEWS_FETCH_START provider=NewsAPI symbol_count=%d request_cap=%d batch_count=%d remaining_budget=%d"
        % (
            len(ordered),
            _newsapi_request_cap(cfg),
            len(batches),
            _newsapi_remaining_daily_budget(cfg, now),
        )
    )
    _emit(
        "NEWSAPI_LOOKBACK_WINDOW provider=NewsAPI endpoint=everything lookback_hours=%d from_date=%s to_date=%s previous_lookback_hours=24"
        % (
            int(lookback_h),
            (now.astimezone(timezone.utc) - timedelta(hours=max(1, lookback_h))).strftime("%Y-%m-%dT%H:%M:%SZ"),
            now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )
    per_request_timeout = max(1.0, float(timeout_seconds))
    raw_articles_before_filter = 0
    articles_after_filter = 0
    returned_symbols: list[str] = []
    sample_titles: list[str] = []
    rate_limit_headers: dict[str, str] = {}
    reason_breakdown: dict[str, int] = {
        "dedupe_removed": 0,
        "no_symbol_match": 0,
    }
    article_cache: set[str] = set()
    events: list[NewsEvent] = []
    meta: dict[str, Any] = {}
    request_sent = False
    http_status: int | None = None
    cache_ttl = _newsapi_cache_ttl_seconds(cfg)
    cache_hits = 0
    requests_made = 0
    symbol_search_raw_articles = 0

    for batch in batches:
        if _newsapi_remaining_daily_budget(cfg, now) <= 0:
            result.skip_reason = "newsapi_daily_budget_exhausted"
            break
        if _newsapi_rate_limited_for_job(rate_limit_log_state):
            result.skip_reason = "newsapi_rate_limited_for_job"
            break
        batch_query = batch.query
        if not batch_query:
            continue
        cache_key = f"{batch_query}|lookback={lookback_h}|page={page_size}|lang=en"
        cached = _NEWSAPI_QUERY_CACHE.get(cache_key)
        batch_meta: dict[str, Any] = {}
        batch_articles: list[dict[str, Any]] = []
        batch_http: int | None = None
        batch_request_sent = False
        elapsed_ms = 0.0

        if cached is not None and (now - cached.fetched_at).total_seconds() < cache_ttl:
            cache_hits += 1
            batch_articles = list(cached.articles)
            batch_http = cached.http_status
            batch_meta["skip_reason"] = cached.skip_reason
            batch_meta["error"] = cached.error
        else:
            _emit(
                "NEWS_FETCH_START provider=NewsAPI symbols=%s query_length=%d"
                % (",".join(batch.symbols), len(batch_query))
            )
            batch_start = time_module.monotonic()
            requests_made += 1
            _newsapi_consume_daily_budget(1, now)
            try:
                batch_articles = fetch_articles_query(
                    batch_query,
                    api_key,
                    lookback_hours=lookback_h,
                    now=now,
                    page_size=min(100, max(page_size, 3)),
                    timeout_sec=per_request_timeout,
                    raise_on_rate_limit=True,
                    meta=batch_meta,
                )
                batch_request_sent = bool(batch_meta.get("request_sent"))
            except NewsAPIRateLimitError as exc:
                batch_request_sent = True
                batch_http = 429
                batch_meta["http_status"] = 429
                batch_meta["skip_reason"] = "rate_limited"
                batch_meta["error"] = str(exc)
                if isinstance(batch_meta.get("rate_limit_headers"), Mapping):
                    rate_limit_headers.update({str(k): str(v) for k, v in batch_meta["rate_limit_headers"].items()})
                request_sent = True
                http_status = 429
                _emit_newsapi_rate_limited_once(rate_limit_log_state, provider="newsapi")
                _NEWSAPI_QUERY_CACHE[cache_key] = _NewsapiBatchCacheEntry(
                    fetched_at=now,
                    articles=[],
                    http_status=429,
                    skip_reason="rate_limited",
                    error=str(exc)[:240],
                )
                _emit("NEWS_FETCH_RESULT provider=NewsAPI status_code=429 article_count=0 elapsed_ms=%.1f" % ((time_module.monotonic() - batch_start) * 1000.0))
                _emit("NEWS_FETCH_FILTER before_count=0 after_count=0 reason_breakdown=%s" % ",".join(f"{k}:{v}" for k, v in reason_breakdown.items()))
                break
            except Exception as exc:
                batch_articles = []
                batch_meta["error"] = str(exc)
                _emit("NEWS_FETCH_ERROR exception=%s" % redact_newsapi_secret(str(exc))[:240])
            elapsed_ms = (time_module.monotonic() - batch_start) * 1000.0
            batch_http = batch_meta.get("http_status")
            _NEWSAPI_QUERY_CACHE[cache_key] = _NewsapiBatchCacheEntry(
                fetched_at=now,
                articles=list(batch_articles),
                http_status=int(batch_http) if isinstance(batch_http, int) else (int(batch_http) if str(batch_http).isdigit() else None),
                skip_reason=str(batch_meta.get("skip_reason") or ""),
                error=str(batch_meta.get("error") or "")[:240],
            )
            request_sent = request_sent or batch_request_sent
            if batch_http is not None:
                http_status = int(batch_http)
            elif http_status is None and batch_meta.get("http_status") is not None:
                try:
                    http_status = int(batch_meta.get("http_status"))
                except (TypeError, ValueError):
                    http_status = http_status

        raw_count = len(batch_articles)
        _log_newsapi_window_diagnostics(
            route="symbol_batch",
            articles=batch_articles,
            now=now,
            lookback_hours=lookback_h,
        )
        symbol_search_raw_articles += raw_count
        if not sample_titles:
            sample_titles = _sample_article_titles(batch_articles)
        for su in _symbols_from_newsapi_articles(batch_articles):
            if su not in returned_symbols:
                returned_symbols.append(su)
        raw_articles_before_filter += raw_count
        new_articles = _filter_new_articles(batch_articles, article_cache)
        dedupe_removed = max(0, raw_count - len(new_articles))
        batch_events = _newsapi_article_to_events(new_articles, batch.symbols, cfg)
        filtered_events = [ev for ev in batch_events if ev.symbol]
        for ev in filtered_events:
            su = str(ev.symbol or "").strip().upper()
            if su and su not in returned_symbols:
                returned_symbols.append(su)
        no_symbol_match = max(0, len(batch_events) - len(filtered_events))
        articles_after_filter += len(filtered_events)
        reason_breakdown["dedupe_removed"] += dedupe_removed
        reason_breakdown["no_symbol_match"] += no_symbol_match
        events.extend(filtered_events)

        if batch_http is None and batch_request_sent:
            try:
                batch_http = int(batch_meta.get("http_status")) if batch_meta.get("http_status") is not None else None
            except (TypeError, ValueError):
                batch_http = None

        _emit(
            "NEWS_FETCH_RESULT provider=NewsAPI status_code=%s article_count=%d elapsed_ms=%.1f"
            % (str(batch_http) if batch_http is not None else "cache", raw_count, elapsed_ms)
        )
        _emit(
            "NEWSAPI_RAW_ARTICLES provider=NewsAPI route=symbol_batch endpoint=everything raw_article_count=%d before_symbol_filter=%d symbols=%s"
            % (raw_count, raw_count, ",".join(batch.symbols))
        )
        _emit(
            "NEWS_FETCH_FILTER before_count=%d after_count=%d reason_breakdown=%s"
            % (
                raw_count,
                len(filtered_events),
                ",".join(f"{k}:{v}" for k, v in reason_breakdown.items()),
            )
        )
        if batch_request_sent:
            meta["request_sent"] = True
        if batch_http is not None:
            meta["http_status"] = batch_http
        if batch_meta.get("error"):
            meta["error"] = batch_meta.get("error")
        if isinstance(batch_meta.get("rate_limit_headers"), Mapping):
            rate_limit_headers.update({str(k): str(v) for k, v in batch_meta["rate_limit_headers"].items()})
        if batch_meta.get("skip_reason") and not meta.get("skip_reason"):
            meta["skip_reason"] = batch_meta.get("skip_reason")
        if batch_meta.get("query"):
            meta["query"] = str(batch_meta.get("query"))
        if batch_meta.get("from"):
            meta["from"] = str(batch_meta.get("from"))
        if batch_meta.get("to"):
            meta["to"] = str(batch_meta.get("to"))

    def _fetch_and_process_fallback(route: str, query: str, *, top_headlines: bool = False) -> None:
        nonlocal request_sent, http_status, requests_made, raw_articles_before_filter
        nonlocal articles_after_filter, sample_titles, rate_limit_headers, events
        nonlocal cache_hits
        if not query or _newsapi_remaining_daily_budget(cfg, now) <= 0:
            return
        if _newsapi_rate_limited_for_job(rate_limit_log_state):
            return
        cache_key = f"{route}|{query}|lookback={lookback_h}|page={page_size}|lang=en"
        cached = _NEWSAPI_QUERY_CACHE.get(cache_key)
        batch_meta: dict[str, Any] = {}
        batch_articles: list[dict[str, Any]] = []
        batch_http: int | None = None
        batch_request_sent = False
        elapsed_ms = 0.0

        if cached is not None and (now - cached.fetched_at).total_seconds() < cache_ttl:
            cache_hits += 1
            batch_articles = list(cached.articles)
            batch_http = cached.http_status
            batch_meta["skip_reason"] = cached.skip_reason
            batch_meta["error"] = cached.error
        else:
            _emit(
                "NEWS_FETCH_FALLBACK_START provider=NewsAPI route=%s endpoint=%s query=%s"
                % (route, "top-headlines" if top_headlines else "everything", query)
            )
            batch_start = time_module.monotonic()
            requests_made += 1
            _newsapi_consume_daily_budget(1, now)
            try:
                if top_headlines:
                    batch_articles = fetch_top_headlines_query(
                        query,
                        api_key,
                        page_size=min(100, max(page_size, 20)),
                        timeout_sec=per_request_timeout,
                        raise_on_rate_limit=True,
                        meta=batch_meta,
                    )
                else:
                    batch_articles = fetch_articles_query(
                        query,
                        api_key,
                        lookback_hours=lookback_h,
                        now=now,
                        page_size=min(100, max(page_size, 20)),
                        timeout_sec=per_request_timeout,
                        raise_on_rate_limit=True,
                        meta=batch_meta,
                    )
                batch_request_sent = bool(batch_meta.get("request_sent"))
            except NewsAPIRateLimitError as exc:
                batch_request_sent = True
                batch_http = 429
                batch_meta["http_status"] = 429
                batch_meta["skip_reason"] = "rate_limited"
                batch_meta["error"] = str(exc)
                request_sent = True
                http_status = 429
                _emit_newsapi_rate_limited_once(rate_limit_log_state, provider="newsapi")
                _NEWSAPI_QUERY_CACHE[cache_key] = _NewsapiBatchCacheEntry(
                    fetched_at=now,
                    articles=[],
                    http_status=429,
                    skip_reason="rate_limited",
                    error=str(exc)[:240],
                )
                _emit(
                    "NEWS_FETCH_RESULT provider=NewsAPI route=%s status_code=429 raw_article_count=0 article_count=0 elapsed_ms=%.1f"
                    % (route, (time_module.monotonic() - batch_start) * 1000.0)
                )
                return
            except Exception as exc:
                batch_articles = []
                batch_meta["error"] = str(exc)
                _emit("NEWS_FETCH_ERROR exception=%s" % redact_newsapi_secret(str(exc))[:240])
            elapsed_ms = (time_module.monotonic() - batch_start) * 1000.0
            batch_http = batch_meta.get("http_status")
            _NEWSAPI_QUERY_CACHE[cache_key] = _NewsapiBatchCacheEntry(
                fetched_at=now,
                articles=list(batch_articles),
                http_status=int(batch_http) if isinstance(batch_http, int) else (int(batch_http) if str(batch_http).isdigit() else None),
                skip_reason=str(batch_meta.get("skip_reason") or ""),
                error=str(batch_meta.get("error") or "")[:240],
            )

        request_sent = request_sent or batch_request_sent
        if batch_http is not None:
            http_status = int(batch_http)
        raw_count = len(batch_articles)
        _log_newsapi_window_diagnostics(
            route=route,
            articles=batch_articles,
            now=now,
            lookback_hours=lookback_h,
        )
        if not sample_titles:
            sample_titles = _sample_article_titles(batch_articles)
        for su in _symbols_from_newsapi_articles(batch_articles):
            if su not in returned_symbols:
                returned_symbols.append(su)
        raw_articles_before_filter += raw_count
        new_articles = _filter_new_articles(batch_articles, article_cache)
        dedupe_removed = max(0, raw_count - len(new_articles))
        batch_events = _newsapi_article_to_events(new_articles, ordered, cfg)
        filtered_events = [ev for ev in batch_events if ev.symbol]
        for ev in filtered_events:
            su = str(ev.symbol or "").strip().upper()
            if su and su not in returned_symbols:
                returned_symbols.append(su)
        no_symbol_match = max(0, len(batch_events) - len(filtered_events))
        articles_after_filter += len(filtered_events)
        reason_breakdown["dedupe_removed"] += dedupe_removed
        reason_breakdown["no_symbol_match"] += no_symbol_match
        events.extend(filtered_events)
        _emit(
            "NEWSAPI_RAW_ARTICLES provider=NewsAPI route=%s endpoint=%s raw_article_count=%d before_symbol_filter=%d"
            % (route, "top-headlines" if top_headlines else "everything", raw_count, raw_count)
        )
        _emit(
            "NEWS_FETCH_RESULT provider=NewsAPI route=%s status_code=%s raw_article_count=%d article_count=%d elapsed_ms=%.1f"
            % (route, str(batch_http) if batch_http is not None else "cache", raw_count, len(filtered_events), elapsed_ms)
        )
        _emit(
            "NEWS_FETCH_FILTER route=%s before_count=%d after_count=%d reason_breakdown=%s"
            % (
                route,
                raw_count,
                len(filtered_events),
                ",".join(f"{k}:{v}" for k, v in reason_breakdown.items()),
            )
        )
        if batch_request_sent:
            meta["request_sent"] = True
        if batch_http is not None:
            meta["http_status"] = batch_http
        if batch_meta.get("error"):
            meta["error"] = batch_meta.get("error")
        if isinstance(batch_meta.get("rate_limit_headers"), Mapping):
            rate_limit_headers.update({str(k): str(v) for k, v in batch_meta["rate_limit_headers"].items()})
        if batch_meta.get("skip_reason") and not meta.get("skip_reason"):
            meta["skip_reason"] = batch_meta.get("skip_reason")
        if batch_meta.get("query"):
            meta["query"] = str(batch_meta.get("query"))
        if batch_meta.get("from"):
            meta["from"] = str(batch_meta.get("from"))
        if batch_meta.get("to"):
            meta["to"] = str(batch_meta.get("to"))
        if batch_meta.get("endpoint"):
            meta["endpoint"] = str(batch_meta.get("endpoint"))

    if not events and not result.skip_reason and int(http_status or 0) != 429:
        _fetch_and_process_fallback(
            "broad_catalyst_everything",
            NEWSAPI_CATALYST_FALLBACK_QUERY,
            top_headlines=False,
        )
        if not events and symbol_search_raw_articles == 0:
            _fetch_and_process_fallback(
                "top_headlines",
                NEWSAPI_CATALYST_FALLBACK_QUERY,
                top_headlines=True,
            )

    result.request_sent = bool(request_sent)
    result.http_status = http_status
    result.error = str(meta.get("error") or "")
    if meta.get("skip_reason"):
        result.skip_reason = str(meta["skip_reason"])
    result.events = events
    result.articles = len(result.events)
    result.requests_made = requests_made
    result.raw_articles_before_filter = raw_articles_before_filter
    result.articles_after_filter = articles_after_filter
    result.returned_symbol_count = len(returned_symbols)
    result.sample_article_titles = sample_titles
    result.rate_limit_headers = rate_limit_headers
    result.duration_ms = (time_module.monotonic() - started) * 1000.0

    _news_provider_line("newsapi", request_symbol_count=result.request_symbol_count)
    _news_provider_line("newsapi", returned_symbol_count=result.returned_symbol_count)
    _news_provider_line("newsapi", sample_titles=_format_sample_titles(result.sample_article_titles))
    _news_provider_line("newsapi", rate_limit_headers=_format_rate_limit_headers(result.rate_limit_headers))
    _news_provider_line("newsapi", request_sent=_bool_text(result.request_sent))
    if result.http_status is not None:
        _news_provider_line("newsapi", http_status=int(result.http_status))
    if result.skip_reason:
        _news_provider_line("newsapi", reason=result.skip_reason)
    if result.error:
        _news_provider_line("newsapi", error=redact_newsapi_secret(result.error)[:240])
    _news_provider_line("newsapi", articles=result.articles)
    _news_provider_line("newsapi", raw_articles_before_filter=result.raw_articles_before_filter)
    _news_provider_line("newsapi", articles_after_filter=result.articles_after_filter)
    _news_provider_line(
        "newsapi",
        calls=result.requests_made,
        remaining_budget=_newsapi_remaining_daily_budget(cfg, now),
    )
    _news_provider_line("newsapi", duration_ms=f"{result.duration_ms:.1f}")
    if result.articles == 0 and result.request_sent:
        if meta.get("query"):
            _news_provider_line("newsapi", query=str(meta["query"])[:500])
        if meta.get("from"):
            _news_provider_line("newsapi", from_date=str(meta["from"]))
        if meta.get("to"):
            _news_provider_line("newsapi", to_date=str(meta["to"]))
        if meta.get("endpoint"):
            _news_provider_line("newsapi", endpoint=str(meta["endpoint"]))
    return result


def _provider_cache_get(cache_key: str, now: datetime | None) -> ProviderExecResult | None:
    entry = _PREMARKET_PROVIDER_CACHE.get(cache_key)
    if entry is None:
        return None
    when = now or datetime.now(timezone.utc)
    if (when - entry.fetched_at).total_seconds() > PREMARKET_EVENT_CACHE_TTL_SECONDS:
        return None
    return entry.result


def _provider_cache_set(cache_key: str, result: ProviderExecResult, now: datetime | None) -> None:
    when = now or datetime.now(timezone.utc)
    _PREMARKET_PROVIDER_CACHE[cache_key] = _ProviderEventCacheEntry(fetched_at=when, result=result)


def _load_feed_items_from_config(
    config: Mapping[str, Any] | None,
    *,
    key: str,
) -> list[Mapping[str, Any]]:
    pm = _pm_section(config)
    raw = pm.get(key)
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, Mapping)]
    return []


def _fetch_json_feed(url: str, timeout_seconds: float) -> list[Mapping[str, Any]]:
    if not url.strip():
        return []
    resp = requests.get(url, timeout=float(timeout_seconds))
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return [item for item in data if isinstance(item, Mapping)]
    if isinstance(data, Mapping):
        items = data.get("items") or data.get("data") or data.get("results") or []
        if isinstance(items, list):
            return [item for item in items if isinstance(item, Mapping)]
    return []


def _api_key_from_env(config: Mapping[str, Any] | None, key_name: str, default_env: str) -> str:
    import os

    pm = _pm_section(config)
    env_name = str(pm.get(key_name) or default_env).strip()
    return os.environ.get(env_name, "").strip() if env_name else ""


def _provider_feed_url(
    pm: Mapping[str, Any],
    *,
    provider: str,
    api_key: str,
    symbols: Sequence[str],
) -> str:
    explicit = str(pm.get(f"{provider}_feed_url") or "").strip()
    if explicit:
        return explicit
    joined_symbols = ",".join(str(sym or "").strip().upper() for sym in symbols if str(sym or "").strip())
    if provider == "finnhub" and api_key:
        return f"https://finnhub.io/api/v1/news?category=general&token={api_key}"
    if provider == "marketaux" and api_key:
        return f"https://api.marketaux.com/v1/news/all?symbols={joined_symbols}&language=en&api_token={api_key}"
    if provider == "fmp" and api_key:
        return f"https://financialmodelingprep.com/api/v3/stock_news?tickers={joined_symbols}&apikey={api_key}"
    return ""


def _external_news_provider_result(
    provider: str,
    symbols: Sequence[str],
    cfg: Mapping[str, Any] | None,
    timeout_seconds: float,
    *,
    now: datetime | None = None,
    enabled_key: str,
    events_key: str,
    api_key_env_key: str,
    default_api_key_env: str,
    source_name: str | None = None,
) -> ProviderExecResult:
    started = time_module.monotonic()
    result = ProviderExecResult(provider=provider)
    pm = _pm_section(cfg)
    result.enabled = _as_bool(pm.get(enabled_key), False)
    _news_provider_line(provider, enabled=_bool_text(result.enabled))
    if not result.enabled:
        result.skip_reason = f"{provider}_disabled"
        result.duration_ms = (time_module.monotonic() - started) * 1000.0
        _news_provider_line(provider, request_sent=_bool_text(False), reason=result.skip_reason)
        return result

    cache_key = _event_feed_cache_key(provider, symbols, cfg)
    cached = _provider_cache_get(cache_key, now)
    if cached is not None:
        _news_provider_line(provider, request_sent=_bool_text(False), reason="cache_hit")
        _news_provider_line(provider, articles=cached.articles)
        return cached

    feed_items = _load_feed_items_from_config(cfg, key=events_key)
    if not feed_items:
        api_key = _api_key_from_env(cfg, api_key_env_key, default_api_key_env)
        feed_url = _provider_feed_url(pm, provider=provider, api_key=api_key, symbols=symbols)
        if not feed_url:
            result.skip_reason = "missing_feed_or_api_key"
            result.duration_ms = (time_module.monotonic() - started) * 1000.0
            _news_provider_line(provider, request_sent=_bool_text(False), reason=result.skip_reason)
            _news_provider_line(provider, articles=0)
            return result
        try:
            feed_items = _fetch_json_feed(feed_url, timeout_seconds)
            result.request_sent = True
            result.http_status = 200
        except requests.HTTPError as exc:
            result.request_sent = True
            status = getattr(getattr(exc, "response", None), "status_code", None)
            result.http_status = int(status) if isinstance(status, int) else None
            result.skip_reason = "rate_limited" if result.http_status == 429 else f"HTTPError:{str(exc)[:200]}"
            result.error = result.skip_reason
            _news_provider_line(provider, request_sent=_bool_text(True), reason=result.skip_reason)
            if result.http_status is not None:
                _news_provider_line(provider, http_status=result.http_status)
            _news_provider_line(provider, articles=0)
            return result
        except Exception as exc:
            result.request_sent = True
            result.skip_reason = f"{exc.__class__.__name__}:{str(exc)[:200]}"
            result.error = result.skip_reason
            _news_provider_line(provider, request_sent=_bool_text(True), reason=result.skip_reason)
            _news_provider_line(provider, articles=0)
            return result

    events = _events_from_feed_items(feed_items, source=source_name or provider, symbols=symbols)
    result.request_sent = bool(result.request_sent or feed_items)
    result.events = events
    result.articles = len(events)
    result.raw_articles_before_filter = len(feed_items)
    result.articles_after_filter = len(events)
    result.duration_ms = (time_module.monotonic() - started) * 1000.0
    _news_provider_line(provider, request_sent=_bool_text(result.request_sent))
    _news_provider_line(provider, articles=result.articles)
    _news_provider_line(provider, raw_articles_before_filter=result.raw_articles_before_filter)
    _news_provider_line(provider, articles_after_filter=result.articles_after_filter)
    _news_provider_line(provider, duration_ms=f"{result.duration_ms:.1f}")
    _provider_cache_set(cache_key, result, now)
    return result


def _events_from_feed_items(
    items: Sequence[Mapping[str, Any]],
    *,
    source: str,
    symbols: Sequence[str],
) -> list[NewsEvent]:
    allowed = {str(sym or "").strip().upper() for sym in symbols if str(sym or "").strip()}
    events: list[NewsEvent] = []
    for raw in items:
        ev = _event_from_mapping(raw, source=source)
        if ev is None:
            continue
        if allowed and ev.symbol and ev.symbol not in allowed:
            continue
        if ev.score == 0.0:
            ev = NewsEvent(
                symbol=ev.symbol,
                headline=ev.headline,
                source=ev.source,
                publisher=ev.publisher,
                url=ev.url,
                published_at=ev.published_at,
                sentiment=ev.sentiment,
                score=max(0.0, _news_event_score(ev)),
            )
        events.append(ev)
    return events


def fetch_benzinga_events(
    symbols: Sequence[str],
    cfg: Mapping[str, Any] | None,
    timeout_seconds: float,
    *,
    now: datetime | None = None,
) -> ProviderExecResult:
    started = time_module.monotonic()
    result = ProviderExecResult(provider="benzinga")
    pm = _pm_section(cfg)
    result.enabled = _as_bool(pm.get("benzinga_enabled"), False)
    _news_provider_line("benzinga", enabled=_bool_text(result.enabled))
    if not result.enabled:
        result.skip_reason = "benzinga_disabled"
        result.duration_ms = (time_module.monotonic() - started) * 1000.0
        _news_provider_line("benzinga", request_sent=_bool_text(False), reason=result.skip_reason)
        return result

    cache_key = _event_feed_cache_key("benzinga", symbols, cfg)
    cached = _provider_cache_get(cache_key, now)
    if cached is not None:
        _news_provider_line("benzinga", request_sent=_bool_text(False), reason="cache_hit")
        _news_provider_line("benzinga", articles=cached.articles)
        return cached

    feed_items = _load_feed_items_from_config(cfg, key="benzinga_events")
    if not feed_items:
        feed_url = str(pm.get("benzinga_feed_url") or "").strip()
        if feed_url:
            try:
                feed_items = _fetch_json_feed(feed_url, timeout_seconds)
                result.request_sent = True
            except Exception as exc:
                result.request_sent = True
                result.skip_reason = f"{exc.__class__.__name__}:{str(exc)[:200]}"
                result.error = result.skip_reason
                _news_provider_line("benzinga", request_sent=_bool_text(True), reason=result.skip_reason)
                _news_provider_line("benzinga", articles=0)
                return result
    events = _events_from_feed_items(feed_items, source="benzinga", symbols=symbols)
    result.request_sent = bool(feed_items)
    result.events = events
    result.articles = len(events)
    result.duration_ms = (time_module.monotonic() - started) * 1000.0
    _news_provider_line("benzinga", request_sent=_bool_text(result.request_sent))
    _news_provider_line("benzinga", articles=result.articles)
    _news_provider_line("benzinga", duration_ms=f"{result.duration_ms:.1f}")
    _provider_cache_set(cache_key, result, now)
    return result


def fetch_finnhub_events(
    symbols: Sequence[str],
    cfg: Mapping[str, Any] | None,
    timeout_seconds: float,
    *,
    now: datetime | None = None,
) -> ProviderExecResult:
    return _external_news_provider_result(
        "finnhub",
        symbols,
        cfg,
        timeout_seconds,
        now=now,
        enabled_key="finnhub_enabled",
        events_key="finnhub_events",
        api_key_env_key="finnhub_api_key_env",
        default_api_key_env="FINNHUB_API_KEY",
    )


def fetch_marketaux_events(
    symbols: Sequence[str],
    cfg: Mapping[str, Any] | None,
    timeout_seconds: float,
    *,
    now: datetime | None = None,
) -> ProviderExecResult:
    pm = _pm_section(cfg)
    if _as_bool(pm.get("marketaux_enabled"), False):
        return _external_news_provider_result(
            "marketaux",
            symbols,
            cfg,
            timeout_seconds,
            now=now,
            enabled_key="marketaux_enabled",
            events_key="marketaux_events",
            api_key_env_key="marketaux_api_key_env",
            default_api_key_env="MARKETAUX_API_KEY",
        )
    return _external_news_provider_result(
        "fmp",
        symbols,
        cfg,
        timeout_seconds,
        now=now,
        enabled_key="fmp_enabled",
        events_key="fmp_events",
        api_key_env_key="fmp_api_key_env",
        default_api_key_env="FMP_API_KEY",
        source_name="fmp",
    )


def fetch_twitter_trusted_events(
    symbols: Sequence[str],
    cfg: Mapping[str, Any] | None,
    timeout_seconds: float,
    *,
    now: datetime | None = None,
) -> ProviderExecResult:
    started = time_module.monotonic()
    result = ProviderExecResult(provider="twitter")
    pm = _pm_section(cfg)
    result.enabled = _as_bool(pm.get("twitter_trusted_enabled"), False)
    _news_provider_line("twitter", enabled=_bool_text(result.enabled))
    if not result.enabled:
        result.skip_reason = "twitter_disabled"
        result.duration_ms = (time_module.monotonic() - started) * 1000.0
        _news_provider_line("twitter", request_sent=_bool_text(False), reason=result.skip_reason)
        return result

    cache_key = _event_feed_cache_key("twitter", symbols, cfg)
    cached = _provider_cache_get(cache_key, now)
    if cached is not None:
        _news_provider_line("twitter", request_sent=_bool_text(False), reason="cache_hit")
        _news_provider_line("twitter", posts=cached.articles, trusted_posts=cached.articles)
        return cached

    feed_items = _load_feed_items_from_config(cfg, key="twitter_trusted_events")
    if not feed_items:
        feed_url = str(pm.get("twitter_trusted_feed_url") or "").strip()
        if feed_url:
            try:
                feed_items = _fetch_json_feed(feed_url, timeout_seconds)
                result.request_sent = True
            except Exception as exc:
                result.request_sent = True
                result.skip_reason = f"{exc.__class__.__name__}:{str(exc)[:200]}"
                result.error = result.skip_reason
                _news_provider_line("twitter", request_sent=_bool_text(True), reason=result.skip_reason)
                _news_provider_line("twitter", posts=0, trusted_posts=0)
                return result
    events = _events_from_feed_items(feed_items, source="twitter", symbols=symbols)
    boosted_events: list[NewsEvent] = []
    for ev in events:
        boosted_events.append(
            NewsEvent(
                symbol=ev.symbol,
                headline=ev.headline,
                source=ev.source,
                publisher=ev.publisher,
                url=ev.url,
                published_at=ev.published_at,
                form=ev.form,
                accession=ev.accession,
                cik=ev.cik,
                primary_doc=ev.primary_doc,
                catalyst_type=ev.catalyst_type,
                rank_reason=ev.rank_reason,
                rank_source=ev.rank_source,
                sec_routine=ev.sec_routine,
                rankable=ev.rankable,
                sentiment=max(ev.sentiment, 0.25),
                score=max(ev.score, 0.5),
            )
        )
    events = boosted_events
    result.request_sent = bool(feed_items)
    result.events = events
    result.articles = len(events)
    result.duration_ms = (time_module.monotonic() - started) * 1000.0
    _news_provider_line("twitter", request_sent=_bool_text(result.request_sent))
    _news_provider_line("twitter", posts=len(feed_items), trusted_posts=result.articles)
    _news_provider_line("twitter", duration_ms=f"{result.duration_ms:.1f}")
    _provider_cache_set(cache_key, result, now)
    return result


def fetch_reddit_social_sentiment(
    symbols: Sequence[str],
    cfg: Mapping[str, Any] | None,
    timeout_seconds: float,
    *,
    now: datetime | None = None,
    project_root: Path | str = ".",
) -> ProviderExecResult:
    """Collect Reddit social sentiment diagnostics without producing trade events."""
    result = ProviderExecResult(provider="reddit")
    try:
        payload = collect_social_sentiment(
            symbols=symbols,
            config=cfg,
            project_root=project_root,
            hours=int((_pm_section(cfg).get("social") or {}).get("hours", 24))
            if isinstance((_pm_section(cfg).get("social") or {}), Mapping)
            else 24,
            limit=10,
            now=now,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        result.enabled = True
        result.request_sent = False
        result.skip_reason = f"{exc.__class__.__name__}:{str(exc)[:200]}"
        result.error = result.skip_reason
        return result
    providers = payload.get("providers") if isinstance(payload.get("providers"), Mapping) else {}
    reddit = providers.get("reddit") if isinstance(providers.get("reddit"), Mapping) else {}
    result.enabled = bool(reddit.get("enabled"))
    result.request_sent = bool(reddit.get("request_sent"))
    result.http_status = reddit.get("http_status") if isinstance(reddit.get("http_status"), int) else None
    result.raw_articles_before_filter = int(reddit.get("raw_count") or 0)
    result.articles_after_filter = int(reddit.get("filtered_count") or 0)
    result.articles = int(reddit.get("filtered_count") or 0)
    result.duration_ms = float(reddit.get("duration_ms") or 0.0)
    result.skip_reason = str(reddit.get("reason") or "")
    _news_provider_line("reddit", enabled=_bool_text(result.enabled))
    _news_provider_line("reddit", request_sent=_bool_text(result.request_sent), reason=result.skip_reason or "ok")
    if result.http_status is not None:
        _news_provider_line("reddit", http_status=int(result.http_status))
    _news_provider_line("reddit", raw_count=result.raw_articles_before_filter, filtered_count=result.articles_after_filter)
    return result


def _alpaca_raw_strings(item: Any, *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        raw: Any
        if isinstance(item, Mapping):
            raw = item.get(key)
        else:
            raw = getattr(item, key, None)
        if raw is None:
            continue
        if isinstance(raw, str):
            text = raw.strip()
            if text:
                values.append(text)
            continue
        if isinstance(raw, Mapping):
            text = str(raw.get("symbol") or raw.get("ticker") or raw.get("name") or "").strip()
            if text:
                values.append(text)
            continue
        if isinstance(raw, (list, tuple, set)):
            for entry in raw:
                if isinstance(entry, Mapping):
                    text = str(entry.get("symbol") or entry.get("ticker") or entry.get("name") or "").strip()
                else:
                    text = str(entry or "").strip()
                if text:
                    values.append(text)
            continue
        text = str(raw or "").strip()
        if text:
            values.append(text)
    return values


def _alpaca_article_field_values(item: Any) -> tuple[list[str], list[str], list[str]]:
    symbols = _alpaca_raw_strings(item, "symbols")
    tickers = _alpaca_raw_strings(item, "tickers")
    entities = _alpaca_raw_strings(item, "entities")
    return symbols, tickers, entities


def _alpaca_article_to_mapping(item: Any) -> dict[str, Any]:
    if isinstance(item, Mapping):
        return dict(item)
    if hasattr(item, "model_dump") and callable(getattr(item, "model_dump")):
        try:
            dumped = item.model_dump()
            if isinstance(dumped, Mapping):
                return dict(dumped)
        except Exception:
            pass
    if hasattr(item, "dict") and callable(getattr(item, "dict")):
        try:
            dumped = item.dict()
            if isinstance(dumped, Mapping):
                return dict(dumped)
        except Exception:
            pass
    if is_dataclass(item):
        try:
            dumped = asdict(item)
            if isinstance(dumped, Mapping):
                return dict(dumped)
        except Exception:
            pass
    if hasattr(item, "__dict__"):
        try:
            return dict(vars(item))
        except Exception:
            pass
    return {}


def _alpaca_match_requested_symbol(title: str, universe: Sequence[str]) -> str:
    text = str(title or "").upper()
    for sym in universe:
        su = str(sym or "").strip().upper()
        if not su:
            continue
        label, _used_name = _resolve_search_label(su, {})
        if re.search(rf"\b{re.escape(su)}\b", text):
            return su
        if label and re.search(rf"\b{re.escape(label.upper())}\b", text):
            return su
    return ""


def _alpaca_item_to_event(item: Any, universe: Sequence[str]) -> NewsEvent | None:
    data = _alpaca_article_to_mapping(item)
    headline = str(data.get("headline") or data.get("title") or getattr(item, "headline", None) or getattr(item, "title", "") or "").strip()
    created = data.get("created_at") or data.get("updated_at") or data.get("publishedAt") or getattr(item, "created_at", None) or getattr(item, "updated_at", None)
    url = str(data.get("url") or getattr(item, "url", "") or "").strip()
    publisher = str(data.get("source") or data.get("source_name") or data.get("provider") or "").strip()
    if not headline:
        return None
    symbols, tickers, entities = _alpaca_article_field_values(data if data else item)
    sym = ""
    for raw in (*symbols, *tickers, *entities):
        su = str(raw or "").strip().upper()
        if su:
            sym = su
            break
    if not sym:
        sym = _alpaca_match_requested_symbol(headline, universe)
    rank_source, confidence = _classify_catalyst_headline(headline=headline, catalyst_type="")
    _log_catalyst_classified(sym, headline, rank_source, confidence)
    score, _ctype = score_article_text(headline)
    return NewsEvent(
        symbol=sym,
        headline=headline,
        source="alpaca",
        publisher=publisher,
        url=url,
        published_at=str(created or "").strip(),
        rank_reason=rank_source,
        rank_source=rank_source,
        catalyst_type=rank_source,
        sentiment=max(-1.0, min(1.0, float(score) / 10.0)),
        score=float(score),
        gap_pct=_first_float(data, ("gap_pct", "premarket_gap_pct", "day_gain_pct", "gain_pct")),
        volume_surge_pct=_first_float(data, ("volume_surge_pct", "relative_volume_pct", "rel_volume_pct")),
    )


def _alpaca_symbols_from_raw(items: Sequence[Any]) -> list[str]:
    out: list[str] = []
    for item in items:
        symbols, tickers, entities = _alpaca_article_field_values(_alpaca_article_to_mapping(item) or item)
        for raw in (*symbols, *tickers, *entities):
            su = str(raw or "").strip().upper()
            if su and su not in out:
                out.append(su)
    return out


def _alpaca_response_news_items(resp: Any) -> list[Any]:
    def _response_keys(obj: Any) -> list[str]:
        keys: list[str] = []
        if isinstance(obj, Mapping):
            keys.extend(str(key) for key in obj.keys())
        elif isinstance(obj, tuple) and len(obj) == 2 and isinstance(obj[0], str):
            keys.append(str(obj[0]))
            if isinstance(obj[1], Mapping):
                keys.extend(str(key) for key in obj[1].keys())
        elif hasattr(obj, "__dict__"):
            try:
                keys.extend(str(key) for key in vars(obj).keys())
            except Exception:
                pass
        elif isinstance(obj, (tuple, list)):
            keys.extend(str(idx) for idx in range(len(obj)))
        return keys

    def _extract(obj: Any, *, depth: int = 0) -> list[Any]:
        if obj is None or depth > 4:
            return []
        if isinstance(obj, list):
            if obj and all(isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str) for item in obj):
                for key, val in obj:
                    if key in {"news", "data", "results"}:
                        nested = _extract(val, depth=depth + 1)
                        if nested:
                            return nested
                for _, val in obj:
                    nested = _extract(val, depth=depth + 1)
                    if nested:
                        return nested
                return []
            return list(obj)
        if isinstance(obj, tuple):
            if len(obj) == 2 and isinstance(obj[1], Mapping):
                return _extract(obj[1], depth=depth + 1)
            for item in obj:
                payload = _extract(item, depth=depth + 1)
                if payload:
                    return payload
            return list(obj)
        if isinstance(obj, Mapping):
            for key in ("news", "data", "results"):
                if key not in obj:
                    continue
                payload = obj.get(key)
                if key == "news" and payload is not None:
                    return _extract(payload, depth=depth + 1)
                if key in {"data", "results"} and payload is not None:
                    nested = _extract(payload, depth=depth + 1)
                    if nested:
                        return nested
            return []
        for attr in ("news", "data", "results"):
            if hasattr(obj, attr):
                payload = getattr(obj, attr)
                nested = _extract(payload, depth=depth + 1)
                if nested:
                    return nested
        return []

    _emit("ALPACA_RESPONSE_TYPE type=%s" % (type(resp).__name__ if resp is not None else "NoneType"))
    _emit("ALPACA_RESPONSE_KEYS keys=%s" % (",".join(_response_keys(resp)) or "none"))
    news = _extract(resp)
    _emit("ALPACA_NEWS_NORMALIZED_COUNT count=%d" % len(news))
    _emit("ALPACA_NEWS_COUNT count=%d" % len(news))
    return list(news)


def fetch_overnight_earnings_events(
    symbols: Sequence[str],
    cfg: Mapping[str, Any] | None,
    timeout_seconds: float,
    *,
    now: datetime | None = None,
    rate_limit_log_state: dict[str, bool] | None = None,
) -> ProviderExecResult:
    """Fetch overnight / pre-market earnings headlines via NewsAPI."""
    started = time_module.monotonic()
    result = ProviderExecResult(provider="earnings_overnight")
    pm = _pm_section(cfg)
    result.enabled = _as_bool(pm.get("overnight_earnings_enabled"), True)
    _news_provider_line("earnings_overnight", enabled=_bool_text(result.enabled))
    if not result.enabled:
        result.skip_reason = "overnight_earnings_disabled"
        result.duration_ms = (time_module.monotonic() - started) * 1000.0
        _news_provider_line("earnings_overnight", request_sent=_bool_text(False), reason=result.skip_reason)
        return result

    if not _premarket_newsapi_enabled(cfg):
        result.skip_reason = "depends_on_newsapi_disabled"
        result.duration_ms = (time_module.monotonic() - started) * 1000.0
        _news_provider_line("earnings_overnight", request_sent=_bool_text(False), reason=result.skip_reason)
        return result

    api_key = newsapi_key_from_config(dict(cfg or {}))
    if not api_key:
        result.skip_reason = "missing_api_key"
        result.duration_ms = (time_module.monotonic() - started) * 1000.0
        _news_provider_line("earnings_overnight", request_sent=_bool_text(False), reason=result.skip_reason)
        return result

    if _newsapi_rate_limited_for_job(rate_limit_log_state):
        result.skip_reason = "depends_on_newsapi_rate_limited"
        result.duration_ms = (time_module.monotonic() - started) * 1000.0
        _news_provider_line("earnings_overnight", request_sent=_bool_text(False), reason=result.skip_reason)
        return result

    cache_key = _event_feed_cache_key("earnings_overnight", symbols, cfg)
    cached = _provider_cache_get(cache_key, now)
    if cached is not None:
        _news_provider_line("earnings_overnight", request_sent=_bool_text(False), reason="cache_hit")
        _news_provider_line("earnings_overnight", articles=cached.articles)
        return cached

    uniq = _operating_company_symbols(symbols)[:5]
    result.request_symbol_count = len(uniq)
    if not uniq:
        result.skip_reason = "no_operating_companies_after_etf_filter"
        result.duration_ms = (time_module.monotonic() - started) * 1000.0
        _news_provider_line("earnings_overnight", request_symbol_count=result.request_symbol_count)
        _news_provider_line("earnings_overnight", request_sent=_bool_text(False), reason=result.skip_reason)
        return result

    if now is None:
        now = datetime.now(timezone.utc)
    ns = _news_sentiment_section(cfg)
    try:
        lookback_h = max(10, int(pm.get("overnight_earnings_lookback_hours", 14) or 14))
    except (TypeError, ValueError):
        lookback_h = 14

    per_symbol_timeout = max(1.0, float(timeout_seconds) / max(1, len(uniq)))
    article_cache: set[str] = set()
    tagged_articles: list[dict[str, Any]] = []
    returned_symbols: list[str] = []
    sample_titles: list[str] = []
    rate_limit_headers: dict[str, str] = {}
    meta: dict[str, Any] = {}
    batch_num = 0
    for sym in uniq:
        if _newsapi_remaining_daily_budget(cfg, now) <= 0:
            result.skip_reason = "newsapi_daily_budget_exhausted"
            break
        if _newsapi_rate_limited_for_job(rate_limit_log_state):
            result.skip_reason = "depends_on_newsapi_rate_limited"
            break
        batch_num += 1
        api_query, display_query, company_name_used = _build_symbol_search_query(sym, "earnings", cfg)
        _log_news_query(sym, display_query)
        batch_meta: dict[str, Any] = {}
        _newsapi_consume_daily_budget(1, now)
        try:
            batch = fetch_articles_query(
                api_query,
                api_key,
                lookback_hours=lookback_h,
                page_size=20,
                timeout_sec=per_symbol_timeout,
                raise_on_rate_limit=True,
                meta=batch_meta,
            )
            batch_request_sent = bool(batch_meta.get("request_sent"))
        except NewsAPIRateLimitError as exc:
            batch = []
            batch_meta["request_sent"] = True
            batch_meta["http_status"] = 429
            batch_meta["skip_reason"] = "rate_limited"
            batch_meta["error"] = str(exc)
            meta["request_sent"] = True
            meta["http_status"] = 429
            meta["skip_reason"] = "rate_limited"
            meta["error"] = str(exc)
            if isinstance(batch_meta.get("rate_limit_headers"), Mapping):
                rate_limit_headers.update({str(k): str(v) for k, v in batch_meta["rate_limit_headers"].items()})
            _emit_newsapi_rate_limited_once(
                rate_limit_log_state,
                provider="earnings_overnight",
            )
            batch_request_sent = True
            batch_http = 429
            new_articles = []
            log_fields = {
                "batch": batch_num,
                "symbols": sym,
                "query_length": len(api_query),
                "articles": 0,
                "company_name_used": _bool_text(company_name_used),
                "http_status": 429,
                "error": "rate_limited",
                "rate_limit_headers": _format_rate_limit_headers(rate_limit_headers),
            }
            _news_provider_line("earnings_overnight", **log_fields)
            break
        except Exception as exc:
            batch = []
            batch_meta.setdefault("error", str(exc))
        batch_http = batch_meta.get("http_status")
        if batch_http == 429:
            meta["request_sent"] = True
            meta["http_status"] = 429
            meta["skip_reason"] = "rate_limited"
            if batch_meta.get("error"):
                meta["error"] = batch_meta.get("error")
            if isinstance(batch_meta.get("rate_limit_headers"), Mapping):
                rate_limit_headers.update({str(k): str(v) for k, v in batch_meta["rate_limit_headers"].items()})
            _emit_newsapi_rate_limited_once(
                rate_limit_log_state,
                provider="earnings_overnight",
            )
            _news_provider_line(
                "earnings_overnight",
                batch=batch_num,
                symbols=sym,
                query_length=len(api_query),
                articles=0,
                company_name_used=_bool_text(company_name_used),
                http_status=429,
                error="rate_limited",
                rate_limit_headers=_format_rate_limit_headers(rate_limit_headers),
            )
            break
        new_articles = _filter_new_articles(batch, article_cache)
        if not sample_titles:
            sample_titles = _sample_article_titles(new_articles)
        if new_articles and sym not in returned_symbols:
            returned_symbols.append(sym)
        for su in _symbols_from_newsapi_articles(new_articles):
            if su not in returned_symbols:
                returned_symbols.append(su)
        for art in new_articles:
            tagged = dict(art)
            tagged["_premarket_rank_source"] = "earnings"
            tagged["_premarket_symbol"] = sym
            tagged_articles.append(tagged)
        if batch_meta.get("request_sent"):
            meta["request_sent"] = True
        if batch_http is not None:
            meta["http_status"] = batch_http
        if batch_meta.get("error"):
            meta["error"] = batch_meta.get("error")
        if isinstance(batch_meta.get("rate_limit_headers"), Mapping):
            rate_limit_headers.update({str(k): str(v) for k, v in batch_meta["rate_limit_headers"].items()})
        if batch_meta.get("query"):
            meta["query"] = str(batch_meta.get("query"))
        log_fields: dict[str, Any] = {
            "batch": batch_num,
            "symbols": sym,
            "query_length": len(api_query),
            "articles": len(new_articles),
            "company_name_used": _bool_text(company_name_used),
        }
        if batch_http == 400:
            log_fields["http_status"] = 400
            log_fields["error"] = "bad_request"
        elif batch_http is not None:
            log_fields["http_status"] = int(batch_http)
        else:
            log_fields["http_status"] = "none"
        _news_provider_line("earnings_overnight", **log_fields)
    events: list[NewsEvent] = []
    for ev in _newsapi_article_to_events(tagged_articles, uniq, cfg):
        if not ev.symbol:
            continue
        events.append(
            NewsEvent(
                symbol=ev.symbol,
                headline=ev.headline,
                source="earnings_overnight",
                publisher=ev.publisher,
                url=ev.url,
                published_at=ev.published_at,
                catalyst_type="earnings",
                rank_reason="earnings",
                rank_source="earnings",
                sentiment=max(-1.0, min(1.0, float(getattr(ev, "score", 2.5) or 2.5) / 10.0)),
                score=float(getattr(ev, "score", 2.5) or 2.5),
            )
        )
    result.request_sent = bool(meta.get("request_sent"))
    result.http_status = meta.get("http_status")
    result.events = events
    result.articles = len(events)
    result.requests_made = batch_num
    result.raw_articles_before_filter = len(tagged_articles)
    result.articles_after_filter = len(events)
    result.returned_symbol_count = len(returned_symbols)
    result.sample_article_titles = sample_titles
    result.rate_limit_headers = rate_limit_headers
    result.duration_ms = (time_module.monotonic() - started) * 1000.0
    _news_provider_line("earnings_overnight", request_symbol_count=result.request_symbol_count)
    _news_provider_line("earnings_overnight", returned_symbol_count=result.returned_symbol_count)
    _news_provider_line("earnings_overnight", sample_titles=_format_sample_titles(result.sample_article_titles))
    _news_provider_line("earnings_overnight", rate_limit_headers=_format_rate_limit_headers(result.rate_limit_headers))
    _news_provider_line("earnings_overnight", request_sent=_bool_text(result.request_sent))
    if result.http_status is not None:
        _news_provider_line("earnings_overnight", http_status=int(result.http_status))
    _news_provider_line("earnings_overnight", articles=result.articles)
    _news_provider_line("earnings_overnight", duration_ms=f"{result.duration_ms:.1f}")
    if meta.get("query"):
        _news_provider_line("earnings_overnight", query=str(meta["query"])[:500])
    _news_provider_line(
        "earnings_overnight",
        calls=result.requests_made,
        articles=result.articles,
        status=(
            "rate_limited"
            if int(result.http_status or 0) == 429
            else ("budget_exhausted" if result.skip_reason == "newsapi_daily_budget_exhausted" else ("cache_hit" if not result.request_sent and result.articles > 0 else "ok"))
        ),
    )
    _provider_cache_set(cache_key, result, now)
    return result


def fetch_alpaca_news_events(
    symbols: Sequence[str],
    cfg: Mapping[str, Any] | None,
    timeout_seconds: float,
    *,
    market_client: Any = None,
    now: datetime | None = None,
) -> ProviderExecResult:
    """Fetch Alpaca news using live credentials with paper fallback."""
    started = time_module.monotonic()
    result = ProviderExecResult(provider="alpaca")
    pm = _pm_section(cfg)
    result.enabled = _as_bool(pm.get("alpaca_news_enabled"), True)
    _news_provider_line("alpaca", enabled=_bool_text(result.enabled))
    if not result.enabled:
        result.skip_reason = "alpaca_news_disabled_in_config"
        result.duration_ms = (time_module.monotonic() - started) * 1000.0
        _news_provider_line("alpaca", request_sent=_bool_text(False), reason=result.skip_reason)
        _news_provider_line("alpaca", duration_ms=f"{result.duration_ms:.1f}")
        return result

    paper = getattr(market_client, "paper", None)
    creds = resolve_alpaca_credentials(cfg, paper=paper, paper_fallback_on_live=True)
    _provider_creds_line(
        "alpaca",
        mode=creds.mode,
        selected=creds.selected,
        live_key_present=_bool_text(creds.live_key_present),
        paper_key_present=_bool_text(creds.paper_key_present),
    )
    has_broker_news = (
        market_client is not None
        and getattr(market_client, "_news", None) is not None
        and callable(getattr(market_client, "get_recent_news", None))
    )
    credentials_present = creds.credentials_present or has_broker_news
    _news_provider_line("alpaca", credentials_present=_bool_text(credentials_present))
    if not credentials_present:
        result.skip_reason = "missing_api_key"
        result.duration_ms = (time_module.monotonic() - started) * 1000.0
        _news_provider_line("alpaca", request_sent=_bool_text(False), reason=result.skip_reason)
        _news_provider_line("alpaca", duration_ms=f"{result.duration_ms:.1f}")
        return result

    cache_key = _event_feed_cache_key("alpaca", symbols, cfg)
    cached = _provider_cache_get(cache_key, now)
    if cached is not None:
        _news_provider_line("alpaca", request_sent=_bool_text(False), reason="cache_hit")
        _news_provider_line("alpaca", articles=cached.articles)
        return cached

    uniq = _operating_company_symbols(symbols)
    result.request_symbol_count = len(uniq)
    if not uniq:
        result.skip_reason = "no_operating_companies_after_etf_filter"
        result.duration_ms = (time_module.monotonic() - started) * 1000.0
        _news_provider_line("alpaca", request_symbol_count=result.request_symbol_count)
        _news_provider_line("alpaca", request_sent=_bool_text(False), reason=result.skip_reason)
        _news_provider_line("alpaca", duration_ms=f"{result.duration_ms:.1f}")
        return result

    if now is None:
        now = datetime.now(timezone.utc)
    ns = _news_sentiment_section(cfg)
    try:
        lookback_h = int(ns.get("headline_lookback_hours", 24) or 24)
    except (TypeError, ValueError):
        lookback_h = 24
    start = now - timedelta(hours=max(1, lookback_h))
    limit = min(100, max(50, len(uniq) * 3))
    _ = timeout_seconds
    endpoint = "alpaca.news.get_news"
    request_params = {
        "symbols": ",".join(uniq),
        "start": start.isoformat(),
        "end": now.isoformat(),
        "limit": int(limit),
        "include_content": False,
        "exclude_contentless": False,
    }
    _news_provider_line("alpaca", endpoint=endpoint)
    _news_provider_line(
        "alpaca",
        request_params=" ".join(f"{key}={value}" for key, value in request_params.items()),
    )

    raw_articles: list[Any] = []
    http_status: int | None = None
    try:
        if has_broker_news:
            endpoint = "broker.get_recent_news"
            _news_provider_line("alpaca", endpoint=endpoint)
            raw_articles = _alpaca_response_news_items(
                market_client.get_recent_news(
                    uniq,
                    start=start,
                    end=now,
                    limit=limit,
                    exclude_contentless=False,
                )
            )
        else:
            endpoint = "fetch_alpaca_news_with_credentials"
            _news_provider_line("alpaca", endpoint=endpoint)
            raw_articles = fetch_alpaca_news_with_credentials(
                creds.api_key,
                creds.secret,
                uniq,
                start=start,
                end=now,
                limit=limit,
                exclude_contentless=False,
            )
        result.request_sent = True
        http_status = 200
    except Exception as exc:
        result.request_sent = True
        result.error = f"{exc.__class__.__name__}:{str(exc)[:200]}"
        result.skip_reason = result.error
        http_status = 500
        raw_articles = []

    events: list[NewsEvent] = []
    returned_symbols = _alpaca_symbols_from_raw(raw_articles)
    sample_titles = _sample_article_titles(raw_articles)
    for idx, item in enumerate(raw_articles):
        if idx < 2:
            raw_dict = _alpaca_article_to_mapping(item)
            raw_keys = sorted(list(raw_dict.keys())) if raw_dict else sorted(list(getattr(item, "__dict__", {}).keys()))
            symbols_field, tickers_field, entities_field = _alpaca_article_field_values(raw_dict if raw_dict else item)
            title_value = str(raw_dict.get("headline") or raw_dict.get("title") or getattr(item, "headline", None) or getattr(item, "title", "") or "").strip()
            _emit("ALPACA_ARTICLE_TYPE type=%s" % type(item).__name__)
            _emit("ALPACA_ARTICLE_REPR repr=%s" % repr(item)[:500])
            _emit("ALPACA_ARTICLE_DICT_KEYS keys=%s" % ",".join(raw_keys[:50]) if raw_keys else "ALPACA_ARTICLE_DICT_KEYS keys=none")
            _emit("ALPACA_NEWS_RAW_KEYS keys=%s" % ",".join(raw_keys[:20]))
            _emit(
                "ALPACA_NEWS_RAW title=%s symbols=%s tickers=%s entities=%s"
                % (
                    title_value[:200],
                    ",".join(symbols_field) if symbols_field else "none",
                    ",".join(tickers_field) if tickers_field else "none",
                    ",".join(entities_field) if entities_field else "none",
                )
            )
        ev = _alpaca_item_to_event(item, uniq)
        if ev is not None:
            events.append(ev)
    sample_symbols = []
    for ev in events:
        su = str(ev.symbol or "").strip().upper()
        if su and su not in sample_symbols:
            sample_symbols.append(su)
        if len(sample_symbols) >= 5:
            break
    raw_count = len(raw_articles)
    filtered_count = len(events)
    result.events = events
    result.articles = filtered_count
    result.raw_articles_before_filter = raw_count
    result.articles_after_filter = filtered_count
    result.http_status = http_status
    result.returned_symbol_count = len(returned_symbols)
    result.sample_article_titles = sample_titles
    result.duration_ms = (time_module.monotonic() - started) * 1000.0
    _news_provider_line("alpaca", request_symbol_count=result.request_symbol_count)
    _news_provider_line("alpaca", returned_symbol_count=result.returned_symbol_count)
    _news_provider_line("alpaca", sample_titles=_format_sample_titles(result.sample_article_titles))
    _news_provider_line("alpaca", request_sent=_bool_text(result.request_sent))
    _news_provider_line("alpaca", http_status=http_status if http_status is not None else "none")
    _news_provider_line("alpaca", raw_articles=raw_count)
    _news_provider_line("alpaca", filtered_articles=filtered_count)
    _news_provider_line("alpaca", sample_symbols=",".join(sample_symbols) if sample_symbols else "none")
    if result.skip_reason:
        _news_provider_line("alpaca", reason=result.skip_reason)
    _news_provider_line("alpaca", articles=result.articles)
    _news_provider_line("alpaca", duration_ms=f"{result.duration_ms:.1f}")
    _emit(
        "ALPACA_NEWS_RESULT status_code=%s raw_articles=%d filtered_articles=%d"
        % (http_status if http_status is not None else "none", raw_count, filtered_count)
    )
    _provider_cache_set(cache_key, result, now)
    return result


def _sec_headers(cfg: Mapping[str, Any] | None) -> dict[str, str]:
    pm = _pm_section(cfg)
    ua = str(pm.get("sec_user_agent") or SEC_USER_AGENT).strip() or SEC_USER_AGENT
    return {"User-Agent": ua, "Accept": "application/json"}


def fetch_sec_filings(
    symbols: Sequence[str],
    cfg: Mapping[str, Any] | None,
    timeout_seconds: float,
    *,
    now: datetime | None = None,
) -> ProviderExecResult:
    """Map symbols to CIK via SEC and count recent submissions."""
    started = time_module.monotonic()
    result = ProviderExecResult(provider="sec")
    pm = _pm_section(cfg)
    result.enabled = _as_bool(pm.get("sec_filings_enabled"), True)
    _sec_provider_line(enabled=_bool_text(result.enabled))
    if not result.enabled:
        result.skip_reason = "sec_filings_disabled_in_config"
        result.duration_ms = (time_module.monotonic() - started) * 1000.0
        _sec_provider_line(request_sent=_bool_text(False), reason=result.skip_reason)
        _sec_provider_line(cik_mapped=0, missing=0, filings=0)
        _sec_provider_line(duration_ms=f"{result.duration_ms:.1f}")
        return result

    if now is None:
        now = datetime.now(timezone.utc)
    uniq = [str(s).strip().upper() for s in symbols if str(s).strip()]
    uniq = list(dict.fromkeys(uniq))
    if not uniq:
        result.skip_reason = "empty_symbol_universe"
        result.duration_ms = (time_module.monotonic() - started) * 1000.0
        _sec_provider_line(request_sent=_bool_text(False), reason=result.skip_reason)
        _sec_provider_line(cik_mapped=0, missing=0, filings=0)
        _sec_provider_line(duration_ms=f"{result.duration_ms:.1f}")
        return result

    cache_key = _event_feed_cache_key("sec", uniq, cfg)
    cached = _provider_cache_get(cache_key, now)
    if cached is not None:
        _sec_provider_line(request_sent=_bool_text(False))
        _sec_provider_line(reason="cache_hit")
        _sec_provider_line(cik_mapped=cached.cik_mapped, missing=cached.cik_missing, filings=cached.filings)
        return cached

    symbol_cik: dict[str, str] = {}
    manual = pm.get("symbol_cik")
    if isinstance(manual, Mapping):
        for sym in uniq:
            raw = manual.get(sym) or manual.get(str(sym).upper())
            if raw is not None and str(raw).strip():
                symbol_cik[sym] = str(int(str(raw).strip())).zfill(10)

    http_status: int | None = None
    try:
        resp = requests.get(
            SEC_COMPANY_TICKERS_URL,
            headers=_sec_headers(cfg),
            timeout=float(timeout_seconds),
        )
        http_status = int(resp.status_code)
        resp.raise_for_status()
        result.request_sent = True
        payload = resp.json()
        ticker_to_cik: dict[str, str] = {}
        if isinstance(payload, Mapping):
            for row in payload.values():
                if not isinstance(row, Mapping):
                    continue
                ticker = str(row.get("ticker") or "").strip().upper()
                cik_raw = row.get("cik_str")
                if ticker and cik_raw is not None:
                    ticker_to_cik[ticker] = str(int(cik_raw)).zfill(10)
        for sym in uniq:
            if sym not in symbol_cik and sym in ticker_to_cik:
                symbol_cik[sym] = ticker_to_cik[sym]
    except Exception as exc:
        result.request_sent = True
        result.error = f"{exc.__class__.__name__}:{str(exc)[:200]}"
        result.skip_reason = result.error

    result.cik_mapped = sum(1 for sym in uniq if sym in symbol_cik)
    result.cik_missing = max(0, len(uniq) - result.cik_mapped)
    try:
        max_symbols = int(pm.get("sec_max_symbols_per_cycle", 12) or 12)
    except (TypeError, ValueError):
        max_symbols = 12
    ns = _news_sentiment_section(cfg)
    try:
        lookback_h = int(ns.get("headline_lookback_hours", 24) or 24)
    except (TypeError, ValueError):
        lookback_h = 24
    cutoff = (now - timedelta(hours=max(1, lookback_h))).date()
    per_symbol_timeout = max(1.0, float(timeout_seconds) / max(1, min(max_symbols, len(uniq))))

    events: list[NewsEvent] = []
    filings = 0
    for sym in [s for s in uniq if s in symbol_cik][:max_symbols]:
        cik = symbol_cik[sym]
        try:
            sub_resp = requests.get(
                SEC_SUBMISSIONS_URL.format(cik=cik),
                headers=_sec_headers(cfg),
                timeout=per_symbol_timeout,
            )
            sub_resp.raise_for_status()
            data = sub_resp.json()
            recent = (data.get("filings") or {}).get("recent") if isinstance(data.get("filings"), Mapping) else None
            if isinstance(recent, Mapping):
                forms = recent.get("form") or []
                dates = recent.get("filingDate") or recent.get("reportDate") or []
                accessions = recent.get("accessionNumber") or []
                primary_docs = recent.get("primaryDocument") or []
                for idx, raw_date in enumerate(dates):
                    text = str(raw_date or "").strip()
                    if not text:
                        continue
                    try:
                        filed = datetime.strptime(text[:10], "%Y-%m-%d").date()
                    except ValueError:
                        continue
                    if filed < cutoff:
                        continue
                    filings += 1
                    form = str(forms[idx] if idx < len(forms) else "").strip()
                    accession = str(accessions[idx] if idx < len(accessions) else "").strip()
                    primary_doc = str(primary_docs[idx] if idx < len(primary_docs) else "").strip()
                    filing_url = _sec_filing_url(cik, accession, primary_doc)
                    classification = classify_sec_filing(form, cfg)
                    ev = NewsEvent(
                        symbol=sym,
                        headline=f"SEC filing {form or 'unknown'}",
                        source="sec",
                        form=form,
                        accession=accession,
                        cik=cik,
                        primary_doc=primary_doc,
                        url=filing_url,
                        published_at=text,
                        catalyst_type=classification.catalyst_type,
                        rank_reason="sec_filing",
                        sec_routine=classification.routine,
                        rankable=classification.rankable and classification.score > 0,
                        sentiment=max(-1.0, min(1.0, float(classification.score) / 10.0)),
                        score=float(classification.score),
                    )
                    _log_sec_filing(ev)
                    events.append(ev)
        except Exception:
            continue

    result.events = events
    result.filings = filings
    result.http_status = http_status
    result.duration_ms = (time_module.monotonic() - started) * 1000.0
    _sec_provider_line(request_sent=_bool_text(result.request_sent))
    if http_status is not None:
        _sec_provider_line(http_status=int(http_status))
    if result.skip_reason:
        _sec_provider_line(reason=result.skip_reason)
    if result.error:
        _sec_provider_line(error=result.error[:240])
    _sec_provider_line(cik_mapped=result.cik_mapped, missing=result.cik_missing, filings=result.filings)
    _sec_provider_line(duration_ms=f"{result.duration_ms:.1f}")
    _provider_cache_set(cache_key, result, now)
    return result


def merge_premarket_events(events: Sequence[NewsEvent]) -> list[NewsEvent]:
    """Dedupe provider events by symbol + headline/url/form/accession."""
    out: list[NewsEvent] = []
    seen: set[str] = set()
    for ev in events:
        if ev.accession:
            key = f"sec:{ev.accession}"
        elif ev.url:
            key = f"{ev.symbol}:{ev.url}"
        elif ev.form:
            key = f"{ev.symbol}:{ev.form}:{ev.published_at}"
        else:
            key = f"{ev.symbol}:{ev.headline.casefold()}"
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out


def _event_to_article(ev: NewsEvent) -> dict[str, Any]:
    symbols = [ev.symbol] if ev.symbol else []
    return {
        "title": ev.headline,
        "description": "",
        "symbols": symbols,
        "publishedAt": ev.published_at,
        "url": ev.url,
        "source": ev.source,
        "publisher": ev.publisher,
    }


def score_premarket_catalysts(
    symbols: Sequence[str],
    events: Sequence[NewsEvent],
) -> dict[str, NewsCatalyst]:
    articles = [_event_to_article(ev) for ev in events if ev.source in {"newsapi", "alpaca", "benzinga", "earnings_overnight"}]
    out: dict[str, NewsCatalyst] = {}
    for sym in symbols:
        su = str(sym or "").strip().upper()
        if not su:
            continue
        cat = _best_catalyst_for_symbol(su, articles)
        if cat is not None:
            out[su] = cat
    return out


def _sec_event_catalysts(
    events: Sequence[NewsEvent],
    cfg: Mapping[str, Any] | None,
) -> dict[str, NewsCatalyst]:
    out: dict[str, NewsCatalyst] = {}
    for ev in events:
        su = str(ev.symbol or "").strip().upper()
        if not su or str(ev.source or "").strip().lower() != "sec" or not ev.rankable:
            continue
        classification = classify_sec_filing(ev.form, cfg)
        if not classification.rankable or classification.score <= 0:
            continue
        score = float(ev.score or classification.score)
        prev = out.get(su)
        if prev is not None and float(prev.score or 0.0) >= score:
            continue
        out[su] = NewsCatalyst(
            symbol=su,
            score=score,
            headline=str(ev.headline or f"SEC filing {ev.form or ''}").strip(),
            article_symbols=(su,),
            source="sec",
            catalyst_type=classification.catalyst_type,
            article_count=1,
            sentiment=0.0,
        )
    return out


def execute_premarket_providers(
    config: Mapping[str, Any] | None,
    symbols: Sequence[str],
    *,
    market_client: Any = None,
    now: datetime | None = None,
    project_root: Path | str = ".",
) -> PremarketProviderResults:
    """Run premarket providers in priority order; merge and score catalysts."""
    if now is None:
        now = datetime.now(timezone.utc)
    newsapi_rate_limit_log_state: dict[str, bool] = {}
    uniq_symbols = _premarket_candidate_universe(
        config,
        market_client=market_client,
        base_symbols=symbols,
    )
    preferred_sample = ["AAPL", "NVDA", "AMZN", "MSFT", "GOOGL"]
    sample_symbols = [sym for sym in preferred_sample if sym in uniq_symbols]
    for sym in uniq_symbols:
        if len(sample_symbols) >= 5:
            break
        if sym not in sample_symbols:
            sample_symbols.append(sym)
    sample = ",".join(sample_symbols)
    _emit(
        "PREMARKET_UNIVERSE base_symbols=%d candidate_symbols=%d sample=%s"
        % (len(symbols), len(uniq_symbols), sample)
    )

    alpaca = fetch_alpaca_news_events(
        uniq_symbols,
        config,
        _timeout_seconds(config, "alpaca_news_timeout_seconds", 10.0),
        market_client=market_client,
        now=now,
    )
    _emit("NEWS_PIPELINE source=alpaca articles=%d" % int(alpaca.articles))

    sec = fetch_sec_filings(
        uniq_symbols,
        config,
        _timeout_seconds(config, "sec_timeout_seconds", 10.0),
        now=now,
    )
    _emit("NEWS_PIPELINE source=sec filings=%d cik_mapped=%d" % (int(sec.filings), int(sec.cik_mapped)))

    benzinga = fetch_benzinga_events(
        uniq_symbols,
        config,
        _timeout_seconds(config, "news_timeout_seconds", 10.0),
        now=now,
    )
    _emit("NEWS_PIPELINE source=benzinga articles=%d" % int(benzinga.articles))

    twitter = fetch_twitter_trusted_events(
        uniq_symbols,
        config,
        _timeout_seconds(config, "news_timeout_seconds", 10.0),
        now=now,
    )
    _emit("NEWS_PIPELINE source=twitter posts=%d trusted_posts=%d" % (int(twitter.articles), int(twitter.articles)))

    reddit = fetch_reddit_social_sentiment(
        uniq_symbols,
        config,
        _timeout_seconds(config, "news_timeout_seconds", 10.0),
        now=now,
        project_root=project_root,
    )
    _emit(
        "NEWS_PIPELINE source=reddit mentions=%d"
        % int(reddit.articles_after_filter or reddit.articles or 0)
    )

    newsapi_fallback_symbols = _operating_company_symbols(uniq_symbols)[: _newsapi_fallback_top_n(config)]
    newsapi = fetch_newsapi_articles(
        newsapi_fallback_symbols,
        config,
        _timeout_seconds(config, "news_timeout_seconds", 10.0),
        now=now,
        rate_limit_log_state=newsapi_rate_limit_log_state,
    )
    _emit(
        "NEWS_PIPELINE source=newsapi calls=%d remaining_budget=%d"
        % (int(newsapi.requests_made), _newsapi_remaining_daily_budget(config, now))
    )

    finnhub = fetch_finnhub_events(
        uniq_symbols,
        config,
        _timeout_seconds(config, "news_timeout_seconds", 10.0),
        now=now,
    )
    _emit("NEWS_PIPELINE source=finnhub articles=%d" % int(finnhub.articles))

    marketaux = fetch_marketaux_events(
        uniq_symbols,
        config,
        _timeout_seconds(config, "news_timeout_seconds", 10.0),
        now=now,
    )
    _emit("NEWS_PIPELINE source=%s articles=%d" % (marketaux.provider, int(marketaux.articles)))

    if int(newsapi.http_status or 0) == 429 or newsapi.skip_reason == "rate_limited":
        overnight = ProviderExecResult(
            provider="earnings_overnight",
            request_sent=False,
            skip_reason="depends_on_newsapi_rate_limited",
            http_status=429,
        )
        _news_provider_line(
            "earnings_overnight",
            request_sent=_bool_text(False),
            reason=overnight.skip_reason,
        )
    else:
        overnight = fetch_overnight_earnings_events(
            uniq_symbols,
            config,
            _timeout_seconds(config, "news_timeout_seconds", 10.0),
            now=now,
            rate_limit_log_state=newsapi_rate_limit_log_state,
        )
    _emit(
        "NEWS_PIPELINE source=earnings_overnight calls=%d articles=%d status=%s"
        % (
            int(getattr(overnight, "requests_made", 0) or 0),
            int(overnight.articles),
            (
                "rate_limited"
                if int(getattr(overnight, "http_status", 0) or 0) == 429
                else ("budget_exhausted" if overnight.skip_reason == "newsapi_daily_budget_exhausted" else ("cache_hit" if not overnight.request_sent and overnight.articles > 0 else "ok"))
            ),
        )
        )
    merged = merge_premarket_events(
        alpaca.events
        + sec.events
        + benzinga.events
        + newsapi.events
        + finnhub.events
        + marketaux.events
        + twitter.events
        + overnight.events
    )
    source_counts: dict[str, int] = {}
    for ev in merged:
        source = str(ev.source or "unknown").strip().lower() or "unknown"
        source_counts[source] = source_counts.get(source, 0) + 1
    _emit(
        "PREMARKET_SOURCE_COUNTS "
        + (
            " ".join(
                "%s=%d" % (source, count)
                for source, count in sorted(source_counts.items())
            )
            if source_counts
            else "none=0"
        )
    )
    article_counts: dict[str, int] = {}
    for ev in merged:
        su = str(ev.symbol or "").strip().upper()
        if su:
            article_counts[su] = article_counts.get(su, 0) + 1
        _event_feed_line(ev)
    for sym in uniq_symbols:
        count = int(article_counts.get(sym, 0))
        if count > 0:
            _emit(f"PREMARKET_NEWS_HIT symbol={sym} articles={count}")
    catalysts = score_premarket_catalysts(uniq_symbols, merged) if merged else {}
    if merged:
        for sym, cat in _sec_event_catalysts(merged, config).items():
            catalysts.setdefault(sym, cat)
    for sym, cat in catalysts.items():
        _emit(
            "PREMARKET_CATALYST_WRITTEN symbol=%s score=%.2f source=%s"
            % (
                str(sym or "").strip().upper(),
                float(getattr(cat, "score", 0.0) or 0.0),
                str(getattr(cat, "source", "") or "unknown"),
            )
        )
    covered_symbols = {str(sym or "").strip().upper() for sym in catalysts.keys()}
    for sym in uniq_symbols:
        if sym not in covered_symbols:
            _emit(
                "PREMARKET_MOVER_COVERAGE_MISS symbol=%s reason=no_news_or_not_queried"
                % sym
            )
    rankings = (
        build_premarket_rankings(uniq_symbols, catalysts=catalysts, events=merged, cfg=config, now=now)
        if merged
        else []
    )
    _emit(
        "PREMARKET_RANKED_SYMBOLS count=%d symbols=%s"
        % (
            len({str(row.symbol or "").strip().upper() for row in rankings if str(row.symbol or "").strip()}),
            ",".join(sorted({str(row.symbol or "").strip().upper() for row in rankings if str(row.symbol or "").strip()})) or "none",
        )
    )
    _emit(
        "PREMARKET_CATALYST_SYMBOLS count=%d symbols=%s"
        % (
            len(covered_symbols),
            ",".join(sorted(covered_symbols)) or "none",
        )
    )
    return PremarketProviderResults(
        newsapi=newsapi,
        alpaca=alpaca,
        sec=sec,
        benzinga=benzinga,
        finnhub=finnhub,
        marketaux=marketaux,
        twitter=twitter,
        reddit=reddit,
        overnight_earnings=overnight,
        events=merged,
        catalysts=catalysts,
        rankings=rankings,
        candidate_symbols=uniq_symbols,
    )


def _should_fetch_providers(*, dry_run: bool, manual_debug: bool) -> bool:
    """Providers run on live jobs and on manual_debug dry-runs."""
    return manual_debug or not dry_run


def _run_news_5am_job(
    config: Mapping[str, Any] | None,
    now: datetime,
    *,
    project_root: Path | None = None,
    market_client: Any = None,
    dry_run: bool = False,
    manual_debug: bool = False,
) -> _PremarketJobStats:
    now = _as_eastern(now)
    job = "news_5am"
    _step(job, "load_universe")
    symbols = _news_symbols(config)
    symbol_count = len(symbols)
    catalysts: dict[str, Any] = {}
    filings = 0
    news_articles = 0
    rankings: list[PremarketRankEntry] = []
    provider_results: PremarketProviderResults | None = None
    fetch_providers = bool(symbols) and _should_fetch_providers(
        dry_run=dry_run,
        manual_debug=manual_debug,
    )

    if fetch_providers:
        _step(job, "fetch_news_start", symbols=symbol_count, timeout_sec=10)
        provider_results = execute_premarket_providers(
            config,
            symbols,
            market_client=market_client,
            now=now,
            project_root=project_root,
        )
        catalysts = provider_results.catalysts
        news_articles = provider_results.news_article_count
        filings = provider_results.filings_count
        rankings = list(provider_results.rankings)
        _step(
            job,
            "fetch_news_done",
            articles=news_articles,
            catalysts=len(catalysts),
            newsapi_ms=f"{provider_results.newsapi.duration_ms:.1f}",
            alpaca_ms=f"{provider_results.alpaca.duration_ms:.1f}",
            sec_ms=f"{provider_results.sec.duration_ms:.1f}",
        )
    elif dry_run:
        _step(job, "fetch_news_start", symbols=symbol_count, dry_run=True)
        _step(job, "fetch_news_done", articles=0)
        _step(job, "sec_fetch_start", symbols=symbol_count, dry_run=True)
        _step(job, "sec_fetch_done", filings=0)
    else:
        _step(job, "fetch_news_start", symbols=0)
        _step(job, "fetch_news_done", articles=0)
        _step(job, "sec_fetch_start", symbols=0)
        _step(job, "sec_fetch_done", filings=0)

    scored = 0
    _step(job, "finbert_start", articles=len(catalysts), timeout_sec=60)
    if news_articles > 0 or filings > 0:
        for sym, cat in sorted(catalysts.items()):
            scored += 1
            _emit(
                "NEWS_SCORE symbol=%s score=%s catalyst_type=%s headline=%s"
                % (
                    sym,
                    getattr(cat, "score", 0),
                    getattr(cat, "catalyst_type", None) or "unknown",
                    str(getattr(cat, "headline", "") or "").replace("\n", " ")[:180],
                )
            )
    _step(job, "finbert_done", scored=scored)

    ranked = 0
    _step(job, "rank_start")
    preserve_decision: _PremarketArtifactPreservation | None = None
    if project_root is not None and fetch_providers and provider_results is not None and not dry_run:
        catalyst_ranked_symbols = {
            str(sym or "").strip().upper()
            for sym in provider_results.catalysts.keys()
            if str(sym or "").strip()
        }
        catalyst_ranked_symbols.update(
            str(row.symbol or "").strip().upper()
            for row in rankings
            if str(row.symbol or "").strip()
        )
        preserve_decision = _premarket_artifact_preservation_decision(
            project_root,
            now=now,
            config=config,
            provider_rate_limited=_provider_results_rate_limited(provider_results),
            new_event_count=len(provider_results.events),
            new_catalyst_count=len(provider_results.catalysts),
            new_ranking_count=len(rankings),
            new_catalyst_ranked_symbols=len(catalyst_ranked_symbols),
        )
    preserve_artifacts = bool(preserve_decision and preserve_decision.preserve)
    if preserve_decision is not None and preserve_decision.preserve:
        _log_premarket_artifact_preserved(preserve_decision)
    if rankings:
        pm_cfg = _pm_section(config)
        top_n = int(pm_cfg.get("rank_top_n") or 10)
        log_premarket_rankings(rankings, top_n=top_n)
        if project_root is not None and not dry_run and not preserve_artifacts:
            write_premarket_rank_json(
                default_premarket_rank_path(project_root),
                rankings,
                now=now,
            )
        ranked = len(rankings)
        _step(job, "rank_done", ranked=ranked)
    else:
        _step(job, "rank_skipped", reason="no_rankable_events")

    if project_root is not None:
        pm_cfg = _pm_section(config)
        try:
            ttl_minutes = int(float(pm_cfg.get("artifact_ttl_minutes", 390) or 390))
        except (TypeError, ValueError):
            ttl_minutes = 390
        if not preserve_artifacts:
            write_premarket_artifacts(
                project_root,
                now=now,
                source="news_5am",
                events=provider_results.events if fetch_providers and provider_results is not None else [],
                catalysts=provider_results.catalysts if fetch_providers and provider_results is not None else {},
                rankings=rankings,
                candidate_symbols=provider_results.candidate_symbols if fetch_providers and provider_results is not None else symbols,
                ttl_minutes=ttl_minutes,
                config=config,
                provider_rate_limited=_provider_results_rate_limited(provider_results),
                preserve_existing=False,
            )
        if fetch_providers:
            write_premarket_provider_diagnostics(
                project_root,
                now=now,
                source="news_5am",
                provider_results=provider_results,
            )

    return _PremarketJobStats(
        symbols=symbol_count,
        news=max(news_articles, len(catalysts)),
        filings=filings,
        ranked=ranked,
    )


def _run_job_with_timeout(
    job: str,
    job_fn: Any,
    *,
    timeout_seconds: float,
    heartbeat_seconds: float = 15.0,
) -> _PremarketJobStats:
    if threading.current_thread() is threading.main_thread():
        started = time_module.monotonic()
        stop_heartbeat = threading.Event()

        def _heartbeat() -> None:
            while not stop_heartbeat.wait(float(heartbeat_seconds)):
                _emit(
                    "PREMARKET_JOB_HEARTBEAT job=%s elapsed_sec=%.1f"
                    % (job, time_module.monotonic() - started)
                )

        def _timeout_handler(signum: int, frame: Any) -> None:
            raise PremarketJobTimeout(
                "job %s exceeded %.0f second timeout" % (job, timeout_seconds)
            )

        hb_thread = threading.Thread(
            target=_heartbeat,
            name=f"premarket_{job}_heartbeat",
            daemon=True,
        )
        old_handler = signal.getsignal(signal.SIGALRM)
        hb_thread.start()
        try:
            signal.signal(signal.SIGALRM, _timeout_handler)
            signal.setitimer(signal.ITIMER_REAL, float(timeout_seconds))
            return job_fn()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            signal.signal(signal.SIGALRM, old_handler)
            stop_heartbeat.set()

    started = time_module.monotonic()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"premarket_{job}")
    future = executor.submit(job_fn)
    try:
        while True:
            elapsed = time_module.monotonic() - started
            remaining = timeout_seconds - elapsed
            if remaining <= 0:
                raise TimeoutError()
            try:
                return future.result(timeout=min(float(heartbeat_seconds), remaining))
            except TimeoutError:
                if future.done():
                    return future.result(timeout=0)
                _emit(
                    "PREMARKET_JOB_HEARTBEAT job=%s elapsed_sec=%.1f"
                    % (job, time_module.monotonic() - started)
                )
    finally:
        if not future.done():
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)


def run_premarket_scheduler_tick(
    config: Mapping[str, Any] | None,
    now: datetime,
    *,
    project_root: Path,
    market_client: Any = None,
    reason: str = "scheduled",
    dry_run: bool = False,
    manual_debug: bool | None = None,
    force_jobs: list[str] | None = None,
) -> list[PremarketJobResult]:
    now = _as_eastern(now)
    state_path = default_state_path(project_root)
    next_job = next_premarket_job(config, now)
    _emit("PREMARKET_SCHEDULER_TICK now=%s next_job=%s" % (now.isoformat(), next_job))
    due = list(
        force_jobs
        or due_premarket_jobs(
            config,
            now,
            state_path=state_path,
            project_root=project_root,
        )
    )
    if manual_debug is None:
        manual_debug = reason == "manual_debug"
    if not due:
        return [PremarketJobResult("news_5am", False, False, "not_due", reason)]

    results: list[PremarketJobResult] = []
    for job in due:
        _emit("PREMARKET_JOB_DUE job=%s now=%s" % (job, now.isoformat()))
        _emit("PREMARKET_JOB_START job=%s" % job)
        msg = "PREMARKET_JOB_RUNNING job=%s reason=%s" % (job, reason)
        _emit(msg)
        if not dry_run:
            _mark_job_state(
                state_path,
                job=job,
                run_date=now.date(),
                status="running",
                now=now,
                reason=reason,
            )
        started = time_module.monotonic()
        try:
            _step(job, "start")
            timeout_seconds = _timeout_seconds(config, "job_timeout_seconds", 120.0)
            if job != "news_5am":
                raise ValueError(f"unsupported premarket job: {job}")
            stats = _run_job_with_timeout(
                job,
                lambda: _run_news_5am_job(
                    config,
                    now,
                    project_root=project_root,
                    market_client=market_client,
                    dry_run=dry_run,
                    manual_debug=manual_debug,
                ),
                timeout_seconds=timeout_seconds,
            )
            duration_sec = time_module.monotonic() - started
            _emit(
                "PREMARKET_JOB_DONE job=%s symbols=%d news=%d filings=%d ranked=%d duration_sec=%.2f"
                % (
                    job,
                    stats.symbols,
                    stats.news,
                    stats.filings,
                    stats.ranked,
                    duration_sec,
                )
            )
        except Exception as exc:
            duration_sec = time_module.monotonic() - started
            err = "%s: %s" % (exc.__class__.__name__, str(exc))
            tb = traceback.format_exc().replace("\n", "\\n")[:4000]
            _emit(
                "PREMARKET_JOB_ERROR job=%s error=%s duration_sec=%.2f traceback=%s"
                % (job, err.replace("\n", " ")[:500], duration_sec, tb),
                level=logging.ERROR,
            )
            _emit(
                "PREMARKET_JOB_DONE job=%s status=error duration_sec=%.2f"
                % (job, duration_sec)
            )
            if not dry_run:
                _mark_job_state(
                    state_path,
                    job=job,
                    run_date=now.date(),
                    status="failed",
                    now=now,
                    reason=reason,
                    error=err,
                    duration_sec=round(duration_sec, 2),
                )
            results.append(
                PremarketJobResult(
                    job,
                    True,
                    False,
                    "error",
                    reason,
                    error=err,
                )
            )
            continue

        if not dry_run:
            _mark_job_state(
                state_path,
                job=job,
                run_date=now.date(),
                status="done",
                now=now,
                reason=reason,
                symbols=stats.symbols,
                news_count=stats.news,
                filings_count=stats.filings,
                ranked=stats.ranked,
                duration_sec=round(duration_sec, 2),
            )
        results.append(
            PremarketJobResult(
                job,
                True,
                not dry_run,
                "dry_run" if dry_run else "",
                reason,
                symbols=stats.symbols,
                news=stats.news,
                filings=stats.filings,
                ranked=stats.ranked,
            )
        )
    return results


def run_premarket_scheduler_startup_catchup(
    config: Mapping[str, Any] | None,
    now: datetime,
    *,
    project_root: Path,
    market_client: Any = None,
    dry_run: bool = False,
    force_jobs: list[str] | None = None,
) -> list[PremarketJobResult]:
    return run_premarket_scheduler_tick(
        config,
        now,
        project_root=project_root,
        market_client=market_client,
        reason="startup_catchup",
        dry_run=dry_run,
        force_jobs=force_jobs,
    )


def run_due_premarket_jobs(
    config: Mapping[str, Any] | None,
    now: datetime,
    *,
    project_root: Path,
    market_client: Any = None,
    reason: str = "scheduled",
    dry_run: bool = False,
    manual_debug: bool | None = None,
) -> list[PremarketJobResult]:
    """Run any premarket jobs due as of ``now``."""
    return run_premarket_scheduler_tick(
        config,
        now,
        project_root=project_root,
        market_client=market_client,
        reason=reason,
        dry_run=dry_run,
        manual_debug=manual_debug,
    )
