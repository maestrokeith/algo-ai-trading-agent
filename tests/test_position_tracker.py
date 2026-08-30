"""Tests for position_tracker — per-user scoping, CRUD, migration."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import position_tracker as pt


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def data_dir(tmp_path):
    """Return a clean temporary data directory."""
    d = tmp_path / "data"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

class TestUserPath:

    def test_default_user(self, data_dir):
        p = pt._user_path("default", data_dir)
        assert p.name == "positions_default.json"

    def test_named_user(self, data_dir):
        p = pt._user_path("alice", data_dir)
        assert p.name == "positions_alice.json"


# ---------------------------------------------------------------------------
# Legacy migration
# ---------------------------------------------------------------------------

class TestLegacyMigration:

    def test_migrates_legacy_file(self, data_dir):
        legacy = data_dir / "positions_tracked.json"
        legacy.write_text(json.dumps({"AAPL": {"qty": 10}}))

        pt._migrate_legacy(data_dir)

        target = data_dir / "positions_default.json"
        assert target.exists()
        assert json.loads(target.read_text()) == {"AAPL": {"qty": 10}}
        # Legacy renamed to .bak
        assert not legacy.exists()
        assert (data_dir / "positions_tracked.json.bak").exists()

    def test_no_migration_when_target_exists(self, data_dir):
        legacy = data_dir / "positions_tracked.json"
        legacy.write_text(json.dumps({"OLD": {}}))
        target = data_dir / "positions_default.json"
        target.write_text(json.dumps({"NEW": {}}))

        pt._migrate_legacy(data_dir)

        # Target not overwritten
        assert json.loads(target.read_text()) == {"NEW": {}}
        # Legacy not renamed
        assert legacy.exists()

    def test_no_migration_when_no_legacy(self, data_dir):
        pt._migrate_legacy(data_dir)
        assert not (data_dir / "positions_default.json").exists()


# ---------------------------------------------------------------------------
# CRUD operations with user_id
# ---------------------------------------------------------------------------

class TestLoadSave:

    def test_load_empty(self, data_dir):
        result = pt.load("alice", data_dir=data_dir)
        assert result == {}

    def test_save_and_load(self, data_dir):
        pt.save({"AAPL": {"qty": 5}}, "alice", data_dir=data_dir)
        result = pt.load("alice", data_dir=data_dir)
        assert result == {"AAPL": {"qty": 5}}

    def test_users_isolated(self, data_dir):
        pt.save({"AAPL": {"qty": 5}}, "alice", data_dir=data_dir)
        pt.save({"TSLA": {"qty": 10}}, "bob", data_dir=data_dir)

        assert "AAPL" in pt.load("alice", data_dir=data_dir)
        assert "TSLA" not in pt.load("alice", data_dir=data_dir)
        assert "TSLA" in pt.load("bob", data_dir=data_dir)
        assert "AAPL" not in pt.load("bob", data_dir=data_dir)

    def test_base_path_backward_compat(self, data_dir):
        custom = data_dir / "custom.json"
        pt.save({"SPY": {"qty": 1}}, base_path=custom)
        assert pt.load(base_path=custom) == {"SPY": {"qty": 1}}


class TestAdd:

    def test_add_creates_position(self, data_dir):
        pt.add("AAPL", 10, 150.0, 1.5, user_id="alice", data_dir=data_dir)
        data = pt.load("alice", data_dir=data_dir)
        assert "AAPL" in data
        assert data["AAPL"]["qty"] == 10
        assert data["AAPL"]["entry_price"] == 150.0
        assert data["AAPL"]["stop_pct"] == 1.5
        assert data["AAPL"]["side"] == "long"

    def test_add_uppercases_symbol(self, data_dir):
        pt.add("aapl", 5, 100.0, 1.0, user_id="alice", data_dir=data_dir)
        assert "AAPL" in pt.load("alice", data_dir=data_dir)

    def test_add_same_symbol_different_users(self, data_dir):
        pt.add("AAPL", 10, 150.0, 1.5, user_id="alice", data_dir=data_dir)
        pt.add("AAPL", 20, 160.0, 2.0, user_id="bob", data_dir=data_dir)

        alice = pt.load("alice", data_dir=data_dir)
        bob = pt.load("bob", data_dir=data_dir)
        assert alice["AAPL"]["qty"] == 10
        assert bob["AAPL"]["qty"] == 20

    def test_add_extras_persist_pyramid_fields(self, data_dir):
        pt.add(
            "SQQQ",
            10,
            15.0,
            2.0,
            user_id="alice",
            data_dir=data_dir,
            extras={"scale_count": 1, "last_entry_price": 15.0, "last_scale_ts": "2026-03-30T14:00:00"},
        )
        row = pt.load("alice", data_dir=data_dir)["SQQQ"]
        assert row["scale_count"] == 1
        assert row["last_entry_price"] == 15.0
        assert "last_scale_ts" in row


class TestAddOnPullbackOk:

    def test_disabled_when_ratio_none(self):
        assert pt.add_on_pullback_ok(200.0, 100.0, None) is True

    def test_disabled_when_ratio_one(self):
        assert pt.add_on_pullback_ok(200.0, 100.0, 1.0) is True

    def test_allows_when_price_below_threshold(self):
        assert pt.add_on_pullback_ok(97.0, 100.0, 0.98) is True

    def test_blocks_when_price_at_or_above_threshold(self):
        assert pt.add_on_pullback_ok(99.0, 100.0, 0.98) is False
        assert pt.add_on_pullback_ok(98.0, 100.0, 0.98) is False

    def test_allows_when_no_last_entry(self):
        assert pt.add_on_pullback_ok(150.0, None, 0.98) is True


class TestAddOnPullbackOrMomentumOk:
    def test_pullback_still_primary(self) -> None:
        assert pt.add_on_pullback_or_momentum_ok(
            97.0,
            100.0,
            0.98,
            allow_momentum_bypass=False,
            trend_long_ok=False,
            news_buy=False,
        )

    def test_momentum_bypass_when_trend_ok(self) -> None:
        assert pt.add_on_pullback_or_momentum_ok(
            101.0,
            100.0,
            0.98,
            allow_momentum_bypass=True,
            trend_long_ok=True,
            news_buy=False,
        )

    def test_momentum_bypass_when_news_buy(self) -> None:
        assert pt.add_on_pullback_or_momentum_ok(
            101.0,
            100.0,
            0.98,
            allow_momentum_bypass=True,
            trend_long_ok=False,
            news_buy=True,
        )

    def test_no_bypass_when_disabled_even_if_trend(self) -> None:
        assert not pt.add_on_pullback_or_momentum_ok(
            101.0,
            100.0,
            0.98,
            allow_momentum_bypass=False,
            trend_long_ok=True,
            news_buy=False,
        )


def test_tracked_row_has_open_long_qty_or_notional() -> None:
    assert pt.tracked_row_has_open_long({"qty": 3}) is True
    assert pt.tracked_row_has_open_long({"qty": 0, "notional": 1000.0}) is True
    assert pt.tracked_row_has_open_long({"qty": 0, "notional": 0}) is False
    assert pt.tracked_row_has_open_long(None) is False


class TestLastTrackerFillAgeMinutes:

    def test_age_from_latest_stamp(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        tracked = {
            "AAPL": {
                "qty": 10,
                "entry_time": "2026-01-01T10:00:00+00:00",
                "last_add_time": "2026-01-01T11:30:00+00:00",
            }
        }
        age = pt.last_tracker_fill_age_minutes("AAPL", tracked=tracked, now_dt=now)
        assert age is not None
        assert 29.0 < age < 31.0


class TestLastEntryWithin:

    def test_true_when_recent_entry_time(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        tracked = {"AAPL": {"qty": 10, "entry_time": "2026-01-01T11:30:00+00:00"}}
        assert pt.last_entry_within("AAPL", 60.0, tracked=tracked, now_dt=now) is True

    def test_true_when_notional_only_recent_entry(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        tracked = {"AAPL": {"notional": 1000.0, "entry_time": "2026-01-01T11:30:00+00:00"}}
        assert pt.last_entry_within("AAPL", 60.0, tracked=tracked, now_dt=now) is True

    def test_false_when_entry_older_than_window(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        tracked = {"AAPL": {"qty": 10, "entry_time": "2026-01-01T10:00:00+00:00"}}
        assert pt.last_entry_within("AAPL", 60.0, tracked=tracked, now_dt=now) is False

    def test_disabled_zero_window(self):
        tracked = {"AAPL": {"qty": 10, "entry_time": "2026-01-01T11:59:00+00:00"}}
        assert pt.last_entry_within("AAPL", 0.0, tracked=tracked, now_dt=datetime.now(timezone.utc)) is False

    def test_false_when_not_tracked(self):
        assert pt.last_entry_within("ZZZ", 60.0, tracked={}, now_dt=datetime.now(timezone.utc)) is False

    def test_uses_latest_of_entry_and_last_add_time(self, data_dir):
        pt.add("AAPL", 10, 100.0, 1.5, user_id="alice", data_dir=data_dir)
        pt.merge_add_shares("AAPL", 5, 110.0, user_id="alice", data_dir=data_dir)
        row = pt.load("alice", data_dir=data_dir)["AAPL"]
        assert "last_add_time" in row
        last_add = datetime.fromisoformat(str(row["last_add_time"]).replace("Z", "+00:00"))
        now = last_add + timedelta(minutes=30)
        assert pt.last_entry_within("AAPL", 60.0, tracked={"AAPL": row}, now_dt=now) is True


class TestMergeAddShares:

    def test_merge_adds_to_existing(self, data_dir):
        pt.add("AAPL", 10, 100.0, 1.5, user_id="alice", data_dir=data_dir)
        pt.merge_add_shares("AAPL", 10, 120.0, user_id="alice", data_dir=data_dir)
        data = pt.load("alice", data_dir=data_dir)
        assert data["AAPL"]["qty"] == 20
        # Weighted average: (100*10 + 120*10) / 20 = 110
        assert data["AAPL"]["entry_price"] == pytest.approx(110.0)
        assert "last_add_time" in data["AAPL"]

    def test_merge_without_extras_preserves_scale_count(self, data_dir):
        pt.add(
            "SQQQ",
            5,
            20.0,
            2.0,
            user_id="alice",
            data_dir=data_dir,
            extras={"scale_count": 2, "last_entry_price": 19.0},
        )
        pt.merge_add_shares("SQQQ", 5, 22.0, user_id="alice", data_dir=data_dir)
        row = pt.load("alice", data_dir=data_dir)["SQQQ"]
        assert row["qty"] == 10
        assert row["scale_count"] == 2
        assert row["last_entry_price"] == 19.0

    def test_merge_creates_if_not_tracked(self, data_dir):
        pt.merge_add_shares("NVDA", 5, 200.0, user_id="alice", data_dir=data_dir)
        data = pt.load("alice", data_dir=data_dir)
        assert "NVDA" in data
        assert data["NVDA"]["qty"] == 5

    def test_merge_zero_qty_noop(self, data_dir):
        pt.merge_add_shares("AAPL", 0, 100.0, user_id="alice", data_dir=data_dir)
        assert pt.load("alice", data_dir=data_dir) == {}

    def test_merge_et_trading_date_counts(self, data_dir):
        pt.add("AAPL", 10, 100.0, 1.5, user_id="alice", data_dir=data_dir)
        pt.merge_add_shares(
            "AAPL", 5, 110.0, user_id="alice", data_dir=data_dir, et_trading_date="2026-04-14"
        )
        row = pt.load("alice", data_dir=data_dir)["AAPL"]
        assert row.get("adds_et_date") == "2026-04-14"
        assert int(row.get("adds_et_date_count") or 0) == 1
        pt.merge_add_shares(
            "AAPL", 5, 112.0, user_id="alice", data_dir=data_dir, et_trading_date="2026-04-14"
        )
        row2 = pt.load("alice", data_dir=data_dir)["AAPL"]
        assert int(row2.get("adds_et_date_count") or 0) == 2


class TestUpdate:

    def test_update_fields(self, data_dir):
        pt.add("AAPL", 10, 150.0, 1.5, user_id="alice", data_dir=data_dir)
        pt.update("AAPL", qty=5, partial_taken=True, trail_high=155.0,
                  user_id="alice", data_dir=data_dir)
        data = pt.load("alice", data_dir=data_dir)
        assert data["AAPL"]["qty"] == 5
        assert data["AAPL"]["partial_taken"] is True
        assert data["AAPL"]["trail_high"] == 155.0

    def test_update_option_pnl_peak_pct(self, data_dir):
        pt.add("AAPL", 10, 150.0, 1.5, user_id="alice", data_dir=data_dir)
        pt.update("AAPL", option_pnl_peak_pct=42.5, user_id="alice", data_dir=data_dir)
        assert pt.load("alice", data_dir=data_dir)["AAPL"]["option_pnl_peak_pct"] == 42.5

    def test_update_smart_scale_out_index(self, data_dir):
        pt.add("AAPL", 10, 150.0, 1.5, user_id="alice", data_dir=data_dir)
        pt.update("AAPL", smart_scale_out_index=1, user_id="alice", data_dir=data_dir)
        assert int(pt.load("alice", data_dir=data_dir)["AAPL"]["smart_scale_out_index"]) == 1

    def test_update_smart_exit_state(self, data_dir):
        pt.add("AAPL", 10, 150.0, 1.5, user_id="alice", data_dir=data_dir)
        blob = {
            "entry_price": 150.0,
            "high_price": 155.0,
            "scaled_levels": [5.0],
            "trailing_active": True,
        }
        pt.update("AAPL", smart_exit_state=blob, user_id="alice", data_dir=data_dir)
        row = pt.load("alice", data_dir=data_dir)["AAPL"]
        assert row["smart_exit_state"] == blob

    def test_update_missing_symbol_noop(self, data_dir):
        pt.update("AAPL", qty=5, user_id="alice", data_dir=data_dir)
        assert pt.load("alice", data_dir=data_dir) == {}


class TestRemove:

    def test_remove_position(self, data_dir):
        pt.add("AAPL", 10, 150.0, 1.5, user_id="alice", data_dir=data_dir)
        pt.remove("AAPL", user_id="alice", data_dir=data_dir)
        assert "AAPL" not in pt.load("alice", data_dir=data_dir)

    def test_remove_missing_noop(self, data_dir):
        pt.remove("AAPL", user_id="alice", data_dir=data_dir)


class TestClearAll:

    def test_clears_all(self, data_dir):
        pt.add("AAPL", 10, 150.0, 1.5, user_id="alice", data_dir=data_dir)
        pt.add("TSLA", 5, 200.0, 2.0, user_id="alice", data_dir=data_dir)
        pt.clear_all(user_id="alice", data_dir=data_dir)
        assert pt.load("alice", data_dir=data_dir) == {}


class TestReconcile:

    def test_inserts_baseline_when_untracked(self, data_dir):
        pt.reconcile("MSFT", 4, 310.0, user_id="alice", data_dir=data_dir)
        row = pt.load("alice", data_dir=data_dir)["MSFT"]
        assert row["qty"] == 4
        assert row["entry_price"] == 310.0
        assert row["side"] == "long"
        assert row["scale_count"] == 1
        assert bool(row.get("entry_time_uncertain")) is True

    def test_untracked_without_price_noop(self, data_dir):
        pt.reconcile("XOM", 10, 0.0, user_id="alice", data_dir=data_dir)
        assert pt.load("alice", data_dir=data_dir) == {}

    def test_updates_qty_and_entry_from_broker(self, data_dir):
        pt.add("AAPL", 10, 150.0, 1.5, user_id="alice", data_dir=data_dir)
        pt.reconcile("AAPL", 8, 148.0, user_id="alice", data_dir=data_dir)
        row = pt.load("alice", data_dir=data_dir)["AAPL"]
        assert row["qty"] == 8
        assert row["entry_price"] == 148.0
        assert row["stop_pct"] == 1.5

    def test_preserves_entry_when_avg_missing(self, data_dir):
        pt.add("AAPL", 10, 150.0, 1.5, user_id="alice", data_dir=data_dir)
        pt.reconcile("AAPL", 7, 0.0, user_id="alice", data_dir=data_dir)
        row = pt.load("alice", data_dir=data_dir)["AAPL"]
        assert row["qty"] == 7
        assert row["entry_price"] == 150.0

    def test_short_negative_qty(self, data_dir):
        pt.reconcile("SPY", -2, 500.0, user_id="alice", data_dir=data_dir)
        row = pt.load("alice", data_dir=data_dir)["SPY"]
        assert row["qty"] == 2
        assert row["side"] == "short"

    def test_zero_qty_removes(self, data_dir):
        pt.add("AAPL", 10, 150.0, 1.5, user_id="alice", data_dir=data_dir)
        pt.reconcile("AAPL", 0, 0.0, user_id="alice", data_dir=data_dir)
        assert "AAPL" not in pt.load("alice", data_dir=data_dir)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

class TestTimeHelpers:

    def test_bars_held(self):
        two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        assert pt.bars_held(two_days_ago) == 2

    def test_bars_held_invalid_string(self):
        # Invalid string falls back to now → 0 days
        assert pt.bars_held("not-a-date") == 0

    def test_minutes_held(self):
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        result = pt.minutes_held(one_hour_ago)
        assert 59.0 <= result <= 61.0

    def test_minutes_held_empty_string(self):
        assert pt.minutes_held("") == 0.0

    def test_minutes_held_invalid(self):
        assert pt.minutes_held("garbage") == 0.0

    def test_minutes_held_z_suffix(self):
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1))
        iso = one_hour_ago.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        result = pt.minutes_held(iso)
        assert 59.0 <= result <= 61.0
