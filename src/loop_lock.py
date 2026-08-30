"""Exclusive per-user lock for the Alpaca trading loop.

Prevents two ``run_alpaca_loop`` processes from trading the same ``user_id``
(double entries, conflicting exits). Uses ``fcntl.flock`` on macOS/Linux; a
PID file with ``O_EXCL`` on Windows.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)


class _LoopContext(Protocol):
    user_id: str
    data_dir: Path | None


class LoopLockError(RuntimeError):
    """Another process holds the loop lock for this user."""


def _slug(user_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(user_id).strip())
    return (safe or "user")[:200]


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _acquire_fcntl(path: Path, user_id: str) -> int:
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as e:
        os.close(fd)
        raise LoopLockError(
            f"Another trading loop is already running for user_id={user_id!r} "
            f"(lock: {path}). Stop the other process, or run with --no-lock "
            f"(not recommended — can duplicate orders)."
        ) from e
    body = (
        f"pid={os.getpid()}\n"
        f"started_utc={datetime.now(timezone.utc).isoformat()}\n"
    ).encode()
    os.ftruncate(fd, 0)
    os.write(fd, body)
    os.fsync(fd)
    return fd


def _acquire_pidfile(path: Path, user_id: str) -> int:
    """Windows-style exclusive lock file (no fcntl flock)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(8):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            body = (
                f"pid={os.getpid()}\n"
                f"started_utc={datetime.now(timezone.utc).isoformat()}\n"
            ).encode()
            os.write(fd, body)
            os.fsync(fd)
            return fd
        except FileExistsError:
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                raw = ""
            old_pid = None
            for line in raw.splitlines():
                if line.startswith("pid="):
                    try:
                        old_pid = int(line.split("=", 1)[1].strip())
                    except ValueError:
                        pass
                    break
            if old_pid is not None and not _pid_alive(old_pid):
                try:
                    path.unlink(missing_ok=True)  # type: ignore[arg-type]
                except OSError:
                    pass
                continue
            raise LoopLockError(
                f"Another trading loop is already running for user_id={user_id!r} "
                f"(lock: {path}, pid={old_pid}). Stop it or use --no-lock."
            )
    raise LoopLockError(f"Could not acquire loop lock for user_id={user_id!r} at {path}")


def _release_fcntl(fd: int) -> None:
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def _release_pidfile(fd: int, path: Path) -> None:
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        path.unlink(missing_ok=True)  # type: ignore[arg-type]
    except OSError:
        pass


@dataclass
class UserLoopLock:
    """Holds one user's loop lock until :meth:`release`."""

    user_id: str
    data_dir: Path
    _fd: int | None = field(default=None, repr=False)
    _path: Path | None = field(default=None, repr=False)
    _use_fcntl: bool = field(default=False, repr=False)

    def acquire(self) -> None:
        if self._fd is not None:
            return
        locks_dir = Path(self.data_dir).resolve() / "locks"
        path = locks_dir / f"alpaca_loop_{_slug(self.user_id)}.lock"
        self._path = path
        if sys.platform == "win32":
            self._fd = _acquire_pidfile(path, self.user_id)
            self._use_fcntl = False
        else:
            self._fd = _acquire_fcntl(path, self.user_id)
            self._use_fcntl = True
        log.info("[%s] Loop lock acquired (%s)", self.user_id, path)

    def release(self) -> None:
        if self._fd is None:
            return
        fd, path, use_fc = self._fd, self._path, self._use_fcntl
        self._fd = None
        self._path = None
        if use_fc:
            _release_fcntl(fd)
        else:
            assert path is not None
            _release_pidfile(fd, path)
        log.info("[%s] Loop lock released", self.user_id)


def acquire_user_loop_locks(
    user_contexts: list[_LoopContext],
    *,
    enabled: bool = True,
) -> list[UserLoopLock]:
    """Acquire a lock for each context's ``user_id`` (order preserved).

    On failure, releases any locks already taken in this call.
    """
    if not enabled:
        return []
    locks: list[UserLoopLock] = []
    try:
        for ctx in user_contexts:
            data_dir = ctx.data_dir if ctx.data_dir is not None else Path(".")
            lock = UserLoopLock(ctx.user_id, Path(data_dir))
            lock.acquire()
            locks.append(lock)
        return locks
    except LoopLockError:
        for lk in reversed(locks):
            lk.release()
        raise
