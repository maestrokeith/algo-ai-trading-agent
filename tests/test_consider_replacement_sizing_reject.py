"""Tests for :func:`src.trend_long_ranked_dispatch.consider_replacement_for_sizing_reject`."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.strategy import EntrySignal
from src.trading_engine import TradeDecision
from src.trend_long_ranked_dispatch import consider_replacement_for_sizing_reject


def _entry_sig() -> EntrySignal:
    return EntrySignal(
        symbol="ZZZ",
        side="long",
        strength=1.0,
        stop_pct=1.5,
        take_profit_pct=3.0,
        time_bars_exit=20,
        metadata={},
    )


@pytest.fixture
def dt() -> datetime:
    return datetime(2024, 6, 1, 15, 0, 0, tzinfo=timezone.utc)


def test_consider_replacement_false_when_evaluate_skips(monkeypatch: pytest.MonkeyPatch, dt: datetime) -> None:
    import src.trend_long_ranked_dispatch as tld

    monkeypatch.setattr(tld, "evaluate_portfolio_replacement_for_dispatch", lambda **kw: (None, "no rotate"))
    decision = TradeDecision(
        allowed=False,
        reason="portfolio caps leave no room",
        entry_signal=_entry_sig(),
        position_sizing=SimpleNamespace(reject_reason="portfolio caps leave no room"),
    )
    ok = consider_replacement_for_sizing_reject(
        incoming_sym_upper="ZZZ",
        decision=decision,
        tracked={"AAA": {"qty": 5, "entry_time": "2024-01-01T10:00:00+00:00"}},
        eligible_active=["AAA"],
        positions=[{"symbol": "AAA", "market_value": 5000.0, "qty": 5}],
        dt=dt,
        config={"portfolio": {}},
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
        current_positions={"AAA": {"notional": 5000.0}},
        log_entry_skip=lambda *a, **k: None,
        verbose=False,
        cycle_risk_state=None,
        stale_quote_max_age=60.0,
    )
    assert ok is False


def test_consider_replacement_sells_and_returns_true(monkeypatch: pytest.MonkeyPatch, dt: datetime) -> None:
    import src.trend_long_ranked_dispatch as tld

    monkeypatch.setattr(tld, "evaluate_portfolio_replacement_for_dispatch", lambda **kw: (["AAA"], None))
    monkeypatch.setattr(tld, "remove_tracked", lambda *a, **k: None)
    _submit = MagicMock()
    broker = MagicMock()
    broker.get_positions = MagicMock(
        return_value=[{"symbol": "AAA", "market_value": 5000.0, "qty": 5}]
    )
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
    decision = TradeDecision(
        allowed=False,
        reason="symbol cap remaining yields zero shares",
        entry_signal=_entry_sig(),
        position_sizing=SimpleNamespace(reject_reason="symbol cap remaining yields zero shares"),
    )
    tracked = {"AAA": {"qty": 5, "entry_time": "2024-01-01T10:00:00+00:00"}}
    eligible = ["AAA"]
    current = {"AAA": {"notional": 5000.0}}
    positions_list = [{"symbol": "AAA", "market_value": 5000.0, "qty": 5}]

    def _lt(_uid: str, data_dir: Any) -> dict:
        return tracked

    monkeypatch.setattr(tld, "load_tracked", _lt)
    ok = consider_replacement_for_sizing_reject(
        incoming_sym_upper="ZZZ",
        decision=decision,
        tracked=tracked,
        eligible_active=eligible,
        positions=positions_list,
        dt=dt,
        config={"portfolio": {"enable_replacement": True}},
        engine=engine,
        broker=broker,
        df=None,
        atr_pct=1.0,
        quote=MagicMock(),
        spread_pct_eval=0.1,
        regime_result=MagicMock(score=4, condition="bullish"),
        entry_regime_score_int=4,
        rep_sub={"min_hold_minutes": 0, "min_market_value_to_replace_usd": 100},
        strength_jitter_max=0.0,
        replace_if_weakest_older_than=None,
        max_position_age_bars=None,
        allow_equal_replacement=False,
        replacement_threshold=0.0,
        incoming_notional_usd=1000.0,
        replacement_scan_state={"count": 0, "max": 5},
        user_id="u1",
        data_dir=MagicMock(),
        current_positions=current,
        log_entry_skip=lambda *a, **k: None,
        verbose=False,
        cycle_risk_state={"replacements": 0},
        stale_quote_max_age=60.0,
    )
    assert ok is True
    _submit.assert_called_once()
    assert "AAA" not in current
    assert "AAA" not in eligible


def test_consider_replacement_skips_when_per_cycle_exit_cap(monkeypatch: pytest.MonkeyPatch, dt: datetime) -> None:
    import src.trend_long_ranked_dispatch as tld

    monkeypatch.setattr(tld, "evaluate_portfolio_replacement_for_dispatch", lambda **kw: (["AAA"], None))
    monkeypatch.setattr(tld, "remove_tracked", lambda *a, **k: None)
    _submit = MagicMock()
    broker = MagicMock()
    broker.get_positions = MagicMock(
        return_value=[{"symbol": "AAA", "market_value": 5000.0, "qty": 5}]
    )
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
    decision = TradeDecision(
        allowed=False,
        reason="symbol cap remaining yields zero shares",
        entry_signal=_entry_sig(),
        position_sizing=SimpleNamespace(reject_reason="symbol cap remaining yields zero shares"),
    )
    current = {"AAA": {"notional": 5000.0}}
    eligible = ["AAA"]
    _tr = {"AAA": {"qty": 5, "entry_time": "2024-01-01T10:00:00+00:00"}}
    positions_list2 = [{"symbol": "AAA", "market_value": 5000.0, "qty": 5}]

    def _lt2(_uid: str, data_dir: Any) -> dict:
        return _tr

    monkeypatch.setattr(tld, "load_tracked", _lt2)
    gate = MagicMock()
    gate.skip_exit_for_action_cap.return_value = True
    ok = consider_replacement_for_sizing_reject(
        incoming_sym_upper="ZZZ",
        decision=decision,
        tracked=_tr,
        eligible_active=eligible,
        positions=positions_list2,
        dt=dt,
        config={"portfolio": {"enable_replacement": True}},
        engine=engine,
        broker=broker,
        df=None,
        atr_pct=1.0,
        quote=MagicMock(),
        spread_pct_eval=0.1,
        regime_result=MagicMock(score=4, condition="bullish"),
        entry_regime_score_int=4,
        rep_sub={"min_hold_minutes": 0, "min_market_value_to_replace_usd": 100},
        strength_jitter_max=0.0,
        replace_if_weakest_older_than=None,
        max_position_age_bars=None,
        allow_equal_replacement=False,
        replacement_threshold=0.0,
        incoming_notional_usd=1000.0,
        replacement_scan_state={"count": 0, "max": 5},
        user_id="u1",
        data_dir=MagicMock(),
        current_positions=current,
        log_entry_skip=lambda *a, **k: None,
        verbose=False,
        cycle_risk_state={"replacements": 0},
        stale_quote_max_age=60.0,
        per_cycle_exit_ctx=gate,
    )
    assert ok is False
    _submit.assert_not_called()


def test_consider_replacement_skips_rotation_when_symbol_cap_only_and_replacement_disabled(
    monkeypatch: pytest.MonkeyPatch, dt: datetime
) -> None:
    """No book-level cap message + enable_replacement false → no evaluate / no sells (avoid KO-style churn)."""
    import src.trend_long_ranked_dispatch as tld

    ev = MagicMock()
    monkeypatch.setattr(tld, "evaluate_portfolio_replacement_for_dispatch", ev)
    decision = TradeDecision(
        allowed=False,
        reason="symbol cap remaining yields zero shares",
        entry_signal=_entry_sig(),
        position_sizing=SimpleNamespace(
            reject_reason="symbol cap remaining yields zero shares"
        ),
    )
    ok = consider_replacement_for_sizing_reject(
        incoming_sym_upper="ZZZ",
        decision=decision,
        tracked={"KO": {"qty": 10}},
        eligible_active=["KO"],
        positions=[{"symbol": "KO", "market_value": 5000.0, "qty": 10}],
        dt=dt,
        config={"portfolio": {"enable_replacement": False}},
        engine=MagicMock(),
        broker=MagicMock(),
        df=None,
        atr_pct=1.0,
        quote=MagicMock(),
        spread_pct_eval=0.1,
        regime_result=MagicMock(score=4, condition="bullish"),
        entry_regime_score_int=4,
        rep_sub={},
        strength_jitter_max=0.0,
        replace_if_weakest_older_than=None,
        max_position_age_bars=None,
        allow_equal_replacement=False,
        replacement_threshold=0.0,
        incoming_notional_usd=1000.0,
        replacement_scan_state=None,
        user_id="u1",
        data_dir=MagicMock(),
        current_positions={"KO": {}},
        log_entry_skip=lambda *a, **k: None,
        verbose=False,
        cycle_risk_state=None,
        stale_quote_max_age=60.0,
    )
    assert ok is False
    ev.assert_not_called()


def test_replacement_cap_substring_triggers() -> None:
    from src.portfolio_replacement import replacement_entry_fail_reason_invites_cap_rotation

    assert replacement_entry_fail_reason_invites_cap_rotation("portfolio caps leave no room")
    assert replacement_entry_fail_reason_invites_cap_rotation("symbol cap remaining yields zero shares")
    assert replacement_entry_fail_reason_invites_cap_rotation("sector cap leaves no room for additional shares")
    assert replacement_entry_fail_reason_invites_cap_rotation("exposure_gate: total book over cap")
    assert not replacement_entry_fail_reason_invites_cap_rotation("spread too wide")
    assert not replacement_entry_fail_reason_invites_cap_rotation(None)
