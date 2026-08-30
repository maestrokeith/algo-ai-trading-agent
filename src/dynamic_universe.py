from __future__ import annotations

import json
import logging
import math
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from src.news_catalyst import (
    NewsCatalyst,
    get_cached_news_metadata,
    fetch_recent_news_catalysts,
    get_news_score,
    news_cache_age_seconds,
    news_dynamic_entry_bypass_passes,
    news_early_entry_passes,
)
from src.news_sentiment.rules import evaluate_high_conviction_news_override
from src.options_premium_risk import is_option_symbol
from src.dynamic_price import effective_dynamic_min_price
from src.dynamic_entry_adaptive import (
    approved_minor_rule,
    classify_flexible_setup,
    classify_setup,
    dynamic_feature_readiness,
    dynamic_size_multiplier,
    flexible_entries_enabled,
    flexible_setup_enabled,
    flexible_size_multiplier,
    one_minor_rule_exception_allowed,
    resolve_adaptive_sensitivity,
)
from src.market.theme_intelligence import (
    symbol_theme_bonus,
    theme_etf_symbols,
    theme_intelligence_enabled,
    theme_momentum_scores,
)
from src.strategy_v2.entry_signals import rsi_wilder_last
from src.exposure import ETF_SYMBOLS

STATE_FILE = Path("data/dynamic_universe_state.json")

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

_DYNAMIC_LIVE_LOOSEN_OLD_MIN_GAIN = 3.0
_DYNAMIC_LIVE_LOOSEN_OLD_MIN_REL_VOLUME = 0.8


def _bool_cfg(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _float_cfg(value: Any, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float(default)
    return out if math.isfinite(out) else float(default)


def dynamic_adaptive_volume_min_relative_volume(
    *,
    base_min_relative_volume: float,
    cfg: Mapping[str, Any] | None,
    current_time: datetime | None,
    is_dynamic: bool,
    gain_pct: float | None = None,
    alignment_score: float | None = None,
    momentum_confirmed: bool | None = None,
) -> tuple[float, bool, str]:
    """Return dynamic momentum RVOL threshold after optional adaptive session logic."""
    base = max(0.0, float(base_min_relative_volume or 0.0))
    c = cfg if isinstance(cfg, Mapping) else {}
    raw = c.get("adaptive_volume_confirmation")
    av = raw if isinstance(raw, Mapping) else {}
    if not is_dynamic or not _bool_cfg(av.get("enabled"), False):
        return base, False, "disabled"

    try:
        et_now = current_time.astimezone(_ET) if current_time is not None else None
    except Exception:
        et_now = None
    after_11 = bool(et_now is not None and (et_now.hour, et_now.minute) >= (11, 0))
    threshold = base
    reasons: list[str] = []
    if after_11 and _bool_cfg(av.get("after_11am_enabled"), True):
        after_min = max(0.0, _float_cfg(av.get("after_11am_min_relative_volume"), base))
        if after_min < threshold:
            threshold = after_min
            reasons.append("session_after_11am")

    score = _finite_float(alignment_score)
    gain = _finite_float(gain_pct)
    strong_score_min = _float_cfg(av.get("strong_score_min"), 80.0)
    strong_gain_min = _float_cfg(av.get("strong_gain_pct_min"), 8.0)
    strong_score = score is not None and score >= strong_score_min
    strong_gain = gain is not None and gain >= strong_gain_min
    require_momentum = _bool_cfg(av.get("require_momentum_confirmation"), True)
    momentum_ok = bool(momentum_confirmed) if require_momentum else True
    if momentum_ok and (strong_score or strong_gain):
        tolerance = max(0.0, min(0.75, _float_cfg(av.get("minor_miss_tolerance"), 0.15)))
        tolerated = base * (1.0 - tolerance)
        if tolerated < threshold:
            threshold = tolerated
            reasons.append("strong_momentum_minor_volume_miss")

    threshold = max(0.0, min(base, threshold))
    adapted = threshold < base - 1e-12
    return threshold, adapted, "+".join(reasons) if adapted else "no_adaptation"


@dataclass(frozen=True)
class DynamicEntrySignals:
    """Intraday metrics for stricter gates on symbols outside the core YAML universe."""

    price_above_vwap: bool
    ema_5_above_20: bool
    rsi: float | None
    distance_from_vwap_pct: float


@dataclass(frozen=True)
class DynamicScanQuality:
    """Intraday quality metrics used before adding a symbol to the live universe."""

    price_above_vwap: bool | None
    five_min_trend_aligned: bool | None
    intraday_range_pct: float
    atr_expansion_ratio: float | None
    current_atr: float | None = None
    baseline_atr: float | None = None
    five_min_up_streak: int = 0


@dataclass(frozen=True)
class DynamicScanCandidate:
    """Scored dynamic-universe candidate after market-data collection."""

    symbol: str
    score: float
    accepted: bool
    rejection_reason: str | None
    price: float
    day_gain_pct: float
    volume: float
    avg_volume: float
    relative_volume: float
    spread_pct: float
    quality: DynamicScanQuality | None
    news_score: int = 0
    event_score: float = 0.0
    catalyst_score: float = 0.0
    article_count: int = 0
    news_headline: str | None = None
    catalyst_type: str | None = None
    catalyst_headline: str | None = None
    catalyst_age_minutes: float | None = None
    theme: str | None = None
    theme_bonus: float = 0.0
    timestamp: str | None = None
    later_same_day_high: float | None = None
    later_same_day_return_pct: float | None = None
    bid: float | None = None
    ask: float | None = None
    quote_timestamp: str | None = None
    quote_age_seconds: float | None = None
    quote_source: str | None = None
    scan_timestamp: str | None = None
    effective_min_rel_volume: float | None = None
    scanner_effective_min_rel_volume: float | None = None
    catalyst_fastlane_active: bool = False
    premarket_injected: bool = False
    corporate_action_type: str | None = None
    corporate_action_severity: str | None = None
    corporate_action_description: str | None = None


@dataclass(frozen=True)
class DynamicScanBatchResult:
    """Batch scanner result plus summary counts for live-loop logging."""

    selected: list[str]
    accepted: list[DynamicScanCandidate]
    rejected: list[DynamicScanCandidate]
    elapsed_ms: int

    @property
    def candidates(self) -> int:
        return len(self.accepted) + len(self.rejected)


def dynamic_scan_candidate_to_dict(row: DynamicScanCandidate) -> dict[str, Any]:
    """Serialize a dynamic scan row for durable scan history artifacts."""
    quality = row.quality
    return {
        "symbol": row.symbol,
        "score": float(row.score),
        "accepted": bool(row.accepted),
        "rejection_reason": row.rejection_reason,
        "price": float(row.price),
        "gain_pct": float(row.day_gain_pct),
        "day_gain_pct": float(row.day_gain_pct),
        "volume": float(row.volume),
        "avg_volume": float(row.avg_volume),
        "rel_volume": float(row.relative_volume),
        "relative_volume": float(row.relative_volume),
        "spread_pct": float(row.spread_pct),
        "bid": row.bid,
        "ask": row.ask,
        "quote_timestamp": row.quote_timestamp,
        "quote_age_seconds": row.quote_age_seconds,
        "quote_source": row.quote_source,
        "scan_timestamp": row.scan_timestamp or row.timestamp,
        "effective_min_rel_volume": row.effective_min_rel_volume,
        "scanner_effective_min_rel_volume": row.scanner_effective_min_rel_volume,
        "catalyst_fastlane_active": bool(row.catalyst_fastlane_active),
        "corporate_action_type": row.corporate_action_type,
        "corporate_action_severity": row.corporate_action_severity,
        "corporate_action_description": row.corporate_action_description,
        "quality": None
        if quality is None
        else {
            "price_above_vwap": quality.price_above_vwap,
            "five_min_trend_aligned": quality.five_min_trend_aligned,
            "intraday_range_pct": float(quality.intraday_range_pct),
            "atr_expansion_ratio": quality.atr_expansion_ratio,
            "five_min_up_streak": int(quality.five_min_up_streak),
        },
        "news_score": int(row.news_score),
        "event_score": float(row.event_score),
        "catalyst_score": float(row.catalyst_score),
        "article_count": int(row.article_count),
        "news_headline": row.news_headline,
        "catalyst_type": row.catalyst_type,
        "catalyst_headline": row.catalyst_headline,
        "catalyst_age_minutes": row.catalyst_age_minutes,
        "premarket_injected": bool(row.premarket_injected),
        "theme": row.theme,
        "theme_bonus": float(row.theme_bonus),
        "timestamp": row.timestamp,
        "later_same_day_high": row.later_same_day_high,
        "later_same_day_return_pct": row.later_same_day_return_pct,
    }


def dynamic_scan_candidates_to_dicts(
    rows: Sequence[DynamicScanCandidate],
) -> list[dict[str, Any]]:
    """Serialize dynamic scan candidate rows for JSON artifacts and SQLite payloads."""
    return [dynamic_scan_candidate_to_dict(row) for row in rows]


def _artifact_float(row: Mapping[str, Any] | None, key: str, default: float = 0.0) -> float:
    if not isinstance(row, Mapping):
        return default
    try:
        value = float(row.get(key, default) or default)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _artifact_int(row: Mapping[str, Any] | None, key: str, default: int = 0) -> int:
    if not isinstance(row, Mapping):
        return default
    try:
        return int(float(row.get(key, default) or default))
    except (TypeError, ValueError):
        return default


def _confirmed_artifact_metadata(row: Mapping[str, Any] | None) -> bool:
    """True when an artifact row has real catalyst/news metadata, not just a symbol."""
    if not isinstance(row, Mapping) or not row:
        return False
    headline = str(row.get("headline") or row.get("catalyst_headline") or "").strip()
    catalyst_type = str(row.get("catalyst_type") or "").strip()
    return bool(
        _artifact_float(row, "news_score") > 0.0
        or _artifact_float(row, "event_score") > 0.0
        or _artifact_float(row, "catalyst_score") > 0.0
        or _artifact_int(row, "article_count") > 0
        or headline
        or catalyst_type
    )


def _log_catalyst_lookup(
    symbol: str,
    *,
    artifact: Mapping[str, Any] | None,
    emit_logs: bool,
) -> bool:
    if not emit_logs:
        return _confirmed_artifact_metadata(artifact)
    lookup_key = str(symbol or "").strip().upper()
    if not lookup_key:
        log.info("CATALYST_LOOKUP symbol=%s found=false reason=missing_lookup_key lookup_key=", symbol)
        return False
    if not isinstance(artifact, Mapping) or not artifact:
        log.info(
            "CATALYST_LOOKUP symbol=%s found=false reason=missing_lookup_key lookup_key=%s",
            lookup_key,
            lookup_key,
        )
        return False
    confirmed = _confirmed_artifact_metadata(artifact)
    if not confirmed:
        log.info(
            "CATALYST_LOOKUP symbol=%s found=false reason=below_threshold lookup_key=%s",
            lookup_key,
            lookup_key,
        )
        return False
    log.info(
        "CATALYST_LOOKUP symbol=%s found=true lookup_key=%s source=%s "
        "news_score=%.2f event_score=%.2f catalyst_score=%.2f article_count=%d headline=%s",
        lookup_key,
        lookup_key,
        str(artifact.get("source") or artifact.get("artifact_kind") or "catalysts"),
        _artifact_float(artifact, "news_score"),
        _artifact_float(artifact, "event_score"),
        _artifact_float(artifact, "catalyst_score"),
        _artifact_int(artifact, "article_count"),
        str(artifact.get("headline") or artifact.get("catalyst_headline") or "")[:180],
    )
    return True


def _bar_timestamps_utc(bars: pd.DataFrame) -> pd.Series | None:
    if bars.empty:
        return None
    if isinstance(bars.index, pd.DatetimeIndex):
        values = pd.Series(bars.index, index=bars.index)
    else:
        values = None
        for col in ("timestamp", "datetime", "time", "t"):
            if col in bars.columns:
                values = bars[col]
                break
        if values is None:
            return None
    parsed = pd.to_datetime(values, utc=True, errors="coerce")
    if parsed.isna().all():
        return None
    return pd.Series(parsed, index=bars.index)


def _dynamic_candidate_bar_rows(bars: pd.DataFrame | None, *, limit: int = 120) -> list[dict[str, Any]]:
    """Serialize already-loaded intraday bars for research artifacts."""
    if bars is None or bars.empty:
        return []

    frame = bars.tail(limit).copy()
    timestamps = _bar_timestamps_utc(frame)
    if timestamps is None:
        return []

    aliases = {
        "open": ("open", "Open", "o"),
        "high": ("high", "High", "h"),
        "low": ("low", "Low", "l"),
        "close": ("close", "Close", "c"),
        "volume": ("volume", "Volume", "v"),
    }

    def pick(row: pd.Series, names: tuple[str, ...]) -> float | int | None:
        for name in names:
            if name in row:
                value = row[name]
                if pd.isna(value):
                    return None
                try:
                    number = float(value)
                except (TypeError, ValueError, OverflowError):
                    return None
                if not math.isfinite(number):
                    return None
                return int(number) if name in {"volume", "Volume", "v"} else number
        return None

    rows: list[dict[str, Any]] = []
    for pos, (_idx, row) in enumerate(frame.iterrows()):
        ts = timestamps.iloc[pos]
        if pd.isna(ts):
            continue
        out: dict[str, Any] = {"timestamp": ts.isoformat()}
        for key, names in aliases.items():
            out[key] = pick(row, names)
        if all(out.get(key) is None for key in ("open", "high", "low", "close")):
            continue
        rows.append(out)
    return rows


def persist_dynamic_candidate_bar_snapshot(
    *,
    symbol: str,
    user_id: str | None,
    bars: pd.DataFrame | None,
    timeframe: str,
    project_root: Path | None = None,
    now: datetime | None = None,
    source: str = "dynamic_selected",
) -> Path | None:
    """Persist a small research-only bar snapshot from bars already in memory."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    rows = _dynamic_candidate_bar_rows(bars)
    if not rows:
        return None

    captured_at = now or datetime.now(tz=_ET)
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=_ET)
    captured_et = captured_at.astimezone(_ET)
    safe_user = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(user_id or "default"))
    safe_user = safe_user or "default"
    root = Path(project_root or ".")
    path = (
        root
        / "data"
        / "research"
        / "dynamic_candidate_bars"
        / captured_et.date().isoformat()
        / safe_user
        / f"{sym}.json"
    )
    payload = {
        "symbol": sym,
        "user": safe_user,
        "captured_at": captured_et.isoformat(),
        "source": source,
        "timeframe": str(timeframe),
        "bars": rows,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def persist_allocator_candidate_bar_snapshot(
    *,
    symbol: str,
    user_id: str | None,
    bars: pd.DataFrame | None,
    timeframe: str,
    project_root: Path | None = None,
    now: datetime | None = None,
    source: str = "allocator_candidate",
    route: str | None = None,
) -> Path | None:
    """Persist a research-only allocator candidate bar snapshot from in-memory bars."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    rows = _dynamic_candidate_bar_rows(bars)
    if not rows:
        return None

    captured_at = now or datetime.now(tz=_ET)
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=_ET)
    captured_et = captured_at.astimezone(_ET)
    safe_user = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(user_id or "default"))
    safe_user = safe_user or "default"
    root = Path(project_root or ".")
    path = (
        root
        / "data"
        / "research"
        / "allocator_candidate_bars"
        / captured_et.date().isoformat()
        / safe_user
        / f"{sym}.json"
    )
    payload = {
        "symbol": sym,
        "user": safe_user,
        "captured_at": captured_et.isoformat(),
        "source": source,
        "route": route,
        "timeframe": str(timeframe),
        "bars": rows,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _persist_selected_dynamic_candidate_bars(
    *,
    selected_rows: Sequence[DynamicScanCandidate],
    bars_1m_by_symbol: Mapping[str, pd.DataFrame | None],
    bars_5m_by_symbol: Mapping[str, pd.DataFrame | None],
    history_user_id: str | None,
    history_project_root: Path | None,
    now: datetime | None,
) -> None:
    for row in selected_rows:
        symbol = str(row.symbol).upper()
        bars = bars_1m_by_symbol.get(symbol)
        timeframe = "1Min"
        if bars is None or bars.empty:
            bars = bars_5m_by_symbol.get(symbol)
            timeframe = "5Min"
        try:
            persist_dynamic_candidate_bar_snapshot(
                symbol=symbol,
                user_id=history_user_id,
                bars=bars,
                timeframe=timeframe,
                project_root=history_project_root,
                now=now,
            )
        except Exception:
            log.debug("dynamic selected bar snapshot write failed symbol=%s", symbol, exc_info=True)


def _later_same_day_high_return_from_bars(
    bars: pd.DataFrame | None,
    *,
    rejected_at: datetime,
    rejection_price: float,
) -> tuple[float | None, float | None]:
    """Return same-day high/return after rejection using already loaded bars."""
    if bars is None or bars.empty or rejection_price <= 0:
        return None, None
    high_col = "high" if "high" in bars.columns else None
    if high_col is None:
        for col in ("h", "High"):
            if col in bars.columns:
                high_col = col
                break
    if high_col is None:
        return None, None
    timestamps = _bar_timestamps_utc(bars)
    if timestamps is None:
        return None, None
    observed_utc = rejected_at.astimezone(timezone.utc)
    observed_day = observed_utc.astimezone(_ET).date()
    day_mask = timestamps.dt.tz_convert(_ET).dt.date == observed_day
    later_mask = timestamps > observed_utc
    later = bars.loc[day_mask & later_mask]
    if later.empty:
        return None, None
    highs = pd.to_numeric(later[high_col], errors="coerce").dropna()
    if highs.empty:
        return None, None
    high = float(highs.max())
    return high, ((high / float(rejection_price)) - 1.0) * 100.0


def _snapshot_quote_quality(snapshot: Mapping[str, Any], *, scan_timestamp: datetime) -> dict[str, Any]:
    """Return persisted quote diagnostics from a market snapshot."""
    quote_ts_raw = (
        snapshot.get("quote_timestamp")
        or snapshot.get("quote_time")
        or snapshot.get("timestamp")
        or snapshot.get("updated_at")
        or snapshot.get("t")
    )
    quote_ts: datetime | None = None
    if quote_ts_raw is not None:
        text = str(quote_ts_raw).strip()
        if text:
            try:
                quote_ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                quote_ts = None
            if quote_ts is not None and quote_ts.tzinfo is None:
                quote_ts = quote_ts.replace(tzinfo=timezone.utc)
    quote_age = _safe_float(
        snapshot.get("quote_age_seconds")
        or snapshot.get("quote_age_sec")
        or snapshot.get("age_seconds"),
        math.nan,
    )
    if (not math.isfinite(float(quote_age))) and quote_ts is not None:
        quote_age = max(
            0.0,
            (scan_timestamp.astimezone(timezone.utc) - quote_ts.astimezone(timezone.utc)).total_seconds(),
        )
    source = (
        str(
            snapshot.get("quote_source")
            or snapshot.get("source")
            or snapshot.get("provider")
            or snapshot.get("feed")
            or ""
        ).strip()
        or None
    )
    bid = _safe_float(snapshot.get("bid"), math.nan)
    ask = _safe_float(snapshot.get("ask"), math.nan)
    return {
        "bid": float(bid) if math.isfinite(float(bid)) else None,
        "ask": float(ask) if math.isfinite(float(ask)) else None,
        "quote_timestamp": quote_ts.isoformat() if quote_ts is not None else None,
        "quote_age_seconds": float(quote_age) if math.isfinite(float(quote_age)) else None,
        "quote_source": source,
    }


def _dynamic_scan_artifact_cfg(cfg: Mapping[str, Any]) -> dict[str, Any]:
    raw = cfg.get("artifact_history") if isinstance(cfg, Mapping) else None
    history_cfg = dict(raw) if isinstance(raw, Mapping) else {}
    return {
        "enabled": bool(history_cfg.get("enabled", True)),
        "directory": str(history_cfg.get("directory") or "data/dynamic_scan_history"),
        "retention_days": int(history_cfg.get("retention_days", 30) or 30),
        "daily_report_enabled": bool(history_cfg.get("daily_report_enabled", True)),
    }


def _dynamic_scan_reason_key(reason: Any) -> str:
    text = str(reason or "unknown").strip().lower()
    if not text:
        return "unknown"
    text = text.split(":", 1)[0].strip()
    out = []
    prev_sep = False
    for ch in text:
        if ch.isalnum():
            out.append(ch)
            prev_sep = False
        elif not prev_sep:
            out.append("_")
            prev_sep = True
    key = "".join(out).strip("_")
    return key or "unknown"


def _dynamic_scan_accept_reason(row: DynamicScanCandidate) -> str:
    if float(row.catalyst_score or 0.0) > 0.0 or int(row.news_score or 0) > 0 or float(row.event_score or 0.0) > 0.0:
        return "catalyst"
    if float(row.theme_bonus or 0.0) > 0.0:
        return "theme_momentum"
    return "quality_filters"


def dynamic_scan_analytics(
    result: DynamicScanBatchResult,
    *,
    top_n: int = 6,
) -> dict[str, Any]:
    """Return per-cycle accept/reject reason analytics for logs and reports."""
    reject_counts = Counter(
        _dynamic_scan_reason_key(row.rejection_reason) for row in result.rejected
    )
    accept_counts = Counter(_dynamic_scan_accept_reason(row) for row in result.accepted)
    top_rejections = dict(reject_counts.most_common(max(0, int(top_n))))
    return {
        "rejections": dict(sorted(reject_counts.items())),
        "accepts": dict(sorted(accept_counts.items())),
        "top_rejections": top_rejections,
    }


def log_dynamic_scan_rejection_summary(
    result: DynamicScanBatchResult,
    *,
    emit_logs: bool = True,
    top_n: int = 6,
) -> dict[str, Any]:
    """Log top dynamic rejection causes and return the analytics payload."""
    analytics = dynamic_scan_analytics(result, top_n=top_n)
    if not emit_logs:
        return analytics
    log.info("DYNAMIC_REJECTION_SUMMARY")
    print("DYNAMIC_REJECTION_SUMMARY", flush=True)
    for reason, count in analytics["top_rejections"].items():
        line = f"{reason}={int(count)}"
        log.info(line)
        print(line, flush=True)
    if analytics["accepts"]:
        log.info("DYNAMIC_ACCEPT_SUMMARY %s", analytics["accepts"])
    return analytics


def _log_dynamic_loosened_pass(
    *,
    symbol: str,
    old_reason: str,
    old_threshold: float,
    new_threshold: float,
    observed: float,
    emit_logs: bool,
) -> None:
    if not emit_logs:
        return
    line = (
        "DYNAMIC_LOOSENED_PASS symbol=%s old_reason=%s old_threshold=%.2f "
        "new_threshold=%.2f observed=%.3f"
        % (
            str(symbol or "").strip().upper(),
            str(old_reason or "unknown"),
            float(old_threshold),
            float(new_threshold),
            float(observed),
        )
    )
    log.info(line)
    print(line, flush=True)


def _dynamic_alignment_bypass_ok(
    *,
    price: float,
    spread_pct: float,
    avg_volume: float,
    day_gain_pct: float,
    relative_volume: float,
    quality: DynamicScanQuality,
    settings: Mapping[str, Any],
) -> bool:
    try:
        min_price = float(settings.get("min_price", 0.0) or 0.0)
        max_price = float(settings.get("max_price", float("inf")) or float("inf"))
        max_spread_pct = float(settings.get("max_spread_pct", 0.0) or 0.0)
        min_avg_volume = float(settings.get("min_avg_vol", 0.0) or 0.0)
        min_atr_expansion_ratio = float(settings.get("min_atr_expansion_ratio", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    if price < min_price or price > max_price:
        return False
    if max_spread_pct > 0.0 and spread_pct > max_spread_pct + 1e-9:
        return False
    if avg_volume < min_avg_volume:
        return False
    if day_gain_pct < 15.0:
        return False
    if relative_volume < 1.2:
        return False
    if min_atr_expansion_ratio > 0.0:
        if quality.atr_expansion_ratio is None:
            return False
        if float(quality.atr_expansion_ratio) < min_atr_expansion_ratio:
            return False
    return True


def _resolve_artifact_dir(directory: str, project_root: Path | None) -> Path:
    path = Path(directory)
    if path.is_absolute():
        return path
    return (project_root or Path.cwd()) / path


def _prune_dynamic_scan_artifacts(
    directory: Path,
    *,
    retention_days: int,
    now: datetime,
) -> None:
    if retention_days <= 0 or not directory.exists():
        return
    cutoff_ts = now.timestamp() - (float(retention_days) * 86400.0)
    for path in directory.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff_ts:
                path.unlink()
        except OSError:
            log.debug("dynamic scan artifact prune skipped path=%s", path, exc_info=True)


def _write_dynamic_scan_daily_report(
    *,
    artifact_dir: Path,
    generated_at: datetime,
    user_id: str,
    result: DynamicScanBatchResult,
    analytics: Mapping[str, Any],
) -> Path:
    daily_dir = artifact_dir / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    day = generated_at.astimezone(_ET).date().isoformat()
    safe_user = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(user_id or "default"))
    path = daily_dir / f"{day}_{safe_user}.json"
    payload: dict[str, Any]
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            payload = loaded if isinstance(loaded, dict) else {}
        except Exception:
            payload = {}
    else:
        payload = {}
    rejection_totals = Counter(
        {
            str(k): int(v)
            for k, v in (payload.get("rejection_counts") or {}).items()
            if str(k)
        }
    )
    accept_totals = Counter(
        {
            str(k): int(v)
            for k, v in (payload.get("accept_counts") or {}).items()
            if str(k)
        }
    )
    rejection_totals.update(
        {
            str(k): int(v)
            for k, v in (analytics.get("rejections") or {}).items()
            if str(k)
        }
    )
    accept_totals.update(
        {
            str(k): int(v)
            for k, v in (analytics.get("accepts") or {}).items()
            if str(k)
        }
    )
    cycles = int(payload.get("cycles", 0) or 0) + 1
    payload = {
        "date": day,
        "user_id": user_id,
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
        "cycles": cycles,
        "counts": {
            "candidates": int((payload.get("counts") or {}).get("candidates", 0) or 0) + result.candidates,
            "accepted": int((payload.get("counts") or {}).get("accepted", 0) or 0) + len(result.accepted),
            "rejected": int((payload.get("counts") or {}).get("rejected", 0) or 0) + len(result.rejected),
            "selected": int((payload.get("counts") or {}).get("selected", 0) or 0) + len(result.selected),
        },
        "rejection_counts": dict(sorted(rejection_totals.items())),
        "accept_counts": dict(sorted(accept_totals.items())),
        "top_rejection_causes": dict(rejection_totals.most_common(6)),
        "last_cycle": {
            "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
            "selected": list(result.selected),
            "counts": {
                "candidates": result.candidates,
                "accepted": len(result.accepted),
                "rejected": len(result.rejected),
                "selected": len(result.selected),
            },
            "analytics": dict(analytics),
        },
    }
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)
    return path


def persist_dynamic_scan_history(
    result: DynamicScanBatchResult,
    cfg: Mapping[str, Any],
    *,
    user_id: str | None = None,
    project_root: Path | None = None,
    now: datetime | None = None,
) -> Path | None:
    """Persist accepted/rejected dynamic scan candidates and prune old artifacts."""
    history_cfg = _dynamic_scan_artifact_cfg(cfg)
    if not history_cfg["enabled"]:
        return None
    generated_at = now or datetime.now(timezone.utc)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    artifact_dir = _resolve_artifact_dir(str(history_cfg["directory"]), project_root)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    accepted = dynamic_scan_candidates_to_dicts(result.accepted)
    rejected = dynamic_scan_candidates_to_dicts(result.rejected)
    generated_at_iso = generated_at.astimezone(timezone.utc).isoformat()
    for row in rejected:
        row["timestamp"] = row.get("timestamp") or generated_at_iso
    analytics = dynamic_scan_analytics(result)
    payload = {
        "generated_at": generated_at_iso,
        "user_id": user_id or "default",
        "selected": list(result.selected),
        "counts": {
            "candidates": result.candidates,
            "accepted": len(result.accepted),
            "rejected": len(result.rejected),
            "selected": len(result.selected),
        },
        "elapsed_ms": int(result.elapsed_ms),
        "accepted": accepted,
        "rejected": rejected,
        "candidates": accepted + rejected,
        "analytics": analytics,
    }
    stamp = generated_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    user_part = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(user_id or "default"))
    path = artifact_dir / f"{stamp}_{user_part}.json"
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)
    _prune_dynamic_scan_artifacts(
        artifact_dir,
        retention_days=int(history_cfg["retention_days"]),
        now=generated_at,
    )
    log.info(
        "DYNAMIC_SCAN_ARTIFACT path=%s accepted=%d rejected=%d retention_days=%d",
        path,
        len(result.accepted),
        len(result.rejected),
        int(history_cfg["retention_days"]),
    )
    if history_cfg.get("daily_report_enabled", True):
        try:
            daily_path = _write_dynamic_scan_daily_report(
                artifact_dir=artifact_dir,
                generated_at=generated_at,
                user_id=user_id or "default",
                result=result,
                analytics=analytics,
            )
            log.info("DYNAMIC_SCAN_DAILY_REPORT path=%s", daily_path)
        except Exception:
            log.debug("dynamic scan daily report write failed", exc_info=True)
    return path


