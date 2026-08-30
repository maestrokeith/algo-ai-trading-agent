from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace

from src.options_config import options_ordering_allowed
from src.options_position_manager import record_option_entry, record_option_exit
from src.options_safety import (
    build_options_daily_report,
    build_options_promotion_report,
    evaluate_option_contract_liquidity,
    evaluate_option_lifecycle_exit,
    evaluate_options_daily_risk,
    option_underlying_signal_allowed,
)


def test_options_kill_switch_blocks_orders() -> None:
    ok, reason = options_ordering_allowed(
        {"options": {"enabled": False, "mode": "paper_only"}},
        broker_is_paper=True,
    )
    assert ok is False
    assert reason == "options.enabled is false"


def test_paper_only_blocks_live_activation_without_explicit_live_flag() -> None:
    ok, reason = options_ordering_allowed(
        {"options": {"enabled": True, "mode": "paper_only"}},
        broker_is_paper=False,
    )
    assert ok is False
    assert reason == "live options not explicitly enabled"


def test_daily_loss_guard_blocks_new_entries(caplog) -> None:
    caplog.set_level(logging.INFO)
    decision = evaluate_options_daily_risk(
        {"options": {"max_daily_options_loss_dollars": 100}},
        daily_realized_pl=-120,
        daily_unrealized_pl=0,
        equity=50_000,
        symbol="AAPL240621C00190000",
    )
    assert decision.allowed is False
    assert "daily_loss_dollars" in (decision.reason or "")
    assert "OPTIONS_DAILY_RISK_BLOCK" in caplog.text


def test_daily_loss_percent_guard_blocks_new_entries() -> None:
    decision = evaluate_options_daily_risk(
        {"options": {"max_daily_options_loss_percent": 1.0}},
        daily_realized_pl=-600,
        equity=50_000,
    )
    assert decision.allowed is False
    assert "daily_loss_percent" in (decision.reason or "")


def test_max_contracts_per_day_blocks_new_entries(caplog) -> None:
    caplog.set_level(logging.INFO)
    decision = evaluate_options_daily_risk(
        {"options": {"max_option_contracts_per_day": 3}},
        contracts_opened_today=3,
        new_contracts=1,
        symbol="MSFT240621C00420000",
    )
    assert decision.allowed is False
    assert "max_contracts_per_day" in (decision.reason or "")
    assert "OPTIONS_DAILY_RISK_BLOCK" in caplog.text


def test_max_option_positions_blocks_new_entries() -> None:
    decision = evaluate_options_daily_risk(
        {"options": {"max_option_positions": 2}},
        open_option_positions=2,
    )
    assert decision.allowed is False
    assert "max_option_positions" in (decision.reason or "")


def test_liquidity_rejects_wide_spread_low_volume_low_oi_and_logs_reason(caplog) -> None:
    caplog.set_level(logging.INFO)
    cfg = {
        "options": {
            "max_bid_ask_spread_pct": 5,
            "min_option_volume": 100,
            "min_open_interest": 250,
        }
    }
    wide = SimpleNamespace(symbol="AAPL240621C00190000", bid=1.0, ask=1.2, volume=500, open_interest=500)
    assert evaluate_option_contract_liquidity(cfg, wide).reason == "wide_spread"
    assert "OPTIONS_CONTRACT_REJECT" in caplog.text
    assert "option_symbol=AAPL240621C00190000" in caplog.text
    assert "underlying=AAPL" in caplog.text
    assert "bid=1.0" in caplog.text
    assert "ask=1.2" in caplog.text
    assert "spread_pct=" in caplog.text
    assert "volume=500" in caplog.text
    assert "open_interest=500" in caplog.text
    assert "delta=n/a" in caplog.text
    assert "dte=" in caplog.text
    assert "wide_spread" in caplog.text

    low_volume = SimpleNamespace(symbol="AAPL240621C00190000", bid=1.0, ask=1.02, volume=10, open_interest=500)
    assert evaluate_option_contract_liquidity(cfg, low_volume).reason == "low_volume"

    low_oi = SimpleNamespace(symbol="AAPL240621C00190000", bid=1.0, ask=1.02, volume=500, open_interest=10)
    assert evaluate_option_contract_liquidity(cfg, low_oi).reason == "low_open_interest"


