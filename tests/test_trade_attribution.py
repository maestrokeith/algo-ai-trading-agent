from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.trade_attribution import (
    attribution_daily_path,
    load_daily_artifact,
    record_allocator_candidate,
    record_candidate,
    record_exit,
    record_order_event,
    recent_core_rebuild_churn_symbols,
)


def test_load_daily_artifact_tolerates_corrupt_json_with_invalid_control_character(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "trade_attribution" / "daily" / "2026-06-15_paper_bot.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"orders": [{"symbol": "ASTN\u0001"}]}', encoding="utf-8")

    with caplog.at_level("WARNING", logger="src.trade_attribution"):
        payload = load_daily_artifact(path)

    assert payload["date"] == "2026-06-15"
    assert payload["user_id"] == "paper_bot"
    assert payload["orders"] == [{"symbol": "ASTN\u0001"}]
    assert "TRADE_ATTRIBUTION_CORRUPT_ARTIFACT" in caplog.text
    assert "TRADE_ATTRIBUTION_RECOVERED" in caplog.text
    assert str(path) in caplog.text
    assert "Traceback" not in caplog.text
    assert list(path.parent.glob(f"{path.name}.corrupt.*"))
    assert json.loads(path.read_text(encoding="utf-8"))["orders"][0]["symbol"] == "ASTN\u0001"


def test_record_event_recovers_malformed_daily_artifact_and_appends(tmp_path: Path) -> None:
    now = datetime(2026, 6, 15, 13, 45, tzinfo=timezone.utc)
    path = attribution_daily_path(data_dir=tmp_path, user_id="paper_bot", day=now.date())
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"orders": [{"symbol": "OLD", "note": "bad\u0001json"}]}',
        encoding="utf-8",
    )

    out = record_order_event(
        data_dir=tmp_path,
        user_id="paper_bot",
        timestamp=now,
        symbol="AMD",
        action="buy",
        route="dynamic_momentum_override",
        notional=1200.0,
        order_build_status="built",
        submit_attempt=True,
        submitted=True,
        order_id="order-amd",
    )

    assert out == path
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert [row["symbol"] for row in saved["orders"]] == ["OLD", "AMD"]
    assert saved["orders"][0]["note"] == "bad\u0001json"
    assert saved["summary"]["trades_entered"] == 1
    assert list(path.parent.glob(f"{path.name}.corrupt.*"))


def test_trade_attribution_writer_escapes_control_characters(tmp_path: Path) -> None:
    now = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)

    record_candidate(
        data_dir=tmp_path,
        user_id="paper_bot",
        timestamp=now,
        candidate={
            "symbol": "AMD",
            "source": "dynamic_universe",
            "catalyst_type": "earnings\u0001beat",
        },
    )

    path = attribution_daily_path(data_dir=tmp_path, user_id="paper_bot", day=now.date())
    raw = path.read_text(encoding="utf-8")
    assert "\u0001" not in raw
    parsed = json.loads(raw)
    assert parsed["candidates"][0]["catalyst_type"] == "earnings\u0001beat"


def test_trade_attribution_records_candidate_order_exit_summary(tmp_path: Path) -> None:
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)

    record_candidate(
        data_dir=tmp_path,
        user_id="live_bot",
        timestamp=now,
        candidate={
            "symbol": "BBCP",
            "route": "dynamic_universe",
            "source": "dynamic_universe",
            "dynamic_candidate": True,
            "final": False,
            "reason": "below_min_relative_volume",
            "news_score": 7.5,
            "event_score": 8.0,
            "catalyst_score": 8.5,
            "catalyst_type": "earnings",
            "relative_volume": 1.8,
            "day_gain_pct": 12.0,
            "spread_pct": 0.2,
            "vwap_above": True,
            "atr_expansion_ratio": 1.3,
        },
        regime_score=3,
    )
    record_allocator_candidate(
        data_dir=tmp_path,
        user_id="live_bot",
        timestamp=now,
        candidate={"symbol": "BBCP", "route": "dynamic_universe", "dynamic_candidate": True},
        selected_rank=1,
        action_created=True,
        target_notional=1200.0,
        final_notional=1100.0,
    )
    record_order_event(
        data_dir=tmp_path,
        user_id="live_bot",
        timestamp=now,
        symbol="BBCP",
        action="buy",
        route="dynamic_universe",
        notional=1100.0,
        order_build_status="built",
        submit_attempt=True,
        submitted=True,
        order_id="o-1",
    )
    record_order_event(
        data_dir=tmp_path,
        user_id="live_bot",
        timestamp=now,
        symbol="WEAK",
        action="buy",
        route="dynamic_universe",
        notional=200.0,
        order_build_status="rejected",
        reject_reason="notional_below_min_trade",
    )
    record_exit(
        data_dir=tmp_path,
        user_id="live_bot",
        timestamp=now,
        symbol="BBCP",
        qty=10,
        exit_reason="take_profit",
        pnl=25.0,
        pnl_pct=2.5,
        hold_minutes=22,
        entry_route="dynamic_universe",
    )

    path = attribution_daily_path(data_dir=tmp_path, user_id="live_bot", day=now.date())
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["candidates"][0]["symbol"] == "BBCP"
    assert payload["allocator_candidates"][0]["selected_rank"] == 1
    assert payload["orders"][0]["submitted"] is True
    assert payload["exits"][0]["exit_reason"] == "take_profit"
    assert payload["summary"]["trades_entered"] == 1
    assert payload["summary"]["trades_exited"] == 1
    assert payload["summary"]["churn_count_under_30m"] == 1
    assert payload["summary"]["churn_under_30m_by_route"] == {"dynamic_momentum": 1}
    assert payload["summary"]["top_rejection_reasons"] == {"below_min_relative_volume": 1}
    assert payload["summary"]["top_order_build_rejects"] == {"notional_below_min_trade": 1}
    assert payload["summary"]["pnl_by_route"] == {"dynamic_momentum": pytest.approx(25.0)}