def is_dynamic_symbol(symbol: str, core_symbols: list[str]) -> bool:
    """True when *symbol* is not in the configured core universe list (e.g. scanner-added names)."""
    su = str(symbol or "").strip().upper()
    core_u = {str(s).strip().upper() for s in core_symbols}
    return su not in core_u


def classify_symbol(
    symbol: str,
    core_symbols: Sequence[str] | None = None,
    *,
    allocator_holdings: Sequence[str] | None = None,
    dynamic_symbols: Sequence[str] | None = None,
) -> str:
    """
    Classify a symbol with precedence:

    1. Core universe
    2. Allocator holdings
    3. Dynamic universe

    ``CORE_WITH_DYNAMIC_SIGNAL`` is returned when a core symbol also appears in the dynamic set.
    ``OTHER`` is returned when the symbol does not match any provided set.
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        return "OTHER"
    core_u = {str(s).strip().upper() for s in (core_symbols or []) if str(s or "").strip()}
    alloc_u = {
        str(s).strip().upper()
        for s in (allocator_holdings or [])
        if s is not None and str(s).strip()
    }
    dyn_u = {
        str(s).strip().upper()
        for s in (dynamic_symbols or [])
        if s is not None and str(s).strip()
    }
    if sym in core_u:
        return "CORE_WITH_DYNAMIC_SIGNAL" if sym in dyn_u else "CORE"
    if sym in alloc_u:
        return "ALLOCATOR_HOLDING"
    if sym in dyn_u:
        return "DYNAMIC_ONLY"
    return "OTHER"


def symbol_classification_has_dynamic_signal(symbol_class: str) -> bool:
    """True when the symbol class should be allowed to use dynamic-news conviction, but not dynamic-only exits."""
    return str(symbol_class or "").strip().upper() in {"CORE_WITH_DYNAMIC_SIGNAL", "DYNAMIC_ONLY"}


def dynamic_regime_strength_threshold_multiplier(
    config: Mapping[str, Any] | None,
) -> float:
    """
    When ``dynamic_universe.regime_relax.enabled`` is true, multiply minimum signal-strength floors
    by this factor for **dynamic momentum** names only (see live loop). Values **below 1.0** relax
    regime-style gates (e.g. ``0.85`` × ``strong_signal_strength_min``).
    """
    cfg = dict(config) if config is not None else {}
    du = cfg.get("dynamic_universe")
    if not isinstance(du, Mapping):
        return 1.0
    rr = du.get("regime_relax")
    if not isinstance(rr, Mapping) or not bool(rr.get("enabled", False)):
        return 1.0
    raw = rr.get("strength_threshold_mult", 0.85)
    try:
        m = float(raw)
    except (TypeError, ValueError):
        m = 0.85
    return max(0.0, min(1.5, m))


def compute_dynamic_entry_signals(
    bars_1m: pd.DataFrame,
    ref_price: float,
) -> DynamicEntrySignals:
    """
    Session VWAP and 1m EMA(5) vs EMA(20) from intraday bars; RSI(Wilder, 14) on 1m closes.

    *ref_price* is typically quote mid or last daily close — compared to VWAP for extension / above-VWAP checks.
    """
    bad = DynamicEntrySignals(False, False, None, 0.0)
    if bars_1m is None or getattr(bars_1m, "empty", True):
        return bad
    need = {"high", "low", "close", "volume"}
    if not need.issubset(set(bars_1m.columns)):
        return bad

    close_s = bars_1m["close"].astype(float)
    if len(close_s) < 5:
        return bad

    vwap = session_vwap_from_bars(bars_1m)

    px = float(ref_price)
    if vwap is None or vwap <= 0:
        return DynamicEntrySignals(False, False, None, 999.0)

    price_above_vwap = px > vwap
    distance_from_vwap_pct = (px - vwap) / vwap * 100.0

    ema_5_above_20 = False
    if len(close_s) >= 20:
        ema5 = float(close_s.ewm(span=5, adjust=False).mean().iloc[-1])
        ema20 = float(close_s.ewm(span=20, adjust=False).mean().iloc[-1])
        ema_5_above_20 = ema5 > ema20

    rsi_val = rsi_wilder_last(close_s, 14)

    return DynamicEntrySignals(
        price_above_vwap=price_above_vwap,
        ema_5_above_20=ema_5_above_20,
        rsi=rsi_val,
        distance_from_vwap_pct=distance_from_vwap_pct,
    )


def session_vwap_from_bars(bars_1m: pd.DataFrame | None) -> float | None:
    """Return session VWAP from 1m bars when high/low/close/volume are available."""
    if bars_1m is None or getattr(bars_1m, "empty", True):
        return None
    need = {"high", "low", "close", "volume"}
    if not need.issubset(set(bars_1m.columns)):
        return None
    volume = bars_1m["volume"].astype(float)
    if float(volume.sum()) <= 0:
        return None
    close_s = bars_1m["close"].astype(float)
    typical = (
        bars_1m["high"].astype(float)
        + bars_1m["low"].astype(float)
        + close_s
    ) / 3.0
    vwap = float((typical * volume).sum() / float(volume.sum()))
    if vwap <= 0 or not math.isfinite(vwap):
        return None
    return vwap


def dynamic_entry_vwap_extension_pct(price: float, vwap: float | None) -> float | None:
    """Percent extension above VWAP; positive means price is above VWAP."""
    try:
        px = float(price)
        vw = float(vwap) if vwap is not None else math.nan
    except (TypeError, ValueError):
        return None
    if not math.isfinite(px) or not math.isfinite(vw) or vw <= 0:
        return None
    return ((px - vw) / vw) * 100.0


def dynamic_entry_spread_override_cap(
    *,
    gain_pct: float | None,
    relative_volume: float | None,
) -> float | None:
    """Relax dynamic BUY spread cap for exceptional momentum names."""
    try:
        gain = float(gain_pct) if gain_pct is not None else math.nan
        rel = float(relative_volume) if relative_volume is not None else math.nan
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(gain) and math.isfinite(rel)):
        return None
    if gain >= 15.0 and rel >= 1.3:
        return 3.5
    return None


def dynamic_entry_guard_passes(
    sig: DynamicEntrySignals,
    *,
    max_distance_from_vwap_pct: float = 2.0,
    require_above_vwap: bool = True,
    require_ema_5_above_20: bool = True,
) -> bool:
    """Stricter entry rules for dynamic (non-core) symbols."""
    max_dist = max(0.0, float(max_distance_from_vwap_pct))
    if require_above_vwap and not sig.price_above_vwap:
        return False
    if require_ema_5_above_20 and not sig.ema_5_above_20:
        return False
    if sig.rsi is not None and sig.rsi > 75:
        return False
    if sig.distance_from_vwap_pct > max_dist:
        return False
    return True


def dynamic_entry_guard_failure_reason(
    sig: DynamicEntrySignals,
    *,
    max_distance_from_vwap_pct: float = 2.0,
    require_above_vwap: bool = True,
    require_ema_5_above_20: bool = True,
) -> str:
    """First failing rule (for skip logs)."""
    max_dist = max(0.0, float(max_distance_from_vwap_pct))
    if require_above_vwap and not sig.price_above_vwap:
        return "price not above session VWAP"
    if require_ema_5_above_20 and not sig.ema_5_above_20:
        return "EMA(5) not above EMA(20) on 1m"
    if sig.rsi is not None and sig.rsi > 75:
        return "RSI %.1f > 75" % sig.rsi
    if sig.distance_from_vwap_pct > max_dist:
        return "distance from VWAP %.2f%% > %.1f%%" % (
            sig.distance_from_vwap_pct,
            max_dist,
        )
    return "unknown"


def high_momentum_bypass_ok(
    *,
    gain_pct: float | None,
    relative_volume: float | None,
    vwap_above: bool,
    spread_pct: float | None,
    cfg: Mapping[str, Any] | None = None,
) -> bool:
    """
    True for exceptional dynamic momentum names that can skip minor structure confirmations.

    Default operator rule:
    day gain > 12%, relative volume > 3, above session VWAP, and spread < 0.5%.
    """
    c = dict(cfg) if isinstance(cfg, Mapping) else {}
    hmb = c.get("high_momentum_bypass")
    hmb_cfg = hmb if isinstance(hmb, Mapping) else {}
    if hmb_cfg and hmb_cfg.get("enabled") is False:
        return False
    try:
        min_gain = float(hmb_cfg.get("min_day_gain_pct", 12.0))
    except (TypeError, ValueError):
        min_gain = 12.0
    try:
        min_rel = float(hmb_cfg.get("min_relative_volume", 3.0))
    except (TypeError, ValueError):
        min_rel = 3.0
    try:
        max_spread = float(hmb_cfg.get("max_spread_pct", 0.5))
    except (TypeError, ValueError):
        max_spread = 0.5
    try:
        gain = float(gain_pct) if gain_pct is not None else math.nan
        rel = float(relative_volume) if relative_volume is not None else math.nan
        spread = float(spread_pct) if spread_pct is not None else math.nan
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(gain) and math.isfinite(rel) and math.isfinite(spread)):
        return False
    require_vwap = bool(hmb_cfg.get("require_above_vwap", True))
    return (
    gain > min_gain
    and rel > min_rel
    and (bool(vwap_above) or not require_vwap)
    and spread < max_spread
    )
    #return gain > min_gain and rel > min_rel and bool(vwap_above) and spread < max_spread


def _datetime_index_et(bars_1m: pd.DataFrame) -> pd.DatetimeIndex | None:
    """Return the frame index as timezone-aware America/New_York (Alpaca bars are often naive UTC)."""
    idx = bars_1m.index
    if not isinstance(idx, pd.DatetimeIndex):
        return None
    if idx.tz is None:
        try:
            idx = idx.tz_localize("UTC", ambiguous="infer", nonexistent="shift_forward")
        except (TypeError, ValueError):
            return None
    try:
        return idx.tz_convert(_ET)
    except (TypeError, ValueError):
        return None


def opening_range_high_first_minutes(
    bars_1m: pd.DataFrame | None,
    *,
    minutes: int = 15,
    session_date: date | None = None,
) -> float | None:
    """
    Maximum high of regular-session 1m bars from 09:30 ET through 09:30+(*minutes*) on *session_date*.

    Requires a :class:`~pandas.DatetimeIndex` on *bars_1m*. Returns ``None`` if the index is unusable
    or no bars fall in the opening window (e.g. missing data).
    """
    if bars_1m is None or getattr(bars_1m, "empty", True):
        return None
    if "high" not in bars_1m.columns:
        return None
    idx_et = _datetime_index_et(bars_1m)
    if idx_et is None:
        return None
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        m = 15
    m = max(1, min(120, m))
    if session_date is None:
        session_date = idx_et[-1].date()
    session_start = datetime.combine(session_date, dt_time(9, 30), tzinfo=_ET)
    session_end = session_start + timedelta(minutes=m)
    tmp = bars_1m.copy()
    tmp.index = idx_et
    sub = tmp.loc[(tmp.index >= session_start) & (tmp.index < session_end)]
    if sub.empty:
        return None
    highs = sub["high"].astype(float)
    mx = float(highs.max())
    return mx if mx > 0 and math.isfinite(mx) else None


def opening_range_breakout_above(
    bars_1m: pd.DataFrame | None,
    ref_price: float,
    *,
    minutes: int = 15,
    session_date: date | None = None,
) -> bool:
    """True when *ref_price* is strictly above the opening-range high (first *minutes* of RTH)."""
    orh = opening_range_high_first_minutes(
        bars_1m, minutes=minutes, session_date=session_date
    )
    if orh is None or orh <= 0:
        return False
    try:
        px = float(ref_price)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(px):
        return False
    return px > orh


def five_min_breakout_from_bars(bars_5m: pd.DataFrame | None, ref_price: float) -> bool:
    """True when *ref_price* breaks above the max high of all **prior** completed 5m bars."""
    if bars_5m is None or getattr(bars_5m, "empty", True):
        return False
    if not {"high"}.issubset(set(bars_5m.columns)):
        return False
    hi = bars_5m["high"].astype(float)
    if len(hi) < 2:
        return False
    prior_max = float(hi.iloc[:-1].max())
    px = float(ref_price)
    return prior_max > 0 and px > prior_max


def new_intraday_high_from_1m(bars_1m: pd.DataFrame | None, ref_price: float) -> bool:
    """True when *ref_price* is at/near the session high from 1m bars."""
    if bars_1m is None or getattr(bars_1m, "empty", True):
        return False
    if "high" not in bars_1m.columns:
        return False
    mx = float(bars_1m["high"].astype(float).max())
    px = float(ref_price)
    if mx <= 0:
        return False
    return px >= mx * (1.0 - 2e-4)


def five_min_trend_positive_from_bars(bars_5m: pd.DataFrame | None) -> bool:
    """True when the latest 5m close is above the prior 5m close."""
    if bars_5m is None or getattr(bars_5m, "empty", True):
        return False
    if "close" not in bars_5m.columns or len(bars_5m) < 2:
        return False
    try:
        prev = float(bars_5m["close"].iloc[-2])
        last = float(bars_5m["close"].iloc[-1])
    except (TypeError, ValueError, IndexError):
        return False
    return math.isfinite(prev) and math.isfinite(last) and last > prev


def strong_green_candle_1m(
    bars_1m: pd.DataFrame | None,
    *,
    body_frac: float = 0.55,
) -> bool:
    """Last 1m bar: bullish close with body at least *body_frac* of the bar range."""
    if bars_1m is None or getattr(bars_1m, "empty", True):
        return False
    need = {"open", "high", "low", "close"}
    if not need.issubset(set(bars_1m.columns)):
        return False
    row = bars_1m.iloc[-1]
    o = float(row["open"])
    h = float(row["high"])
    low = float(row["low"])
    c = float(row["close"])
    rng = h - low
    if rng <= 0:
        return False
    if c <= o:
        return False
    body = c - o
    try:
        bf = float(body_frac)
    except (TypeError, ValueError):
        bf = 0.55
    bf = max(0.25, min(0.95, bf))
    return (body / rng) >= bf


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _ema_last_from_bars(bars: pd.DataFrame | None, *, span: int) -> float | None:
    if bars is None or getattr(bars, "empty", True) or "close" not in bars.columns:
        return None
    try:
        close_s = bars["close"].astype(float)
    except (TypeError, ValueError):
        return None
    if close_s.empty:
        return None
    value = float(close_s.ewm(span=max(1, int(span)), adjust=False).mean().iloc[-1])
    return value if math.isfinite(value) else None


def _five_min_trend_context(bars_5m: pd.DataFrame | None) -> tuple[float | None, float | None, str]:
    if bars_5m is None or getattr(bars_5m, "empty", True) or "close" not in bars_5m.columns:
        return None, None, "unknown"
    try:
        close_s = bars_5m["close"].astype(float)
    except (TypeError, ValueError):
        return None, None, "unknown"
    if len(close_s) < 2:
        return None, None, "unknown"
    first = float(close_s.iloc[0])
    prev = float(close_s.iloc[-2])
    last = float(close_s.iloc[-1])
    if not (math.isfinite(first) and math.isfinite(prev) and math.isfinite(last)) or first <= 0:
        return None, None, "unknown"
    slope = last - prev
    strength = ((last - first) / first) * 100.0
    if slope > 0:
        direction = "up"
    elif slope < 0:
        direction = "down"
    else:
        direction = "flat"
    return strength, slope, direction


def _atr_from_bars(bars: pd.DataFrame | None) -> float | None:
    if bars is None or getattr(bars, "empty", True):
        return None
    if not {"high", "low", "close"}.issubset(set(bars.columns)):
        return None
    try:
        highs = bars["high"].astype(float)
        lows = bars["low"].astype(float)
        closes = bars["close"].astype(float)
    except (TypeError, ValueError):
        return None
    if len(highs) == 0:
        return None
    ranges: list[float] = []
    prev_close: float | None = None
    for high, low, close in zip(highs, lows, closes, strict=False):
        h = float(high)
        lo = float(low)
        c = float(close)
        if not (math.isfinite(h) and math.isfinite(lo) and math.isfinite(c)):
            prev_close = c if math.isfinite(c) else prev_close
            continue
        if prev_close is None or not math.isfinite(prev_close):
            ranges.append(max(0.0, h - lo))
        else:
            ranges.append(max(h - lo, abs(h - prev_close), abs(lo - prev_close)))
        prev_close = c
    if not ranges:
        return None
    atr = sum(ranges[-14:]) / min(len(ranges), 14)
    return atr if math.isfinite(atr) else None


def _entry_alignment_log_value(value: Any, *, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str):
        return re.sub(r"[^A-Za-z0-9_.:+-]+", "_", value.strip()) or "n/a"
    number = _finite_float(value)
    if number is None:
        return "n/a"
    return f"{number:.{digits}f}"


def _log_entry_alignment_context(
    *,
    symbol: str,
    outcome: str,
    reason: str,
    momentum_score: float | None,
    ema20: float | None,
    ema50: float | None,
    price: float | None,
    vwap: float | None,
    vwap_distance_pct: float | None,
    breakout: bool,
    higher_high: bool,
    orb: bool,
    strong_green: bool,
    trend_strength: float | None,
    slope: float | None,
    five_min_trend_direction: str,
    atr: float | None,
    relative_volume: float | None,
    gain_pct: float | None,
    spread_pct: float | None,
    above_vwap: bool,
    above_ema20: bool | None,
    above_ema50: bool | None,
    volume_confirmation: bool | None,
) -> None:
    ema_distance_values = []
    for ema in (ema20, ema50):
        if price is not None and ema is not None and ema > 0:
            ema_distance_values.append(((price - ema) / ema) * 100.0)
    ema_distance_pct = (
        sum(ema_distance_values) / len(ema_distance_values)
        if ema_distance_values
        else None
    )
    log.info(
        "ENTRY_ALIGNMENT_CONTEXT symbol=%s outcome=%s reason=%s momentum_score=%s ema20=%s ema50=%s "
        "price=%s vwap=%s vwap_distance_pct=%s ema_distance_pct=%s breakout=%s higher_high=%s orb=%s "
        "strong_green=%s trend_strength=%s slope=%s five_min_trend_direction=%s atr=%s "
        "relative_volume=%s gain_pct=%s spread_pct=%s above_vwap=%s above_ema20=%s above_ema50=%s "
        "volume_confirmation=%s",
        _entry_alignment_log_value(symbol, digits=0),
        _entry_alignment_log_value(outcome, digits=0),
        _entry_alignment_log_value(reason, digits=0),
        _entry_alignment_log_value(momentum_score),
        _entry_alignment_log_value(ema20),
        _entry_alignment_log_value(ema50),
        _entry_alignment_log_value(price),
        _entry_alignment_log_value(vwap),
        _entry_alignment_log_value(vwap_distance_pct),
        _entry_alignment_log_value(ema_distance_pct),
        _entry_alignment_log_value(breakout),
        _entry_alignment_log_value(higher_high),
        _entry_alignment_log_value(orb),
        _entry_alignment_log_value(strong_green),
        _entry_alignment_log_value(trend_strength),
        _entry_alignment_log_value(slope),
        _entry_alignment_log_value(five_min_trend_direction, digits=0),
        _entry_alignment_log_value(atr),
        _entry_alignment_log_value(relative_volume),
        _entry_alignment_log_value(gain_pct),
        _entry_alignment_log_value(spread_pct),
        _entry_alignment_log_value(above_vwap),
        _entry_alignment_log_value(above_ema20),
        _entry_alignment_log_value(above_ema50),
        _entry_alignment_log_value(volume_confirmation),
    )


def dynamic_momentum_entry_passes(
    *,
    gain_pct: float | None,
    relative_volume: float | None,
    vwap_above: bool,
    spread_pct: float | None,
    bars_1m: pd.DataFrame | None,
    bars_5m: pd.DataFrame | None,
    ref_price: float,
    cfg: Mapping[str, Any] | None = None,
    session_date: date | None = None,
    ai_catalyst_score: float | None = None,
    symbol: str = "",
    news_score: int = 0,
    event_score: float = 0.0,
    catalyst_score: float = 0.0,
    catalyst_age_minutes: float | None = None,
    premarket_rank: int | float | str | None = None,
    current_time: datetime | None = None,
    is_dynamic: bool = False,
    quote_unstable: bool = False,
    alignment_score: float | None = None,
) -> tuple[bool, str]:
    """
    Live-loop gate for **dynamic** (scanner-added) names.

    Requires (defaults match operator policy):

    * ``gain_pct`` >= ``min_day_gain_pct`` (default 15)
    * ``relative_volume`` >= ``min_relative_volume`` (default 2)
    * ``vwap_above`` if ``require_above_vwap`` (default true)
    * ``spread_pct`` < ``max_entry_spread_pct`` (default 3)

    And **at least one** of: 5m breakout, new intraday high (1m session high), strong green 1m candle,
    or (when enabled) opening-range breakout above the first-N-minute RTH high.

    *session_date* — ET calendar date for opening-range windows (defaults inferred from 1m bars).
    """
    c = dict(cfg) if isinstance(cfg, Mapping) else {}
    aggressive_cfg = c.get("aggressive_mode") if isinstance(c.get("aggressive_mode"), Mapping) else {}
    aggressive_enabled = bool(aggressive_cfg.get("enabled", False))
    sym = str(symbol or "").strip().upper()
    try:
        min_gain = float(c.get("min_day_gain_pct", 15.0))
    except (TypeError, ValueError):
        min_gain = 15.0
    try:
        min_rel = float(c.get("min_relative_volume", 2.0))
    except (TypeError, ValueError):
        min_rel = 2.0
    adaptive_state = resolve_adaptive_sensitivity(
        c,
        metrics=c.get("adaptive_metrics") if isinstance(c.get("adaptive_metrics"), Mapping) else None,
        context=c.get("adaptive_context") if isinstance(c.get("adaptive_context"), Mapping) else None,
        base_min_rvol=min_rel,
    )
    if adaptive_state.enabled:
        log.info(
            "DYNAMIC_ENTRY_ADAPTIVE_CONFIG enabled=true mode=%s quality_threshold=%.2f rvol_threshold=%.3f max_vwap_distance_atr=%.2f one_minor_rule_exception=%s relaxed_size_multiplier=%.2f final_status=active",
            adaptive_state.mode,
            adaptive_state.effective_quality_score,
            adaptive_state.effective_rvol,
            adaptive_state.max_vwap_distance_atr,
            str(adaptive_state.one_minor_rule_exception).lower(),
            adaptive_state.relaxed_size_multiplier,
        )
        if adaptive_state.mode == "relaxed" and adaptive_state.effective_rvol < min_rel:
            log.info(
                "DYNAMIC_ENTRY_SENSITIVITY_CHANGE from=normal to=relaxed reason=%s lookback_days=%d trades_per_day=%s win_rate=%s drawdown=%s",
                adaptive_state.reason,
                adaptive_state.lookback_trading_days,
                "n/a" if adaptive_state.trades_per_day is None else "%.3f" % float(adaptive_state.trades_per_day),
                "n/a" if adaptive_state.win_rate is None else "%.3f" % float(adaptive_state.win_rate),
                "n/a" if adaptive_state.drawdown_pct is None else "%.3f" % float(adaptive_state.drawdown_pct),
            )
            min_rel = float(adaptive_state.effective_rvol)
    if aggressive_enabled:
        try:
            min_gain = float(aggressive_cfg.get("minimum_day_gain_pct", c.get("min_day_gain_pct", min_gain)) or min_gain)
        except (TypeError, ValueError):
            pass
        try:
            min_rel = min(float(min_rel), float(aggressive_cfg.get("minimum_relative_volume", 0.75) or 0.75))
        except (TypeError, ValueError):
            min_rel = min(float(min_rel), 0.75)
        c["require_above_vwap"] = False
        c["require_5m_trend_alignment"] = False
    try:
        max_spread = float(c.get("max_entry_spread_pct", 3.0))
    except (TypeError, ValueError):
        max_spread = 3.0
    if aggressive_enabled:
        tier_caps = aggressive_cfg.get("max_spread_by_tier") if isinstance(aggressive_cfg.get("max_spread_by_tier"), Mapping) else {}
        try:
            price_for_tier = float(ref_price)
        except (TypeError, ValueError):
            price_for_tier = math.nan
        tier_key = "low_price" if math.isfinite(price_for_tier) and price_for_tier < 5.0 else "normal"
        try:
            max_spread = max(max_spread, float(tier_caps.get(tier_key, 5.0 if tier_key == "low_price" else 3.0) or max_spread))
        except (TypeError, ValueError):
            pass
    ai_cfg = c.get("ai_catalyst") if isinstance(c.get("ai_catalyst"), Mapping) else {}
    try:
        ai_block_below = float(ai_cfg.get("block_below_score", 45.0))
    except (TypeError, ValueError):
        ai_block_below = 45.0
    try:
        ai_boost_at = float(ai_cfg.get("boost_at_score", 70.0))
    except (TypeError, ValueError):
        ai_boost_at = 70.0
    try:
        ai_boost_factor = float(ai_cfg.get("boost_threshold_factor", 0.90))
    except (TypeError, ValueError):
        ai_boost_factor = 0.90
    ai_boost_factor = max(0.50, min(1.0, ai_boost_factor))
    ai_score = None
    if ai_catalyst_score is not None:
        try:
            ai_score = max(0.0, min(100.0, float(ai_catalyst_score)))
        except (TypeError, ValueError):
            ai_score = None
    score_for_alignment = None
    for raw_score in (alignment_score, ai_score):
        if raw_score is None:
            continue
        try:
            score_for_alignment = float(raw_score)
            break
        except (TypeError, ValueError):
            continue
    if ai_score is not None and ai_score >= ai_boost_at:
        min_gain *= ai_boost_factor
        min_rel *= ai_boost_factor
    require_vwap = bool(c.get("require_above_vwap", True))
    try:
        green_frac = float(c.get("strong_green_body_frac", 0.55))
    except (TypeError, ValueError):
        green_frac = 0.55
    strong_news = bool(
        is_dynamic
        and int(news_score or 0) >= _STRONG_NEWS_OVERRIDE_MIN_SCORE
        and catalyst_age_minutes is not None
        and float(catalyst_age_minutes) <= _STRONG_NEWS_OVERRIDE_MAX_CATALYST_AGE_MIN
    )
    try:
        catalyst_backed = is_dynamic and float(catalyst_score or 0.0) >= 0.60
    except (TypeError, ValueError):
        catalyst_backed = False
    try:
        event_score_f = float(event_score or 0.0)
    except (TypeError, ValueError):
        event_score_f = 0.0
    try:
        catalyst_score_f = float(catalyst_score or 0.0)
    except (TypeError, ValueError):
        catalyst_score_f = 0.0
    premarket_rank_i = _coerce_premarket_rank(premarket_rank)

    orb_root = c.get("opening_range_breakout")
    orb_cfg = orb_root if isinstance(orb_root, dict) else {}
    orb_enabled = bool(orb_cfg.get("enabled", True))
    try:
        orb_minutes = int(orb_cfg.get("minutes", 15))
    except (TypeError, ValueError):
        orb_minutes = 15
    orb_minutes = max(1, min(120, orb_minutes))
    brk = five_min_breakout_from_bars(bars_5m, ref_price)
    five_min_trend = five_min_trend_positive_from_bars(bars_5m)
    nh = new_intraday_high_from_1m(bars_1m, ref_price)
    sg = strong_green_candle_1m(bars_1m, body_frac=green_frac)
    orb = False
    if orb_enabled:
        orb = opening_range_breakout_above(
            bars_1m,
            ref_price,
            minutes=orb_minutes,
            session_date=session_date,
        )
    price_context = _finite_float(ref_price)
    vwap_context = session_vwap_from_bars(bars_1m)
    vwap_distance_context = dynamic_entry_vwap_extension_pct(ref_price, vwap_context)
    ema20_context = _ema_last_from_bars(bars_1m, span=20)
    ema50_context = _ema_last_from_bars(bars_1m, span=50)
    trend_strength_context, slope_context, trend_direction_context = _five_min_trend_context(bars_5m)
    atr_context = _atr_from_bars(bars_5m if bars_5m is not None else bars_1m)
    above_ema20_context = (
        None
        if price_context is None or ema20_context is None
        else price_context > ema20_context
    )
    above_ema50_context = (
        None
        if price_context is None or ema50_context is None
        else price_context > ema50_context
    )
    volume_confirmation_context = (
        None
        if relative_volume is None or _finite_float(relative_volume) is None
        else float(relative_volume) >= 1.0
    )
    vwap_guard_ok = bool(vwap_above)

    def finish(ok: bool, reason: str) -> tuple[bool, str]:
        _log_entry_alignment_context(
            symbol=sym or "",
            outcome="pass" if ok else "fail",
            reason=reason,
            momentum_score=score_for_alignment,
            ema20=ema20_context,
            ema50=ema50_context,
            price=price_context,
            vwap=vwap_context,
            vwap_distance_pct=vwap_distance_context,
            breakout=brk,
            higher_high=nh,
            orb=orb,
            strong_green=sg,
            trend_strength=trend_strength_context,
            slope=slope_context,
            five_min_trend_direction=trend_direction_context,
            atr=atr_context,
            relative_volume=_finite_float(relative_volume),
            gain_pct=_finite_float(gain_pct),
            spread_pct=_finite_float(spread_pct),
            above_vwap=bool(vwap_guard_ok),
            above_ema20=above_ema20_context,
            above_ema50=above_ema50_context,
            volume_confirmation=volume_confirmation_context,
        )
        return ok, reason

    if is_dynamic and flexible_entries_enabled(c) and not aggressive_enabled:
        readiness = dynamic_feature_readiness(
            bars_1m_count=0 if bars_1m is None else len(bars_1m),
            bars_5m_count=0 if bars_5m is None else len(bars_5m),
            vwap=vwap_context,
            ema20=ema20_context,
            ema50=ema50_context,
            atr=atr_context,
            momentum_score=score_for_alignment,
            trend_5m=trend_direction_context,
            config=c,
        )
        log.info(
            "DYNAMIC_FEATURE_READINESS symbol=%s bars_1m=%s bars_5m=%s vwap_ready=%s ema20_ready=%s ema50_ready=%s atr_ready=%s momentum_ready=%s trend_5m_ready=%s final_status=%s",
            sym or "",
            readiness.get("bars_1m"),
            readiness.get("bars_5m"),
            str(bool(readiness.get("vwap_ready"))).lower(),
            str(bool(readiness.get("ema20_ready"))).lower(),
            str(bool(readiness.get("ema50_ready"))).lower(),
            str(bool(readiness.get("atr_ready"))).lower(),
            str(bool(readiness.get("momentum_ready"))).lower(),
            str(bool(readiness.get("trend_5m_ready"))).lower(),
            readiness.get("final_status"),
        )
        if readiness.get("final_status") != "ready":
            missing = ",".join(str(item) for item in readiness.get("missing_features") or [])
            log.info(
                "ENTRY_ALIGNMENT_REJECT symbol=%s classification=data_quality_block missing_features=%s",
                sym or "",
                missing or "unknown",
            )
            return finish(False, "feature_unavailable missing_features=%s" % (missing or "unknown"))

    if ai_score is not None and ai_score < ai_block_below:
        return finish(False, "ai_catalyst_score %.0f < %.0f" % (ai_score, ai_block_below))
    catalyst_fastlane_requested = bool(c.get("catalyst_fastlane_active", False))
    catalyst_fastlane_threshold = 0.35
    try:
        catalyst_fastlane_threshold = max(
            0.0,
            float(c.get("catalyst_min_relative_volume", catalyst_fastlane_threshold) or catalyst_fastlane_threshold),
        )
    except (TypeError, ValueError):
        catalyst_fastlane_threshold = 0.35
    try:
        catalyst_fastlane_age = float(catalyst_age_minutes)
    except (TypeError, ValueError):
        catalyst_fastlane_age = math.inf
    catalyst_fastlane_strong = bool(
        catalyst_fastlane_requested
        and (
            int(news_score or 0) >= 7
            or event_score_f >= 7.0
            or catalyst_score_f >= 0.7
        )
        and math.isfinite(catalyst_fastlane_age)
        and catalyst_fastlane_age <= 300.0
    )
    catalyst_fastlane_momentum_confirm = bool(vwap_above or sg or five_min_trend or orb)
    catalyst_fastlane_reason = "not_requested"
    if catalyst_fastlane_requested:
        if not catalyst_fastlane_strong:
            catalyst_fastlane_reason = "weak_or_stale_catalyst"
        elif quote_unstable:
            catalyst_fastlane_reason = "unstable_quote"
        elif relative_volume is None or not math.isfinite(float(relative_volume)):
            catalyst_fastlane_reason = "relative_volume_unavailable"
        elif float(relative_volume) < catalyst_fastlane_threshold:
            catalyst_fastlane_reason = "relative_volume_below_catalyst_threshold"
        elif spread_pct is None or not math.isfinite(float(spread_pct)):
            catalyst_fastlane_reason = "spread_unavailable"
        elif float(spread_pct) >= max_spread:
            catalyst_fastlane_reason = "spread_too_wide"
        elif not catalyst_fastlane_momentum_confirm:
            catalyst_fastlane_reason = "no_momentum_confirmation"
        else:
            catalyst_fastlane_reason = "ok"
        log.info(
            "CATALYST_FASTLANE_CHECK symbol=%s news_score=%d event_score=%.2f catalyst_score=%.2f "
            "rel_volume=%s spread=%s vwap=%s momentum_confirm=%s eligible=%s reason=%s",
            sym or "",
            int(news_score or 0),
            float(event_score_f),
            float(catalyst_score_f),
            "n/a" if relative_volume is None else f"{float(relative_volume):.3f}",
            "n/a" if spread_pct is None else f"{float(spread_pct):.3f}",
            str(bool(vwap_above)).lower(),
            str(bool(catalyst_fastlane_momentum_confirm)).lower(),
            str(catalyst_fastlane_reason == "ok").lower(),
            catalyst_fastlane_reason,
        )
        if catalyst_fastlane_strong:
            if quote_unstable:
                return finish(False, "catalyst_fastlane unstable_quote")
            if spread_pct is None or not math.isfinite(float(spread_pct)):
                return finish(False, "spread_pct unavailable")
            if float(spread_pct) >= max_spread:
                return finish(
                    False,
                    "spread_pct %.3f%% >= %.2f%%" % (float(spread_pct), max_spread),
                )
            if not catalyst_fastlane_momentum_confirm:
                return finish(False, "catalyst_fastlane no_momentum_confirmation")
            if relative_volume is None or not math.isfinite(float(relative_volume)):
                return finish(False, "relative_volume unavailable")
            if float(relative_volume) < catalyst_fastlane_threshold:
                return finish(
                    False,
                    "relative_volume %.2f < %.2f" % (float(relative_volume), catalyst_fastlane_threshold),
                )

    if gain_pct is None or not math.isfinite(float(gain_pct)):
        return finish(False, "day gain_pct unavailable")

    if relative_volume is None or not math.isfinite(float(relative_volume)):
        return finish(False, "relative_volume unavailable")
    effective_min_rel = float(min_rel)
    catalyst_entry_signal = bool(
        (premarket_rank_i is not None and premarket_rank_i <= 10)
        or int(news_score or 0) >= 8
        or catalyst_score_f >= 0.70
    )
    catalyst_fastlane_ok = catalyst_fastlane_reason == "ok"
    if catalyst_fastlane_ok:
        effective_min_rel = min(float(min_rel), float(catalyst_fastlane_threshold))
        log.info(
            "CATALYST_FASTLANE_RVOL_THRESHOLD symbol=%s threshold=%.2f",
            sym or "",
            float(catalyst_fastlane_threshold),
        )
    elif is_dynamic and strong_news:
        effective_min_rel = min(float(min_rel), 0.75)
    elif catalyst_backed and not (current_time is not None and catalyst_entry_signal):
        effective_min_rel = min(float(min_rel), float(min_rel) * 0.95)
    catalyst_entry_window = _catalyst_entry_rvol_relax_window_active(current_time)
    catalyst_entry_rvol_relaxed = bool(is_dynamic and catalyst_entry_signal and catalyst_entry_window)
    if catalyst_entry_rvol_relaxed and effective_min_rel > _CATALYST_ENTRY_RVOL_RELAX_MIN_REL_VOLUME:
        old_min_rel = float(effective_min_rel)
        effective_min_rel = float(_CATALYST_ENTRY_RVOL_RELAX_MIN_REL_VOLUME)
        log.info(
            "CATALYST_ENTRY_RVOL_RELAXED symbol=%s rel=%.3f old_min=%.2f new_min=%.2f "
            "news_score=%d catalyst_score=%.2f premarket_rank=%s time=%s",
            sym or "",
            float(relative_volume),
            old_min_rel,
            float(effective_min_rel),
            int(news_score or 0),
            float(catalyst_score_f),
            "n/a" if premarket_rank_i is None else str(int(premarket_rank_i)),
            "n/a" if current_time is None else current_time.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M:%S%z"),
        )
    elif is_dynamic and catalyst_entry_signal and not catalyst_entry_window and float(relative_volume) < float(effective_min_rel):
        log.info(
            "CATALYST_ENTRY_RVOL_RELAX_BLOCKED symbol=%s reason=outside_window",
            sym or "",
        )
    if is_dynamic and not catalyst_fastlane_ok and not catalyst_entry_rvol_relaxed:
        adaptive_min_rel, adaptive_used, adaptive_reason = dynamic_adaptive_volume_min_relative_volume(
            base_min_relative_volume=float(effective_min_rel),
            cfg=c,
            current_time=current_time,
            is_dynamic=True,
            gain_pct=gain_pct,
            alignment_score=score_for_alignment,
            momentum_confirmed=bool(brk or nh or sg or orb or vwap_guard_ok),
        )
        if adaptive_used:
            log.info(
                "DYNAMIC_ADAPTIVE_VOLUME_CHECK symbol=%s base_min=%.3f effective_min=%.3f "
                "relative_volume=%.3f score=%s gain_pct=%.2f reason=%s",
                sym or "",
                float(effective_min_rel),
                float(adaptive_min_rel),
                float(relative_volume),
                "n/a" if score_for_alignment is None else "%.2f" % float(score_for_alignment),
                float(gain_pct),
                adaptive_reason,
            )
            effective_min_rel = float(adaptive_min_rel)
    if is_dynamic:
        rvol_allowed = float(relative_volume) >= effective_min_rel
        if not rvol_allowed:
            reason = "relative_volume %.2f < %.2f" % (float(relative_volume), effective_min_rel)
        else:
            reason = "relative_volume %.2f >= %.2f" % (float(relative_volume), effective_min_rel)
        log.info(
            "DYNAMIC_RVOL_GUARD symbol=%s rel_volume=%.3f base_min=%.3f effective_min=%.3f news_score=%d catalyst_score=%.2f allowed=%s reason=%s",
            sym or "",
            float(relative_volume),
            float(min_rel),
            float(effective_min_rel),
            int(news_score or 0),
            float(catalyst_score or 0.0),
            str(bool(rvol_allowed)).lower(),
            reason,
        )
        if not rvol_allowed:
            if catalyst_entry_signal:
                log.info(
                    "CATALYST_ENTRY_RVOL_RELAX_BLOCKED symbol=%s reason=relative_volume_below_floor",
                    sym or "",
                )
            return finish(False, "relative_volume %.2f < %.2f" % (float(relative_volume), effective_min_rel))
    elif float(relative_volume) < min_rel:
        return finish(False, "relative_volume %.2f < %.2f" % (float(relative_volume), min_rel))

    vwap_guard_ok = bool(vwap_above)
    if require_vwap and not vwap_guard_ok and strong_news:
        vwap = session_vwap_from_bars(bars_1m)
        distance_pct_s = None
        allowed = False
        reason = "price not above session VWAP"
        if vwap is None or not math.isfinite(float(vwap)) or float(vwap) <= 0:
            reason = "session_vwap unavailable"
        else:
            try:
                px = float(ref_price)
            except (TypeError, ValueError):
                px = math.nan
            if math.isfinite(px):
                distance_pct_s = ((px - float(vwap)) / float(vwap)) * 100.0
                within_half_pct_below = -0.5 <= float(distance_pct_s) <= 0.0
                reclaimed = vwap_reclaim_within_last_bars(
                    bars_1m,
                    ref_price,
                    bars=_STRONG_NEWS_VWAP_RECLAIM_BARS,
                )
                allowed = bool(px > float(vwap) or within_half_pct_below or reclaimed)
                if px > float(vwap):
                    reason = "price above session VWAP"
                elif within_half_pct_below:
                    reason = "within 0.5% below VWAP"
                elif reclaimed:
                    reason = "VWAP reclaimed within last %d bars" % _STRONG_NEWS_VWAP_RECLAIM_BARS
            else:
                reason = "price not above session VWAP"
        log.info(
            "DYNAMIC_VWAP_GUARD symbol=%s price=%.4f vwap=%s distance_pct=%s news_score=%d allowed=%s reason=%s",
            sym or "",
            float(ref_price),
            "n/a" if vwap is None else f"{float(vwap):.4f}",
            "n/a" if distance_pct_s is None or not math.isfinite(float(distance_pct_s)) else f"{float(distance_pct_s):.3f}",
            int(news_score or 0),
            str(bool(allowed)).lower(),
            reason,
        )
        vwap_guard_ok = allowed
    if require_vwap and not vwap_guard_ok and not catalyst_fastlane_ok:
        if catalyst_entry_signal and catalyst_entry_rvol_relaxed:
            log.info(
                "CATALYST_ENTRY_RVOL_RELAX_BLOCKED symbol=%s reason=vwap",
                sym or "",
            )
        return finish(False, "price not above session VWAP")

    if spread_pct is None or not math.isfinite(float(spread_pct)):
        if catalyst_entry_signal and catalyst_entry_rvol_relaxed:
            log.info(
                "CATALYST_ENTRY_RVOL_RELAX_BLOCKED symbol=%s reason=spread_unavailable",
                sym or "",
            )
        return finish(False, "spread_pct unavailable")
    if quote_unstable:
        return finish(False, "unstable_quote")
    if float(spread_pct) >= max_spread:
        if catalyst_entry_signal and catalyst_entry_rvol_relaxed:
            log.info(
                "CATALYST_ENTRY_RVOL_RELAX_BLOCKED symbol=%s reason=spread_too_wide",
                sym or "",
            )
        return finish(
            False,
            "spread_pct %.3f%% >= %.2f%%" % (float(spread_pct), max_spread),
        )
    if aggressive_enabled and is_dynamic:
        gain_f = float(gain_pct)
        rel_f = float(relative_volume)
        fast_lane = bool(
            int(news_score or 0) >= 3
            or catalyst_score_f >= 0.25
            or event_score_f >= 2.0
            or (rel_f >= 2.0 and gain_f >= 4.0)
            or (rel_f >= 4.0 and gain_f >= 2.0)
            or gain_f >= 8.0
        )
        catalyst_points = min(25.0, max(float(news_score or 0) / 5.0, catalyst_score_f / 0.60, event_score_f / 3.0) * 25.0)
        momentum_points = 20.0 if bool(brk or nh or sg or orb or five_min_trend or vwap_guard_ok) else 0.0
        rvol_points = min(15.0, max(0.0, rel_f / 2.0) * 15.0)
        gain_points = min(10.0, max(0.0, gain_f / 8.0) * 10.0)
        trend_points = 10.0 if five_min_trend else 5.0
        structure_points = 10.0 if bool(brk or nh or sg or orb) else 0.0
        symbol_vwap_points = 5.0 if vwap_guard_ok else 0.0
        market_points = 0.0
        score = catalyst_points + momentum_points + rvol_points + gain_points + trend_points + structure_points + symbol_vwap_points + market_points
        threshold = float(aggressive_cfg.get("fast_lane_threshold", 50.0) if fast_lane else aggressive_cfg.get("normal_threshold", 60.0))
        zero_count = sum(
            1
            for value in (
                catalyst_points,
                momentum_points,
                rvol_points,
                gain_points,
                structure_points,
                symbol_vwap_points,
                market_points,
            )
            if value <= 1e-9
        )
        max_failures = int(float(aggressive_cfg.get("max_noncritical_failures", 3) or 3))
        primary = bool(fast_lane or momentum_points > 0 or rvol_points >= 7.5 or gain_points >= 5.0)
        if primary and score >= threshold - 1e-9 and zero_count <= max_failures:
            log.info(
                "DYNAMIC_AGGRESSIVE_ENTRY_ACCEPT symbol=%s score=%.2f threshold=%.2f fast_lane=%s trigger=%s zero_factors=%d bypassed_noncritical_rules=%s",
                sym or "",
                float(score),
                float(threshold),
                str(bool(fast_lane)).lower(),
                "fast_lane" if fast_lane else "score",
                int(zero_count),
                "market_vwap,sector,pullback,breakout",
            )
            return finish(True, "ok aggressive_dynamic score=%.2f threshold=%.2f fast_lane=%s" % (float(score), float(threshold), str(bool(fast_lane)).lower()))
        log.info(
            "DYNAMIC_AGGRESSIVE_ENTRY_REJECT symbol=%s score=%.2f threshold=%.2f fast_lane=%s primary=%s zero_factors=%d reason=%s",
            sym or "",
            float(score),
            float(threshold),
            str(bool(fast_lane)).lower(),
            str(bool(primary)).lower(),
            int(zero_count),
            "too_many_noncritical_failures" if zero_count > max_failures else ("no_primary_trigger" if not primary else "score_below_threshold"),
        )
    if high_momentum_bypass_ok(
        gain_pct=gain_pct,
        relative_volume=relative_volume,
        vwap_above=vwap_above,
        spread_pct=spread_pct,
        cfg=c,
    ):
        return finish(True, "ok high_momentum_bypass")

    if float(gain_pct) < min_gain:
        return finish(False, "gain_pct %.2f%% < %.2f%%" % (float(gain_pct), min_gain))

    if catalyst_fastlane_ok:
        return finish(True, "ok catalyst_fastlane")

    vwap_score_cfg = c.get("vwap_score_alignment")
    vwap_score_cfg = vwap_score_cfg if isinstance(vwap_score_cfg, Mapping) else {}
    vwap_score_enabled = bool(vwap_score_cfg.get("enabled", False))
    try:
        vwap_score_min = float(vwap_score_cfg.get("min_score", 80.0) or 80.0)
    except (TypeError, ValueError):
        vwap_score_min = 80.0
    try:
        vwap_gain_min = float(vwap_score_cfg.get("min_day_gain_pct", 15.0) or 15.0)
    except (TypeError, ValueError):
        vwap_gain_min = 15.0
    score_pass = bool(score_for_alignment is not None and float(score_for_alignment) >= vwap_score_min)
    gain_pass = bool(float(gain_pct) >= vwap_gain_min)
    if vwap_score_enabled and is_dynamic and bool(vwap_guard_ok) and (score_pass or gain_pass):
        log.info(
            "DYNAMIC_ALIGNMENT_PASS_VWAP_SCORE symbol=%s effective_min_rel=%.3f score=%s day_gain_pct=%.2f vwap_above=%s score_pass=%s gain_pass=%s",
            sym or "",
            float(effective_min_rel),
            "n/a" if score_for_alignment is None else "%.2f" % float(score_for_alignment),
            float(gain_pct),
            str(bool(vwap_guard_ok)).lower(),
            str(bool(score_pass)).lower(),
            str(bool(gain_pass)).lower(),
        )
        return finish(True, "ok vwap_score_alignment")

    if is_dynamic:
        bypass_ok, _bypass_reason = news_dynamic_entry_bypass_passes(
            symbol=symbol,
            news_score=int(news_score or 0),
            relative_volume=relative_volume,
            price_above_vwap=bool(vwap_guard_ok),
            spread_pct=spread_pct,
            quote_unstable=bool(quote_unstable),
            is_dynamic=True,
            min_relative_volume=effective_min_rel,
            cfg=c,
        )
        if bypass_ok:
            return finish(True, "ok news_catalyst")

    if brk or nh or sg or orb:
        return finish(True, "ok")
    vwap_distance_atr = None
    if price_context is not None and vwap_context is not None and atr_context is not None:
        try:
            if float(atr_context) > 0:
                vwap_distance_atr = (float(price_context) - float(vwap_context)) / float(atr_context)
        except (TypeError, ValueError):
            vwap_distance_atr = None
    setup = classify_setup(
        breakout=bool(brk),
        higher_high=bool(nh),
        strong_green=bool(sg),
        orb=bool(orb),
        price_above_vwap=bool(vwap_guard_ok),
        five_min_trend=bool(five_min_trend),
        vwap_distance_atr=vwap_distance_atr,
    )
    flexible_setup = classify_flexible_setup(
        price_above_vwap=bool(vwap_guard_ok),
        five_min_trend=bool(five_min_trend),
        vwap_distance_atr=vwap_distance_atr,
        momentum_score=score_for_alignment,
        volume_confirmation=bool(volume_confirmation_context),
        higher_low=bool(above_ema20_context and above_ema50_context),
        consolidation_break=False,
    )
    quality_for_exception = score_for_alignment
    if quality_for_exception is None:
        quality_for_exception = 100.0 if float(gain_pct) >= min_gain and float(relative_volume) >= effective_min_rel else 0.0
    failed_minor = approved_minor_rule("entry_alignment")
    exception_ok, exception_rule = one_minor_rule_exception_allowed(
        adaptive_state,
        failed_rules=[failed_minor] if failed_minor else [],
        hard_rules=[],
        quality_score=quality_for_exception,
    )
    if flexible_setup != "none" and flexible_setup_enabled(c, flexible_setup):
        minor_exception = bool(exception_ok)
        size_mult = flexible_size_multiplier(c, flexible_setup, minor_exception=minor_exception)
        exception_rule_out = exception_rule if minor_exception else None
        log.info(
            "DYNAMIC_ENTRY_FLEXIBLE_ACCEPT symbol=%s setup=%s quality_score=%.2f normal_threshold=%.2f effective_threshold=%.2f minor_rule_exception=%s failed_minor_rule=%s size_multiplier=%.2f",
            sym or "",
            flexible_setup,
            float(quality_for_exception),
            float(adaptive_state.normal_min_quality_score),
            float(adaptive_state.effective_quality_score),
            str(minor_exception).lower(),
            exception_rule_out or "none",
            float(size_mult),
        )
        return finish(
            True,
            "ok flexible_setup=%s minor_rule_exception=%s failed_minor_rule=%s size_multiplier=%.2f"
            % (flexible_setup, str(minor_exception).lower(), exception_rule_out or "none", size_mult),
        )
    if setup in {"vwap_reclaim", "ema9_or_ema20_pullback", "higher_low_continuation"} and exception_ok:
        size_mult = dynamic_size_multiplier(adaptive_state, exception=True)
        log.info(
            "DYNAMIC_ENTRY_RELAXED_ACCEPT symbol=%s setup=%s quality_score=%.2f normal_threshold=%.2f effective_threshold=%.2f failed_minor_rule=%s size_multiplier=%.2f",
            sym or "",
            setup,
            float(quality_for_exception),
            float(adaptive_state.normal_min_quality_score),
            float(adaptive_state.effective_quality_score),
            exception_rule or "entry_alignment",
            float(size_mult),
        )
        return finish(True, "ok adaptive_relaxed %s minor_rule=%s size_multiplier=%.2f" % (setup, exception_rule or "entry_alignment", size_mult))
    log.info(
        "DYNAMIC_ALIGNMENT_REJECT symbol=%s effective_min_rel=%.3f score=%s day_gain_pct=%.2f vwap_above=%s breakout=%s new_high=%s strong_green=%s orb=%s",
        sym or "",
        float(effective_min_rel),
        "n/a" if score_for_alignment is None else "%.2f" % float(score_for_alignment),
        float(gain_pct),
        str(bool(vwap_guard_ok)).lower(),
        str(bool(brk)).lower(),
        str(bool(nh)).lower(),
        str(bool(sg)).lower(),
        str(bool(orb)).lower(),
    )
    return finish(
        False,
        "need 5m breakout OR new intraday high OR strong green 1m OR opening-range breakout "
        "(got breakout=%s nh=%s green=%s orb=%s)" % (brk, nh, sg, orb),
    )


def compute_intraday_momentum_score(
    *,
    relative_volume: float | None,
    gain_pct: float | None,
    five_min_breakout: bool,
    distance_from_vwap_pct: float,
    cfg: Mapping[str, Any] | None = None,
) -> tuple[float, dict[str, float]]:
    """
    Weighted intraday momentum score for ranking scanner-added names.

    Each term is normalized to ~[0, 1] then multiplied by its weight (defaults 0.3 / 0.3 / 0.2 / 0.2):

    * relative volume vs ``normalize.rel_volume_max`` (default 5× avg)
    * day gain %% vs ``normalize.gain_pct_max`` (default 50)
    * 5m breakout flag (0 or 1)
    * positive distance above session VWAP %% vs ``normalize.vwap_distance_pct_max`` (default 15)
    """
    dme = dict(cfg) if isinstance(cfg, Mapping) else {}
    ms = dme.get("momentum_score") if isinstance(dme.get("momentum_score"), dict) else {}
    wmap = ms.get("weights") if isinstance(ms.get("weights"), dict) else {}
    try:
        w_rel = float(wmap.get("rel_volume", 0.3))
    except (TypeError, ValueError):
        w_rel = 0.3
    try:
        w_gain = float(wmap.get("gain_pct", 0.3))
    except (TypeError, ValueError):
        w_gain = 0.3
    try:
        w_brk = float(wmap.get("five_min_breakout", 0.2))
    except (TypeError, ValueError):
        w_brk = 0.2
    try:
        w_vwap = float(wmap.get("vwap_distance", 0.2))
    except (TypeError, ValueError):
        w_vwap = 0.2

    norm = ms.get("normalize") if isinstance(ms.get("normalize"), dict) else {}
    try:
        rv_cap = float(norm.get("rel_volume_max", 5.0))
    except (TypeError, ValueError):
        rv_cap = 5.0
    try:
        gg_cap = float(norm.get("gain_pct_max", 50.0))
    except (TypeError, ValueError):
        gg_cap = 50.0
    try:
        vd_cap = float(norm.get("vwap_distance_pct_max", 15.0))
    except (TypeError, ValueError):
        vd_cap = 15.0
    rv_cap = max(1e-9, rv_cap)
    gg_cap = max(1e-9, gg_cap)
    vd_cap = max(1e-9, vd_cap)

    try:
        rv_raw = float(relative_volume) if relative_volume is not None else 0.0
    except (TypeError, ValueError):
        rv_raw = 0.0
    if not math.isfinite(rv_raw):
        rv_raw = 0.0
    rv_n = max(0.0, min(rv_raw / rv_cap, 1.0))

    try:
        gg_raw = float(gain_pct) if gain_pct is not None else 0.0
    except (TypeError, ValueError):
        gg_raw = 0.0
    if not math.isfinite(gg_raw):
        gg_raw = 0.0
    gg_n = max(0.0, min(gg_raw / gg_cap, 1.0))

    brk_n = 1.0 if five_min_breakout else 0.0

    try:
        vd_raw = float(distance_from_vwap_pct)
    except (TypeError, ValueError):
        vd_raw = 0.0
    if not math.isfinite(vd_raw):
        vd_raw = 0.0
    vd_pos = max(0.0, vd_raw)
    vd_n = max(0.0, min(vd_pos / vd_cap, 1.0))

    score = (
        w_rel * rv_n + w_gain * gg_n + w_brk * brk_n + w_vwap * vd_n
    )
    breakdown = {
        "rel_volume_norm": rv_n,
        "gain_pct_norm": gg_n,
        "five_min_breakout": brk_n,
        "vwap_distance_norm": vd_n,
        "score": score,
    }
    return (score, breakdown)


def pick_top_n_momentum_symbols(
    ranked: Sequence[tuple[str, float]],
    *,
    top_n: int,
) -> frozenset[str]:
    """Return symbols with the highest momentum scores (ties broken by symbol name)."""
    if top_n <= 0:
        return frozenset()
    pairs = sorted(ranked, key=lambda x: (-x[1], x[0]))
    return frozenset(p[0] for p in pairs[:top_n])


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float(default)
    return out if math.isfinite(out) else float(default)


def _call_get_bars(market_client: Any, symbol: str, *, timeframe: str, limit: int) -> pd.DataFrame | None:
    get_bars = getattr(market_client, "get_bars", None)
    if get_bars is None:
        return None
    try:
        bars = get_bars(symbol, timeframe=timeframe, limit=limit)
    except TypeError:
        try:
            bars = get_bars(symbol, timeframe, limit)
        except Exception:
            return None
    except Exception:
        return None
    if bars is None or getattr(bars, "empty", True):
        return None
    return bars


def _snapshot_symbol_from_row(row: Any) -> str:
    if isinstance(row, Mapping):
        return str(row.get("symbol") or "").strip().upper()
    return str(getattr(row, "symbol", "") or "").strip().upper()


def _snapshot_row_to_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    return {
        "symbol": getattr(row, "symbol", None),
        "price": getattr(row, "price", None),
        "day_gain_pct": getattr(row, "day_gain_pct", None),
        "volume": getattr(row, "volume", None),
        "bid": getattr(row, "bid", None),
        "ask": getattr(row, "ask", None),
    }


def _call_get_snapshots_batch(
    market_client: Any,
    symbols: Sequence[str],
) -> dict[str, dict[str, Any]]:
    syms = [
        str(s).strip().upper()
        for s in symbols
        if str(s).strip() and not is_option_symbol(str(s).strip().upper())
    ]
    for method_name in ("get_snapshots", "get_snapshots_batch", "get_snapshot_batch"):
        method = getattr(market_client, method_name, None)
        if method is None:
            continue
        try:
            raw = method(syms)
        except TypeError:
            try:
                raw = method(symbols=syms)
            except Exception:
                continue
        except Exception:
            continue
        out: dict[str, dict[str, Any]] = {}
        if isinstance(raw, Mapping):
            for key, value in raw.items():
                su = str(key).strip().upper()
                if su:
                    row = _snapshot_row_to_dict(value)
                    row.setdefault("symbol", su)
                    out[su] = row
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            for value in raw:
                row = _snapshot_row_to_dict(value)
                su = _snapshot_symbol_from_row(row)
                if su:
                    out[su] = row
        if out:
            return out

    out = {}
    for sym in syms:
        try:
            out[sym] = dict(market_client.get_snapshot(sym))
        except Exception:
            out[sym] = {}
    return out


def _call_get_snapshot_once(market_client: Any, symbol: str) -> dict[str, Any]:
    sym = str(symbol).strip().upper()
    if not sym or is_option_symbol(sym):
        return {}
    get_snapshot = getattr(market_client, "get_snapshot", None)
    if get_snapshot is not None:
        try:
            row = get_snapshot(sym)
            if isinstance(row, Mapping):
                out = _snapshot_row_to_dict(row)
                out.setdefault("symbol", sym)
                return out
        except Exception:
            pass
    return _call_get_snapshots_batch(market_client, [sym]).get(sym, {})


def _call_get_avg_volumes_batch(
    market_client: Any,
    symbols: Sequence[str],
) -> dict[str, float]:
    syms = [
        str(s).strip().upper()
        for s in symbols
        if str(s).strip() and not is_option_symbol(str(s).strip().upper())
    ]
    method = getattr(market_client, "get_avg_volumes", None)
    if method is not None:
        try:
            raw = method(syms)
        except TypeError:
            try:
                raw = method(symbols=syms)
            except Exception:
                raw = None
        except Exception:
            raw = None
        if isinstance(raw, Mapping):
            return {
                str(k).strip().upper(): _safe_float(v, 1.0)
                for k, v in raw.items()
                if str(k).strip()
            }
    out: dict[str, float] = {}
    for sym in syms:
        try:
            out[sym] = float(market_client.get_avg_volume(sym))
        except Exception:
            out[sym] = 1.0
    return out


def _call_get_bars_batch(
    market_client: Any,
    symbols: Sequence[str],
    *,
    timeframe: str,
    limit: int,
) -> dict[str, pd.DataFrame | None]:
    syms = [
        str(s).strip().upper()
        for s in symbols
        if str(s).strip() and not is_option_symbol(str(s).strip().upper())
    ]
    for method_name in ("get_bars_batch", "get_multi_bars"):
        method = getattr(market_client, method_name, None)
        if method is None:
            continue
        try:
            raw = method(syms, timeframe=timeframe, limit=limit)
        except TypeError:
            try:
                raw = method(symbols=syms, timeframe=timeframe, limit=limit)
            except Exception:
                continue
        except Exception:
            continue
        if isinstance(raw, Mapping):
            return {
                str(k).strip().upper(): v
                for k, v in raw.items()
                if str(k).strip()
            }

    return {
        sym: _call_get_bars(market_client, sym, timeframe=timeframe, limit=limit)
        for sym in syms
    }


def _intraday_quality_from_bars(
    *,
    bars_1m: pd.DataFrame | None,
    bars_5m: pd.DataFrame | None,
    price: float,
) -> DynamicScanQuality:
    price_above_vwap: bool | None = None
    intraday_range_pct = 0.0
    atr_expansion_ratio: float | None = None
    current_atr: float | None = None
    baseline_atr: float | None = None
    if bars_1m is not None and not getattr(bars_1m, "empty", True):
        cols = set(bars_1m.columns)
        if {"high", "low", "close", "volume"}.issubset(cols):
            high = bars_1m["high"].astype(float)
            low = bars_1m["low"].astype(float)
            close = bars_1m["close"].astype(float)
            volume = bars_1m["volume"].astype(float)
            px_ref = float(price) if price > 0 else float(close.iloc[-1])
            if px_ref > 0:
                intraday_range_pct = max(0.0, (float(high.max()) - float(low.min())) / px_ref * 100.0)
            vol_sum = float(volume.sum())
            if vol_sum > 0:
                typical = (high + low + close) / 3.0
                vwap = float((typical * volume).sum() / vol_sum)
                if vwap > 0:
                    price_above_vwap = px_ref > vwap
            tr_pct = ((high - low).abs() / close.replace(0, math.nan).abs()) * 100.0
            tr_pct = tr_pct.dropna()
            if len(tr_pct) >= 10:
                recent = float(tr_pct.tail(5).mean())
                base = float(tr_pct.iloc[:-5].tail(20).mean())
                current_atr = recent
                baseline_atr = base
                if base > 0:
                    atr_expansion_ratio = recent / base
    five_min_trend_aligned: bool | None = None
    five_min_up_streak = 0
    if bars_5m is not None and not getattr(bars_5m, "empty", True) and "close" in bars_5m.columns:
        close5 = bars_5m["close"].astype(float)
        if len(close5) >= 3:
            recent = close5.tail(3).tolist()
            five_min_trend_aligned = bool(
                recent[-1] >= recent[0]
                and recent[-1] >= recent[-2]
            )
        if len(close5) >= 2:
            streak = 0
            for i in range(len(close5) - 1, 0, -1):
                if float(close5.iloc[i]) > float(close5.iloc[i - 1]):
                    streak += 1
                else:
                    break
            five_min_up_streak = streak
    return DynamicScanQuality(
        price_above_vwap=price_above_vwap,
        five_min_trend_aligned=five_min_trend_aligned,
        intraday_range_pct=intraday_range_pct,
        atr_expansion_ratio=atr_expansion_ratio,
        current_atr=current_atr,
        baseline_atr=baseline_atr,
        five_min_up_streak=five_min_up_streak,
    )


_STRONG_NEWS_OVERRIDE_MIN_SCORE = 7
_STRONG_NEWS_OVERRIDE_MAX_CATALYST_AGE_MIN = 180.0
_STRONG_NEWS_OVERRIDE_MIN_REL_VOLUME = 0.35
_STRONG_NEWS_OVERRIDE_MAX_GAIN_PCT = 120.0
_STRONG_NEWS_VWAP_RECLAIM_BARS = 2
_CATALYST_RVOL_RELAX_MAX_AGE_MIN = 300.0
_CATALYST_RVOL_RELAX_OPEN_WINDOW_MIN = 45.0
_CATALYST_RVOL_RELAX_MIN_REL_VOLUME = 0.25
_CATALYST_ENTRY_RVOL_RELAX_MIN_REL_VOLUME = 0.50
_CATALYST_ENTRY_RVOL_RELAX_END_MINUTE_ET = 10 * 60 + 45


def _market_open_relax_window_active(now: datetime | None) -> bool:
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(ZoneInfo("America/New_York"))
    open_dt = local.replace(hour=9, minute=30, second=0, microsecond=0)
    minutes = (local - open_dt).total_seconds() / 60.0
    return 0.0 <= minutes < _CATALYST_RVOL_RELAX_OPEN_WINDOW_MIN


def _catalyst_entry_rvol_relax_window_active(now: datetime | None) -> bool:
    if now is None:
        return False
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(ZoneInfo("America/New_York"))
    minutes = local.hour * 60 + local.minute + (local.second / 60.0)
    return (9 * 60 + 30) <= minutes <= _CATALYST_ENTRY_RVOL_RELAX_END_MINUTE_ET


def _premarket_rank_value(artifact: Mapping[str, Any]) -> int | None:
    for key in ("premarket_rank", "rank"):
        raw = artifact.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            return max(1, int(float(raw)))
        except (TypeError, ValueError):
            continue
    return None


def _coerce_premarket_rank(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return max(1, int(float(value)))
    except (TypeError, ValueError):
        return None


def _catalyst_rvol_relax_age_minutes(catalyst_age_minutes: float | None) -> float:
    if catalyst_age_minutes is None:
        return math.inf
    try:
        age = float(catalyst_age_minutes)
    except (TypeError, ValueError):
        return math.inf
    return age if math.isfinite(age) else math.inf


def _catalyst_rvol_relax_signal(
    *,
    news_score: int,
    catalyst_score: float,
    artifact: Mapping[str, Any],
) -> bool:
    rank = _premarket_rank_value(artifact)
    return bool(
        int(news_score or 0) >= 8
        or float(catalyst_score or 0.0) >= 0.70
        or (rank is not None and rank <= 10)
    )


def vwap_reclaim_within_last_bars(
    bars_1m: pd.DataFrame | None,
    price: float,
    *,
    bars: int = _STRONG_NEWS_VWAP_RECLAIM_BARS,
) -> bool:
    """True when session VWAP was reclaimed upward within the last *bars* 1m closes."""
    if bars_1m is None or getattr(bars_1m, "empty", True):
        return False
    cols = set(bars_1m.columns)
    if not {"high", "low", "close", "volume"}.issubset(cols):
        return False
    high = bars_1m["high"].astype(float)
    low = bars_1m["low"].astype(float)
    close = bars_1m["close"].astype(float)
    volume = bars_1m["volume"].astype(float)
    vol_sum = float(volume.sum())
    if vol_sum <= 0:
        return False
    typical = (high + low + close) / 3.0
    vwap = float((typical * volume).sum() / vol_sum)
    if vwap <= 0:
        return False
    n = max(1, int(bars))
    tail = close.tail(n + 1)
    if len(tail) < 2:
        px = float(price) if price > 0 else float(close.iloc[-1])
        return px > vwap
    prev_below = False
    for idx in range(len(tail) - 1, 0, -1):
        prev_c = float(tail.iloc[idx - 1])
        cur_c = float(tail.iloc[idx])
        if prev_c <= vwap and cur_c > vwap:
            return True
        if prev_c < vwap:
            prev_below = True
    px = float(price) if price > 0 else float(tail.iloc[-1])
    return prev_below and px > vwap


def _resolve_catalyst_age_minutes(
    symbol: str,
    *,
    artifact_age_minutes: Any,
    news_cat: NewsCatalyst | None,
    now: datetime | None = None,
) -> float | None:
    """Freshness in minutes: premarket artifact, headline ``published_at``, then news-cache age."""
    if artifact_age_minutes is not None:
        try:
            return float(artifact_age_minutes)
        except (TypeError, ValueError):
            pass
    if news_cat is not None and news_cat.published_at is not None:
        ref = now or datetime.now(timezone.utc)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        pub = news_cat.published_at
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        else:
            pub = pub.astimezone(timezone.utc)
        return max(0.0, (ref - pub).total_seconds() / 60.0)
    age_sec = news_cache_age_seconds(symbol, now=now)
    if age_sec is not None:
        return float(age_sec) / 60.0
    return None


@dataclass(frozen=True)
class StrongNewsDynamicOverride:
    """Relaxed tape thresholds for very strong, fresh news catalysts."""

    candidate: bool
    rel_volume_active: bool
    applied: bool
    min_rel_volume: float | None
    max_gain_pct: float | None
    vwap_ok: bool
    block_reason: str | None


def evaluate_strong_news_dynamic_override(
    *,
    symbol: str,
    news_score: int,
    catalyst_age_minutes: float | None,
    price: float,
    spread_pct: float,
    quality: DynamicScanQuality,
    bars_1m: pd.DataFrame | None,
    min_price: float,
    max_spread_pct: float,
    emit_logs: bool,
) -> StrongNewsDynamicOverride:
    """
    Strong-news dynamic override: news_score >= 7, catalyst_age <= 180 min, price hard gate,
    VWAP above or reclaim within last 2 bars; relaxes rel_volume (0.35) and max gain (120).
    """
    sym = str(symbol or "").strip().upper()
    if int(news_score or 0) < _STRONG_NEWS_OVERRIDE_MIN_SCORE:
        return StrongNewsDynamicOverride(False, False, False, None, None, False, None)

    def _blocked(
        reason: str,
        *,
        rel_volume_active: bool = False,
    ) -> StrongNewsDynamicOverride:
        if emit_logs:
            log.info(
                "DYNAMIC_NEWS_OVERRIDE_BLOCKED symbol=%s news_score=%d catalyst_age_minutes=%s reason=%s",
                sym,
                int(news_score),
                "n/a" if catalyst_age_minutes is None else f"{float(catalyst_age_minutes):.1f}",
                reason,
            )
        return StrongNewsDynamicOverride(
            True,
            rel_volume_active,
            False,
            _STRONG_NEWS_OVERRIDE_MIN_REL_VOLUME if rel_volume_active else None,
            None,
            False,
            reason,
        )

    if catalyst_age_minutes is None:
        return _blocked("catalyst_age_minutes unavailable")
    if float(catalyst_age_minutes) > _STRONG_NEWS_OVERRIDE_MAX_CATALYST_AGE_MIN:
        return _blocked(
            "catalyst_age_minutes %.1f > %.0f"
            % (float(catalyst_age_minutes), _STRONG_NEWS_OVERRIDE_MAX_CATALYST_AGE_MIN)
        )
    if price < float(min_price):
        return _blocked("price %.2f < min_price %.2f" % (float(price), float(min_price)))
    vwap_above = quality.price_above_vwap is True
    reclaim = vwap_reclaim_within_last_bars(bars_1m, price)
    if not vwap_above and not reclaim:
        return _blocked(
            "not above VWAP and no VWAP reclaim in last %d bars" % _STRONG_NEWS_VWAP_RECLAIM_BARS,
            rel_volume_active=True,
        )
    if emit_logs:
        log.info(
            "DYNAMIC_NEWS_OVERRIDE_APPLIED symbol=%s news_score=%d catalyst_age_minutes=%.1f "
            "min_rel_volume=%.2f max_gain_pct=%.0f vwap_above=%s vwap_reclaim=%s",
            sym,
            int(news_score),
            float(catalyst_age_minutes),
            _STRONG_NEWS_OVERRIDE_MIN_REL_VOLUME,
            _STRONG_NEWS_OVERRIDE_MAX_GAIN_PCT,
            vwap_above,
            reclaim,
        )
    return StrongNewsDynamicOverride(
        True,
        True,
        True,
        _STRONG_NEWS_OVERRIDE_MIN_REL_VOLUME,
        _STRONG_NEWS_OVERRIDE_MAX_GAIN_PCT,
        True,
        None,
    )


def _log_dynamic_reject(
    symbol: str,
    reason: str,
    *,
    price: float,
    day_gain_pct: float,
    relative_volume: float,
    spread_pct: float,
    vwap_above: bool | None,
    news_score: int,
    catalyst_age_minutes: float | None,
    emit_logs: bool,
    print_reason: str | None = None,
) -> None:
    if not emit_logs:
        return
    if print_reason is not None:
        print(print_reason, flush=True)
    log.info(
        "DYNAMIC_REJECT symbol=%s price=%.4f gain_pct=%.2f rel_volume=%.3f spread_pct=%.3f "
        "vwap_above=%s news_score=%d catalyst_age_minutes=%s reason=%s",
        symbol,
        float(price),
        float(day_gain_pct),
        float(relative_volume),
        float(spread_pct),
        "n/a" if vwap_above is None else str(bool(vwap_above)),
        int(news_score),
        "n/a" if catalyst_age_minutes is None else f"{float(catalyst_age_minutes):.1f}",
        reason,
    )


def _log_dynamic_gate_debug(
    symbol: str,
    *,
    price_ok: bool,
    spread_ok: bool,
    avg_volume_ok: bool,
    rel_volume_ok: bool,
    gain_ok: bool,
    min_gain_ok: bool,
    max_gain_ok: bool,
    breakout_ok: bool,
    catalyst_ok: bool,
    entry_alignment_ok: bool,
    price: float,
    day_gain_pct: float,
    max_gain_pct: float,
    relative_volume: float,
    spread_pct: float,
    news_score: int,
    event_score: float,
    catalyst_score: float,
    emit_logs: bool,
) -> None:
    if not emit_logs:
        return
    log.info(
        "DYNAMIC_GATE_DEBUG symbol=%s price_ok=%s spread_ok=%s avg_volume_ok=%s "
        "rel_volume_ok=%s gain_ok=%s min_gain_ok=%s max_gain_ok=%s breakout_ok=%s "
        "catalyst_ok=%s entry_alignment_ok=%s "
        "price=%.4f gain_pct=%.2f max_gain_pct=%.2f rel_volume=%.3f spread_pct=%.3f "
        "news_score=%d event_score=%.2f catalyst_score=%.2f",
        symbol,
        bool(price_ok),
        bool(spread_ok),
        bool(avg_volume_ok),
        bool(rel_volume_ok),
        bool(gain_ok),
        bool(min_gain_ok),
        bool(max_gain_ok),
        bool(breakout_ok),
        bool(catalyst_ok),
        bool(entry_alignment_ok),
        float(price),
        float(day_gain_pct),
        float(max_gain_pct),
        float(relative_volume),
        float(spread_pct),
        int(news_score or 0),
        float(event_score or 0.0),
        float(catalyst_score or 0.0),
    )


def _position_qty_for_dynamic_exit(position: Any) -> int:
    if isinstance(position, Mapping):
        try:
            return int(float(position.get("qty", 0) or 0))
        except (TypeError, ValueError):
            return 0
    try:
        return int(float(getattr(position, "qty", 0) or 0))
    except (TypeError, ValueError):
        return 0


def manage_dynamic_exit(
    symbol: str,
    position: Any,
    price: float,
    vwap: float | None,
    config: dict[str, Any] | None,
    submit_sell: Callable[[str, int, str], bool],
    atr: float | None = None,
) -> bool:
    """
    Staged take-profit, trailing, and VWAP-break exits for symbols recorded in
    :func:`remember_entry` (``data/dynamic_universe_state.json``).

    *submit_sell(sym, qty, reason_tag)* must submit the order and return True if an order was sent.
    """
    sym_u = str(symbol or "").strip().upper()
    if not sym_u:
        return False

    state = load_state()
    active = state.get("active", {}).get(sym_u)
    if not active:
        return False

    try:
        entry = float(active["entry_price"])
    except (KeyError, TypeError, ValueError):
        remove_dynamic_symbol(sym_u, state)
        return False

    qty = _position_qty_for_dynamic_exit(position)
    if qty <= 0:
        remove_dynamic_symbol(sym_u, state)
        return False

    exit_cfg = (config or {}).get("dynamic_exits") or {}
    dyn_cfg = (config or {}).get("dynamic_universe") or {}
    atr_cfg = dyn_cfg.get("atr_exit") if isinstance(dyn_cfg.get("atr_exit"), Mapping) else {}
    news_cfg = (config or {}).get("news_ai") or {}
    try:
        strong_news_score = int(news_cfg.get("strong_news_score", 8) or 8)
    except (TypeError, ValueError):
        strong_news_score = 8

    news_score, news_reason = get_news_score(sym_u, config=config)

    update_high(sym_u, float(price), state)
    active = state.get("active", {}).get(sym_u) or {}
    high = float(active.get("high_price", entry))

    pnl_pct = ((float(price) - entry) / entry) * 100.0 if entry > 0 else 0.0
    drawdown_from_high_pct = (
        ((high - float(price)) / high) * 100.0 if high > 0 else 0.0
    )

    def _mark_dynamic_cooldown(reason_tag: str, state_obj: dict[str, Any] | None = None, *, remove_active: bool = True) -> None:
        minutes, cooldown_reason = adaptive_dynamic_reentry_cooldown_minutes(
            pnl_pct=float(pnl_pct),
            news_score=float(news_score or 0),
            catalyst_age_minutes=None,
            config=config,
        )
        st_obj = state_obj if state_obj is not None else load_state()
        mark_cooldown(
            sym_u,
            int(minutes),
            st_obj,
            remove_active=remove_active,
            metadata={
                "cooldown_required": int(minutes),
                "cooldown_used": int(minutes),
                "cooldown_reason": cooldown_reason,
                "exit_reason": reason_tag,
                "pnl_pct": round(float(pnl_pct), 6),
            },
        )
        log.info(
            "DYNAMIC_REENTRY_COOLDOWN_SET symbol=%s cooldown_required=%d cooldown_used=%d cooldown_reason=%s exit_reason=%s pnl_pct=%.3f",
            sym_u,
            int(minutes),
            int(minutes),
            cooldown_reason,
            reason_tag,
            float(pnl_pct),
        )

    tp1_pct = float(exit_cfg.get("take_profit_1_pct", 2.0))
    tp2_pct = float(exit_cfg.get("take_profit_2_pct", 4.0))
    trail_pct = float(exit_cfg.get("trailing_stop_pct", 1.5))
    strong_hold_minutes = int(exit_cfg.get("strong_news_hold_minutes", 30) or 30)
    strong_trail_trigger_pct = float(exit_cfg.get("strong_news_trailing_trigger_pct", 8.0) or 8.0)
    strong_trail_pct = float(exit_cfg.get("strong_news_trailing_stop_pct", 4.0) or 4.0)

    atr_enabled = bool(atr_cfg.get("enabled", False)) and atr is not None and float(atr) > 0.0
    if atr_enabled:
        try:
            atr_val = float(atr)
        except (TypeError, ValueError):
            atr_val = None
        if atr_val is not None and atr_val > 0.0:
            try:
                stop_mult = float(atr_cfg.get("stop_atr_mult", 1.5) or 1.5)
            except (TypeError, ValueError):
                stop_mult = 1.5
            try:
                target_mult = float(atr_cfg.get("target_atr_mult", 3.0) or 3.0)
            except (TypeError, ValueError):
                target_mult = 3.0
            try:
                trail_after_mult = float(atr_cfg.get("trail_after_atr_mult", 2.0) or 2.0)
            except (TypeError, ValueError):
                trail_after_mult = 2.0
            try:
                trail_mult = float(atr_cfg.get("trail_atr_mult", 1.0) or 1.0)
            except (TypeError, ValueError):
                trail_mult = 1.0

            strong_news = int(news_score or 0) >= strong_news_score
            if strong_news:
                stop_mult *= 1.25
                trail_after_mult *= 1.10
                trail_mult *= 1.10
            initial_stop = entry - stop_mult * atr_val
            profit_target = entry + target_mult * atr_val
            trail_trigger = entry + trail_after_mult * atr_val
            trail_stop = high - trail_mult * atr_val if high >= trail_trigger else None
            if float(price) <= initial_stop:
                log.info(
                    "DYNAMIC_ATR_EXIT symbol=%s entry=%.2f price=%.2f atr=%.2f stop=%.2f target=%.2f trail=%.2f",
                    sym_u,
                    entry,
                    float(price),
                    atr_val,
                    initial_stop,
                    profit_target,
                    trail_stop if trail_stop is not None else trail_trigger,
                )
                if submit_sell(sym_u, qty, "dynamic_atr_stop"):
                    _mark_dynamic_cooldown("dynamic_atr_stop")
                    return True
                return False
            if not strong_news and float(price) >= profit_target:
                log.info(
                    "DYNAMIC_ATR_EXIT symbol=%s entry=%.2f price=%.2f atr=%.2f stop=%.2f target=%.2f trail=%.2f",
                    sym_u,
                    entry,
                    float(price),
                    atr_val,
                    initial_stop,
                    profit_target,
                    trail_stop if trail_stop is not None else trail_trigger,
                )
                if submit_sell(sym_u, qty, "dynamic_atr_target"):
                    _mark_dynamic_cooldown("dynamic_atr_target")
                    return True
                return False
            if trail_stop is not None and float(price) <= trail_stop:
                log.info(
                    "DYNAMIC_ATR_EXIT symbol=%s entry=%.2f price=%.2f atr=%.2f stop=%.2f target=%.2f trail=%.2f",
                    sym_u,
                    entry,
                    float(price),
                    atr_val,
                    initial_stop,
                    profit_target,
                    trail_stop,
                )
                if submit_sell(sym_u, qty, "dynamic_atr_trailing"):
                    _mark_dynamic_cooldown("dynamic_atr_trailing")
                    return True
                return False

            # ATR regime is active; skip legacy tiny-profit exits and let the dynamic ATR levels manage it.
            return False

    try:
        entry_time_raw = active.get("entry_time")
        entry_ts = int(float(entry_time_raw or 0))
    except (TypeError, ValueError):
        entry_ts = 0
    held_minutes = max(0.0, (_now() - entry_ts) / 60.0) if entry_ts > 0 else None
    strong_news_hold_active = (
        int(news_score or 0) >= 7
        and held_minutes is not None
        and held_minutes < float(strong_hold_minutes)
    )
    if strong_news_hold_active:
        log.info(
            "DYNAMIC_HOLD_TIMER symbol=%s news_score=%d held_minutes=%.1f hold_minutes=%d action=block_non_emergency",
            sym_u,
            int(news_score or 0),
            float(held_minutes or 0.0),
            int(strong_hold_minutes),
        )
        log.info(
            "DYNAMIC_EXIT_REASON symbol=%s reason=strong_news_hold_timer",
            sym_u,
        )
        return False

    strong_news_profit_trailing = int(news_score or 0) >= 7 and pnl_pct >= float(strong_trail_trigger_pct)
    if strong_news_profit_trailing:
        log.info(
            "DYNAMIC_TRAILING_STOP symbol=%s news_score=%d gain_pct=%.2f trail_pct=%.2f drawdown_from_high_pct=%.2f reason=profit_protection",
            sym_u,
            int(news_score or 0),
            float(pnl_pct),
            float(strong_trail_pct),
            float(drawdown_from_high_pct),
        )
        if drawdown_from_high_pct >= float(strong_trail_pct):
            log.info(
                "DYNAMIC_EXIT_REASON symbol=%s reason=strong_news_trailing_stop",
                sym_u,
            )
            if submit_sell(sym_u, qty, "dynamic_trailing_stop"):
                _mark_dynamic_cooldown("dynamic_trailing_stop")
                return True
            return False
        log.info(
            "DYNAMIC_EXIT_REASON symbol=%s reason=strong_news_trailing_hold",
            sym_u,
        )
        return False

    # TP1 — partial
    if pnl_pct >= tp1_pct and not active.get("tp1_done", False):
        frac = float(exit_cfg.get("take_profit_1_sell_frac", 0.5))
        sell_qty = max(1, int(float(qty) * frac))
        sell_qty = min(sell_qty, qty)
        log.info("DYNAMIC_EXIT_REASON symbol=%s reason=tp1", sym_u)
        if submit_sell(sym_u, sell_qty, "dynamic_tp1"):
            _st = load_state()
            _st.setdefault("active", {}).setdefault(sym_u, {})["tp1_done"] = True
            if sell_qty >= max(1, qty // 2):
                _mark_dynamic_cooldown("dynamic_tp1", _st, remove_active=False)
            save_state(_st)
            return True
        return False

    # TP2 — full
    if pnl_pct >= tp2_pct:
        log.info("DYNAMIC_EXIT_REASON symbol=%s reason=tp2_full", sym_u)
        if submit_sell(sym_u, qty, "dynamic_tp2_full"):
            _mark_dynamic_cooldown("dynamic_tp2_full")
            return True
        return False

    # Trailing stop from session high (stored in state)
    if drawdown_from_high_pct >= trail_pct:
        log.info("DYNAMIC_TRAILING_STOP symbol=%s gain_pct=%.2f trail_pct=%.2f drawdown_from_high_pct=%.2f reason=session_high", sym_u, float(pnl_pct), float(trail_pct), float(drawdown_from_high_pct))
        log.info("DYNAMIC_EXIT_REASON symbol=%s reason=trailing_stop", sym_u)
        if submit_sell(sym_u, qty, "dynamic_trailing_stop"):
            _mark_dynamic_cooldown("dynamic_trailing_stop")
            return True
        return False

    # VWAP break
    vw = float(vwap) if vwap is not None and float(vwap) == float(vwap) else None
    if (
        bool(exit_cfg.get("vwap_break_exit", True))
        and vw is not None
        and vw > 0
        and float(price) < vw
    ):
        log.info("DYNAMIC_EXIT_REASON symbol=%s reason=vwap_break", sym_u)
        if submit_sell(sym_u, qty, "dynamic_vwap_break"):
            _mark_dynamic_cooldown("dynamic_vwap_break")
            return True
        return False

    return False


def entry_target_dollars_for_symbol(
    normal_target_dollars: float,
    *,
    symbol: str,
    core_symbols: list[str],
    account_equity: float,
    config: dict[str, Any] | None,
) -> float:
    """
    Planned entry notional: core symbols use *normal_target_dollars* from the engine sizer.

    Non-core (dynamic) symbols use ``dynamic_universe.max_symbol_exposure_pct`` of equity (default 3%%).
    """
    if not is_dynamic_symbol(symbol, core_symbols):
        return float(normal_target_dollars)
    du = (config or {}).get("dynamic_universe") or {}
    max_symbol_pct = float(du.get("max_symbol_exposure_pct", 3))
    return float(account_equity) * (max_symbol_pct / 100.0)


def _now() -> int:
    return int(time.time())


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"cooldowns": {}, "active": {}}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"cooldowns": {}, "active": {}}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def in_cooldown(symbol: str, state: dict[str, Any]) -> bool:
    until = state.get("cooldowns", {}).get(symbol)
    return bool(until and _now() < until)


def mark_cooldown(
    symbol: str,
    minutes: int,
    state: dict[str, Any],
    *,
    remove_active: bool = True,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    state.setdefault("cooldowns", {})[symbol] = _now() + minutes * 60
    if metadata:
        state.setdefault("cooldown_meta", {})[symbol] = dict(metadata)
    if remove_active:
        state.get("active", {}).pop(symbol, None)
    save_state(state)


def adaptive_dynamic_reentry_cooldown_minutes(
    *,
    pnl_pct: float,
    news_score: int | float = 0,
    catalyst_age_minutes: float | None = None,
    config: Mapping[str, Any] | None = None,
) -> tuple[int, str]:
    cfg = (config or {}).get("entry_quality") if isinstance(config, Mapping) else {}
    cfg = cfg if isinstance(cfg, Mapping) else {}
    if not bool(cfg.get("dynamic_cooldown", False)):
        return 60, "fixed_default"
    raw = cfg.get("dynamic_cooldown_minutes") if isinstance(cfg.get("dynamic_cooldown_minutes"), Mapping) else {}
    loss_min = int(float(raw.get("loss_min", 60) or 60))
    loss_max = int(float(raw.get("loss_max", 90) or 90))
    small_winner = int(float(raw.get("small_winner", 30) or 30))
    large_winner = int(float(raw.get("large_winner", 15) or 15))
    reduction = int(float(raw.get("fresh_catalyst_reduction", 15) or 15))
    window = float(raw.get("fresh_catalyst_window_minutes", 120) or 120)
    reason = "loss_exit"
    if pnl_pct < 0:
        minutes = loss_max if pnl_pct <= -1.0 else loss_min
    elif pnl_pct >= 1.0:
        minutes = large_winner
        reason = "large_winner"
    else:
        minutes = small_winner
        reason = "small_winner"
    fresh_catalyst = bool(float(news_score or 0) >= 3 and catalyst_age_minutes is not None and float(catalyst_age_minutes) <= window)
    if fresh_catalyst and pnl_pct >= 0:
        minutes = max(large_winner, minutes - reduction)
        reason = f"{reason}_fresh_catalyst"
    return max(0, int(minutes)), reason


def dynamic_reentry_cooldown_remaining_minutes(
    symbol: str,
    *,
    state: dict[str, Any] | None = None,
    now: int | None = None,
) -> float | None:
    """Remaining cooldown minutes for a symbol after a dynamic exit/reduction."""
    su = str(symbol or "").strip().upper()
    if not su:
        return None
    st = state or load_state()
    until = st.get("cooldowns", {}).get(su)
    if not until:
        return None
    try:
        until_i = int(float(until))
    except (TypeError, ValueError):
        return None
    now_i = _now() if now is None else int(now)
    rem = (until_i - now_i) / 60.0
    return max(0.0, rem)


def dynamic_reentry_cooldown_active(
    symbol: str,
    *,
    state: dict[str, Any] | None = None,
    now: int | None = None,
) -> tuple[bool, float | None]:
    """True when *symbol* is still inside its post-exit dynamic re-entry cooldown."""
    rem = dynamic_reentry_cooldown_remaining_minutes(symbol, state=state, now=now)
    if rem is None or rem <= 0.0:
        return False, None
    return True, rem


def remember_entry(symbol: str, entry_price: float, state: dict[str, Any]) -> None:
    state.setdefault("active", {})[symbol] = {
        "entry_price": float(entry_price),
        "entry_time": _now(),
        "high_price": float(entry_price),
        "tp1_done": False,
    }
    save_state(state)


def update_high(symbol: str, price: float, state: dict[str, Any]) -> None:
    active = state.setdefault("active", {}).setdefault(symbol, {})
    active["high_price"] = max(float(price), float(active.get("high_price", price)))
    save_state(state)


def remove_dynamic_symbol(symbol: str, state: dict[str, Any]) -> None:
    state.get("active", {}).pop(symbol, None)
    save_state(state)


def _expand_mover_feed(market_client: Any, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Merge Alpaca screener output with optional YAML **leader_pools** (AI / semis / biotech / …),
    ``ai_dynamic_symbols`` (pinned AI / infra tape), and ``extra_mover_symbols`` (earnings runners,
    manual watchlist). Symbols are deduped in feed order.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    exclude_suffixes = tuple(
        sorted(
            {
                str(s).strip().upper()
                for s in (cfg.get("exclude_suffixes") or [])
                if str(s).strip()
            },
            key=len,
            reverse=True,
        )
    )

    def add_sym(sym: str) -> None:
        su = str(sym).strip().upper()
        if is_option_symbol(su):
            return
        if exclude_suffixes and any(su.endswith(sfx) for sfx in exclude_suffixes):
            return
        if su and su not in seen:
            seen.add(su)
            rows.append({"symbol": su})

    try:
        for item in market_client.get_top_movers():
            add_sym(str(item.get("symbol", "")))
    except Exception as e:
        log.warning("dynamic universe get_top_movers failed: %s", e)

    pools = cfg.get("leader_pools")
    if isinstance(pools, dict):
        for _name, syms in pools.items():
            if isinstance(syms, (list, tuple)):
                for s in syms:
                    add_sym(str(s))
    for s in cfg.get("extra_mover_symbols") or []:
        add_sym(str(s))
    raw_ai_dyn = cfg.get("ai_dynamic_symbols")
    if isinstance(raw_ai_dyn, (list, tuple)):
        for s in raw_ai_dyn:
            add_sym(str(s))
    return rows


def _filter_core_movers(
    movers: Sequence[Mapping[str, Any]],
    core_symbols: Sequence[str],
    *,
    emit_logs: bool,
) -> tuple[list[dict[str, Any]], int]:
    core = {str(s or "").strip().upper() for s in core_symbols if str(s or "").strip()}
    if not core:
        if emit_logs:
            log.info("DYNAMIC_SCAN_CORE_SKIPPED_SUMMARY count=0")
        return [dict(item) for item in movers], 0
    out: list[dict[str, Any]] = []
    skipped = 0
    for item in movers:
        sym = str(item.get("symbol", "") if isinstance(item, Mapping) else "").strip().upper()
        if sym in core:
            skipped += 1
            continue
        out.append(dict(item))
    if emit_logs:
        log.info("DYNAMIC_SCAN_CORE_SKIPPED_SUMMARY count=%d", skipped)
    return out, skipped


def _append_news_dynamic_movers(
    movers: list[dict[str, Any]],
    mover_symbols: list[str],
    news_by_symbol: Mapping[str, NewsCatalyst],
    core_symbols: Sequence[str],
) -> int:
    core = {str(s or "").strip().upper() for s in core_symbols if str(s or "").strip()}
    news_syms = set(mover_symbols)
    skipped_core = 0
    for sym, cat in news_by_symbol.items():
        sym_u = str(sym or "").strip().upper()
        if not sym_u or is_option_symbol(sym_u) or sym_u in news_syms:
            continue
        if sym_u in core:
            skipped_core += 1
            continue
        if cat.score >= 3:
            movers.append({"symbol": sym_u})
            mover_symbols.append(sym_u)
            news_syms.add(sym_u)
            if str(cat.source or "").strip().lower() == "alpaca":
                now = datetime.now(timezone.utc)
                latency = None
                if cat.published_at is not None:
                    published = cat.published_at
                    if published.tzinfo is None:
                        published = published.replace(tzinfo=timezone.utc)
                    latency = max(0.0, (now - published).total_seconds())
                log.info(
                    "ALPACA_NEWS_CANDIDATE_INJECTED symbol=%s score=%d latency_seconds=%s headline=%s",
                    sym_u,
                    int(cat.score),
                    "n/a" if latency is None else "%.1f" % latency,
                    cat.headline[:180],
                )
    return skipped_core


def _corporate_action_cfg(cfg: Mapping[str, Any]) -> dict[str, Any]:
    raw = cfg.get("corporate_actions")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _corporate_action_enabled(cfg: Mapping[str, Any]) -> bool:
    ca_cfg = _corporate_action_cfg(cfg)
    return str(ca_cfg.get("enabled", False)).strip().lower() in {"1", "true", "yes", "on"}


def _corp_action_value(action: Any, key: str, default: Any = None) -> Any:
    if isinstance(action, Mapping):
        return action.get(key, default)
    return getattr(action, key, default)


def _corp_action_text(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip().lower()


def _corp_action_symbol(action: Any) -> str:
    for key in ("symbol", "new_symbol", "old_symbol"):
        sym = str(_corp_action_value(action, key, "") or "").strip().upper()
        if sym:
            return sym
    return ""


def _classify_corporate_action(action: Any) -> dict[str, Any]:
    ca_type = _corp_action_text(
        _corp_action_value(action, "ca_type")
        or _corp_action_value(action, "type")
        or _corp_action_value(action, "corporate_action_type")
    )
    subtype = _corp_action_text(_corp_action_value(action, "sub_type") or _corp_action_value(action, "subtype"))
    description = str(
        _corp_action_value(action, "description")
        or _corp_action_value(action, "name")
        or _corp_action_value(action, "announcement_type")
        or ca_type
        or subtype
        or "corporate_action"
    )
    text = f"{ca_type} {subtype} {description}".lower()
    severity = "info"
    normalized = ca_type or subtype or "corporate_action"
    block = False
    if "reverse" in text and "split" in text:
        normalized = "reverse_split"
        severity = "block"
        block = True
    elif "merger" in text or "acquisition" in text:
        normalized = "merger"
        severity = "block"
        block = True
    elif "symbol" in text and ("change" in text or "changed" in text):
        normalized = "symbol_change"
        severity = "warn"
    elif "split" in text:
        normalized = "split"
        severity = "warn"
    elif "dividend" in text:
        normalized = "dividend"
        severity = "info"
    return {
        "symbol": _corp_action_symbol(action),
        "type": normalized,
        "severity": severity,
        "block": block,
        "description": description,
    }


def _persist_corporate_action_diagnostics(
    annotation: Mapping[str, Any],
    *,
    cfg: Mapping[str, Any],
    now: datetime,
) -> None:
    ca_cfg = _corporate_action_cfg(cfg)
    if str(ca_cfg.get("persist", True)).strip().lower() in {"0", "false", "no", "off"}:
        return
    out_dir = Path(str(ca_cfg.get("persist_dir") or "data/research/corporate_actions"))
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{now.date().isoformat()}.jsonl"
        payload = dict(annotation)
        payload["recorded_at"] = now.isoformat()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, sort_keys=True) + "\n")
    except Exception:
        log.debug("corporate action diagnostic persistence failed", exc_info=True)


def _lookup_corporate_action_annotations(
    market_client: Any,
    symbols: Sequence[str],
    cfg: Mapping[str, Any],
    *,
    now: datetime | None = None,
    emit_logs: bool = True,
) -> dict[str, dict[str, Any]]:
    if not _corporate_action_enabled(cfg):
        return {}
    ts = now or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    syms = list(dict.fromkeys(str(s or "").strip().upper() for s in symbols if str(s or "").strip()))
    if not syms:
        return {}
    ca_cfg = _corporate_action_cfg(cfg)
    try:
        lookback_days = max(1, int(ca_cfg.get("lookback_days", 7) or 7))
    except (TypeError, ValueError):
        lookback_days = 7
    if emit_logs:
        log.info("ALPACA_CORP_ACTION_LOOKUP symbols=%d lookback_days=%d", len(syms), lookback_days)
    if market_client is None or not hasattr(market_client, "get_corporate_actions"):
        log.info("ALPACA_CORP_ACTION_FALLBACK reason=client_missing")
        return {}
    try:
        actions = market_client.get_corporate_actions(
            syms,
            start=(ts - timedelta(days=lookback_days)).date(),
            end=ts.date(),
        )
    except Exception as exc:
        log.info("ALPACA_CORP_ACTION_FALLBACK reason=fetch_failed error=%s", str(exc)[:180])
        return {}
    out: dict[str, dict[str, Any]] = {}
    for action in actions or []:
        annotation = _classify_corporate_action(action)
        sym = str(annotation.get("symbol") or "").strip().upper()
        if not sym or sym not in syms:
            continue
        previous = out.get(sym)
        if previous is None or (annotation.get("severity") == "block" and previous.get("severity") != "block"):
            out[sym] = annotation
        if emit_logs:
            log.info(
                "ALPACA_CORP_ACTION_MATCH symbol=%s type=%s severity=%s description=%s",
                sym,
                annotation.get("type") or "unknown",
                annotation.get("severity") or "unknown",
                str(annotation.get("description") or "")[:180],
            )
        _persist_corporate_action_diagnostics(annotation, cfg=cfg, now=ts)
    return out


def _tape_momentum_bonus(market_client: Any, cfg: dict[str, Any]) -> float:
    """
    Optional bonus when sector/theme ETFs are strong (XLK, SMH, XLE, XBI, …).
    Config: ``theme_momentum_bonus_etfs: { SMH: 0.4, XBI: 0.3 }`` weights summed with ETF day %%.
    """
    raw = cfg.get("theme_momentum_bonus_etfs")
    if not isinstance(raw, dict) or not raw:
        return 0.0
    bonus = 0.0
    for etf, w in raw.items():
        sym = str(etf).strip().upper()
        if not sym:
            continue
        try:
            ww = float(w)
        except (TypeError, ValueError):
            continue
        try:
            sn = market_client.get_snapshot(sym)
            bonus += float(sn.get("day_gain_pct", 0) or 0) * ww
        except Exception:
            continue
    return bonus


def _theme_momentum_context(
    market_client: Any,
    cfg: dict[str, Any],
    snapshots: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, float]:
    """Theme ETF day-gain scores used for symbol-specific dynamic ranking bonuses."""
    if not theme_intelligence_enabled(cfg):
        return {}
    rows: dict[str, Mapping[str, Any]] = {}
    for etf in theme_etf_symbols(cfg):
        snap = snapshots.get(etf) if snapshots is not None else None
        if not snap and callable(getattr(market_client, "get_snapshot", None)):
            try:
                snap = market_client.get_snapshot(etf)
            except Exception:
                snap = None
        if isinstance(snap, Mapping):
            rows[str(etf).upper()] = snap
    return theme_momentum_scores(rows, cfg)


def merge_dynamic_momentum_override_scan_cfg(
    dynamic_universe_cfg: Mapping[str, Any] | None,
    full_config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """
    When ``dynamic_momentum_override.enabled`` is true, overlay scanner tape thresholds.

    Applies override scan keys onto the dynamic-universe scan dict
    (same keys as :func:`scan_dynamic_candidates` expects).
    """
    out = dict(dynamic_universe_cfg or {})
    ov = (full_config or {}).get("dynamic_momentum_override")
    if not isinstance(ov, dict) or not bool(ov.get("enabled")):
        return out
    raw_gain = ov.get("min_day_gain_pct")
    if raw_gain is not None and str(raw_gain).strip() != "":
        try:
            out["min_day_gain_pct"] = float(raw_gain)
        except (TypeError, ValueError):
            pass
    raw_rel = ov.get("min_relative_volume")
    if raw_rel is not None and str(raw_rel).strip() != "":
        try:
            rv = float(raw_rel)
            out["min_rel_volume"] = rv
            out["min_relative_volume"] = rv
        except (TypeError, ValueError):
            pass
    if "require_above_vwap" in ov:
        out["require_above_vwap"] = bool(ov.get("require_above_vwap"))
    return out


def dynamic_scan_cfg_with_entry_alignment(
    dynamic_universe_cfg: Mapping[str, Any] | None,
    full_config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return dynamic scanner config carrying the live dynamic-entry gate settings."""
    out = dict(dynamic_universe_cfg or {})
    cfg = full_config if isinstance(full_config, Mapping) else {}
    market_data_cfg = cfg.get("market_data")
    if isinstance(market_data_cfg, Mapping) and "market_data" not in out:
        out["market_data"] = dict(market_data_cfg)
    broker_cfg = cfg.get("broker")
    if (
        isinstance(broker_cfg, Mapping)
        and "paper" in broker_cfg
        and "broker_is_paper" not in out
    ):
        out["broker_is_paper"] = bool(broker_cfg.get("paper"))
    entry = cfg.get("dynamic_momentum_entry")
    if not isinstance(entry, Mapping):
        return out
    aligned_entry = dict(entry)
    override = cfg.get("dynamic_momentum_override")
    if isinstance(override, Mapping) and bool(override.get("enabled")):
        for key in ("min_day_gain_pct", "min_relative_volume"):
            raw = override.get(key)
            if raw is None or str(raw).strip() == "":
                continue
            try:
                override_value = float(raw)
            except (TypeError, ValueError):
                continue
            current = aligned_entry.get(key)
            if current is None or str(current).strip() == "":
                aligned_entry[key] = override_value
                continue
            try:
                aligned_entry[key] = min(float(current), override_value)
            except (TypeError, ValueError):
                aligned_entry[key] = override_value
        if "require_above_vwap" in override:
            aligned_entry["require_above_vwap"] = bool(override.get("require_above_vwap"))
        if "allow_without_ema_pullback" in override:
            aligned_entry["allow_without_ema_pullback"] = bool(override.get("allow_without_ema_pullback"))
        if "allow_without_pullback" in override:
            aligned_entry["allow_without_pullback"] = bool(override.get("allow_without_pullback"))
    out["dynamic_momentum_entry"] = aligned_entry
    return out


