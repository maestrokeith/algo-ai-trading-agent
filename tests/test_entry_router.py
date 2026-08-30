"""Tests for entry_router options gating."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from src.entry_router import (
    EntryRouteSignal,
    allow_long_options_trades,
    log_options_stock_path_if_ineligible,
    route_to_options_executor,
    should_use_options,
    trend_long_options_extra_gate_ok,
    trend_long_options_extra_gate_reason,
    use_equity_fallback_after_options,
)
from src.options_selector import OptionContractCandidate
from src.options_observability import emit_options_cycle_summary, reset_options_cycle_stats


def _base_config() -> dict:
    return {
        "portfolio": {"max_options_capital_pct": 40},
        "options": {
            "enabled": True,
            "mode": "long_premium_only",
            "allowed_underlyings": ["QQQ", "SPY"],
            "entry_mapping": {"bullish_signal": "call", "bearish_signal": "put"},
            "max_total_options_exposure_pct": 40,
            "risk_per_trade_pct": 2,
            "max_option_position_pct": 100,
            "max_open_option_positions": 5,
            "v1_max_contracts_per_trade": 200,
        }
    }


@pytest.fixture
def signal_trend() -> EntryRouteSignal:
    return EntryRouteSignal(
        underlying="QQQ",
        direction="bullish",
        source="trend_long",
        stock_symbol="QQQ",
    )


def test_extra_gate_off_allows_without_sqqq(signal_trend: EntryRouteSignal) -> None:
    cfg = _base_config()
    cfg["options"]["trend_long_options_require_sqqq_and_strong_trend"] = False
    assert should_use_options(cfg, signal_trend)
    assert trend_long_options_extra_gate_ok(
        cfg, holding_sqqq=False, pct_above_50d=None
    )


def test_extra_gate_on_bearish_requires_inverse_holding(signal_trend: EntryRouteSignal) -> None:
    cfg = _base_config()
    cfg["options"]["trend_long_options_require_sqqq_and_strong_trend"] = True
    pos_sqqq = [{"symbol": "SQQQ", "qty": 1}]
    assert trend_long_options_extra_gate_ok(
        cfg,
        holding_sqqq=False,
        pct_above_50d=0.2,
        bearish_regime=True,
        positions=pos_sqqq,
        tracked={},
    )
    assert not trend_long_options_extra_gate_ok(
        cfg,
        holding_sqqq=False,
        pct_above_50d=0.9,
        bearish_regime=True,
        positions=[],
        tracked={},
    )
    # Non-bearish: allow without inverse (same as allow_long_options_trades()).
    assert trend_long_options_extra_gate_ok(
        cfg,
        holding_sqqq=False,
        pct_above_50d=0.2,
        bearish_regime=False,
        positions=[],
        tracked={},
    )
    assert allow_long_options_trades() is True


def test_extra_gate_reason_bearish_inverse(signal_trend: EntryRouteSignal) -> None:
    cfg = _base_config()
    cfg["options"]["trend_long_options_require_sqqq_and_strong_trend"] = True
    r = trend_long_options_extra_gate_reason(
        cfg,
        holding_sqqq=False,
        pct_above_50d=0.9,
        bearish_regime=True,
        positions=[],
        tracked={},
    )
    assert r is not None and "bearish regime" in r.lower() and "SQQQ" in r
    assert (
        trend_long_options_extra_gate_reason(
            cfg,
            holding_sqqq=False,
            pct_above_50d=0.9,
            bearish_regime=False,
            positions=[],
            tracked={},
        )
        is None
    )


def test_extra_gate_regime_condition_bearish_string(signal_trend: EntryRouteSignal) -> None:
    cfg = _base_config()
    cfg["options"]["trend_long_options_require_sqqq_and_strong_trend"] = True
    assert not trend_long_options_extra_gate_ok(
        cfg,
        holding_sqqq=False,
        pct_above_50d=None,
        bearish_regime=False,
        regime_condition="bearish",
        positions=[],
        tracked={},
    )
    assert trend_long_options_extra_gate_ok(
        cfg,
        holding_sqqq=True,
        pct_above_50d=None,
        bearish_regime=False,
        regime_condition="bearish",
        positions=[],
        tracked={},
    )


def test_route_skips_options_when_holding_underlying_equity(capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _base_config()
    sig = EntryRouteSignal(
        underlying="QQQ",
        direction="bullish",
        source="trend_long",
        stock_symbol="QQQ",
    )
    assert (
        route_to_options_executor(
            cfg,
            sig,
            log_dt=None,
            account_equity=100_000.0,
            positions=[{"symbol": "QQQ", "qty": 5}],
        )
        is False
    )
    out = capsys.readouterr().out
    assert "holding equity" in out.lower()


def test_route_logs_option_lifecycle_for_mock_order(
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = _base_config()
    cfg["options"]["mode"] = "paper_only"
    exp = date(2026, 6, 30)
    chain = [
        OptionContractCandidate(
            symbol="QQQ260630C00350000",
            strike=350.0,
            expiration=exp,
            right="call",
            open_interest=1000,
            volume=250,
            bid=1.0,
            ask=1.02,
            delta=0.5,
        )
    ]
    sig = EntryRouteSignal(
        underlying="QQQ",
        direction="bullish",
        source="dynamic_universe",
        stock_symbol="QQQ",
        conviction_score=0.9,
        news_score=1.0,
        event_score=1.0,
        relative_volume=5.0,
        catalyst_type="earnings",
    )

    class Broker:
        paper = True
        _sqlite_user_id = "paper_bot"

        def submit_order(self, req):
            return SimpleNamespace(id="opt-order-1", status="accepted", filled_avg_price=req.limit_price)

        def resolve_entry_price_from_fill(self, order, fallback):
            return float(getattr(order, "filled_avg_price", None) or fallback)

    class Execution:
        _sqlite_user_id = "paper_bot"
        _options_data_dir = tmp_path

        def build_order_for_entry(self, symbol, side, quantity, mid_price, spread_pct, **kwargs):
            return SimpleNamespace(
                symbol=symbol,
                side=side,
                quantity=quantity,
                limit_price=round(float(mid_price), 2),
            )

    caplog.set_level(logging.INFO)
    placed = route_to_options_executor(
        cfg,
        sig,
        log_dt=datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc),
        account_equity=100_000.0,
        positions=[],
        broker=Broker(),
        execution_manager=Execution(),
        chain_candidates=chain,
        underlying_spot=350.0,
        tracked={},
    )

    assert placed is True
    assert "OPTION_SIGNAL symbol=QQQ underlying=QQQ direction=bullish" in caplog.text
    assert "OPTION_SCAN_START symbol=QQQ right=call chain_rows=1 path=ranked_budget" in caplog.text
    assert "OPTION_SELECTED symbol=QQQ right=call contract=QQQ260630C00350000" in caplog.text
    assert "OPTION_ORDER_INTENT symbol=QQQ contract=QQQ260630C00350000 side=buy qty=" in caplog.text
    assert "OPTION_ORDER_SUBMITTED symbol=QQQ contract=QQQ260630C00350000 side=buy" in caplog.text
    assert "OPTIONS_ORDER_ACCEPTED symbol=QQQ" in caplog.text
    assert "OPTIONS_ORDER_FILLED symbol=QQQ" in caplog.text
    assert "OPTION_POSITION_OPENED symbol=QQQ contract=QQQ260630C00350000 qty=" in caplog.text
    assert "OPTIONS_POSITION_OPENED symbol=QQQ contract=QQQ260630C00350000 qty=" in caplog.text


def test_options_observability_no_signal_underlying_rejected(caplog: pytest.LogCaptureFixture) -> None:
    reset_options_cycle_stats()
    cfg = _base_config()
    sig = EntryRouteSignal(
        underlying="NVDA",
        direction="bullish",
        source="trend_long",
        stock_symbol="NVDA",
    )
    caplog.set_level(logging.INFO)

    assert route_to_options_executor(cfg, sig, log_dt=None) is False
    emit_options_cycle_summary()

    assert "OPTIONS_CANDIDATE symbol=NVDA underlying=NVDA direction=bullish source=trend_long" in caplog.text
    assert "OPTIONS_REJECT symbol=NVDA stage=signal reason=symbol_not_allowed" in caplog.text
    assert "OPTIONS_CYCLE_SUMMARY symbols_evaluated=1 signals_generated=0 candidates_rejected=" in caplog.text


def test_options_observability_no_contract_rejected(caplog: pytest.LogCaptureFixture) -> None:
    reset_options_cycle_stats()
    cfg = _base_config()
    cfg["options"]["mode"] = "paper_only"
    sig = EntryRouteSignal(
        underlying="QQQ",
        direction="bullish",
        source="dynamic_universe",
        stock_symbol="QQQ",
        conviction_score=0.9,
    )
    caplog.set_level(logging.INFO)

    placed = route_to_options_executor(
        cfg,
        sig,
        log_dt=datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc),
        account_equity=100_000.0,
        positions=[],
        broker=type("Broker", (), {"paper": True})(),
        execution_manager=object(),
        chain_candidates=[],
        underlying_spot=350.0,
        tracked={},
    )
    emit_options_cycle_summary()

    assert placed is False
    assert "OPTIONS_REJECT symbol=QQQ stage=select reason=no_contract_found" in caplog.text
    assert "top_rejection_reasons=no_contract_found:" in caplog.text


def test_options_observability_wide_spread_contract_rejected(caplog: pytest.LogCaptureFixture) -> None:
    reset_options_cycle_stats()
    cfg = _base_config()
    cfg["options"]["mode"] = "paper_only"
    cfg["options"]["max_bid_ask_spread_pct"] = 5.0
    cfg["options"]["contract_selection"] = {"max_bid_ask_spread_pct": 5.0}
    chain = [
        OptionContractCandidate(
            symbol="QQQ260630C00350000",
            strike=350.0,
            expiration=date(2026, 6, 30),
            right="call",
            open_interest=1000,
            volume=250,
            bid=1.0,
            ask=1.4,
            delta=0.5,
        )
    ]
    sig = EntryRouteSignal(
        underlying="QQQ",
        direction="bullish",
        source="dynamic_universe",
        stock_symbol="QQQ",
        conviction_score=0.9,
    )
    caplog.set_level(logging.INFO)

    placed = route_to_options_executor(
        cfg,
        sig,
        log_dt=datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc),
        account_equity=100_000.0,
        positions=[],
        broker=type("Broker", (), {"paper": True})(),
        execution_manager=object(),
        chain_candidates=chain,
        underlying_spot=350.0,
        tracked={},
    )

    assert placed is False
    assert "OPTIONS_CONTRACT_REJECT option_symbol=QQQ260630C00350000 underlying=QQQ reason=quote_fail" in caplog.text
    assert "expiry=2026-06-30 strike=350" in caplog.text
    assert "OPTIONS_REJECT symbol=QQQ stage=select reason=liquidity_filter" in caplog.text


def test_options_observability_broker_rejection(caplog: pytest.LogCaptureFixture, tmp_path) -> None:
    reset_options_cycle_stats()
    cfg = _base_config()
    cfg["options"]["mode"] = "paper_only"

    class Broker:
        paper = True
        _sqlite_user_id = "paper_bot"

        def submit_order(self, req):
            return SimpleNamespace(id="reject-1", status="rejected", filled_avg_price=None)

    class Execution:
        _sqlite_user_id = "paper_bot"
        _options_data_dir = tmp_path

        def build_order_for_entry(self, symbol, side, quantity, mid_price, spread_pct, **kwargs):
            return SimpleNamespace(symbol=symbol, side=side, quantity=quantity, limit_price=round(float(mid_price), 2))

    sig = EntryRouteSignal(
        underlying="QQQ",
        direction="bullish",
        source="dynamic_universe",
        stock_symbol="QQQ",
        conviction_score=0.9,
    )
    caplog.set_level(logging.INFO)

    placed = route_to_options_executor(
        cfg,
        sig,
        log_dt=datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc),
        account_equity=100_000.0,
        positions=[],
        broker=Broker(),
        execution_manager=Execution(),
        chain_candidates=_option_chain(),
        underlying_spot=350.0,
        tracked={},
    )

    assert placed is False
    assert "OPTIONS_ORDER_SUBMITTED symbol=QQQ contract=QQQ260630C00350000 side=buy" in caplog.text
    assert "OPTIONS_ORDER_REJECTED symbol=QQQ contract=QQQ260630C00350000 side=buy qty=" in caplog.text
    assert "OPTIONS_REJECT symbol=QQQ stage=broker reason=order_submission_failed" in caplog.text


def _live_pilot_config() -> dict:
    cfg = _base_config()
    cfg["options"].update(
        {
            "mode": "live",
            "live_pilot_enabled": True,
            "total_exposure_limit": 0.01,
            "max_total_options_exposure_pct": 1,
            "per_trade": 0.005,
            "risk_per_trade_pct": 0.5,
            "max_option_position_pct": 0.005,
            "max_option_positions": 1,
            "max_positions": 1,
            "max_open_option_positions": 1,
            "max_contracts_per_trade": 1,
            "v1_max_contracts_per_trade": 1,
            "require_top_signal": True,
            "never_bypass_stock_risk_caps": True,
        }
    )
    return cfg


def _option_chain(mid: float = 1.0) -> list[OptionContractCandidate]:
    return [
        OptionContractCandidate(
            symbol="QQQ260630C00350000",
            strike=350.0,
            expiration=date(2026, 6, 30),
            right="call",
            open_interest=1000,
            volume=250,
            bid=max(0.01, mid - 0.01),
            ask=mid + 0.01,
            delta=0.5,
        )
    ]


class _LivePilotBroker:
    paper = False
    _sqlite_user_id = "live_bot"

    def __init__(self) -> None:
        self.orders: list[object] = []

    def submit_order(self, req):
        self.orders.append(req)
        return SimpleNamespace(id="live-opt-order-1", status="accepted", filled_avg_price=req.limit_price)

    def resolve_entry_price_from_fill(self, order, fallback):
        return float(getattr(order, "filled_avg_price", None) or fallback)


class _LivePilotExecution:
    _sqlite_user_id = "live_bot"
    _options_data_dir = None

    def build_order_for_entry(self, symbol, side, quantity, mid_price, spread_pct, **kwargs):
        return SimpleNamespace(
            symbol=symbol,
            side=side,
            quantity=quantity,
            limit_price=round(float(mid_price), 2),
        )


def _live_pilot_signal() -> EntryRouteSignal:
    return EntryRouteSignal(
        underlying="QQQ",
        direction="bullish",
        source="trend_long",
        stock_symbol="SPY",
        conviction_score=0.9,
    )


def test_live_options_pilot_disabled_blocks_live_options(caplog: pytest.LogCaptureFixture) -> None:
    cfg = _live_pilot_config()
    cfg["options"]["live_pilot_enabled"] = False
    broker = _LivePilotBroker()
    caplog.set_level(logging.INFO)

    placed = route_to_options_executor(
        cfg,
        _live_pilot_signal(),
        log_dt=datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc),
        account_equity=27_000.0,
        positions=[],
        broker=broker,
        execution_manager=_LivePilotExecution(),
        chain_candidates=_option_chain(),
        underlying_spot=350.0,
        tracked={},
    )

    assert placed is False
    assert broker.orders == []
    assert "OPTIONS_LIVE_BLOCKED reason=live_pilot_disabled" in caplog.text
    assert "OPTIONS_DISABLED_NON_PAPER_MODE" in caplog.text


def test_live_options_pilot_enabled_submits_one_contract_with_visibility_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = _live_pilot_config()
    broker = _LivePilotBroker()
    caplog.set_level(logging.INFO)

    placed = route_to_options_executor(
        cfg,
        _live_pilot_signal(),
        log_dt=datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc),
        account_equity=27_000.0,
        positions=[],
        broker=broker,
        execution_manager=_LivePilotExecution(),
        chain_candidates=_option_chain(),
        underlying_spot=350.0,
        tracked={},
    )

    assert placed is True
    assert len(broker.orders) == 1
    assert getattr(broker.orders[0], "quantity") == 1
    assert "OPTIONS_ALLOCATOR_ACCEPT symbol=SPY contract=QQQ260630C00350000 qty=1 debit=100.00" in caplog.text
    assert "OPTIONS_ORDER_INTENT symbol=SPY contract=QQQ260630C00350000 side=buy qty=1" in caplog.text
    assert "OPTIONS_ORDER_SUBMITTED symbol=SPY contract=QQQ260630C00350000 side=buy qty=1" in caplog.text


def test_live_options_pilot_max_position_count_enforced(caplog: pytest.LogCaptureFixture) -> None:
    cfg = _live_pilot_config()
    broker = _LivePilotBroker()
    caplog.set_level(logging.INFO)

    placed = route_to_options_executor(
        cfg,
        _live_pilot_signal(),
        log_dt=datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc),
        account_equity=27_000.0,
        positions=[{"symbol": "QQQ260630C00350000", "qty": 1, "cost_basis": 100.0}],
        broker=broker,
        execution_manager=_LivePilotExecution(),
        chain_candidates=_option_chain(),
        underlying_spot=350.0,
        tracked={},
    )

    assert placed is False
    assert broker.orders == []
    assert "OPTIONS_ALLOCATOR_REJECT symbol=SPY reason=max open option positions (1)" in caplog.text


def test_live_options_pilot_max_contracts_enforced(caplog: pytest.LogCaptureFixture) -> None:
    cfg = _live_pilot_config()
    cfg["options"]["per_trade"] = 0.01
    cfg["options"]["max_option_position_pct"] = 0.01
    broker = _LivePilotBroker()
    caplog.set_level(logging.INFO)

    placed = route_to_options_executor(
        cfg,
        _live_pilot_signal(),
        log_dt=datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc),
        account_equity=27_000.0,
        positions=[],
        broker=broker,
        execution_manager=_LivePilotExecution(),
        chain_candidates=_option_chain(mid=0.5),
        underlying_spot=350.0,
        tracked={},
    )

    assert placed is True
    assert getattr(broker.orders[0], "quantity") == 1
    assert "OPTIONS_ALLOCATOR_ACCEPT symbol=SPY contract=QQQ260630C00350000 qty=1 debit=50.00" in caplog.text


def test_live_options_pilot_exposure_cap_and_kill_switch_enforced(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = _live_pilot_config()
    cfg["options"]["max_option_positions"] = 5
    cfg["options"]["max_positions"] = 5
    cfg["options"]["max_open_option_positions"] = 5
    broker = _LivePilotBroker()
    caplog.set_level(logging.INFO)

    placed = route_to_options_executor(
        cfg,
        _live_pilot_signal(),
        log_dt=datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc),
        account_equity=27_000.0,
        positions=[{"symbol": "QQQ260630C00350000", "qty": 1, "cost_basis": 275.0}],
        broker=broker,
        execution_manager=_LivePilotExecution(),
        chain_candidates=_option_chain(),
        underlying_spot=350.0,
        tracked={},
    )

    assert placed is False
    assert broker.orders == []
    assert "OPTIONS_ALLOCATOR_REJECT symbol=SPY reason=options exposure cap" in caplog.text
    assert "OPTIONS_KILL_SWITCH triggered symbol=SPY reason=options exposure cap" in caplog.text


def test_route_false_underlying_logs_trade_stock_not_skip(capsys: pytest.CaptureFixture[str]) -> None:
    from src import entry_decision_log as _edl

    _edl.set_entry_skip_runtime_context(config={"entries": {"structured_skip_logs": False}})
    cfg = _base_config()
    sig = EntryRouteSignal(
        underlying="NVDA",
        direction="bullish",
        source="trend_long",
        stock_symbol="NVDA",
    )
    assert route_to_options_executor(cfg, sig, log_dt=None) is False
    out = capsys.readouterr().out
    assert "NVDA trade stock (trend_long)" in out
    assert "underlying not allowed for options" in out
    assert "skip" not in out.lower()


def test_paper_only_mode_allows_options_and_blocks_stock_fallback() -> None:
    cfg = _base_config()
    cfg["options"]["mode"] = "paper_only"
    sig = EntryRouteSignal(
        underlying="QQQ",
        direction="bullish",
        source="trend_long",
        stock_symbol="QQQ",
    )
    assert should_use_options(cfg, sig) is True
    assert not use_equity_fallback_after_options(
        cfg.get("options"),
        options_routing_attempted=True,
        options_order_placed=False,
    )


def test_log_options_stock_path_if_ineligible_structured_route(capsys: pytest.CaptureFixture[str]) -> None:
    import json

    from src import entry_decision_log as _edl

    _edl.set_entry_skip_runtime_context(
        user_id="u1",
        config={"entries": {"structured_skip_logs": True}},
        regime="neutral",
        position_count=1,
        cash_available=100.0,
    )
    cfg = _base_config()
    sig = EntryRouteSignal(
        underlying="NVDA",
        direction="bullish",
        source="trend_long",
        stock_symbol="NVDA",
    )
    from datetime import datetime
    import pytz

    et = pytz.timezone("America/New_York")
    log_dt = datetime(2026, 4, 10, 10, 0, 0, tzinfo=et)
    log_options_stock_path_if_ineligible(cfg, sig, log_dt)
    row = json.loads(capsys.readouterr().out.strip())
    assert row["symbol"] == "NVDA"
    assert row["decision"] == "route"
    assert row["reason"] == "options_not_allowed_trade_stock"
    assert row["signal"] == "trend_long"
    assert "detail" in row


def test_min_regime_for_long_blocks_low_score() -> None:
    cfg = _base_config()
    cfg["options"]["trend_long_options_require_sqqq_and_strong_trend"] = False
    cfg["options"]["min_regime_for_long"] = 2
    cfg["options"].pop("min_bullish_regime", None)
    assert not trend_long_options_extra_gate_ok(
        cfg, holding_sqqq=False, pct_above_50d=None, regime_score=1
    )
    assert trend_long_options_extra_gate_ok(
        cfg, holding_sqqq=False, pct_above_50d=None, regime_score=2
    )
    r = trend_long_options_extra_gate_reason(
        cfg, holding_sqqq=False, pct_above_50d=None, regime_score=1
    )
    assert r is not None and "regime score" in r.lower()


def test_min_bullish_regime_allows_neutral_score_and_label() -> None:
    cfg = _base_config()
    cfg["options"]["trend_long_options_require_sqqq_and_strong_trend"] = False
    cfg["options"]["min_bullish_regime"] = 4
    assert trend_long_options_extra_gate_ok(
        cfg,
        holding_sqqq=False,
        pct_above_50d=None,
        regime_score=3,
        regime_condition="neutral",
    )
    assert trend_long_options_extra_gate_ok(
        cfg, holding_sqqq=False, pct_above_50d=None, regime_score=3
    )
    assert trend_long_options_extra_gate_ok(
        cfg,
        holding_sqqq=False,
        pct_above_50d=None,
        regime_score=5,
        regime_condition="bullish",
    )


def test_min_bullish_regime_blocks_defensive() -> None:
    cfg = _base_config()
    cfg["options"]["trend_long_options_require_sqqq_and_strong_trend"] = False
    cfg["options"]["min_bullish_regime"] = 4
    assert not trend_long_options_extra_gate_ok(
        cfg,
        holding_sqqq=False,
        pct_above_50d=None,
        regime_score=4,
        regime_condition="defensive",
    )
    assert not trend_long_options_extra_gate_ok(
        cfg, holding_sqqq=False, pct_above_50d=None, regime_score=1
    )
    assert trend_long_options_extra_gate_reason(
        cfg,
        holding_sqqq=False,
        pct_above_50d=None,
        regime_score=1,
    ) == "trend-long options require bullish or neutral regime"


def test_min_bullish_regime_omitted_skipped() -> None:
    cfg = _base_config()
    cfg["options"]["trend_long_options_require_sqqq_and_strong_trend"] = False
    assert trend_long_options_extra_gate_ok(
        cfg, holding_sqqq=False, pct_above_50d=None, regime_score=None
    )


def test_min_bullish_regime_blocks_unlabeled_low_score() -> None:
    cfg = _base_config()
    cfg["options"]["trend_long_options_require_sqqq_and_strong_trend"] = False
    cfg["options"]["min_bullish_regime"] = 4
    assert not trend_long_options_extra_gate_ok(
        cfg,
        holding_sqqq=False,
        pct_above_50d=None,
        regime_score=None,
        regime_condition=None,
    )


def test_use_equity_fallback_not_attempted_always_stock() -> None:
    opts = {"fallback_to_stock": False}
    assert use_equity_fallback_after_options(
        opts,
        options_routing_attempted=False,
        options_order_placed=False,
    )


def test_use_equity_fallback_order_placed_no_stock() -> None:
    assert not use_equity_fallback_after_options(
        {"fallback_to_stock": True},
        options_routing_attempted=True,
        options_order_placed=True,
    )


def test_use_equity_fallback_attempted_failed_default_true() -> None:
    assert use_equity_fallback_after_options(
        {},
        options_routing_attempted=True,
        options_order_placed=False,
    )


def test_use_equity_fallback_attempted_failed_disabled() -> None:
    assert not use_equity_fallback_after_options(
        {"fallback_to_stock": False},
        options_routing_attempted=True,
        options_order_placed=False,
    )


def test_use_equity_fallback_allow_fallback_to_shares_false() -> None:
    assert not use_equity_fallback_after_options(
        {"allow_fallback_to_shares": False},
        options_routing_attempted=True,
        options_order_placed=False,
    )


def test_conviction_required_high_blocks_medium_score(signal_trend: EntryRouteSignal) -> None:
    cfg = _base_config()
    cfg["options"]["conviction_required"] = "high"
    assert should_use_options(cfg, signal_trend)
    sig_med = EntryRouteSignal(
        underlying="QQQ",
        direction="bullish",
        source="trend_long",
        stock_symbol="QQQ",
        conviction_score=0.5,
    )
    assert not should_use_options(cfg, sig_med)
    sig_strong = EntryRouteSignal(
        underlying="QQQ",
        direction="bullish",
        source="trend_long",
        stock_symbol="QQQ",
        conviction_score=0.9,
    )
    assert should_use_options(cfg, sig_strong)


def test_conviction_required_respects_explicit_band(signal_trend: EntryRouteSignal) -> None:
    cfg = _base_config()
    cfg["options"]["conviction_required"] = "medium"
    sig_weak = EntryRouteSignal(
        underlying="QQQ",
        direction="bullish",
        source="trend_long",
        stock_symbol="QQQ",
        conviction_band="weak",
    )
    assert not should_use_options(cfg, sig_weak)
