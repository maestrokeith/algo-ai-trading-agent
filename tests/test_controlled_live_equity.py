from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scripts import limited_live_readiness as readiness
from src.controlled_live_equity import (
    PREMARKET_PENDING_FIRST_EXIT_CYCLE,
    bounded_live_pilot_active,
    controlled_live_equity_active,
    controlled_live_limit_blockers,
    controlled_live_limits,
    runtime_profile,
)
from src.execution import OrderRequest, OrderType
from src.limited_live_pilot import load_pilot_state
from src.trading_control import EntryBlocked, TradingControlBroker, authorize_order_submission


def _controlled_config() -> dict:
    return {
        "trading_control": {
            "mode": "live",
            "runtime_profile": "controlled_live_equity",
            "strategy_states": {
                "trend_long": "LIVE",
                "momentum_breakout": "SHADOW",
                "dynamic_no_catalyst": "SHADOW",
                "news_only": "DISABLED",
                "options_live": "DISABLED",
                "options_paper": "DISABLED",
            },
            "live_pilot": {
                "enabled": False,
                "allowed_strategies": ["trend_long"],
                "preexisting_position_allowlist": ["AMZN", "NFLX"],
                "eod_flatten_required": True,
            },
            "controlled_live_equity": {
                "enabled": True,
                "max_managed_positions": 10,
                "max_single_order_notional_pct": 0.12,
                "max_single_order_notional": 5000,
                "max_symbol_exposure_pct": 15,
                "strategy_allocation_cap_pct": 60,
                "portfolio_exposure_cap_pct": 85,
                "stock_capital_pct": 60,
                "min_cash_reserve_pct": 12,
                "daily_loss_limit_pct": 3,
            },
        },
        "portfolio": {
            "max_positions": 10,
            "max_stock_capital_pct": 60,
            "max_single_position_pct": 22,
            "min_cash_reserve_pct": 12,
            "exposure_gates": {"enabled": True, "max_total_exposure_frac": 0.95},
            "capital_allocator": {
                "enabled": True,
                "max_positions": 10,
                "max_single_order_notional_pct": 0.12,
                "max_single_order_notional": 5000,
            },
        },
        "risk": {"max_symbol_allocation_pct": {"default": 15, "etf": 22}, "max_total_exposure_pct": 85},
        "portfolio_risk": {"max_daily_loss_pct": 3.0},
        "options": {"enabled": False, "live_pilot_enabled": False, "live_pilot": {"enabled": False}},
    }


def _order(symbol: str = "XLF", route: str = "trend_long", notional: float = 1312.5) -> OrderRequest:
    order = OrderRequest(symbol=symbol, side="buy", quantity=4, order_type=OrderType.MARKET, notional=notional)
    order.route = route
    order.source = route
    order.strategy = route
    order.expected_price = 32.8
    return order


class _Broker:
    paper = False

    def __init__(self, positions=()) -> None:
        self.positions = list(positions)
        self.submitted: list[OrderRequest] = []

    def get_positions(self):
        return list(self.positions)

    def submit_order(self, order):
        self.submitted.append(order)
        return SimpleNamespace(id=f"ord-{len(self.submitted)}", status="accepted")


def test_controlled_live_profile_uses_normal_caps_and_not_bounded_pilot() -> None:
    cfg = _controlled_config()

    limits = controlled_live_limits(cfg)

    assert runtime_profile(cfg) == "controlled_live_equity"
    assert controlled_live_equity_active(cfg) is True
    assert bounded_live_pilot_active(cfg) is False
    assert controlled_live_limit_blockers(cfg) == []
    assert limits.max_managed_positions == 10
    assert limits.per_order_max_notional == 5000
    assert limits.per_order_max_pct == pytest.approx(12)
    assert limits.per_symbol_max_pct == pytest.approx(15)
    assert limits.strategy_allocation_cap_pct == pytest.approx(60)
    assert limits.portfolio_exposure_cap_pct == pytest.approx(85)
    assert limits.daily_loss_limit_pct == pytest.approx(3)


def test_controlled_live_allows_multiple_trend_long_submissions_without_pilot_reservation(tmp_path) -> None:
    cfg = _controlled_config()
    broker = _Broker()
    wrapped = TradingControlBroker(broker, config=cfg, paper=False, data_dir=tmp_path, user_id="live_bot")

    first = wrapped.submit_order(_order("XLF"))
    second = wrapped.submit_order(_order("XLE"))

    assert first.id == "ord-1"
    assert second.id == "ord-2"
    assert [o.notional for o in broker.submitted] == [1312.5, 1312.5]
    state = load_pilot_state(tmp_path, "live_bot")
    assert state["entry_submissions"] == 0
    assert state["broker_dispatch_attempts"] == 0
    assert state["entry_locked"] is False