def _dynamic_quote_retry_settings(cfg: Mapping[str, Any] | None, *, env: str) -> dict[str, Any]:
    env_norm = str(env or "").strip().lower()
    cfg_map = cfg if isinstance(cfg, Mapping) else {}
    market_data_cfg = cfg_map.get("market_data")
    if not isinstance(market_data_cfg, Mapping):
        return {
            "enabled": False,
            "attempts": 0,
            "delay_seconds": 0.0,
        }
    retry_cfg = market_data_cfg.get("dynamic_quote_retry")
    if not isinstance(retry_cfg, Mapping) and env_norm == "live":
        retry_cfg = market_data_cfg.get("live_dynamic_quote_retry")
    if not isinstance(retry_cfg, Mapping):
        return {
            "enabled": False,
            "attempts": 0,
            "delay_seconds": 0.0,
        }
    try:
        attempts = int(retry_cfg.get("attempts", 0) or 0)
    except (TypeError, ValueError):
        attempts = 0
    try:
        delay_seconds = float(retry_cfg.get("delay_seconds", 0.0) or 0.0)
    except (TypeError, ValueError):
        delay_seconds = 0.0
    enabled = bool(retry_cfg.get("enabled", False))
    if env_norm == "paper":
        env_enabled = bool(retry_cfg.get("paper_enabled", False))
    elif env_norm == "live":
        env_enabled = bool(retry_cfg.get("live_enabled", enabled))
    else:
        env_enabled = False
    return {
        "enabled": enabled and env_enabled,
        "attempts": max(0, min(attempts, 5)),
        "delay_seconds": max(0.0, min(delay_seconds, 2.0)),
    }


