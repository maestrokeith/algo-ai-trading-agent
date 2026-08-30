"""General strategy shadow mode ledger and comparison tools."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ShadowDecision:
    """No-capital strategy decision."""

    strategy: str
    symbol: str
    action: str
    confidence: float
    reason: str
    timestamp: str
    reference_price: float | None = None


@dataclass(frozen=True)
class ShadowComparison:
    """Comparison between shadow and live decisions."""

    strategy: str
    total_shadow: int
    total_live: int
    matches: int
    shadow_only: int
    live_only: int
    realized_pnl: float

    @property
    def match_rate(self) -> float:
        """Decision match rate over shadow decisions."""
        if self.total_shadow <= 0:
            return 0.0
        return self.matches / self.total_shadow

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-serializable comparison."""
        return {**self.__dict__, "match_rate": self.match_rate}


def shadow_ledger_path(user_id: str = "default", *, data_dir: Path | None = None) -> Path:
    """Ledger path for generic shadow-mode records."""
    root = data_dir or (Path(__file__).resolve().parents[1] / "data")
    return root / f"shadow_mode_{user_id}.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"decisions": [], "outcomes": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"decisions": [], "outcomes": []}
    if not isinstance(payload, dict):
        return {"decisions": [], "outcomes": []}
    payload.setdefault("decisions", [])
    payload.setdefault("outcomes", [])
    return payload


def _save(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def shadow_mode_enabled(config: Mapping[str, Any] | None, strategy: str | None = None) -> bool:
    """Return whether shadow mode is enabled globally or for a strategy."""
    shadow = (config or {}).get("shadow_mode")
    if not isinstance(shadow, Mapping) or not bool(shadow.get("enabled")):
        return False
    strategies = shadow.get("strategies")
    if strategy and isinstance(strategies, Sequence) and not isinstance(strategies, (str, bytes)):
        return str(strategy) in {str(item) for item in strategies}
    return True


def record_shadow_decision(
    *,
    strategy: str,
    symbol: str,
    action: str,
    confidence: float = 0.0,
    reason: str = "",
    reference_price: float | None = None,
    user_id: str = "default",
    data_dir: Path | None = None,
    timestamp: str | None = None,
) -> ShadowDecision:
    """Record a no-capital strategy decision."""
    decision = ShadowDecision(
        strategy=str(strategy),
        symbol=str(symbol).strip().upper(),
        action=str(action).strip().lower(),
        confidence=float(confidence),
        reason=str(reason),
        timestamp=timestamp or _utc_now(),
        reference_price=reference_price,
    )
    path = shadow_ledger_path(user_id, data_dir=data_dir)
    payload = _load(path)
    payload["decisions"].append(decision.__dict__)
    _save(path, payload)
    return decision


def record_shadow_outcome(
    *,
    strategy: str,
    symbol: str,
    pnl: float,
    return_pct: float | None = None,
    user_id: str = "default",
    data_dir: Path | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Record simulated shadow outcome."""
    outcome = {
        "strategy": str(strategy),
        "symbol": str(symbol).strip().upper(),
        "pnl": float(pnl),
        "return_pct": return_pct,
        "timestamp": timestamp or _utc_now(),
    }
    path = shadow_ledger_path(user_id, data_dir=data_dir)
    payload = _load(path)
    payload["outcomes"].append(outcome)
    _save(path, payload)
    return outcome


def load_shadow_ledger(user_id: str = "default", *, data_dir: Path | None = None) -> dict[str, Any]:
    """Load shadow-mode ledger."""
    return _load(shadow_ledger_path(user_id, data_dir=data_dir))


def compare_shadow_to_live(
    *,
    live_decisions: Sequence[Mapping[str, Any]],
    user_id: str = "default",
    data_dir: Path | None = None,
    strategy: str | None = None,
) -> ShadowComparison:
    """Compare shadow decisions against live decisions by strategy/symbol/action."""
    ledger = load_shadow_ledger(user_id, data_dir=data_dir)
    shadow_decisions = [
        row for row in ledger.get("decisions", []) if strategy is None or row.get("strategy") == strategy
    ]
    live_rows = [
        row for row in live_decisions if strategy is None or row.get("strategy") == strategy
    ]
    live_keys = {
        (
            str(row.get("strategy") or ""),
            str(row.get("symbol") or "").upper(),
            str(row.get("action") or "").lower(),
        )
        for row in live_rows
    }
    shadow_keys = {
        (
            str(row.get("strategy") or ""),
            str(row.get("symbol") or "").upper(),
            str(row.get("action") or "").lower(),
        )
        for row in shadow_decisions
    }
    matches = len(shadow_keys & live_keys)
    outcomes = [
        row for row in ledger.get("outcomes", []) if strategy is None or row.get("strategy") == strategy
    ]
    realized = 0.0
    for row in outcomes:
        try:
            realized += float(row.get("pnl") or 0.0)
        except (TypeError, ValueError):
            continue
    return ShadowComparison(
        strategy=strategy or "all",
        total_shadow=len(shadow_keys),
        total_live=len(live_keys),
        matches=matches,
        shadow_only=len(shadow_keys - live_keys),
        live_only=len(live_keys - shadow_keys),
        realized_pnl=realized,
    )


__all__ = [
    "ShadowComparison",
    "ShadowDecision",
    "compare_shadow_to_live",
    "load_shadow_ledger",
    "record_shadow_decision",
    "record_shadow_outcome",
    "shadow_ledger_path",
    "shadow_mode_enabled",
]
