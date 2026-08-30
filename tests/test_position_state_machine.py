"""Tests for :mod:`src.position_state_machine`."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytz

from src.position_state_machine import (
    HOLD,
    TRIMMED,
    blocks_discretionary_stock_exit,
    exit_reason_is_stop_like,
    load_machine,
    record_buy_after_tracker_write,
    record_sell_after_exit,
    save_machine,
)
from src.strategy import ExitReason


@pytest.fixture
def cfg_on(tmp_path: Path) -> dict:
    return {
        "position_states": {
            "enabled": True,
            "hold_after_buy_minutes": 60,
            "cooldown_after_sell_minutes": 30,
        }
    }


def test_blocks_hold_after_buy(tmp_path: Path, cfg_on: dict) -> None:
    et = pytz.timezone("America/New_York")
    t0 = et.localize(datetime(2026, 4, 1, 10, 0, 0))
    record_buy_after_tracker_write("SPY", "u1", tmp_path, t0, cfg_on)
    t1 = et.localize(datetime(2026, 4, 1, 10, 15, 0))
    blocked, reason = blocks_discretionary_stock_exit("SPY", "u1", tmp_path, t1, cfg_on)
    assert blocked is True
    assert reason and "HOLD" in reason
    t2 = et.localize(datetime(2026, 4, 1, 11, 5, 0))
    blocked2, _ = blocks_discretionary_stock_exit("SPY", "u1", tmp_path, t2, cfg_on)
    assert blocked2 is False


def test_cooldown_after_discretionary_sell_partial(tmp_path: Path, cfg_on: dict) -> None:
    et = pytz.timezone("America/New_York")
    t0 = et.localize(datetime(2026, 4, 1, 12, 0, 0))
    record_sell_after_exit(
        "QQQ",
        "u1",
        tmp_path,
        t0,
        ExitReason.OVERWEIGHT_TRIM,
        remaining_qty_after=5,
        config=cfg_on,
    )
    t1 = et.localize(datetime(2026, 4, 1, 12, 10, 0))
    blocked, reason = blocks_discretionary_stock_exit("QQQ", "u1", tmp_path, t1, cfg_on)
    assert blocked is True
    assert "TRIMMED" in (reason or "").upper()


def test_stop_like_reason_constant() -> None:
    assert exit_reason_is_stop_like(ExitReason.STOP_LOSS) is True
    assert exit_reason_is_stop_like(ExitReason.TRAILING_STOP) is True
    assert exit_reason_is_stop_like(ExitReason.TAKE_PROFIT) is False


def test_prune_expired_row(tmp_path: Path, cfg_on: dict) -> None:
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    save_machine(
        {"ZZZ": {"state": HOLD, "until": past, "flat": False}},
        "u2",
        data_dir=tmp_path,
    )
    now = datetime.now(timezone.utc)
    blocked, _ = blocks_discretionary_stock_exit("ZZZ", "u2", tmp_path, now, cfg_on)
    assert blocked is False
    assert load_machine("u2", data_dir=tmp_path) == {}
