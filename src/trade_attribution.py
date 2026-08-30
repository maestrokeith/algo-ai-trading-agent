"""Daily strategy attribution artifact writer.

This module records candidate, allocator, order-build, and exit metadata as a
best-effort sidecar. It must not affect trading decisions or execution.
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.allocation_profile import normalize_strategy_route

log = logging.getLogger(__name__)

RESEARCH_SECTIONS = ("candidates", "allocator_candidates", "orders", "exits", "rejected_one_rule")

TRADE_FEATURE_FIELDS = (
    "sleeve",
    "entry_time",
    "exit_time",
    "holding_minutes",
    "realized_pnl",
    "realized_pnl_pct",
    "quantity",
    "notional",
    "market_regime_score",
    "market_regime_label",
    "spy_above_vwap",
    "qqq_above_vwap",
    "symbol_above_vwap",
    "sector_etf",
    "sector_above_vwap",
    "relative_volume",
    "spread_pct",
    "day_gain_pct",
    "atr_expansion",
    "vwap_distance_pct",
    "alignment_1m",
    "alignment_5m",
    "trend_15m",
    "catalyst_score",
    "news_score",
    "event_score",
    "article_count",
    "premarket_injected",
    "max_favorable_excursion_pct",
    "max_adverse_excursion_pct",
    "trend_long_quality_score",
    "entry_quality_score",
    "entry_quality_threshold",
    "entry_quality_penalties",
    "entry_quality_reason",
    "entry_quality_adaptive_market_vwap",
    "entry_quality_size_multiplier",
    "market_vwap_confirmed",
    "market_vwap_distance_pct",
    "market_vwap_slope",
    "market_vwap_score",
    "market_vwap_state",
    "market_vwap_data_available",
    "adaptive_entry",
    "aggressive_dynamic_mode",
    "aggressive_dynamic_score",
    "aggressive_dynamic_threshold",
    "aggressive_fast_lane",
    "fast_lane_trigger",
    "bypassed_noncritical_rules",
    "score_before_override",
    "score_after_override",
    "size_multiplier",
    "price_tier",
    "aggressive_dynamic_reason",
)


def _safe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _safe_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on", "y"}:
            return True
        if text in {"0", "false", "no", "off", "n"}:
            return False
    return bool(value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)


def attribution_daily_path(
    *,
    data_dir: Path | str,
    user_id: str,
    day: date | str | None = None,
) -> Path:
    """Return the daily trade attribution artifact path."""
    d = Path(data_dir)
    day_s = day.isoformat() if isinstance(day, date) else str(day or date.today().isoformat())
    user_s = str(user_id or "default").strip() or "default"
    return d / "trade_attribution" / "daily" / f"{day_s}_{user_s}.json"


def _empty_daily_artifact(*, day: str = "", user_id: str = "") -> dict[str, Any]:
    return {
        "version": 1,
        "date": day,
        "user_id": user_id,
        "candidates": [],
        "allocator_candidates": [],
        "orders": [],
        "exits": [],
        "rejected_one_rule": [],
        "summary": {},
    }


def _path_day_user(path: Path) -> tuple[str, str]:
    stem = path.stem
    if "_" not in stem:
        return "", ""
    day, user = stem.split("_", 1)
    return day, user


def _normalize_daily_artifact(payload: Mapping[str, Any] | None, *, path: Path) -> dict[str, Any]:
    day, user = _path_day_user(path)
    out = dict(payload or {})
    out.setdefault("version", 1)
    out.setdefault("date", day)
    out.setdefault("user_id", user)
    for key in RESEARCH_SECTIONS:
        if not isinstance(out.get(key), list):
            out[key] = []
    if not isinstance(out.get("summary"), Mapping):
        out["summary"] = summarize_attribution(out)
    return out


def _corrupt_backup_path(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = path.with_name(f"{path.name}.corrupt.{stamp}")
    idx = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.corrupt.{stamp}.{idx}")
        idx += 1
    return candidate


def _recover_corrupt_daily_artifact(path: Path, error: json.JSONDecodeError) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    day, user = _path_day_user(path)
    recovered: dict[str, Any]
    recovered_count = 0
    try:
        parsed = json.JSONDecoder(strict=False).decode(raw)
        recovered = _normalize_daily_artifact(parsed if isinstance(parsed, Mapping) else {}, path=path)
        recovered_count = sum(
            len(recovered.get(key, []))
            for key in ("candidates", "allocator_candidates", "orders", "exits")
            if isinstance(recovered.get(key), list)
        )
    except Exception:
        recovered = _empty_daily_artifact(day=day, user_id=user)
    backup = _corrupt_backup_path(path)
    try:
        path.replace(backup)
        _write_daily_artifact(path, recovered)
        log.warning(
            "TRADE_ATTRIBUTION_RECOVERED path=%s backup_path=%s recovered_records=%d error=%s",
            path,
            backup,
            int(recovered_count),
            error,
        )
    except Exception:
        log.warning(
            "TRADE_ATTRIBUTION_RECOVERY_FAILED path=%s backup_path=%s error=%s",
            path,
            backup,
            error,
            exc_info=True,
        )
        return recovered
    return recovered


def load_daily_artifact(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_daily_artifact()
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except json.JSONDecodeError as exc:
        log.warning("TRADE_ATTRIBUTION_CORRUPT_ARTIFACT path=%s error=%s", path, exc)
        return _recover_corrupt_daily_artifact(path, exc)
    return _normalize_daily_artifact(payload if isinstance(payload, Mapping) else {}, path=path)


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _same_time_basis(left: datetime, right: datetime) -> tuple[datetime, datetime]:
    if left.tzinfo is not None and right.tzinfo is None:
        return left.replace(tzinfo=None), right
    if left.tzinfo is None and right.tzinfo is not None:
        return left, right.replace(tzinfo=None)
    return left, right


def recent_core_rebuild_churn_symbols(
    *,
    data_dir: Path | str,
    user_id: str,
    now: datetime,
    max_hold_minutes: float,
    cooldown_minutes: float,
    lookback_days: int = 3,
) -> dict[str, dict[str, Any]]:
    """Return symbols whose recent core-rebuild exits look like churn.

    A churn hit is an exit linked to ``core_rebuild`` with hold time less than
    or equal to ``max_hold_minutes`` and exit timestamp within
    ``cooldown_minutes`` of ``now``.
    """
    if max_hold_minutes <= 0.0 or cooldown_minutes <= 0.0:
        return {}
    base = Path(data_dir) / "trade_attribution" / "daily"
    if not base.exists():
        return {}
    user_s = str(user_id or "default").strip() or "default"
    days = max(1, int(lookback_days or 1))
    out: dict[str, dict[str, Any]] = {}
    for offset in range(days):
        day = (now.date() - timedelta(days=offset)).isoformat()
        path = base / f"{day}_{user_s}.json"
        if not path.exists():
            continue
        payload = load_daily_artifact(path)
        exits = payload.get("exits") if isinstance(payload.get("exits"), list) else []
        for row in exits:
            if not isinstance(row, Mapping):
                continue
            route = str(row.get("entry_route") or row.get("entry_source") or "").strip().lower()
            if route != "core_rebuild":
                continue
            hold = _safe_float(row.get("hold_minutes"))
            if hold is None or hold > float(max_hold_minutes):
                continue
            ts = _parse_timestamp(row.get("timestamp"))
            if ts is None:
                continue
            ts_cmp, now_cmp = _same_time_basis(ts, now)
            age_minutes = (now_cmp - ts_cmp).total_seconds() / 60.0
            if age_minutes < 0.0 or age_minutes > float(cooldown_minutes):
                continue
            sym = str(row.get("symbol") or "").strip().upper()
            if not sym:
                continue
            out[sym] = {
                "symbol": sym,
                "exit_reason": row.get("exit_reason"),
                "hold_minutes": hold,
                "age_minutes": age_minutes,
                "cooldown_minutes": float(cooldown_minutes),
            }
    return out


def symbols_sold_on_day(
    *,
    data_dir: Path | str,
    user_id: str,
    day: date | str,
) -> set[str]:
    """Return symbols with any recorded exit in the daily attribution artifact."""
    path = attribution_daily_path(data_dir=data_dir, user_id=user_id, day=day)
    if not path.exists():
        return set()
    payload = load_daily_artifact(path)
    exits = payload.get("exits") if isinstance(payload.get("exits"), list) else []
    out: set[str] = set()
    for row in exits:
        if not isinstance(row, Mapping):
            continue
        sym = str(row.get("symbol") or "").strip().upper()
        if sym:
            out.add(sym)
    return out


def _write_daily_artifact(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(_jsonable(payload), fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    with tmp.open("r", encoding="utf-8") as fh:
        validated = json.load(fh)
    if not isinstance(validated, dict):
        tmp.unlink(missing_ok=True)
        raise ValueError(f"trade attribution artifact did not validate as object: {tmp}")
    tmp.replace(path)


def _append_record(
    *,
    data_dir: Path | str,
    user_id: str,
    timestamp: Any,
    section: str,
    record: Mapping[str, Any],
) -> Path | None:
    try:
        ts = timestamp if isinstance(timestamp, datetime) else datetime.now()
        day = ts.date() if isinstance(ts, datetime) else date.today()
        path = attribution_daily_path(data_dir=data_dir, user_id=user_id, day=day)
        payload = load_daily_artifact(path)
        payload.setdefault("version", 1)
        payload["date"] = day.isoformat()
        payload["user_id"] = str(user_id or "default")
        for key in RESEARCH_SECTIONS:
            payload.setdefault(key, [])
        row = {"timestamp": ts.isoformat() if isinstance(ts, datetime) else str(ts)}
        row.update(dict(record))
        payload.setdefault(section, []).append(_jsonable(row))
        payload["summary"] = summarize_attribution(payload)
        _write_daily_artifact(path, payload)
        log.info(
            "TRADE_ATTRIBUTION_WRITE_OK section=%s user_id=%s path=%s symbol=%s",
            section,
            user_id,
            path,
            str(record.get("symbol") or "").strip().upper() or "n/a",
        )
        return path
    except Exception:
        log.warning("TRADE_ATTRIBUTION_WRITE_FAILED section=%s user_id=%s", section, user_id, exc_info=True)
        return None


def record_candidate(
    *,
    data_dir: Path | str,
    user_id: str,
    timestamp: Any,
    candidate: Mapping[str, Any],
    regime_score: int | None = None,
) -> Path | None:
    """Record one entry-evaluated candidate row."""
    symbol = str(candidate.get("symbol") or candidate.get("sym_u") or "").strip().upper()
    accepted = candidate.get("accepted")
    if accepted is None:
        accepted = candidate.get("final")
    if accepted is None:
        accepted = True
    route = candidate.get("route") or candidate.get("source")
    dynamic = bool(candidate.get("dynamic_candidate") or candidate.get("dynamic"))
    row = {
        "symbol": symbol,
        "route": route,
        "source": candidate.get("source"),
        "dynamic": dynamic,
        "core": bool(candidate.get("core") or (not dynamic and str(route or "").lower() != "dynamic_universe")),
        "accepted": _safe_bool(accepted),
        "reason": candidate.get("reason") or candidate.get("rejection_reason") or candidate.get("skip_reason"),
        "news_score": _safe_float(candidate.get("news_score")),
        "event_score": _safe_float(candidate.get("event_score")),
        "catalyst_score": _safe_float(candidate.get("catalyst_score")),
        "catalyst_type": candidate.get("catalyst_type"),
        "relative_volume": _safe_float(candidate.get("relative_volume", candidate.get("rel_volume"))),
        "day_gain_pct": _safe_float(candidate.get("day_gain_pct", candidate.get("gain_pct"))),
        "spread_pct": _safe_float(candidate.get("spread_pct")),
        "vwap_above": _safe_bool(candidate.get("vwap_above", candidate.get("price_above_vwap"))),
        "atr_expansion_ratio": _safe_float(candidate.get("atr_expansion_ratio", candidate.get("atr_expansion"))),
        "regime_score": regime_score,
    }
    for field in TRADE_FEATURE_FIELDS:
        if field in candidate:
            row[field] = candidate.get(field)
    return _append_record(
        data_dir=data_dir,
        user_id=user_id,
        timestamp=timestamp,
        section="candidates",
        record=row,
    )


def record_allocator_candidate(
    *,
    data_dir: Path | str,
    user_id: str,
    timestamp: Any,
    candidate: Mapping[str, Any],
    selected_rank: int | None,
    action_created: bool,
    no_action_reason: str | None = None,
    target_notional: Any = None,
    final_notional: Any = None,
) -> Path | None:
    symbol = str(candidate.get("symbol") or candidate.get("sym_u") or "").strip().upper()
    row = {
        "symbol": symbol,
        "selected_rank": selected_rank,
        "action_created": bool(action_created),
        "no_action_reason": no_action_reason,
        "target_notional": _safe_float(target_notional),
        "final_notional": _safe_float(final_notional),
        "source": candidate.get("source"),
        "route": candidate.get("route") or candidate.get("source"),
        "core_rebuild": bool(candidate.get("core_rebuild") or str(candidate.get("route") or "").lower() == "core_rebuild"),
        "dynamic": bool(candidate.get("dynamic_candidate") or candidate.get("dynamic")),
        "score": _safe_float(candidate.get("score")),
        "news_score": _safe_float(candidate.get("news_score")),
        "event_score": _safe_float(candidate.get("event_score")),
        "catalyst_score": _safe_float(candidate.get("catalyst_score")),
    }
    return _append_record(
        data_dir=data_dir,
        user_id=user_id,
        timestamp=timestamp,
        section="allocator_candidates",
        record=row,
    )


def record_order_event(
    *,
    data_dir: Path | str,
    user_id: str,
    timestamp: Any,
    symbol: str,
    action: str,
    route: str | None = None,
    source: str | None = None,
    notional: Any = None,
    order_build_status: str | None = None,
    reject_reason: str | None = None,
    submit_attempt: bool = False,
    submitted: bool = False,
    order_id: Any = None,
    qty: Any = None,
    status: Any = None,
    filled_qty: Any = None,
    filled_avg_price: Any = None,
    dynamic_candidate: Any = None,
    news_score: Any = None,
    event_score: Any = None,
    catalyst_score: Any = None,
    catalyst_type: Any = None,
    relative_volume: Any = None,
    gain_pct: Any = None,
    environment: str | None = None,
    hypothetical: Any = None,
    broker_dispatch_attempted: Any = None,
    execution_allowed: Any = None,
    allow_replay_attribution: bool = False,
    **metadata: Any,
) -> Path | None:
    row = {
        "symbol": str(symbol or "").strip().upper(),
        "action": str(action or "").strip().lower(),
        "route": route,
        "source": source,
        "notional": _safe_float(notional),
        "qty": _safe_float(qty),
        "order_build_status": order_build_status,
        "reject_reason": reject_reason,
        "submit_attempt": bool(submit_attempt),
        "submitted": bool(submitted),
        "order_id": str(order_id) if order_id is not None else None,
        "status": str(status) if status is not None else None,
        "filled_qty": _safe_float(filled_qty),
        "filled_avg_price": _safe_float(filled_avg_price),
        "dynamic_candidate": _safe_bool(dynamic_candidate),
        "news_score": _safe_float(news_score),
        "event_score": _safe_float(event_score),
        "catalyst_score": _safe_float(catalyst_score),
        "catalyst_type": catalyst_type,
        "relative_volume": _safe_float(relative_volume),
        "gain_pct": _safe_float(gain_pct),
    }
    if environment is not None:
        row["environment"] = str(environment)
    if hypothetical is not None:
        row["hypothetical"] = _safe_bool(hypothetical)
    if broker_dispatch_attempted is not None:
        row["broker_dispatch_attempted"] = _safe_bool(broker_dispatch_attempted)
    if execution_allowed is not None:
        row["execution_allowed"] = _safe_bool(execution_allowed)
    for key, value in metadata.items():
        if value is None:
            continue
        if key in row:
            continue
        row[str(key)] = _jsonable(value)
    try:
        from src.trading_lifecycle import validate_live_persistence_record

        allowed, violation = validate_live_persistence_record(
            row,
            user_id=user_id,
            destination="trade_attribution.orders",
            record_type="order",
        )
        if not allowed and bool(allow_replay_attribution) and (violation or {}).get("environment") == "replay":
            log.info(
                "REPLAY_TRADE_ATTRIBUTION_ALLOWED record_type=%s destination=%s identifier=%s source=%s",
                (violation or {}).get("record_type"),
                (violation or {}).get("destination"),
                (violation or {}).get("identifier"),
                (violation or {}).get("source"),
            )
        elif not allowed:
            log.error(
                "LIVE_DATA_CONTAMINATION_BLOCKED record_type=%s destination=%s environment=%s origin=%s identifier=%s source=%s reason=%s",
                (violation or {}).get("record_type"),
                (violation or {}).get("destination"),
                (violation or {}).get("environment"),
                (violation or {}).get("origin"),
                (violation or {}).get("identifier"),
                (violation or {}).get("source"),
                (violation or {}).get("reason"),
            )
            return None
    except Exception:
        log.warning("LIVE_DATA_CONTAMINATION_VALIDATION_UNAVAILABLE user_id=%s symbol=%s", user_id, symbol, exc_info=True)
    return _append_record(
        data_dir=data_dir,
        user_id=user_id,
        timestamp=timestamp,
        section="orders",
        record=row,
    )


def _order_recovery_key(record: Mapping[str, Any]) -> str:
    recovery_key = str(record.get("recovery_key") or "").strip()
    if recovery_key:
        return recovery_key
    for key in ("broker_activity_id", "activity_id", "fill_id"):
        value = str(record.get(key) or "").strip()
        if value:
            return f"{key}:{value}"
    order_id = str(record.get("broker_order_id") or record.get("order_id") or "").strip()
    status = str(record.get("status") or "").strip().lower()
    filled = _safe_float(record.get("filled_qty")) or 0.0
    avg = _safe_float(record.get("filled_avg_price")) or 0.0
    return f"order:{order_id}:status:{status}:filled:{filled:.9f}:avg:{avg:.6f}"


def record_recovered_order_event(
    *,
    data_dir: Path | str,
    user_id: str,
    day: date | str,
    timestamp: Any,
    record: Mapping[str, Any],
) -> Path | None:
    """Append an idempotent broker-recovered order/fill snapshot.

    Recovery rows are canonical lifecycle evidence, but keep explicit provenance
    so diagnostics never imply they were emitted by the original live submitter.
    """

    day_s = day.isoformat() if isinstance(day, date) else str(day)
    path = attribution_daily_path(data_dir=data_dir, user_id=user_id, day=day_s)
    payload = load_daily_artifact(path)
    existing = {
        _order_recovery_key(row)
        for row in payload.get("orders", [])
        if isinstance(row, Mapping)
    }
    row = {
        "event_origin": "broker_reconciliation",
        "record_origin": "broker",
        "environment": "live",
        "recovered": True,
        "reconciliation_timestamp": datetime.now(timezone.utc).isoformat(),
        **dict(record),
    }
    key = _order_recovery_key(row)
    row["recovery_key"] = key
    if key in existing:
        log.info(
            "BROKER_RECONCILED_ORDER_IDEMPOTENT user_id=%s symbol=%s order_id=%s recovery_key=%s",
            user_id,
            row.get("symbol"),
            row.get("broker_order_id") or row.get("order_id"),
            key,
        )
        return path
    payload.setdefault("version", 1)
    payload["date"] = day_s
    payload["user_id"] = str(user_id or "default")
    for section in RESEARCH_SECTIONS:
        payload.setdefault(section, [])
    ts = timestamp if isinstance(timestamp, datetime) else _parse_timestamp(timestamp) or datetime.now(timezone.utc)
    row = {"timestamp": ts.isoformat(), **row}
    payload.setdefault("orders", []).append(_jsonable(row))
    payload["summary"] = summarize_attribution(payload)
    _write_daily_artifact(path, payload)
    log.info(
        "BROKER_RECONCILED_ORDER_WRITTEN user_id=%s symbol=%s order_id=%s status=%s recovered=true recovery_key=%s",
        user_id,
        row.get("symbol"),
        row.get("broker_order_id") or row.get("order_id"),
        row.get("status"),
        key,
    )
    if (_safe_float(row.get("filled_qty")) or 0.0) > 0.0:
        log.info(
            "BROKER_FILL_RECOVERED user_id=%s symbol=%s order_id=%s filled_qty=%s filled_avg_price=%s recovery_key=%s",
            user_id,
            row.get("symbol"),
            row.get("broker_order_id") or row.get("order_id"),
            row.get("filled_qty"),
            row.get("filled_avg_price"),
            key,
        )
    return path


def record_exit(
    *,
    data_dir: Path | str,
    user_id: str,
    timestamp: Any,
    symbol: str,
    qty: Any = None,
    exit_reason: str | None = None,
    pnl: Any = None,
    pnl_pct: Any = None,
    hold_minutes: Any = None,
    entry_route: str | None = None,
    entry_source: str | None = None,
    mfe_pct: Any = None,
    mae_pct: Any = None,
    **features: Any,
) -> Path | None:
    realized_pnl = features.get("realized_pnl", pnl)
    realized_pnl_pct = features.get("realized_pnl_pct", pnl_pct)
    quantity = features.get("quantity", qty)
    holding_minutes = features.get("holding_minutes", hold_minutes)
    row = {
        "symbol": str(symbol or "").strip().upper(),
        "qty": _safe_float(qty),
        "quantity": _safe_float(quantity),
        "exit_reason": exit_reason,
        "pnl": _safe_float(pnl),
        "pnl_pct": _safe_float(pnl_pct),
        "realized_pnl": _safe_float(realized_pnl),
        "realized_pnl_pct": _safe_float(realized_pnl_pct),
        "hold_minutes": _safe_float(hold_minutes),
        "holding_minutes": _safe_float(holding_minutes),
        "entry_route": entry_route,
        "entry_source": entry_source,
        "sleeve": features.get("sleeve") or entry_route,
        "entry_time": features.get("entry_time"),
        "exit_time": features.get("exit_time"),
        "mfe_pct": _safe_float(mfe_pct),
        "mae_pct": _safe_float(mae_pct),
        "max_favorable_excursion_pct": _safe_float(features.get("max_favorable_excursion_pct", mfe_pct)),
        "max_adverse_excursion_pct": _safe_float(features.get("max_adverse_excursion_pct", mae_pct)),
    }
    for field in TRADE_FEATURE_FIELDS:
        if field not in row and field in features:
            row[field] = features.get(field)
    try:
        from src.trading_lifecycle import validate_live_persistence_record

        allowed, violation = validate_live_persistence_record(
            row,
            user_id=user_id,
            destination="trade_attribution.exits",
            record_type="exit",
        )
        if not allowed:
            log.error(
                "LIVE_DATA_CONTAMINATION_BLOCKED record_type=%s destination=%s environment=%s origin=%s identifier=%s source=%s reason=%s",
                (violation or {}).get("record_type"),
                (violation or {}).get("destination"),
                (violation or {}).get("environment"),
                (violation or {}).get("origin"),
                (violation or {}).get("identifier"),
                (violation or {}).get("source"),
                (violation or {}).get("reason"),
            )
            return None
    except Exception:
        log.warning("LIVE_DATA_CONTAMINATION_VALIDATION_UNAVAILABLE user_id=%s symbol=%s", user_id, symbol, exc_info=True)
    return _append_record(
        data_dir=data_dir,
        user_id=user_id,
        timestamp=timestamp,
        section="exits",
        record=row,
    )


def record_rejected_one_rule(
    *,
    data_dir: Path | str,
    user_id: str,
    timestamp: Any,
    symbol: str,
    rejected_rule: str,
    features: Mapping[str, Any] | None = None,
    price: Any = None,
) -> Path | None:
    row = {
        "symbol": str(symbol or "").strip().upper(),
        "rejected_rule": str(rejected_rule or "unknown"),
        "price": _safe_float(price),
    }
    if features:
        row.update(dict(features))
    return _append_record(
        data_dir=data_dir,
        user_id=user_id,
        timestamp=timestamp,
        section="rejected_one_rule",
        record=row,
    )


def _top(counter: Counter[str], limit: int = 10) -> dict[str, int]:
    return {key: int(count) for key, count in counter.most_common(limit)}


def summarize_attribution(payload: Mapping[str, Any]) -> dict[str, Any]:
    orders = payload.get("orders") if isinstance(payload.get("orders"), list) else []
    exits = payload.get("exits") if isinstance(payload.get("exits"), list) else []
    candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []

    submitted_buys = [
        row for row in orders
        if isinstance(row, Mapping)
        and bool(row.get("submitted"))
        and str(row.get("action") or "").lower() == "buy"
    ]
    exit_rows = [row for row in exits if isinstance(row, Mapping)]
    hold_values = [_safe_float(row.get("hold_minutes")) for row in exit_rows]
    hold_values = [v for v in hold_values if v is not None]
    pnl_pct_values = [_safe_float(row.get("pnl_pct")) for row in exit_rows]
    pnl_pct_values = [v for v in pnl_pct_values if v is not None]
    pnl_by_route: defaultdict[str, float] = defaultdict(float)
    churn_by_route: Counter[str] = Counter()
    submitted_buy_route_by_symbol: dict[str, str] = {}
    for row in submitted_buys:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        submitted_buy_route_by_symbol[symbol] = normalize_strategy_route(
            row.get("route"),
            row.get("source"),
        )
    for row in exit_rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        route = normalize_strategy_route(
            row.get("entry_route"),
            row.get("entry_source"),
            submitted_buy_route_by_symbol.get(symbol),
        )
        pnl = _safe_float(row.get("pnl"))
        if pnl is not None:
            pnl_by_route[route] += pnl
        hold = _safe_float(row.get("hold_minutes"))
        if hold is not None and hold < 30.0:
            churn_by_route[route] += 1
    rejection_reasons = Counter(
        str(row.get("reason") or "unknown")
        for row in candidates
        if isinstance(row, Mapping) and row.get("accepted") is False
    )
    order_rejects = Counter(
        str(row.get("reject_reason") or "unknown")
        for row in orders
        if isinstance(row, Mapping)
        and str(row.get("order_build_status") or "").lower() in {"rejected", "not_built"}
    )
    wins = len([v for v in pnl_pct_values if v > 0.0])
    return {
        "trades_entered": len(submitted_buys),
        "trades_exited": len(exit_rows),
        "avg_hold_minutes": (sum(hold_values) / len(hold_values)) if hold_values else 0.0,
        "win_rate": (wins / len(pnl_pct_values)) if pnl_pct_values else 0.0,
        "avg_pnl_pct": (sum(pnl_pct_values) / len(pnl_pct_values)) if pnl_pct_values else 0.0,
        "churn_count_under_30m": len([v for v in hold_values if v < 30.0]),
        "churn_under_30m_by_route": _top(churn_by_route),
        "pnl_by_route": {k: round(v, 6) for k, v in sorted(pnl_by_route.items())},
        "exits_by_reason": _top(Counter(str(row.get("exit_reason") or "unknown") for row in exit_rows)),
        "top_rejection_reasons": _top(rejection_reasons),
        "top_order_build_rejects": _top(order_rejects),
    }
