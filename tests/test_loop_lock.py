"""Tests for per-user trading loop file locks."""

from __future__ import annotations

import multiprocessing
import sys
import time
from pathlib import Path

import pytest

from src.loop_lock import LoopLockError, UserLoopLock


def _hold_lock(data_dir: str, ready: multiprocessing.Queue, stop: multiprocessing.Event) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    ul = UserLoopLock("testuser", Path(data_dir))
    ul.acquire()
    ready.put("ok")
    stop.wait(timeout=60)
    ul.release()


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl path tested on POSIX")
def test_second_process_cannot_acquire_same_user(tmp_path: Path) -> None:
    ready: multiprocessing.Queue = multiprocessing.Queue()
    stop = multiprocessing.Event()
    p = multiprocessing.Process(target=_hold_lock, args=(str(tmp_path), ready, stop))
    p.start()
    try:
        assert ready.get(timeout=5) == "ok"
        ul2 = UserLoopLock("testuser", tmp_path)
        with pytest.raises(LoopLockError, match="Another trading loop"):
            ul2.acquire()
    finally:
        stop.set()
        p.join(timeout=5)
        if p.is_alive():
            p.terminate()
            p.join(timeout=2)


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl path tested on POSIX")
def test_lock_released_after_other_process_exits(tmp_path: Path) -> None:
    ready: multiprocessing.Queue = multiprocessing.Queue()
    stop = multiprocessing.Event()
    p = multiprocessing.Process(target=_hold_lock, args=(str(tmp_path), ready, stop))
    p.start()
    try:
        assert ready.get(timeout=5) == "ok"
    finally:
        stop.set()
        p.join(timeout=10)
    time.sleep(0.2)
    ul = UserLoopLock("testuser", tmp_path)
    ul.acquire()
    ul.release()


def test_same_instance_double_acquire_is_idempotent(tmp_path: Path) -> None:
    ul = UserLoopLock("u", tmp_path)
    ul.acquire()
    ul.acquire()
    ul.release()
