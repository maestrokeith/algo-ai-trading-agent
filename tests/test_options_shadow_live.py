from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.brokers.alpaca_client import OptionContractCandidate, QuoteInfo
from src.live.options_shadow import (
    ShadowReportSummary,
    attempt_shadow_option_entry,
    manage_shadow_option_positions,
    shadow_live_options_active,
    shadow_state_path,
    summarize_shadow_report,
)


def _config() -> dict:
    return {
        "user": {"id": "shadow"},
        "options": {
            "enabled": True,
            "mode": "shadow_live",
            "allowed_underlyings": ["HPE"],
            "entry_mapping": {"bullish_signal": "call", "bearish_signal": "put"},
            "per_trade": 0.005,
            "total_exposure_limit": 0.05,
            "max_bid_ask_spread_pct": 8.0,
            "target_dte_min": 7,
            "target_dte_max": 21,
            "max_option_position_pct": 0.005,
            "max_option_positions": 3,
            "max_daily_loss_pct": 1.0,
            "only_buy_options": True,
            "allow_0dte": False,
            "allow_weeklies": True,
            "contract_selection": {"min_open_interest": 500, "min_volume": 100},
            "exits": {"automation_enabled": True, "profit_take_pct": 10, "stop_loss_pct": 20},
        },
    }


class _Broker:
    def __init__(self, quote: QuoteInfo | None = None, underlying_quote: QuoteInfo | None = None) -> None:
        self.quote = quote
        self.underlying_quote = underlying_quote

    def get_option_latest_quote(self, symbol):  # type: ignore[no-untyped-def]
        return self.quote

    def get_latest_quote(self, symbol):  # type: ignore[no-untyped-def]
        return self.underlying_quote


class _Exec:
    def build_order_for_entry(self, symbol, side, quantity, mid_price, spread_pct, **kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(limit_price=1.05, symbol=symbol, side=side, qty=quantity)

    def build_order(self, symbol, side, quantity, mid_price, spread_pct, **kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(limit_price=1.15, symbol=symbol, side=side, qty=quantity)


def _chain() -> list[OptionContractCandidate]:
    return [
        OptionContractCandidate(
            symbol="HPE260619C00020000",
            strike=20.0,
            expiration=date(2026, 6, 19),
            right="call",
            open_interest=1200,
            volume=300,
            bid=0.93,
            ask=1.00,
            delta=0.50,
            iv=0.35,
        )
    ]


def test_shadow_active_gate() -> None:
    assert shadow_live_options_active(_config()) is True
    assert shadow_live_options_active({"options": {"enabled": True, "mode": "paper_only"}}) is False


def test_shadow_entry_records_fill_and_avoids_broker_submit(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO")
    broker = _Broker()
    exec_mgr = _Exec()
    now = datetime(2026, 6, 2, 9, 45, tzinfo=timezone.utc)
    res = attempt_shadow_option_entry(
        _config(),
        broker=broker,
        execution_manager=exec_mgr,
        symbol="HPE",
        dt=now,
        current_price=20.0,
        session_vwap=19.0,
        account_equity=100_000.0,
        positions=[],
        source="dynamic_universe",
        conviction_score=0.8,
        news_score=4.0,
        event_score=2.5,
        chain_candidates=_chain(),
        user_id="shadow",
        data_dir=tmp_path,
    )

    assert res.intended is True
    assert res.filled is True
    assert res.symbol == "HPE260619C00020000"
    state = shadow_state_path("shadow", data_dir=tmp_path)
    assert state.exists()
    assert "OPTIONS_SHADOW_ORDER_INTENDED" in caplog.text
    assert "OPTIONS_SHADOW_ORDER_RESULT" in caplog.text
    assert "filled=true" in caplog.text
    report = summarize_shadow_report(user_id="shadow", data_dir=tmp_path, now=now)
    assert isinstance(report, ShadowReportSummary)
    assert report.total_intents == 1
    assert report.total_fills == 1
    assert report.open_positions == 1


def test_shadow_entry_unfilled_does_not_open_position(tmp_path: Path) -> None:
    broker = _Broker()
    exec_mgr = SimpleNamespace(build_order_for_entry=lambda *args, **kwargs: SimpleNamespace(limit_price=0.80))
    now = datetime(2026, 6, 2, 9, 45, tzinfo=timezone.utc)
    res = attempt_shadow_option_entry(
        _config(),
        broker=broker,
        execution_manager=exec_mgr,
        symbol="HPE",
        dt=now,
        current_price=20.0,
        session_vwap=19.0,
        account_equity=100_000.0,
        positions=[],
        source="dynamic_universe",
        conviction_score=0.8,
        news_score=4.0,
        event_score=2.5,
        chain_candidates=_chain(),
        user_id="shadow",
        data_dir=tmp_path,
    )

    assert res.intended is True
    assert res.filled is False
    report = summarize_shadow_report(user_id="shadow", data_dir=tmp_path, now=now)
    assert report.total_intents == 1
    assert report.total_fills == 0
    assert report.open_positions == 0


def test_shadow_exit_closes_position(tmp_path: Path) -> None:
    now = datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc)
    state = {
        "meta": {},
        "positions": {
            "HPE260619C00020000": {
                "symbol": "HPE260619C00020000",
                "underlying": "HPE",
                "right": "call",
                "status": "open",
                "qty": 1,
                "entry_time": "2026-06-02T13:00:00+00:00",
                "entry_price": 1.00,
                "entry_fill_price": 1.00,
                "premium_paid": 100.0,
                "cost_basis": -100.0,
                "current_price": 1.00,
                "current_value": 100.0,
                "unrealized_pl": 0.0,
            }
        },
        "history": [],
        "daily": {},
    }
    path = shadow_state_path("shadow", data_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(__import__("json").dumps(state))
    broker = _Broker(
        quote=QuoteInfo(bid=1.20, ask=1.30, mid=1.25, spread_pct=8.0),
        underlying_quote=QuoteInfo(bid=21.0, ask=21.1, mid=21.05, spread_pct=0.5),
    )
    exec_mgr = _Exec()
    manage_shadow_option_positions(
        broker=broker,
        config=_config(),
        user_id="shadow",
        data_dir=tmp_path,
        now=now,
        execution_manager=exec_mgr,
    )
    report = summarize_shadow_report(user_id="shadow", data_dir=tmp_path, now=now)
    assert report.closed_positions == 1
    assert report.realized_pl > 0
