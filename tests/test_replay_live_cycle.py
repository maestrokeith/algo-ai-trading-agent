"""Tests for offline live-cycle replay tooling."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import replay_live_cycle as replay
from scripts.generate_paper_session_conversion_report import (
    _no_trade_explanation,
    build_paper_session_conversion_report,
)


def _write_replay_project(
    root: Path,
    *,
    user: str = "live_bot",
    accepted_relative_volume: float = 4.0,
    target_core_stock_pct: int = 0,
    accepted_candidates: list[dict] | None = None,
) -> None:
    (root / "config").mkdir(parents=True)
    (root / "data" / "dynamic_scan_history").mkdir(parents=True)
    (root / "data" / "premarket").mkdir(parents=True)
    (root / "data" / "reports").mkdir(parents=True)
    (root / "config" / "default.yaml").write_text(
        """
options:
  enabled: false
execution:
  max_spread_pct: 5.0
  min_trade_dollars: 500
  allow_fractional: true
  prefer_limit_orders: false
portfolio:
  target_core_stock_pct: TARGET_CORE_STOCK_PCT
  target_dynamic_pct: 25
  target_cash_pct: 10
  capital_allocator:
    enabled: true
    max_positions: 5
    symbol_cap: 0.25
    min_trade_size: 500
    min_realloc_leg: 300
    rotate_trim_fraction: 0.3
    require_net_sell_gte_buy: false
    fallback_on_empty_alloc: false
    force_minimum_trade_single_candidate: false
    min_gross_deployment_pct: 0
dynamic_universe:
  min_relative_volume: 1.0
universe:
  symbols: [AAPL, MSFT, XOS]
""".replace("TARGET_CORE_STOCK_PCT", str(int(target_core_stock_pct))),
        encoding="utf-8",
    )
    accepted_rows = accepted_candidates
    if accepted_rows is None:
        accepted_rows = [
            {
                "accepted": True,
                "symbol": "XOS",
                "score": 95.0,
                "price": 10.0,
                "spread_pct": 0.2,
                "relative_volume": accepted_relative_volume,
                "news_score": 8.0,
                "event_score": 8.0,
                "catalyst_score": 0.9,
                "quality": {"price_above_vwap": True},
            }
        ]
    scan = {
        "accepted": accepted_rows,
        "candidates": list(accepted_rows)
        + [
            {
                "accepted": False,
                "symbol": "WEAK",
                "score": 0.0,
                "price": 4.0,
                "spread_pct": 1.0,
                "relative_volume": 0.2,
                "rejection_reason": "below_min_relative_volume",
            },
        ],
    }
    (root / "data" / "dynamic_scan_history" / f"20260605T120000000000Z_{user}.json").write_text(
        json.dumps(scan),
        encoding="utf-8",
    )
    (root / "data" / "premarket" / "latest_rankings.json").write_text(json.dumps({"rankings": []}), encoding="utf-8")
    (root / "data" / "premarket" / "latest_catalysts.json").write_text(json.dumps({"catalysts": []}), encoding="utf-8")
    (root / "data" / "premarket" / "latest_event_feed.json").write_text(json.dumps({"events": []}), encoding="utf-8")


def _write_replay_premarket_artifacts(root: Path, *, generated_at: datetime) -> None:
    payload_common = {
        "generated_at": generated_at.isoformat(),
        "ttl_minutes": 390,
        "source": "test",
        "symbols": ["AMZN", "GOOGL"],
    }
    (root / "data" / "premarket" / "latest_rankings.json").write_text(
        json.dumps(
            {
                **payload_common,
                "rankings": [
                    {
                        "rank": 1,
                        "symbol": "AMZN",
                        "score": 8.4,
                        "news_score": 8,
                        "event_score": 7.5,
                        "catalyst_score": 0.84,
                        "headline": "Amazon raises guidance",
                        "catalyst_type": "earnings",
                        "source": "alpaca",
                    },
                    {
                        "rank": 2,
                        "symbol": "GOOGL",
                        "score": 7.8,
                        "news_score": 7,
                        "event_score": 6.5,
                        "catalyst_score": 0.78,
                        "headline": "Google wins cloud contract",
                        "catalyst_type": "deal",
                        "source": "sec",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "data" / "premarket" / "latest_catalysts.json").write_text(
        json.dumps(
            {
                **payload_common,
                "catalysts": [
                    {"symbol": "AMZN", "score": 8, "headline": "Amazon raises guidance", "source": "alpaca", "catalyst_type": "earnings"},
                    {"symbol": "GOOGL", "score": 7, "headline": "Google wins cloud contract", "source": "sec", "catalyst_type": "deal"},
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "data" / "premarket" / "latest_event_feed.json").write_text(
        json.dumps(
            {
                **payload_common,
                "events": [
                    {"symbol": "AMZN", "score": 7.5, "headline": "Amazon raises guidance", "source": "alpaca"},
                    {"symbol": "GOOGL", "score": 6.5, "headline": "Google wins cloud contract", "source": "sec"},
                ],
            }
        ),
        encoding="utf-8",
    )


def _write_paper_bot_replay_override(root: Path) -> None:
    (root / "config" / "users.yaml").write_text(
        """
