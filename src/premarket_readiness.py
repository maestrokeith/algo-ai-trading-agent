"""Premarket artifact readiness checks shared by startup logs and CLI preflight."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.news_catalyst import (
    PREMARKET_ARTIFACT_TTL_MINUTES,
    load_premarket_artifacts,
    premarket_artifact_paths,
)


PROVIDER_DIAGNOSTICS_NAME = "provider_diagnostics_latest.json"


@dataclass(frozen=True)
class PremarketArtifactStatus:
    """Readiness state for one premarket artifact file."""

    kind: str
    path: Path
    status: str
    present: bool
    age_minutes: float | None = None
    ttl_minutes: int | None = None
    symbols: int = 0
    events: int = 0
    rankings: int = 0
    catalysts: int = 0
    error: str | None = None


@dataclass(frozen=True)
class PremarketReadiness:
    """Aggregate premarket artifact readiness for the live session."""

    status: str
    present: bool
    fresh: bool
    missing: list[str]
    stale: list[str]
    artifacts: list[PremarketArtifactStatus]
    catalyst_ranked_symbols: int
    ranking_count: int
    catalyst_count: int
    event_count: int
    max_age_minutes: float | None
    provider_diagnostics: dict[str, dict[str, Any]]
    provider_diagnostics_path: Path
    provider_diagnostics_present: bool


def _parse_dt(raw: Any, *, fallback_path: Path | None = None) -> datetime | None:
    text = str(raw or "").strip()
    if text:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            dt = None
        else:
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    if fallback_path is not None:
        try:
            return datetime.fromtimestamp(fallback_path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return None
    return None


def _sequence_len(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _symbol_set(rows: Any) -> set[str]:
    out: set[str] = set()
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        sym = str(row.get("symbol") or row.get("ticker") or "").strip().upper()
        if sym:
            out.add(sym)
    return out


def _artifact_status(kind: str, path: Path, *, now: datetime) -> PremarketArtifactStatus:
    if not path.exists():
        return PremarketArtifactStatus(kind=kind, path=path, status="missing", present=False)
    try:
        payload = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception as exc:
        return PremarketArtifactStatus(
            kind=kind,
            path=path,
            status="unreadable",
            present=True,
            error=f"{type(exc).__name__}: {exc}",
        )
    if not isinstance(payload, Mapping):
        return PremarketArtifactStatus(kind=kind, path=path, status="unreadable", present=True, error="not_mapping")
    generated_at = _parse_dt(payload.get("generated_at"), fallback_path=path)
    ttl_raw = payload.get("ttl_minutes")
    try:
        ttl_minutes = int(ttl_raw or PREMARKET_ARTIFACT_TTL_MINUTES)
    except (TypeError, ValueError):
        ttl_minutes = PREMARKET_ARTIFACT_TTL_MINUTES
    if int(ttl_minutes) == 60 and str(payload.get("source") or "").strip().lower() == "news_5am":
        ttl_minutes = PREMARKET_ARTIFACT_TTL_MINUTES
    age_minutes: float | None = None
    status = "present"
    if generated_at is not None:
        now_cmp = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        age_minutes = max(0.0, (now_cmp.astimezone(timezone.utc) - generated_at.astimezone(timezone.utc)).total_seconds() / 60.0)
        status = "stale" if age_minutes > float(ttl_minutes) else "fresh"
    symbols = _sequence_len(payload.get("symbols"))
    return PremarketArtifactStatus(
        kind=kind,
        path=path,
        status=status,
        present=True,
        age_minutes=age_minutes,
        ttl_minutes=ttl_minutes,
        symbols=symbols,
        events=_sequence_len(payload.get("events")),
        rankings=_sequence_len(payload.get("rankings")),
        catalysts=_sequence_len(payload.get("catalysts")),
    )


def _provider_diagnostics_path(project_root: Path) -> Path:
    return project_root / "data" / "premarket" / PROVIDER_DIAGNOSTICS_NAME


def _load_provider_diagnostics(project_root: Path) -> tuple[dict[str, dict[str, Any]], Path, bool]:
    path = _provider_diagnostics_path(project_root)
    if not path.exists():
        return {}, path, False
    try:
        payload = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {}, path, True
    if not isinstance(payload, Mapping):
        return {}, path, True
    providers = payload.get("providers")
    if not isinstance(providers, Mapping):
        return {}, path, True
    return {
        str(name): dict(row)
        for name, row in providers.items()
        if isinstance(row, Mapping)
    }, path, True


def _provider_bool(row: Mapping[str, Any] | None, key: str) -> bool:
    if not isinstance(row, Mapping):
        return False
    return bool(row.get(key))


def check_premarket_readiness(
    project_root: Path,
    *,
    now: datetime | None = None,
) -> PremarketReadiness:
    """Return aggregate readiness for ``data/premarket/latest_*.json`` artifacts."""
    now = now or datetime.now(timezone.utc)
    statuses = [
        _artifact_status(kind, path, now=now)
        for kind, path in premarket_artifact_paths(project_root).items()
    ]
    missing = [row.kind for row in statuses if row.status == "missing"]
    stale = [row.kind for row in statuses if row.status == "stale"]
    unreadable = [row.kind for row in statuses if row.status == "unreadable"]
    present = all(row.present for row in statuses)
    fresh = present and not stale and not unreadable
    if missing:
        status = "missing"
    elif unreadable:
        status = "unreadable"
    elif stale:
        status = "stale"
    elif fresh:
        status = "fresh"
    else:
        status = "present"

    catalyst_ranked_symbols: set[str] = set()
    for row in statuses:
        if not row.present or row.status == "unreadable":
            continue
        try:
            payload = json.loads(row.path.read_text(encoding="utf-8") or "{}")
        except Exception:
            continue
        if not isinstance(payload, Mapping):
            continue
        catalyst_ranked_symbols.update(_symbol_set(payload.get("rankings")))
        catalyst_ranked_symbols.update(_symbol_set(payload.get("catalysts")))

    ages = [row.age_minutes for row in statuses if row.age_minutes is not None]
    provider_diagnostics, diagnostics_path, diagnostics_present = _load_provider_diagnostics(project_root)
    status = "fresh_empty" if status == "fresh" and len(catalyst_ranked_symbols) == 0 else status
    return PremarketReadiness(
        status=status,
        present=present,
        fresh=fresh,
        missing=missing,
        stale=stale,
        artifacts=statuses,
        catalyst_ranked_symbols=len(catalyst_ranked_symbols),
        ranking_count=sum(row.rankings for row in statuses),
        catalyst_count=sum(row.catalysts for row in statuses),
        event_count=sum(row.events for row in statuses),
        max_age_minutes=max(ages) if ages else None,
        provider_diagnostics=provider_diagnostics,
        provider_diagnostics_path=diagnostics_path,
        provider_diagnostics_present=diagnostics_present,
    )


def premarket_runtime_ready(readiness: PremarketReadiness) -> bool:
    """True when live catalyst/news runtime has usable fresh artifacts."""
    return bool(
        readiness.present
        and readiness.fresh
        and readiness.status != "fresh_empty"
        and readiness.catalyst_ranked_symbols > 0
        and (
            readiness.ranking_count > 0
            or readiness.catalyst_count > 0
            or readiness.event_count > 0
        )
    )


def premarket_runtime_reason(readiness: PremarketReadiness) -> str:
    """Return a compact readiness reason for live runtime verification."""
    if premarket_runtime_ready(readiness):
        return "ok"
    if readiness.missing or readiness.stale or not readiness.present or not readiness.fresh:
        return "missing_or_stale_premarket_artifacts"
    if readiness.status == "fresh_empty" or readiness.catalyst_ranked_symbols <= 0:
        return "empty_premarket_artifacts"
    return str(readiness.status or "not_ready")


def premarket_runtime_symbols(project_root: Path) -> list[str]:
    """Return symbols present in premarket artifacts without mutating news runtime caches."""
    symbols: set[str] = set()
    for path in premarket_artifact_paths(project_root).values():
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8") or "{}")
        except Exception:
            continue
        if not isinstance(payload, Mapping):
            continue
        symbols.update(_symbol_set(payload.get("rankings")))
        symbols.update(_symbol_set(payload.get("catalysts")))
        symbols.update(_symbol_set(payload.get("events")))
    return sorted(symbols)


def premarket_runtime_symbol_rows(
    project_root: Path,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return per-symbol catalyst metadata as live runtime would load it."""
    rows = load_premarket_artifacts(
        project_root,
        now=now or datetime.now(timezone.utc),
        emit_log=False,
    )
    out: list[dict[str, Any]] = []
    for symbol, data in sorted(rows.items()):
        if not isinstance(data, Mapping):
            continue
        out.append(
            {
                "symbol": str(symbol or "").strip().upper(),
                "rank": data.get("premarket_rank") or data.get("rank") or "n/a",
                "news_score": data.get("news_score", 0) or 0,
                "event_score": data.get("event_score", 0.0) or 0.0,
                "catalyst_score": data.get("catalyst_score", 0.0) or 0.0,
                "article_count": data.get("article_count", 0) or 0,
                "headline": str(data.get("headline") or data.get("catalyst_headline") or ""),
            }
        )
    return [row for row in out if row["symbol"]]


