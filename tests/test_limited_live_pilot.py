from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from src.execution import OrderRequest, OrderType
from src.limited_live_pilot import (
    adjust_pilot_order_size,
    classify_broker_positions,
    finalize_pilot_submission_reservation,
    load_pilot_state,
    reserve_pilot_submission,
    trading_day_et,
    validate_pilot_order,
)
from src.trading_control import TradingControlBroker


def _config() -> dict:
    return {
        "trading_control": {
            "mode": "live",
            "live_pilot": {
                "enabled": True,
                "allowed_strategies": ["trend_long"],
                "preexisting_position_allowlist": ["AMZN", "NFLX"],
                "max_trades_per_day": 1,
                "max_entry_submissions_per_day": 1,
                "max_entry_fills_per_day": 1,
                "max_open_positions": 1,
                "max_notional_per_trade": 100,
                "max_total_deployed_notional": 100,
                "max_daily_loss_usd": 25,
                "allow_short_selling": False,
                "allow_add_to_existing": False,
                "allow_replacements": False,
                "allow_reallocation": False,
                "allow_overnight": False,
                "eod_flatten_required": True,
            },
        },
        "options": {"enabled": False, "live_pilot_enabled": False},
    }


def _order(symbol: str = "AAPL", notional: float = 50.0, route: str = "trend_long") -> OrderRequest:
    order = OrderRequest(symbol=symbol, side="buy", quantity=1, order_type=OrderType.MARKET, notional=notional)
    order.route = route
    return order


def test_trading_day_et_uses_new_york_not_naive_utc_date() -> None:
    assert trading_day_et(datetime(2026, 8, 4, 3, 30, tzinfo=timezone.utc)) == "2026-08-03"
    assert trading_day_et(datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc)) == "2026-03-08"
    assert trading_day_et(datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)) == "2026-11-01"
    assert trading_day_et(datetime(2027, 1, 1, 4, 30, tzinfo=timezone.utc)) == "2026-12-31"
    assert trading_day_et(datetime(2026, 8, 10, 8, 0, tzinfo=ZoneInfo("America/New_York"))) == "2026-08-10"


def test_bounded_live_sizing_resizes_normal_request_to_trade_cap(tmp_path) -> None:
    order = _order(notional=1200.0)

    decision = adjust_pilot_order_size(_config(), order, data_dir=tmp_path, user_id="u", reference_price=25.0)

    assert decision.allowed is True
    assert order.notional == 100.0
    assert order.quantity == 4.0
    assert order._allocator_requested_notional == 1200.0
    assert order._allocator_requested_qty == 1.0
    assert order._limited_live_final_quantity == 4.0
    assert order._limited_live_final_notional == 100.0
    assert order._limited_live_reference_price == 25.0


def test_bounded_live_sizing_preserves_request_below_trade_cap(tmp_path) -> None:
    order = _order(notional=80.0)

    decision = adjust_pilot_order_size(_config(), order, data_dir=tmp_path, user_id="u", reference_price=20.0)

    assert decision.allowed is True
    assert order.notional == 80.0
    assert order.quantity == 4.0


def test_bounded_live_sizing_respects_remaining_exposure(tmp_path) -> None:
    order = _order(notional=1200.0)

    decision = adjust_pilot_order_size(
        _config(),
        order,
        data_dir=tmp_path,
        user_id="u",
        reference_price=20.0,
        state={"deployed_notional": 60.0},
    )

    assert decision.allowed is True
    assert order.notional == 40.0
    assert order.quantity == 2.0


def test_bounded_live_sizing_uses_fractional_quantity(tmp_path) -> None:
    order = _order(notional=1200.0)

    decision = adjust_pilot_order_size(_config(), order, data_dir=tmp_path, user_id="u", reference_price=33.33)

    assert decision.allowed is True
    assert order.quantity == 3.0003
    assert order.notional == 99.99


def test_bounded_live_sizing_rounding_never_exceeds_trade_cap(tmp_path) -> None:
    order = _order(notional=1200.0)

    decision = adjust_pilot_order_size(_config(), order, data_dir=tmp_path, user_id="u", reference_price=33.335)

    assert decision.allowed is True
    assert order.notional <= 100.0


def test_non_fractionable_share_above_cap_blocks_without_resizing(tmp_path) -> None:
    order = _order(notional=1200.0)
    broker = SimpleNamespace(is_asset_fractionable=lambda symbol: False)

    decision = adjust_pilot_order_size(
        _config(),
        order,
        data_dir=tmp_path,
        user_id="u",
        broker=broker,
        reference_price=150.0,
    )

    assert decision.allowed is False
    assert decision.reason == "minimum_share_cost_exceeds_cap"
    assert order.notional == 1200.0