def _quote_fields(snapshot: Mapping[str, Any] | None) -> tuple[float, float, float, float, float]:
    snap = snapshot if isinstance(snapshot, Mapping) else {}
    price = _safe_float(snap.get("price"), 0.0)
    day_gain_pct = _safe_float(snap.get("day_gain_pct"), 0.0)
    volume = _safe_float(snap.get("volume"), 0.0)
    bid = _safe_float(snap.get("bid"), 0.0)
    ask = _safe_float(snap.get("ask"), 0.0)
    return price, day_gain_pct, volume, bid, ask


def _dynamic_scan_settings(cfg: dict[str, Any]) -> dict[str, Any]:
    try:
        min_5m_up_streak = int(float(cfg.get("min_5m_consecutive_up_bars", 0) or 0))
    except (TypeError, ValueError):
        min_5m_up_streak = 0
    try:
        max_symbols = int(float(cfg.get("max_symbols", cfg.get("top_n", 3)) or 3))
    except (TypeError, ValueError):
        max_symbols = 3
    min_rel_raw = cfg.get("min_rel_volume")
    if min_rel_raw is None or str(min_rel_raw).strip() == "":
        min_rel_raw = cfg.get("min_relative_volume", 1.5)
    max_gain_raw = cfg.get("max_day_gain_pct")
    if max_gain_raw is None or str(max_gain_raw).strip() == "":
        max_gain_raw = cfg.get("max_gain_pct", 15.0)
    settings = {
        "min_price": effective_dynamic_min_price(cfg),
        "max_price": _safe_float(cfg.get("max_price", 1000), 1000.0),
        "min_gain": _safe_float(cfg.get("min_day_gain_pct", cfg.get("min_gain_pct", 3.0)), 3.0),
        "max_gain": _safe_float(max_gain_raw, 15.0),
        "min_intraday_range_pct": _safe_float(cfg.get("min_intraday_range_pct", 0.0), 0.0),
        "min_avg_vol": _safe_float(cfg.get("min_avg_volume", 2_000_000), 2_000_000.0),
        "min_rel_vol": _safe_float(min_rel_raw, 1.5),
        "min_atr_expansion_ratio": _safe_float(cfg.get("min_atr_expansion_ratio", 0.0), 0.0),
        "require_above_vwap": bool(cfg.get("require_above_vwap", False)),
        "require_5m_trend_alignment": bool(cfg.get("require_5m_trend_alignment", False)),
        "min_5m_up_streak": max(0, min_5m_up_streak),
        "max_spread_pct": _safe_float(cfg.get("max_spread_pct", 0.25), 0.25),
        "max_symbols": max(0, max_symbols),
    }
    aggressive_cfg = cfg.get("aggressive_mode") if isinstance(cfg.get("aggressive_mode"), Mapping) else {}
    if bool(aggressive_cfg.get("enabled", False)):
        catalyst_min_gain = _safe_float(
            aggressive_cfg.get("catalyst_minimum_day_gain_pct"),
            _safe_float(aggressive_cfg.get("minimum_day_gain_pct"), 0.5),
        )
        settings["min_price"] = max(2.0, _safe_float(aggressive_cfg.get("minimum_price"), 2.0))
        settings["max_price"] = _safe_float(aggressive_cfg.get("maximum_price"), 300.0)
        settings["min_gain"] = _safe_float(aggressive_cfg.get("minimum_day_gain_pct"), 0.5)
        settings["catalyst_min_gain"] = catalyst_min_gain
        settings["min_rel_vol"] = _safe_float(aggressive_cfg.get("minimum_relative_volume"), 0.75)
        settings["catalyst_min_rel_vol"] = _safe_float(aggressive_cfg.get("catalyst_minimum_relative_volume"), 0.40)
        settings["max_symbols"] = max(settings["max_symbols"], int(_safe_float(aggressive_cfg.get("max_symbols"), 50.0)))
        settings["require_above_vwap"] = False
        settings["require_5m_trend_alignment"] = False
        spread_tiers = aggressive_cfg.get("max_spread_by_tier") if isinstance(aggressive_cfg.get("max_spread_by_tier"), Mapping) else {}
        settings["max_spread_pct"] = max(
            settings["max_spread_pct"],
            _safe_float(spread_tiers.get("normal"), 3.0),
        )
        settings["aggressive_mode"] = True
        log.info(
            "DYNAMIC_AGGRESSIVE_CONFIG enabled=true rollout=scanner min_price=%.2f max_price=%.2f min_gain=%.2f catalyst_min_gain=%.2f min_rel=%.2f catalyst_min_rel=%.2f max_symbols=%d max_spread=%.2f",
            float(settings["min_price"]),
            float(settings["max_price"]),
            float(settings["min_gain"]),
            float(settings["catalyst_min_gain"]),
            float(settings["min_rel_vol"]),
            float(settings["catalyst_min_rel_vol"]),
            int(settings["max_symbols"]),
            float(settings["max_spread_pct"]),
        )
    strong_override_cfg = cfg.get("strong_catalyst_override")
    if not isinstance(strong_override_cfg, Mapping):
        strong_override_cfg = {}
    catalyst_cfg = cfg.get("catalyst_boost")
    if not isinstance(catalyst_cfg, Mapping):
        catalyst_cfg = {}
    high_conviction_cfg = cfg.get("high_conviction_news_override")
    if not isinstance(high_conviction_cfg, Mapping):
        trading_cfg = cfg.get("trading") if isinstance(cfg.get("trading"), Mapping) else {}
        dynamic_cfg = trading_cfg.get("dynamic") if isinstance(trading_cfg.get("dynamic"), Mapping) else {}
        high_conviction_cfg = dynamic_cfg.get("high_conviction_news_override")
    if not isinstance(high_conviction_cfg, Mapping):
        high_conviction_cfg = {}
    try:
        min_event_score = float(catalyst_cfg.get("min_event_score", 0.0) or 0.0)
    except (TypeError, ValueError):
        min_event_score = 0.0
    catalyst_gain_override_raw = catalyst_cfg.get("max_gain_pct_catalyst")
    if catalyst_gain_override_raw is None or str(catalyst_gain_override_raw).strip() == "":
        catalyst_gain_override_raw = catalyst_cfg.get("max_gain_pct_with_catalyst", 250)
    catalyst_gain_override = _safe_float(catalyst_gain_override_raw, 250.0)
    settings["catalyst_boost"] = {
        "enabled": bool(catalyst_cfg.get("enabled", False)),
        "min_news_score": _safe_float(catalyst_cfg.get("min_news_score", 0.60), 0.60),
        "min_event_score": min_event_score,
        "score_boost": _safe_float(catalyst_cfg.get("score_boost", 2.0), 2.0),
        "allow_rel_volume_relax": bool(catalyst_cfg.get("allow_rel_volume_relax", True)),
        "min_relative_volume_with_catalyst": _safe_float(
            catalyst_cfg.get("min_relative_volume_with_catalyst", settings["min_rel_vol"]),
            settings["min_rel_vol"],
        ),
        "allow_vwap_relax": bool(catalyst_cfg.get("allow_vwap_relax", True)),
        "max_gain_pct_catalyst": catalyst_gain_override,
        "max_gain_pct_with_catalyst": catalyst_gain_override,
    }
    settings["strong_catalyst_override"] = {
        "enabled": bool(strong_override_cfg.get("enabled", False)),
        "min_news_score": _safe_float(strong_override_cfg.get("min_news_score", 3.0), 3.0),
        "min_event_score": _safe_float(strong_override_cfg.get("min_event_score", 2.5), 2.5),
        "max_day_gain_pct": _safe_float(strong_override_cfg.get("max_day_gain_pct", 250.0), 250.0),
        "max_spread_pct": _safe_float(strong_override_cfg.get("max_spread_pct", 4.0), 4.0),
        "keep_min_price_filter": bool(strong_override_cfg.get("keep_min_price_filter", True)),
        "keep_bad_quote_filter": bool(strong_override_cfg.get("keep_bad_quote_filter", True)),
        "keep_unstable_quote_filter": bool(strong_override_cfg.get("keep_unstable_quote_filter", True)),
    }
    settings["high_conviction_news_override"] = {
        "enabled": bool(high_conviction_cfg.get("enabled", False)),
        "min_catalyst_score": _safe_float(high_conviction_cfg.get("min_catalyst_score", 8.0), 8.0),
        "min_event_score": _safe_float(high_conviction_cfg.get("min_event_score", 7.0), 7.0),
        "min_news_score": _safe_float(high_conviction_cfg.get("min_news_score", 7.0), 7.0),
        "min_relative_volume": _safe_float(high_conviction_cfg.get("min_relative_volume", 1.5), 1.5),
        "require_positive_sentiment": bool(high_conviction_cfg.get("require_positive_sentiment", True)),
    }
    log.info(
        "DYNAMIC_CONFIG_EFFECTIVE min_price=%s max_price=%s min_gain=%s max_gain=%s "
        "min_avg_vol=%s min_rel_vol=%s min_atr_expansion_ratio=%s max_spread_pct=%s "
        "max_symbols=%s catalyst_boost_enabled=%s catalyst_min_news_score=%s catalyst_score_boost=%s "
        "strong_catalyst_enabled=%s strong_catalyst_min_news_score=%s strong_catalyst_min_event_score=%s "
        "strong_catalyst_max_day_gain_pct=%s strong_catalyst_max_spread_pct=%s "
        "strong_catalyst_keep_min_price_filter=%s strong_catalyst_keep_bad_quote_filter=%s "
        "strong_catalyst_keep_unstable_quote_filter=%s high_conviction_news_override_enabled=%s "
        "high_conviction_min_news_score=%s high_conviction_min_event_score=%s "
        "high_conviction_min_catalyst_score=%s high_conviction_min_relative_volume=%s cfg_keys=%s",
        settings["min_price"],
        settings["max_price"],
        settings["min_gain"],
        settings["max_gain"],
        settings["min_avg_vol"],
        settings["min_rel_vol"],
        settings["min_atr_expansion_ratio"],
        settings["max_spread_pct"],
        settings["max_symbols"],
        settings["catalyst_boost"]["enabled"],
        settings["catalyst_boost"]["min_news_score"],
        settings["catalyst_boost"]["score_boost"],
        settings["strong_catalyst_override"]["enabled"],
        settings["strong_catalyst_override"]["min_news_score"],
        settings["strong_catalyst_override"]["min_event_score"],
        settings["strong_catalyst_override"]["max_day_gain_pct"],
        settings["strong_catalyst_override"]["max_spread_pct"],
        settings["strong_catalyst_override"]["keep_min_price_filter"],
        settings["strong_catalyst_override"]["keep_bad_quote_filter"],
        settings["strong_catalyst_override"]["keep_unstable_quote_filter"],
        settings["high_conviction_news_override"]["enabled"],
        settings["high_conviction_news_override"]["min_news_score"],
        settings["high_conviction_news_override"]["min_event_score"],
        settings["high_conviction_news_override"]["min_catalyst_score"],
        settings["high_conviction_news_override"]["min_relative_volume"],
        ",".join(sorted(str(k) for k in cfg.keys())),
    )
    return settings


