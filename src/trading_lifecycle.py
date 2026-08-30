"""Canonical trading lifecycle records and day-level reconstruction.

The lifecycle layer is intentionally conservative: raw sidecar rows are kept as
evidence, while canonical orders, fills, and positions are derived from stable
identities. Replay, mock, shadow, paper, and test records are never treated as
live broker evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence
from zoneinfo import ZoneInfo

from src.options_selector import parse_occ_equity_option_symbol
from src.trade_attribution import attribution_daily_path, load_daily_artifact

ET = ZoneInfo("America/New_York")

Environment = Literal["live", "paper", "shadow", "replay", "test"]
RecordOrigin = Literal["broker", "application", "shadow", "replay", "mock", "reconstructed"]
LifecycleStatus = Literal["CLEAN", "PARTIAL", "CONTAMINATED", "UNRECONCILED", "INSUFFICIENT_DATA"]
AlignmentState = Literal["PASS", "FAIL", "UNAVAILABLE", "STALE", "ERROR"]

SUSPICIOUS_ID_PREFIXES = ("replay-", "mock-", "test-", "shadow-", "paper-")
CANONICAL_ORDER_STATES = {
    "accepted": "ACCEPTED",
    "new": "ACCEPTED",
    "pending_new": "PENDING",
    "pending_replace": "PENDING",
    "pending_cancel": "PENDING",
    "held": "PENDING",
    "partially_filled": "PARTIALLY_FILLED",
    "filled": "FILLED",
    "done_for_day": "ACCEPTED",
    "calculated": "ACCEPTED",
    "canceled": "CANCELLED",
    "cancelled": "CANCELLED",
    "rejected": "REJECTED",
    "expired": "EXPIRED",
    "replaced": "REPLACED",
    "stopped": "CANCELLED",
    "suspended": "PENDING",
}


def safe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def norm_id(value: Any) -> str:
    return str(value or "").strip()


def norm_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def event_timestamp(row: Mapping[str, Any]) -> datetime | None:
    for key in (
        "event_timestamp_utc",
        "broker_event_timestamp",
        "broker_timestamp",
        "transaction_time",
        "decision_timestamp",
        "evaluation_timestamp",
        "filled_at",
        "submitted_at",
        "created_at",
        "timestamp",
    ):
        ts = parse_ts(row.get(key))
        if ts is not None:
            return ts.astimezone(timezone.utc)
    return None


def event_timestamp_et(row: Mapping[str, Any]) -> datetime | None:
    ts = event_timestamp(row)
    return ts.astimezone(ET) if ts else None


def event_trading_date_et(row: Mapping[str, Any]) -> str | None:
    ts = event_timestamp_et(row)
    return ts.date().isoformat() if ts else None


def raw_record_hash(row: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LifecycleBase:
    user_id: str
    account_id: str | None
    environment: Environment
    record_origin: RecordOrigin
    run_id: str | None
    session_id: str | None
    trading_date_et: str | None
    event_timestamp_utc: str | None
    event_timestamp_et: str | None
    ingested_at_utc: str | None
    symbol: str | None
    option_symbol: str | None
    strategy_route: str | None
    decision_id: str | None
    logical_order_id: str | None
    client_order_id: str | None
    broker_order_id: str | None
    broker_fill_id: str | None
    position_id: str | None
    source_name: str
    source_path: str
    raw_record_hash: str


@dataclass(frozen=True)
class ScannerEvent(LifecycleBase):
    raw: dict[str, Any]


@dataclass(frozen=True)
class CandidateSelection(LifecycleBase):
    selected_rank: int | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class EntryEvaluation(LifecycleBase):
    alignment_state: AlignmentState
    block_reason: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class EntryDecision(LifecycleBase):
    approved: bool | None
    block_reason: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class AllocatorAction(LifecycleBase):
    action_created: bool
    raw: dict[str, Any]


@dataclass(frozen=True)
class LogicalOrder(LifecycleBase):
    canonical_order_key: str
    side: str
    status: str
    raw_event_count: int
    raw: dict[str, Any]


@dataclass(frozen=True)
class BrokerOrder(LogicalOrder):
    broker_status: str


@dataclass(frozen=True)
class Fill(LifecycleBase):
    canonical_fill_key: str
    canonical_order_key: str
    side: str
    quantity: float
    price: float | None
    classification: str


@dataclass(frozen=True)
class Position(LifecycleBase):
    entry_fill_ids: list[str]
    exit_fill_ids: list[str]
    entry_qty: float
    exit_qty: float
    status: str


@dataclass(frozen=True)
class Exit(LifecycleBase):
    exit_reason: str | None
    quantity: float | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class ClosedTrade(LifecycleBase):
    entry_fill_ids: list[str]
    exit_fill_ids: list[str]
    realized_pnl: float | None


@dataclass(frozen=True)
class SignalOutcome(LifecycleBase):
    status: str
    forward_returns: dict[str, float | None]
    mfe: float | None
    mae: float | None
    notes: list[str]


@dataclass(frozen=True)
class NewsEventLink(LifecycleBase):
    provider: str | None
    article_id: str | None
    link_status: str


@dataclass(frozen=True)
class SourceRow:
    source: str
    path: str
    index: int
    row: dict[str, Any]


@dataclass(frozen=True)
class FillEvent:
    key: str
    order_key: str
    position_key: str
    side: str
    quantity: float
    price: float | None
    timestamp: datetime | None
    source_rows: tuple[SourceRow, ...]
    classification: str = "EXACT_MATCH"


def infer_environment(row: Mapping[str, Any], *, user_id: str) -> Environment:
    raw = str(row.get("environment") or row.get("mode") or "").strip().lower()
    if raw in {"live", "paper", "shadow", "replay", "test"}:
        return raw  # type: ignore[return-value]
    if is_synthetic_or_replay(row):
        return "replay"
    return "live" if user_id == "live_bot" else "paper"


def infer_origin(row: Mapping[str, Any]) -> RecordOrigin:
    text = " ".join(str(row.get(k) or "").lower() for k in ("source", "route", "status", "order_id", "client_order_id"))
    if "shadow" in text:
        return "shadow"
    if "mock" in text:
        return "mock"
    if "replay" in text or str(row.get("status") or "").lower() in {"n/a", "na"}:
        return "replay"
    if row.get("broker_order_id") or row.get("broker_activity_id") or row.get("activity_id"):
        return "broker"
    return "application"


def make_base(
    row: Mapping[str, Any],
    *,
    user_id: str,
    source_name: str,
    source_path: Path | str,
    position_id: str | None = None,
) -> LifecycleBase:
    ts_utc = event_timestamp(row)
    ts_et = ts_utc.astimezone(ET) if ts_utc else None
    return LifecycleBase(
        user_id=user_id,
        account_id=str(row.get("account_id")) if row.get("account_id") else None,
        environment=infer_environment(row, user_id=user_id),
        record_origin=infer_origin(row),
        run_id=str(row.get("run_id")) if row.get("run_id") else None,
        session_id=str(row.get("session_id") or row.get("cycle_id")) if row.get("session_id") or row.get("cycle_id") else None,
        trading_date_et=ts_et.date().isoformat() if ts_et else None,
        event_timestamp_utc=ts_utc.isoformat() if ts_utc else None,
        event_timestamp_et=ts_et.isoformat() if ts_et else None,
        ingested_at_utc=(parse_ts(row.get("ingested_at_utc") or row.get("ingested_at")) or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
        symbol=norm_symbol(row.get("underlying_symbol") or row.get("symbol")) or None,
        option_symbol=norm_symbol(row.get("option_symbol")) or None,
        strategy_route=str(row.get("route") or row.get("entry_route") or row.get("source")) if row.get("route") or row.get("entry_route") or row.get("source") else None,
        decision_id=str(row.get("decision_id")) if row.get("decision_id") else None,
        logical_order_id=str(row.get("logical_order_id")) if row.get("logical_order_id") else None,
        client_order_id=str(row.get("client_order_id")) if row.get("client_order_id") else None,
        broker_order_id=str(row.get("broker_order_id") or row.get("order_id")) if row.get("broker_order_id") or row.get("order_id") else None,
        broker_fill_id=str(row.get("broker_fill_id") or row.get("broker_activity_id") or row.get("activity_id") or row.get("fill_id")) if row.get("broker_fill_id") or row.get("broker_activity_id") or row.get("activity_id") or row.get("fill_id") else None,
        position_id=position_id,
        source_name=source_name,
        source_path=str(source_path),
        raw_record_hash=raw_record_hash(row),
    )


def row_side(row: Mapping[str, Any]) -> str:
    return str(row.get("action") or row.get("side") or "").strip().lower()


def row_status(row: Mapping[str, Any]) -> str:
    text = str(row.get("status") or "").strip().lower()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text


def canonical_order_state(row: Mapping[str, Any]) -> str:
    status = row_status(row)
    if status in CANONICAL_ORDER_STATES:
        return CANONICAL_ORDER_STATES[status]
    if not status:
        return "UNKNOWN_BROKER_STATE"
    return "UNKNOWN_BROKER_STATE"


def _bool_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _bool_false(value: Any) -> bool:
    if isinstance(value, bool):
        return not value
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"", "0", "false", "no", "n", "none", "null"}
    return not bool(value)


def _shadow_identifier(row: Mapping[str, Any]) -> bool:
    for key in ("order_id", "broker_order_id", "client_order_id", "logical_order_id"):
        value = norm_id(row.get(key)).lower()
        if value.startswith("shadow-"):
            return True
    return False


def _has_real_broker_identity(row: Mapping[str, Any]) -> bool:
    for key in ("broker_order_id", "order_id", "client_order_id", "broker_activity_id", "activity_id", "broker_fill_id", "fill_id"):
        value = norm_id(row.get(key)).lower()
        if value and not value.startswith(SUSPICIOUS_ID_PREFIXES):
            return True
    return False


def _has_fill_evidence(row: Mapping[str, Any]) -> bool:
    return (safe_float(row.get("filled_qty")) or 0.0) > 0 or row_status(row) in {"filled", "partially_filled"}


def shadow_reclassification_reason(row: Mapping[str, Any]) -> str | None:
    """Return why an order-like row is safe shadow telemetry, not broker evidence."""

    status = row_status(row)
    no_fill = not _has_fill_evidence(row)
    no_real_broker_id = not _has_real_broker_identity(row)
    submitted_false = _bool_false(row.get("submitted"))
    explicit_shadow = str(row.get("environment") or row.get("mode") or "").strip().lower() == "shadow"
    order_like = any(key in row for key in ("submit_attempt", "submitted", "order_id", "broker_order_id", "order_build_status"))
    if (
        explicit_shadow
        and _bool_true(row.get("hypothetical"))
        and (not order_like or _bool_false(row.get("broker_dispatch_attempted")))
        and (not order_like or _bool_false(row.get("execution_allowed")))
        and no_fill
        and no_real_broker_id
        and status in {"", "shadow", "blocked", "rejected"}
    ):
        return "explicit_shadow_expected"
    if _shadow_identifier(row) and status in {"", "shadow", "blocked", "rejected"} and submitted_false and no_fill and no_real_broker_id:
        return "legacy_shadow_order_result"
    if (
        _bool_true(row.get("submit_attempt"))
        and submitted_false
        and no_fill
        and no_real_broker_id
        and not norm_id(row.get("order_id"))
        and status == ""
        and str(row.get("order_build_status") or "").strip().lower() == "built"
    ):
        return "legacy_shadow_pre_submit_intent"
    return None


def is_synthetic_or_replay(row: Mapping[str, Any]) -> bool:
    return is_replay_or_mock_row(row)


def is_replay_or_mock_row(row: Mapping[str, Any]) -> bool:
    if shadow_reclassification_reason(row):
        return False
    text = " ".join(str(row.get(k) or "").lower() for k in ("source", "route", "status", "order_id", "broker_order_id", "client_order_id", "mode", "environment"))
    return any(token in text for token in ("replay", "mock", "test")) or row_status(row) in {"n/a", "na", "mock"}


def is_shadow_row(row: Mapping[str, Any]) -> bool:
    return shadow_reclassification_reason(row) is not None


def suspicious_live_identifier(row: Mapping[str, Any]) -> str | None:
    for key in ("order_id", "broker_order_id", "client_order_id", "logical_order_id", "broker_fill_id", "broker_activity_id", "activity_id", "fill_id"):
        value = norm_id(row.get(key)).lower()
        if value.startswith(SUSPICIOUS_ID_PREFIXES):
            return value
    return None


def validate_live_persistence_record(
    row: Mapping[str, Any],
    *,
    user_id: str,
    destination: str,
    record_type: str,
) -> tuple[bool, dict[str, Any] | None]:
    env = infer_environment(row, user_id=user_id)
    origin = infer_origin(row)
    identifier = suspicious_live_identifier(row)
    if user_id == "live_bot" and env == "shadow":
        status = row_status(row)
        filled_qty = safe_float(row.get("filled_qty")) or 0.0
        safe_shadow = (
            bool(row.get("hypothetical"))
            and row.get("broker_dispatch_attempted") is False
            and row.get("execution_allowed") is False
            and filled_qty <= 0.0
            and status in {"", "shadow", "blocked", "rejected"}
        )
        if safe_shadow:
            return True, None
    if user_id == "live_bot" and (env != "live" or origin not in {"broker", "application"} or identifier):
        return False, {
            "reason": "LIVE_DATA_CONTAMINATION_BLOCKED",
            "record_type": record_type,
            "destination": destination,
            "environment": env,
            "origin": origin,
            "identifier": identifier,
            "source": row.get("source") or row.get("route"),
        }
    return True, None


def canonical_order_key(row: Mapping[str, Any]) -> str:
    for label, key in (("broker_order_id", "broker_order_id"), ("broker_order_id", "order_id"), ("client_order_id", "client_order_id"), ("logical_order_id", "logical_order_id")):
        value = norm_id(row.get(key))
        if value:
            return f"{label}:{value}"
    ts = event_timestamp(row)
    payload = {
        "symbol": norm_symbol(row.get("option_symbol") or row.get("symbol")),
        "side": row_side(row),
        "timestamp": ts.isoformat() if ts else "",
        "notional": safe_float(row.get("notional")),
        "qty": safe_float(row.get("qty") or row.get("quantity")),
        "route": row.get("route"),
        "source": row.get("source"),
        "cycle_id": row.get("cycle_id"),
    }
    return "submission_attempt:" + hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]


def canonical_decision_key(row: Mapping[str, Any], *, user_id: str) -> str:
    if row.get("decision_id"):
        return f"decision_id:{row.get('decision_id')}"
    ts = event_timestamp(row)
    ts_s = ts.isoformat() if ts else str(row.get("timestamp") or "")
    payload = {
        "user_id": user_id,
        "environment": infer_environment(row, user_id=user_id),
        "symbol": norm_symbol(row.get("symbol")),
        "route": row.get("route") or row.get("source"),
        "timestamp": ts_s,
        "cycle_id": row.get("cycle_id") or row.get("session_id"),
        "accepted": row.get("accepted"),
    }
    return "decision:" + hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]


def explicit_fill_id(row: Mapping[str, Any]) -> str:
    for key in ("broker_activity_id", "activity_id", "broker_fill_id", "fill_id", "execution_id"):
        value = norm_id(row.get(key))
        if value:
            return value
    return ""


def canonical_fill_key(row: Mapping[str, Any]) -> str:
    for label, key in (
        ("broker_activity_id", "broker_activity_id"),
        ("broker_activity_id", "activity_id"),
        ("broker_fill_id", "broker_fill_id"),
        ("broker_fill_id", "fill_id"),
        ("broker_fill_id", "execution_id"),
    ):
        value = norm_id(row.get(key))
        if value:
            return f"{label}:{value}"
    ts = event_timestamp(row)
    qty = safe_float(row.get("filled_qty") or row.get("qty") or row.get("quantity")) or 0.0
    price = safe_float(row.get("filled_avg_price") or row.get("fill_price") or row.get("price"))
    return (
        f"composite:{canonical_order_key(row)}:"
        f"{ts.isoformat() if ts else ''}:{qty:g}:{price}"
    )


def canonical_position_key(row: Mapping[str, Any], *, user_id: str) -> str:
    symbol = norm_symbol(row.get("option_symbol") or row.get("symbol"))
    parsed = parse_occ_equity_option_symbol(symbol)
    if parsed:
        underlying, exp, right, strike = parsed
        return f"option:{user_id}:{underlying}:{exp.isoformat()}:{strike}:{right}:{symbol}"
    return f"equity:{user_id}:{symbol}"


def rows(payload: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    return [dict(row) for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def source_rows(items: Sequence[Mapping[str, Any]], *, path: Path, source: str) -> list[SourceRow]:
    return [SourceRow(source=source, path=str(path), index=i, row=dict(row)) for i, row in enumerate(items)]


def rows_for_et_day(items: Sequence[Mapping[str, Any]], *, day: str) -> list[dict[str, Any]]:
    """Keep only rows whose own event timestamp normalizes to the requested ET date."""

    return [dict(row) for row in items if event_trading_date_et(row) == day]


def unique_orders(items: Sequence[SourceRow]) -> dict[str, list[SourceRow]]:
    out: dict[str, list[SourceRow]] = {}
    for src in items:
        out.setdefault(canonical_order_key(src.row), []).append(src)
    return out


def order_snapshot_signature(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        canonical_order_key(row),
        row_status(row),
        safe_float(row.get("filled_qty")),
        safe_float(row.get("filled_avg_price") or row.get("fill_price") or row.get("price")),
        str(row.get("event_origin") or ""),
        str(row.get("recovery_key") or ""),
    )


def duplicate_order_snapshots(items: Sequence[SourceRow]) -> tuple[list[SourceRow], list[str]]:
    grouped: dict[tuple[Any, ...], list[SourceRow]] = {}
    for src in items:
        grouped.setdefault(order_snapshot_signature(src.row), []).append(src)
    duplicate_sources = [src for rows in grouped.values() for src in rows[1:]]
    duplicate_ids = sorted({str(sig[0]) for sig, rows in grouped.items() if len(rows) > 1})
    return duplicate_sources, duplicate_ids


def submitted_row(row: Mapping[str, Any]) -> bool:
    return _bool_true(row.get("submitted")) or _bool_true(row.get("submit_attempt"))


def real_submitted_row(row: Mapping[str, Any]) -> bool:
    return _bool_true(row.get("submitted")) and not is_shadow_row(row) and not is_replay_or_mock_row(row)


def filled_row(row: Mapping[str, Any]) -> bool:
    return real_filled_row(row)


def real_filled_row(row: Mapping[str, Any]) -> bool:
    if _bool_true(row.get("fill_summary_only")):
        return False
    return (
        _has_fill_evidence(row)
        and real_submitted_row(row)
        and (_has_real_broker_identity(row) or broker_accepted_row(row))
    )


def lifecycle_record_class(row: Mapping[str, Any]) -> str:
    if is_shadow_row(row):
        return "EXPECTED_SHADOW_INTENT"
    if is_replay_or_mock_row(row):
        if _has_real_broker_identity(row) and (row_status(row) not in {"n/a", "na", "mock"} or _bool_true(row.get("execution_allowed"))):
            return "AMBIGUOUS_MALFORMED"
        return "REPLAY_RESEARCH_OUTCOME"
    if real_filled_row(row):
        return "REAL_BROKER_FILL"
    if real_submitted_row(row):
        return "REAL_BROKER_ORDER"
    if _has_fill_evidence(row):
        return "AMBIGUOUS_MALFORMED"
    if submitted_row(row):
        return "ATTRIBUTION_SNAPSHOT"
    return "ATTRIBUTION_SNAPSHOT"


def broker_accepted_row(row: Mapping[str, Any]) -> bool:
    return row_status(row) in {"new", "accepted", "pending_new", "partially_filled", "filled", "done_for_day", "calculated"}


def raw_fill_sources(items: Sequence[SourceRow]) -> list[SourceRow]:
    return [src for src in items if filled_row(src.row)]


def canonical_fill_events(items: Sequence[SourceRow], *, user_id: str) -> tuple[list[FillEvent], list[SourceRow]]:
    submitted_order_keys = {
        canonical_order_key(src.row)
        for src in items
        if real_submitted_row(src.row)
    }
    by_order = unique_orders(raw_fill_sources(items))
    events: list[FillEvent] = []
    duplicate_sources: list[SourceRow] = []
    for order_key, group in sorted(by_order.items()):
        if order_key not in submitted_order_keys or any(is_replay_or_mock_row(src.row) or is_shadow_row(src.row) for src in group):
            duplicate_sources.extend(group[1:])
            continue
        explicit: dict[str, list[SourceRow]] = {}
        implicit: list[SourceRow] = []
        for src in group:
            fid = canonical_fill_key(src.row) if explicit_fill_id(src.row) else ""
            if fid:
                explicit.setdefault(fid, []).append(src)
            else:
                implicit.append(src)
        for fill_key, group_rows in explicit.items():
            first = group_rows[0].row
            duplicate_sources.extend(group_rows[1:])
            events.append(
                FillEvent(
                    key=fill_key,
                    order_key=order_key,
                    position_key=canonical_position_key(first, user_id=user_id),
                    side=row_side(first),
                    quantity=safe_float(first.get("filled_qty") or first.get("qty") or first.get("quantity")) or 0.0,
                    price=safe_float(first.get("filled_avg_price") or first.get("fill_price") or first.get("price")),
                    timestamp=event_timestamp(first),
                    source_rows=tuple(group_rows),
                )
            )
        implicit_sorted = sorted(implicit, key=lambda src: ((event_timestamp(src.row) or datetime.min.replace(tzinfo=timezone.utc)), src.index))
        seen: set[tuple[float, float | None, str]] = set()
        prev_qty = 0.0
        for src in implicit_sorted:
            row = src.row
            cumulative_qty = safe_float(row.get("filled_qty") or row.get("qty") or row.get("quantity")) or 0.0
            price = safe_float(row.get("filled_avg_price") or row.get("fill_price") or row.get("price"))
            snap = (cumulative_qty, price, row_status(row))
            if snap in seen and cumulative_qty <= prev_qty:
                duplicate_sources.append(src)
                continue
            seen.add(snap)
            delta = cumulative_qty - prev_qty
            if delta <= 0:
                duplicate_sources.append(src)
                continue
            prev_qty = cumulative_qty
            ts = event_timestamp(row)
            fill_key = f"composite:{order_key}:{norm_symbol(row.get('option_symbol') or row.get('symbol'))}:{row_side(row)}:{ts.isoformat() if ts else ''}:{delta:g}:{price}"
            events.append(
                FillEvent(
                    key=fill_key,
                    order_key=order_key,
                    position_key=canonical_position_key(row, user_id=user_id),
                    side=row_side(row),
                    quantity=delta,
                    price=price,
                    timestamp=ts,
                    source_rows=(src,),
                    classification="DETERMINISTIC_RECOVERY",
                )
            )
    return events, duplicate_sources


def position_state(fill_events: Sequence[FillEvent]) -> dict[str, Any]:
    opened: dict[str, dict[str, Any]] = {}
    closed: dict[str, dict[str, Any]] = {}
    for event in fill_events:
        book = opened.setdefault(
            event.position_key,
            {"position_id": event.position_key, "entry_fill_ids": [], "exit_fill_ids": [], "entry_qty": 0.0, "exit_qty": 0.0, "status": "open"},
        )
        if event.side == "sell":
            book["exit_qty"] += event.quantity
            book["exit_fill_ids"].append(event.key)
        else:
            book["entry_qty"] += event.quantity
            book["entry_fill_ids"].append(event.key)
            closed.pop(event.position_key, None)
        if book["entry_qty"] > 0 and book["exit_qty"] >= book["entry_qty"]:
            book["status"] = "closed"
            closed[event.position_key] = book
        elif book["exit_qty"] > 0:
            book["status"] = "partially_closed"
    return {"positions": opened, "closed": closed, "open": {k: v for k, v in opened.items() if v.get("status") != "closed"}}


def lifecycle_sources(*, root: Path, day: str, user_id: str) -> dict[str, Any]:
    attr_path = attribution_daily_path(data_dir=root / "data", user_id=user_id, day=day)
    return {
        "scanner_events": {"authoritative": "trade_attribution.candidates", "scope": "raw scanner/decision sidecar rows", "path": str(attr_path)},
        "entry_decisions": {"authoritative": "trade_attribution.candidates canonical_decision_key", "scope": "unique decisions", "path": str(attr_path)},
        "orders": {"authoritative": "trade_attribution.orders canonical_order_key", "scope": "unique logical/broker orders", "path": str(attr_path)},
        "fills": {"authoritative": "broker activity/fill id, else deterministic fill deltas", "scope": "unique fill events", "path": str(attr_path)},
        "positions": {"authoritative": "derived from unique fills and exits", "scope": "canonical positions", "path": str(attr_path)},
        "pnl": {"authoritative": "reconciled entry and exit fills", "scope": "closed trades only", "path": str(attr_path)},
        "forward_signal_outcomes": {"authoritative": "local historical bars after decision timestamp", "scope": "underlying outcomes", "path": str(root / "data")},
        "news": {"authoritative": "persisted news events plus explicit signal links", "scope": "news effectiveness", "path": str(root / "data" / "premarket")},
    }


def lifecycle_status(counts: Mapping[str, Any], problems: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    reasons: list[str] = []
    problem_kinds = {str(p.get("kind")) for p in problems or []}
    if int(counts.get("synthetic_or_replay_order_events") or 0) > 0:
        reasons.append("replay/mock/test records exist in live lifecycle sources")
        status: LifecycleStatus = "CONTAMINATED"
    elif problem_kinds & {"unmatched_fills", "positions_without_fills", "exits_without_positions", "lifecycle_invariant_failed"}:
        reasons.append("orders, fills, positions, or exits do not link cleanly")
        status = "UNRECONCILED"
    elif int(counts.get("unique_fills") or 0) == 0 and int(counts.get("unique_entry_decisions") or 0) > 0:
        reasons.append("lifecycle valid but no broker fills/trades available")
        status = "PARTIAL"
    else:
        status = "CLEAN"
    if not reasons:
        reasons.append("canonical counts reconcile")
    return {"status": status, "reasons": reasons}


def build_canonical_day(*, root: Path, day: str, user_id: str, mode: str | None = None, symbol: str | None = None, strategy: str | None = None) -> dict[str, Any]:
    payload = load_daily_artifact(attribution_daily_path(data_dir=root / "data", user_id=user_id, day=day))
    attr_path = attribution_daily_path(data_dir=root / "data", user_id=user_id, day=day)
    raw_candidates_all = rows(payload, "candidates")
    raw_allocator_all = rows(payload, "allocator_candidates")
    raw_orders_all = rows(payload, "orders")
    raw_exits_all = rows(payload, "exits")
    candidates = rows_for_et_day(raw_candidates_all, day=day)
    allocator = rows_for_et_day(raw_allocator_all, day=day)
    orders = rows_for_et_day(raw_orders_all, day=day)
    exits = rows_for_et_day(raw_exits_all, day=day)
    excluded_wrong_date = (
        len(raw_candidates_all) - len(candidates)
        + len(raw_allocator_all) - len(allocator)
        + len(raw_orders_all) - len(orders)
        + len(raw_exits_all) - len(exits)
    )
    if symbol:
        sym = norm_symbol(symbol)
        candidates = [r for r in candidates if norm_symbol(r.get("symbol")) == sym]
        allocator = [r for r in allocator if norm_symbol(r.get("symbol")) == sym]
        orders = [r for r in orders if norm_symbol(r.get("symbol")) == sym or norm_symbol(r.get("option_symbol")) == sym]
        exits = [r for r in exits if norm_symbol(r.get("symbol")) == sym]
    if strategy:
        candidates = [r for r in candidates if str(r.get("route") or r.get("source") or "") == strategy]
        allocator = [r for r in allocator if str(r.get("route") or r.get("source") or "") == strategy]
        orders = [r for r in orders if str(r.get("route") or r.get("source") or "") == strategy]
        exits = [r for r in exits if str(r.get("entry_route") or r.get("route") or "") == strategy]
    if mode:
        candidates = [r for r in candidates if infer_environment(r, user_id=user_id) == mode]
        allocator = [r for r in allocator if infer_environment(r, user_id=user_id) == mode]
        orders = [r for r in orders if infer_environment(r, user_id=user_id) == mode]
        exits = [r for r in exits if infer_environment(r, user_id=user_id) == mode]
    order_sources = source_rows(orders, path=attr_path, source="trade_attribution.orders")
    shadow_order_sources = [src for src in order_sources if is_shadow_row(src.row)]
    raw_submitted_sources = [src for src in order_sources if real_submitted_row(src.row)]
    replay_sources = [src for src in order_sources if is_replay_or_mock_row(src.row)]
    ambiguous_sources = [src for src in order_sources if lifecycle_record_class(src.row) == "AMBIGUOUS_MALFORMED"]
    clean_order_sources = [src for src in order_sources if not is_replay_or_mock_row(src.row) and not is_shadow_row(src.row)]
    submitted_sources = [src for src in clean_order_sources if real_submitted_row(src.row)]
    unique_submitted = unique_orders(submitted_sources)
    fill_events, duplicate_fill_sources = canonical_fill_events(order_sources, user_id=user_id)
    clean_submitted_keys = set(unique_submitted)
    orphan_fill_sources = [
        src
        for src in raw_fill_sources(clean_order_sources)
        if canonical_order_key(src.row) not in clean_submitted_keys
    ]
    pos_state = position_state(fill_events)
    decision_keys = {canonical_decision_key(row, user_id=user_id) for row in candidates}
    selected_keys = {canonical_decision_key(row, user_id=user_id) for row in allocator}
    duplicate_order_sources, duplicate_order_ids = duplicate_order_snapshots([src for src in order_sources if real_submitted_row(src.row)])
    snapshot_sources = [src for src in order_sources if lifecycle_record_class(src.row) == "ATTRIBUTION_SNAPSHOT"]
    replay_snapshot_groups = unique_orders(replay_sources)
    duplicate_replay_snapshot_count = sum(max(0, len(group) - 1) for group in replay_snapshot_groups.values())
    raw_fill_rows = raw_fill_sources(order_sources)
    broker_accepted = [src for src in submitted_sources if broker_accepted_row(src.row)]
    latest_by_order: dict[str, SourceRow] = {}
    for key, group in unique_submitted.items():
        latest_by_order[key] = sorted(
            group,
            key=lambda src: ((event_timestamp(src.row) or datetime.min.replace(tzinfo=timezone.utc)), src.index),
        )[-1]
    canonical_order_states = {key: canonical_order_state(src.row) for key, src in latest_by_order.items()}
    broker_confirmed_order_keys = {
        key
        for key, state in canonical_order_states.items()
        if state != "UNKNOWN_BROKER_STATE"
    }
    broker_terminal_order_keys = {
        key
        for key, state in canonical_order_states.items()
        if state in {"FILLED", "CANCELLED", "REJECTED", "EXPIRED", "REPLACED"}
    }
    broker_current_order_keys = set(canonical_order_states) - broker_terminal_order_keys
    position_lineage = {}
    for pos_id, pos in pos_state["positions"].items():
        entry_fill_ids = set(pos.get("entry_fill_ids") or [])
        source_rows_for_position = [
            src
            for event in fill_events
            if event.position_key == pos_id and event.key in entry_fill_ids
            for src in event.source_rows
        ]
        if source_rows_for_position and all(src.row.get("event_origin") == "broker_reconciliation" or src.row.get("recovered") is True for src in source_rows_for_position):
            classification = "RECOVERED_LINEAGE"
        elif source_rows_for_position:
            classification = "EXACT_LINEAGE"
        else:
            classification = "UNKNOWN_POSITION"
        position_lineage[pos_id] = {**pos, "lineage_classification": classification}
    completed_entry_fills = [event for event in fill_events if event.side != "sell"]
    recovered_order_sources = [src for src in order_sources if src.row.get("event_origin") == "broker_reconciliation" or src.row.get("recovered") is True]
    recovered_fill_sources = [src for src in raw_fill_rows if src.row.get("event_origin") == "broker_reconciliation" or src.row.get("recovered") is True]
    recovered_fill_keys = {
        event.key
        for event in fill_events
        if any(src.row.get("event_origin") == "broker_reconciliation" or src.row.get("recovered") is True for src in event.source_rows)
    }
    approved = [row for row in candidates if row.get("accepted") is True]
    blocked = [row for row in candidates if row.get("accepted") is False]
    counts = {
        "raw_scanner_events": len(candidates),
        "unique_scanner_events": len(decision_keys),
        "raw_entry_evaluations": len(candidates),
        "unique_entry_decisions": len(decision_keys),
        "route_evaluation_count": len(candidates),
        "symbol_cycle_count": len({(norm_symbol(r.get("symbol")), r.get("cycle_id") or r.get("timestamp")) for r in candidates}),
        "repeated_evaluation_count": max(0, len(candidates) - len(decision_keys)),
        "replay_event_count": 0,
        "selected_candidates": len(allocator),
        "unique_selected_candidates": len(selected_keys),
        "approved_decisions": len(approved),
        "blocked_decisions": len(blocked),
        "allocator_actions": len([r for r in allocator if r.get("action_created") or r.get("selected_rank") is not None]),
        "raw_submitted_order_events": len(raw_submitted_sources),
        "unique_submitted_orders": len(unique_submitted),
        "duplicate_order_events": len(duplicate_order_sources),
        "raw_local_order_snapshots": len([src for src in order_sources if not (src.row.get("event_origin") == "broker_reconciliation" or src.row.get("recovered") is True)]),
        "recovered_broker_order_snapshots": len(recovered_order_sources),
        "raw_broker_accepted_order_events": len(broker_accepted),
        "unique_broker_accepted_orders": len(unique_orders(broker_accepted)),
        "local_submitted_orders": len(unique_submitted),
        "broker_confirmed_orders": len(broker_confirmed_order_keys),
        "broker_current_orders": len(broker_current_order_keys),
        "broker_terminal_orders": len(broker_terminal_order_keys),
        "broker_filled_orders": sum(1 for state in canonical_order_states.values() if state == "FILLED"),
        "broker_partially_filled_orders": sum(1 for state in canonical_order_states.values() if state == "PARTIALLY_FILLED"),
        "broker_canonical_accepted_orders": sum(1 for state in canonical_order_states.values() if state == "ACCEPTED"),
        "broker_rejected_orders": sum(1 for state in canonical_order_states.values() if state == "REJECTED"),
        "broker_cancelled_orders": sum(1 for state in canonical_order_states.values() if state == "CANCELLED"),
        "broker_expired_orders": sum(1 for state in canonical_order_states.values() if state == "EXPIRED"),
        "broker_replaced_orders": sum(1 for state in canonical_order_states.values() if state == "REPLACED"),
        "broker_unresolved_orders": sum(1 for state in canonical_order_states.values() if state in {"PENDING", "UNKNOWN_BROKER_STATE"}),
        "broker_pending_orders": sum(1 for state in canonical_order_states.values() if state == "PENDING"),
        "broker_unknown_state_orders": sum(1 for state in canonical_order_states.values() if state == "UNKNOWN_BROKER_STATE"),
        "broker_accepted_orders_local": len(unique_orders([src for src in broker_accepted if src.row.get("event_origin") != "broker_reconciliation" and src.row.get("recovered") is not True])),
        "broker_accepted_orders_reconciled": len(unique_orders([src for src in broker_accepted if src.row.get("event_origin") == "broker_reconciliation" or src.row.get("recovered") is True])),
        "raw_fill_events": len(raw_fill_rows),
        "local_fill_events": len([src for src in raw_fill_rows if src.row.get("event_origin") != "broker_reconciliation" and src.row.get("recovered") is not True]),
        "raw_local_fill_events": len([src for src in raw_fill_rows if src.row.get("event_origin") != "broker_reconciliation" and src.row.get("recovered") is not True]),
        "recovered_broker_fill_events": len(recovered_fill_sources),
        "contaminated_fill_events": len(ambiguous_sources),
        "orphan_fill_events": len(orphan_fill_sources),
        "unique_fills": len(fill_events),
        "canonical_fills": len(fill_events),
        "broker_reconciled_fill_events": len(recovered_fill_keys),
        "duplicate_fill_events": len(duplicate_fill_sources),
        "raw_position_records": len(raw_fill_rows),
        "unique_opened_positions": len(pos_state["positions"]),
        "canonical_positions": len(pos_state["positions"]),
        "positions_with_exact_lineage": len([p for p in position_lineage.values() if p.get("lineage_classification") == "EXACT_LINEAGE"]),
        "positions_with_recovered_lineage": len([p for p in position_lineage.values() if p.get("lineage_classification") == "RECOVERED_LINEAGE"]),
        "positions_with_missing_lineage": len([p for p in position_lineage.values() if p.get("lineage_classification") in {"AMBIGUOUS_LINEAGE", "UNKNOWN_POSITION"}]),
        "local_positions_today": len(
            {
                event.position_key
                for event in fill_events
                if not any(src.row.get("event_origin") == "broker_reconciliation" or src.row.get("recovered") is True for src in event.source_rows)
            }
        ),
        "broker_reconciled_positions_today": len(
            {
                event.position_key
                for event in fill_events
                if any(src.row.get("event_origin") == "broker_reconciliation" or src.row.get("recovered") is True for src in event.source_rows)
            }
        ),
        "unique_closed_positions": len(pos_state["closed"]),
        "unique_still_open_positions": len(pos_state["open"]),
        "exit_orders": len([src for src in order_sources if row_side(src.row) == "sell"]),
        "synthetic_or_replay_order_events": len(ambiguous_sources),
        "unresolved_contamination": len(ambiguous_sources),
        "attribution_snapshots": len(snapshot_sources),
        "duplicate_attribution_snapshots": len(snapshot_sources) - len(unique_orders(snapshot_sources)),
        "replay_research_outcomes": len(replay_sources),
        "duplicate_replay_research_outcomes": duplicate_replay_snapshot_count,
        "shadow_hypothetical_outcomes": len(shadow_order_sources),
        "ambiguous_unresolved_records": len(ambiguous_sources),
        "real_broker_fills": len(fill_events),
        "real_broker_positions": len(pos_state["positions"]),
        "legacy_shadow_records_reclassified": len(
            [src for src in shadow_order_sources if str(shadow_reclassification_reason(src.row) or "").startswith("legacy_")]
        ),
        "real_order_submission_attempts": len(raw_submitted_sources),
        "real_broker_accepted_orders": len(unique_orders(broker_accepted)),
        "shadow_decisions": len([r for r in candidates if is_shadow_row(r)]),
        "shadow_allocator_actions": len(
            [
                r
                for r in allocator
                if is_shadow_row(r) and (r.get("action_created") or r.get("selected_rank") is not None)
            ]
        ),
        "shadow_order_intents": len([src for src in shadow_order_sources if submitted_row(src.row) or src.row.get("hypothetical")]),
        "shadow_execution_blocks": len(
            [
                src
                for src in shadow_order_sources
                if src.row.get("broker_dispatch_attempted") is False
                or src.row.get("execution_allowed") is False
                or "blocked" in str(src.row.get("reason") or src.row.get("status") or "").lower()
            ]
        ),
        "excluded_wrong_or_missing_event_date_records": excluded_wrong_date,
        "submitted_orders": len(unique_submitted),
        "broker_accepted_orders": len(unique_orders(broker_accepted)),
        "completed_fills": len(completed_entry_fills),
        "opened_positions": len(pos_state["positions"]),
        "closed_positions": len(pos_state["closed"]),
        "still_open_positions": len(pos_state["open"]),
    }
    status = lifecycle_status(counts, [])
    return {
        "date": day,
        "user": user_id,
        "scope": {"source": str(attr_path), "date_basis": "event_timestamp_america_new_york", "environment_filter": mode, "record_origin_filter": "live excludes replay/mock/test and expected shadow telemetry as broker evidence"},
        "sources": lifecycle_sources(root=root, day=day, user_id=user_id),
        "payload": payload,
        "candidates": candidates,
        "allocator": allocator,
        "orders": orders,
        "exits": exits,
        "order_sources": order_sources,
        "submitted_sources": submitted_sources,
        "unique_submitted": unique_submitted,
        "fill_events": fill_events,
        "duplicate_order_sources": duplicate_order_sources,
        "duplicate_order_ids": duplicate_order_ids,
        "duplicate_fill_sources": duplicate_fill_sources,
        "position_state": pos_state,
        "position_lineage": position_lineage,
        "order_reconciliation": {
            key: {
                "canonical_order_key": key,
                "decision_id": latest_by_order[key].row.get("decision_id"),
                "logical_order_id": latest_by_order[key].row.get("logical_order_id") or latest_by_order[key].row.get("order_id"),
                "client_order_id": latest_by_order[key].row.get("client_order_id"),
                "route": latest_by_order[key].row.get("route") or latest_by_order[key].row.get("source") or latest_by_order[key].row.get("strategy"),
                "submitted_at": latest_by_order[key].row.get("submitted_at") or latest_by_order[key].row.get("created_at") or latest_by_order[key].row.get("timestamp"),
                "terminal_state": state_name,
                "broker_status": row_status(latest_by_order[key].row),
                "broker_status_timestamp": latest_by_order[key].row.get("broker_status_timestamp") or latest_by_order[key].row.get("updated_at") or latest_by_order[key].row.get("timestamp"),
                "reconciled_at": latest_by_order[key].row.get("reconciled_at") or latest_by_order[key].row.get("reconciliation_timestamp"),
                "broker_order_id": latest_by_order[key].row.get("broker_order_id") or latest_by_order[key].row.get("order_id"),
                "symbol": latest_by_order[key].row.get("symbol"),
                "side": row_side(latest_by_order[key].row),
            }
            for key, state_name in sorted(canonical_order_states.items())
        },
        "counts": counts,
        "integrity_status": status,
    }


def quarantine_candidates(day: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for src in day.get("order_sources") or []:
        if isinstance(src, SourceRow) and lifecycle_record_class(src.row) == "AMBIGUOUS_MALFORMED":
            out.append(
                {
                    "source_path": src.path,
                    "source_record_index": src.index,
                    "record_hash": raw_record_hash(src.row),
                    "classification": "DUPLICATE" if filled_row(src.row) else "AMBIGUOUS",
                    "quarantine_reason": "LIVE_DATA_CONTAMINATION_BLOCKED",
                }
            )
    return out


def write_quarantine_artifact(*, root: Path, day: str, candidates: Sequence[Mapping[str, Any]]) -> Path:
    out_dir = root / "data" / "quarantine" / "replay_contamination" / day
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "manifest.json"
    payload = {
        "date": day,
        "quarantine_timestamp": datetime.now(timezone.utc).isoformat(),
        "records": [dict(row) for row in candidates],
        "non_destructive": True,
    }
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def as_dict(record: Any) -> dict[str, Any]:
    return asdict(record)
