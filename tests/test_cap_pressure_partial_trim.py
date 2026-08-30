"""Tests for portfolio.cap_pressure_trim partial sells before rotation."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.portfolio_replacement import parse_cap_pressure_trim_cfg, portfolio_budget_cap_sizing_reject
from src.strategy import EntrySignal
from src.trading_engine import TradeDecision
from src.trend_long_ranked_dispatch import (
    consider_replacement_for_sizing_reject,
    execute_cap_pressure_partial_trim,
)


def test_parse_cap_pressure_trim_cfg_missing_off() -> None:
    enabled, frac, mx = parse_cap_pressure_trim_cfg({})
    assert enabled is False
    assert frac == pytest.approx(0.15)


def test_parse_cap_pressure_trim_cfg_enabled() -> None:
    enabled, frac, mx = parse_cap_pressure_trim_cfg(
        {"cap_pressure_trim": {"enabled": True, "trim_frac": 0.12, "max_symbols_per_cycle": 8}}
    )
    assert enabled is True
    assert frac == pytest.approx(0.12)
    assert mx == 8


def test_parse_cap_pressure_trim_frac_clamped() -> None:
    _, frac, _ = parse_cap_pressure_trim_cfg(
        {"cap_pressure_trim": {"enabled": True, "trim_frac": 0.99}}
    )
    assert frac == pytest.approx(0.20)


def test_portfolio_budget_cap_sizing_reject() -> None:
    assert portfolio_budget_cap_sizing_reject("portfolio caps leave no room")
    assert not portfolio_budget_cap_sizing_reject("symbol cap remaining yields zero shares")


@pytest.fixture
def dt() -> datetime:
    return datetime(2024, 6, 1, 15, 0, 0, tzinfo=timezone.utc)


def test_execute_cap_pressure_partial_trim_submits_fraction(dt: datetime, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.trend_long_ranked_dispatch as tld

    monkeypatch.setattr(tld, "update_tracked", lambda *a, **k: None)
    monkeypatch.setattr(tld, "remove_tracked", lambda *a, **k: None)
    monkeypatch.setattr(tld, "log_sell", lambda *a, **k: None)

    _submit = MagicMock()
    broker = MagicMock()
    broker.get_positions = MagicMock(
        return_value=[{"symbol": "AAA", "market_value": 10000.0, "qty": 100}]
    )
    broker.get_position.return_value = {"symbol": "AAA", "qty": 100}
    broker.get_latest_quote.return_value = MagicMock(
        reference_mid=lambda _fb: 100.0,
        spread_pct=0.1,
        bid=99.0,
        ask=101.0,
        skip_spread_check=False,
        is_stale=lambda *_a, **_k: False,
    )
    broker.get_bars.return_value = MagicMock(empty=True)
    broker.get_open_orders.return_value = []
    broker.submit_order = _submit
    engine = MagicMock()
    engine.execution.build_order.return_value = MagicMock()

    tracked = {"AAA": {"qty": 100, "entry_time": "2024-01-01T10:00:00+00:00", "entry_price": 90.0}}
    positions: list = [{"symbol": "AAA", "market_value": 10000.0, "qty": 100}]

    def _lt(_uid: str, **_kw: object) -> dict:
        return tracked

    monkeypatch.setattr(tld, "load_tracked", _lt)

    ok = execute_cap_pressure_partial_trim(
        incoming_sym_upper="ZZZ",
        eligible_active=["AAA"],
        tracked=tracked,
        positions=positions,
        dt=dt,
        trim_frac=0.15,
        max_symbols=24,
        broker=broker,
        engine=engine,
        rep_sub={"min_hold_minutes": 0, "min_market_value_to_replace_usd": 100},
        user_id="u1",
        data_dir=MagicMock(),
        log_entry_skip=lambda *a, **k: None,
        verbose=False,
        replacement_scan_state={"count": 0, "max": 5},
        cycle_risk_state={"replacements": 0},
        per_cycle_exit_ctx=None,
        live_risk_order_callback=None,
        stale_quote_max_age=60.0,
    )
    assert ok is True
    assert _submit.called
    bo = engine.execution.build_order.call_args
    assert bo[0][0] == "AAA"
    assert bo[0][1] == "sell"
    assert bo[0][2] == 15


def test_execute_cap_pressure_partial_trim_min_hold_debug_log_does_not_crash(
    dt: datetime, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging
    import src.trend_long_ranked_dispatch as tld

    monkeypatch.setattr(tld, "update_tracked", lambda *a, **k: None)
    monkeypatch.setattr(tld, "remove_tracked", lambda *a, **k: None)
    monkeypatch.setattr(tld, "log_sell", lambda *a, **k: None)

    broker = MagicMock()
    broker.get_positions = MagicMock(
        return_value=[{"symbol": "AAA", "market_value": 10000.0, "qty": 100}]
    )
    broker.get_position.return_value = {"symbol": "AAA", "qty": 100}
    broker.get_latest_quote.return_value = MagicMock(
        reference_mid=lambda _fb: 100.0,
        spread_pct=0.1,
        bid=99.0,
        ask=101.0,
        skip_spread_check=False,
        is_stale=lambda *_a, **_k: False,
    )
    broker.get_bars.return_value = MagicMock(empty=True)
    broker.get_open_orders.return_value = []
    engine = MagicMock()
    engine.execution.build_order.return_value = MagicMock(quantity=15)

    tracked = {"AAA": {"qty": 100, "entry_time": "2024-06-01T14:45:00+00:00", "entry_price": 90.0}}
    positions: list = [{"symbol": "AAA", "market_value": 10000.0, "qty": 100}]

    monkeypatch.setattr(tld, "load_tracked", lambda _uid, **_kw: tracked)

    caplog.set_level(logging.INFO)
    ok = execute_cap_pressure_partial_trim(
        incoming_sym_upper="ZZZ",
        eligible_active=["AAA"],
        tracked=tracked,
        positions=positions,
        dt=dt,
        trim_frac=0.15,
        max_symbols=24,
        broker=broker,
        engine=engine,
        rep_sub={"min_hold_minutes": 60, "min_market_value_to_replace_usd": 100},
        user_id="u1",
        data_dir=MagicMock(),
        log_entry_skip=lambda *a, **k: None,
        verbose=False,
        replacement_scan_state={"count": 0, "max": 5},
        cycle_risk_state={"replacements": 0},
        per_cycle_exit_ctx=None,
        live_risk_order_callback=None,
        stale_quote_max_age=60.0,
    )

    assert ok is True
    assert "MIN_HOLD_DEBUG symbol=AAA path=cap_pressure_partial_trim" in caplog.text
    assert "blocked_by_min_hold=True" in caplog.text
    broker.submit_order.assert_called_once()


def test_execute_cap_pressure_partial_trim_prefers_weakest_signal_strength_first(
    dt: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Largest-notional names are no longer trimmed first — lowest ``signal_strength`` in tracker wins."""
    import src.trend_long_ranked_dispatch as tld

    monkeypatch.setattr(tld, "update_tracked", lambda *a, **k: None)
    monkeypatch.setattr(tld, "remove_tracked", lambda *a, **k: None)
    monkeypatch.setattr(tld, "log_sell", lambda *a, **k: None)

    _submit = MagicMock()
    broker = MagicMock()
    broker.get_positions = MagicMock(
        return_value=[
            {"symbol": "AAA", "market_value": 50000.0, "qty": 500},
            {"symbol": "BBB", "market_value": 5000.0, "qty": 50},
        ]
    )
    broker.get_position.side_effect = lambda sym: {
        "AAA": {"symbol": "AAA", "qty": 500},
        "BBB": {"symbol": "BBB", "qty": 50},
    }[str(sym).upper()]
    broker.get_latest_quote.return_value = MagicMock(
        reference_mid=lambda _fb: 100.0,
        spread_pct=0.1,
        bid=99.0,
        ask=101.0,
        skip_spread_check=False,
        is_stale=lambda *_a, **_k: False,
    )
    broker.get_bars.return_value = MagicMock(empty=True)
    broker.get_open_orders.return_value = []
    broker.submit_order = _submit
    engine = MagicMock()
    engine.execution.build_order.return_value = MagicMock()

    tracked = {
        "AAA": {
            "qty": 500,
            "signal_strength": 0.95,
            "entry_time": "2024-01-01T10:00:00+00:00",
            "entry_price": 90.0,
        },
        "BBB": {
            "qty": 50,
            "signal_strength": 0.35,
            "entry_time": "2024-01-01T10:00:00+00:00",
            "entry_price": 95.0,
        },
    }
    positions: list = [
        {"symbol": "AAA", "market_value": 50000.0, "qty": 500},
        {"symbol": "BBB", "market_value": 5000.0, "qty": 50},
    ]

    def _lt(_uid: str, **_kw: object) -> dict:
        return tracked

    monkeypatch.setattr(tld, "load_tracked", _lt)

    ok = execute_cap_pressure_partial_trim(
        incoming_sym_upper="ZZZ",
        eligible_active=["AAA", "BBB"],
        tracked=tracked,
        positions=positions,
        dt=dt,
        trim_frac=0.15,
        max_symbols=1,
        broker=broker,
        engine=engine,
        rep_sub={"min_hold_minutes": 0, "min_market_value_to_replace_usd": 100},
        user_id="u1",
        data_dir=MagicMock(),
        log_entry_skip=lambda *a, **k: None,
        verbose=False,
        replacement_scan_state={"count": 0, "max": 5},
        cycle_risk_state={"replacements": 0},
        per_cycle_exit_ctx=None,
        live_risk_order_callback=None,
        stale_quote_max_age=60.0,
    )
    assert ok is True
    assert _submit.called
    bo = engine.execution.build_order.call_args
    assert bo[0][0] == "BBB"