def _scaled_catalyst_score(score: Any) -> float:
    try:
        value = float(score or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if value != value:
        return 0.0
    return value * 10.0 if 0.0 < value <= 1.0 else value


def _high_conviction_news_override_decision(
    *,
    symbol: str,
    core_symbols: set[str],
    cfg: Mapping[str, Any] | None,
    news_score: Any,
    event_score: Any,
    catalyst_score: Any,
    relative_volume: Any = None,
    sentiment: Any = None,
    catalyst_type: Any = None,
) -> tuple[bool, str]:
    sym = str(symbol or "").strip().upper()
    hc_cfg = cfg if isinstance(cfg, Mapping) else {}
    if not bool(hc_cfg.get("enabled", False)):
        return False, "override_disabled"
    if not sym:
        return False, "invalid_symbol"
    if sym in core_symbols:
        return False, "core_symbol"
    if sym in ETF_SYMBOLS:
        return False, "etf_symbol"

    if max(_safe_float(news_score, 0.0), _safe_float(event_score, 0.0), _scaled_catalyst_score(catalyst_score)) <= 0.0:
        return False, "missing_scores"
    allowed, reason, _score, thresholds = evaluate_high_conviction_news_override(
        {"trading": {"dynamic": {"high_conviction_news_override": hc_cfg}}},
        catalyst_type=catalyst_type or "earnings_beat",
        news_score=news_score,
        event_score=event_score,
        catalyst_score=catalyst_score,
        relative_volume=relative_volume,
        sentiment=sentiment,
    )
    if allowed:
        return True, reason
    if reason == "relative_volume_below_threshold":
        return False, "relative_volume %.2f < %.2f" % (
            _safe_float(relative_volume, 0.0),
            thresholds.get("min_relative_volume", 1.5),
        )
    if reason == "non_positive_sentiment":
        return False, "sentiment %.2f <= 0" % _safe_float(sentiment, 0.0)
    return False, reason


def _sector_strength_context(
    market_client: Any,
    cfg: dict[str, Any],
    snapshots: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[str, float | None, float]:
    ss_cfg = cfg.get("sector_strength") or {}
    bench_sym = str(ss_cfg.get("benchmark", "SPY") or "SPY").strip().upper() or "SPY"
    benchmark_gain: float | None = None
    if bool(ss_cfg.get("enabled", False)):
        try:
            bench_snap = (
                snapshots.get(bench_sym, {}) if snapshots is not None else market_client.get_snapshot(bench_sym)
            )
            benchmark_gain = float(bench_snap.get("day_gain_pct", 0) or 0)
        except Exception:
            benchmark_gain = None
    min_alpha_vs_bench = _safe_float(ss_cfg.get("min_outperformance_vs_benchmark_pct"), 0.0)
    return bench_sym, benchmark_gain, min_alpha_vs_bench


def _evaluate_dynamic_scan_rows(
    *,
    market_client: Any | None,
    movers: Sequence[Mapping[str, Any]],
    core_symbols: list[str],
    state: dict[str, Any],
    cfg: dict[str, Any],
    snapshots: Mapping[str, Mapping[str, Any]],
    avg_volumes: Mapping[str, float],
    bars_1m_by_symbol: Mapping[str, pd.DataFrame | None],
    bars_5m_by_symbol: Mapping[str, pd.DataFrame | None],
    tape_bonus: float,
    theme_scores: Mapping[str, float] | None,
    benchmark_gain: float | None,
    benchmark_symbol: str,
    min_alpha_vs_bench: float,
    news_by_symbol: Mapping[str, NewsCatalyst] | None,
    corporate_actions_by_symbol: Mapping[str, Mapping[str, Any]] | None,
    premarket_artifacts: Mapping[str, Mapping[str, Any]] | None,
    emit_logs: bool,
    now: datetime | None = None,
) -> tuple[list[DynamicScanCandidate], list[DynamicScanCandidate]]:
    core = {str(s).upper() for s in core_symbols}
    settings = _dynamic_scan_settings(cfg)
    broker_is_paper = bool(cfg.get("broker_is_paper", False))
    retry_settings = _dynamic_quote_retry_settings(
        cfg,
        env="paper" if broker_is_paper else "live",
    )
    retry_enabled = (
        bool(retry_settings.get("enabled"))
        and int(retry_settings.get("attempts", 0) or 0) > 0
        and market_client is not None
    )
    catalyst_cfg = settings.get("catalyst_boost") or {}
    artifact_map = {
        str(sym or "").strip().upper(): dict(data)
        for sym, data in (premarket_artifacts or {}).items()
        if str(sym or "").strip()
    }
    scan_timestamp = now or datetime.now(timezone.utc)
    if scan_timestamp.tzinfo is None:
        scan_timestamp = scan_timestamp.replace(tzinfo=timezone.utc)
    scan_timestamp_iso = scan_timestamp.isoformat()

    accepted: list[DynamicScanCandidate] = []
    rejected: list[DynamicScanCandidate] = []

    def log_dynamic_news(symbol: str, news_score: int, article_count: int) -> None:
        log.info(
            "DYNAMIC_NEWS symbol=%s news_score=%d articles=%d",
            symbol,
            news_score,
            int(article_count),
        )

    def log_premarket_catalyst(symbol: str, *, score: float, catalyst_type: str | None, headline: str | None, age_minutes: float | None, reason: str | None = None) -> None:
        if not emit_logs:
            return
        if score > 0:
            log.info(
                "PREMARKET_CATALYST_APPLIED symbol=%s score=%.2f catalyst_type=%s headline=%s age_minutes=%s",
                symbol,
                float(score),
                str(catalyst_type or "unknown"),
                (headline or "")[:180],
                "n/a" if age_minutes is None else f"{float(age_minutes):.1f}",
            )
            return
        log.info(
            "PREMARKET_CATALYST_MISS symbol=%s reason=%s",
            symbol,
            reason or "no_catalyst_score",
        )

    def reject(
        symbol: str,
        reason: str,
        *,
        price: float = 0.0,
        day_gain_pct: float = 0.0,
        volume: float = 0.0,
        avg_volume: float = 0.0,
        relative_volume: float = 0.0,
        spread_pct: float = 0.0,
        quality: DynamicScanQuality | None = None,
        print_reason: str | None = None,
        news_score: int = 0,
        catalyst_age_minutes: float | None = None,
        event_score: float = 0.0,
        catalyst_score: float = 0.0,
        catalyst_type: str | None = None,
        catalyst_headline: str | None = None,
        article_count: int = 0,
        premarket_injected: bool = False,
        effective_min_rel_volume: float | None = None,
        catalyst_fastlane_active: bool = False,
    ) -> None:
        vwap_above = quality.price_above_vwap if quality is not None else None
        _log_dynamic_reject(
            symbol,
            reason,
            price=price,
            day_gain_pct=day_gain_pct,
            relative_volume=relative_volume,
            spread_pct=spread_pct,
            vwap_above=vwap_above,
            news_score=int(news_score),
            catalyst_age_minutes=catalyst_age_minutes,
            emit_logs=emit_logs,
            print_reason=print_reason,
        )
        if emit_logs:
            log.info(
                "DYNAMIC_REJECT_FUNNEL reason=%s symbol=%s stage=scanner",
                reason,
                symbol,
            )
        news_cat = (news_by_symbol or {}).get(symbol)
        news_meta = get_cached_news_metadata(symbol, emit_log=False)
        artifact = artifact_map.get(str(symbol or "").strip().upper(), {})
        try:
            artifact_news_score = float(artifact.get("news_score", 0) or 0) if isinstance(artifact, Mapping) else 0.0
        except (TypeError, ValueError):
            artifact_news_score = 0.0
        try:
            artifact_event_score = float(artifact.get("event_score", 0.0) or 0.0) if isinstance(artifact, Mapping) else 0.0
        except (TypeError, ValueError):
            artifact_event_score = 0.0
        try:
            artifact_catalyst_score = float(artifact.get("catalyst_score", 0.0) or 0.0) if isinstance(artifact, Mapping) else 0.0
        except (TypeError, ValueError):
            artifact_catalyst_score = 0.0
        rejected_news_score = int(
            max(
                int(news_score or 0),
                int(news_cat.score) if news_cat is not None else 0,
                int((news_meta or {}).get("score", 0) or 0),
                int(artifact_news_score or 0),
                int(math.ceil(artifact_event_score)) if artifact_event_score > 0 else 0,
            )
        )
        rejected_event_score = float(
            max(
                float(event_score or 0.0),
                artifact_event_score,
                float((news_meta or {}).get("event_score", 0.0) or 0.0),
            )
        )
        rejected_catalyst_score = float(
            max(
                float(catalyst_score or 0.0),
                artifact_catalyst_score,
                float(
                    max(
                        int(news_cat.score) if news_cat is not None else 0,
                        int((news_meta or {}).get("score", 0) or 0),
                        int(artifact_news_score or 0),
                    )
                    / 10.0
                ),
                float(rejected_event_score / 10.0),
            )
        )
        rejected_catalyst_type = (
            catalyst_type
            or (str(artifact.get("catalyst_type") or "").strip() if isinstance(artifact, Mapping) else "")
            or (news_cat.catalyst_type if news_cat is not None else None)
            or (news_meta or {}).get("catalyst_type")
        )
        rejected_headline = (
            catalyst_headline
            or (str(artifact.get("headline") or "").strip() if isinstance(artifact, Mapping) else "")
            or (news_cat.headline if news_cat is not None else None)
            or str((news_meta or {}).get("headline", "") or "")
            or None
        )
        later_high, later_return = _later_same_day_high_return_from_bars(
            bars_1m_by_symbol.get(symbol),
            rejected_at=scan_timestamp,
            rejection_price=float(price or 0.0),
        )
        rejected.append(
            DynamicScanCandidate(
                symbol=symbol,
                score=0.0,
                accepted=False,
                rejection_reason=reason,
                price=price,
                day_gain_pct=day_gain_pct,
                volume=volume,
                avg_volume=avg_volume,
                relative_volume=relative_volume,
                spread_pct=spread_pct,
                quality=quality,
                news_score=rejected_news_score,
                event_score=rejected_event_score,
                catalyst_score=rejected_catalyst_score,
                article_count=int(article_count or 0),
                news_headline=rejected_headline,
                catalyst_type=rejected_catalyst_type,
                catalyst_headline=rejected_headline,
                catalyst_age_minutes=catalyst_age_minutes,
                timestamp=scan_timestamp_iso,
                later_same_day_high=later_high,
                later_same_day_return_pct=later_return,
                bid=current_quote_quality.get("bid"),
                ask=current_quote_quality.get("ask"),
                quote_timestamp=current_quote_quality.get("quote_timestamp"),
                quote_age_seconds=current_quote_quality.get("quote_age_seconds"),
                quote_source=current_quote_quality.get("quote_source"),
                scan_timestamp=scan_timestamp_iso,
                effective_min_rel_volume=effective_min_rel_volume,
                scanner_effective_min_rel_volume=effective_min_rel_volume,
                catalyst_fastlane_active=bool(catalyst_fastlane_active),
                premarket_injected=bool(premarket_injected),
                corporate_action_type=current_corp_action.get("type") or None,
                corporate_action_severity=current_corp_action.get("severity") or None,
                corporate_action_description=current_corp_action.get("description") or None,
            )
        )

    if emit_logs:
        candidate_symbols = [
            str(item.get("symbol") or "").strip().upper()
            for item in movers
            if not is_option_symbol(str(item.get("symbol", "")).upper())
        ]
        candidate_symbols = [sym for sym in candidate_symbols if sym]
        artifact_symbols = set(artifact_map.keys())
        matched_symbols = sorted(set(candidate_symbols) & artifact_symbols)
        missed_symbols = sorted(set(candidate_symbols) - artifact_symbols)
        log.info(
            "DYNAMIC_ARTIFACT_COVERAGE artifact_symbols_count=%d candidates=%d matched_count=%d missed_count=%d matched_symbols=%s missed_symbols=%s",
            len(artifact_symbols),
            len(candidate_symbols),
            len(matched_symbols),
            len(missed_symbols),
            ",".join(matched_symbols[:25]) or "none",
            ",".join(missed_symbols[:25]) or "none",
        )

    for item in movers:
        symbol = str(item["symbol"]).upper()
        current_quote_quality: dict[str, Any] = {}
        current_corp_action = dict((corporate_actions_by_symbol or {}).get(symbol) or {})
        if is_option_symbol(symbol):
            reject(symbol, "option_symbol", print_reason=f"DYNAMIC_SCAN reject {symbol}: option symbol")
            continue
        news_cat = (news_by_symbol or {}).get(symbol)
        news_meta = get_cached_news_metadata(symbol, emit_log=False)
        artifact = artifact_map.get(symbol, {})
        artifact_confirmed = _log_catalyst_lookup(symbol, artifact=artifact, emit_logs=emit_logs)
        if artifact:
            _artifact_match_score = float(max(
                _artifact_float(artifact, "news_score"),
                _artifact_float(artifact, "event_score"),
                _artifact_float(artifact, "catalyst_score") * 10.0,
            ))
            log.info(
                "DYNAMIC_CATALYST_COVERAGE symbol=%s has_artifact=true score=%.2f",
                symbol,
                _artifact_match_score,
            )
            log.info(
                "CATALYST_MATCH_DEBUG symbol=%s source=%s headline=%s age_minutes=%s raw_score=%.2f mapped_score=%.2f",
                symbol,
                str(artifact.get("source") or "unknown"),
                str(artifact.get("headline") or "")[:180],
                "n/a" if artifact.get("age_minutes") is None else f"{float(artifact.get('age_minutes')):.1f}",
                _artifact_match_score,
                _artifact_match_score,
            )
        else:
            log.info("DYNAMIC_CATALYST_COVERAGE symbol=%s has_artifact=false", symbol)
        artifact_score = _artifact_float(artifact, "news_score")
        artifact_event_score = _artifact_float(artifact, "event_score")
        artifact_catalyst_score = _artifact_float(artifact, "catalyst_score")
        artifact_ranking_score = _artifact_float(artifact, "ranking_score", _artifact_float(artifact, "score"))
        artifact_headline = str(artifact.get("headline", "") or "").strip()
        artifact_catalyst_type = str(artifact.get("catalyst_type", "") or "").strip() or None
        artifact_age_minutes = artifact.get("age_minutes")
        artifact_article_count = _artifact_int(artifact, "article_count")
        news_article_count = int(
            max(
                artifact_article_count,
                int(
                    getattr(news_cat, "article_count", 0)
                    if news_cat is not None
                    else (news_meta or {}).get("article_count", 0)
                    or 0
                ),
            )
        )
        news_sentiment = float(
            getattr(news_cat, "sentiment", 0.0) if news_cat is not None else (news_meta or {}).get("sentiment", 0.0)
            or 0.0
        )
        news_event_score = float(
            max(
                artifact_event_score or 0.0,
                float((news_meta or {}).get("event_score", 0.0) or 0.0),
            )
        )
        catalyst_score = max(
            float(artifact_catalyst_score or 0.0),
            float(max(news_cat.score if news_cat is not None else 0, int((news_meta or {}).get("score", 0) or 0), int(artifact_score or 0)) / 10.0),
            float(max(artifact_event_score or 0.0, news_event_score or 0.0) / 10.0),
        )
        news_catalyst_type = (
            news_cat.catalyst_type
            if news_cat is not None
            else (news_meta or {}).get("catalyst_type")
        )
        if artifact_catalyst_type:
            news_catalyst_type = artifact_catalyst_type
        news_headline = (
            artifact_headline
            or (
                news_cat.headline
                if news_cat is not None
                else str((news_meta or {}).get("headline", "") or "") or None
            )
        )
        news_score = int(
            max(
                int(news_cat.score) if news_cat is not None else 0,
                int((news_meta or {}).get("score", 0) or 0),
                int(artifact_score or 0),
                int(math.ceil(artifact_event_score)) if artifact_event_score > 0 else 0,
            )
        )
        catalyst_age_minutes = _resolve_catalyst_age_minutes(
            symbol,
            artifact_age_minutes=artifact_age_minutes,
            news_cat=news_cat,
        )
        if emit_logs:
            log.info(
                "DYNAMIC_SCANNER_SCORE_TRACE symbol=%s artifact_match=%s ranking_score=%.2f "
                "artifact_news_score=%.2f artifact_event_score=%.2f artifact_catalyst_score=%.2f "
                "live_news_score=%d live_event_score=%.2f final_news_score=%d "
                "final_event_score=%.2f final_catalyst_score=%.2f catalyst_type=%s source=%s",
                symbol,
                str(bool(artifact)).lower(),
                float(artifact_ranking_score),
                float(artifact_score),
                float(artifact_event_score),
                float(artifact_catalyst_score),
                int(news_cat.score) if news_cat is not None else int((news_meta or {}).get("score", 0) or 0),
                float((news_meta or {}).get("event_score", 0.0) or 0.0),
                int(news_score),
                float(news_event_score),
                float(catalyst_score),
                str(news_catalyst_type or "none"),
                str(artifact.get("source") or (news_meta or {}).get("source") or "none"),
            )
            log.info(
                "DYNAMIC_CATALYST_SCORE symbol=%s artifact_match=%s news_score=%d event_score=%.2f catalyst_score=%.2f catalyst_age_minutes=%s source=%s catalyst_type=%s",
                symbol,
                str(bool(artifact)).lower(),
                int(news_score),
                float(news_event_score),
                float(catalyst_score),
                "n/a" if catalyst_age_minutes is None else f"{float(catalyst_age_minutes):.1f}",
                str(artifact.get("source") or (news_meta or {}).get("source") or "none"),
                str(news_catalyst_type or "none"),
            )
            log.info(
                "DYNAMIC_SCORE_SOURCE symbol=%s news_score=%d catalyst_score=%.2f event_score=%.2f from_artifact=%s from_live_feed=%s",
                symbol,
                int(news_score),
                float(catalyst_score),
                float(news_event_score),
                str(bool(artifact)).lower(),
                str(bool(news_cat is not None or news_meta)).lower(),
            )
        if news_score > 0:
            log_premarket_catalyst(
                symbol,
                score=float(max(float(artifact_score), float(artifact_event_score), float(news_score))),
                catalyst_type=news_catalyst_type or artifact_catalyst_type,
                headline=news_headline,
                age_minutes=catalyst_age_minutes,
            )
        else:
            reason = "no_matching_item" if not artifact and news_cat is None and not news_meta else "no_catalyst_score"
            log_premarket_catalyst(
                symbol,
                score=0.0,
                catalyst_type=None,
                headline=None,
                age_minutes=None,
                reason=reason,
            )
        if emit_logs:
            log_dynamic_news(
                symbol,
                news_score,
                news_article_count,
            )

        if symbol in core:
            reject(symbol, "already core", print_reason=f"DYNAMIC_SCAN reject {symbol}: already core")
            continue

        if current_corp_action:
            action_type = str(current_corp_action.get("type") or "unknown")
            severity = str(current_corp_action.get("severity") or "unknown")
            if current_corp_action.get("block"):
                log.info(
                    "DYNAMIC_CORP_ACTION_FILTER symbol=%s type=%s severity=%s description=%s",
                    symbol,
                    action_type,
                    severity,
                    str(current_corp_action.get("description") or "")[:180],
                )
                reject(
                    symbol,
                    f"corporate_action_{action_type}",
                    print_reason=f"DYNAMIC_SCAN reject {symbol}: corporate_action type={action_type} severity={severity}",
                    news_score=news_score,
                    catalyst_age_minutes=catalyst_age_minutes,
                    event_score=news_event_score,
                    catalyst_score=catalyst_score,
                    catalyst_type=news_catalyst_type,
                    catalyst_headline=news_headline,
                )
                continue
            log.info(
                "DYNAMIC_CORP_ACTION_ALLOW symbol=%s type=%s severity=%s description=%s",
                symbol,
                action_type,
                severity,
                str(current_corp_action.get("description") or "")[:180],
            )

        cooldown_minutes = dynamic_reentry_cooldown_remaining_minutes(symbol, state=state)
        if cooldown_minutes is not None and cooldown_minutes > 0.0:
            if emit_logs:
                log.info(
                    "DYNAMIC_REENTRY_BLOCK symbol=%s minutes_remaining=%d reason=post_exit_cooldown",
                    symbol,
                    int(math.ceil(cooldown_minutes)),
                )
                log.info(
                    "DYNAMIC_REENTRY_COOLDOWN symbol=%s remaining_minutes=%d",
                    symbol,
                    int(math.ceil(cooldown_minutes)),
                )
            reject(symbol, "cooldown", print_reason=f"DYNAMIC_SCAN reject {symbol}: cooldown")
            continue

        snapshot = snapshots.get(symbol, {})
        current_quote_quality = _snapshot_quote_quality(snapshot, scan_timestamp=scan_timestamp)
        price, day_gain_pct, volume, bid, ask = _quote_fields(snapshot)
        avg_volume = _safe_float(avg_volumes.get(symbol), 1.0)

        if price <= 0 or bid <= 0 or ask <= 0:
            reject(
                symbol,
                "bad quote",
                price=price,
                day_gain_pct=day_gain_pct,
                volume=volume,
                avg_volume=avg_volume,
                print_reason=f"DYNAMIC_SCAN reject {symbol}: bad quote price={price} bid={bid} ask={ask}",
            )
            continue

        mid = (ask + bid) / 2.0
        if mid <= 0:
            reject(
                symbol,
                "invalid_quote",
                price=price,
                day_gain_pct=day_gain_pct,
                volume=volume,
                avg_volume=avg_volume,
                print_reason=f"DYNAMIC_SCAN reject {symbol}: invalid_quote bid={bid} ask={ask} mid={mid}",
            )
            continue

        spread_pct = abs(ask - bid) / mid * 100.0
        if spread_pct > 15.0:
            if retry_enabled:
                attempts = int(retry_settings.get("attempts", 0) or 0)
                delay_seconds = float(retry_settings.get("delay_seconds", 0.0) or 0.0)
                retry_succeeded = False
                retry_bad_quote = False
                for attempt in range(1, attempts + 1):
                    if emit_logs:
                        log.info(
                            "QUOTE_RETRY_START symbol=%s reason=unstable_quote attempt=%d",
                            symbol,
                            attempt,
                        )
                    if delay_seconds > 0.0:
                        time.sleep(delay_seconds)
                    retry_snapshot = _call_get_snapshot_once(market_client, symbol)
                    retry_price, retry_day_gain_pct, retry_volume, retry_bid, retry_ask = _quote_fields(
                        retry_snapshot
                    )
                    if retry_price <= 0 or retry_bid <= 0 or retry_ask <= 0:
                        snapshot = retry_snapshot
                        price = retry_price
                        day_gain_pct = retry_day_gain_pct
                        volume = retry_volume
                        bid = retry_bid
                        ask = retry_ask
                        current_quote_quality = _snapshot_quote_quality(
                            snapshot,
                            scan_timestamp=scan_timestamp,
                        )
                        retry_bad_quote = True
                        break
                    retry_mid = (retry_ask + retry_bid) / 2.0
                    if retry_mid <= 0:
                        snapshot = retry_snapshot
                        price = retry_price
                        day_gain_pct = retry_day_gain_pct
                        volume = retry_volume
                        bid = retry_bid
                        ask = retry_ask
                        current_quote_quality = _snapshot_quote_quality(
                            snapshot,
                            scan_timestamp=scan_timestamp,
                        )
                        retry_bad_quote = True
                        break
                    retry_spread_pct = abs(retry_ask - retry_bid) / retry_mid * 100.0
                    if retry_spread_pct <= 15.0:
                        snapshot = retry_snapshot
                        current_quote_quality = _snapshot_quote_quality(
                            snapshot,
                            scan_timestamp=scan_timestamp,
                        )
                        price = retry_price
                        day_gain_pct = retry_day_gain_pct
                        volume = retry_volume
                        bid = retry_bid
                        ask = retry_ask
                        mid = retry_mid
                        spread_pct = retry_spread_pct
                        retry_succeeded = True
                        if emit_logs:
                            log.info(
                                "QUOTE_RETRY_SUCCESS symbol=%s attempt=%d",
                                symbol,
                                attempt,
                            )
                        break
                if retry_bad_quote:
                    reject(
                        symbol,
                        "bad quote",
                        price=price,
                        day_gain_pct=day_gain_pct,
                        volume=volume,
                        avg_volume=avg_volume,
                        print_reason=f"DYNAMIC_SCAN reject {symbol}: bad quote price={price} bid={bid} ask={ask}",
                    )
                    continue
                if retry_succeeded:
                    # Continue through the normal downstream scanner gates using the fresh quote.
                    pass
                else:
                    if emit_logs:
                        log.info(
                            "QUOTE_RETRY_FAILED symbol=%s attempts=%d",
                            symbol,
                            attempts,
                        )
                        log.info(
                            "QUOTE_RETRY_FINAL_REJECT symbol=%s reason=unstable_quote",
                            symbol,
                        )
            if spread_pct <= 15.0:
                pass
            else:
                log.warning("Unstable quote %s", symbol)
                if (
                    emit_logs
                    and _market_open_relax_window_active(scan_timestamp)
                    and _catalyst_rvol_relax_signal(
                        news_score=news_score,
                        catalyst_score=catalyst_score,
                        artifact=artifact,
                    )
                ):
                    log.info(
                        "CATALYST_RVOL_RELAX_BLOCKED symbol=%s reason=unstable_quote",
                        symbol,
                    )
                if int(news_score or 0) >= _STRONG_NEWS_OVERRIDE_MIN_SCORE and emit_logs:
                    log.info(
                        "DYNAMIC_NEWS_OVERRIDE_BLOCKED symbol=%s news_score=%d catalyst_age_minutes=%s reason=%s",
                        symbol,
                        int(news_score),
                        "n/a" if catalyst_age_minutes is None else f"{float(catalyst_age_minutes):.1f}",
                        "unstable quote spread_pct %.2f" % float(spread_pct),
                    )
                reject(
                    symbol,
                    "unstable quote",
                    price=price,
                    day_gain_pct=day_gain_pct,
                    volume=volume,
                    avg_volume=avg_volume,
                    spread_pct=spread_pct,
                    news_score=news_score,
                    catalyst_age_minutes=catalyst_age_minutes,
                    print_reason=f"DYNAMIC_SCAN reject {symbol}: unstable quote spread={spread_pct:.2f}%",
                )
                continue

        rel_volume = volume / avg_volume if avg_volume > 0 else 0.0
        bars_1m = bars_1m_by_symbol.get(symbol)
        bars_5m = bars_5m_by_symbol.get(symbol)
        catalyst_type = news_catalyst_type
        catalyst_headline = news_headline
        quality = _intraday_quality_from_bars(
            bars_1m=bars_1m,
            bars_5m=bars_5m,
            price=price,
        )
        strong_news_override = evaluate_strong_news_dynamic_override(
            symbol=symbol,
            news_score=news_score,
            catalyst_age_minutes=catalyst_age_minutes,
            price=price,
            spread_pct=spread_pct,
            quality=quality,
            bars_1m=bars_1m,
            min_price=float(settings["min_price"]),
            max_spread_pct=float(settings["max_spread_pct"]),
            emit_logs=emit_logs,
        )
        catalyst_type_norm = str(catalyst_type or "").strip().lower()
        strong_override_cfg = settings["strong_catalyst_override"]
        strong_catalyst_override_allowed, _strong_catalyst_override_reason, _strong_catalyst_score, _ = (
            evaluate_high_conviction_news_override(
                {
                    "trading": {
                        "dynamic": {
                            "high_conviction_news_override": {
                                "enabled": bool(strong_override_cfg.get("enabled")),
                                "min_news_score": float(strong_override_cfg.get("min_news_score", 3.0) or 3.0),
                                "min_event_score": float(strong_override_cfg.get("min_event_score", 2.5) or 2.5),
                                "min_catalyst_score": float(strong_override_cfg.get("min_event_score", 2.5) or 2.5),
                                "min_relative_volume": 0.0,
                                "max_catalyst_age_minutes": _STRONG_NEWS_OVERRIDE_MAX_CATALYST_AGE_MIN,
                                "require_positive_sentiment": False,
                            }
                        }
                    }
                },
                catalyst_type=catalyst_type_norm or "earnings_beat",
                news_score=news_score,
                event_score=news_event_score,
                catalyst_score=catalyst_score,
                relative_volume=rel_volume,
                sentiment=news_sentiment,
                catalyst_age_minutes=catalyst_age_minutes,
            )
        )
        strong_catalyst_override = bool(
            strong_catalyst_override_allowed
            and news_score < _STRONG_NEWS_OVERRIDE_MIN_SCORE
        )
        if (
            strong_news_override.candidate
            and not strong_news_override.applied
            and catalyst_age_minutes is not None
            and float(catalyst_age_minutes) <= _STRONG_NEWS_OVERRIDE_MAX_CATALYST_AGE_MIN
        ):
            try:
                _candidate_catalyst_max_gain = float(
                    catalyst_cfg.get("max_gain_pct_catalyst", catalyst_cfg.get("max_gain_pct_with_catalyst", 250.0)) or 250.0
                )
            except (TypeError, ValueError):
                _candidate_catalyst_max_gain = 250.0
            try:
                _candidate_catalyst_min_score = float(
                    catalyst_cfg.get("min_news_score", 0.6) or 0.6
                )
            except (TypeError, ValueError):
                _candidate_catalyst_min_score = 0.6
            _strong_news_candidate_max_gain = float(_STRONG_NEWS_OVERRIDE_MAX_GAIN_PCT)
            if catalyst_score >= _candidate_catalyst_min_score:
                _strong_news_candidate_max_gain = max(
                    _strong_news_candidate_max_gain,
                    _candidate_catalyst_max_gain,
                )
            if day_gain_pct > _strong_news_candidate_max_gain:
                reject(
                    symbol,
                    "gain filter",
                    price=price,
                    day_gain_pct=day_gain_pct,
                    volume=volume,
                    avg_volume=avg_volume,
                    relative_volume=rel_volume,
                    spread_pct=spread_pct,
                    quality=quality,
                    news_score=news_score,
                    catalyst_age_minutes=catalyst_age_minutes,
                    event_score=news_event_score,
                    catalyst_score=catalyst_score,
                    catalyst_type=catalyst_type,
                    catalyst_headline=catalyst_headline,
                    print_reason=(
                        f"DYNAMIC_SCAN reject {symbol}: gain filter gain={day_gain_pct:.1f} "
                        f"max={_strong_news_candidate_max_gain:.0f} strong_news_override"
                    ),
                )
                continue
            if settings["require_above_vwap"] and quality.price_above_vwap is not True:
                if not vwap_reclaim_within_last_bars(bars_1m, price):
                    reject(
                        symbol,
                        "not above VWAP",
                        price=price,
                        day_gain_pct=day_gain_pct,
                        volume=volume,
                        avg_volume=avg_volume,
                        relative_volume=rel_volume,
                        spread_pct=spread_pct,
                        quality=quality,
                        news_score=news_score,
                        catalyst_age_minutes=catalyst_age_minutes,
                        print_reason=f"DYNAMIC_SCAN reject {symbol}: not above VWAP strong_news_override",
                    )
                    continue
        if strong_news_override.applied:
            effective_max_gain_pct = float(
                strong_news_override.max_gain_pct or _STRONG_NEWS_OVERRIDE_MAX_GAIN_PCT
            )
        elif strong_catalyst_override:
            effective_max_gain_pct = float(strong_override_cfg.get("max_day_gain_pct", 250.0) or 250.0)
        else:
            effective_max_gain_pct = float(settings["max_gain"])
        effective_spread_pct = (
            float(strong_override_cfg.get("max_spread_pct", 4.0) or 4.0)
            if strong_catalyst_override
            else float(settings["max_spread_pct"])
        )
        try:
            _news_score_check = float(news_score)
        except (TypeError, ValueError):
            _news_score_check = float(0)
        _age_check = (
            catalyst_age_minutes is not None
            and float(catalyst_age_minutes) <= _STRONG_NEWS_OVERRIDE_MAX_CATALYST_AGE_MIN
        )
        _price_check = price >= float(settings["min_price"])
        _spread_check = spread_pct <= float(settings["max_spread_pct"])
        _override_active_pre = bool(
            _news_score_check >= float(_STRONG_NEWS_OVERRIDE_MIN_SCORE)
            and _age_check
            and _price_check
        )
        if emit_logs:
            if _news_score_check < float(_STRONG_NEWS_OVERRIDE_MIN_SCORE):
                _override_decision_reason = "below_required_score"
            elif not _age_check:
                _override_decision_reason = "stale_or_missing_catalyst_age"
            elif not _price_check:
                _override_decision_reason = "below_min_price"
            else:
                _override_decision_reason = "fresh_score_match"
            log.info(
                "DYNAMIC_OVERRIDE_DECISION symbol=%s reason=%s override_active=%s required_score=%.2f actual_score=%.2f",
                symbol,
                _override_decision_reason,
                bool(_override_active_pre),
                float(_STRONG_NEWS_OVERRIDE_MIN_SCORE),
                float(_news_score_check),
            )
            _override_debug_line = (
                "DYNAMIC_OVERRIDE_DEBUG symbol=%s news_score=%s news_score_check=%s "
                "catalyst_age_minutes=%s age_check=%s price=%.4f price_check=%s "
                "spread_pct=%.4f spread_check=%s override_active=%s type(news_score)=%s repr(news_score)=%r"
                % (
                    symbol,
                    str(news_score),
                    f"{_news_score_check:.3f}",
                    "n/a" if catalyst_age_minutes is None else f"{float(catalyst_age_minutes):.1f}",
                    bool(_age_check),
                    float(price),
                    bool(_price_check),
                    float(spread_pct),
                    bool(_spread_check),
                    bool(_override_active_pre),
                    type(news_score).__name__,
                    news_score,
                )
            )
            log.info(_override_debug_line)
            print(_override_debug_line, flush=True)
        strong_news_floor_active = bool(_override_active_pre)
        effective_min_rel_vol = (
            float(_STRONG_NEWS_OVERRIDE_MIN_REL_VOLUME)
            if strong_news_floor_active
            else float(settings["min_rel_vol"])
        )
        catalyst_rvol_relax_age = _catalyst_rvol_relax_age_minutes(catalyst_age_minutes)
        catalyst_rvol_relax_signal = _catalyst_rvol_relax_signal(
            news_score=news_score,
            catalyst_score=catalyst_score,
            artifact=artifact,
        ) and artifact_confirmed
        catalyst_rvol_relax_window = _market_open_relax_window_active(scan_timestamp)
        catalyst_rvol_relax_reason = ""
        if not catalyst_rvol_relax_window:
            catalyst_rvol_relax_reason = "outside_open_window"
        elif not catalyst_rvol_relax_signal:
            catalyst_rvol_relax_reason = "weak_catalyst"
        elif not math.isfinite(catalyst_rvol_relax_age) or catalyst_rvol_relax_age > _CATALYST_RVOL_RELAX_MAX_AGE_MIN:
            catalyst_rvol_relax_reason = "stale_catalyst"
        elif price < float(settings["min_price"]):
            catalyst_rvol_relax_reason = "below_min_price"
        elif price > float(settings["max_price"]):
            catalyst_rvol_relax_reason = "above_max_price"
        elif spread_pct > float(effective_spread_pct):
            catalyst_rvol_relax_reason = "spread_too_wide"
        else:
            catalyst_rvol_relax_reason = "ok"
        catalyst_rvol_relax_active = catalyst_rvol_relax_reason == "ok"
        if catalyst_rvol_relax_active:
            old_min_rel_vol = float(settings["min_rel_vol"])
            effective_min_rel_vol = min(
                float(effective_min_rel_vol),
                float(_CATALYST_RVOL_RELAX_MIN_REL_VOLUME),
            )
            if emit_logs and rel_volume < old_min_rel_vol:
                log.info(
                    "CATALYST_RVOL_RELAXED symbol=%s rel=%.3f old_min=%.2f new_min=%.2f age=%.1f",
                    symbol,
                    float(rel_volume),
                    old_min_rel_vol,
                    float(effective_min_rel_vol),
                    float(catalyst_rvol_relax_age),
                )
        elif emit_logs and (rel_volume < float(settings["min_rel_vol"]) or catalyst_rvol_relax_signal):
            log.info(
                "CATALYST_RVOL_RELAX_BLOCKED symbol=%s reason=%s",
                symbol,
                catalyst_rvol_relax_reason,
            )
        if emit_logs:
            _override_line = (
                "DYNAMIC_OVERRIDE symbol=%s news_score=%d base_min_rel=%.3f "
                "effective_min_rel=%.3f override_active=%s"
                % (
                    symbol,
                    int(news_score),
                    float(settings["min_rel_vol"]),
                    float(effective_min_rel_vol),
                    bool(strong_news_floor_active),
                )
            )
            log.info(_override_line)
            print(_override_line, flush=True)
        if strong_news_floor_active and emit_logs:
            _override_active_line = (
                "DYNAMIC_OVERRIDE_ACTIVE symbol=%s news_score=%d rel_volume=%.3f "
                "effective_min_rel_volume=%.3f"
                % (
                    symbol,
                    int(news_score),
                    float(rel_volume),
                    float(effective_min_rel_vol),
                )
            )
            log.info(_override_active_line)
            print(_override_active_line, flush=True)

        if emit_logs:
            print(
                f"DYNAMIC_SCAN {symbol}: price={price} gain={day_gain_pct} "
                f"vol={volume} avg={avg_volume} rel={rel_volume:.2f} "
                f"spread={spread_pct:.2f}% range={quality.intraday_range_pct:.2f}% "
                f"vwap_above={quality.price_above_vwap} trend5m={quality.five_min_trend_aligned} "
                f"atr_exp={quality.atr_expansion_ratio if quality.atr_expansion_ratio is not None else 'n/a'} "
                f"news_score={news_score}",
                flush=True,
            )
            log.info(
                "ATR_DEBUG_DETAIL symbol=%s current_atr=%s baseline_atr=%s atr_expansion_ratio=%s",
                symbol,
                "n/a" if quality.current_atr is None else f"{quality.current_atr:.4f}",
                "n/a" if quality.baseline_atr is None else f"{quality.baseline_atr:.4f}",
                "n/a" if quality.atr_expansion_ratio is None else f"{quality.atr_expansion_ratio:.4f}",
            )
            if news_score != 0 and news_headline:
                _news_line = (
                    "NEWS_CATALYST symbol=%s score=%d catalyst_type=%s headline=%s"
                    % (
                        symbol,
                        news_score,
                        catalyst_type or "unknown",
                        news_headline.replace("\n", " ")[:180],
                    )
                )
                log.info(
                    _news_line,
                )
                print(_news_line, flush=True)
            if strong_catalyst_override:
                log.info(
                    "DYNAMIC_STRONG_CATALYST_OVERRIDE symbol=%s news_score=%d event_score=%.2f catalyst_type=%s min_news_score=%.2f min_event_score=%.2f effective_max_gain_pct=%.0f effective_spread_pct=%.1f",
                    symbol,
                    int(news_score),
                    float(news_event_score),
                    catalyst_type_norm or "none",
                    float(strong_override_cfg.get("min_news_score", 3.0) or 3.0),
                    float(strong_override_cfg.get("min_event_score", 2.5) or 2.5),
                    effective_max_gain_pct,
                    effective_spread_pct,
                )

        if price < settings["min_price"]:
            reject(
                symbol,
                "below_min_price",
                price=price,
                day_gain_pct=day_gain_pct,
                volume=volume,
                avg_volume=avg_volume,
                relative_volume=rel_volume,
                spread_pct=spread_pct,
                quality=quality,
                print_reason=f"DYNAMIC_SCAN reject {symbol}: below_min_price price={price:.2f} min={settings['min_price']:.2f} catalyst_score={catalyst_score:.2f}",
            )
            continue
        if price > settings["max_price"]:
            reject(
                symbol,
                "above_max_price",
                price=price,
                day_gain_pct=day_gain_pct,
                volume=volume,
                avg_volume=avg_volume,
                relative_volume=rel_volume,
                spread_pct=spread_pct,
                quality=quality,
                print_reason=f"DYNAMIC_SCAN reject {symbol}: above_max_price price={price:.2f} max={settings['max_price']:.2f} catalyst_score={catalyst_score:.2f}",
            )
            continue
        catalyst_active = bool(catalyst_cfg.get("enabled")) and catalyst_score >= float(catalyst_cfg.get("min_news_score", 0.6) or 0.6)
        strong_catalyst_relax_ok = (
            strong_catalyst_override
            and avg_volume >= settings["min_avg_vol"]
            and price >= settings["min_price"]
            and spread_pct <= effective_spread_pct
        )
        catalyst_relax_ok = (
            catalyst_active
            and avg_volume >= settings["min_avg_vol"]
            and price >= settings["min_price"]
            and spread_pct <= effective_spread_pct
        )
        catalyst_rel_volume_floor = float(catalyst_cfg.get("min_relative_volume_with_catalyst", settings["min_rel_vol"]) or settings["min_rel_vol"])
        catalyst_max_gain = float(
            catalyst_cfg.get(
                "max_gain_pct_catalyst",
                catalyst_cfg.get("max_gain_pct_with_catalyst", 250.0),
            )
            or 250.0
        )
        allow_rel_volume_relax = bool(catalyst_cfg.get("allow_rel_volume_relax", True))
        allow_vwap_relax = bool(catalyst_cfg.get("allow_vwap_relax", True))
        ultra_momentum_mode = bool(cfg.get("ultra_momentum_mode", False))
        early_ok, early_reason = news_early_entry_passes(
            news_score=news_score,
            relative_volume=rel_volume,
            price_above_vwap=quality.price_above_vwap,
            spread_pct=spread_pct,
            bars_1m=bars_1m,
        )
        gain_ok_with_catalyst = (
            (catalyst_relax_ok or strong_catalyst_relax_ok)
            and day_gain_pct <= max(catalyst_max_gain, effective_max_gain_pct)
            and (day_gain_pct <= catalyst_max_gain or ultra_momentum_mode or strong_catalyst_override)
        )
        rel_volume_ok_with_catalyst = (
            (catalyst_relax_ok or strong_catalyst_relax_ok)
            and allow_rel_volume_relax
            and rel_volume >= catalyst_rel_volume_floor
        )
        strong_news_rel_ok = strong_news_floor_active and rel_volume >= float(effective_min_rel_vol)
        aggressive_scan_active = bool(settings.get("aggressive_mode", False))
        aggressive_fast_lane_scan = bool(
            aggressive_scan_active
            and (
                news_score >= 3
                or catalyst_score >= 0.25
                or news_event_score >= 2.0
                or (rel_volume >= 2.0 and day_gain_pct >= 4.0)
                or (rel_volume >= 4.0 and day_gain_pct >= 2.0)
                or day_gain_pct >= 8.0
            )
        )
        if aggressive_fast_lane_scan:
            effective_min_rel_vol = min(
                float(effective_min_rel_vol),
                float(settings.get("catalyst_min_rel_vol", effective_min_rel_vol)),
            )
        vwap_ok_with_catalyst = (catalyst_relax_ok or strong_catalyst_relax_ok) and allow_vwap_relax
        min_gain_floor = (
            float(settings.get("catalyst_min_gain", settings["min_gain"]))
            if aggressive_fast_lane_scan
            else float(settings["min_gain"])
        )
        min_gain_ok = bool(day_gain_pct >= min_gain_floor or early_ok)
        max_gain_ok = bool(day_gain_pct <= effective_max_gain_pct or early_ok or gain_ok_with_catalyst)
        base_gain_ok = bool(min_gain_ok and max_gain_ok)
        gain_ok = bool(base_gain_ok or early_ok or gain_ok_with_catalyst)
        rel_volume_ok = bool(
            rel_volume >= effective_min_rel_vol
            or early_ok
            or rel_volume_ok_with_catalyst
            or strong_news_rel_ok
            or (catalyst_rvol_relax_active and rel_volume >= float(effective_min_rel_vol))
            or (aggressive_fast_lane_scan and rel_volume >= float(settings.get("catalyst_min_rel_vol", effective_min_rel_vol)))
        )
        if aggressive_fast_lane_scan and emit_logs:
            log.info(
                "DYNAMIC_AGGRESSIVE_FAST_LANE symbol=%s trigger=scanner_threshold_relax gain=%.2f rel=%.3f effective_min_gain=%.2f effective_min_rel=%.3f",
                symbol,
                float(day_gain_pct),
                float(rel_volume),
                float(min_gain_floor),
                float(effective_min_rel_vol),
            )
        breakout_ok = bool(quality.price_above_vwap is True or vwap_reclaim_within_last_bars(bars_1m, price))
        _log_dynamic_gate_debug(
            symbol,
            price_ok=True,
            spread_ok=spread_pct <= effective_spread_pct,
            avg_volume_ok=avg_volume >= settings["min_avg_vol"],
            rel_volume_ok=rel_volume_ok,
            gain_ok=gain_ok,
            min_gain_ok=min_gain_ok,
            max_gain_ok=max_gain_ok,
            breakout_ok=breakout_ok,
            catalyst_ok=bool(catalyst_relax_ok or strong_catalyst_relax_ok),
            entry_alignment_ok=True,
            price=price,
            day_gain_pct=day_gain_pct,
            max_gain_pct=effective_max_gain_pct,
            relative_volume=rel_volume,
            spread_pct=spread_pct,
            news_score=news_score,
            event_score=news_event_score,
            catalyst_score=catalyst_score,
            emit_logs=emit_logs,
        )
        if emit_logs:
            log.info(
                "DYNAMIC_GAIN_FILTER_LIMITS symbol=%s normal_max_gain_pct=%.2f catalyst_override_max_gain_pct=%.2f effective_max_gain_pct=%.2f catalyst_backed=%s news_score=%d event_score=%.2f catalyst_score=%.2f",
                symbol,
                float(settings["max_gain"]),
                float(catalyst_max_gain),
                float(max(catalyst_max_gain, effective_max_gain_pct) if (catalyst_relax_ok or strong_catalyst_relax_ok) else effective_max_gain_pct),
                str(bool(catalyst_relax_ok or strong_catalyst_relax_ok)).lower(),
                int(news_score),
                float(news_event_score),
                float(catalyst_score),
            )
        if (
            (gain_ok_with_catalyst or strong_catalyst_override)
            and not (settings["min_gain"] <= day_gain_pct <= settings["max_gain"])
            and emit_logs
        ):
            log.info(
                "DYNAMIC_CATALYST_RELAXED_GATE symbol=%s gate=gain_filter reason=catalyst_backed gain_pct=%.2f base_min=%.2f base_max=%.2f relaxed_max=%.2f news_score=%d event_score=%.2f catalyst_score=%.2f",
                symbol,
                float(day_gain_pct),
                float(settings["min_gain"]),
                float(settings["max_gain"]),
                float(max(catalyst_max_gain, effective_max_gain_pct)),
                int(news_score),
                float(news_event_score),
                float(catalyst_score),
            )
        if (
            not gain_ok
        ):
            gain_reject_reason = "below_min_day_gain" if day_gain_pct < settings["min_gain"] else "gain filter"
            if news_score > 0 or catalyst_score > 0:
                _news_blocked = "NEWS_BLOCKED symbol=%s reason=%s" % (
                    symbol,
                    f"{gain_reject_reason}; {early_reason}",
                )
                log.info(_news_blocked)
                print(_news_blocked, flush=True)
            reject(
                symbol,
                gain_reject_reason,
                price=price,
                day_gain_pct=day_gain_pct,
                volume=volume,
                avg_volume=avg_volume,
                relative_volume=rel_volume,
                spread_pct=spread_pct,
                quality=quality,
                news_score=news_score,
                catalyst_age_minutes=catalyst_age_minutes,
                event_score=news_event_score,
                catalyst_score=catalyst_score,
                catalyst_type=catalyst_type,
                catalyst_headline=catalyst_headline,
                print_reason=(
                    (
                        f"DYNAMIC_SCAN reject {symbol}: below_min_day_gain gain={day_gain_pct:.1f} "
                        f"min={settings['min_gain']:.1f} catalyst_score={catalyst_score:.2f}"
                    )
                    if gain_reject_reason == "below_min_day_gain"
                    else (
                        f"DYNAMIC_SCAN reject {symbol}: gain filter gain={day_gain_pct:.1f} max={effective_max_gain_pct:.0f} "
                        f"catalyst_score={catalyst_score:.2f}"
                    )
                ),
            )
            continue
        if avg_volume < settings["min_avg_vol"] and not early_ok:
            reject(
                symbol,
                "below_min_avg_volume",
                price=price,
                day_gain_pct=day_gain_pct,
                volume=volume,
                avg_volume=avg_volume,
                relative_volume=rel_volume,
                spread_pct=spread_pct,
                quality=quality,
                print_reason=f"DYNAMIC_SCAN reject {symbol}: below_min_avg_volume avg={avg_volume:.0f} min={settings['min_avg_vol']:.0f} catalyst_score={catalyst_score:.2f}",
            )
            continue
        relaxed_rel_volume_floor = (
            float(effective_min_rel_vol)
            if strong_news_rel_ok and float(effective_min_rel_vol) < float(settings["min_rel_vol"])
            else float(catalyst_rel_volume_floor)
        )
        if (rel_volume_ok_with_catalyst or strong_news_rel_ok) and rel_volume < float(settings["min_rel_vol"]) and emit_logs:
            log.info(
                "DYNAMIC_CATALYST_RELAXED_GATE symbol=%s gate=relative_volume old=%.3f new=%.3f rel_volume=%.3f news_score=%d event_score=%.2f catalyst_score=%.2f",
                symbol,
                float(settings["min_rel_vol"]),
                float(relaxed_rel_volume_floor),
                float(rel_volume),
                int(news_score),
                float(news_event_score),
                float(catalyst_score),
            )
        if (
            rel_volume + 1e-9 < effective_min_rel_vol
            and not early_ok
            and not rel_volume_ok_with_catalyst
            and not strong_news_rel_ok
        ):
            if news_score > 0 or catalyst_score > 0:
                _news_blocked = "NEWS_BLOCKED symbol=%s reason=below_min_relative_volume" % symbol
                log.info(_news_blocked)
                print(_news_blocked, flush=True)
            reject(
                symbol,
                "below_min_relative_volume",
                price=price,
                day_gain_pct=day_gain_pct,
                volume=volume,
                avg_volume=avg_volume,
                relative_volume=rel_volume,
                spread_pct=spread_pct,
                quality=quality,
                news_score=news_score,
                catalyst_age_minutes=catalyst_age_minutes,
                print_reason=(
                    f"DYNAMIC_SCAN reject {symbol}: below_min_relative_volume rel={rel_volume:.2f} "
                    f"min={effective_min_rel_vol:.2f} catalyst_score={catalyst_score:.2f}"
                ),
            )
            continue
        if spread_pct > effective_spread_pct and not early_ok:
            if news_score > 0 or catalyst_score > 0:
                _news_blocked = "NEWS_BLOCKED symbol=%s reason=spread too wide" % symbol
                log.info(_news_blocked)
                print(_news_blocked, flush=True)
                reject(
                    symbol,
                    "spread too wide",
                    price=price,
                    day_gain_pct=day_gain_pct,
                    volume=volume,
                    avg_volume=avg_volume,
                    relative_volume=rel_volume,
                    spread_pct=spread_pct,
                    quality=quality,
                print_reason=f"DYNAMIC_SCAN reject {symbol}: spread too wide spread={spread_pct:.2f}% max={effective_spread_pct:.2f}% catalyst_score={catalyst_score:.2f}",
                )
                continue
            reject(
                symbol,
                "spread too wide",
                price=price,
                day_gain_pct=day_gain_pct,
                volume=volume,
                avg_volume=avg_volume,
                relative_volume=rel_volume,
                spread_pct=spread_pct,
                quality=quality,
                print_reason=f"DYNAMIC_SCAN reject {symbol}: spread too wide spread={spread_pct:.2f}% max={effective_spread_pct:.2f}% catalyst_score={catalyst_score:.2f}",
            )
            continue
        if (
            settings["min_intraday_range_pct"] > 0
            and quality.intraday_range_pct < settings["min_intraday_range_pct"]
            and not early_ok
        ):
            reject(
                symbol,
                "intraday range",
                price=price,
                day_gain_pct=day_gain_pct,
                volume=volume,
                avg_volume=avg_volume,
                relative_volume=rel_volume,
                spread_pct=spread_pct,
                quality=quality,
                print_reason=f"DYNAMIC_SCAN reject {symbol}: intraday range {quality.intraday_range_pct:.2f}% < {settings['min_intraday_range_pct']:.2f}%",
            )
            continue
        if settings["require_above_vwap"] and quality.price_above_vwap is not True and not early_ok:
            if not vwap_ok_with_catalyst and not strong_news_override.applied:
                reject(
                    symbol,
                    "not above VWAP",
                    price=price,
                    day_gain_pct=day_gain_pct,
                    volume=volume,
                    avg_volume=avg_volume,
                    relative_volume=rel_volume,
                    spread_pct=spread_pct,
                    quality=quality,
                    news_score=news_score,
                    catalyst_age_minutes=catalyst_age_minutes,
                    print_reason=f"DYNAMIC_SCAN reject {symbol}: not above VWAP catalyst_score={catalyst_score:.2f}",
                )
                continue
        if (
            settings["require_5m_trend_alignment"]
            and quality.five_min_trend_aligned is not True
            and not early_ok
        ):
            reject(
                symbol,
                "5m trend not aligned",
                price=price,
                day_gain_pct=day_gain_pct,
                volume=volume,
                avg_volume=avg_volume,
                relative_volume=rel_volume,
                spread_pct=spread_pct,
                quality=quality,
                print_reason=f"DYNAMIC_SCAN reject {symbol}: 5m trend not aligned catalyst_score={catalyst_score:.2f}",
            )
            continue
        if (
            settings["min_5m_up_streak"] > 0
            and quality.five_min_up_streak < settings["min_5m_up_streak"]
            and not early_ok
        ):
            if emit_logs:
                log.info(
                    "DYNAMIC_SCAN reject %s: 5m up streak %d < %d",
                    symbol,
                    quality.five_min_up_streak,
                    settings["min_5m_up_streak"],
                )
            reject(symbol, "5m up streak", price=price, day_gain_pct=day_gain_pct, volume=volume, avg_volume=avg_volume, relative_volume=rel_volume, spread_pct=spread_pct, quality=quality)
            continue
        if (
            benchmark_gain is not None
            and day_gain_pct < benchmark_gain + min_alpha_vs_bench
            and not early_ok
        ):
            if emit_logs:
                log.info(
                    "DYNAMIC_SCAN reject %s: vs %s gain %.2f%% < bench %.2f%% + %.2f%%",
                    symbol,
                    benchmark_symbol,
                    day_gain_pct,
                    benchmark_gain,
                    min_alpha_vs_bench,
                )
            reject(symbol, "benchmark underperformance", price=price, day_gain_pct=day_gain_pct, volume=volume, avg_volume=avg_volume, relative_volume=rel_volume, spread_pct=spread_pct, quality=quality)
            continue
        if (
            settings["min_atr_expansion_ratio"] > 0
            and (
                quality.atr_expansion_ratio is None
                or quality.atr_expansion_ratio < settings["min_atr_expansion_ratio"]
            )
            and not early_ok
        ):
            reject(
                symbol,
                "ATR expansion",
                price=price,
                day_gain_pct=day_gain_pct,
                volume=volume,
                avg_volume=avg_volume,
                relative_volume=rel_volume,
                spread_pct=spread_pct,
                quality=quality,
                print_reason=(
                    "DYNAMIC_SCAN reject %s: ATR expansion %s < %.2f"
                    % (
                        symbol,
                        "n/a" if quality.atr_expansion_ratio is None else f"{quality.atr_expansion_ratio:.2f}",
                        settings["min_atr_expansion_ratio"],
                    )
                ),
            )
            continue

        entry_cfg = cfg.get("dynamic_momentum_entry")
        if isinstance(entry_cfg, Mapping) and bool(entry_cfg.get("enabled", True)):
            entry_alignment_score = (
                float(day_gain_pct)
                + max(0.0, float(rel_volume) - 1.0)
                + float(quality.intraday_range_pct)
                + 0.15 * float(quality.five_min_up_streak)
                + float(tape_bonus)
                + (max(float(news_score), float(news_event_score)) * 2.0 if max(float(news_score), float(news_event_score)) > 0 else 0.0)
                + (float(catalyst_cfg.get("score_boost", 2.0) or 2.0) if catalyst_active else 0.0)
            )
            if strong_news_override.applied:
                try:
                    entry_max_spread = float(entry_cfg.get("max_entry_spread_pct", 3.0))
                except (TypeError, ValueError):
                    entry_max_spread = 3.0
                entry_safety_reason = ""
                if spread_pct >= entry_max_spread:
                    entry_safety_reason = "spread_pct %.3f%% >= %.2f%%" % (
                        float(spread_pct),
                        entry_max_spread,
                    )
                elif bool(entry_cfg.get("require_above_vwap", True)) and quality.price_above_vwap is not True:
                    entry_safety_reason = "price not above session VWAP"
                    if vwap_reclaim_within_last_bars(bars_1m, price):
                        entry_safety_reason = ""
                if entry_safety_reason:
                    reason = f"entry_alignment: {entry_safety_reason}"
                    alignment_bypass_ok = _dynamic_alignment_bypass_ok(
                        price=price,
                        spread_pct=spread_pct,
                        avg_volume=avg_volume,
                        day_gain_pct=day_gain_pct,
                        relative_volume=rel_volume,
                        quality=quality,
                        settings=settings,
                    )
                    if alignment_bypass_ok:
                        log.info(
                            "DYNAMIC_ALIGNMENT_BYPASS symbol=%s gain=%.2f rel=%.3f avg=%.0f spread=%.3f",
                            symbol,
                            float(day_gain_pct),
                            float(rel_volume),
                            float(avg_volume),
                            float(spread_pct),
                        )
                    else:
                        _log_dynamic_gate_debug(
                            symbol,
                            price_ok=True,
                            spread_ok=spread_pct <= effective_spread_pct,
                            avg_volume_ok=avg_volume >= settings["min_avg_vol"],
                            rel_volume_ok=rel_volume_ok,
                            gain_ok=gain_ok,
                            min_gain_ok=min_gain_ok,
                            max_gain_ok=max_gain_ok,
                            breakout_ok=breakout_ok,
                            catalyst_ok=bool(catalyst_relax_ok or strong_catalyst_relax_ok),
                            entry_alignment_ok=False,
                            price=price,
                            day_gain_pct=day_gain_pct,
                            max_gain_pct=effective_max_gain_pct,
                            relative_volume=rel_volume,
                            spread_pct=spread_pct,
                            news_score=news_score,
                            event_score=news_event_score,
                            catalyst_score=catalyst_score,
                            emit_logs=emit_logs,
                        )
                        if news_score > 0 or catalyst_score > 0:
                            _news_blocked = "NEWS_BLOCKED symbol=%s reason=%s" % (symbol, reason)
                            log.info(_news_blocked)
                            print(_news_blocked, flush=True)
                        reject(
                            symbol,
                            reason,
                            price=price,
                            day_gain_pct=day_gain_pct,
                            volume=volume,
                            avg_volume=avg_volume,
                            relative_volume=rel_volume,
                            spread_pct=spread_pct,
                            quality=quality,
                            news_score=news_score,
                            catalyst_age_minutes=catalyst_age_minutes,
                            print_reason=f"DYNAMIC_SCAN reject {symbol}: {reason}",
                        )
                        continue
            else:
                entry_ok, entry_reason = dynamic_momentum_entry_passes(
                    gain_pct=day_gain_pct,
                    relative_volume=rel_volume,
                    vwap_above=bool(quality.price_above_vwap),
                    spread_pct=spread_pct,
                    bars_1m=bars_1m,
                    bars_5m=bars_5m,
                    ref_price=price,
                    cfg=entry_cfg,
                    symbol=symbol,
                    news_score=int(news_score or 0),
                    catalyst_score=float(catalyst_score or 0.0),
                    catalyst_age_minutes=catalyst_age_minutes,
                    is_dynamic=True,
                    alignment_score=float(entry_alignment_score),
                )
                if not entry_ok:
                    reason = f"entry_alignment: {entry_reason}"
                    if _dynamic_alignment_bypass_ok(
                        price=price,
                        spread_pct=spread_pct,
                        avg_volume=avg_volume,
                        day_gain_pct=day_gain_pct,
                        relative_volume=rel_volume,
                        quality=quality,
                        settings=settings,
                    ):
                        log.info(
                            "DYNAMIC_ALIGNMENT_BYPASS symbol=%s gain=%.2f rel=%.3f avg=%.0f spread=%.3f",
                            symbol,
                            float(day_gain_pct),
                            float(rel_volume),
                            float(avg_volume),
                            float(spread_pct),
                        )
                    else:
                        _log_dynamic_gate_debug(
                            symbol,
                            price_ok=True,
                            spread_ok=spread_pct <= effective_spread_pct,
                            avg_volume_ok=avg_volume >= settings["min_avg_vol"],
                            rel_volume_ok=rel_volume_ok,
                            gain_ok=gain_ok,
                            min_gain_ok=min_gain_ok,
                            max_gain_ok=max_gain_ok,
                            breakout_ok=breakout_ok,
                            catalyst_ok=bool(catalyst_relax_ok or strong_catalyst_relax_ok),
                            entry_alignment_ok=False,
                            price=price,
                            day_gain_pct=day_gain_pct,
                            max_gain_pct=effective_max_gain_pct,
                            relative_volume=rel_volume,
                            spread_pct=spread_pct,
                            news_score=news_score,
                            event_score=news_event_score,
                            catalyst_score=catalyst_score,
                            emit_logs=emit_logs,
                        )
                        if news_score > 0 or catalyst_score > 0:
                            _news_blocked = "NEWS_BLOCKED symbol=%s reason=%s" % (symbol, reason)
                            log.info(_news_blocked)
                            print(_news_blocked, flush=True)
                        reject(
                            symbol,
                            reason,
                            price=price,
                            day_gain_pct=day_gain_pct,
                            volume=volume,
                            avg_volume=avg_volume,
                            relative_volume=rel_volume,
                            spread_pct=spread_pct,
                            quality=quality,
                            news_score=news_score,
                            catalyst_age_minutes=catalyst_age_minutes,
                            print_reason=f"DYNAMIC_SCAN reject {symbol}: {reason}",
                        )
                        continue

        if emit_logs:
            print(f"DYNAMIC_SCAN accept {symbol}", flush=True)
        if (
            day_gain_pct < _DYNAMIC_LIVE_LOOSEN_OLD_MIN_GAIN
            and day_gain_pct >= float(settings["min_gain"])
            and float(settings["min_gain"]) < _DYNAMIC_LIVE_LOOSEN_OLD_MIN_GAIN
        ):
            _log_dynamic_loosened_pass(
                symbol=symbol,
                old_reason="below_min_day_gain",
                old_threshold=_DYNAMIC_LIVE_LOOSEN_OLD_MIN_GAIN,
                new_threshold=float(settings["min_gain"]),
                observed=float(day_gain_pct),
                emit_logs=emit_logs,
            )
        if (
            rel_volume < _DYNAMIC_LIVE_LOOSEN_OLD_MIN_REL_VOLUME
            and rel_volume >= float(effective_min_rel_vol)
            and float(effective_min_rel_vol) < _DYNAMIC_LIVE_LOOSEN_OLD_MIN_REL_VOLUME
        ):
            _log_dynamic_loosened_pass(
                symbol=symbol,
                old_reason="below_min_relative_volume",
                old_threshold=_DYNAMIC_LIVE_LOOSEN_OLD_MIN_REL_VOLUME,
                new_threshold=float(effective_min_rel_vol),
                observed=float(rel_volume),
                emit_logs=emit_logs,
            )
        theme_name, theme_bonus = symbol_theme_bonus(symbol, theme_scores or {}, cfg)
        if emit_logs and theme_name and theme_bonus > 0.0:
            log.info(
                "DYNAMIC_THEME_MOMENTUM symbol=%s theme=%s theme_score=%.2f bonus=%.2f",
                symbol,
                theme_name,
                float((theme_scores or {}).get(theme_name, 0.0) or 0.0),
                float(theme_bonus),
            )
        score = (
            day_gain_pct
            + max(0.0, rel_volume - 1.0)
            + quality.intraday_range_pct
            + 0.15 * float(quality.five_min_up_streak)
            + tape_bonus
            + theme_bonus
            + (max(float(news_score), float(news_event_score)) * 2.0 if max(float(news_score), float(news_event_score)) > 0 else 0.0)
            + (float(catalyst_cfg.get("score_boost", 2.0) or 2.0) if catalyst_active else 0.0)
        )
        if emit_logs:
            _accepted_line = (
                "DYNAMIC_ACCEPTED symbol=%s news_score=%d article_count=%d sentiment_score=%.2f catalyst_type=%s catalyst_score=%.2f catalyst_headline=%s catalyst_age_minutes=%s event_score=%.2f score=%.2f"
                % (
                    symbol,
                    int(news_score),
                    int(news_article_count),
                    float(news_sentiment),
                    str(news_catalyst_type or "none"),
                    float(catalyst_score),
                    (catalyst_headline or "")[:120],
                    "n/a" if catalyst_age_minutes is None else f"{float(catalyst_age_minutes):.1f}",
                    float(news_event_score),
                    float(score),
                )
            )
            log.info(_accepted_line)
            print(_accepted_line, flush=True)
        accepted.append(
            DynamicScanCandidate(
                symbol=symbol,
                score=score,
                accepted=True,
                rejection_reason=None,
                price=price,
                day_gain_pct=day_gain_pct,
                volume=volume,
                avg_volume=avg_volume,
                relative_volume=rel_volume,
                spread_pct=spread_pct,
                quality=quality,
                news_score=news_score,
                event_score=news_event_score,
                catalyst_score=catalyst_score,
                article_count=int(news_article_count),
                news_headline=news_headline,
                catalyst_type=catalyst_type or (str(news_catalyst_type) if news_catalyst_type else None),
                catalyst_headline=catalyst_headline,
                catalyst_age_minutes=float(catalyst_age_minutes) if catalyst_age_minutes is not None else None,
                theme=theme_name,
                theme_bonus=theme_bonus,
                timestamp=scan_timestamp_iso,
                bid=current_quote_quality.get("bid"),
                ask=current_quote_quality.get("ask"),
                quote_timestamp=current_quote_quality.get("quote_timestamp"),
                quote_age_seconds=current_quote_quality.get("quote_age_seconds"),
                quote_source=current_quote_quality.get("quote_source"),
                scan_timestamp=scan_timestamp_iso,
                effective_min_rel_volume=float(effective_min_rel_vol),
                scanner_effective_min_rel_volume=float(effective_min_rel_vol),
                catalyst_fastlane_active=bool(catalyst_rvol_relax_active),
                premarket_injected=bool(artifact_confirmed),
                corporate_action_type=current_corp_action.get("type") or None,
                corporate_action_severity=current_corp_action.get("severity") or None,
                corporate_action_description=current_corp_action.get("description") or None,
            )
        )

    accepted.sort(key=lambda x: x.score, reverse=True)
    return accepted, rejected


def _scan_candidates_per_symbol(
    market_client: Any,
    core_symbols: list[str],
    cfg: dict[str, Any],
    *,
    emit_logs: bool = True,
    news_config: Mapping[str, Any] | None = None,
    news_max_age_seconds: float | None = None,
    premarket_artifacts: Mapping[str, Mapping[str, Any]] | None = None,
    history_user_id: str | None = None,
    history_project_root: Path | None = None,
    now: datetime | None = None,
) -> DynamicScanBatchResult:
    if not cfg.get("enabled", False):
        if emit_logs:
            print("DYNAMIC_SCAN disabled", flush=True)
        return DynamicScanBatchResult([], [], [], 0)

    started = time.perf_counter()
    state = load_state()
    movers = _expand_mover_feed(market_client, cfg)
    movers, core_skipped = _filter_core_movers(movers, core_symbols, emit_logs=False)
    mover_symbols = [str(item["symbol"]).upper() for item in movers]
    news_by_symbol = fetch_recent_news_catalysts(
        market_client,
        mover_symbols,
        config=news_config,
        max_age_seconds=news_max_age_seconds,
    )
    core_skipped += _append_news_dynamic_movers(movers, mover_symbols, news_by_symbol, core_symbols)
    corporate_actions_by_symbol = _lookup_corporate_action_annotations(
        market_client,
        mover_symbols,
        cfg,
        now=now,
        emit_logs=emit_logs,
    )
    if emit_logs:
        log.info("DYNAMIC_SCAN_CORE_SKIPPED_SUMMARY count=%d", core_skipped)
    tape_bonus = _tape_momentum_bonus(market_client, cfg)
    bench_sym, benchmark_gain, min_alpha_vs_bench = _sector_strength_context(market_client, cfg)
    if emit_logs:
        log.info(
            "DYNAMIC_SCAN feed_size=%d tape_bonus=%.3f benchmark_%s=%s",
            len(movers),
            tape_bonus,
            bench_sym,
            "n/a" if benchmark_gain is None else f"{benchmark_gain:.2f}%",
        )
    snapshots: dict[str, dict[str, Any]] = {}
    avg_volumes: dict[str, float] = {}
    bars_1m_by_symbol: dict[str, pd.DataFrame | None] = {}
    bars_5m_by_symbol: dict[str, pd.DataFrame | None] = {}
    movers = [m for m in movers if not is_option_symbol(str(m.get("symbol", "")).upper())]
    for item in movers:
        symbol = str(item["symbol"]).upper()
        if is_option_symbol(symbol):
            continue
        try:
            snapshots[symbol] = dict(market_client.get_snapshot(symbol))
        except Exception:
            snapshots[symbol] = {}
        try:
            avg_volumes[symbol] = float(market_client.get_avg_volume(symbol))
        except Exception:
            avg_volumes[symbol] = 1.0
        bars_1m_by_symbol[symbol] = _call_get_bars(market_client, symbol, timeframe="1Min", limit=60)
        bars_5m_by_symbol[symbol] = _call_get_bars(market_client, symbol, timeframe="5Min", limit=12)
    theme_scores = _theme_momentum_context(market_client, cfg, snapshots)
    accepted, rejected = _evaluate_dynamic_scan_rows(
        market_client=market_client,
        movers=movers,
        core_symbols=core_symbols,
        state=state,
        cfg=cfg,
        snapshots=snapshots,
        avg_volumes=avg_volumes,
        bars_1m_by_symbol=bars_1m_by_symbol,
        bars_5m_by_symbol=bars_5m_by_symbol,
        tape_bonus=tape_bonus,
        theme_scores=theme_scores,
        benchmark_gain=benchmark_gain,
        benchmark_symbol=bench_sym,
        min_alpha_vs_bench=min_alpha_vs_bench,
        news_by_symbol=news_by_symbol,
        corporate_actions_by_symbol=corporate_actions_by_symbol,
        premarket_artifacts=premarket_artifacts,
        emit_logs=emit_logs,
        now=now,
    )
    settings = _dynamic_scan_settings(cfg)
    selected = [row.symbol for row in accepted[: settings["max_symbols"]]]
    _persist_selected_dynamic_candidate_bars(
        selected_rows=accepted[: settings["max_symbols"]],
        bars_1m_by_symbol=bars_1m_by_symbol,
        bars_5m_by_symbol=bars_5m_by_symbol,
        history_user_id=history_user_id,
        history_project_root=history_project_root,
        now=now,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    if emit_logs:
        print(f"DYNAMIC_SCAN selected={selected}", flush=True)
    result = DynamicScanBatchResult(selected, accepted, rejected, elapsed_ms)
    log_dynamic_scan_rejection_summary(result, emit_logs=emit_logs)
    try:
        persist_dynamic_scan_history(
            result,
            cfg,
            user_id=history_user_id,
            project_root=history_project_root,
        )
    except Exception:
        log.debug("dynamic scan artifact write failed", exc_info=True)
    return result


def scan_candidates_batch(
    market_client: Any,
    core_symbols: list[str],
    cfg: dict[str, Any],
    *,
    emit_logs: bool = True,
    news_config: Mapping[str, Any] | None = None,
    news_max_age_seconds: float | None = None,
    premarket_artifacts: Mapping[str, Mapping[str, Any]] | None = None,
    history_user_id: str | None = None,
    history_project_root: Path | None = None,
    now: datetime | None = None,
) -> DynamicScanBatchResult:
    """Batch dynamic scanner: collect market data first, then score in memory."""
    if not cfg.get("enabled", False):
        if emit_logs:
            print("DYNAMIC_SCAN disabled", flush=True)
            print(
                "DYNAMIC_SCAN_BATCH candidates=0 accepted=0 rejected=0 elapsed_ms=0",
                flush=True,
            )
        return DynamicScanBatchResult([], [], [], 0)

    started = time.perf_counter()
    state = load_state()
    movers = _expand_mover_feed(market_client, cfg)
    movers, core_skipped = _filter_core_movers(movers, core_symbols, emit_logs=False)
    mover_symbols = [
        str(item["symbol"]).upper()
        for item in movers
        if not is_option_symbol(str(item.get("symbol", "")).upper())
    ]
    news_by_symbol = fetch_recent_news_catalysts(
        market_client,
        mover_symbols,
        config=news_config,
        max_age_seconds=news_max_age_seconds,
    )
    core_skipped += _append_news_dynamic_movers(movers, mover_symbols, news_by_symbol, core_symbols)
    corporate_actions_by_symbol = _lookup_corporate_action_annotations(
        market_client,
        mover_symbols,
        cfg,
        now=now,
        emit_logs=emit_logs,
    )
    if emit_logs:
        log.info("DYNAMIC_SCAN_CORE_SKIPPED_SUMMARY count=%d", core_skipped)
    ss_cfg = cfg.get("sector_strength") or {}
    bench_sym = str(ss_cfg.get("benchmark", "SPY") or "SPY").strip().upper() or "SPY"
    snapshot_symbols = list(
        dict.fromkeys(
            [s for s in mover_symbols if not is_option_symbol(s)]
            + ([bench_sym] if bool(ss_cfg.get("enabled", False)) else [])
            + (theme_etf_symbols(cfg) if theme_intelligence_enabled(cfg) else [])
        )
    )
    snapshots = _call_get_snapshots_batch(market_client, snapshot_symbols)
    avg_volumes = _call_get_avg_volumes_batch(market_client, mover_symbols)
    bars_1m_by_symbol = _call_get_bars_batch(market_client, mover_symbols, timeframe="1Min", limit=60)
    bars_5m_by_symbol = _call_get_bars_batch(market_client, mover_symbols, timeframe="5Min", limit=12)
    tape_bonus = _tape_momentum_bonus(market_client, cfg)
    theme_scores = _theme_momentum_context(market_client, cfg, snapshots)
    bench_sym, benchmark_gain, min_alpha_vs_bench = _sector_strength_context(
        market_client,
        cfg,
        snapshots,
    )
    if emit_logs:
        log.info(
            "DYNAMIC_SCAN feed_size=%d tape_bonus=%.3f benchmark_%s=%s",
            len(movers),
            tape_bonus,
            bench_sym,
            "n/a" if benchmark_gain is None else f"{benchmark_gain:.2f}%",
        )
    accepted, rejected = _evaluate_dynamic_scan_rows(
        market_client=market_client,
        movers=movers,
        core_symbols=core_symbols,
        state=state,
        cfg=cfg,
        snapshots=snapshots,
        avg_volumes=avg_volumes,
        bars_1m_by_symbol=bars_1m_by_symbol,
        bars_5m_by_symbol=bars_5m_by_symbol,
        tape_bonus=tape_bonus,
        theme_scores=theme_scores,
        benchmark_gain=benchmark_gain,
        benchmark_symbol=bench_sym,
        min_alpha_vs_bench=min_alpha_vs_bench,
        news_by_symbol=news_by_symbol,
        corporate_actions_by_symbol=corporate_actions_by_symbol,
        premarket_artifacts=premarket_artifacts,
        emit_logs=emit_logs,
        now=now,
    )
    settings = _dynamic_scan_settings(cfg)
    selected = [row.symbol for row in accepted[: settings["max_symbols"]]]
    _persist_selected_dynamic_candidate_bars(
        selected_rows=accepted[: settings["max_symbols"]],
        bars_1m_by_symbol=bars_1m_by_symbol,
        bars_5m_by_symbol=bars_5m_by_symbol,
        history_user_id=history_user_id,
        history_project_root=history_project_root,
        now=now,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    if emit_logs:
        print(f"DYNAMIC_SCAN selected={selected}", flush=True)
        for row in accepted[: settings["max_symbols"]]:
            print(
                "DYNAMIC_SELECTED symbol=%s score=%.2f news_score=%d event_score=%.2f catalyst_score=%.2f "
                "article_count=%d premarket_injected=%s catalyst_type=%s headline=%s age_minutes=%s"
                % (
                    row.symbol,
                    float(row.score),
                    int(row.news_score),
                    float(row.event_score),
                    float(row.catalyst_score),
                    int(row.article_count),
                    str(bool(row.premarket_injected)).lower(),
                    str(row.catalyst_type or "none"),
                    (row.catalyst_headline or "")[:120],
                    "n/a" if row.catalyst_age_minutes is None else f"{float(row.catalyst_age_minutes):.1f}",
                ),
                flush=True,
            )
        print(
            "DYNAMIC_SCAN_BATCH candidates=%d accepted=%d rejected=%d elapsed_ms=%d"
            % (len(accepted) + len(rejected), len(accepted), len(rejected), elapsed_ms),
            flush=True,
        )
    result = DynamicScanBatchResult(selected, accepted, rejected, elapsed_ms)
    log_dynamic_scan_rejection_summary(result, emit_logs=emit_logs)
    try:
        persist_dynamic_scan_history(
            result,
            cfg,
            user_id=history_user_id,
            project_root=history_project_root,
        )
    except Exception:
        log.debug("dynamic scan artifact write failed", exc_info=True)
    return result


def scan_dynamic_candidates(
    market_client,
    core_symbols: list[str],
    cfg: dict[str, Any],
    *,
    premarket_artifacts: Mapping[str, Mapping[str, Any]] | None = None,
    history_user_id: str | None = None,
    history_project_root: Path | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Compatibility wrapper returning only selected dynamic symbols."""
    return scan_candidates_batch(
        market_client,
        core_symbols,
        cfg,
        premarket_artifacts=premarket_artifacts,
        history_user_id=history_user_id,
        history_project_root=history_project_root,
        now=now,
    ).selected