def test_zero_or_invalid_reference_price_blocks(tmp_path) -> None:
    order = _order(notional=1200.0)

    decision = adjust_pilot_order_size(_config(), order, data_dir=tmp_path, user_id="u", reference_price=0.0)

    assert decision.allowed is False
    assert decision.reason == "invalid_reference_price"


def test_normal_live_and_shadow_sizing_are_unchanged(tmp_path) -> None:
    live_disabled = _config()
    live_disabled["trading_control"]["live_pilot"]["enabled"] = False
    shadow = _config()
    shadow["trading_control"]["mode"] = "shadow"
    for cfg in (live_disabled, shadow):
        order = _order(notional=1200.0)

        decision = adjust_pilot_order_size(cfg, order, data_dir=tmp_path, user_id="u", reference_price=25.0)

        assert decision.allowed is True
        assert order.notional == 1200.0
        assert order.quantity == 1


def test_legacy_state_without_trading_date_is_not_loaded_as_current_day(tmp_path) -> None:
    state_path = tmp_path / "limited_live_pilot" / "2026-08-04_u.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        '{"entry_submissions": 1, "broker_dispatch_attempts": 1, "active_submission_reservations": 1, "entry_locked": true}',
        encoding="utf-8",
    )

    state = load_pilot_state(tmp_path, "u", "2026-08-04")

    assert state["entry_submissions"] == 0
    assert state["broker_dispatch_attempts"] == 0
    assert state["active_submission_reservations"] == 0
    assert state["entry_locked"] is False
    assert state["prior_or_ambiguous_state_ignored"] is True
    assert state["historical_active_submission_reservations"] == 1


def test_schema_state_with_different_trading_date_is_not_loaded_as_current_day(tmp_path) -> None:
    state_path = tmp_path / "limited_live_pilot" / "2026-08-10_u.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        '{"schema_version": 2, "trading_date": "2026-08-07", "entry_submissions": 1, "entry_locked": true}',
        encoding="utf-8",
    )

    state = load_pilot_state(tmp_path, "u", "2026-08-10")

    assert state["entry_submissions"] == 0
    assert state["entry_locked"] is False
    assert state["ignored_state_record_trading_date"] == "2026-08-07"


def test_reservation_persists_schema_and_trading_date(tmp_path) -> None:
    order = _order("MSFT")

    decision = reserve_pilot_submission(_config(), order, data_dir=tmp_path, user_id="u", day="2026-08-04")

    assert decision.allowed is True
    state = load_pilot_state(tmp_path, "u", "2026-08-04")
    assert state["schema_version"] == 2
    assert state["user_id"] == "u"
    assert state["environment"] == "live"
    assert state["trading_date"] == "2026-08-04"
    assert state["reservation_status"] == "active"
    assert state["broker_dispatch_attempted"] is True
    assert state["consumed_submission"] is True


def test_only_trend_long_can_submit(tmp_path) -> None:
    cfg = _config()
    assert validate_pilot_order(cfg, _order(route="trend_long"), data_dir=tmp_path, user_id="u").allowed
    blocked = validate_pilot_order(cfg, _order(route="dynamic_no_catalyst"), data_dir=tmp_path, user_id="u")
    assert blocked.allowed is False
    assert blocked.reason == "strategy_not_allowed"


def test_options_and_shorts_are_blocked(tmp_path) -> None:
    cfg = _config()
    option = _order("AAPL260717C00190000")
    assert validate_pilot_order(cfg, option, data_dir=tmp_path, user_id="u").reason == "options_blocked_live_pilot"
    short = _order()
    short.short = True
    assert validate_pilot_order(cfg, short, data_dir=tmp_path, user_id="u").reason == "short_selling_blocked"


def test_preexisting_allowlisted_symbols_block_all_pilot_actions(tmp_path) -> None:
    cfg = _config()
    assert validate_pilot_order(cfg, _order("AMZN"), data_dir=tmp_path, user_id="u").reason == "preexisting_position_symbol"
    assert validate_pilot_order(cfg, _order("NFLX"), data_dir=tmp_path, user_id="u").reason == "preexisting_position_symbol"
    sell = OrderRequest(symbol="AMZN", side="sell", quantity=1, order_type=OrderType.MARKET)
    sell.route = "trend_long"
    assert validate_pilot_order(cfg, sell, data_dir=tmp_path, user_id="u").reason == "preexisting_position_symbol"