def test_trade_attribution_summary_infers_exit_route_from_submitted_order(tmp_path: Path) -> None:
    now = datetime(2026, 6, 25, 14, 30, tzinfo=timezone.utc)
    record_order_event(
        data_dir=tmp_path,
        user_id="paper_bot",
        timestamp=now,
        symbol="MEI",
        action="buy",
        route="dynamic_momentum_override",
        source="dynamic_universe",
        order_build_status="built",
        submit_attempt=True,
        submitted=True,
        order_id="mei-buy",
    )
    record_exit(
        data_dir=tmp_path,
        user_id="paper_bot",
        timestamp=now,
        symbol="MEI",
        exit_reason="dynamic_eod_flatten",
        pnl=86.80,
        pnl_pct=8.68,
        hold_minutes=90,
    )

    payload = load_daily_artifact(attribution_daily_path(data_dir=tmp_path, user_id="paper_bot", day=now.date()))

    assert payload["summary"]["pnl_by_route"] == {"dynamic_momentum": pytest.approx(86.8)}
    assert "unknown" not in payload["summary"]["pnl_by_route"]


def test_trade_attribution_logs_successful_order_and_exit_writes(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime(2026, 6, 16, 14, 30, tzinfo=timezone.utc)

    with caplog.at_level("INFO", logger="src.trade_attribution"):
        record_order_event(
            data_dir=tmp_path,
            user_id="paper_bot",
            timestamp=now,
            symbol="AAL",
            action="buy",
            route="dynamic_momentum_override",
            source="capital_allocator",
            notional=4633.19,
            qty=291.394968553,
            order_build_status="built",
            submit_attempt=True,
            submitted=True,
            order_id="aal-buy",
            status="accepted",
            filled_qty=291.394968553,
            filled_avg_price=15.90,
            dynamic_candidate=True,
            news_score=9.0466,
            catalyst_score=0.90466,
            relative_volume=0.7548560501565609,
            gain_pct=2.845134173941144,
        )
        record_exit(
            data_dir=tmp_path,
            user_id="paper_bot",
            timestamp=now,
            symbol="AAL",
            qty=291.394968553,
            exit_reason="dynamic_eod_flatten",
            pnl=12.34,
            pnl_pct=0.27,
            hold_minutes=347,
            entry_route="dynamic_momentum_override",
            entry_source="dynamic_universe",
        )

    payload = load_daily_artifact(attribution_daily_path(data_dir=tmp_path, user_id="paper_bot", day=now.date()))
    assert payload["orders"][0]["filled_qty"] == pytest.approx(291.394968553)
    assert payload["orders"][0]["dynamic_candidate"] is True
    assert payload["orders"][0]["catalyst_score"] == pytest.approx(0.90466)
    assert payload["exits"][0]["pnl"] == pytest.approx(12.34)
    assert payload["summary"]["trades_entered"] == 1
    assert payload["summary"]["trades_exited"] == 1
    assert "TRADE_ATTRIBUTION_WRITE_OK section=orders user_id=paper_bot" in caplog.text
    assert "TRADE_ATTRIBUTION_WRITE_OK section=exits user_id=paper_bot" in caplog.text


def test_recent_core_rebuild_churn_symbols_filters_short_recent_core_rebuild_exits(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 6, 6, 15, 0, tzinfo=timezone.utc)
    record_exit(
        data_dir=tmp_path,
        user_id="live_bot",
        timestamp=datetime(2026, 6, 6, 14, 45, tzinfo=timezone.utc),
        symbol="AAPL",
        exit_reason="signal_flip",
        hold_minutes=12,
        entry_route="core_rebuild",
    )
    record_exit(
        data_dir=tmp_path,
        user_id="live_bot",
        timestamp=datetime(2026, 6, 6, 14, 45, tzinfo=timezone.utc),
        symbol="MSFT",
        hold_minutes=90,
        entry_route="core_rebuild",
    )
    record_exit(
        data_dir=tmp_path,
        user_id="live_bot",
        timestamp=datetime(2026, 6, 6, 14, 45, tzinfo=timezone.utc),
        symbol="NVDA",
        hold_minutes=10,
        entry_route="trend_long",
    )

    churn = recent_core_rebuild_churn_symbols(
        data_dir=tmp_path,
        user_id="live_bot",
        now=now,
        max_hold_minutes=30,
        cooldown_minutes=180,
        lookback_days=1,
    )

    assert set(churn) == {"AAPL"}
    assert churn["AAPL"]["exit_reason"] == "signal_flip"
    assert churn["AAPL"]["hold_minutes"] == pytest.approx(12)
