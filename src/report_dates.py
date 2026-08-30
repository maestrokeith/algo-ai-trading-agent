"""Helpers for resolving latest local report dates."""

from __future__ import annotations

import re
from pathlib import Path

_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_COMPACT_SNAPSHOT_RE = re.compile(r"(\d{8})T\d{6}.*")


def _user_suffix(user_id: str) -> str:
    return str(user_id or "default").strip() or "default"


def _iso_date_from_name(path: Path) -> str | None:
    match = _ISO_DATE_RE.search(path.name)
    return match.group(1) if match else None


def _snapshot_date_from_name(path: Path) -> str | None:
    match = _COMPACT_SNAPSHOT_RE.match(path.name)
    if not match:
        return None
    raw = match.group(1)
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"


def latest_report_date(*, data_dir: Path | str, user_id: str = "default") -> str | None:
    """Return the latest YYYY-MM-DD with local daily analytics/report artifacts."""
    data = Path(data_dir)
    user = _user_suffix(user_id)
    candidates: set[str] = set()
    roots = (
        data / "trade_attribution" / "daily",
        data / "profitability_attribution" / "daily",
        data / "daily_summary",
        data / "order_history",
        data / "orders",
        data / "reports",
        data / "replay",
        data / "replay_market_session",
    )
    for root in roots:
        if not root.exists():
            continue
        for path in root.glob(f"*{user}*.json"):
            day = _iso_date_from_name(path)
            if day:
                candidates.add(day)
    return max(candidates) if candidates else None


def latest_market_session_replay_date(*, project_root: Path | str, user_id: str = "default") -> str | None:
    """Return the latest date available for market-session replay."""
    root = Path(project_root)
    user = _user_suffix(user_id)
    candidates: set[str] = set()
    history_dir = root / "data" / "dynamic_scan_history"
    if history_dir.exists():
        for path in history_dir.glob(f"*_{user}.json"):
            day = _snapshot_date_from_name(path)
            if day:
                candidates.add(day)
    replay_dir = root / "data" / "replay_market_session"
    if replay_dir.exists():
        for path in replay_dir.glob(f"*{user}.json"):
            day = _iso_date_from_name(path)
            if day:
                candidates.add(day)
    return max(candidates) if candidates else None


__all__ = ["latest_market_session_replay_date", "latest_report_date"]