def test_allowlisted_holdings_are_excluded_from_pilot_exposure_and_position_count() -> None:
    cfg = _config()
    positions = [
        SimpleNamespace(symbol="AMZN", qty="0.927483266", market_value="120.50"),
        SimpleNamespace(symbol="NFLX", qty="0.984126326", market_value="111.75"),
    ]
    report = classify_broker_positions(cfg, positions, pilot_state={"deployed_notional": 0, "submitted_symbols": []})

    assert report["broker_positions_total"] == 2
    assert report["preexisting_allowed_positions"] == 2
    assert report["pilot_managed_positions"] == 0
    assert report["unknown_positions"] == 0
    assert report["pilot_deployed_notional"] == 0.0
    assert report["preexisting_allowed_notional"] == 232.25
    assert [row["classification"] for row in report["position_classifications"]] == [
        "PREEXISTING_ALLOWED",
        "PREEXISTING_ALLOWED",
    ]


def test_unknown_third_position_is_not_allowlisted() -> None:
    cfg = _config()
    report = classify_broker_positions(
        cfg,
        [
            {"symbol": "AMZN", "market_value": 100},
            {"symbol": "TSLA", "market_value": 80},
        ],
        pilot_state={},
    )

    assert report["preexisting_allowed_positions"] == 1
    assert report["unknown_positions"] == 1


def test_notional_and_daily_loss_caps_block(tmp_path) -> None:
    cfg = _config()
    assert validate_pilot_order(cfg, _order(notional=101), data_dir=tmp_path, user_id="u").reason == "order_notional_above_cap"
    state = {"realized_plus_unrealized_pnl": -25.0}
    assert validate_pilot_order(cfg, _order(), data_dir=tmp_path, user_id="u", state=state).reason == "daily_loss_lock"


def test_submission_reservation_locks_day_and_same_symbol(tmp_path) -> None:
    cfg = _config()
    decision = reserve_pilot_submission(cfg, _order("MSFT"), data_dir=tmp_path, user_id="u", day="2026-07-30")
    assert decision.allowed is True
    state = load_pilot_state(tmp_path, "u", "2026-07-30")
    assert state["entry_submissions"] == 1
    assert state["broker_dispatch_attempts"] == 1
    assert state["active_submission_reservations"] == 1
    assert state["entry_locked"] is True
    assert reserve_pilot_submission(cfg, _order("MSFT"), data_dir=tmp_path, user_id="u", day="2026-07-30").reason == "pilot_entry_locked"


def test_submission_reservation_release_clears_active_marker_but_keeps_attempt(tmp_path) -> None:
    cfg = _config()
    order = _order("MSFT")
    decision = reserve_pilot_submission(cfg, order, data_dir=tmp_path, user_id="u", day="2026-07-30")
    assert decision.allowed is True

    finalize_pilot_submission_reservation(tmp_path, "u", day="2026-07-30", order=order)

    state = load_pilot_state(tmp_path, "u", "2026-07-30")
    assert state["active_submission_reservations"] == 0
    assert state["released_reservations"] == 1
    assert state["broker_dispatch_attempts"] == 1
    assert state["entry_submissions"] == 1
    assert validate_pilot_order(cfg, _order("AAPL"), data_dir=tmp_path, user_id="u", day="2026-07-30").reason == "pilot_entry_locked"


def test_pre_dispatch_strategy_block_does_not_consume_submission(tmp_path) -> None:
    cfg = _config()
    blocked = validate_pilot_order(cfg, _order(route="momentum_breakout"), data_dir=tmp_path, user_id="u")

    assert blocked.reason == "strategy_not_allowed"
    state = load_pilot_state(tmp_path, "u")
    assert state["entry_submissions"] == 0
    assert state["broker_dispatch_attempts"] == 0
    assert state["entry_locked"] is False


def test_missing_route_metadata_blocks_without_consuming_submission(tmp_path) -> None:
    cfg = _config()
    order = _order()
    order.route = ""
    order.source = ""

    blocked = validate_pilot_order(cfg, order, data_dir=tmp_path, user_id="u")

    assert blocked.reason == "metadata_route_missing"
    state = load_pilot_state(tmp_path, "u")
    assert state["entry_submissions"] == 0
    assert state["broker_dispatch_attempts"] == 0
    assert state["entry_locked"] is False


def test_pre_dispatch_notional_and_price_blocks_do_not_consume_submission(tmp_path) -> None:
    cfg = _config()
    assert validate_pilot_order(cfg, _order(notional=101), data_dir=tmp_path, user_id="u").reason == "order_notional_above_cap"
    invalid = _order(notional=1200)
    assert adjust_pilot_order_size(cfg, invalid, data_dir=tmp_path, user_id="u", reference_price=0).reason == "invalid_reference_price"
    state = load_pilot_state(tmp_path, "u")
    assert state["entry_submissions"] == 0
    assert state["broker_dispatch_attempts"] == 0
    assert state["entry_locked"] is False


def test_concurrent_submission_race_allows_at_most_one(tmp_path) -> None:
    cfg = _config()

    def attempt(i: int):
        return reserve_pilot_submission(cfg, _order(f"T{i}"), data_dir=tmp_path, user_id="u", day="2026-07-30")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))

    assert sum(1 for row in results if row.allowed) == 1
    assert load_pilot_state(tmp_path, "u", "2026-07-30")["entry_submissions"] == 1


