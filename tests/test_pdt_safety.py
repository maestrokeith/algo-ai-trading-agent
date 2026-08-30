"""Tests for same-day close deferral (PDT-style margin rules)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytz

from src.pdt_safety import (
    block_same_day_close_for_pdt,
    entry_opened_same_calendar_day_et,
)

ET = pytz.timezone("America/New_York")


def test_same_calendar_day_et() -> None:
    now = ET.localize(datetime(2026, 3, 31, 14, 0, 0))
    morning_utc = datetime(2026, 3, 31, 13, 0, 0, tzinfo=timezone.utc)
    assert entry_opened_same_calendar_day_et(morning_utc.isoformat(), now) is True
    prior = datetime(2026, 3, 30, 20, 0, 0, tzinfo=timezone.utc)
    assert entry_opened_same_calendar_day_et(prior.isoformat(), now) is False


def test_uncertain_not_same_day() -> None:
    now = ET.localize(datetime(2026, 3, 31, 14, 0, 0))
    assert entry_opened_same_calendar_day_et(now.isoformat(), now, entry_time_uncertain=True) is False


def test_block_when_below_equity_margin_same_day() -> None:
    now = ET.localize(datetime(2026, 3, 31, 15, 0, 0))
    cfg = {
        "compliance": {
            "avoid_same_day_close_below_pdt_equity": True,
            "margin_account": True,
            "pdt_min_equity": 25_000,
        }
    }
    opened = datetime(2026, 3, 31, 10, 0, 0, tzinfo=timezone.utc)
    blocked, reason = block_same_day_close_for_pdt(
        config=cfg,
        account_equity=10_000.0,
        entry_time_iso=opened.isoformat(),
        now_et=now,
    )
    assert blocked is True
    assert reason is not None and "same-day" in reason.lower()


def test_no_block_above_threshold() -> None:
    now = ET.localize(datetime(2026, 3, 31, 15, 0, 0))
    cfg = {"compliance": {"margin_account": True, "pdt_min_equity": 25_000}}
    opened = datetime(2026, 3, 31, 10, 0, 0, tzinfo=timezone.utc)
    blocked, _ = block_same_day_close_for_pdt(
        config=cfg,
        account_equity=30_000.0,
        entry_time_iso=opened.isoformat(),
        now_et=now,
    )
    assert blocked is False


def test_no_block_cash_account() -> None:
    now = ET.localize(datetime(2026, 3, 31, 15, 0, 0))
    cfg = {"compliance": {"margin_account": False, "pdt_min_equity": 25_000}}
    opened = datetime(2026, 3, 31, 10, 0, 0, tzinfo=timezone.utc)
    blocked, _ = block_same_day_close_for_pdt(
        config=cfg,
        account_equity=5_000.0,
        entry_time_iso=opened.isoformat(),
        now_et=now,
    )
    assert blocked is False


def test_feature_disabled() -> None:
    now = ET.localize(datetime(2026, 3, 31, 15, 0, 0))
    cfg = {
        "compliance": {
            "avoid_same_day_close_below_pdt_equity": False,
            "margin_account": True,
            "pdt_min_equity": 25_000,
        }
    }
    opened = datetime(2026, 3, 31, 10, 0, 0, tzinfo=timezone.utc)
    blocked, _ = block_same_day_close_for_pdt(
        config=cfg,
        account_equity=5_000.0,
        entry_time_iso=opened.isoformat(),
        now_et=now,
    )
    assert blocked is False
