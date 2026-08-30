from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.profitability_attribution import (
    ROUTE_BUCKETS,
    build_profitability_report,
    build_trade_churn_analysis,
    format_profitability_report,
    load_trade_churn_analysis,
    load_profitability_report_inputs,
    profitability_daily_path,
    write_profitability_report,
)
from src.trade_attribution import record_exit, record_order_event


def _fixed_now() -> datetime:
    return datetime(2026, 6, 6, 20, 0, tzinfo=timezone.utc)


def test_profitability_report_mixed_route_profitability() -> None:
    attribution = {
        "exits": [
            {"symbol": "AAPL", "entry_route": "core_rebuild", "pnl": 100.0, "pnl_pct": 5.0},
            {"symbol": "IREZ", "entry_route": "dynamic_universe", "pnl": -40.0, "pnl_pct": -2.0},
            {"symbol": "MSFT", "entry_route": "trend_long", "pnl": 60.0, "pnl_pct": 3.0},
            {"symbol": "OPT", "entry_route": "options_paper", "pnl": -20.0, "pnl_pct": -10.0},
            {"symbol": "ROT", "entry_route": "allocator_rotation", "pnl": 30.0, "pnl_pct": 1.5},
            {"symbol": "UNK", "entry_route": "something_else", "pnl": 10.0, "pnl_pct": 1.0},
        ]
    }

    report = build_profitability_report(
        user_id="live_bot",
        day="2026-06-06",
        attribution_payload=attribution,
        daily_summary_payload={"unrealized_pnl": 15.0},
        generated_at=_fixed_now(),
    )

    assert report["overall_pnl"] == {"realized": 140.0, "unrealized": 15.0, "total": 155.0}
    assert report["pnl_by_route"] == {
        "core_rebuild": 100.0,
        "dynamic_momentum": -40.0,
        "trend_long": 60.0,
        "options_paper": -20.0,
        "allocator_rotation": 30.0,
        "unknown": 10.0,
    }
    assert report["route_stats"]["core_rebuild"]["trades"] == 1
    assert report["route_stats"]["core_rebuild"]["wins"] == 1
    assert report["route_stats"]["dynamic_momentum"]["losses"] == 1
    assert report["exit_reason_stats"]["unknown"]["exits"] == 6
    assert report["top_winners"][0]["symbol"] == "AAPL"
    assert report["top_losers"][0]["symbol"] == "IREZ"


def test_profitability_report_missing_attribution_uses_order_history() -> None:
    order_history = {
        "orders": [
            {"symbol": "BBCP", "strategy": "dynamic_momentum", "realized_pnl": 25.0, "return_pct": 2.5},
            {"symbol": "SPY", "strategy": "trend_long", "profit_loss": -5.0, "return_pct": -0.2},
        ]
    }

    report = build_profitability_report(
        user_id="default",
        day="2026-06-06",
        attribution_payload=None,
        order_history_payload=order_history,
        daily_summary_payload={"unrealized": 7.5},
        generated_at=_fixed_now(),
    )

    assert report["inputs"]["realized_trade_source"] == "order_history"
    assert report["overall_pnl"] == {"realized": 20.0, "unrealized": 7.5, "total": 27.5}
    assert report["pnl_by_route"]["dynamic_momentum"] == 25.0
    assert report["pnl_by_route"]["trend_long"] == -5.0


def test_profitability_report_no_trades() -> None:
    report = build_profitability_report(
        user_id="default",
        day="2026-06-06",
        attribution_payload={"exits": []},
        order_history_payload={"orders": []},
        daily_summary_payload={"realized_pnl": 0.0, "unrealized_pnl": 0.0},
        generated_at=_fixed_now(),
    )

    assert report["overall_pnl"] == {"realized": 0.0, "unrealized": 0.0, "total": 0.0}
    assert report["top_winners"] == []
    assert report["top_losers"] == []
    assert set(report["route_stats"]) == set(ROUTE_BUCKETS)
    assert all(row["trades"] == 0 for row in report["route_stats"].values())


def test_profitability_report_winning_and_losing_trade_stats() -> None:
    attribution = {
        "exits": [
            {"symbol": "A", "entry_route": "core_rebuild", "pnl": 100.0},
            {"symbol": "B", "entry_route": "core_rebuild", "pnl": 50.0},
            {"symbol": "C", "entry_route": "core_rebuild", "pnl": -30.0},
        ]
    }

    report = build_profitability_report(
        user_id="default",
        day="2026-06-06",
        attribution_payload=attribution,
        generated_at=_fixed_now(),
    )

    stats = report["route_stats"]["core_rebuild"]
    assert stats["trades"] == 3
    assert stats["wins"] == 2
    assert stats["losses"] == 1
    assert stats["win_rate"] == pytest.approx(2 / 3)
    assert stats["avg_gain"] == pytest.approx(75.0)
    assert stats["avg_loss"] == pytest.approx(-30.0)
    assert stats["profit_factor"] == pytest.approx(5.0)