def test_broker_boundary_does_not_call_sdk_when_gate_fails(tmp_path) -> None:
    cfg = _config()
    broker = SimpleNamespace(submit_order=lambda order: (_ for _ in ()).throw(AssertionError("called")))
    wrapped = TradingControlBroker(broker, config=cfg, paper=False, data_dir=tmp_path, user_id="u")
    try:
        wrapped.submit_order(_order(route="momentum_breakout"))
    except Exception as exc:
        assert "strategy_not_allowed" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected block")
    state = load_pilot_state(tmp_path, "u")
    assert state["entry_submissions"] == 0
    assert state["broker_dispatch_attempts"] == 0
    assert state["entry_locked"] is False


def test_broker_boundary_blocks_allowlisted_symbol_without_sdk_call(tmp_path) -> None:
    cfg = _config()
    broker = SimpleNamespace(submit_order=lambda order: (_ for _ in ()).throw(AssertionError("called")))
    wrapped = TradingControlBroker(broker, config=cfg, paper=False, data_dir=tmp_path, user_id="u")

    try:
        wrapped.submit_order(_order("NFLX"))
    except Exception as exc:
        assert "preexisting_position_symbol" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected block")
    state = load_pilot_state(tmp_path, "u")
    assert state["entry_submissions"] == 0
    assert state["broker_dispatch_attempts"] == 0
    assert state["entry_locked"] is False


def test_broker_boundary_blocks_allowlisted_sell_without_sdk_call(tmp_path) -> None:
    cfg = _config()
    broker = SimpleNamespace(submit_order=lambda order: (_ for _ in ()).throw(AssertionError("called")))
    wrapped = TradingControlBroker(broker, config=cfg, paper=False, data_dir=tmp_path, user_id="u")
    sell = OrderRequest(symbol="AMZN", side="sell", quantity=1, order_type=OrderType.MARKET)
    sell.route = "trend_long"

    try:
        wrapped.submit_order(sell)
    except Exception as exc:
        assert "preexisting_position_symbol" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected block")


def test_broker_boundary_blocks_invalid_pilot_sizing_without_sdk_call(tmp_path) -> None:
    cfg = _config()
    broker = SimpleNamespace(submit_order=lambda order: (_ for _ in ()).throw(AssertionError("called")))
    wrapped = TradingControlBroker(broker, config=cfg, paper=False, data_dir=tmp_path, user_id="u")

    try:
        wrapped.submit_order(_order(notional=1200.0))
    except Exception as exc:
        assert "invalid_reference_price" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected block")
    state = load_pilot_state(tmp_path, "u")
    assert state["entry_submissions"] == 0
    assert state["broker_dispatch_attempts"] == 0
    assert state["entry_locked"] is False


def test_broker_boundary_dispatch_consumes_attempt_and_preserves_metadata(tmp_path) -> None:
    cfg = _config()
    seen = {}

    def submit(order):
        seen["order"] = order
        return SimpleNamespace(id="ord-1", status="accepted")

    wrapped = TradingControlBroker(SimpleNamespace(submit_order=submit), config=cfg, paper=False, data_dir=tmp_path, user_id="live_bot")
    order = _order("XLF", notional=100)
    order.expected_price = 40
    order.source = "trend_long"
    order.strategy = "trend_long"

    result = wrapped.submit_order(order)

    assert result.id == "ord-1"
    assert seen["order"].user_id == "live_bot"
    assert seen["order"].route == "trend_long"
    assert seen["order"].source == "trend_long"
    assert seen["order"].strategy == "trend_long"
    state = load_pilot_state(tmp_path, "live_bot")
    assert state["entry_submissions"] == 1
    assert state["broker_dispatch_attempts"] == 1
    assert state["active_submission_reservations"] == 0
    assert state["released_reservations"] == 1
    assert state["entry_locked"] is True


def test_broker_exception_after_dispatch_consumes_attempt(tmp_path) -> None:
    cfg = _config()

    def submit(order):
        raise RuntimeError("broker rejected")

    wrapped = TradingControlBroker(SimpleNamespace(submit_order=submit), config=cfg, paper=False, data_dir=tmp_path, user_id="live_bot")
    order = _order("XLF", notional=100)
    order.expected_price = 40

    try:
        wrapped.submit_order(order)
    except RuntimeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected broker failure")

    state = load_pilot_state(tmp_path, "live_bot")
    assert state["entry_submissions"] == 1
    assert state["broker_dispatch_attempts"] == 1
    assert state["active_submission_reservations"] == 0
    assert state["entry_locked"] is True