def format_premarket_runtime_symbol(row: Mapping[str, Any]) -> str:
    """Render one verbose catalyst runtime verification symbol line."""
    def _float(key: str) -> float:
        try:
            return float(row.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    try:
        article_count = int(float(row.get("article_count", 0) or 0))
    except (TypeError, ValueError):
        article_count = 0
    return (
        "PREMARKET_RUNTIME_SYMBOL "
        f"symbol={str(row.get('symbol') or '').strip().upper()} "
        f"rank={row.get('rank') or 'n/a'} "
        f"news_score={_float('news_score'):.2f} "
        f"event_score={_float('event_score'):.2f} "
        f"catalyst_score={_float('catalyst_score'):.2f} "
        f"article_count={article_count} "
        f"headline={str(row.get('headline') or '')[:180]}"
    )


def format_premarket_runtime_verify(
    readiness: PremarketReadiness,
    *,
    symbols: list[str] | tuple[str, ...],
) -> str:
    """Render the read-only live catalyst runtime verification line."""
    ready = premarket_runtime_ready(readiness)
    return (
        "PREMARKET_RUNTIME_VERIFY "
        f"ready={str(ready).lower()} reason={premarket_runtime_reason(readiness)} "
        f"rankings={readiness.ranking_count} catalysts={readiness.catalyst_count} "
        f"events={readiness.event_count} symbols={','.join(symbols) if symbols else 'none'}"
    )


def format_premarket_readiness(readiness: PremarketReadiness) -> str:
    """Render readiness for CLI/preflight output."""
    max_age = "n/a" if readiness.max_age_minutes is None else f"{readiness.max_age_minutes:.1f}"
    lines = [
        (
            "PREMARKET_READINESS "
            f"status={readiness.status} present={str(readiness.present).lower()} "
            f"fresh={str(readiness.fresh).lower()} catalyst_ranked_symbols={readiness.catalyst_ranked_symbols} "
            f"rankings={readiness.ranking_count} catalysts={readiness.catalyst_count} "
            f"events={readiness.event_count} max_age_minutes={max_age}"
        )
    ]
    for row in readiness.artifacts:
        age = "n/a" if row.age_minutes is None else f"{row.age_minutes:.1f}"
        ttl = "n/a" if row.ttl_minutes is None else str(row.ttl_minutes)
        lines.append(
            "PREMARKET_ARTIFACT_STATUS "
            f"kind={row.kind} status={row.status} present={str(row.present).lower()} "
            f"age_minutes={age} ttl_minutes={ttl} symbols={row.symbols} "
            f"events={row.events} rankings={row.rankings} catalysts={row.catalysts} path={row.path}"
        )
    lines.append(
        "PREMARKET_PROVIDER_DIAGNOSTICS "
        f"present={str(readiness.provider_diagnostics_present).lower()} path={readiness.provider_diagnostics_path}"
    )
    for provider, row in sorted(readiness.provider_diagnostics.items()):
        lines.append(
            "PREMARKET_PROVIDER_STATUS "
            f"provider={provider} enabled={str(bool(row.get('enabled'))).lower()} "
            f"request_sent={str(bool(row.get('request_sent'))).lower()} "
            f"http_status={row.get('http_status') if row.get('http_status') is not None else 'none'} "
            f"raw_count={int(row.get('raw_count') or 0)} filtered_count={int(row.get('filtered_count') or 0)} "
            f"rate_limited={str(bool(row.get('rate_limited'))).lower()} "
            f"duration_ms={row.get('duration_ms') if row.get('duration_ms') is not None else '0.0'} "
            f"reason={row.get('reason') or 'ok'}"
        )
    newsapi = readiness.provider_diagnostics.get("newsapi")
    earnings = readiness.provider_diagnostics.get("earnings_overnight")
    earnings_reason = str((earnings or {}).get("reason") or "").strip().lower()
    if (
        newsapi is not None
        and earnings is not None
        and not _provider_bool(newsapi, "enabled")
        and earnings_reason == "depends_on_newsapi_disabled"
    ):
        lines.append(
            "PREMARKET_READY_HINT "
            "reason=newsapi_disabled_earnings_overnight_skipped "
            "detail=earnings_overnight depends on NewsAPI; enable NewsAPI to include overnight earnings coverage"
        )
    return "\n".join(lines)


__all__ = [
    "PremarketArtifactStatus",
    "PremarketReadiness",
    "check_premarket_readiness",
    "format_premarket_readiness",
    "format_premarket_runtime_symbol",
    "format_premarket_runtime_verify",
    "premarket_runtime_ready",
    "premarket_runtime_reason",
    "premarket_runtime_symbol_rows",
    "premarket_runtime_symbols",
]
