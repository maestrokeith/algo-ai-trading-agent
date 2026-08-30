"""Tests for persistent option-position management."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.entry_router import EntryRouteSignal, route_to_options_executor
from src.options_position_manager import record_option_exit, sync_options_positions


class _Broker:
    def __init__(self, positions, *, equity=100_000.0, quotes=None):
        self._positions = list(positions)
        self._equity = equity
        self._quotes = quotes or {}

    def get_positions(self):
        return list(self._positions)

    def get_equity(self):
        return self._equity

    def get_option_latest_quote(self, symbol):
        return self._quotes.get(symbol)


def _config() -> dict:
    return {
        "options": {
            "enabled": True,
            "mode": "paper_only",
            "allowed_underlyings": ["HPE", "QQQ"],
            "entry_mapping": {"bullish_signal": "call", "bearish_signal": "put"},
            "data_stale_max_age_seconds": 1,
            "max_daily_loss_pct": 1.0,
            "exits": {
                "stop_loss_exit_limit_per_day": 2,
            },
        }
    }


def test_sync_options_positions_blocks_on_stale_quote(tmp_path, caplog) -> None:
    caplog.set_level("INFO")
    broker = _Broker(
        [
            {
                "symbol": "HPE260619C00020000",
                "qty": 2,
                "avg_entry_price": 1.50,
                "cost_basis": -300.0,
                "market_value": 360.0,
                "unrealized_pl": 60.0,
            }
        ],
        quotes={},
    )

    snapshot = sync_options_positions(
        broker,
        _config(),
        user_id="alice",
        data_dir=tmp_path,
        now=datetime(2026, 6, 2, 14, 30, tzinfo=timezone.utc),
    )

    assert snapshot.open_count == 1
    assert snapshot.kill_switch_on is True
    assert snapshot.block_new_entries is True
    assert "stale_data" in snapshot.kill_switch_reasons
    assert "OPTIONS_POSITION_OPENED symbol=HPE260619C00020000" in caplog.text
    assert "OPTIONS_KILL_SWITCH_ON" in caplog.text
    assert "OPTIONS_ENTRY_BLOCKED" in caplog.text


def test_sync_options_positions_blocks_after_two_stop_losses(tmp_path) -> None:
    now = datetime(2026, 6, 2, 14, 30, tzinfo=timezone.utc)
    record_option_exit(
        "HPE260619C00020000",
        user_id="alice",
        data_dir=tmp_path,
        exit_reason="option_stop_loss",
        exit_price=1.20,
        realized_pl=-60.0,
        now=now,
    )
    record_option_exit(
        "QQQ260619C00450000",
        user_id="alice",
        data_dir=tmp_path,
        exit_reason="option_stop_loss",
        exit_price=0.80,
        realized_pl=-40.0,
        now=now,
    )

    broker = _Broker([], equity=100_000.0)
    snapshot = sync_options_positions(
        broker,
        _config(),
        user_id="alice",
        data_dir=tmp_path,
        now=now,
    )

    assert snapshot.kill_switch_on is True
    assert snapshot.block_new_entries is True
    assert snapshot.stop_loss_exits_today == 2
    assert "stop_loss_count" in snapshot.kill_switch_reasons


def test_route_to_options_executor_blocks_when_execution_manager_blocked(capsys) -> None:
    cfg = _config()
    sig = EntryRouteSignal(
        underlying="HPE",
        direction="bullish",
        source="dynamic_universe",
        stock_symbol="HPE",
    )
    em = SimpleNamespace(
        options_entry_blocked=True,
        options_entry_block_reasons=["daily_loss"],
        options_open_underlying_right={"HPE|call"},
        options_open_contracts={"HPE260619C00020000"},
    )

    ok = route_to_options_executor(
        cfg,
        sig,
        log_dt=datetime(2026, 6, 2, 14, 30, tzinfo=timezone.utc),
        execution_manager=em,
    )

    assert ok is False
    out = capsys.readouterr().out
    assert "OPTIONS_ENTRY_BLOCKED" in out

