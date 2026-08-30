"""Live risk protection guards driven by recent session and intraday state."""

from __future__ import annotations

import json
import logging
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from src.trade_attribution import attribution_daily_path, load_daily_artifact

log = logging.getLogger(__name__)

WEAK_EXIT_REASONS = {
    "signal_flip",
    "stop_loss",
    "trailing_stop",
    "dynamic_trailing_stop",
    "dynamic_vwap_break",
    "kill_switch",
    "risk_emergency_deleverage",
}


@dataclass(frozen=True)
class LiveRiskGuardState:
    trend_long_entries_blocked: bool = False
    new_entries_blocked: bool = False
    flatten_risk: bool = False
    triggered_guards: tuple[str, ...] = ()
    total_pnl: float = 0.0
    loss_pct_equity: float = 0.0
    sleeve_blocks: Mapping[str, int] | None = None
    sleeve_size_multipliers: Mapping[str, float] | None = None
    sleeve_loss_counts: Mapping[str, int] | None = None


def _safe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _artifact_candidates(data_dir: Path, *, day: str, user_id: str) -> list[Path]:
    return [
        data_dir / "profitability_attribution" / "daily" / f"{day}_{user_id}.json",
        data_dir / "trade_attribution" / "daily" / f"{day}_{user_id}.json",
        data_dir / "research_metrics" / day / "signal_expectancy_report.json",
        data_dir / "daily_summary" / f"{day}_{user_id}.json",
    ]


