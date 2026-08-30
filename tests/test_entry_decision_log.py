"""Tests for structured entry skip JSON (src/entry_decision_log)."""
from __future__ import annotations

import json
from datetime import datetime

import pytest
import pytz

from src import entry_decision_log as edl


def _dt() -> datetime:
    et = pytz.timezone("America/New_York")
    return datetime(2026, 4, 10, 10, 0, 0, tzinfo=et)


def test_emit_entry_skip_json_shape(capsys: pytest.CaptureFixture[str]) -> None:
    edl.set_entry_skip_runtime_context(
        user_id="u1",
        config={"entries": {"structured_skip_logs": True}},
        regime="bullish",
        position_count=10,
        cash_available=0.0,
        signal_default="trend_long",
    )
    edl.emit_entry_skip(
        _dt(),
        "SPY",
        "trend longs blocked — no long SQQQ (universe.require_sqqq_for_trend_long_entries)",
        verbose=True,
        force=True,
        signal="trend_long",
        reason_code="requires_sqqq_position",
    )
    out = capsys.readouterr().out.strip()
    row = json.loads(out)
    assert row["symbol"] == "SPY"
    assert row["signal"] == "trend_long"
    assert row["decision"] == "skip"
    assert row["reason"] == "requires_sqqq_position"
    assert row["regime"] == "bullish"
    assert row["position_count"] == 10
    assert row["cash_available"] == 0.0
    assert row["user_id"] == "u1"
    assert "ts" in row


def test_emit_legacy_when_structured_off(capsys: pytest.CaptureFixture[str]) -> None:
    edl.set_entry_skip_runtime_context(
        config={"entries": {"structured_skip_logs": False}},
    )
    edl.emit_entry_skip(_dt(), "QQQ", "open order pending", verbose=True, force=True)
    out = capsys.readouterr().out
    assert "QQQ skip — open order pending" in out


def test_emit_options_fallback_to_stock_json(capsys: pytest.CaptureFixture[str]) -> None:
    edl.set_entry_skip_runtime_context(
        user_id="u1",
        config={"entries": {"structured_skip_logs": True}},
        regime="bullish",
        position_count=2,
        cash_available=5000.0,
        signal_default="trend_long",
    )
    edl.emit_options_fallback_to_stock(_dt(), "nvda", signal="trend_long")
    row = json.loads(capsys.readouterr().out.strip())
    assert row["symbol"] == "NVDA"
    assert row["signal"] == "trend_long"
    assert row["decision"] == "route"
    assert row["reason"] == "options_fallback_to_stock"
    assert row["regime"] == "bullish"
    assert row["position_count"] == 2
    assert row["cash_available"] == 5000.0


def test_emit_options_fallback_legacy_line(capsys: pytest.CaptureFixture[str]) -> None:
    edl.set_entry_skip_runtime_context(
        config={"entries": {"structured_skip_logs": False}},
    )
    edl.emit_options_fallback_to_stock(_dt(), "QQQ", signal="bear_etf")
    assert "QQQ options → fallback to stock" in capsys.readouterr().out


def test_emit_options_trade_stock_json(capsys: pytest.CaptureFixture[str]) -> None:
    edl.set_entry_skip_runtime_context(
        user_id="u1",
        config={"entries": {"structured_skip_logs": True}},
        regime="bullish",
        position_count=2,
        cash_available=100.0,
    )
    edl.emit_options_trade_stock(
        _dt(),
        "aapl",
        signal="trend_long",
        detail="underlying not in allowed_underlyings",
    )
    row = json.loads(capsys.readouterr().out.strip())
    assert row["symbol"] == "AAPL"
    assert row["signal"] == "trend_long"
    assert row["decision"] == "route"
    assert row["reason"] == "options_not_allowed_trade_stock"
    assert row["detail"] == "underlying not in allowed_underlyings"


def test_emit_options_trade_stock_legacy_line(capsys: pytest.CaptureFixture[str]) -> None:
    edl.set_entry_skip_runtime_context(
        config={"entries": {"structured_skip_logs": False}},
    )
    edl.emit_options_trade_stock(_dt(), "MSFT", signal="bear_etf", detail="mode off")
    out = capsys.readouterr().out
    assert "MSFT trade stock (bear_etf)" in out
    assert "mode off" in out


def test_reason_slug_open_order_pending(capsys: pytest.CaptureFixture[str]) -> None:
    edl.set_entry_skip_runtime_context(
        user_id="default",
        config={"entries": {"structured_skip_logs": True}},
        regime=None,
        position_count=3,
        cash_available=100.5,
        signal_default="trend_long",
    )
    edl.emit_entry_skip(
        _dt(), "MSFT", "open order pending", verbose=True, force=True, signal="trend_long"
    )
    row = json.loads(capsys.readouterr().out.strip())
    assert row["reason"] == "open_order_pending"
    assert row["symbol"] == "MSFT"
    assert row["position_count"] == 3
    assert row["cash_available"] == 100.5
    assert row["regime"] is None
