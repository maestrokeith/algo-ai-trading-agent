"""Runtime progress artifacts for shadow/session diagnostics."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from src.artifact_writability import atomic_write_text

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def trading_day_et(now: datetime | None = None) -> str:
    dt = now or datetime.now(ET)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return dt.astimezone(ET).date().isoformat()


def runtime_progress_path(data_dir: Path | str, *, day: str, user_id: str) -> Path:
    safe_user = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(user_id or "default")) or "default"
    return Path(data_dir) / "runtime_progress" / "daily" / f"{day}_{safe_user}.json"


def _iso(dt: datetime | None = None) -> str:
    value = dt or datetime.now(ET)
    if value.tzinfo is None:
        value = value.replace(tzinfo=ET)
    return value.astimezone(ET).isoformat()


def _git_commit(project_root: Path | str | None) -> str | None:
    if project_root is None:
        return None
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(project_root),
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
    except Exception:
        return None


def _load_payload(path: Path, *, day: str, user_id: str) -> dict[str, Any]:
    if not path.exists():
        return {"date": day, "user_id": user_id, "events": [], "counts": {}, "latest": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"date": day, "user_id": user_id, "events": [], "counts": {}, "latest": {}, "recovered_from_unreadable": True}
    if not isinstance(payload, dict):
        return {"date": day, "user_id": user_id, "events": [], "counts": {}, "latest": {}, "recovered_from_invalid": True}
    payload.setdefault("date", day)
    payload.setdefault("user_id", user_id)
    payload.setdefault("events", [])
    payload.setdefault("counts", {})
    payload.setdefault("latest", {})
    return payload


def record_runtime_event(
    data_dir: Path | str,
    *,
    user_id: str,
    event: str,
    timestamp: datetime | None = None,
    project_root: Path | str | None = None,
    configured_mode: str | None = None,
    effective_mode: str | None = None,
    live_orders_allowed: bool | None = None,
    paper_orders_allowed: bool | None = None,
    broker_submission_allowed: bool | None = None,
    pid: int | None = None,
    details: Mapping[str, Any] | None = None,
) -> Path:
    ts = timestamp or datetime.now(ET)
    day = trading_day_et(ts)
    path = runtime_progress_path(data_dir, day=day, user_id=user_id)
    payload = _load_payload(path, day=day, user_id=user_id)
    counts = payload.setdefault("counts", {})
    latest = payload.setdefault("latest", {})
    event_name = str(event or "RUNTIME_HEARTBEAT").strip().upper()
    row = {
        "event": event_name,
        "timestamp": _iso(ts),
        "user_id": user_id,
        "pid": int(pid if pid is not None else os.getpid()),
        "configured_mode": configured_mode,
        "effective_mode": effective_mode,
        "live_orders_allowed": live_orders_allowed,
        "paper_orders_allowed": paper_orders_allowed,
        "broker_submission_allowed": broker_submission_allowed,
        "git_commit": _git_commit(project_root),
        "details": dict(details or {}),
    }
    payload["generated_at"] = _iso()
    payload["last_event"] = row
    events = [item for item in payload.get("events", []) if isinstance(item, dict)]
    events.append(row)
    payload["events"] = events[-500:]
    counts[event_name] = int(counts.get(event_name) or 0) + 1
    latest[event_name] = row["timestamp"]
    if event_name == "SERVICE_STARTUP":
        payload["startup"] = row
    if effective_mode:
        payload["effective_mode"] = str(effective_mode)
    if configured_mode:
        payload["configured_mode"] = str(configured_mode)
    atomic_write_text(
        path,
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        generator="runtime_progress",
    )
    return path


def load_runtime_progress(data_dir: Path | str, *, day: str, user_id: str) -> dict[str, Any] | None:
    path = runtime_progress_path(data_dir, day=day, user_id=user_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"date": day, "user_id": user_id, "path": str(path), "unreadable": True}
    if isinstance(payload, dict):
        payload["path"] = str(path)
        return payload
    return {"date": day, "user_id": user_id, "path": str(path), "invalid": True}


@dataclass(frozen=True)
class SessionActivity:
    status: str
    account_fetch_succeeded: bool
    scanner_cycles_expected: int
    scanner_cycles_completed: int
    entry_cycles_expected: int
    entry_cycles_completed: int
    first_cycle_timestamp: str | None
    last_cycle_timestamp: str | None
    skipped_cycle_reasons: list[str]
    minutes_of_observed_open_session: float
    shadow_intents_current_day: int
    service_start_time: str | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_activity_status": self.status,
            "account_fetch_succeeded": self.account_fetch_succeeded,
            "scanner_cycles_expected": self.scanner_cycles_expected,
            "scanner_cycles_completed": self.scanner_cycles_completed,
            "entry_cycles_expected": self.entry_cycles_expected,
            "entry_cycles_completed": self.entry_cycles_completed,
            "first_cycle_timestamp": self.first_cycle_timestamp,
            "last_cycle_timestamp": self.last_cycle_timestamp,
            "skipped_cycle_reasons": self.skipped_cycle_reasons,
            "minutes_of_observed_open_session": self.minutes_of_observed_open_session,
            "shadow_intents_current_day": self.shadow_intents_current_day,
            "service_start_time": self.service_start_time,
            "reason": self.reason,
        }


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).astimezone(ET)
    except Exception:
        return None


def summarize_session_activity(
    progress: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
    cadence_minutes: float = 10.0,
    grace_minutes: float = 5.0,
    market_open: time = time(9, 30),
    entry_cutoff: time | None = None,
    shadow_intents_current_day: int = 0,
) -> dict[str, Any]:
    dt = now or datetime.now(ET)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    dt = dt.astimezone(ET)
    events = [row for row in ((progress or {}).get("events") or []) if isinstance(row, Mapping)]
    counts = (progress or {}).get("counts") if isinstance((progress or {}).get("counts"), Mapping) else {}
    service_start = _parse_ts(((progress or {}).get("startup") or {}).get("timestamp"))
    scan_times = [_parse_ts(row.get("timestamp")) for row in events if row.get("event") in {"SCAN_CYCLE_COMPLETED", "DYNAMIC_SCAN_COMPLETED"}]
    scan_times = [ts for ts in scan_times if ts is not None]
    entry_times = [_parse_ts(row.get("timestamp")) for row in events if row.get("event") == "ENTRY_CYCLE_COMPLETED"]
    entry_times = [ts for ts in entry_times if ts is not None]
    account_ok = bool(counts.get("ACCOUNT_FETCH_SUCCESS"))
    skipped = sorted(
        {
            str(((row.get("details") or {}).get("reason")) or "unknown")
            for row in events
            if str(row.get("event") or "") == "ENTRY_CYCLE_SKIPPED"
        }
    )
    day_start = dt.replace(hour=market_open.hour, minute=market_open.minute, second=0, microsecond=0)
    cutoff_dt = dt.replace(hour=entry_cutoff.hour, minute=entry_cutoff.minute, second=0, microsecond=0) if entry_cutoff else None
    if not progress:
        status = "INSUFFICIENT_CURRENT_DAY_DATA"
        reason = "runtime_progress_artifact_missing"
    elif not account_ok and bool(counts.get("ACCOUNT_FETCH_FAILURE")):
        status = "ACCOUNT_CONNECTIVITY_FAILURE"
        reason = "account_fetch_failed"
    elif scan_times or entry_times:
        status = "ACTIVE_VALIDATED"
        reason = "runtime_progress_observed"
    elif dt.time() < market_open:
        status = "EXPECTED_NO_ACTIVITY"
        reason = "before_market_open"
    elif cutoff_dt is not None and dt >= cutoff_dt and not scan_times and not entry_times:
        status = "EXPECTED_NO_ACTIVITY_AFTER_ENTRY_CUTOFF"
        reason = "service_started_or_observed_after_entry_cutoff"
    elif dt.time() >= time(16, 0) and not scan_times and not entry_times:
        status = "MARKET_CLOSED"
        reason = "market_closed"
    elif account_ok:
        anchor = service_start or day_start
        if dt - anchor > timedelta(minutes=float(cadence_minutes) + float(grace_minutes)):
            status = "SCANNER_STALLED"
            reason = "account_ok_no_scan_after_cadence_grace"
        else:
            status = "INSUFFICIENT_CURRENT_DAY_DATA"
            reason = "waiting_for_first_cycle"
    else:
        status = "INSUFFICIENT_CURRENT_DAY_DATA"
        reason = "no_account_or_cycle_progress"
    observed_start = service_start or (min(scan_times + entry_times) if scan_times or entry_times else None)
    minutes_observed = 0.0
    if observed_start:
        minutes_observed = max(0.0, round((dt - observed_start).total_seconds() / 60.0, 3))
    expected = 0
    if observed_start and dt > observed_start:
        expected = max(0, int((dt - observed_start).total_seconds() // max(1.0, float(cadence_minutes) * 60.0)))
    activity = SessionActivity(
        status=status,
        account_fetch_succeeded=account_ok,
        scanner_cycles_expected=expected,
        scanner_cycles_completed=int(counts.get("SCAN_CYCLE_COMPLETED") or counts.get("DYNAMIC_SCAN_COMPLETED") or 0),
        entry_cycles_expected=expected,
        entry_cycles_completed=int(counts.get("ENTRY_CYCLE_COMPLETED") or 0),
        first_cycle_timestamp=min([ts.isoformat() for ts in scan_times + entry_times], default=None),
        last_cycle_timestamp=max([ts.isoformat() for ts in scan_times + entry_times], default=None),
        skipped_cycle_reasons=skipped,
        minutes_of_observed_open_session=minutes_observed,
        shadow_intents_current_day=int(shadow_intents_current_day),
        service_start_time=service_start.isoformat() if service_start else None,
        reason=reason,
    )
    return activity.as_dict()