def test_liquidity_rejects_stale_or_missing_quote() -> None:
    cfg = {"options": {"quote_stale_max_age_seconds": 60}}
    old = datetime(2026, 6, 21, 14, 0, tzinfo=timezone.utc)
    now = datetime(2026, 6, 21, 14, 2, tzinfo=timezone.utc)
    stale = SimpleNamespace(
        symbol="AAPL240621C00190000",
        bid=1.0,
        ask=1.01,
        volume=500,
        open_interest=500,
        timestamp=old,
    )
    assert evaluate_option_contract_liquidity(cfg, stale, now=now).reason == "stale_quote"

    missing = SimpleNamespace(symbol="AAPL240621C00190000", bid=0.0, ask=1.01, volume=500, open_interest=500)
    assert evaluate_option_contract_liquidity(cfg, missing).reason == "missing_quote"


def test_option_lifecycle_exits_take_profit_stop_loss_max_hold_and_no_overnight() -> None:
    cfg = {
        "options": {
            "exits": {
                "take_profit_pct": 40,
                "stop_loss_pct": 20,
                "max_hold_minutes": 90,
                "allow_overnight": False,
                "force_close_minutes_before_close": 15,
            }
        }
    }
    entry = datetime(2026, 6, 21, 13, 0, tzinfo=timezone.utc)
    now = datetime(2026, 6, 21, 14, 0, tzinfo=timezone.utc)
    assert evaluate_option_lifecycle_exit(cfg, entry_time=entry, now=now, pnl_pct=45).reason == "option_take_profit"
    assert evaluate_option_lifecycle_exit(cfg, entry_time=entry, now=now, pnl_pct=-21).reason == "option_stop_loss"
    assert evaluate_option_lifecycle_exit(cfg, entry_time=entry, now=datetime(2026, 6, 21, 14, 31, tzinfo=timezone.utc), pnl_pct=0).reason == "option_max_hold_minutes"
    near_close_entry = datetime(2026, 6, 21, 15, 0, tzinfo=timezone.utc)
    assert evaluate_option_lifecycle_exit(cfg, entry_time=near_close_entry, now=datetime(2026, 6, 21, 15, 50, tzinfo=timezone.utc), pnl_pct=0).reason == "option_no_overnight"


def test_option_lifecycle_allows_overnight_when_explicit() -> None:
    cfg = {"options": {"exits": {"allow_overnight": True, "max_hold_minutes": 0}}}
    decision = evaluate_option_lifecycle_exit(
        cfg,
        entry_time=datetime(2026, 6, 21, 13, 0, tzinfo=timezone.utc),
        now=datetime(2026, 6, 21, 15, 55, tzinfo=timezone.utc),
        pnl_pct=0,
    )
    assert decision.should_exit is False


def test_underlying_signal_required() -> None:
    assert option_underlying_signal_allowed(None).allowed is False
    assert option_underlying_signal_allowed({"final": False, "route": "trend_long"}).reason == "underlying_signal_not_final"
    assert option_underlying_signal_allowed({"final": True, "route": "trend_long", "risk_allowed": False}).reason == "underlying_risk_gate_failed"
    assert option_underlying_signal_allowed({"final": True, "route": "trend_long", "risk_allowed": True}).allowed is True


