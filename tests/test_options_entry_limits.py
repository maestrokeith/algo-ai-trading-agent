"""Tests for in-process option entry caps (daily + cooldown)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.options_entry_limits import (
    option_entries_used_today,
    option_entry_allowed_by_daily_cap,
    option_entry_cooldown_blocks,
    record_option_entry,
    record_option_entry_utc,
)


def test_daily_cap_resets_per_et_day() -> None:
    record_option_entry("u1", "2026-04-01")
    record_option_entry("u1", "2026-04-01")
    assert option_entries_used_today("u1", "2026-04-01") == 2
    assert option_entries_used_today("u1", "2026-04-02") == 0


def test_daily_cap_gate() -> None:
    cfg = {"options": {"max_option_trades_per_day": 2}}
    record_option_entry("alice", "2026-04-28")
    record_option_entry("alice", "2026-04-28")
    ok, reason = option_entry_allowed_by_daily_cap(cfg, "alice", "2026-04-28")
    assert ok is False
    assert "max option entries per day" in (reason or "")


def test_entry_cooldown_blocks_within_window() -> None:
    cfg = {"options": {"cooldown_minutes_after_entry": 60}}
    now = datetime(2026, 4, 28, 15, 0, tzinfo=timezone.utc)
    record_option_entry_utc("bob", "SPY", now - timedelta(minutes=30))
    blocked, reason = option_entry_cooldown_blocks(cfg, "bob", "SPY", now)
    assert blocked is True
    assert "cooldown" in (reason or "").lower()
    later = now + timedelta(minutes=40)
    blocked2, _ = option_entry_cooldown_blocks(cfg, "bob", "SPY", later)
    assert blocked2 is False
