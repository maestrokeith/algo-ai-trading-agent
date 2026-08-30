from __future__ import annotations

import json
from types import SimpleNamespace

from scripts import limited_live_readiness as readiness


def _config(*, mode: str = "live", pilot: bool = True, trend_state: str = "LIVE") -> dict:
    return {
        "trading_control": {
            "mode": mode,
            "strategy_states": {
                "trend_long": trend_state,
                "momentum_breakout": "SHADOW",
                "dynamic_no_catalyst": "SHADOW",
                "news_only": "DISABLED",
                "options_live": "DISABLED",
                "options_paper": "DISABLED",
            },
            "live_pilot": {
                "enabled": pilot,
                "allowed_strategies": ["trend_long"],
                "preexisting_position_allowlist": ["AMZN", "NFLX"],
                "max_trades_per_day": 1,
                "max_entry_submissions_per_day": 1,
                "max_entry_fills_per_day": 1,
                "max_open_positions": 1,
                "max_notional_per_trade": 100,
                "max_total_deployed_notional": 100,
                "max_daily_loss_usd": 25,
                "eod_flatten_required": True,
            },
        },
        "options": {"enabled": False, "live_pilot_enabled": False},
    }


class _AccountStatus:
    ACTIVE = "ACTIVE"

    def __str__(self) -> str:
        return "AccountStatus.ACTIVE"


class _Broker:
    paper = False

    def __init__(self, *, orders=(), positions=(), account_status="ACTIVE") -> None:
        self._orders = list(orders)
        self._positions = list(positions)
        self._trading = SimpleNamespace(
            get_account=lambda: SimpleNamespace(status=account_status, trading_blocked=False, account_blocked=False)
        )

    def list_orders(self, status: str = "open"):
        return list(self._orders)

    def get_positions(self):
        return list(self._positions)


class _Manager:
    def __init__(self, config: dict, broker: _Broker) -> None:
        self._config = config
        self._broker = broker

    def get_user(self, user: str):
        return SimpleNamespace(user_id=user, paper=False, config=self._config)

    def get_broker(self, user: str):
        return self._broker


def test_limited_live_readiness_ready_with_clean_mocked_state(tmp_path, monkeypatch) -> None:
    cfg = _config()
    monkeypatch.setattr(readiness, "load_config", lambda path: cfg)
    monkeypatch.setattr(readiness, "UserManager", lambda *args, **kwargs: _Manager(cfg, _Broker()))
    monkeypatch.setattr(readiness, "_git_state", lambda root: {"head": "abc", "working_tree_clean": True, "origin_sync": "0\t0", "origin_synced": True})
    monkeypatch.setattr(readiness, "build_canonical_day", lambda **kwargs: {"counts": {}})

    report = readiness.build_limited_live_readiness(root=tmp_path, user="live_bot", day="2026-07-30")

    assert report["ready"] is True
    assert report["effective_mode"] == "live"
    assert report["live_enabled_strategies"] == ["trend_long"]
    assert report["options_active"] is False
    assert report["max_notional"] == 100
    assert report["daily_loss_cap"] == 25
    assert report["account_status"] == "ACTIVE"
    assert report["submissions_today"] == 0
    assert report["broker_dispatch_attempts_today"] == 0
    assert report["false_or_stale_lock_detected"] is False


def test_limited_live_readiness_reports_broker_reconciled_lifecycle_and_keeps_lock(tmp_path, monkeypatch) -> None:
    cfg = _config()
    state_path = tmp_path / "data" / "limited_live_pilot" / "2026-08-05_live_bot.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "trading_date": "2026-08-05",
                "broker_dispatch_attempts": 1,
                "entry_submissions": 1,
                "entry_locked": True,
                "entry_lock_reason": "broker_dispatch_attempt_reserved",
                "submitted_symbols": ["IWM"],
            }
        ),
        encoding="utf-8",
    )
    counts = {
        "submitted_orders": 1,
        "broker_accepted_orders": 1,
        "broker_accepted_orders_local": 0,
        "broker_accepted_orders_reconciled": 1,
        "completed_fills": 1,
        "raw_local_fill_events": 0,
        "broker_reconciled_fill_events": 1,
        "opened_positions": 1,
        "local_positions_today": 0,
        "broker_reconciled_positions_today": 1,
        "recovered_broker_order_snapshots": 1,
        "recovered_broker_fill_events": 1,
    }
    monkeypatch.setattr(readiness, "load_config", lambda path: cfg)
    monkeypatch.setattr(
        readiness,
        "UserManager",
        lambda *args, **kwargs: _Manager(
            cfg,
            _Broker(positions=[SimpleNamespace(symbol="IWM", qty="0.330702357", market_value="99.42")]),
        ),
    )
    monkeypatch.setattr(readiness, "_git_state", lambda root: {"head": "abc", "working_tree_clean": True, "origin_sync": "0\t0", "origin_synced": True})
    monkeypatch.setattr(readiness, "build_canonical_day", lambda **kwargs: {"counts": counts})

    report = readiness.build_limited_live_readiness(root=tmp_path, user="live_bot", day="2026-08-05")

    assert report["ready"] is False
    assert report["submissions_today"] == 1
    assert report["accepted_entry_orders_today"] == 1
    assert report["fills_today"] == 1
    assert report["positions_today"] == 1
    assert report["broker_accepted_orders_reconciled"] == 1
    assert report["broker_reconciled_fill_events"] == 1
    assert report["broker_reconciled_positions_today"] == 1
    assert report["broker_recovery_performed"] is True
    assert "pilot_submission_already_used" in report["blocking_reasons"]


