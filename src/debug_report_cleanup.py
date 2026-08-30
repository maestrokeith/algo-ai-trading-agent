"""Retention cleanup for operator debug report artifacts."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

DEBUG_DIR_RELATIVE = Path("reports") / "debug"
LATEST_NAMES = {
    "algo_debug_latest.log",
    "algo_debug_latest.log.gz",
    "chatgpt_analysis_latest.md",
}
KNOWN_PATTERNS = (
    "algo_debug_*.log",
    "algo_debug_*.log.gz",
    "chatgpt_analysis_*.md",
)


@dataclass(frozen=True)
class CleanupEvent:
    """A single cleanup action or skip reason."""

    action: str
    path: Path
    reason: str | None = None

    def log_line(self) -> str:
        if self.action == "deleted":
            return f"CLEANUP_DELETED path={self.path}"
        reason = self.reason or "unknown"
        return f"CLEANUP_SKIPPED reason={reason} path={self.path}"


def _now_utc(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _iter_known_debug_files(debug_dir: Path) -> Iterable[Path]:
    seen: set[Path] = set()
    for pattern in KNOWN_PATTERNS:
        for path in debug_dir.glob(pattern):
            if path in seen:
                continue
            seen.add(path)
            yield path


def _latest_symlink_targets(debug_dir: Path) -> set[Path]:
    targets: set[Path] = set()
    for name in LATEST_NAMES:
        latest = debug_dir / name
        if not latest.is_symlink():
            continue
        try:
            target = latest.resolve()
            target.relative_to(debug_dir)
        except (OSError, ValueError):
            continue
        targets.add(target)
    return targets


def cleanup_debug_reports(
    project_root: Path,
    *,
    retention_days: int = 5,
    now: datetime | None = None,
    enabled: bool = True,
) -> list[CleanupEvent]:
    """Delete old known debug artifacts under ``reports/debug``.

    Safety constraints:
    - Only files directly under ``reports/debug`` are considered.
    - Only known filename families are considered.
    - ``*_latest`` names are always preserved.
    """

    root = project_root.resolve()
    debug_dir = (root / DEBUG_DIR_RELATIVE).resolve()
    events: list[CleanupEvent] = []

    if not enabled:
        event = CleanupEvent("skipped", debug_dir, "no_cleanup")
        logger.info(event.log_line())
        return [event]
    if retention_days < 0:
        event = CleanupEvent("skipped", debug_dir, "invalid_retention_days")
        logger.info(event.log_line())
        return [event]
    if not debug_dir.exists():
        event = CleanupEvent("skipped", debug_dir, "debug_dir_missing")
        logger.info(event.log_line())
        return [event]
    if not debug_dir.is_dir():
        event = CleanupEvent("skipped", debug_dir, "debug_path_not_directory")
        logger.info(event.log_line())
        return [event]
    try:
        debug_dir.relative_to(root)
    except ValueError:
        event = CleanupEvent("skipped", debug_dir, "outside_project_root")
        logger.info(event.log_line())
        return [event]

    cutoff_seconds = float(retention_days) * 86400.0
    now_utc = _now_utc(now)
    latest_targets = _latest_symlink_targets(debug_dir)
    for path in sorted(_iter_known_debug_files(debug_dir)):
        resolved = path.resolve()
        try:
            resolved.relative_to(debug_dir)
        except ValueError:
            event = CleanupEvent("skipped", path, "outside_debug_dir")
            events.append(event)
            logger.info(event.log_line())
            continue
        if path.parent.resolve() != debug_dir:
            event = CleanupEvent("skipped", path, "not_direct_child")
            events.append(event)
            logger.info(event.log_line())
            continue
        if path.name in LATEST_NAMES or "_latest." in path.name:
            event = CleanupEvent("skipped", path, "latest_artifact")
            events.append(event)
            logger.info(event.log_line())
            continue
        if path.is_symlink():
            event = CleanupEvent("skipped", path, "symlink")
            events.append(event)
            logger.info(event.log_line())
            continue
        if resolved in latest_targets:
            event = CleanupEvent("skipped", path, "latest_target")
            events.append(event)
            logger.info(event.log_line())
            continue
        if not path.is_file():
            event = CleanupEvent("skipped", path, "not_file")
            events.append(event)
            logger.info(event.log_line())
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            event = CleanupEvent("skipped", path, "stat_failed")
            events.append(event)
            logger.info(event.log_line())
            continue
        age_seconds = (now_utc - mtime).total_seconds()
        if age_seconds <= cutoff_seconds:
            event = CleanupEvent("skipped", path, "within_retention")
            events.append(event)
            logger.info(event.log_line())
            continue
        try:
            path.unlink()
        except OSError:
            event = CleanupEvent("skipped", path, "delete_failed")
        else:
            event = CleanupEvent("deleted", path)
        events.append(event)
        logger.info(event.log_line())
    return events
