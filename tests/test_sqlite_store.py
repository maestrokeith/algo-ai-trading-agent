from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

from src.sqlite_store import SQLiteEventStore


def _enabled_config(path: str) -> dict:
    return {
        "database": {
            "enabled": True,
            "type": "sqlite",
            "path": path,
            "journal_mode": "WAL",
            "synchronous": "NORMAL",
            "busy_timeout_ms": 5000,
        }
    }


def _count(db_path, table: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_disabled_store_is_noop(tmp_path):
    db_path = tmp_path / "disabled.db"
    store = SQLiteEventStore({"database": {"enabled": False, "path": str(db_path)}})

    store.record_trade(user_id="u1", symbol="AAPL", side="buy", qty=1)
    store.flush()
    store.close()

    assert store.enabled is False
    assert not db_path.exists()


def test_enabled_store_creates_schema_and_records_live_events(tmp_path):
    db_path = tmp_path / "algo_live.db"
    store = SQLiteEventStore(_enabled_config(str(db_path)))

    store.record_trade(
        user_id="live_bot",
        symbol="aapl",
        side="buy",
        qty=1.5,
        price=200.25,
        order_id="ord-1",
        status="accepted",
    )
    store.record_signal(
        user_id="live_bot",
        symbol="AAPL",
        signal_type="trend_long",
        strength=0.82,
        decision="entry",
        reason="ok",
    )
    store.record_entry_evaluation(
        user_id="live_bot",
        symbol="AAPL",
        route="trend_long",
        final=True,
        reason="ok",
        payload={"spread": "T"},
    )
    store.record_entry_terminal_outcome(
        user_id="live_bot",
        symbol="AAPL",
        route="trend_long",
        stage="allocator_no_action",
        reason="minimum_cash_to_deploy",
        payload={"minimum_cash_to_deploy": 840},
    )
    store.record_dynamic_scan(
        user_id="live_bot",
        selected=["MSFT", "NVDA"],
        candidates=[
            {"symbol": "MSFT", "accepted": True, "rejection_reason": None},
            {"symbol": "XYZ", "accepted": False, "rejection_reason": "below_min_price"},
        ],
        payload={"status": "ok"},
    )
    store.record_portfolio_snapshot(
        user_id="live_bot",
        equity=25_000,
        cash=3_000,
        buying_power=3_000,
        gross_exposure_pct=53.2,
        net_exposure_pct=53.2,
        positions_count=12,
    )
    store.record_daily_performance(
        user_id="live_bot",
        trading_date="2026-05-26",
        equity=25_000,
        pnl=125,
        pnl_pct=0.5,
        trades_count=3,
    )
    store.record_catalyst_outcome(
        user_id="live_bot",
        observed_date="2026-05-26",
        symbol="CRWD",
        catalyst_type="ai",
        news_score=8,
        subsequent_return_pct=4.25,
        source="dynamic_universe",
        trade_id="ord-2",
    )
    store.flush()

    for table in (
        "trades",
        "signals",
        "entry_evaluations",
        "entry_terminal_outcomes",
        "dynamic_scans",
        "portfolio_snapshots",
        "daily_performance",
        "catalyst_outcomes",
    ):
        assert _count(db_path, table) == 1

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT user_id, symbol, qty, price, order_id, status FROM trades"
        ).fetchone()
        assert row == ("live_bot", "AAPL", 1.5, 200.25, "ord-1", "accepted")
        dyn_row = conn.execute(
            "SELECT selected_json, candidates_json FROM dynamic_scans WHERE user_id = ?",
            ("live_bot",),
        ).fetchone()
        assert json.loads(dyn_row[0]) == ["MSFT", "NVDA"]
        assert json.loads(dyn_row[1])[1]["rejection_reason"] == "below_min_price"
        outcome_row = conn.execute(
            "SELECT symbol, catalyst_type, news_score, subsequent_return_pct FROM catalyst_outcomes WHERE user_id = ?",
            ("live_bot",),
        ).fetchone()
        assert outcome_row == ("CRWD", "ai", 8.0, 4.25)
        terminal_row = conn.execute(
            "SELECT symbol, route, stage, reason, terminal, payload_json FROM entry_terminal_outcomes WHERE user_id = ?",
            ("live_bot",),
        ).fetchone()
        assert terminal_row[:5] == (
            "AAPL",
            "trend_long",
            "allocator_no_action",
            "minimum_cash_to_deploy",
            1,
        )
        assert json.loads(terminal_row[5])["minimum_cash_to_deploy"] == 840

    store.close()


def test_portfolio_snapshot_rate_limit(tmp_path):
    db_path = tmp_path / "algo_live.db"
    store = SQLiteEventStore(_enabled_config(str(db_path)))

    store.record_portfolio_snapshot(
        user_id="u1",
        equity=100,
        min_interval_seconds=300,
    )
    store.record_portfolio_snapshot(
        user_id="u1",
        equity=101,
        min_interval_seconds=300,
    )
    store.record_portfolio_snapshot(
        user_id="u2",
        equity=102,
        min_interval_seconds=300,
    )
    store.flush()

    assert _count(db_path, "portfolio_snapshots") == 2
    store.close()


def test_broker_trade_hook_does_not_raise_without_store():
    from src.brokers.alpaca_client import AlpacaBroker

    broker = object.__new__(AlpacaBroker)

    broker._record_sqlite_trade_event(
        symbol="AAPL",
        side="sell",
        qty=0.25,
        notional=None,
        result=SimpleNamespace(id="ord-2", status="filled", filled_avg_price="201.5"),
        payload={"source": "test"},
    )


def test_broker_trade_hook_records_filled_qty_when_present():
    from src.brokers.alpaca_client import AlpacaBroker

    class Recorder:
        def __init__(self) -> None:
            self.rows = []

        def record_trade(self, **kwargs):
            self.rows.append(kwargs)

    recorder = Recorder()
    broker = object.__new__(AlpacaBroker)
    broker._sqlite_event_store = recorder
    broker._sqlite_user_id = "live_bot"

    broker._record_sqlite_trade_event(
        symbol="AAPL",
        side="sell",
        qty=2.0,
        notional=None,
        result=SimpleNamespace(
            id="ord-2",
            status="filled",
            filled_avg_price="201.5",
            filled_qty="0.75",
        ),
        payload={"source": "test"},
    )

    assert recorder.rows[0]["user_id"] == "live_bot"
    assert recorder.rows[0]["qty"] == "0.75"
    assert recorder.rows[0]["payload"]["filled_qty"] == "0.75"


def test_broker_trade_hook_skips_unfilled_submission():
    from src.brokers.alpaca_client import AlpacaBroker

    class Recorder:
        def __init__(self) -> None:
            self.rows = []

        def record_trade(self, **kwargs):
            self.rows.append(kwargs)

    recorder = Recorder()
    broker = object.__new__(AlpacaBroker)
    broker._sqlite_event_store = recorder
    broker._sqlite_user_id = "live_bot"

    broker._record_sqlite_trade_event(
        symbol="AAPL",
        side="buy",
        qty=1.0,
        notional=None,
        result=SimpleNamespace(
            id="ord-3",
            status="accepted",
            limit_price="1077.59",
            filled_qty="0",
        ),
        payload={"source": "test"},
    )

    assert recorder.rows == []
