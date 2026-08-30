"""Helpers for dated runtime review log paths."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")


def market_day(now: datetime | None = None) -> str:
    """Return the current New York market date as ``YYYY-MM-DD``."""
    dt = now.astimezone(MARKET_TZ) if now is not None else datetime.now(MARKET_TZ)
    return dt.date().isoformat()


def paper_review_dir(root: Path, day: str | date | None = None) -> Path:
    """Return ``data/review/<day>`` under a project root."""
    day_text = market_day() if day is None else str(day)
    return Path(root) / "data" / "review" / day_text


def paper_full_log_path(root: Path, day: str | date | None = None) -> Path:
    """Return the dated paper runtime log path."""
    return paper_review_dir(root, day) / "paper_full.log"


def ensure_paper_review_log(root: Path, day: str | date | None = None) -> Path:
    """Create the dated paper review directory and empty log file if needed."""
    path = paper_full_log_path(root, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    return path