def test_daily_report_and_promotion_report_generated(tmp_path) -> None:
    now = datetime(2026, 6, 21, 14, 0, tzinfo=timezone.utc)
    later = datetime(2026, 6, 21, 15, 0, tzinfo=timezone.utc)
    record_option_entry(
        "AAPL260717C00190000",
        user_id="paper_bot",
        data_dir=tmp_path,
        entry_reason="underlying_trend_long",
        intended_limit_price=1.0,
        entry_fill_price=1.0,
        contracts=1,
        premium_paid=100,
        quote_spread_pct=2.0,
        now=now,
    )
    record_option_exit(
        "AAPL260717C00190000",
        user_id="paper_bot",
        data_dir=tmp_path,
        exit_reason="option_take_profit",
        exit_price=1.5,
        realized_pl=50,
        now=later,
    )
    review_dir = tmp_path / "review" / "2026-06-21"
    review_dir.mkdir(parents=True)
    (review_dir / "paper_full.log").write_text(
        "\n".join(
            [
                "OPTIONS_CHAIN_SUMMARY underlying=AAPL direction=call chain_size=42 spot_price=190.12 dte_range_used=14-35 budget_used=125.00 selected_count=1 surviving_contracts=3 top_rejection_reason=budget_fail",
                "OPTION_NEAR_MISS underlying=AAPL option_symbol=AAPL260717C00195000 strike=195 expiration=2026-07-17 call_put=call dte=26 bid=1.2 ask=1.4 mid=1.3 premium=130.00 spread_pct=15.38 volume=80 open_interest=400 rejection_reason=budget_fail",
                "OPTION_SELECTED underlying=AAPL option_symbol=AAPL260717C00190000 strike=190 expiration=2026-07-17 call_put=call dte=26 bid=0.95 ask=1.05 mid=1 premium=100.00 spread_pct=10.00 volume=500 open_interest=1000 ranking_score=8.2 selected_reason=spread_ok",
                "OPTION_ORDER_SUBMITTED symbol=AAPL260717C00190000",
                "OPTION_ORDER_FILLED symbol=AAPL260717C00190000",
            ]
        ),
        encoding="utf-8",
    )
    report = build_options_daily_report(user_id="paper_bot", data_dir=tmp_path, day="2026-06-21")
    assert report["missing_exits"] == 0
    assert report["stuck_positions"] == 0
    assert report["chain_evaluations"][0]["underlying"] == "AAPL"
    assert report["chain_evaluations"][0]["top_rejection_reason"] == "budget_fail"
    assert report["selected_contracts"][0]["option_symbol"] == "AAPL260717C00190000"
    assert report["near_misses"][0]["rejection_reason"] == "budget_fail"
    assert report["rejection_summary"]["budget_fail"] >= 1
    assert report["orders_submitted"] == 1
    assert report["orders_filled"] == 1
    assert report["trades"][0]["underlying"] == "AAPL"
    assert report["trades"][0]["call_put"] == "call"
    assert report["trades"][0]["exit_reason"] == "option_take_profit"
    assert report["trades"][0]["contracts"] == 1
    assert report["trades"][0]["spread_at_entry"] == 2.0
    assert report["trades"][0]["hold_duration_minutes"] == 60.0
    assert report["trades"][0]["hold_duration"] == "60.0 minutes"

    promotion = build_options_promotion_report(
        {"options": {"promotion_min_trades": 1, "promotion_min_profit_factor": 1.0}},
        user_id="paper_bot",
        data_dir=tmp_path,
    )
    assert promotion["total_trades"] == 1
    assert promotion["total_paper_option_trades"] == 1
    assert promotion["promotion_verdict"] == "READY_FOR_EXTENDED_PAPER"
    assert promotion["max_drawdown"] == 0.0
    assert promotion["largest_winner"]["option_symbol"] == "AAPL260717C00190000"
    assert promotion["largest_loser"]["option_symbol"] == "AAPL260717C00190000"
    assert promotion["average_spread_paid"] == 2.0
    assert promotion["average_hold_duration"] == 60.0
    assert promotion["missing_exits"] == 0
    assert promotion["missing_exits_count"] == 0
    assert promotion["stuck_positions"] == 0
    assert promotion["stuck_positions_count"] == 0
    assert promotion["lifecycle_failures"] == 0
    assert promotion["report_only"] is True
    assert promotion["live_options_enabled"] is False
