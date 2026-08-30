"""Tests for SQQQ inverse re-entry state (cooldown, reference close, daily cap)."""

from __future__ import annotations

from datetime import datetime

import pytest
import pytz

from src.inverse_reentry import (
    check_sqqq_stock_reentry_allowed,
    record_sqqq_full_exit,
    record_sqqq_initial_stock_entry,
)

ET = pytz.timezone("America/New_York")


def _bear_cfg(**overrides):
    base = {
        "sqqq_reentry": {
            "enabled": True,
            "full_exit_cooldown_minutes": 60,
            "require_qqq_close_below_exit_reference": False,
            "max_initial_stock_entries_per_et_day": None,
        }
    }
    base["sqqq_reentry"].update(overrides)
    return base


class TestCooldown:
    def test_blocks_within_cooldown(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        uid = "u1"
        t0 = ET.localize(datetime(2026, 3, 31, 10, 0, 0))
        record_sqqq_full_exit(uid, data_dir, t0, 400.0)
        t30 = ET.localize(datetime(2026, 3, 31, 10, 30, 0))
        ok, reason = check_sqqq_stock_reentry_allowed(
            _bear_cfg(full_exit_cooldown_minutes=60),
            uid,
            data_dir,
            t30,
            395.0,
            et_date_str="2026-03-31",
        )
        assert ok is False
        assert "cooldown" in (reason or "").lower()

    def test_allows_after_cooldown(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        uid = "u1"
        t0 = ET.localize(datetime(2026, 3, 31, 10, 0, 0))
        record_sqqq_full_exit(uid, data_dir, t0, 400.0)
        t70 = ET.localize(datetime(2026, 3, 31, 11, 10, 0))
        ok, reason = check_sqqq_stock_reentry_allowed(
            _bear_cfg(full_exit_cooldown_minutes=60),
            uid,
            data_dir,
            t70,
            395.0,
            et_date_str="2026-03-31",
        )
        assert ok is True
        assert reason is None


class TestFreshBreakdown:
    def test_blocks_until_qqq_below_exit_reference(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        uid = "u1"
        t0 = ET.localize(datetime(2026, 3, 31, 12, 0, 0))
        record_sqqq_full_exit(uid, data_dir, t0, 400.0)
        t_late = ET.localize(datetime(2026, 3, 31, 14, 0, 0))
        cfg = _bear_cfg(
            full_exit_cooldown_minutes=0,
            require_qqq_close_below_exit_reference=True,
        )
        ok, reason = check_sqqq_stock_reentry_allowed(
            cfg, uid, data_dir, t_late, 401.0, et_date_str="2026-03-31"
        )
        assert ok is False
        assert "reference" in (reason or "").lower() or "below" in (reason or "").lower()

        ok2, _ = check_sqqq_stock_reentry_allowed(
            cfg, uid, data_dir, t_late, 399.0, et_date_str="2026-03-31"
        )
        assert ok2 is True


class TestDailyCap:
    def test_one_initial_per_day(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        uid = "u1"
        day = "2026-03-31"
        record_sqqq_initial_stock_entry(uid, data_dir, day)
        cfg = _bear_cfg(
            full_exit_cooldown_minutes=0,
            max_initial_stock_entries_per_et_day=1,
        )
        ok, reason = check_sqqq_stock_reentry_allowed(
            cfg, uid, data_dir, ET.localize(datetime(2026, 3, 31, 15, 0)), 390.0, et_date_str=day
        )
        assert ok is False
        assert "cap" in (reason or "").lower() or "blocked" in (reason or "").lower()


class TestDisabled:
    def test_disabled_passes(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        uid = "u1"
        record_sqqq_full_exit(uid, data_dir, datetime.now(), 400.0)
        cfg = {"sqqq_reentry": {"enabled": False}}
        ok, _ = check_sqqq_stock_reentry_allowed(
            cfg, uid, data_dir, datetime.now(), 400.0, et_date_str="2026-03-31"
        )
        assert ok is True
