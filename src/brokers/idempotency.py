"""Broker-neutral idempotency helpers for order submission."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ACTIVE_ORDER_STATUSES = {
    "NEW",
    "ACCEPTED",
    "PENDING",
    "PARTIALLY_FILLED",
    "new",
    "queued",
    "confirmed",
    "unconfirmed",
    "partially_filled",
    "accepted",
    "pending",
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def stable_client_order_id(
    *,
    user: str | None,
    route: str | None,
    symbol: str,
    side: str,
    signal_identity: str | None = None,
    trading_date: date | str | None = None,
    entry_exit_identity: str | None = None,
) -> str:
    """Return an AlgoSphere idempotency identity stable across retries/restarts."""

    d = trading_date or datetime.now(timezone.utc).date().isoformat()
    parts = [
        _clean(user) or "default",
        _clean(route) or "unknown_route",
        _clean(symbol).upper(),
        _clean(side).lower(),
        _clean(signal_identity) or "signal_unknown",
        _clean(d),
        _clean(entry_exit_identity) or "entry",
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"as-{digest}"


def client_order_id_for_order(order: Any, *, default_user: str = "default") -> str:
    existing = getattr(order, "client_order_id", None) or getattr(order, "client_order_key", None)
    if existing:
        return str(existing)
    route = (
        getattr(order, "route", None)
        or getattr(order, "strategy_route", None)
        or getattr(order, "strategy", None)
        or getattr(order, "source", None)
    )
    signal_identity = (
        getattr(order, "signal_identity", None)
        or getattr(order, "signal_id", None)
        or getattr(order, "entry_signal_id", None)
    )
    entry_exit_identity = getattr(order, "entry_exit_identity", None) or getattr(order, "exit_reason", None)
    cid = stable_client_order_id(
        user=getattr(order, "user_id", None) or default_user,
        route=route,
        symbol=getattr(order, "symbol", ""),
        side=getattr(order, "side", ""),
        signal_identity=signal_identity,
        trading_date=getattr(order, "trading_date", None),
        entry_exit_identity=entry_exit_identity,
    )
    try:
        setattr(order, "client_order_id", cid)
    except Exception:
        pass
    return cid


@dataclass
class IdempotencyRecord:
    client_order_id: str
    broker: str
    symbol: str
    side: str
    broker_order_id: str | None = None
    status: str | None = None
    updated_at: str | None = None
    raw: Mapping[str, Any] | None = None


class LocalIdempotencyStore:
    """Small JSON-backed local submission index used before broker submission."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root or Path.cwd() / "data" / "broker_idempotency")
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, client_order_id: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(client_order_id))
        return self.root / f"{safe}.json"

    def get(self, client_order_id: str) -> IdempotencyRecord | None:
        path = self._path(client_order_id)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return IdempotencyRecord(**{k: payload.get(k) for k in IdempotencyRecord.__dataclass_fields__})

    def put(self, record: IdempotencyRecord) -> Path:
        path = self._path(record.client_order_id)
        payload = asdict(record)
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        tmp.replace(path)
        return path

    def has_active(self, client_order_id: str) -> bool:
        record = self.get(client_order_id)
        if record is None:
            return False
        return str(record.status or "").upper() in ACTIVE_ORDER_STATUSES
