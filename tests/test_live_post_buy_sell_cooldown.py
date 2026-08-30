"""``execution.no_sell_within_min_of_buy`` + :class:`src.live.exits.LiveExitContext`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.live.exits import LiveExitContext


def _ctx(
    now: datetime,
    *,
    nsb: float = 30.0,
) -> LiveExitContext:
    return LiveExitContext(
        user_id="t",
        data_dir=Path("/tmp"),
        now=now,
        verbose=False,
        broker=MagicMock(),
        engine=MagicMock(),
        config={"execution": {"no_sell_within_min_of_buy": nsb}},
        account_equity=100_000.0,
        symbols=["X"],
        news_enabled=False,
        news_pipeline=None,
        news_rules=None,
    )


def test_post_buy_sell_cooldown_off_when_unset() -> None:
    c = _ctx(datetime.now(timezone.utc), nsb=0.0)
    b, r = c.post_buy_sell_cooldown_active(
        "X",
        {"qty": 10, "side": "long", "entry_time": "2020-01-01T00:00:00+00:00", "entry_price": 100.0},
    )
    assert b is False
    assert r is None


def test_post_buy_sell_cooldown_active_recent_entry() -> None:
    now = datetime(2026, 1, 15, 16, 0, tzinfo=timezone.utc)
    ent = (now - timedelta(minutes=10)).isoformat()
    c = _ctx(now, nsb=30.0)
    b, r = c.post_buy_sell_cooldown_active(
        "X",
        {"qty": 10, "side": "long", "entry_time": ent, "entry_price": 100.0},
    )
    assert b is True
    assert r and "30" in r


def test_post_buy_sell_cooldown_inactive_after_window() -> None:
    now = datetime(2026, 1, 15, 16, 0, tzinfo=timezone.utc)
    ent = (now - timedelta(minutes=45)).isoformat()
    c = _ctx(now, nsb=30.0)
    b, r = c.post_buy_sell_cooldown_active(
        "X",
        {"qty": 10, "side": "long", "entry_time": ent, "entry_price": 100.0},
    )
    assert b is False
    assert r is None