def test_limited_live_readiness_uses_prior_pilot_symbol_for_open_position_lineage(tmp_path, monkeypatch) -> None:
    cfg = _config()
    state_path = tmp_path / "data" / "limited_live_pilot" / "2026-08-05_live_bot.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "trading_date": "2026-08-05",
                "broker_dispatch_attempted": True,
                "broker_dispatch_attempts": 1,
                "consumed_submission": True,
                "submitted_symbols": ["IWM"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(readiness, "load_config", lambda path: cfg)
    monkeypatch.setattr(
        readiness,
        "UserManager",
        lambda *args, **kwargs: _Manager(
            cfg,
            _Broker(positions=[SimpleNamespace(symbol="IWM", qty="0.330702357", market_value="99.42")]),
        ),
    )
    monkeypatch.setattr(readiness, "_git_state", lambda root: {"head": "abc", "working_tree_clean": True, "origin_sync": "0\t0", "origin_synced": True})
    monkeypatch.setattr(readiness, "build_canonical_day", lambda **kwargs: {"counts": {}})

    report = readiness.build_limited_live_readiness(root=tmp_path, user="live_bot", day="2026-08-06")

    assert report["pilot_managed_positions"] == 1
    assert report["unknown_positions"] == 0
    assert report["positions_today"] == 1
    assert report["historical_pilot_symbols"] == ["IWM"]
    assert report["entry_lock_state"] is True
    assert report["entry_lock_reason"] == "pilot_open_position_present"


def test_limited_live_readiness_reclassifies_legacy_intent_only_lock(tmp_path, monkeypatch) -> None:
    cfg = _config()
    state_path = tmp_path / "data" / "limited_live_pilot" / "2026-08-03_live_bot.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "entry_submissions": 1,
                "entry_locked": True,
                "lock_reasons": ["first_submission_reserved"],
                "accepted_entry_orders": 0,
                "entry_fills": 0,
                "open_positions": 0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(readiness, "load_config", lambda path: cfg)
    monkeypatch.setattr(readiness, "UserManager", lambda *args, **kwargs: _Manager(cfg, _Broker()))
    monkeypatch.setattr(readiness, "_git_state", lambda root: {"head": "abc", "working_tree_clean": True, "origin_sync": "0\t0", "origin_synced": True})
    monkeypatch.setattr(readiness, "build_canonical_day", lambda **kwargs: {"counts": {}})

    report = readiness.build_limited_live_readiness(root=tmp_path, user="live_bot", day="2026-08-03")

    assert report["ready"] is True
    assert report["submissions_today"] == 0
    assert report["broker_dispatch_attempts_today"] == 0
    assert report["false_or_stale_lock_detected"] is True
    assert report["ambiguous_legacy_reservations"] == 0
    assert "false_or_stale_pilot_lock_detected" not in report["blocking_reasons"]


def test_limited_live_readiness_next_day_does_not_inherit_stale_lock(tmp_path, monkeypatch) -> None:
    cfg = _config()
    stale_path = tmp_path / "data" / "limited_live_pilot" / "2026-08-03_live_bot.json"
    stale_path.parent.mkdir(parents=True)
    stale_path.write_text(json.dumps({"entry_submissions": 1, "entry_locked": True, "lock_reasons": ["first_submission_reserved"]}), encoding="utf-8")
    monkeypatch.setattr(readiness, "load_config", lambda path: cfg)
    monkeypatch.setattr(readiness, "UserManager", lambda *args, **kwargs: _Manager(cfg, _Broker()))
    monkeypatch.setattr(readiness, "_git_state", lambda root: {"head": "abc", "working_tree_clean": True, "origin_sync": "0\t0", "origin_synced": True})
    monkeypatch.setattr(readiness, "build_canonical_day", lambda **kwargs: {"counts": {}})

    report = readiness.build_limited_live_readiness(root=tmp_path, user="live_bot", day="2026-08-04")

    assert report["ready"] is True
    assert report["submissions_today"] == 0
    assert report["false_or_stale_lock_detected"] is False


def test_prior_legacy_active_reservation_is_visible_but_not_current_day_blocker(tmp_path, monkeypatch) -> None:
    cfg = _config()
    state_path = tmp_path / "data" / "limited_live_pilot" / "2026-08-04_live_bot.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "entry_submissions": 1,
                "broker_dispatch_attempts": 1,
                "active_submission_reservations": 1,
                "entry_locked": True,
                "entry_lock_reason": "broker_dispatch_attempt_reserved",
                "lock_reasons": ["broker_dispatch_attempt_reserved"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(readiness, "load_config", lambda path: cfg)
    monkeypatch.setattr(readiness, "UserManager", lambda *args, **kwargs: _Manager(cfg, _Broker()))
    monkeypatch.setattr(readiness, "_git_state", lambda root: {"head": "abc", "working_tree_clean": True, "origin_sync": "0\t0", "origin_synced": True})
    monkeypatch.setattr(readiness, "build_canonical_day", lambda **kwargs: {"counts": {}})

    report = readiness.build_limited_live_readiness(root=tmp_path, user="live_bot", day="2026-08-04")

    assert report["ready"] is True
    assert report["current_day_active_reservations"] == 0
    assert report["current_day_dispatch_attempts"] == 0
    assert report["current_day_submissions"] == 0
    assert report["entry_lock_state"] is False
    assert report["stale_prior_day_reservations"] == 1
    assert report["ambiguous_legacy_reservations"] == 1
    assert report["false_or_stale_lock_detected"] is True
    assert "pilot_submission_already_used" not in report["blocking_reasons"]
    assert "active_submission_reservation_present" not in report["blocking_reasons"]


def test_current_day_schema_active_reservation_still_blocks(tmp_path, monkeypatch) -> None:
    cfg = _config()
    state_path = tmp_path / "data" / "limited_live_pilot" / "2026-08-04_live_bot.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "user_id": "live_bot",
                "environment": "live",
                "trading_date": "2026-08-04",
                "entry_submissions": 1,
                "broker_dispatch_attempts": 1,
                "active_submission_reservations": 1,
                "entry_locked": True,
                "entry_lock_reason": "broker_dispatch_attempt_reserved",
                "lock_reasons": ["broker_dispatch_attempt_reserved"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(readiness, "load_config", lambda path: cfg)
    monkeypatch.setattr(readiness, "UserManager", lambda *args, **kwargs: _Manager(cfg, _Broker()))
    monkeypatch.setattr(readiness, "_git_state", lambda root: {"head": "abc", "working_tree_clean": True, "origin_sync": "0\t0", "origin_synced": True})
    monkeypatch.setattr(readiness, "build_canonical_day", lambda **kwargs: {"counts": {}})

    report = readiness.build_limited_live_readiness(root=tmp_path, user="live_bot", day="2026-08-04")

    assert report["ready"] is False
    assert report["current_day_active_reservations"] == 1
    assert report["current_day_dispatch_attempts"] == 1
    assert "active_submission_reservation_present" in report["blocking_reasons"]
    assert "pilot_submission_already_used" in report["blocking_reasons"]


def test_august_4_shadow_readiness_has_only_activation_blockers_with_legacy_state(tmp_path, monkeypatch) -> None:
    cfg = _config(mode="shadow", pilot=False, trend_state="SHADOW")
    state_path = tmp_path / "data" / "limited_live_pilot" / "2026-08-04_live_bot.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"entry_submissions": 1, "broker_dispatch_attempts": 1, "active_submission_reservations": 1, "entry_locked": True}), encoding="utf-8")
    monkeypatch.setattr(readiness, "load_config", lambda path: cfg)
    monkeypatch.setattr(readiness, "UserManager", lambda *args, **kwargs: _Manager(cfg, _Broker()))
    monkeypatch.setattr(readiness, "_git_state", lambda root: {"head": "abc", "working_tree_clean": True, "origin_sync": "0\t0", "origin_synced": True})
    monkeypatch.setattr(readiness, "build_canonical_day", lambda **kwargs: {"counts": {}})

    report = readiness.build_limited_live_readiness(root=tmp_path, user="live_bot", day="2026-08-04")

    assert report["current_day_active_reservations"] == 0
    assert report["current_day_dispatch_attempts"] == 0
    assert report["current_day_submissions"] == 0
    assert report["blocking_reasons"] == [
        "live_pilot_disabled",
        "live_strategy_set_not_trend_long_only",
        "mode_not_live:shadow",
    ]


def test_limited_live_readiness_blocks_dirty_origin_and_unknown_position(tmp_path, monkeypatch) -> None:
    cfg = _config()
    monkeypatch.setattr(readiness, "load_config", lambda path: cfg)
    monkeypatch.setattr(readiness, "UserManager", lambda *args, **kwargs: _Manager(cfg, _Broker(positions=[{"symbol": "AAPL"}])))
    monkeypatch.setattr(readiness, "_git_state", lambda root: {"head": "abc", "working_tree_clean": False, "origin_sync": "0\t1", "origin_synced": False})
    monkeypatch.setattr(readiness, "build_canonical_day", lambda **kwargs: {"counts": {}})

    report = readiness.build_limited_live_readiness(root=tmp_path, user="live_bot", day="2026-07-30")

    assert report["ready"] is False
    assert "dirty_working_tree" in report["blocking_reasons"]
    assert "origin_not_synced:0\t1" in report["blocking_reasons"]
    assert "unknown_positions" in report["blocking_reasons"]


def test_account_status_normalizes_enum_and_strings(tmp_path, monkeypatch) -> None:
    cfg = _config()
    statuses = [_AccountStatus(), "ACTIVE", "active"]
    for status in statuses:
        monkeypatch.setattr(readiness, "load_config", lambda path, cfg=cfg: cfg)
        monkeypatch.setattr(readiness, "UserManager", lambda *args, status=status, **kwargs: _Manager(cfg, _Broker(account_status=status)))
        monkeypatch.setattr(readiness, "_git_state", lambda root: {"head": "abc", "working_tree_clean": True, "origin_sync": "0\t0", "origin_synced": True})
        monkeypatch.setattr(readiness, "build_canonical_day", lambda **kwargs: {"counts": {}})

        report = readiness.build_limited_live_readiness(root=tmp_path, user="live_bot", day="2026-07-31")

        assert report["account_status"] == "ACTIVE"
        assert "account_not_active" not in report["blocking_reasons"]


def test_allowlisted_positions_do_not_block_readiness_or_pilot_caps(tmp_path, monkeypatch) -> None:
    cfg = _config()
    positions = [
        SimpleNamespace(symbol="AMZN", qty="0.927483266", market_value="180.00"),
        SimpleNamespace(symbol="NFLX", qty="0.984126326", market_value="210.00"),
    ]
    monkeypatch.setattr(readiness, "load_config", lambda path: cfg)
    monkeypatch.setattr(readiness, "UserManager", lambda *args, **kwargs: _Manager(cfg, _Broker(positions=positions)))
    monkeypatch.setattr(readiness, "_git_state", lambda root: {"head": "abc", "working_tree_clean": True, "origin_sync": "0\t0", "origin_synced": True})
    monkeypatch.setattr(readiness, "build_canonical_day", lambda **kwargs: {"counts": {}})

    report = readiness.build_limited_live_readiness(root=tmp_path, user="live_bot", day="2026-07-31")

    assert report["ready"] is True
    assert report["broker_positions_total"] == 2
    assert report["preexisting_allowed_positions"] == 2
    assert report["pilot_managed_positions"] == 0
    assert report["unknown_positions"] == 0
    assert report["preexisting_allowed_notional"] == 390.0
    assert report["pilot_deployed_notional"] == 0.0
    assert report["local_broker_reconciliation"] == "clean"
    assert "broker_positions_present" not in report["blocking_reasons"]
    assert "unknown_positions" not in report["blocking_reasons"]


def test_open_order_on_allowlisted_symbol_still_blocks_readiness(tmp_path, monkeypatch) -> None:
    cfg = _config()
    monkeypatch.setattr(readiness, "load_config", lambda path: cfg)
    monkeypatch.setattr(
        readiness,
        "UserManager",
        lambda *args, **kwargs: _Manager(
            cfg,
            _Broker(
                orders=[SimpleNamespace(symbol="AMZN", id="manual-order")],
                positions=[SimpleNamespace(symbol="AMZN", market_value="180.00")],
            ),
        ),
    )
    monkeypatch.setattr(readiness, "_git_state", lambda root: {"head": "abc", "working_tree_clean": True, "origin_sync": "0\t0", "origin_synced": True})
    monkeypatch.setattr(readiness, "build_canonical_day", lambda **kwargs: {"counts": {}})

    report = readiness.build_limited_live_readiness(root=tmp_path, user="live_bot", day="2026-07-31")

    assert report["ready"] is False
    assert "open_broker_orders_present" in report["blocking_reasons"]


def test_limited_live_readiness_blocks_shadow_or_options(tmp_path, monkeypatch) -> None:
    cfg = _config(mode="shadow", pilot=False)
    cfg["options"]["enabled"] = True
    monkeypatch.setattr(readiness, "load_config", lambda path: cfg)
    monkeypatch.setattr(readiness, "UserManager", lambda *args, **kwargs: _Manager(cfg, _Broker()))
    monkeypatch.setattr(readiness, "_git_state", lambda root: {"head": "abc", "working_tree_clean": True, "origin_sync": "0\t0", "origin_synced": True})
    monkeypatch.setattr(readiness, "build_canonical_day", lambda **kwargs: {"counts": {}})

    report = readiness.build_limited_live_readiness(root=tmp_path, user="live_bot", day="2026-07-30")

    assert report["ready"] is False
    assert "mode_not_live:shadow" in report["blocking_reasons"]
    assert "live_pilot_disabled" in report["blocking_reasons"]
    assert "options_active" in report["blocking_reasons"]


def test_limited_live_readiness_blocks_missing_exit_registration_for_pilot_position(tmp_path, monkeypatch) -> None:
    cfg = _config()
    state_path = tmp_path / "data" / "limited_live_pilot" / "2026-08-05_live_bot.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"trading_date": "2026-08-05", "broker_dispatch_attempts": 1, "submitted_symbols": ["IWM"]}),
        encoding="utf-8",
    )
    broker = _Broker(positions=[{"symbol": "IWM", "qty": "0.330702357", "market_value": "99"}])
    monkeypatch.setattr(readiness, "load_config", lambda path: cfg)
    monkeypatch.setattr(readiness, "UserManager", lambda *args, **kwargs: _Manager(cfg, broker))
    monkeypatch.setattr(readiness, "_git_state", lambda root: {"head": "abc", "working_tree_clean": True, "origin_sync": "0\t0", "origin_synced": True})
    monkeypatch.setattr(readiness, "build_canonical_day", lambda **kwargs: {"counts": {}})

    report = readiness.build_limited_live_readiness(root=tmp_path, user="live_bot", day="2026-08-10")

    assert report["managed_positions_missing_exit_registration"] == ["IWM"]
    assert report["exit_manager_healthy"] is False
    assert "pilot_position_exit_management_stale:IWM" in report["blocking_reasons"]


def test_limited_live_readiness_clears_exit_registration_block_after_eval(tmp_path, monkeypatch) -> None:
    cfg = _config()
    state_path = tmp_path / "data" / "limited_live_pilot" / "2026-08-05_live_bot.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"trading_date": "2026-08-05", "broker_dispatch_attempts": 1, "submitted_symbols": ["IWM"]}),
        encoding="utf-8",
    )
    status_path = tmp_path / "data" / "position_management" / "2026-08-10_live_bot.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(
        json.dumps({"positions": {"IWM": {"last_exit_eval_at": "2026-08-10T14:00:00+00:00", "eod_flatten_registration": True}}}),
        encoding="utf-8",
    )
    broker = _Broker(positions=[{"symbol": "IWM", "qty": "0.330702357", "market_value": "99"}])
    monkeypatch.setattr(readiness, "load_config", lambda path: cfg)
    monkeypatch.setattr(readiness, "UserManager", lambda *args, **kwargs: _Manager(cfg, broker))
    monkeypatch.setattr(readiness, "_git_state", lambda root: {"head": "abc", "working_tree_clean": True, "origin_sync": "0\t0", "origin_synced": True})
    monkeypatch.setattr(readiness, "build_canonical_day", lambda **kwargs: {"counts": {}})

    report = readiness.build_limited_live_readiness(root=tmp_path, user="live_bot", day="2026-08-10")

    assert report["managed_positions_registered_for_exit"] == ["IWM"]
    assert report["managed_positions_missing_exit_registration"] == []
    assert report["exit_manager_healthy"] is True
    assert "pilot_position_exit_management_stale:IWM" not in report["blocking_reasons"]
