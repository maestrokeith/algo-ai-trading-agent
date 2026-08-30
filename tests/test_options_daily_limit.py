from __future__ import annotations

import json

from src.options_daily_limit import build_options_daily_limit_usage, format_options_daily_limit_usage


def _write_state(tmp_path, user_id: str, rows: list[dict]) -> None:
    (tmp_path / f"options_positions_{user_id}.json").write_text(
        json.dumps({"positions": {}, "history": rows}),
        encoding="utf-8",
    )


def _option_row(**overrides) -> dict:
    row = {
        "symbol": "QQQ260717C00350000",
        "entry_time": "2026-07-14T14:30:00+00:00",
        "entry_order_id": "order-1",
        "entry_order_status": "filled",
        "entry_fill_price": 1.25,
        "entry_reason": "source=trend_long; direction=bullish",
        "qty": 1,
        "contracts": 1,
    }
    row.update(overrides)
    return row


def test_filled_option_entry_counted_once(tmp_path) -> None:
    _write_state(tmp_path, "live_bot", [_option_row()])

    usage = build_options_daily_limit_usage(
        root=tmp_path,
        user_id="live_bot",
        environment="live",
        limit=1,
        trading_date="2026-07-14",
    )

    assert usage.counted == 1
    assert usage.excluded == 0
    assert usage.counted_records[0].option_contract_id == "QQQ260717C00350000"


def test_equity_trade_excluded(tmp_path) -> None:
    _write_state(tmp_path, "live_bot", [_option_row(symbol="QQQ")])

    usage = build_options_daily_limit_usage(
        root=tmp_path,
        user_id="live_bot",
        environment="live",
        limit=1,
        trading_date="2026-07-14",
    )

    assert usage.counted == 0
    assert usage.excluded_records[0].reason == "not_option_entry"


def test_rejected_cancelled_and_unfilled_option_orders_excluded(tmp_path) -> None:
    _write_state(
        tmp_path,
        "live_bot",
        [
            _option_row(entry_order_id="reject", entry_order_status="rejected", entry_fill_price=None),
            _option_row(entry_order_id="cancel", entry_order_status="cancelled", entry_fill_price=None),
            _option_row(entry_order_id="submitted", entry_order_status="submitted", entry_fill_price=None),
        ],
    )

    usage = build_options_daily_limit_usage(
        root=tmp_path,
        user_id="live_bot",
        environment="live",
        limit=1,
        trading_date="2026-07-14",
    )

    assert usage.counted == 0
    assert {row.reason for row in usage.excluded_records} == {"not_successfully_filled"}


def test_option_exit_excluded(tmp_path) -> None:
    _write_state(tmp_path, "live_bot", [_option_row(side="sell", entry_order_id="exit-1")])

    usage = build_options_daily_limit_usage(
        root=tmp_path,
        user_id="live_bot",
        environment="live",
        limit=1,
        trading_date="2026-07-14",
    )

    assert usage.counted == 0
    assert usage.excluded_records[0].reason == "not_entry_buy"


def test_duplicate_fill_event_counted_once(tmp_path) -> None:
    _write_state(
        tmp_path,
        "live_bot",
        [
            _option_row(entry_order_id="dup-order"),
            _option_row(entry_order_id="dup-order", entry_time="2026-07-14T14:31:00+00:00"),
        ],
    )

    usage = build_options_daily_limit_usage(
        root=tmp_path,
        user_id="live_bot",
        environment="live",
        limit=1,
        trading_date="2026-07-14",
    )

    assert usage.counted == 1
    assert usage.excluded_records[0].reason == "duplicate_entry"


def test_prior_day_trade_excluded(tmp_path) -> None:
    _write_state(tmp_path, "live_bot", [_option_row(entry_time="2026-07-13T14:30:00+00:00")])

    usage = build_options_daily_limit_usage(
        root=tmp_path,
        user_id="live_bot",
        environment="live",
        limit=1,
        trading_date="2026-07-14",
    )

    assert usage.counted == 0
    assert usage.excluded_records[0].reason == "different_trading_date"


def test_utc_to_america_new_york_boundary(tmp_path) -> None:
    _write_state(
        tmp_path,
        "live_bot",
        [
            _option_row(entry_order_id="late-13", entry_time="2026-07-14T01:55:00+00:00"),
            _option_row(entry_order_id="day-14", entry_time="2026-07-14T13:31:00+00:00"),
        ],
    )

    usage = build_options_daily_limit_usage(
        root=tmp_path,
        user_id="live_bot",
        environment="live",
        limit=1,
        trading_date="2026-07-14",
    )

    assert usage.counted == 1
    assert [row.trading_date_et for row in usage.records] == ["2026-07-13", "2026-07-14"]


def test_paper_live_isolation(tmp_path) -> None:
    _write_state(
        tmp_path,
        "live_bot",
        [_option_row(environment="paper"), _option_row(entry_order_id="live-order")],
    )

    usage = build_options_daily_limit_usage(
        root=tmp_path,
        user_id="live_bot",
        environment="live",
        limit=1,
        trading_date="2026-07-14",
    )

    assert usage.counted == 1
    assert usage.excluded_records[0].reason == "paper_record_in_live_counter"


def test_restart_persistence_does_not_double_count_open_and_history_duplicate(tmp_path) -> None:
    row = _option_row(entry_order_id="same-order")
    (tmp_path / "options_positions_live_bot.json").write_text(
        json.dumps({"positions": {"QQQ260717C00350000": row}, "history": [row]}),
        encoding="utf-8",
    )

    usage = build_options_daily_limit_usage(
        root=tmp_path,
        user_id="live_bot",
        environment="live",
        limit=1,
        trading_date="2026-07-14",
    )

    assert usage.counted == 1
    assert any(row.reason == "duplicate_entry" for row in usage.excluded_records)


def test_format_options_daily_limit_usage_lists_diagnostic_fields(tmp_path) -> None:
    _write_state(tmp_path, "live_bot", [_option_row()])

    usage = build_options_daily_limit_usage(
        root=tmp_path,
        user_id="live_bot",
        environment="live",
        limit=1,
        trading_date="2026-07-14",
    )
    text = "\n".join(format_options_daily_limit_usage(usage))

    assert "OPTIONS_DAILY_LIMIT_USAGE limit=1 counted=1 excluded=0 date=2026-07-14 timezone=America/New_York" in text
    assert "timestamp=2026-07-14T14:30:00+00:00" in text
    assert "asset_class=option" in text
    assert "counts=true" in text
