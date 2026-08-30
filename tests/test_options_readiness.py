from __future__ import annotations

import json
from datetime import datetime

from src.options_readiness import build_options_readiness, format_options_readiness, format_startup_options_config


def _cfg(**options_overrides):
    options = {
        "enabled": True,
        "mode": "live_long_premium",
        "live_pilot_enabled": True,
        "only_buy_options": True,
        "allowed_contract_types": ["call", "put"],
        "entry_mapping": {"bullish_signal": "call", "bearish_signal": "put"},
        "allowed_underlyings": ["SPY", "QQQ"],
        "total_exposure_limit": 0.01,
        "per_trade": 0.005,
        "max_premium_per_trade": 500,
        "max_option_positions": 1,
        "max_positions": 1,
        "max_contracts_per_trade": 1,
        "max_option_trades_per_day": 1,
        "max_daily_options_loss_dollars": 250,
        "max_bid_ask_spread_pct": 12.0,
        "never_bypass_stock_risk_caps": True,
        "bypass_when_full": {"allow_when_full": False},
        "contract_selection": {"min_open_interest": 500, "min_volume": 100},
        "target_dte_min": 7,
        "target_dte_max": 21,
    }
    options.update(options_overrides)
    return {"options": options}


class _LiveBroker:
    paper = False

    def get_option_chain_candidates(self, *args, **kwargs):
        raise AssertionError("readiness must not fetch chains")


def test_live_options_readiness_active_for_live_long_premium(tmp_path) -> None:
    status = build_options_readiness(
        _cfg(),
        environment="live",
        user_id="live_bot",
        root=tmp_path,
        broker=_LiveBroker(),
    )

    assert status.final_status == "ready"
    assert status.route_active is True
    assert status.long_premium_only is True
    assert status.runtime_entry_enabled is True
    assert status.option_chain_access is True
    assert status.broker_options_permission is True
    assert status.buying_power_ready is True
    assert status.exit_automation_ready is True
    assert status.options_trade_executed_today is False
    assert status.max_positions == 1
    assert status.max_trades_per_day == 1
    assert status.max_contracts_per_trade == 1
    assert "final_status=active" in format_startup_options_config(status)
    lines = format_options_readiness(status)
    assert "OPTIONS_READINESS user=live_bot environment=live" in lines
    assert "runtime_entry_enabled=true" in lines
    assert "option_chain_access=true" in lines
    assert "broker_options_permission=true" in lines
    assert "buying_power_ready=true" in lines
    assert "exit_automation_ready=true" in lines
    assert "options_trade_executed_today=false" in lines
    assert any(line.startswith("OPTIONS_DAILY_LIMIT_USAGE limit=1 counted=0") for line in lines)


def test_live_options_readiness_blocks_disabled_pilot_mode_and_options(tmp_path) -> None:
    status = build_options_readiness(
        _cfg(enabled=False, live_pilot_enabled=False, mode="paper_only"),
        environment="live",
        user_id="live_bot",
        root=tmp_path,
        broker=_LiveBroker(),
    )

    assert status.final_status == "inactive"
    assert "options_disabled" in status.blocking_reasons
    assert "live_pilot_disabled" in status.blocking_reasons
    assert "mode_not_live" in status.blocking_reasons


def test_options_readiness_enforces_long_premium_only(tmp_path) -> None:
    status = build_options_readiness(
        _cfg(only_buy_options=False),
        environment="live",
        user_id="live_bot",
        root=tmp_path,
        broker=_LiveBroker(),
    )

    assert status.long_premium_only is False
    assert "not_long_premium_only" in status.blocking_reasons


def test_options_readiness_enforces_position_and_trade_limits(tmp_path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    now = datetime.now().astimezone()
    (data / "options_positions_live_bot.json").write_text(
        json.dumps(
            {
                "positions": {"SPY260717C00500000": {"qty": 1}},
                "history": [
                    {
                        "symbol": "SPY260717C00500000",
                        "entry_time": now.isoformat(),
                        "entry_order_id": "filled-1",
                        "entry_order_status": "filled",
                        "entry_fill_price": 1.0,
                        "entry_reason": "source=trend_long",
                        "qty": 1,
                        "contracts": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    status = build_options_readiness(
        _cfg(),
        environment="live",
        user_id="live_bot",
        root=tmp_path,
        broker=_LiveBroker(),
    )

    assert "position_limit" in status.blocking_reasons
    assert "daily_trade_limit" in status.blocking_reasons
    assert status.positions_open == 1


def test_options_readiness_detects_broker_capability_failure(tmp_path) -> None:
    broker = type("NoOptionsBroker", (), {"paper": False})()
    status = build_options_readiness(
        _cfg(),
        environment="live",
        user_id="live_bot",
        root=tmp_path,
        broker=broker,
    )

    assert status.broker_supported is False
    assert "broker_not_supported" in status.blocking_reasons


def test_options_readiness_detects_broker_permission_failure(tmp_path) -> None:
    broker = type(
        "NoOptionsPermissionBroker",
        (),
        {
            "paper": False,
            "options_trading_enabled": False,
            "get_option_chain_candidates": lambda self, *args, **kwargs: [],
        },
    )()
    status = build_options_readiness(
        _cfg(),
        environment="live",
        user_id="live_bot",
        root=tmp_path,
        broker=broker,
    )

    assert status.broker_supported is True
    assert status.broker_options_permission is False
    assert "broker_options_not_supported" in status.blocking_reasons


def test_paper_options_readiness_does_not_require_live_pilot(tmp_path) -> None:
    status = build_options_readiness(
        _cfg(mode="paper_only", live_pilot_enabled=False),
        environment="paper",
        user_id="paper_bot",
        root=tmp_path,
    )

    assert "live_pilot_disabled" not in status.blocking_reasons
