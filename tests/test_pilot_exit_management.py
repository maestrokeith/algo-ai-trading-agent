from __future__ import annotations

import json
import logging
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.pilot_exit_management import (
    broker_pilot_position_report,
    evaluate_pilot_position,
    load_exit_status,
    position_management_status_report,
)


def _config() -> dict:
    return {
        "trading_control": {
            "mode": "live",
            "live_pilot": {
                "enabled": True,
                "preexisting_position_allowlist": ["AMZN", "NFLX"],
                "max_notional_per_trade": 100,
                "max_total_deployed_notional": 100,
                "eod_flatten_required": True,
            },
        },
        "options": {"enabled": False, "live_pilot_enabled": False},
    }


class _Quote:
    bid = 299.9
    ask = 300.1
    spread_pct = 0.1
    skip_spread_check = False

    def reference_mid(self, fallback: float) -> float:
        return 300.0


class _Broker:
    def __init__(self) -> None:
        self.closed: list[str] = []

    def get_latest_quote(self, symbol: str):
        return _Quote()

    def close_position(self, symbol: str):
        self.closed.append(symbol)
        return SimpleNamespace(id=f"close-{symbol}")


class _Strategy:
    stop_loss_pct = 2.8
    take_profit_pct = 3.0
    use_trailing_stop = True
    trailing_stop_pct = 1.0


class _Engine:
    strategy = _Strategy()

    def check_exit(self, *args, **kwargs):
        return None


def _ctx(tmp_path):
    return SimpleNamespace(
        user_id="live_bot",
        data_dir=tmp_path,
        now=datetime(2026, 8, 10, 10, 0, tzinfo=ZoneInfo("America/New_York")),
        broker=_Broker(),
        engine=_Engine(),
        config=_config(),
        skip_exit_for_action_cap=lambda symbol, reason: False,
        same_day_close_blocked=lambda symbol, pos: False,
        record_exit_action=lambda symbol: None,
        note_daily_risk_order=lambda symbol, side, full_exit=False: None,
        log_sell_event=lambda symbol, reason, extra=None: None,
    )


def _write_iwm_lineage(tmp_path):
    path = tmp_path / "trade_attribution" / "daily" / "2026-08-05_live_bot.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "orders": [
                    {
                        "symbol": "IWM",
                        "action": "buy",
                        "status": "filled",
                        "broker_order_id": "iwm-order",
                        "filled_qty": 0.330702357,
                        "filled_avg_price": 302.296,
                        "filled_at": "2026-08-05T13:52:28+00:00",
                        "route": "trend_long",
                        "source": "trend_long",
                        "strategy": "trend_long",
                        "event_origin": "broker_reconciliation",
                        "recovered": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    state = tmp_path / "limited_live_pilot" / "2026-08-05_live_bot.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps({"trading_date": "2026-08-05", "broker_dispatch_attempts": 1, "submitted_symbols": ["IWM"]}),
        encoding="utf-8",
    )


def test_pilot_managed_broker_position_is_loaded_and_evaluated(tmp_path, caplog):
    _write_iwm_lineage(tmp_path)
    ctx = _ctx(tmp_path)
    caplog.set_level(logging.INFO)

    rec = evaluate_pilot_position(
        ctx,
        {"symbol": "IWM", "qty": "0.330702357", "market_value": "99.0", "avg_entry_price": "302.296"},
        classification="PILOT_MANAGED",
    )

    assert rec["symbol"] == "IWM"
    assert rec["exit_manager_healthy"] is True
    assert rec["eod_flatten_registration"] is True
    assert "POSITION_MANAGER_POSITION_LOADED user_id=live_bot symbol=IWM classification=PILOT_MANAGED" in caplog.text
    assert "STOP_EVALUATED user_id=live_bot symbol=IWM" in caplog.text
    assert "TRAIL_EVALUATED user_id=live_bot symbol=IWM" in caplog.text
    assert "TAKE_PROFIT_EVALUATED user_id=live_bot symbol=IWM" in caplog.text
    assert ctx.broker.closed == []
    status = load_exit_status(tmp_path, "live_bot", "2026-08-10")
    assert status["positions"]["IWM"]["last_exit_eval_at"]


def test_preexisting_positions_are_not_pilot_managed(tmp_path):
    _write_iwm_lineage(tmp_path)
    positions = [
        {"symbol": "AMZN", "qty": "1", "market_value": "250"},
        {"symbol": "IWM", "qty": "0.330702357", "market_value": "99"},
        {"symbol": "NFLX", "qty": "0.1", "market_value": "75"},
    ]

    report = broker_pilot_position_report(
        config=_config(),
        positions=positions,
        data_dir=tmp_path,
        user_id="live_bot",
        day="2026-08-10",
    )

    classes = {row["symbol"]: row["classification"] for row in report["position_classifications"]}
    assert classes == {"AMZN": "PREEXISTING_ALLOWED", "IWM": "PILOT_MANAGED", "NFLX": "PREEXISTING_ALLOWED"}


def test_position_management_status_reports_missing_then_registered(tmp_path):
    _write_iwm_lineage(tmp_path)
    positions = [{"symbol": "IWM", "qty": "0.330702357", "market_value": "99"}]

    before = position_management_status_report(
        config=_config(),
        data_dir=tmp_path,
        user_id="live_bot",
        day="2026-08-10",
        positions=positions,
    )
    assert before["managed_positions_missing_exit_registration"] == ["IWM"]
    assert before["exit_manager_healthy"] is False

    evaluate_pilot_position(_ctx(tmp_path), positions[0], classification="PILOT_MANAGED")
    after = position_management_status_report(
        config=_config(),
        data_dir=tmp_path,
        user_id="live_bot",
        day="2026-08-10",
        positions=positions,
    )
    assert after["managed_positions_registered_for_exit"] == ["IWM"]
    assert after["managed_positions_missing_exit_registration"] == []
    assert after["exit_manager_healthy"] is True
