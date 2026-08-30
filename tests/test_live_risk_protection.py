from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.live_risk_protection import (
    build_live_risk_guard_state,
    consecutive_live_trend_long_losses,
    intraday_loss_guard,
    sleeve_adaptive_sizing,
    sleeve_weak_exit_blocks,
)
from src.strategies.exits.profit_protection import (
    live_profit_protection_decision,
    live_time_stop_not_green_decision,
)
from src.trade_attribution import record_exit


def _write_profitability(data_dir: Path, day: str, *, win_rate: float, pnl: float) -> None:
    path = data_dir / "profitability_attribution" / "daily" / f"{day}_live_bot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "route_stats": {
                    "trend_long": {
                        "trades": 2,
                        "win_rate": win_rate,
                        "realized_pnl": pnl,
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def test_consecutive_live_trend_long_losses_blocks_next_session(tmp_path: Path) -> None:
    _write_profitability(tmp_path, "2026-07-06", win_rate=0.0, pnl=-25.0)
    _write_profitability(tmp_path, "2026-07-07", win_rate=0.0, pnl=-12.0)

    result = consecutive_live_trend_long_losses(
        data_dir=tmp_path,
        user_id="live_bot",
        session_day="2026-07-08",
    )

    assert result["triggered"] is True
    assert result["reason"] == "two_live_trend_long_zero_win_loss_sessions"
    assert [row["date"] for row in result["sessions"]] == ["2026-07-07", "2026-07-06"]


def test_consecutive_loss_guard_requires_two_sessions(tmp_path: Path) -> None:
    _write_profitability(tmp_path, "2026-07-06", win_rate=0.5, pnl=20.0)
    _write_profitability(tmp_path, "2026-07-07", win_rate=0.0, pnl=-12.0)

    result = consecutive_live_trend_long_losses(
        data_dir=tmp_path,
        user_id="live_bot",
        session_day="2026-07-08",
    )

    assert result["triggered"] is False


@pytest.mark.parametrize(
    ("total_pnl", "expected"),
    [
        (-349.0, "allow"),
        (-350.0, "stop_entries"),
        (-500.0, "flatten"),
    ],
)
def test_intraday_loss_guard_thresholds(total_pnl: float, expected: str) -> None:
    result = intraday_loss_guard(
        realized_pnl=total_pnl,
        unrealized_pnl=0.0,
        account_equity=100000.0,
    )

    assert result["action"] == expected


def test_sleeve_churn_guard_blocks_after_three_weak_exits(tmp_path: Path) -> None:
    ts = datetime(2026, 7, 8, 15, 0, tzinfo=timezone.utc)
    for symbol in ("AAPL", "MSFT", "NVDA"):
        record_exit(
            data_dir=tmp_path,
            user_id="live_bot",
            timestamp=ts,
            symbol=symbol,
            exit_reason="signal_flip",
            pnl=-2.0,
            entry_route="trend_long",
        )
    record_exit(
        data_dir=tmp_path,
        user_id="live_bot",
        timestamp=ts,
        symbol="FHTX",
        exit_reason="take_profit",
        pnl=5.0,
        entry_route="dynamic_momentum",
    )

    blocks = sleeve_weak_exit_blocks(
        data_dir=tmp_path,
        user_id="live_bot",
        day=date(2026, 7, 8),
    )

    assert blocks == {"trend_long": 3}

    sizing = sleeve_adaptive_sizing(
        data_dir=tmp_path,
        user_id="live_bot",
        day=date(2026, 7, 8),
    )
    assert sizing["trend_long"]["blocked"] is True
    assert sizing["trend_long"]["multiplier"] == 0.0


def test_build_live_risk_guard_state_keeps_dynamic_lane_available_for_trend_loss_guard(tmp_path: Path) -> None:
    _write_profitability(tmp_path, "2026-07-06", win_rate=0.0, pnl=-25.0)
    _write_profitability(tmp_path, "2026-07-07", win_rate=0.0, pnl=-12.0)

    state = build_live_risk_guard_state(
        data_dir=tmp_path,
        user_id="live_bot",
        session_day="2026-07-08",
        account_equity=100000.0,
        positions=[],
    )

    assert state.trend_long_entries_blocked is True
    assert state.new_entries_blocked is False
    assert state.flatten_risk is False
    assert state.triggered_guards == ("trend_long_consecutive_losses",)


def test_adaptive_sleeve_multiplier_after_one_and_two_losses(tmp_path: Path) -> None:
    ts = datetime(2026, 7, 8, 15, 0, tzinfo=timezone.utc)
    record_exit(data_dir=tmp_path, user_id="live_bot", timestamp=ts, symbol="AAPL", exit_reason="stop_loss", pnl=-2.0, entry_route="trend_long")
    record_exit(data_dir=tmp_path, user_id="live_bot", timestamp=ts, symbol="MSFT", exit_reason="stop_loss", pnl=-2.0, entry_route="dynamic_momentum")
    record_exit(data_dir=tmp_path, user_id="live_bot", timestamp=ts, symbol="NVDA", exit_reason="signal_flip", pnl=-2.0, entry_route="dynamic_momentum")

    state = build_live_risk_guard_state(
        data_dir=tmp_path,
        user_id="live_bot",
        session_day="2026-07-08",
        account_equity=100000.0,
        positions=[],
    )

    assert state.sleeve_size_multipliers["trend_long"] == 0.5
    assert state.sleeve_size_multipliers["dynamic_momentum"] == 0.25
    assert state.sleeve_blocks == {}


def test_build_live_risk_guard_state_blocks_all_entries_and_flatten_on_deeper_loss(tmp_path: Path) -> None:
    ts = datetime(2026, 7, 8, 15, 0, tzinfo=timezone.utc)
    record_exit(
        data_dir=tmp_path,
        user_id="live_bot",
        timestamp=ts,
        symbol="SPY",
        exit_reason="stop_loss",
        pnl=-300.0,
        entry_route="trend_long",
    )

    state = build_live_risk_guard_state(
        data_dir=tmp_path,
        user_id="live_bot",
        session_day="2026-07-08",
        account_equity=100000.0,
        positions=[{"symbol": "QQQ", "unrealized_pl": -250.0}],
    )

    assert state.new_entries_blocked is True
    assert state.flatten_risk is True
    assert "intraday_loss_flatten" in state.triggered_guards
    assert state.total_pnl == pytest.approx(-550.0)


def test_profit_protection_breakeven_trailing_and_partial() -> None:
    cfg = {"live_risk_protection": {"profit_protection": {"enabled": True}}}

    breakeven = live_profit_protection_decision(
        config=cfg,
        position={"trail_high": 100.6},
        entry_price=100.0,
        current_price=100.0,
        qty=10,
    )
    assert breakeven["action"] == "full_exit"
    assert breakeven["reason"] == "breakeven_stop"

    trailing = live_profit_protection_decision(
        config=cfg,
        position={"trail_high": 101.2},
        entry_price=100.0,
        current_price=100.6,
        qty=10,
    )
    assert trailing["action"] == "full_exit"
    assert trailing["reason"] == "profit_trailing_stop"

    partial = live_profit_protection_decision(
        config=cfg,
        position={"trail_high": 100.0, "partial_taken": False},
        entry_price=100.0,
        current_price=101.5,
        qty=10,
    )
    assert partial["action"] == "partial_exit"
    assert partial["qty"] == pytest.approx(5.0)


def test_time_stop_exits_trades_not_green_after_15_minutes() -> None:
    cfg = {"live_risk_protection": {"profit_protection": {"time_stop_not_green_minutes": 15}}}

    assert live_time_stop_not_green_decision(
        config=cfg,
        minutes_held=15,
        pnl_percent_points=0.0,
    ) is True
    assert live_time_stop_not_green_decision(
        config=cfg,
        minutes_held=14.9,
        pnl_percent_points=-0.1,
    ) is False
    assert live_time_stop_not_green_decision(
        config=cfg,
        minutes_held=20,
        pnl_percent_points=0.1,
    ) is False
