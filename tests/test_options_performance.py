"""Tests for paper options performance analytics and promotion gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.options_performance import (
    compare_stock_vs_options,
    evaluate_options_promotion_gate,
    format_options_performance_dashboard,
    summarize_options_performance,
)


def _state_with_trades(tmp_path: Path, *, trades: list[dict], daily: dict | None = None) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "meta": {"updated_at": "2026-06-02T14:30:00+00:00", "user_id": "default"},
        "positions": {},
        "history": trades,
        "daily": daily or {},
    }
    with open(data_dir / "options_positions_default.json", "w") as f:
        json.dump(state, f, indent=2)
    return data_dir


def _trade(
    *,
    symbol: str,
    entry_reason: str,
    exit_reason: str,
    realized_pl: float,
    premium_paid: float,
    entry_spread: float,
    entry_limit: float,
    entry_fill: float,
    entry_time: str,
    exit_time: str,
) -> dict:
    return {
        "symbol": symbol,
        "status": "closed",
        "entry_reason": entry_reason,
        "exit_reason": exit_reason,
        "realized_pl": realized_pl,
        "premium_paid": premium_paid,
        "entry_quote_spread_pct": entry_spread,
        "intended_limit_price": entry_limit,
        "entry_fill_price": entry_fill,
        "entry_time": entry_time,
        "exit_time": exit_time,
    }


def test_summarize_options_performance_and_gate_pass(tmp_path: Path) -> None:
    trades = []
    for i in range(30):
        trades.append(
            _trade(
                symbol="HPE" if i % 2 == 0 else "OKTA",
                entry_reason="source=dynamic_universe; catalyst=deal" if i % 3 == 0 else "source=dynamic_universe; catalyst=earnings",
                exit_reason="option_profit_take" if i < 24 else "option_stop_loss" if i < 27 else "option_end_of_day",
                realized_pl=60.0 if i < 24 else -20.0 if i < 27 else 15.0,
                premium_paid=300.0,
                entry_spread=6.0,
                entry_limit=1.50,
                entry_fill=1.53,
                entry_time=f"2026-06-01T13:{i:02d}:00+00:00",
                exit_time=f"2026-06-01T14:{i:02d}:00+00:00",
            )
        )
    daily = {
        "2026-05-28": {"kill_switch_on": False, "block_new_entries": False},
        "2026-05-29": {"kill_switch_on": False, "block_new_entries": False},
        "2026-05-30": {"kill_switch_on": False, "block_new_entries": False},
        "2026-06-01": {"kill_switch_on": False, "block_new_entries": False},
        "2026-06-02": {"kill_switch_on": False, "block_new_entries": False},
    }
    data_dir = _state_with_trades(tmp_path, trades=trades, daily=daily)
    summary = summarize_options_performance(data_dir=data_dir)
    assert summary.total_trades == 30
    assert summary.total_pnl == pytest.approx(1425.0)
    assert summary.avg_trade_pnl == pytest.approx(47.5)
    assert summary.best_contract is not None
    assert summary.worst_contract is not None
    assert summary.win_rate > 0
    assert summary.profit_factor >= 1.3
    assert summary.max_drawdown_pct <= 3.0
    assert summary.avg_entry_spread_pct <= 8.0
    passed, reasons = evaluate_options_promotion_gate(summary)
    assert passed is True
    assert reasons == []


def test_summarize_options_performance_and_gate_fail(tmp_path: Path) -> None:
    trades = [
        _trade(
            symbol="DXST",
            entry_reason="source=dynamic_universe; catalyst=none",
            exit_reason="option_stop_loss",
            realized_pl=-120.0,
            premium_paid=300.0,
            entry_spread=10.0,
            entry_limit=1.50,
            entry_fill=1.60,
            entry_time="2026-06-01T13:00:00+00:00",
            exit_time="2026-06-01T14:00:00+00:00",
        )
        for _ in range(2)
    ]
    daily = {
        "2026-05-29": {"kill_switch_on": True, "block_new_entries": True},
        "2026-05-30": {"kill_switch_on": False, "block_new_entries": False},
        "2026-05-31": {"kill_switch_on": False, "block_new_entries": False},
        "2026-06-01": {"kill_switch_on": False, "block_new_entries": False},
        "2026-06-02": {"kill_switch_on": False, "block_new_entries": False},
    }
    data_dir = _state_with_trades(tmp_path, trades=trades, daily=daily)
    summary = summarize_options_performance(data_dir=data_dir)
    passed, reasons = evaluate_options_promotion_gate(summary)
    assert passed is False
    assert any("need at least 30" in r for r in reasons)
    assert any("profit factor" in r for r in reasons)
    assert any("max drawdown" in r for r in reasons)
    assert any("kill-switch days" in r for r in reasons)
    assert any("average entry spread" in r for r in reasons)


def test_compare_stock_vs_options_and_dashboard(tmp_path: Path) -> None:
    trades = [
        _trade(
            symbol="QQQ260619C00350000",
            entry_reason="source=dynamic_universe; catalyst=ai",
            exit_reason="option_profit_take",
            realized_pl=100.0,
            premium_paid=250.0,
            entry_spread=4.0,
            entry_limit=1.25,
            entry_fill=1.26,
            entry_time="2026-06-01T13:00:00+00:00",
            exit_time="2026-06-01T14:30:00+00:00",
        ),
        _trade(
            symbol="HPE260619C00020000",
            entry_reason="source=dynamic_universe; catalyst=earnings",
            exit_reason="option_stop_loss",
            realized_pl=-30.0,
            premium_paid=150.0,
            entry_spread=5.0,
            entry_limit=0.75,
            entry_fill=0.76,
            entry_time="2026-06-01T15:00:00+00:00",
            exit_time="2026-06-01T15:30:00+00:00",
        ),
    ]
    data_dir = _state_with_trades(tmp_path, trades=trades)
    summary = summarize_options_performance(data_dir=data_dir)

    comparison = compare_stock_vs_options(
        summary,
        [
            {"symbol": "AAPL", "pnl": 25.0, "return_pct": 1.0},
            {"symbol": "MSFT", "realized_pnl": -10.0, "filled_qty": 1, "filled_avg_price": 100.0},
            {"symbol": "QQQ260619C00350000", "pnl": 999.0},
        ],
    )
    dashboard = format_options_performance_dashboard(summary, comparison=comparison)

    assert summary.total_pnl == pytest.approx(70.0)
    assert summary.avg_hold_time_minutes == pytest.approx(60.0)
    assert comparison.stocks.total_trades == 2
    assert comparison.stocks.total_pnl == pytest.approx(15.0)
    assert comparison.pnl_edge == "options"
    assert "Options performance dashboard" in dashboard
    assert "best_contract=QQQ260619C00350000" in dashboard
    assert "Stock vs option edge: pnl=options" in dashboard