def test_execute_cap_pressure_partial_trim_skips_when_fully_reserved_by_open_orders(
    dt: datetime, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging
    import src.trend_long_ranked_dispatch as tld

    monkeypatch.setattr(tld, "update_tracked", lambda *a, **k: None)
    monkeypatch.setattr(tld, "remove_tracked", lambda *a, **k: None)
    monkeypatch.setattr(tld, "log_sell", lambda *a, **k: None)

    broker = MagicMock()
    broker.get_positions = MagicMock(
        return_value=[{"symbol": "AAA", "market_value": 10000.0, "qty": 100}]
    )
    broker.get_position.return_value = {"symbol": "AAA", "qty": 100}
    broker.get_open_orders.return_value = [
        {"symbol": "AAA", "side": "sell", "qty": 100},
    ]
    broker.get_latest_quote.return_value = MagicMock(
        reference_mid=lambda _fb: 100.0,
        spread_pct=0.1,
        bid=99.0,
        ask=101.0,
        skip_spread_check=False,
        is_stale=lambda *_a, **_k: False,
    )
    broker.get_bars.return_value = MagicMock(empty=True)
    engine = MagicMock()
    engine.execution.build_order.return_value = MagicMock()

    tracked = {"AAA": {"qty": 100, "entry_time": "2024-01-01T10:00:00+00:00", "entry_price": 90.0}}
    positions: list = [{"symbol": "AAA", "market_value": 10000.0, "qty": 100}]

    monkeypatch.setattr(tld, "load_tracked", lambda _uid, **_kw: tracked)

    caplog.set_level(logging.INFO)
    ok = execute_cap_pressure_partial_trim(
        incoming_sym_upper="ZZZ",
        eligible_active=["AAA"],
        tracked=tracked,
        positions=positions,
        dt=dt,
        trim_frac=0.15,
        max_symbols=24,
        broker=broker,
        engine=engine,
        rep_sub={"min_hold_minutes": 0, "min_market_value_to_replace_usd": 100},
        user_id="u1",
        data_dir=MagicMock(),
        log_entry_skip=lambda *a, **k: None,
        verbose=False,
        replacement_scan_state={"count": 0, "max": 5},
        cycle_risk_state={"replacements": 0},
        per_cycle_exit_ctx=None,
        live_risk_order_callback=None,
        stale_quote_max_age=60.0,
    )

    assert ok is False
    assert "TRIM_SKIPPED symbol=AAA available=0 reserved_by_orders=100" in caplog.text
    broker.submit_order.assert_not_called()


def test_execute_cap_pressure_partial_trim_adjusts_when_partially_reserved(
    dt: datetime, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging
    import src.trend_long_ranked_dispatch as tld

    monkeypatch.setattr(tld, "update_tracked", lambda *a, **k: None)
    monkeypatch.setattr(tld, "remove_tracked", lambda *a, **k: None)
    monkeypatch.setattr(tld, "log_sell", lambda *a, **k: None)

    _submit = MagicMock()
    broker = MagicMock()
    broker.get_positions = MagicMock(
        return_value=[{"symbol": "AAA", "market_value": 900.0, "qty": 9.027}]
    )
    broker.get_position.return_value = {
        "symbol": "AAA",
        "qty": 9.027,
        "qty_held_for_orders": 8.0,
        "qty_available": 1.027,
    }
    broker.get_open_orders.return_value = [{"symbol": "AAA", "side": "sell", "qty": 8}]
    broker.get_latest_quote.return_value = MagicMock(
        reference_mid=lambda _fb: 100.0,
        spread_pct=0.1,
        bid=99.0,
        ask=101.0,
        skip_spread_check=False,
        is_stale=lambda *_a, **_k: False,
    )
    broker.get_bars.return_value = MagicMock(empty=True)
    broker.submit_order = _submit
    engine = MagicMock()
    engine.execution.build_order.return_value = MagicMock()

    tracked = {"AAA": {"qty": 15, "entry_time": "2024-01-01T10:00:00+00:00", "entry_price": 90.0}}
    positions: list = [{"symbol": "AAA", "market_value": 900.0, "qty": 9.027}]

    monkeypatch.setattr(tld, "load_tracked", lambda _uid, **_kw: tracked)

    caplog.set_level(logging.INFO)
    ok = execute_cap_pressure_partial_trim(
        incoming_sym_upper="ZZZ",
        eligible_active=["AAA"],
        tracked=tracked,
        positions=positions,
        dt=dt,
        trim_frac=0.15,
        max_symbols=24,
        broker=broker,
        engine=engine,
        rep_sub={"min_hold_minutes": 0, "min_market_value_to_replace_usd": 100},
        user_id="u1",
        data_dir=MagicMock(),
        log_entry_skip=lambda *a, **k: None,
        verbose=False,
        replacement_scan_state={"count": 0, "max": 5},
        cycle_risk_state={"replacements": 0},
        per_cycle_exit_ctx=None,
        live_risk_order_callback=None,
        stale_quote_max_age=60.0,
    )

    assert ok is True
    assert _submit.called
    assert "TRIM_ADJUSTED symbol=AAA requested=2.0 available=1.027" in caplog.text
    bo = engine.execution.build_order.call_args
    assert bo[0][2] == pytest.approx(1.027)


def test_consider_replacement_partial_trim_before_rotation(
    monkeypatch: pytest.MonkeyPatch, dt: datetime
) -> None:
    """Portfolio budget reject + cap_pressure_trim runs execute path; rotation not evaluated when trim succeeds."""
    import src.trend_long_ranked_dispatch as tld

    _eval = MagicMock()
    monkeypatch.setattr(tld, "evaluate_portfolio_replacement_for_dispatch", _eval)
    monkeypatch.setattr(tld, "execute_cap_pressure_partial_trim", lambda **kw: True)

    decision = TradeDecision(
        allowed=False,
        reason="portfolio caps leave no room",
        entry_signal=EntrySignal(
            symbol="ZZZ",
            side="long",
            strength=1.0,
            stop_pct=1.5,
            take_profit_pct=3.0,
            time_bars_exit=20,
            metadata={},
        ),
        position_sizing=SimpleNamespace(reject_reason="portfolio caps leave no room"),
    )
    ok = consider_replacement_for_sizing_reject(
        incoming_sym_upper="ZZZ",
        decision=decision,
        tracked={"AAA": {"qty": 5}},
        eligible_active=["AAA"],
        positions=[],
        dt=dt,
        config={
            "portfolio": {
                "cap_pressure_trim": {"enabled": True, "trim_frac": 0.15},
            }
        },
        engine=MagicMock(),
        broker=MagicMock(),
        df=None,
        atr_pct=1.0,
        quote=MagicMock(),
        spread_pct_eval=0.1,
        regime_result=MagicMock(score=4, condition="bullish"),
        entry_regime_score_int=4,
        rep_sub={"min_hold_minutes": 0},
        strength_jitter_max=0.0,
        replace_if_weakest_older_than=None,
        max_position_age_bars=None,
        allow_equal_replacement=False,
        replacement_threshold=0.0,
        incoming_notional_usd=1000.0,
        replacement_scan_state=None,
        user_id="u1",
        data_dir=MagicMock(),
        current_positions={},
        log_entry_skip=lambda *a, **k: None,
        verbose=False,
        cycle_risk_state=None,
        stale_quote_max_age=60.0,
    )
    assert ok is True
    _eval.assert_not_called()
