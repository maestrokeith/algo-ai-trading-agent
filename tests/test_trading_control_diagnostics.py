from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import trading_diagnostics as td
from src.accounting_invariants import enforce_daily_invariants, invariant_failures_from_audit
from src.execution import OrderRequest, OrderType
from src.trading_control import (
    ENTRY_BLOCKED_MODE_ENTRIES_DISABLED,
    EntryBlocked,
    EntryCircuitBreaker,
    TradingControlBroker,
    authorize_order_submission,
    entries_blocked_by_integrity,
    resolve_trading_mode,
    run_entry_evaluation_safely,
    strategy_states,
    persist_integrity_incident,
)


def _artifact(root: Path, day: str = "2026-07-20", user: str = "live_bot", payload: dict | None = None) -> Path:
    path = root / "data" / "trade_attribution" / "daily" / f"{day}_{user}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            payload
            or {
                "date": day,
                "user_id": user,
                "candidates": [{"timestamp": f"{day}T13:30:00+00:00", "symbol": "AAPL", "accepted": True, "route": "trend_long"}],
                "allocator_candidates": [{"timestamp": f"{day}T13:31:00+00:00", "symbol": "AAPL", "action_created": True, "selected_rank": 1}],
                "orders": [
                    {
                        "timestamp": f"{day}T13:32:00+00:00",
                        "symbol": "AAPL",
                        "action": "buy",
                        "submitted": True,
                        "order_id": "o1",
                        "status": "filled",
                        "qty": 1,
                        "filled_qty": 1,
                        "filled_avg_price": 10,
                    }
                ],
                "exits": [
                    {
                        "timestamp": f"{day}T14:00:00+00:00",
                        "symbol": "AAPL",
                        "exit_reason": "take_profit",
                        "realized_pnl": 2.0,
                        "pnl": 2.0,
                        "hold_minutes": 28,
                        "entry_route": "trend_long",
                        "mfe_pct": 4.0,
                        "mae_pct": -1.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_trading_modes_and_authorization() -> None:
    assert resolve_trading_mode({"trading_control": {"mode": "live"}}, paper=False, live_operation=True).live_orders_allowed
    assert resolve_trading_mode({"trading_control": {"mode": "paper"}}, paper=True).new_entries_allowed
    assert not resolve_trading_mode({"trading_control": {"mode": "shadow"}}, paper=False).broker_orders_allowed
    buy = OrderRequest("AAPL", "buy", 1, OrderType.MARKET)
    sell = OrderRequest("AAPL", "sell", 1, OrderType.MARKET)
    ok, reason = authorize_order_submission({"trading_control": {"mode": "entries-disabled"}}, buy, paper=False)
    assert not ok
    assert reason == ENTRY_BLOCKED_MODE_ENTRIES_DISABLED
    ok, reason = authorize_order_submission({"trading_control": {"mode": "entries-disabled"}}, sell, paper=False)
    assert ok
    with pytest.raises(ValueError):
        resolve_trading_mode({"trading_control": {"mode": "bad"}}, paper=False, live_operation=True)


def test_shadow_mode_never_calls_broker_order_submission(tmp_path: Path) -> None:
    class Broker:
        called = False

        def submit_order(self, _order):
            self.called = True
            raise AssertionError("broker should not be called")

    broker = Broker()
    wrapped = TradingControlBroker(
        broker,
        config={"trading_control": {"mode": "shadow"}},
        paper=False,
        data_dir=tmp_path,
        user_id="live_bot",
    )
    result = wrapped.submit_order(OrderRequest("AAPL", "buy", 1, OrderType.MARKET))
    assert result.status == "shadow"
    assert broker.called is False


def test_shadow_mode_blocks_exit_submission_at_final_boundary(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    class Broker:
        called = False

        def submit_order(self, _order):
            self.called = True
            raise AssertionError("shadow exit must not reach broker")

    broker = Broker()
    wrapped = TradingControlBroker(
        broker,
        config={"trading_control": {"mode": "shadow"}},
        paper=False,
        data_dir=tmp_path,
        user_id="live_bot",
    )

    with caplog.at_level("INFO"):
        result = wrapped.submit_order(OrderRequest("AAPL", "sell", 1, OrderType.MARKET))

    assert result.status == "shadow"
    assert broker.called is False
    assert "BROKER_DISPATCH_BLOCKED" in caplog.text
    assert "configured_mode=shadow" in caplog.text
    assert "effective_mode=shadow" in caplog.text
    assert "broker_dispatch_attempted=false" in caplog.text
    assert "execution_allowed=false" in caplog.text
    assert "hypothetical=true" in caplog.text


def test_entries_disabled_preserves_exits(tmp_path: Path) -> None:
    calls: list[str] = []

    class Broker:
        def submit_order(self, order):
            calls.append(order.side)
            return SimpleNamespace(status="accepted")

    wrapped = TradingControlBroker(
        Broker(),
        config={"trading_control": {"mode": "entries-disabled"}},
        paper=False,
        data_dir=tmp_path,
        user_id="live_bot",
    )
    with pytest.raises(EntryBlocked):
        wrapped.submit_order(OrderRequest("AAPL", "buy", 1, OrderType.MARKET))
    assert wrapped.submit_order(OrderRequest("AAPL", "sell", 1, OrderType.MARKET)).status == "accepted"
    assert calls == ["sell"]
    assert not entries_blocked_by_integrity(tmp_path, "live_bot")
    assert not (tmp_path / "integrity" / "live_bot.json").exists()


def test_entries_disabled_order_block_logs_without_error_or_incident(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    class Broker:
        def submit_order(self, order):
            raise AssertionError("buy order must not reach broker")

    wrapped = TradingControlBroker(
        Broker(),
        config={"trading_control": {"mode": "entries-disabled"}},
        paper=False,
        data_dir=tmp_path,
        user_id="live_bot",
    )

    with caplog.at_level("INFO"):
        with pytest.raises(EntryBlocked):
            wrapped.submit_order(OrderRequest("AAPL", "buy", 1, OrderType.MARKET))

    assert ENTRY_BLOCKED_MODE_ENTRIES_DISABLED in caplog.text
    assert "INTEGRITY_INCIDENT" not in caplog.text
    assert not entries_blocked_by_integrity(tmp_path, "live_bot")


def test_expected_safety_block_is_not_runtime_exception_or_integrity_incident(tmp_path: Path) -> None:
    persist_integrity_incident(
        tmp_path / "data",
        user_id="live_bot",
        reason_code="preexisting_position_symbol",
        detail="protected position buy blocked",
    )

    incidents = td._load_integrity_incidents(tmp_path, "live_bot", include_expected_blocks=True)
    assert incidents[0]["classification"] == "EXPECTED_SAFETY_BLOCK"
    assert incidents[0]["severity"] == "warning"
    assert td._load_integrity_incidents(tmp_path, "live_bot") == []
    assert entries_blocked_by_integrity(tmp_path, "live_bot") is False


def test_submitted_orders_not_counted_as_fills_and_duplicates(tmp_path: Path) -> None:
    day = "2026-07-20"
    _artifact(
        tmp_path,
        day,
        payload={
            "date": day,
            "user_id": "live_bot",
            "candidates": [{"timestamp": f"{day}T13:30:00+00:00", "symbol": "AAPL", "accepted": True}],
            "orders": [
                {"timestamp": f"{day}T13:31:00+00:00", "symbol": "AAPL", "action": "buy", "submitted": True, "order_id": "dup", "status": "accepted"},
                {"timestamp": f"{day}T13:31:01+00:00", "symbol": "AAPL", "action": "buy", "submitted": True, "order_id": "dup", "status": "accepted"},
            ],
            "exits": [],
        },
    )
    result = td.run_trading_audit(root=tmp_path, day=day, user="live_bot")
    assert result.report["counts"]["raw_submitted_order_events"] == 2
    assert result.report["counts"]["submitted_orders"] == 1
    assert result.report["counts"]["unique_submitted_orders"] == 1
    assert result.report["counts"]["completed_fills"] == 0
    assert any(p["kind"] == "duplicate_orders" for p in result.problems)


def test_trading_audit_records_requested_broker(tmp_path: Path) -> None:
    day = "2026-07-20"
    _artifact(tmp_path, day)

    result = td.run_trading_audit(root=tmp_path, day=day, user="live_bot", broker="alpaca")

    assert result.report["broker"] == "alpaca"


def test_recovered_broker_fill_creates_canonical_position_idempotently(tmp_path: Path) -> None:
    day = "2026-08-05"
    order_id = "299a7450-e2d8-4cdf-9224-70be9e04c30b"
    _artifact(
        tmp_path,
        day,
        payload={
            "date": day,
            "user_id": "live_bot",
            "candidates": [{"timestamp": f"{day}T13:30:00+00:00", "symbol": "IWM", "accepted": True}],
            "orders": [
                {
                    "timestamp": f"{day}T13:31:00+00:00",
                    "symbol": "IWM",
                    "action": "buy",
                    "submitted": True,
                    "order_id": order_id,
                    "status": "new",
                    "allocator_requested_notional": 1312.50,
                    "allocator_requested_qty": 4,
                    "bounded_pilot_applied": True,
                    "final_submitted_qty": 0.330702357,
                    "final_estimated_notional": 99.99,
                }
            ],
            "exits": [],
        },
    )

    broker = SimpleNamespace(
        get_order=lambda oid: SimpleNamespace(
            id=oid,
            symbol="IWM",
            side="buy",
            status="filled",
            qty="0.330702357",
            filled_qty="0.330702357",
            filled_avg_price="302.35",
            submitted_at=f"{day}T13:31:00+00:00",
            filled_at=f"{day}T13:31:02+00:00",
        ),
        get_positions=lambda: [SimpleNamespace(symbol="IWM", qty="0.330702357", market_value="99.99")],
    )

    first = td.reconcile_broker_order_lifecycle(
        root=tmp_path,
        day=day,
        user="live_bot",
        broker=broker,
        order_id=order_id,
        symbol="IWM",
    )
    second = td.reconcile_broker_order_lifecycle(
        root=tmp_path,
        day=day,
        user="live_bot",
        broker=broker,
        order_id=order_id,
        symbol="IWM",
    )
    audit = td.run_trading_audit(root=tmp_path, day=day, user="live_bot")
    for report in (first, second):
        assert report["reconciled"] is True
        assert report["canonical_counts"]["submitted_orders"] == 1
        assert report["canonical_counts"]["broker_accepted_orders"] == 1
        assert report["canonical_counts"]["completed_fills"] == 1
        assert report["canonical_counts"]["opened_positions"] == 1
        assert report["canonical_counts"]["duplicate_order_events"] == 0
    assert audit.report["counts"]["recovered_broker_fill_events"] == 1
    assert audit.report["counts"]["broker_reconciled_positions_today"] == 1
    assert audit.report["counts"]["positions_with_recovered_lineage"] == 1
    assert audit.report["fully_reconciled"] is True


def test_submitted_order_resolves_to_terminal_broker_states(tmp_path: Path) -> None:
    day = "2026-08-06"
    rows = [
        ("ACPT", "accepted"),
        ("REJ", "rejected"),
        ("CXL", "canceled"),
        ("FILL", "filled"),
        ("PEND", "pending_new"),
    ]
    _artifact(
        tmp_path,
        day,
        payload={
            "date": day,
            "user_id": "live_bot",
            "candidates": [
                {"timestamp": f"{day}T13:30:00+00:00", "symbol": symbol, "accepted": True}
                for symbol, _status in rows
            ],
            "orders": [
                {
                    "timestamp": f"{day}T13:31:00+00:00",
                    "symbol": symbol,
                    "action": "buy",
                    "submitted": True,
                    "order_id": f"oid-{symbol}",
                    "status": status,
                    "qty": 1,
                    "filled_qty": 1 if status == "filled" else 0,
                    "filled_avg_price": 10 if status == "filled" else None,
                }
                for symbol, status in rows
            ],
            "exits": [],
        },
    )

    result = td.run_trading_audit(root=tmp_path, day=day, user="live_bot")
    counts = result.report["counts"]

    assert counts["local_submitted_orders"] == 5
    assert counts["broker_confirmed_orders"] == 5
    assert counts["broker_canonical_accepted_orders"] == 1
    assert counts["broker_rejected_orders"] == 1
    assert counts["broker_cancelled_orders"] == 1
    assert counts["broker_filled_orders"] == 1
    assert counts["broker_unresolved_orders"] == 1
    assert counts["canonical_fills"] == 1
    assert not any(p["kind"] == "submitted_order_state_invariant_failed" for p in result.problems)
    assert result.report["order_reconciliation"]["broker_order_id:oid-REJ"]["terminal_state"] == "REJECTED"


def test_reconcile_persists_rejected_and_cancelled_as_resolved_states(tmp_path: Path) -> None:
    day = "2026-08-06"
    _artifact(
        tmp_path,
        day,
        payload={
            "date": day,
            "user_id": "live_bot",
            "candidates": [{"timestamp": f"{day}T13:30:00+00:00", "symbol": "XLF", "accepted": True}],
            "orders": [
                {"timestamp": f"{day}T13:31:00+00:00", "symbol": "XLF", "action": "buy", "submitted": True, "order_id": "xlf-order", "status": "accepted"}
            ],
            "exits": [],
        },
    )
    broker = SimpleNamespace(
        get_order=lambda oid: SimpleNamespace(
            id=oid,
            symbol="XLF",
            side="buy",
            status="rejected",
            qty="1",
            filled_qty="0",
            submitted_at=f"{day}T13:31:00+00:00",
            rejected_at=f"{day}T13:31:05+00:00",
        ),
        get_positions=lambda: [],
    )

    report = td.reconcile_broker_order_lifecycle(root=tmp_path, day=day, user="live_bot", broker=broker, order_id="xlf-order", symbol="XLF")

    assert report["reconciled"] is True
    assert report["terminal_state"] == "REJECTED"
    assert report["canonical_counts"]["broker_rejected_orders"] == 1
    assert report["canonical_counts"]["broker_terminal_orders"] == 1


def test_broker_activity_id_deduplicates_recovered_fills(tmp_path: Path) -> None:
    day = "2026-08-06"
    _artifact(
        tmp_path,
        day,
        payload={
            "date": day,
            "user_id": "live_bot",
            "candidates": [{"timestamp": f"{day}T13:30:00+00:00", "symbol": "IWM", "accepted": True}],
            "orders": [
                {
                    "timestamp": f"{day}T13:31:00+00:00",
                    "symbol": "IWM",
                    "action": "buy",
                    "submitted": True,
                    "order_id": "iwm-order",
                    "status": "filled",
                    "qty": 2,
                    "filled_qty": 2,
                    "filled_avg_price": 200,
                    "broker_activity_id": "activity-1",
                    "event_origin": "broker_reconciliation",
                    "recovered": True,
                },
                {
                    "timestamp": f"{day}T13:31:00+00:00",
                    "symbol": "IWM",
                    "action": "buy",
                    "submitted": True,
                    "order_id": "iwm-order",
                    "status": "filled",
                    "qty": 2,
                    "filled_qty": 2,
                    "filled_avg_price": 200,
                    "broker_activity_id": "activity-1",
                    "event_origin": "broker_reconciliation",
                    "recovered": True,
                },
            ],
            "exits": [],
        },
    )

    result = td.run_trading_audit(root=tmp_path, day=day, user="live_bot")

    assert result.report["counts"]["raw_fill_events"] == 2
    assert result.report["counts"]["canonical_fills"] == 1
    assert result.report["counts"]["duplicate_fill_events"] == 1
    assert result.report["canonical_lineage"]["fills"] == ["broker_activity_id:activity-1"]


def test_reconcile_prefers_broker_native_fill_activity_identity(tmp_path: Path) -> None:
    day = "2026-08-06"
    _artifact(
        tmp_path,
        day,
        payload={
            "date": day,
            "user_id": "live_bot",
            "candidates": [{"timestamp": f"{day}T13:30:00+00:00", "symbol": "IWM", "accepted": True}],
            "orders": [
                {"timestamp": f"{day}T13:31:00+00:00", "symbol": "IWM", "action": "buy", "submitted": True, "order_id": "iwm-order", "status": "new"}
            ],
            "exits": [],
        },
    )
    broker = SimpleNamespace(
        get_order=lambda oid: SimpleNamespace(
            id=oid,
            symbol="IWM",
            side="buy",
            status="filled",
            qty="2",
            filled_qty="2",
            filled_avg_price="200",
            filled_at=f"{day}T13:32:00+00:00",
        ),
        get_positions=lambda: [SimpleNamespace(symbol="IWM", qty="2", market_value="400")],
        get_order_activities=lambda order_id: [
            SimpleNamespace(id="activity-1", order_id=order_id, activity_type="FILL", side="buy", qty="2", price="200", transaction_time=f"{day}T13:32:00+00:00")
        ],
    )

    td.reconcile_broker_order_lifecycle(root=tmp_path, day=day, user="live_bot", broker=broker, order_id="iwm-order", symbol="IWM")
    audit = td.run_trading_audit(root=tmp_path, day=day, user="live_bot")

    assert audit.report["counts"]["canonical_fills"] == 1
    assert audit.report["canonical_lineage"]["fills"] == ["broker_activity_id:activity-1"]
    assert audit.report["counts"]["positions_with_recovered_lineage"] == 1


def test_reconcile_recovers_multiple_partial_fill_activities(tmp_path: Path) -> None:
    day = "2026-08-06"
    _artifact(
        tmp_path,
        day,
        payload={
            "date": day,
            "user_id": "live_bot",
            "candidates": [{"timestamp": f"{day}T13:30:00+00:00", "symbol": "IWM", "accepted": True}],
            "orders": [
                {"timestamp": f"{day}T13:31:00+00:00", "symbol": "IWM", "action": "buy", "submitted": True, "order_id": "iwm-order", "status": "accepted"}
            ],
            "exits": [],
        },
    )
    broker = SimpleNamespace(
        get_order=lambda oid: SimpleNamespace(
            id=oid,
            symbol="IWM",
            side="buy",
            status="filled",
            qty="2",
            filled_qty="2",
            filled_avg_price="201",
            filled_at=f"{day}T13:33:00+00:00",
        ),
        get_positions=lambda: [SimpleNamespace(symbol="IWM", qty="2", market_value="402")],
        get_order_activities=lambda order_id: [
            SimpleNamespace(id="activity-1", order_id=order_id, activity_type="FILL", side="buy", qty="0.75", price="200", transaction_time=f"{day}T13:32:00+00:00"),
            SimpleNamespace(id="activity-2", order_id=order_id, activity_type="FILL", side="buy", qty="1.25", price="201.6", transaction_time=f"{day}T13:33:00+00:00"),
        ],
    )

    td.reconcile_broker_order_lifecycle(root=tmp_path, day=day, user="live_bot", broker=broker, order_id="iwm-order", symbol="IWM")
    td.reconcile_broker_order_lifecycle(root=tmp_path, day=day, user="live_bot", broker=broker, order_id="iwm-order", symbol="IWM")
    audit = td.run_trading_audit(root=tmp_path, day=day, user="live_bot")

    assert audit.report["counts"]["raw_fill_events"] == 2
    assert audit.report["counts"]["canonical_fills"] == 2
    assert audit.report["counts"]["duplicate_fill_events"] == 0
    assert audit.report["canonical_lineage"]["positions"]["equity:live_bot:IWM"]["entry_qty"] == pytest.approx(2.0)


def test_bulk_reconcile_starts_from_every_local_submitted_order(tmp_path: Path) -> None:
    day = "2026-08-06"
    _artifact(
        tmp_path,
        day,
        payload={
            "date": day,
            "user_id": "live_bot",
            "candidates": [
                {"timestamp": f"{day}T13:30:00+00:00", "symbol": "XLF", "accepted": True},
                {"timestamp": f"{day}T13:30:00+00:00", "symbol": "XLE", "accepted": True},
            ],
            "orders": [
                {"timestamp": f"{day}T13:31:00+00:00", "symbol": "XLF", "action": "buy", "submitted": True, "order_id": "xlf-order", "status": "new"},
                {"timestamp": f"{day}T13:32:00+00:00", "symbol": "XLE", "action": "buy", "submitted": True, "order_id": "xle-order", "status": "new"},
            ],
            "exits": [],
        },
    )
    orders = {
        "xlf-order": SimpleNamespace(id="xlf-order", symbol="XLF", side="buy", status="filled", qty="1", filled_qty="1", filled_avg_price="50", filled_at=f"{day}T13:33:00+00:00"),
        "xle-order": SimpleNamespace(id="xle-order", symbol="XLE", side="buy", status="rejected", qty="1", filled_qty="0", rejected_at=f"{day}T13:34:00+00:00"),
    }
    broker = SimpleNamespace(get_order=lambda oid: orders[oid], get_positions=lambda: [])

    report = td.reconcile_submitted_broker_orders(root=tmp_path, day=day, user="live_bot", broker=broker)

    assert report["submitted_order_count"] == 2
    assert report["reconciled_count"] == 2
    assert report["canonical_counts"]["broker_filled_orders"] == 1
    assert report["canonical_counts"]["broker_rejected_orders"] == 1


def test_day_review_reports_global_and_strategy_permission_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    day = "2026-08-06"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "default.yaml").write_text(
        """
trading_control:
  mode: live
  strategy_states:
    trend_long: LIVE
    dynamic_no_catalyst: SHADOW
    momentum_breakout: SHADOW
    news_only: DISABLED
    options_live: DISABLED
    options_paper: DISABLED
""",
        encoding="utf-8",
    )
    _artifact(
        tmp_path,
        day,
        payload={
            "date": day,
            "user_id": "live_bot",
            "candidates": [{"timestamp": f"{day}T13:30:00+00:00", "symbol": "AAPL", "accepted": False, "reason": "wide_spread"}],
            "orders": [],
            "exits": [],
        },
    )
    monkeypatch.setenv("ALGO_RUNTIME_USER", "algosphere")

    report = td.day_review_report(root=tmp_path, day=day, user="live_bot")
    state = report["system_and_safety_state"]

    assert state["global_mode_live_orders_allowed"] is True
    assert state["global_mode_new_entries_allowed"] is True
    assert state["trend_long_live_entries_allowed"] is True
    assert state["strategy_live_entries_allowed"]["dynamic_no_catalyst"] is False
    assert state["shadow_routes_live_entries_allowed"] is False


def test_duplicate_forensics_counts_recovered_without_duplicate_after_repeated_reconciliation(tmp_path: Path) -> None:
    day = "2026-08-05"
    order_id = "iwm-order"
    _artifact(
        tmp_path,
        day,
        payload={
            "date": day,
            "user_id": "live_bot",
            "candidates": [{"timestamp": f"{day}T13:30:00+00:00", "symbol": "IWM", "accepted": True}],
            "orders": [
                {"timestamp": f"{day}T13:31:00+00:00", "symbol": "IWM", "action": "buy", "submitted": True, "order_id": order_id, "status": "new"}
            ],
            "exits": [],
        },
    )
    broker = SimpleNamespace(
        get_order=lambda oid: SimpleNamespace(id=oid, symbol="IWM", side="buy", status="filled", qty="0.5", filled_qty="0.5", filled_avg_price="200", filled_at=f"{day}T13:31:02+00:00"),
        get_positions=lambda: [SimpleNamespace(symbol="IWM", qty="0.5", market_value="100")],
    )

    td.reconcile_broker_order_lifecycle(root=tmp_path, day=day, user="live_bot", broker=broker, order_id=order_id, symbol="IWM")
    td.reconcile_broker_order_lifecycle(root=tmp_path, day=day, user="live_bot", broker=broker, order_id=order_id, symbol="IWM")

    report = td.duplicate_forensics_report(root=tmp_path, day=day, user="live_bot", order_id=order_id)
    assert report["counts"]["recovered_broker_order_snapshots"] == 1
    assert report["counts"]["duplicate_order_events"] == 0
    assert report["counts"]["duplicate_fill_events"] == 0
    assert report["duplicate_orders"] == []
    assert report["duplicate_fills"] == []


def test_shadow_order_intents_are_not_real_submissions_or_fills(tmp_path: Path) -> None:
    day = "2026-07-20"
    _artifact(
        tmp_path,
        day,
        payload={
            "date": day,
            "user_id": "live_bot",
            "candidates": [
                {
                    "timestamp": f"{day}T13:30:00+00:00",
                    "symbol": "AAPL",
                    "accepted": True,
                    "environment": "shadow",
                    "hypothetical": True,
                }
            ],
            "allocator_candidates": [
                {
                    "timestamp": f"{day}T13:31:00+00:00",
                    "symbol": "AAPL",
                    "action_created": True,
                    "selected_rank": 1,
                    "environment": "shadow",
                    "hypothetical": True,
                }
            ],
            "orders": [
                {
                    "timestamp": f"{day}T13:32:00+00:00",
                    "symbol": "AAPL",
                    "action": "buy",
                    "submitted": True,
                    "order_id": "shadow-1",
                    "status": "shadow",
                    "environment": "shadow",
                    "hypothetical": True,
                    "broker_dispatch_attempted": False,
                    "execution_allowed": False,
                    "reason": "ORDER_BLOCKED_SHADOW_MODE",
                }
            ],
            "exits": [],
        },
    )

    result = td.run_trading_audit(root=tmp_path, day=day, user="live_bot")
    counts = result.report["counts"]
    assert counts["raw_submitted_order_events"] == 0
    assert counts["unique_submitted_orders"] == 0
    assert counts["raw_broker_accepted_order_events"] == 0
    assert counts["unique_broker_accepted_orders"] == 0
    assert counts["raw_fill_events"] == 0
    assert counts["unique_fills"] == 0
    assert counts["shadow_decisions"] == 1
    assert counts["shadow_allocator_actions"] == 1
    assert counts["shadow_order_intents"] == 1
    assert counts["shadow_execution_blocks"] == 1
    assert counts["synthetic_or_replay_order_events"] == 0
    assert counts["replay_event_count"] == 0
    assert result.report["integrity_status"]["status"] in {"CLEAN", "PARTIAL"}


def test_legacy_shadow_submit_attempts_are_not_real_orders_or_replay(tmp_path: Path) -> None:
    day = "2026-07-28"
    _artifact(
        tmp_path,
        day,
        payload={
            "date": day,
            "user_id": "live_bot",
            "candidates": [{"timestamp": f"{day}T13:30:00+00:00", "symbol": "XLE", "accepted": True}],
            "allocator_candidates": [{"timestamp": f"{day}T13:31:00+00:00", "symbol": "XLE", "action_created": True}],
            "orders": [
                {
                    "timestamp": f"{day}T13:50:00+00:00",
                    "symbol": "XLE",
                    "action": "buy",
                    "submit_attempt": True,
                    "submitted": False,
                    "order_build_status": "built",
                    "notional": 1312.5,
                },
                {
                    "timestamp": f"{day}T14:07:00+00:00",
                    "symbol": "XLE",
                    "action": "buy",
                    "submit_attempt": True,
                    "submitted": False,
                    "order_build_status": "built",
                    "notional": 1312.5,
                },
                {
                    "timestamp": f"{day}T14:54:00+00:00",
                    "symbol": "XLF",
                    "action": "buy",
                    "submit_attempt": True,
                    "submitted": False,
                    "order_build_status": "built",
                    "notional": 1312.5,
                    "order_id": "shadow-legacy",
                    "status": "shadow",
                    "environment": "shadow",
                    "hypothetical": True,
                    "broker_dispatch_attempted": False,
                    "execution_allowed": False,
                },
            ],
            "exits": [],
        },
    )

    report = td.duplicate_forensics_report(root=tmp_path, day=day, user="live_bot")
    counts = report["counts"]

    assert counts["raw_submitted_order_events"] == 0
    assert counts["unique_submitted_orders"] == 0
    assert counts["raw_broker_accepted_order_events"] == 0
    assert counts["unique_broker_accepted_orders"] == 0
    assert counts["raw_fill_events"] == 0
    assert counts["unique_fills"] == 0
    assert counts["unique_opened_positions"] == 0
    assert counts["synthetic_or_replay_order_events"] == 0
    assert counts["replay_event_count"] == 0
    assert counts["shadow_order_intents"] == 3
    assert counts["legacy_shadow_records_reclassified"] == 2
    assert report["integrity_status"]["status"] in {"CLEAN", "PARTIAL"}
    assert report["duplicate_orders"] == []


def test_position_without_fill_exit_without_position_and_mixed_records(tmp_path: Path) -> None:
    day = "2026-07-20"
    _artifact(
        tmp_path,
        day,
        payload={
            "date": day,
            "user_id": "live_bot",
            "candidates": [],
            "orders": [{"timestamp": "2026-07-19T13:31:00+00:00", "symbol": "AAPL", "action": "buy", "submitted": True, "status": "filled", "filled_qty": 1, "mode": "paper"}],
            "exits": [{"timestamp": f"{day}T14:00:00+00:00", "symbol": "MSFT", "realized_pnl": -1}],
        },
    )
    result = td.run_trading_audit(root=tmp_path, day=day, user="live_bot")
    kinds = {p["kind"] for p in result.problems}
    assert "incorrect_trading_dates" in kinds
    assert "exits_without_positions" in kinds
    assert "incorrect_trading_dates" in kinds
    assert "paper_records_mixed_into_live" not in kinds


def test_equity_and_option_separation(tmp_path: Path) -> None:
    day = "2026-07-20"
    _artifact(
        tmp_path,
        day,
        payload={
            "date": day,
            "user_id": "live_bot",
            "candidates": [{"timestamp": f"{day}T13:30:00+00:00", "symbol": "AAPL250815C00200000", "accepted": True}],
            "orders": [{"timestamp": f"{day}T13:31:00+00:00", "symbol": "AAPL250815C00200000", "action": "buy", "submitted": True, "status": "filled", "filled_qty": 1, "sleeve": "equity"}],
            "exits": [],
        },
    )
    assert any(p["kind"] == "equity_and_option_mixed" for p in td.run_trading_audit(root=tmp_path, day=day, user="live_bot").problems)


def test_lifecycle_repeated_snapshots_are_idempotent_and_cumulative_fills(tmp_path: Path) -> None:
    day = "2026-07-20"
    orders = [
        {"timestamp": f"{day}T13:31:00+00:00", "symbol": "AAPL", "action": "buy", "submitted": True, "order_id": "o1", "status": "accepted", "qty": 2},
        {"timestamp": f"{day}T13:32:00+00:00", "symbol": "AAPL", "action": "buy", "submitted": True, "order_id": "o1", "status": "partially_filled", "qty": 2, "filled_qty": 1, "filled_avg_price": 10},
        {"timestamp": f"{day}T13:32:00+00:00", "symbol": "AAPL", "action": "buy", "submitted": True, "order_id": "o1", "status": "partially_filled", "qty": 2, "filled_qty": 1, "filled_avg_price": 10},
        {"timestamp": f"{day}T13:33:00+00:00", "symbol": "AAPL", "action": "buy", "submitted": True, "order_id": "o1", "status": "filled", "qty": 2, "filled_qty": 2, "filled_avg_price": 10.5},
    ]
    _artifact(
        tmp_path,
        day,
        payload={
            "date": day,
            "user_id": "live_bot",
            "candidates": [{"timestamp": f"{day}T13:30:00+00:00", "symbol": "AAPL", "accepted": True}],
            "orders": orders * 3,
            "exits": [],
        },
    )
    result = td.run_trading_audit(root=tmp_path, day=day, user="live_bot")
    counts = result.report["counts"]
    assert counts["raw_submitted_order_events"] == 12
    assert counts["unique_submitted_orders"] == 1
    assert counts["raw_fill_events"] == 9
    assert counts["unique_fills"] == 2
    assert counts["unique_opened_positions"] == 1
    assert counts["unique_still_open_positions"] == 1


def test_partial_exits_and_same_symbol_reopened_use_position_lineage(tmp_path: Path) -> None:
    day = "2026-07-20"
    _artifact(
        tmp_path,
        day,
        payload={
            "date": day,
            "user_id": "live_bot",
            "candidates": [{"timestamp": f"{day}T13:30:00+00:00", "symbol": "AAPL", "accepted": True}],
            "orders": [
                {"timestamp": f"{day}T13:31:00+00:00", "symbol": "AAPL", "action": "buy", "submitted": True, "order_id": "o1", "status": "filled", "qty": 2, "filled_qty": 2, "filled_avg_price": 10, "fill_id": "f1"},
                {"timestamp": f"{day}T14:00:00+00:00", "symbol": "AAPL", "action": "sell", "submitted": True, "order_id": "o2", "status": "partially_filled", "qty": 2, "filled_qty": 1, "filled_avg_price": 11, "fill_id": "f2"},
                {"timestamp": f"{day}T14:05:00+00:00", "symbol": "AAPL", "action": "sell", "submitted": True, "order_id": "o2", "status": "filled", "qty": 2, "filled_qty": 1, "filled_avg_price": 12, "fill_id": "f3"},
                {"timestamp": f"{day}T15:00:00+00:00", "symbol": "AAPL", "action": "buy", "submitted": True, "order_id": "o3", "status": "filled", "qty": 1, "filled_qty": 1, "filled_avg_price": 13, "fill_id": "f4"},
            ],
            "exits": [],
        },
    )
    result = td.run_trading_audit(root=tmp_path, day=day, user="live_bot")
    counts = result.report["counts"]
    assert counts["unique_fills"] == 4
    assert counts["unique_opened_positions"] == 1
    assert counts["unique_closed_positions"] == 0
    assert counts["unique_still_open_positions"] == 1
    assert result.report["canonical_lineage"]["positions"]["equity:live_bot:AAPL"]["entry_qty"] == 3
    assert result.report["canonical_lineage"]["positions"]["equity:live_bot:AAPL"]["exit_qty"] == 2


def test_trading_date_uses_broker_event_time_in_new_york() -> None:
    row = {
        "broker_event_timestamp": "2026-07-22T01:30:00+00:00",
        "timestamp": "2026-07-22T01:31:00+00:00",
        "ingested_at_utc": "2026-07-22T01:35:00+00:00",
    }
    assert td.event_trading_date_et(row) == "2026-07-21"
    normalized = td.normalize_lifecycle_timestamps(row)
    assert normalized["event_timestamp_utc"] == "2026-07-22T01:30:00+00:00"
    assert normalized["event_trading_date_et"] == "2026-07-21"


def test_duplicate_forensics_identifies_replay_rows_and_filters(tmp_path: Path) -> None:
    day = "2026-07-21"
    _artifact(
        tmp_path,
        day,
        payload={
            "date": day,
            "user_id": "live_bot",
            "candidates": [{"timestamp": f"{day}T13:30:00+00:00", "symbol": "CRWV", "accepted": True}],
            "orders": [
                {"timestamp": f"{day}T13:30:00+00:00", "symbol": "CRWV", "action": "buy", "submitted": True, "order_id": "replay-1", "status": "n/a", "filled_qty": 12, "qty": 12, "route": "premarket_catalyst_replay", "source": "earnings"},
                {"timestamp": f"{day}T13:31:00+00:00", "symbol": "CRWV", "action": "buy", "submitted": True, "order_id": "replay-1", "status": "n/a", "filled_qty": 12, "qty": 12, "route": "premarket_catalyst_replay", "source": "earnings"},
            ],
            "exits": [],
        },
    )
    report = td.duplicate_forensics_report(root=tmp_path, day=day, user="live_bot", order_id="replay-1")
    assert report["counts"]["synthetic_or_replay_order_events"] == 0
    assert report["counts"]["replay_research_outcomes"] == 2
    assert report["counts"]["duplicate_replay_research_outcomes"] == 1
    assert report["counts"]["raw_fill_events"] == 0
    assert report["counts"]["raw_position_records"] == 0
    assert report["counts"]["unresolved_contamination"] == 0
    assert report["duplicate_orders"] == []
    assert report["duplicate_replay_orders"][0]["records"][0]["synthetic_or_replay"] is True
    assert not any("replay/mock records are present in live lifecycle evidence" in item for item in report["cause_summary"])


def test_duplicate_forensics_blocks_replay_row_claiming_live_broker_lineage(tmp_path: Path) -> None:
    day = "2026-07-21"
    _artifact(
        tmp_path,
        day,
        payload={
            "date": day,
            "user_id": "live_bot",
            "candidates": [],
            "orders": [
                {
                    "timestamp": f"{day}T13:30:00+00:00",
                    "symbol": "CRWV",
                    "action": "buy",
                    "submitted": True,
                    "broker_order_id": "alpaca-real-id",
                    "status": "filled",
                    "filled_qty": 12,
                    "route": "premarket_catalyst_replay",
                    "execution_allowed": True,
                }
            ],
            "exits": [],
        },
    )

    report = td.duplicate_forensics_report(root=tmp_path, day=day, user="live_bot")

    assert report["counts"]["synthetic_or_replay_order_events"] == 1
    assert report["counts"]["unresolved_contamination"] == 1
    assert report["counts"]["contaminated_fill_events"] == 1
    assert report["integrity_status"]["status"] == "CONTAMINATED"


def test_pnl_reconciliation_and_profitability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _artifact(tmp_path)
    monkeypatch.setattr(td, "REPORT_ROOT", tmp_path / "reports")
    report = td.profitability_report(root=tmp_path, start="2026-07-20", end="2026-07-20", user="live_bot")
    assert report["metrics"]["realized_pnl"] == 2.0
    assert report["metrics"]["net_expectancy"] == 2.0
    assert report["sample_size_warning"] is True


def test_forward_return_option_spread_slippage_mfe_mae_and_capture() -> None:
    rec = td.build_underlying_signal_record({"symbol": "AAPL", "price": 100, "forward_return_1m": 0.01, "mfe_pct": 2})
    assert rec["forward_returns"]["1m"] == 0.01
    opt = td.build_option_execution_record({"symbol": "AAPL250815C00200000", "bid": 1.0, "ask": 1.2, "filled_avg_price": 1.15})
    assert opt["underlying_symbol"] == "AAPL"
    assert opt["spread_percentage"] == pytest.approx(18.1818, rel=1e-3)
    assert opt["slippage_from_mid"] == pytest.approx(0.05)
    mfe = td.calculate_mfe_mae([101, 99, 103], entry_price=100)
    assert mfe["mfe_pct"] == pytest.approx(3.0)
    assert mfe["mae_pct"] == pytest.approx(-1.0)
    assert td.mfe_capture_ratio(realized_profit=1, mfe=4) == pytest.approx(0.25)


def test_loss_attribution_classifications() -> None:
    assert td.classify_loss({"pnl": -1, "runtime_error": "boom"})["primary"] == "RUNTIME_FAILURE"
    assert td.classify_loss({"pnl": -1, "spread_pct": 20})["primary"] == "WIDE_SPREAD"
    assert td.classify_loss({"pnl": -1, "mfe_pct": 10})["primary"] == "EXIT_GIVEBACK"
    assert td.classify_loss({"pnl": -1, "underlying_return_pct": 2, "option_return_pct": -5})["primary"] == "BAD_OPTION_CONTRACT"
    assert td.classify_loss({"pnl": -1, "forward_return_15m": -1, "mfe_pct": 0})["primary"] == "BAD_SIGNAL"
    assert td.classify_loss({"pnl": -1}, reconciled=False)["primary"] == "UNRECONCILED"


def test_latency_and_contract_quality_blocks() -> None:
    latencies = td.latency_report(
        {
            "setup_valid_timestamp": "2026-07-20T13:30:00+00:00",
            "candidate_selected_timestamp": "2026-07-20T13:30:05+00:00",
            "entry_eval_started_timestamp": "2026-07-20T13:30:06+00:00",
        }
    )
    assert latencies["latencies"]["setup_valid_to_candidate_selected_seconds"] == 5
    blocks = td.latency_blocks(
        {"underlying_quote_age_seconds": 99, "signal_to_order_seconds": 130},
        {"max_underlying_quote_age_seconds": 60, "max_signal_to_order_seconds": 120},
    )
    assert {b["reason_code"] for b in blocks} == {"ENTRY_BLOCKED_STALE_UNDERLYING_QUOTE", "ENTRY_BLOCKED_EXECUTION_LATENCY"}
    audit = td.contract_quality_audit(
        {"bid": 1, "ask": 1.5, "volume": 1, "open_interest": 1, "delta": 0.1, "dte": 1},
        {"maximum_spread_percentage": 10, "minimum_volume": 10, "minimum_open_interest": 10, "minimum_delta": 0.35, "minimum_dte": 7},
    )
    assert audit["accepted"] is False
    assert "minimum_volume" in audit["rejection_reasons"]


def test_strategy_states_and_readiness(tmp_path: Path) -> None:
    states = strategy_states({"trading_control": {"strategy_states": {"trend_long": "LIVE", "x": "bad"}}})
    assert states["trend_long"].state == "LIVE"
    assert states["x"].state == "SHADOW"
    _artifact(tmp_path)
    report = td.strategy_readiness_report(root=tmp_path, config={"trading_control": {"strategy_states": {"trend_long": "LIVE"}}})
    by_route = {row["route"]: row for row in report["strategies"]}
    assert by_route["trend_long"]["state"] == "LIVE"


def test_daily_learning_protections_and_experiment_single_variable(tmp_path: Path) -> None:
    _artifact(tmp_path)
    learning = td.daily_learning_report(root=tmp_path, day="2026-07-20")
    assert learning["recommendation"] == "INSUFFICIENT_DATA"
    assert "single_variable_required" in td.validate_experiment({"variable": ["a", "b"], "mode": "shadow"})
    experiments_dir = tmp_path / "config"
    experiments_dir.mkdir()
    (experiments_dir / "experiments.yaml").write_text(
        """
experiments:
  - id: spread-cap-shadow
    variable: maximum_spread_percentage
    baseline_value: 10
    candidate_value: 8
    eligible_strategies: [trend_long]
    start_date: 2026-07-20
    mode: shadow
    minimum_sample_size: 30
    success_criterion: positive_net_expectancy_after_costs
    maximum_loss_or_degradation_limit: 50
""",
        encoding="utf-8",
    )
    listed = td.experiment_list_report(tmp_path)
    assert listed["experiments"][0]["validation_errors"] == []
    assert td.experiment_detail_report("spread-cap-shadow", tmp_path)["promotion"] == "manual_only"
    assert td.experiment_detail_report("missing", tmp_path)["status"] == "not_found"


def test_news_edge_report_deduplicates_and_renders_summary(tmp_path: Path) -> None:
    premarket = tmp_path / "data" / "premarket"
    premarket.mkdir(parents=True)
    row = {
        "provider": "alpaca",
        "id": "article-1",
        "headline": "AAPL earnings",
        "published_at": "2026-07-20T12:00:00+00:00",
        "symbols": ["AAPL"],
    }
    (premarket / "a.json").write_text(json.dumps({"events": [row, row]}), encoding="utf-8")
    (premarket / "b.json").write_text(
        json.dumps({"articles": [{**row, "id": "article-2", "published_at": "2026-07-19T12:00:00+00:00"}]}),
        encoding="utf-8",
    )
    report = td.news_edge_report(root=tmp_path, start="2026-07-20", end="2026-07-20")
    assert len(report["events"]) == 1
    assert report["provider_counts"] == {"alpaca": 1}
    rendered = td.render_news_edge_md(report)
    assert "Events: 1" in rendered
    assert "article-1" not in rendered


def test_runtime_exception_entry_blocking_and_circuit_breaker(tmp_path: Path) -> None:
    breaker = EntryCircuitBreaker(threshold=1)

    def explode():
        raise UnboundLocalError("missing reference price")

    assert run_entry_evaluation_safely(explode, data_dir=tmp_path, user_id="live_bot", circuit_breaker=breaker) is None
    assert breaker.open is True
    assert entries_blocked_by_integrity(tmp_path, "live_bot") is True


def test_invariants_persist_incidents(tmp_path: Path) -> None:
    failures = invariant_failures_from_audit([{"kind": "unmatched_fills", "detail": "bad"}])
    assert failures[0].code == "FILL_RECONCILIATION_FAILURE"
    _artifact(tmp_path, payload={"date": "2026-07-20", "user_id": "live_bot", "candidates": [], "orders": [{"symbol": "AAPL", "action": "buy", "submitted": True}], "exits": []})
    assert enforce_daily_invariants(root=tmp_path, day="2026-07-20", user="live_bot")
    assert entries_blocked_by_integrity(tmp_path / "data", "live_bot")


def test_atomic_report_generation_and_cli_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _artifact(tmp_path)
    monkeypatch.setattr(td, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(td, "REPORT_ROOT", tmp_path / "reports")
    assert td.trading_audit_main(["--date", "2026-07-20", "--user", "live_bot", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["fully_reconciled"] is True
    assert (tmp_path / "reports" / "trading_audit" / "2026-07-20.json").exists()
    assert td.profitability_main(["--from", "2026-07-20", "--to", "2026-07-20", "--user", "live_bot", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["metrics"]["realized_pnl"] == 2.0
    premarket = tmp_path / "data" / "premarket"
    premarket.mkdir(parents=True)
    (premarket / "news.json").write_text(
        json.dumps({"events": [{"provider": "sec", "id": "n1", "headline": "Filing", "published_at": "2026-07-20T12:00:00+00:00"}]}),
        encoding="utf-8",
    )
    assert td.news_edge_main(["--from", "2026-07-20", "--to", "2026-07-20"]) == 0
    assert "Events: 1" in capsys.readouterr().out
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "experiments.yaml").write_text(
        """
experiments:
  - id: e1
    variable: maximum_spread_percentage
    baseline_value: 10
    candidate_value: 8
    eligible_strategies: [trend_long]
    start_date: 2026-07-20
    mode: shadow
    minimum_sample_size: 30
    success_criterion: positive_net_expectancy_after_costs
    maximum_loss_or_degradation_limit: 50
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(td, "experiment_list_report", lambda root=td.PROJECT_ROOT: {"experiments": [{"id": "e1"}]})
    monkeypatch.setattr(td, "experiment_detail_report", lambda experiment_id, root=td.PROJECT_ROOT: {"id": experiment_id})
    assert td.experiment_main(["list"]) == 0
    assert json.loads(capsys.readouterr().out)["experiments"][0]["id"] == "e1"
    assert td.experiment_main(["report", "e1"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "e1"
    _artifact(tmp_path, payload={"date": "2026-07-20", "user_id": "live_bot", "candidates": [], "orders": [{"symbol": "AAPL", "action": "buy", "submitted": True}], "exits": []})
    assert td.trading_audit_main(["--date", "2026-07-20", "--user", "live_bot"]) == 1