def test_profitability_json_output_validation(tmp_path: Path) -> None:
    report = build_profitability_report(
        user_id="live_bot",
        day="2026-06-06",
        attribution_payload={"exits": [{"symbol": "AAPL", "entry_route": "core_rebuild", "pnl": 12.5}]},
        generated_at=_fixed_now(),
    )

    path = write_profitability_report(report, data_dir=tmp_path, user_id="live_bot", day="2026-06-06")
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert path == profitability_daily_path(data_dir=tmp_path, user_id="live_bot", day="2026-06-06")
    assert saved["version"] == 1
    assert saved["date"] == "2026-06-06"
    assert saved["user_id"] == "live_bot"
    assert set(saved["overall_pnl"]) == {"realized", "unrealized", "total"}
    assert set(saved["pnl_by_route"]) == set(ROUTE_BUCKETS)
    assert set(saved["route_stats"]) == set(ROUTE_BUCKETS)


def test_profitability_cli_format_includes_required_sections() -> None:
    report = build_profitability_report(
        user_id="live_bot",
        day="2026-06-06",
        attribution_payload={"exits": [{"symbol": "AAPL", "entry_route": "core_rebuild", "pnl": 12.5}]},
        generated_at=_fixed_now(),
    )

    text = format_profitability_report(report)

    assert "Overall PnL" in text
    assert "PnL by route" in text
    assert "Trade stats per route" in text
    assert "Top winners" in text
    assert "AAPL core_rebuild $12.50" in text


def test_profitability_input_loader_reads_existing_artifacts(tmp_path: Path) -> None:
    now = datetime(2026, 6, 6, 15, 30, tzinfo=timezone.utc)
    record_exit(
        data_dir=tmp_path,
        user_id="live_bot",
        timestamp=now,
        symbol="AAPL",
        entry_route="core_rebuild",
        pnl=10.0,
    )
    order_path = tmp_path / "orders.json"
    order_path.write_text(json.dumps({"orders": [{"symbol": "MSFT", "pnl": 2.0}]}), encoding="utf-8")
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps({"unrealized_pnl": 3.0}), encoding="utf-8")

    attribution, orders, summary = load_profitability_report_inputs(
        data_dir=tmp_path,
        user_id="live_bot",
        day="2026-06-06",
        order_history_path=order_path,
        daily_summary_path=summary_path,
    )

    assert attribution is not None
    assert attribution["exits"][0]["symbol"] == "AAPL"
    assert orders["orders"][0]["symbol"] == "MSFT"
    assert summary["unrealized_pnl"] == 3.0


def test_trade_churn_analysis_counts_reversals_repeats_and_weak_exits() -> None:
    report = build_trade_churn_analysis(
        user_id="live_bot",
        day="2026-06-06",
        attribution_payload={
            "orders": [
                {"symbol": "AAPL", "action": "buy", "submitted": True},
                {"symbol": "AAPL", "action": "buy", "submitted": True},
                {"symbol": "MSFT", "action": "sell", "submitted": True},
            ],
            "exits": [
                {
                    "symbol": "AAPL",
                    "entry_route": "core_rebuild",
                    "exit_reason": "signal_flip",
                    "pnl": -4.0,
                    "pnl_pct": -0.5,
                    "hold_minutes": 12,
                },
                {
                    "symbol": "MSFT",
                    "entry_route": "trend_long",
                    "exit_reason": "take_profit",
                    "pnl": 20.0,
                    "pnl_pct": 3.0,
                    "hold_minutes": 180,
                },
            ],
        },
        replay_payload={
            "mock_orders": [
                {"symbol": "AAPL", "side": "sell"},
                {"symbol": "MSFT", "side": "sell"},
            ]
        },
        generated_at=_fixed_now(),
    )

    assert report["order_activity"]["buy_counts_by_symbol"] == {"AAPL": 2}
    assert report["same_day_reversals"] == {"count": 1, "symbols": ["AAPL"]}
    assert report["repeated_activity"]["repeated_buy_symbols"] == ["AAPL"]
    assert report["repeated_activity"]["repeated_sell_symbols"] == ["AAPL", "MSFT"]
    assert report["weak_exits"]["count"] == 1
    assert report["weak_exits"]["by_reason"] == {"signal_flip": 1}
    assert report["weak_exits"]["by_route"] == {"core_rebuild": 1}


def test_load_trade_churn_analysis_reads_attribution_and_replay_artifacts(tmp_path: Path) -> None:
    now = datetime(2026, 6, 6, 15, 30, tzinfo=timezone.utc)
    record_order_event(
        data_dir=tmp_path,
        user_id="live_bot",
        timestamp=now,
        symbol="AAPL",
        action="buy",
        order_build_status="built",
        submitted=True,
    )
    record_exit(
        data_dir=tmp_path,
        user_id="live_bot",
        timestamp=now,
        symbol="AAPL",
        exit_reason="stop_loss",
        pnl=-3.0,
        hold_minutes=8,
        entry_route="core_rebuild",
    )
    replay_dir = tmp_path / "replay_market_session"
    replay_dir.mkdir(parents=True)
    (replay_dir / "2026-06-06_live_bot.json").write_text(
        json.dumps({"mock_orders": [{"symbol": "AAPL", "side": "sell"}]}),
        encoding="utf-8",
    )

    report = load_trade_churn_analysis(
        data_dir=tmp_path,
        user_id="live_bot",
        day="2026-06-06",
        generated_at=_fixed_now(),
    )

    assert report["inputs"] == {"trade_attribution_available": True, "replay_available": True}
    assert report["same_day_reversals"]["symbols"] == ["AAPL"]
    assert report["weak_exits"]["rows"][0]["symbol"] == "AAPL"