def test_controlled_live_still_blocks_shadow_strategy_and_protected_holding(tmp_path) -> None:
    cfg = _controlled_config()
    broker = _Broker()
    wrapped = TradingControlBroker(broker, config=cfg, paper=False, data_dir=tmp_path, user_id="live_bot")

    with pytest.raises(EntryBlocked, match="strategy_not_allowed"):
        wrapped.submit_order(_order("MSFT", route="momentum_breakout"))
    with pytest.raises(EntryBlocked, match="preexisting_position_symbol"):
        wrapped.submit_order(_order("AMZN"))
    assert broker.submitted == []


def test_authorize_live_sell_protects_amzn_and_nflx_outside_pilot() -> None:
    cfg = _controlled_config()
    sell = OrderRequest(symbol="NFLX", side="sell", quantity=1, order_type=OrderType.MARKET)
    sell.route = "trend_long"

    allowed, reason = authorize_order_submission(cfg, sell, paper=False)

    assert allowed is False
    assert reason == "preexisting_position_symbol"


def test_controlled_live_regular_session_blocks_entries_when_exit_manager_stale(tmp_path, monkeypatch) -> None:
    cfg = _controlled_config()
    state_path = tmp_path / "limited_live_pilot" / "2026-08-05_live_bot.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"trading_date": "2026-08-05", "broker_dispatch_attempts": 1, "submitted_symbols": ["IWM"]}), encoding="utf-8")
    broker = _Broker(positions=[{"symbol": "IWM", "qty": "0.330702357", "market_value": "99"}])
    monkeypatch.setattr("src.controlled_live_equity.regular_session_open", lambda now=None: True)
    monkeypatch.setattr("src.limited_live_pilot.trading_day_et", lambda now=None: "2026-08-11")
    wrapped = TradingControlBroker(broker, config=cfg, paper=False, data_dir=tmp_path, user_id="live_bot")

    with pytest.raises(EntryBlocked, match="exit_manager_unhealthy:IWM"):
        wrapped.submit_order(_order("XLF"))

    assert broker.submitted == []


def test_controlled_live_readiness_premarket_allows_pending_first_exit_cycle(tmp_path, monkeypatch) -> None:
    cfg = _controlled_config()
    state_path = tmp_path / "data" / "limited_live_pilot" / "2026-08-05_live_bot.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"trading_date": "2026-08-05", "broker_dispatch_attempts": 1, "submitted_symbols": ["IWM"]}), encoding="utf-8")
    fake_broker = SimpleNamespace(
        paper=False,
        _trading=SimpleNamespace(get_account=lambda: SimpleNamespace(status="ACTIVE", trading_blocked=False, account_blocked=False)),
        list_orders=lambda status="open": [],
        get_positions=lambda: [{"symbol": "IWM", "qty": "0.330702357", "market_value": "99"}],
    )

    class Manager:
        def get_user(self, user):
            return SimpleNamespace(user_id=user, paper=False, config=cfg)

        def get_broker(self, user):
            return fake_broker

    monkeypatch.setattr(readiness, "load_config", lambda path: cfg)
    monkeypatch.setattr(readiness, "UserManager", lambda *args, **kwargs: Manager())
    monkeypatch.setattr(readiness, "_git_state", lambda root: {"head": "abc", "working_tree_clean": True, "origin_sync": "0\t0", "origin_synced": True})
    monkeypatch.setattr(readiness, "build_canonical_day", lambda **kwargs: {"counts": {}})
    monkeypatch.setattr(readiness, "premarket_pending_first_exit_cycle_allowed", lambda **kwargs: True)

    report = readiness.build_limited_live_readiness(root=tmp_path, user="live_bot", day="2026-08-11")

    assert report["runtime_profile"] == "controlled_live_equity"
    assert report["ready"] is True
    assert report["positions_today"] == 1
    assert report["managed_positions_missing_exit_registration"] == ["IWM"]
    assert report["exit_management_status"] == PREMARKET_PENDING_FIRST_EXIT_CYCLE
    assert report["premarket_exit_health_state"] == PREMARKET_PENDING_FIRST_EXIT_CYCLE
    assert "pilot_open_position_present" not in report["blocking_reasons"]
    assert "pilot_position_exit_management_stale:IWM" not in report["blocking_reasons"]