def _route_stats_from_payload(payload: Any) -> Mapping[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    route_stats = payload.get("route_stats")
    if isinstance(route_stats, Mapping):
        return route_stats
    routes = payload.get("routes")
    if isinstance(routes, Mapping):
        return routes
    summary = payload.get("summary")
    if isinstance(summary, Mapping):
        nested = summary.get("route_stats") or summary.get("routes")
        if isinstance(nested, Mapping):
            return nested
    return None


def _trend_long_session_loss(payload: Any) -> tuple[bool, dict[str, Any]]:
    stats = _route_stats_from_payload(payload)
    if isinstance(stats, Mapping) and isinstance(stats.get("trend_long"), Mapping):
        row = stats["trend_long"]
        win_rate = _safe_float(row.get("win_rate"))
        pnl = _safe_float(
            row.get("pnl")
            if row.get("pnl") is not None
            else row.get("realized_pnl")
            if row.get("realized_pnl") is not None
            else row.get("total_pnl")
        )
        ok = win_rate == 0.0 and pnl is not None and pnl < 0.0
        return ok, {"win_rate": win_rate, "trend_long_pnl": pnl, "source": "route_stats"}
    if isinstance(payload, Mapping):
        pnl_by_route = payload.get("pnl_by_route")
        if isinstance(pnl_by_route, Mapping):
            pnl = _safe_float(pnl_by_route.get("trend_long"))
            stats = payload.get("route_stats")
            row = stats.get("trend_long") if isinstance(stats, Mapping) and isinstance(stats.get("trend_long"), Mapping) else {}
            win_rate = _safe_float(row.get("win_rate")) if isinstance(row, Mapping) else None
            ok = win_rate == 0.0 and pnl is not None and pnl < 0.0
            return ok, {"win_rate": win_rate, "trend_long_pnl": pnl, "source": "pnl_by_route"}
    return False, {"win_rate": None, "trend_long_pnl": None, "source": "missing"}


def _recent_days(before_day: date, count: int = 7) -> list[str]:
    return [(before_day - timedelta(days=offset)).isoformat() for offset in range(1, count + 1)]


def consecutive_live_trend_long_losses(
    *,
    data_dir: Path | str,
    user_id: str,
    session_day: date | str,
    required_sessions: int = 2,
) -> dict[str, Any]:
    """Return whether recent live summaries should block next-session trend_long entries."""

    data = Path(data_dir)
    day = date.fromisoformat(str(session_day)) if not isinstance(session_day, date) else session_day
    losses: list[dict[str, Any]] = []
    for day_s in _recent_days(day, count=10):
        payload = None
        source = None
        for path in _artifact_candidates(data, day=day_s, user_id=user_id):
            if path.exists():
                payload = _load_json(path)
                source = str(path)
                if payload is not None:
                    break
        if payload is None:
            continue
        is_loss, meta = _trend_long_session_loss(payload)
        meta = dict(meta)
        meta.update({"date": day_s, "artifact": source, "loss_session": bool(is_loss)})
        if not is_loss:
            break
        losses.append(meta)
        if len(losses) >= int(required_sessions):
            break
    triggered = len(losses) >= int(required_sessions)
    return {
        "triggered": triggered,
        "reason": "two_live_trend_long_zero_win_loss_sessions" if triggered else "ok",
        "sessions": losses,
    }


def realized_pnl_for_day(*, data_dir: Path | str, user_id: str, day: date | str) -> float:
    path = attribution_daily_path(data_dir=data_dir, user_id=user_id, day=day)
    if not path.exists():
        return 0.0
    payload = load_daily_artifact(path)
    exits = payload.get("exits") if isinstance(payload.get("exits"), list) else []
    total = 0.0
    for row in exits:
        if isinstance(row, Mapping):
            pnl = _safe_float(row.get("pnl") if row.get("pnl") is not None else row.get("realized_pnl"))
            if pnl is not None:
                total += pnl
    return total


def unrealized_pnl_from_positions(positions: Sequence[Mapping[str, Any]] | None) -> float:
    total = 0.0
    for row in positions or []:
        if not isinstance(row, Mapping):
            continue
        pnl = _safe_float(row.get("unrealized_pl") if row.get("unrealized_pl") is not None else row.get("unrealized_pnl"))
        if pnl is not None:
            total += pnl
    return total


def intraday_loss_guard(
    *,
    realized_pnl: float,
    unrealized_pnl: float,
    account_equity: float,
    entry_stop_pct: float = 0.35,
    flatten_pct: float = 0.50,
) -> dict[str, Any]:
    total = float(realized_pnl) + float(unrealized_pnl)
    equity = max(0.0, float(account_equity or 0.0))
    pct = (total / equity * 100.0) if equity > 0.0 else 0.0
    if equity <= 0.0:
        return {"action": "allow", "reason": "equity_unavailable", "total_pnl": total, "loss_pct_equity": pct}
    if pct <= -abs(float(flatten_pct)):
        return {"action": "flatten", "reason": "intraday_loss_flatten", "total_pnl": total, "loss_pct_equity": pct}
    if pct <= -abs(float(entry_stop_pct)):
        return {"action": "stop_entries", "reason": "intraday_loss_stop_entries", "total_pnl": total, "loss_pct_equity": pct}
    return {"action": "allow", "reason": "ok", "total_pnl": total, "loss_pct_equity": pct}


def _normalize_sleeve(row: Mapping[str, Any]) -> str:
    for key in ("sleeve", "bucket", "entry_route", "route", "entry_source", "source", "strategy"):
        raw = row.get(key)
        if raw is not None and str(raw).strip():
            return str(raw).strip().lower()
    return "unknown"


def sleeve_weak_exit_blocks(
    *,
    data_dir: Path | str,
    user_id: str,
    day: date | str,
    threshold: int = 3,
) -> dict[str, int]:
    path = attribution_daily_path(data_dir=data_dir, user_id=user_id, day=day)
    if not path.exists():
        return {}
    payload = load_daily_artifact(path)
    exits = payload.get("exits") if isinstance(payload.get("exits"), list) else []
    counts: Counter[str] = Counter()
    for row in exits:
        if not isinstance(row, Mapping):
            continue
        reason = str(row.get("exit_reason") or "").strip().lower()
        pnl = _safe_float(row.get("pnl"))
        pnl_pct = _safe_float(row.get("pnl_pct"))
        weak = reason in WEAK_EXIT_REASONS or (pnl is not None and pnl < 0.0) or (pnl_pct is not None and pnl_pct < 0.0)
        if weak:
            counts[_normalize_sleeve(row)] += 1
    return {sleeve: count for sleeve, count in counts.items() if count >= int(threshold)}


def sleeve_adaptive_sizing(
    *,
    data_dir: Path | str,
    user_id: str,
    day: date | str,
    block_threshold: int = 3,
) -> dict[str, dict[str, Any]]:
    path = attribution_daily_path(data_dir=data_dir, user_id=user_id, day=day)
    if not path.exists():
        return {}
    payload = load_daily_artifact(path)
    exits = payload.get("exits") if isinstance(payload.get("exits"), list) else []
    counts: Counter[str] = Counter()
    for row in exits:
        if not isinstance(row, Mapping):
            continue
        reason = str(row.get("exit_reason") or "").strip().lower()
        pnl = _safe_float(row.get("pnl", row.get("realized_pnl")))
        pnl_pct = _safe_float(row.get("pnl_pct", row.get("realized_pnl_pct")))
        weak = reason in WEAK_EXIT_REASONS or (pnl is not None and pnl < 0.0) or (pnl_pct is not None and pnl_pct < 0.0)
        if weak:
            counts[_normalize_sleeve(row)] += 1
    out: dict[str, dict[str, Any]] = {}
    threshold = max(1, int(block_threshold or 3))
    for sleeve, count in counts.items():
        if count >= threshold:
            multiplier = 0.0
            blocked = True
        elif count >= 2:
            multiplier = 0.25
            blocked = False
        elif count >= 1:
            multiplier = 0.5
            blocked = False
        else:
            multiplier = 1.0
            blocked = False
        out[sleeve] = {"count": int(count), "multiplier": float(multiplier), "blocked": bool(blocked)}
    return out


def sleeve_for_route(route: Any, source: Any = None) -> str:
    return _normalize_sleeve({"route": route, "source": source})


def build_live_risk_guard_state(
    *,
    data_dir: Path | str,
    user_id: str,
    session_day: date | str,
    account_equity: float,
    positions: Sequence[Mapping[str, Any]] | None = None,
    config: Mapping[str, Any] | None = None,
) -> LiveRiskGuardState:
    cfg = (config or {}).get("live_risk_protection") if isinstance(config, Mapping) else {}
    cfg = cfg if isinstance(cfg, Mapping) else {}
    consec_cfg = cfg.get("consecutive_losing_sessions") if isinstance(cfg.get("consecutive_losing_sessions"), Mapping) else {}
    loss_cfg = cfg.get("intraday_loss_guard") if isinstance(cfg.get("intraday_loss_guard"), Mapping) else {}
    sleeve_cfg = cfg.get("sleeve_churn_guard") if isinstance(cfg.get("sleeve_churn_guard"), Mapping) else {}
    try:
        sessions = int(consec_cfg.get("sessions", 2) or 2)
    except (TypeError, ValueError):
        sessions = 2
    try:
        stop_loss_pct = float(loss_cfg.get("stop_new_entries_loss_pct", 0.35) or 0.35)
    except (TypeError, ValueError):
        stop_loss_pct = 0.35
    try:
        flatten_pct = float(loss_cfg.get("flatten_loss_pct", 0.50) or 0.50)
    except (TypeError, ValueError):
        flatten_pct = 0.50
    try:
        sleeve_threshold = int(sleeve_cfg.get("weak_exit_threshold", 3) or 3)
    except (TypeError, ValueError):
        sleeve_threshold = 3
    trend_enabled = bool(consec_cfg.get("enabled", True))
    loss_enabled = bool(loss_cfg.get("enabled", True))
    sleeve_enabled = bool(sleeve_cfg.get("enabled", True))
    trend = (
        consecutive_live_trend_long_losses(
            data_dir=data_dir,
            user_id=user_id,
            session_day=session_day,
            required_sessions=sessions,
        )
        if trend_enabled
        else {"triggered": False, "reason": "disabled", "sessions": []}
    )
    realized = realized_pnl_for_day(data_dir=data_dir, user_id=user_id, day=session_day)
    unrealized = unrealized_pnl_from_positions(positions)
    loss = (
        intraday_loss_guard(
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            account_equity=account_equity,
            entry_stop_pct=stop_loss_pct,
            flatten_pct=flatten_pct,
        )
        if loss_enabled
        else {"action": "allow", "reason": "disabled", "total_pnl": realized + unrealized, "loss_pct_equity": 0.0}
    )
    sleeves = (
        sleeve_weak_exit_blocks(
            data_dir=data_dir,
            user_id=user_id,
            day=session_day,
            threshold=sleeve_threshold,
        )
        if sleeve_enabled
        else {}
    )
    sleeve_sizing = (
        sleeve_adaptive_sizing(
            data_dir=data_dir,
            user_id=user_id,
            day=session_day,
            block_threshold=sleeve_threshold,
        )
        if sleeve_enabled
        else {}
    )
    sleeve_size_multipliers = {
        sleeve: float(row.get("multiplier", 1.0))
        for sleeve, row in sleeve_sizing.items()
        if isinstance(row, Mapping) and float(row.get("multiplier", 1.0)) < 1.0
    }
    sleeve_loss_counts = {
        sleeve: int(row.get("count", 0))
        for sleeve, row in sleeve_sizing.items()
        if isinstance(row, Mapping) and int(row.get("count", 0)) > 0
    }
    guards: list[str] = []
    if trend["triggered"]:
        guards.append("trend_long_consecutive_losses")
    if loss["action"] == "stop_entries":
        guards.append("intraday_loss_stop_entries")
    elif loss["action"] == "flatten":
        guards.append("intraday_loss_flatten")
    if sleeves:
        guards.append("sleeve_churn_guard")
    return LiveRiskGuardState(
        trend_long_entries_blocked=bool(trend["triggered"]),
        new_entries_blocked=loss["action"] in {"stop_entries", "flatten"},
        flatten_risk=loss["action"] == "flatten",
        triggered_guards=tuple(guards),
        total_pnl=float(loss["total_pnl"]),
        loss_pct_equity=float(loss["loss_pct_equity"]),
        sleeve_blocks=sleeves,
        sleeve_size_multipliers=sleeve_size_multipliers,
        sleeve_loss_counts=sleeve_loss_counts,
    )


def record_guard_summary(
    *,
    data_dir: Path | str,
    user_id: str,
    day: date | str,
    state: LiveRiskGuardState,
) -> Path | None:
    try:
        path = Path(data_dir) / "risk_guards" / f"{day}_{user_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "date": str(day),
            "user_id": str(user_id),
            "triggered_guards": list(state.triggered_guards),
            "trend_long_entries_blocked": state.trend_long_entries_blocked,
            "new_entries_blocked": state.new_entries_blocked,
            "flatten_risk": state.flatten_risk,
            "total_pnl": state.total_pnl,
            "loss_pct_equity": state.loss_pct_equity,
            "sleeve_blocks": dict(state.sleeve_blocks or {}),
            "sleeve_size_multipliers": dict(state.sleeve_size_multipliers or {}),
            "sleeve_loss_counts": dict(state.sleeve_loss_counts or {}),
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path
    except Exception:
        log.warning("LIVE_RISK_GUARD_SUMMARY_WRITE_FAILED user_id=%s day=%s", user_id, day, exc_info=True)
        return None
