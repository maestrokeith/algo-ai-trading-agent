from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from src import trading_diagnostics as td
from src.dynamic_entry_adaptive import resolve_adaptive_sensitivity
from src.runtime_progress import record_runtime_event
from src.trade_attribution import record_order_event
from src.trading_lifecycle import (
    build_canonical_day,
    canonical_decision_key,
    event_trading_date_et,
    lifecycle_record_class,
    quarantine_candidates,
    validate_live_persistence_record,
)


def _artifact(root: Path, day: str = "2026-07-22", user: str = "live_bot", payload: dict | None = None) -> Path:
    path = root / "data" / "trade_attribution" / "daily" / f"{day}_{user}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            payload
            or {
                "date": day,
                "user_id": user,
                "candidates": [
                    {"timestamp": f"{day}T13:30:00+00:00", "symbol": "AAPL", "route": "trend_long", "accepted": True},
                    {"timestamp": f"{day}T13:31:00+00:00", "symbol": "AAPL", "route": "trend_long", "accepted": False, "reason": "entry_alignment"},
                ],
                "allocator_candidates": [{"timestamp": f"{day}T13:31:30+00:00", "symbol": "AAPL", "route": "trend_long", "action_created": True}],
                "orders": [
                    {"timestamp": f"{day}T13:32:00+00:00", "symbol": "AAPL", "action": "buy", "submitted": True, "order_id": "o1", "status": "filled", "qty": 1, "filled_qty": 1, "filled_avg_price": 10},
                    {"timestamp": f"{day}T13:32:00+00:00", "symbol": "AAPL", "action": "buy", "submitted": True, "order_id": "o1", "status": "filled", "qty": 1, "filled_qty": 1, "filled_avg_price": 10},
                ],
                "exits": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_canonical_day_counts_match_audit_and_day_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _artifact(tmp_path)
    monkeypatch.setattr(td, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(td, "REPORT_ROOT", tmp_path / "reports")
    canonical = build_canonical_day(root=tmp_path, day="2026-07-22", user_id="live_bot")
    audit = td.run_trading_audit(root=tmp_path, day="2026-07-22", user="live_bot")
    review = td.day_review_report(root=tmp_path, day="2026-07-22", user="live_bot")
    assert review["canonical_funnel"]["unique_entry_decisions"] == canonical["counts"]["unique_entry_decisions"]
    assert audit.report["counts"]["unique_fills"] == canonical["counts"]["unique_fills"]
    assert review["canonical_funnel"]["unique_fills"] == audit.report["counts"]["unique_fills"]
    assert review["hypothetical_performance"]["trades"] == 0
    assert td.day_review_main(["--date", "2026-07-22", "--user", "live_bot", "--json"]) == 0
    assert (tmp_path / "reports" / "day_review" / "2026-07-22.json").exists()
    assert (tmp_path / "reports" / "day_review" / "2026-07-22.md").exists()


def test_replay_records_excluded_from_live_evidence_and_quarantine_candidates(tmp_path: Path) -> None:
    _artifact(
        tmp_path,
        payload={
            "date": "2026-07-22",
            "user_id": "live_bot",
            "candidates": [{"timestamp": "2026-07-22T13:30:00+00:00", "symbol": "CRWV", "route": "dynamic_replay", "accepted": True}],
            "allocator_candidates": [],
            "orders": [
                {"timestamp": "2026-07-22T13:31:00+00:00", "symbol": "CRWV", "action": "buy", "submitted": True, "order_id": "replay-1", "status": "n/a", "filled_qty": 12, "qty": 12, "route": "premarket_catalyst_replay"},
                {"timestamp": "2026-07-22T13:32:00+00:00", "symbol": "CRWV", "action": "buy", "submitted": True, "order_id": "replay-1", "status": "n/a", "filled_qty": 12, "qty": 12, "route": "premarket_catalyst_replay"},
            ],
            "exits": [],
        },
    )
    canonical = build_canonical_day(root=tmp_path, day="2026-07-22", user_id="live_bot")
    assert canonical["counts"]["synthetic_or_replay_order_events"] == 0
    assert canonical["counts"]["contaminated_fill_events"] == 0
    assert canonical["counts"]["raw_fill_events"] == 0
    assert canonical["counts"]["raw_position_records"] == 0
    assert canonical["counts"]["unique_fills"] == 0
    assert canonical["counts"]["unique_opened_positions"] == 0
    assert canonical["counts"]["unique_still_open_positions"] == 0
    assert canonical["counts"]["replay_research_outcomes"] == 2
    assert canonical["counts"]["duplicate_replay_research_outcomes"] == 1
    assert quarantine_candidates(canonical) == []
    report = td.day_review_report(root=tmp_path, day="2026-07-22", user="live_bot")
    assert report["data_integrity"]["lifecycle_status"]["status"] in {"CLEAN", "PARTIAL"}
    assert report["system_and_safety_state"]["replay_contamination"] == 0
    assert report["canonical_funnel"]["unique_fills"] == 0
    assert report["open_positions"]["current_day_lifecycle_positions"] == {}


def test_malformed_fake_live_fill_remains_quarantined(tmp_path: Path) -> None:
    _artifact(
        tmp_path,
        payload={
            "date": "2026-07-22",
            "user_id": "live_bot",
            "candidates": [],
            "allocator_candidates": [],
            "orders": [
                {
                    "timestamp": "2026-07-22T13:31:00+00:00",
                    "symbol": "CRWV",
                    "action": "buy",
                    "submitted": False,
                    "status": "filled",
                    "filled_qty": 12,
                    "qty": 12,
                    "environment": "live",
                }
            ],
            "exits": [],
        },
    )
    canonical = build_canonical_day(root=tmp_path, day="2026-07-22", user_id="live_bot")

    assert lifecycle_record_class(canonical["order_sources"][0].row) == "AMBIGUOUS_MALFORMED"
    assert canonical["counts"]["contaminated_fill_events"] == 1
    assert canonical["counts"]["unresolved_contamination"] == 1
    assert canonical["counts"]["unique_fills"] == 0
    assert quarantine_candidates(canonical)[0]["quarantine_reason"] == "LIVE_DATA_CONTAMINATION_BLOCKED"


def test_shadow_records_are_expected_lifecycle_not_quarantine(tmp_path: Path) -> None:
    _artifact(
        tmp_path,
        payload={
            "date": "2026-07-22",
            "user_id": "live_bot",
            "candidates": [
                {
                    "timestamp": "2026-07-22T13:30:00+00:00",
                    "symbol": "XLF",
                    "route": "trend_long",
                    "accepted": True,
                    "environment": "shadow",
                    "hypothetical": True,
                }
            ],
            "allocator_candidates": [
                {
                    "timestamp": "2026-07-22T13:31:00+00:00",
                    "symbol": "XLF",
                    "route": "trend_long",
                    "action_created": True,
                    "environment": "shadow",
                    "hypothetical": True,
                }
            ],
            "orders": [
                {
                    "timestamp": "2026-07-22T13:32:00+00:00",
                    "symbol": "XLF",
                    "action": "buy",
                    "submit_attempt": True,
                    "submitted": False,
                    "order_id": "shadow-20260722T133200Z",
                    "status": "shadow",
                    "environment": "shadow",
                    "hypothetical": True,
                    "broker_dispatch_attempted": False,
                    "execution_allowed": False,
                    "filled_qty": None,
                    "filled_avg_price": None,
                }
            ],
            "exits": [],
        },
    )

    canonical = build_canonical_day(root=tmp_path, day="2026-07-22", user_id="live_bot")
    counts = canonical["counts"]

    assert counts["raw_submitted_order_events"] == 0
    assert counts["unique_submitted_orders"] == 0
    assert counts["synthetic_or_replay_order_events"] == 0
    assert counts["replay_event_count"] == 0
    assert counts["shadow_decisions"] == 1
    assert counts["shadow_allocator_actions"] == 1
    assert counts["shadow_order_intents"] == 1
    assert quarantine_candidates(canonical) == []
    report = td.day_review_report(root=tmp_path, day="2026-07-22", user="live_bot")
    assert report["data_integrity"]["lifecycle_status"]["status"] in {"CLEAN", "PARTIAL"}
    assert report["data_integrity"]["replay_mock_records"] == 0
    assert report["shadow_lifecycle"]["shadow_order_intents"] == 1


def test_day_review_strictly_filters_sources_by_et_event_date(tmp_path: Path) -> None:
    _artifact(
        tmp_path,
        payload={
            "date": "2026-07-22",
            "user_id": "live_bot",
            "candidates": [
                {"timestamp": "2026-07-22T23:30:00+00:00", "symbol": "AAPL", "route": "trend_long", "accepted": True},
                {"timestamp": "2026-07-23T04:30:00+00:00", "symbol": "MSFT", "route": "trend_long", "accepted": True},
            ],
            "allocator_candidates": [
                {"timestamp": "2026-07-22T23:35:00+00:00", "symbol": "AAPL", "route": "trend_long", "action_created": True},
                {"timestamp": "2026-07-23T04:35:00+00:00", "symbol": "MSFT", "route": "trend_long", "action_created": True},
            ],
            "orders": [
                {"timestamp": "2026-07-22T23:40:00+00:00", "symbol": "AAPL", "action": "buy", "submitted": True, "order_id": "o1", "status": "filled", "qty": 1, "filled_qty": 1},
                {"timestamp": "2026-07-23T04:40:00+00:00", "symbol": "MSFT", "action": "buy", "submitted": True, "order_id": "o2", "status": "filled", "qty": 1, "filled_qty": 1},
            ],
            "exits": [],
        },
    )
    integrity = tmp_path / "data" / "integrity"
    integrity.mkdir(parents=True)
    (integrity / "live_bot.json").write_text(
        json.dumps(
            {
                "incidents": [
                    {"timestamp": "2026-07-22T23:50:00+00:00", "reason_code": "ENTRY_BLOCKED_SYSTEM_ERROR"},
                    {"timestamp": "2026-07-23T04:10:00+00:00", "reason_code": "ENTRY_BLOCKED_SYSTEM_ERROR"},
                    {"timestamp": "2026-07-22T18:00:00+00:00", "reason_code": "ENTRY_BLOCKED_MODE_ENTRIES_DISABLED"},
                ]
            }
        ),
        encoding="utf-8",
    )

    report = td.day_review_report(root=tmp_path, day="2026-07-22", user="live_bot")

    assert report["canonical_funnel"]["raw_scanner_events"] == 1
    assert report["canonical_funnel"]["raw_submitted_order_events"] == 1
    assert report["system_and_safety_state"]["runtime_exception_count"] == 1
    assert all("2026-07-23T04:" not in json.dumps(row) for row in report["system_and_safety_state"]["runtime_exceptions"])


def test_live_persistence_blocks_suspicious_ids(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    row = {"symbol": "AAPL", "order_id": "replay-1", "status": "n/a", "source": "replay"}
    allowed, violation = validate_live_persistence_record(row, user_id="live_bot", destination="trade_attribution.orders", record_type="order")
    assert not allowed
    assert violation and violation["reason"] == "LIVE_DATA_CONTAMINATION_BLOCKED"
    path = record_order_event(
        data_dir=tmp_path,
        user_id="live_bot",
        timestamp="2026-07-22T13:31:00+00:00",
        symbol="AAPL",
        action="buy",
        submitted=True,
        order_id="replay-1",
        status="n/a",
        source="replay",
    )
    assert path is None


def test_event_time_et_date_scoping_and_dst_boundaries() -> None:
    assert event_trading_date_et({"broker_event_timestamp": "2026-07-22T00:30:00+00:00"}) == "2026-07-21"
    assert event_trading_date_et({"broker_event_timestamp": "2026-07-22T23:30:00+00:00"}) == "2026-07-22"
    assert event_trading_date_et({"broker_event_timestamp": "2026-03-08T06:30:00+00:00"}) == "2026-03-08"
    assert event_trading_date_et({"broker_event_timestamp": "2026-11-01T05:30:00+00:00"}) == "2026-11-01"


def test_missing_feature_alignment_is_unavailable_not_fail() -> None:
    summary = td._entry_alignment_summary(
        [
            {"accepted": False, "reason": "missing vwap"},
            {"accepted": False, "reason": "stale feature timestamp"},
            {"accepted": False, "reason": "entry_alignment"},
            {"accepted": False, "reason": "no_decision"},
            {"accepted": False, "reason": "strategy_route_not_applicable"},
            {"accepted": False, "reason": "incomplete evaluation"},
        ]
    )
    assert summary["blocked_decisions"] == 6
    assert summary["missing_features"] == 1
    assert summary["stale_features"] == 1
    assert summary["true_alignment_failures"] == 1
    assert summary["no_decision"] == 1
    assert summary["strategy_route_not_applicable"] == 1
    assert summary["incomplete_evaluations"] == 1


def test_day_review_effective_strategy_state_shows_global_entries_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _artifact(tmp_path)
    cfg = {
        "trading_control": {
            "mode": "entries-disabled",
            "strategy_states": {"trend_long": "LIVE"},
        }
    }
    monkeypatch.setattr(td, "load_config", lambda *_args, **_kwargs: cfg)

    report = td.day_review_report(root=tmp_path, day="2026-07-22", user="live_bot")
    state = report["system_and_safety_state"]["effective_strategy_runtime_state"]["trend_long"]

    assert state["configured_state"] == "LIVE"
    assert state["effective_runtime_state"] == "ENTRIES_DISABLED"
    assert state["effective_entry_permission"] is False


def test_day_review_normalizes_live_strategy_state_under_global_shadow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _artifact(tmp_path)
    cfg = {
        "trading_control": {
            "mode": "shadow",
            "strategy_states": {"trend_long": "LIVE"},
        }
    }
    monkeypatch.setattr(td, "load_config", lambda *_args, **_kwargs: cfg)

    report = td.day_review_report(root=tmp_path, day="2026-07-22", user="live_bot")
    state = report["system_and_safety_state"]["effective_strategy_runtime_state"]["trend_long"]

    assert state["configured_state"] == "LIVE"
    assert state["effective_runtime_state"] == "SHADOW"
    assert state["effective_entry_permission"] is False
    assert state["hypothetical_entries_allowed"] is True
    assert state["broker_submission_allowed"] is False


def test_day_review_uses_runtime_progress_for_observed_mode(tmp_path: Path) -> None:
    _artifact(tmp_path)
    record_runtime_event(
        tmp_path / "data",
        user_id="live_bot",
        event="SERVICE_STARTUP",
        timestamp=datetime.fromisoformat("2026-07-22T10:00:00-04:00"),
        project_root=tmp_path,
        configured_mode="shadow",
        effective_mode="shadow",
        live_orders_allowed=False,
        paper_orders_allowed=False,
        broker_submission_allowed=False,
    )
    record_runtime_event(
        tmp_path / "data",
        user_id="live_bot",
        event="SCAN_CYCLE_COMPLETED",
        timestamp=datetime.fromisoformat("2026-07-22T10:01:00-04:00"),
        project_root=tmp_path,
        configured_mode="shadow",
        effective_mode="shadow",
        broker_submission_allowed=False,
    )

    report = td.day_review_report(root=tmp_path, day="2026-07-22", user="live_bot")

    state = report["system_and_safety_state"]
    assert state["observed_runtime_mode"] == "shadow"
    assert state["startup_validation_status"] == "available"
    assert state["session_activity"]["session_activity_status"] == "ACTIVE_VALIDATED"


def test_day_review_uses_signal_expectancy_forward_bar_diagnostics(tmp_path: Path) -> None:
    _artifact(tmp_path)
    report_dir = tmp_path / "data" / "research_metrics" / "2026-07-22"
    report_dir.mkdir(parents=True)
    (report_dir / "signal_expectancy_report.json").write_text(
        json.dumps(
            {
                "data_quality": {
                    "signals_analyzed": 3,
                    "signals_with_valid_forward_bars": 1,
                    "missing_bars": 2,
                    "lookup_success_rate": 0.3333,
                    "lookup_failure_breakdown": {"no_historical_source": 2},
                    "symbols_missing_bars": ["AAPL"],
                    "time_buckets_missing_bars": {"09:30-10:00": 2},
                    "source_selected": ["/tmp/SPY_2026-07-22_1Min.csv"],
                    "cache_hits": 1,
                    "cache_misses": 1,
                    "persistence_status": {"loaded": 1, "no_local_bar_file_for_symbol_day": 1},
                },
                "signals": [{"return_15m_pct": 1.25, "return_30m_pct": 2.0}],
            }
        ),
        encoding="utf-8",
    )

    report = td.day_review_report(root=tmp_path, day="2026-07-22", user="live_bot")

    signal_quality = report["signal_quality"]
    assert signal_quality["scope"] == "signal_expectancy_report"
    assert signal_quality["signals_analyzed"] == 3
    assert signal_quality["signals_with_valid_forward_bars"] == 1
    assert signal_quality["lookup_failure_breakdown"] == {"no_historical_source": 2}
    assert signal_quality["unavailable_reason"] == "OUTCOME_UNAVAILABLE_NO_HISTORICAL_SOURCE"
    assert signal_quality["average_15m_return"] == pytest.approx(1.25)
    assert report["hypothetical_performance"]["status"] == "NO_MODE_BLOCKED_ENTRIES"


def test_day_review_does_not_report_missing_bars_after_forward_outcomes_load(tmp_path: Path) -> None:
    _artifact(tmp_path)
    report_dir = tmp_path / "data" / "research_metrics" / "2026-07-22"
    report_dir.mkdir(parents=True)
    (report_dir / "signal_expectancy_report.json").write_text(
        json.dumps(
            {
                "data_quality": {
                    "signals_analyzed": 2,
                    "signals_with_valid_forward_bars": 2,
                    "missing_bars": 0,
                    "lookup_success_rate": 1.0,
                    "lookup_failure_breakdown": {},
                    "source_selected": ["/tmp/SPY_2026-07-22_1Min.csv"],
                    "cache_hits": 2,
                    "cache_misses": 0,
                    "persistence_status": {"loaded": 1},
                },
                "signals": [{"return_15m_pct": 1.25, "return_30m_pct": 2.0}],
            }
        ),
        encoding="utf-8",
    )

    report = td.day_review_report(root=tmp_path, day="2026-07-22", user="live_bot")

    assert report["signal_quality"]["signals_with_valid_forward_bars"] == 2
    assert report["signal_quality"]["missing_bars"] == 0
    assert report["hypothetical_performance"]["status"] == "NO_MODE_BLOCKED_ENTRIES"
    assert report["rejected_signal_control_group"]["status"] == "FORWARD_OUTCOMES_AVAILABLE"
    assert "without forward bars" not in report["rejected_signal_control_group"]["conclusion"]
    assert "Capture or load local forward bars" not in report["single_priority_for_next_session"]


def test_day_review_classifies_signal_report_permission_failure_without_zeroing_forward_bars(tmp_path: Path) -> None:
    _artifact(tmp_path)
    report_dir = tmp_path / "data" / "research_metrics" / "2026-07-22"
    report_dir.mkdir(parents=True)
    report_dir.chmod(0o555)
    try:
        report = td.day_review_report(root=tmp_path, day="2026-07-22", user="live_bot")
    finally:
        report_dir.chmod(0o755)

    signal_quality = report["signal_quality"]
    assert signal_quality["unavailable_reason"] == "OUTCOME_UNAVAILABLE_ARTIFACT_WRITE_PERMISSION_ERROR"
    assert signal_quality["lookup_failure_breakdown"] == {"artifact_write_permission_error": 1}
    assert signal_quality["missing_bars"] == 0
    assert "artifact_error" in signal_quality
    assert report["final_readiness_gate"]["forward_outcomes_available"] is False


def test_adaptive_relaxation_does_not_auto_apply_in_live_production() -> None:
    cfg = {
        "trading_control": {"adaptive_relaxation": {"production_auto_apply": False}},
        "adaptive_sensitivity": {"enabled": True, "minimum_observations": 1, "target_entries_per_day": {"minimum": 2}},
    }
    state = resolve_adaptive_sensitivity(
        cfg,
        metrics={"observations": 10, "trades_per_day": 0.0, "max_drawdown_pct": 0.0, "loss_rate": 0.0},
        context={"environment": "live", "production": True, "market_regime": "normal"},
        base_min_rvol=1.8,
    )
    assert state.mode == "normal"
    assert state.reason == "low_trade_frequency_informational_only"


def test_decision_identity_distinguishes_repeated_cycles() -> None:
    left = canonical_decision_key({"timestamp": "2026-07-22T13:30:00+00:00", "symbol": "AAPL", "route": "trend_long", "cycle_id": "1"}, user_id="live_bot")
    right = canonical_decision_key({"timestamp": "2026-07-22T13:31:00+00:00", "symbol": "AAPL", "route": "trend_long", "cycle_id": "2"}, user_id="live_bot")
    assert left != right