users:
  - id: paper_bot
    alpaca_key_env: APCA_API_KEY_ID
    alpaca_secret_env: APCA_API_SECRET_KEY
    paper: true
    overrides:
      portfolio:
        dynamic_quality:
          allow_event_news_fallback: true
          min_event_score: 1.0
          min_news_score: 1.0
""",
        encoding="utf-8",
    )


def test_replay_never_submits_real_broker_order_and_records_mock_order(tmp_path: Path) -> None:
    _write_replay_project(tmp_path)

    summary = replay.run_replay(project_root=tmp_path, date="latest", user="live_bot", broker_mock=True)

    assert summary["broker_mock"] is True
    assert summary["selected_candidates"][0]["symbol"] == "XOS"
    assert summary["rejected_before_allocator"][0]["symbol"] == "WEAK"
    assert len(summary["simulated_submitted_orders"]) == 1
    assert summary["simulated_submitted_orders"][0]["symbol"] == "XOS"
    assert summary["allocator_actions_created"][0]["symbol"] == "XOS"
    assert summary["per_symbol_trace"][0]["entry_eval"]["result"] is True
    assert summary["per_symbol_trace"][0]["allocator_action"]["result"] is True
    assert summary["per_symbol_trace"][0]["trade_cycle_allowed"]["result"] is True
    assert summary["per_symbol_trace"][0]["order_build"]["result"] is True
    assert summary["per_symbol_trace"][0]["simulated_submit"]["result"] is True
    assert summary["trade_attribution_path"] is not None
    attribution_path = tmp_path / summary["trade_attribution_path"]
    assert attribution_path.exists()
    attribution = json.loads(attribution_path.read_text(encoding="utf-8"))
    assert attribution["summary"]["trades_entered"] == 1


def test_replay_injects_fresh_premarket_candidates_into_allocator_rows(tmp_path: Path) -> None:
    now = datetime(2026, 6, 9, 14, 0, tzinfo=timezone.utc)
    _write_replay_project(
        tmp_path,
        accepted_candidates=[
            {
                "accepted": True,
                "symbol": "TNGX",
                "score": 95.0,
                "price": 10.0,
                "spread_pct": 0.2,
                "relative_volume": 25.0,
                "news_score": 0.0,
                "event_score": 0.0,
                "catalyst_score": 0.0,
                "quality": {"price_above_vwap": True},
            }
        ],
    )
    _write_replay_premarket_artifacts(tmp_path, generated_at=now)

    summary = replay.run_replay(
        project_root=tmp_path,
        date="latest",
        user="live_bot",
        broker_mock=True,
        now_override=now,
    )

    selected_symbols = {row["symbol"] for row in summary["selected_candidates"]}
    assert {"AMZN", "GOOGL"}.issubset(selected_symbols)
    assert any("PREMARKET_CANDIDATE_INJECTED symbol=AMZN rank=1" in line for line in summary["log_lines"])
    assert any(
        "ALLOCATOR_CANDIDATE_ROW stage=before_filter symbol=AMZN is_dynamic=true" in line
        and "catalyst_score=0.84" in line
        and "event_score=8.0" in line
        for line in summary["log_lines"]
    )
    assert any("DYNAMIC_REJECT symbol=TNGX reason=no_catalyst" in line for line in summary["log_lines"])


def test_paper_replay_user_override_allows_event_news_backed_dynamic_candidate(tmp_path: Path) -> None:
    now = datetime(2026, 6, 9, 14, 0, tzinfo=timezone.utc)
    _write_replay_project(
        tmp_path,
        user="paper_bot",
        accepted_candidates=[
            {
                "accepted": True,
                "symbol": "TNGX",
                "score": 95.0,
                "price": 10.0,
                "spread_pct": 0.2,
                "relative_volume": 5.1,
                "news_score": 1.0,
                "event_score": 1.0,
                "catalyst_score": 0.1,
                "quality": {"price_above_vwap": True},
            }
        ],
    )
    _write_paper_bot_replay_override(tmp_path)

    summary = replay.run_replay(
        project_root=tmp_path,
        date="latest",
        user="paper_bot",
        broker_mock=True,
        now_override=now,
    )

    assert not any("DYNAMIC_REJECT symbol=TNGX reason=no_catalyst" in line for line in summary["log_lines"])
    assert any(
        "DYNAMIC_QUALITY_PASS symbol=TNGX" in line and "catalyst_path=event_score" in line
        for line in summary["log_lines"]
    )
    assert any(
        "ALLOCATOR_CANDIDATE_ROW stage=before_filter symbol=TNGX is_dynamic=true" in line
        for line in summary["log_lines"]
    )
    assert any(
        "ENTRY_PIPELINE_STAGE symbol=TNGX stage=replay_live_cycle result=skipped "
        "reason=offline_allocator_replay_does_not_run_live_entry_eval" in line
        for line in summary["log_lines"]
    )
    assert any(
        "OPTION_PIPELINE_STAGE symbol=TNGX stage=replay_live_cycle result=skipped "
        "reason=options_disabled_by_replay_live_cycle" in line
        for line in summary["log_lines"]
    )
    assert any(
        "TRADE_CYCLE_GATE symbol=TNGX replay_mode=offline_replay broker_mock=True "
        in line
        for line in summary["log_lines"]
    )


def test_replay_live_cycle_logs_options_disabled_guard(tmp_path: Path) -> None:
    now = datetime(2026, 6, 9, 14, 0, tzinfo=timezone.utc)
    _write_replay_project(tmp_path, user="paper_bot")
    _write_paper_bot_replay_override(tmp_path)

    summary = replay.run_replay(
        project_root=tmp_path,
        date="latest",
        user="paper_bot",
        broker_mock=True,
        now_override=now,
    )

    assert any(
        "OPTION_PIPELINE_STAGE symbol=XOS stage=replay_live_cycle result=skipped "
        "reason=options_disabled_by_replay_live_cycle" in line
        for line in summary["log_lines"]
    )


def test_paper_replay_can_use_live_history_user_with_paper_config(tmp_path: Path) -> None:
    now = datetime(2026, 6, 9, 14, 0, tzinfo=timezone.utc)
    _write_replay_project(
        tmp_path,
        user="live_bot",
        accepted_candidates=[
            {
                "accepted": True,
                "symbol": "TNGX",
                "score": 95.0,
                "price": 10.0,
                "spread_pct": 0.2,
                "relative_volume": 5.1,
                "news_score": 1.0,
                "event_score": 1.0,
                "catalyst_score": 0.1,
                "quality": {"price_above_vwap": True},
            }
        ],
    )
    _write_paper_bot_replay_override(tmp_path)

    summary = replay.run_replay(
        project_root=tmp_path,
        date="latest",
        user="paper_bot",
        history_user="live_bot",
        broker_mock=True,
        now_override=now,
    )

    assert summary["user"] == "paper_bot"
    assert summary["history_user"] == "live_bot"
    assert summary["history_path"].endswith("_live_bot.json")
    assert summary["summary_path"].endswith("_paper_bot.json")
    assert not any("DYNAMIC_REJECT symbol=TNGX reason=no_catalyst" in line for line in summary["log_lines"])
    assert any(
        "DYNAMIC_QUALITY_PASS symbol=TNGX" in line and "catalyst_path=event_score" in line
        for line in summary["log_lines"]
    )


def test_replay_emits_summary_json(tmp_path: Path) -> None:
    _write_replay_project(tmp_path)

    summary = replay.run_replay(project_root=tmp_path, date="latest", user="live_bot", broker_mock=True)

    assert summary["history_user"] == "live_bot"
    path = tmp_path / summary["summary_path"]
    assert path.exists()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["ok"] is True
    assert saved["simulated_submitted_orders"][0]["symbol"] == "XOS"
    assert saved["trade_attribution_summary"]["trades_entered"] == 1
    assert saved["per_symbol_trace"][0]["symbol"] == "XOS"


def test_replay_explains_selected_candidate_with_zero_simulated_orders(tmp_path: Path) -> None:
    _write_replay_project(
        tmp_path,
        accepted_candidates=[
            {
                "accepted": True,
                "symbol": "XOS",
                "score": 95.0,
                "price": 10.0,
                "spread_pct": 0.2,
                "relative_volume": 0.2,
                "news_score": 0.0,
                "event_score": 0.0,
                "catalyst_score": 0.3,
                "source": "replay_fixture",
                "route": "dynamic_replay",
                "quality": {"price_above_vwap": True},
            }
        ],
    )

    summary = replay.run_replay(project_root=tmp_path, date="latest", user="live_bot", broker_mock=True)

    assert summary["selected_candidates"][0]["symbol"] == "XOS"
    assert summary["simulated_submitted_orders"] == []
    assert summary["rejected_by_allocator"][0]["symbol"] == "XOS"
    assert summary["rejected_by_allocator"][0]["reason"] == "dynamic_relative_volume"
    trace = summary["per_symbol_trace"][0]
    assert trace["symbol"] == "XOS"
    assert trace["scan_selected"] is True
    assert trace["entry_eval"] == {"result": True, "reason": "ok"}
    assert trace["allocator_candidate"] == {"result": True, "reason": "dynamic_relative_volume"}
    assert trace["allocator_action"] == {
        "result": True,
        "reason": "created",
    }
    assert trace["trade_cycle_allowed"]["result"] is True
    assert trace["order_build"] == {
        "result": False,
        "reason": "dynamic_relative_volume",
    }
    assert trace["simulated_submit"] == {
        "result": False,
        "reason": "dynamic_relative_volume",
    }


def test_underweight_replay_adds_core_rebuild_candidates_and_simulates_order(tmp_path: Path) -> None:
    _write_replay_project(
        tmp_path,
        target_core_stock_pct=65,
        accepted_candidates=[],
    )

    summary = replay.run_replay(project_root=tmp_path, date="latest", user="live_bot", broker_mock=True)

    core_symbols = {row["symbol"] for row in summary["core_rebuild_candidates"]}
    assert {"AAPL", "MSFT"}.issubset(core_symbols)
    submitted_symbols = {row["symbol"] for row in summary["simulated_submitted_orders"]}
    assert submitted_symbols & {"AAPL", "MSFT"}
    core_trace = next(row for row in summary["per_symbol_trace"] if row["symbol"] in {"AAPL", "MSFT"})
    assert core_trace["scan_selected"] is False
    assert core_trace["core_rebuild_candidate"] == {
        "result": True,
        "reason": "allocation_underweight",
    }
    assert core_trace["allocator_action"]["result"] is True
    assert core_trace["trade_cycle_allowed"]["result"] is True
    assert core_trace["order_build"]["result"] is True
    assert core_trace["simulated_submit"]["result"] is True


def test_paper_session_conversion_report_traces_scan_to_simulated_submit(tmp_path: Path) -> None:
    _write_replay_project(tmp_path, user="paper_bot")

    report = build_paper_session_conversion_report(
        project_root=tmp_path,
        date="latest",
        user="paper_bot",
        summary_dir=tmp_path / "data" / "paper_conversion_replay",
    )

    assert report["summary"]["selected_candidates"] == 1
    assert report["summary"]["simulated_submitted_orders"] == 1
    row = report["candidates"][0]
    assert row["symbol"] == "XOS"
    assert row["dynamic_scan"]["result"] is True
    assert row["selected"]["result"] is True
    assert row["allocator_input"]["result"] is True
    assert row["allocator_action"]["result"] is True
    assert row["trade_cycle_allowed"]["result"] is True
    assert row["simulated_order_submitted"]["result"] is True
    assert report["config"]["capital_allocator.allow_no_trade_cycles"] is False


def test_no_trade_cycle_allowed_explanation_names_exact_config_gate() -> None:
    explanation = _no_trade_explanation(
        "no_trade_cycle_allowed",
        {"capital_allocator.allow_no_trade_cycles": True},
    )

    assert explanation == {
        "gate": "capital_allocator.allow_no_trade_cycles",
        "config_value": True,
        "explanation": (
            "Allocator had selected candidates but no action survived sizing/planner filters; "
            "the configured paper/replay idle branch allowed a no-trade cycle instead of forcing an order."
        ),
    }


def test_replay_skips_same_day_sold_core_symbol_but_rebuilds_other_core(tmp_path: Path) -> None:
    from src.trade_attribution import record_exit

    _write_replay_project(
        tmp_path,
        target_core_stock_pct=65,
        accepted_candidates=[],
    )
    record_exit(
        data_dir=tmp_path / "data",
        user_id="live_bot",
        timestamp=datetime(2026, 6, 6, 14, 0, tzinfo=timezone.utc),
        symbol="AAPL",
        exit_reason="take_profit",
        hold_minutes=90,
        entry_route="trend_long",
    )

    summary = replay.run_replay(project_root=tmp_path, date="latest", user="live_bot", broker_mock=True)

    core_symbols = {row["symbol"] for row in summary["core_rebuild_candidates"]}
    assert "AAPL" not in core_symbols
    assert "MSFT" in core_symbols
    assert any(
        "CORE_REBUILD_SKIP symbol=AAPL reason=sold_today" in line
        for line in summary["log_lines"]
    )
    submitted_symbols = {row["symbol"] for row in summary["simulated_submitted_orders"]}
    assert "MSFT" in submitted_symbols


def test_severe_bearish_replay_blocks_core_rebuild(tmp_path: Path) -> None:
    _write_replay_project(
        tmp_path,
        target_core_stock_pct=65,
        accepted_candidates=[],
    )

    summary = replay.run_replay(
        project_root=tmp_path,
        date="latest",
        user="live_bot",
        broker_mock=True,
        regime_score=1,
        regime_condition="bearish",
    )

    assert summary["core_rebuild_candidates"] == []
    assert summary["simulated_submitted_orders"] == []
    assert any(
        "CORE_REBUILD_SKIP symbol=AAPL reason=bearish_regime" in line
        for line in summary["log_lines"]
    )


def test_replay_blocks_live_broker_without_mock(tmp_path: Path) -> None:
    _write_replay_project(tmp_path)

    with pytest.raises(RuntimeError, match="unsafe_live_broker_mode"):
        replay.run_replay(
            project_root=tmp_path,
            date="latest",
            user="live_bot",
            broker_mock=False,
            broker_mode="LIVE",
        )


def test_pr_safety_script_blocks_unsafe_live_broker_mode() -> None:
    env = dict(os.environ)
    env["BROKER_MODE"] = "LIVE"
    env["PR_SAFETY_BROKER_MOCK"] = "0"
    result = subprocess.run(
        ["bash", "scripts/pr_safety_check.sh", "--validate-broker-mode-only"],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "PR_SAFETY_BLOCKED" in result.stderr
