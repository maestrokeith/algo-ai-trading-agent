from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts.shadow_readiness import build_shadow_readiness, main
from src.runtime_progress import record_runtime_event


ET = ZoneInfo("America/New_York")


def _write_project_fixture(root: Path, *, mode: str = "shadow") -> None:
    (root / "config").mkdir(parents=True)
    (root / "data" / "research" / "bars_status").mkdir(parents=True)
    (root / "data" / "research" / "bars_consistency").mkdir(parents=True)
    (root / "data" / "research_metrics" / "2026-07-24").mkdir(parents=True)
    (root / "reports" / "day_review").mkdir(parents=True)
    (root / "data" / "trade_attribution" / "daily").mkdir(parents=True)
    (root / "config" / "default.yaml").write_text(
        f"""
broker:
  paper: false
trading_control:
  mode: {mode}
options:
  live_pilot_enabled: false
""".lstrip(),
        encoding="utf-8",
    )
    (root / "config" / "users.yaml").write_text(
        """
users:
  - id: live_bot
    alpaca_key_env: TEST_ALPACA_KEY
    alpaca_secret_env: TEST_ALPACA_SECRET
    paper: false
""".lstrip(),
        encoding="utf-8",
    )
    (root / "data" / "trade_attribution" / "daily" / "2026-07-24_live_bot.json").write_text(
        json.dumps(
            {
                "date": "2026-07-24",
                "user_id": "live_bot",
                "candidates": [
                    {
                        "timestamp": "2026-07-24T13:30:00+00:00",
                        "symbol": "SPY",
                        "accepted": True,
                        "environment": "shadow",
                        "hypothetical": True,
                    }
                ],
                "allocator_candidates": [
                    {
                        "timestamp": "2026-07-24T13:31:00+00:00",
                        "symbol": "SPY",
                        "action_created": True,
                        "environment": "shadow",
                        "hypothetical": True,
                    }
                ],
                "orders": [
                    {
                        "timestamp": "2026-07-24T13:32:00+00:00",
                        "symbol": "SPY",
                        "action": "buy",
                        "submitted": True,
                        "status": "shadow",
                        "order_id": "shadow-test",
                        "environment": "shadow",
                        "hypothetical": True,
                        "broker_dispatch_attempted": False,
                        "execution_allowed": False,
                    }
                ],
                "exits": [],
            }
        ),
        encoding="utf-8",
    )


def _write_runtime_progress(root: Path, *, day: str = "2026-07-24") -> None:
    ts = datetime.fromisoformat(f"{day}T10:00:00-04:00").replace(tzinfo=ET)
    for event in ("SERVICE_STARTUP", "ACCOUNT_FETCH_SUCCESS", "SCAN_CYCLE_COMPLETED", "ENTRY_CYCLE_COMPLETED"):
        record_runtime_event(
            root / "data",
            user_id="live_bot",
            event=event,
            timestamp=ts,
            project_root=root,
            configured_mode="shadow",
            effective_mode="shadow",
            live_orders_allowed=False,
            paper_orders_allowed=False,
            broker_submission_allowed=False,
        )


def test_shadow_readiness_ready_for_shadow_without_real_orders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_ALPACA_KEY", "key")
    monkeypatch.setenv("TEST_ALPACA_SECRET", "secret")
    _write_project_fixture(tmp_path, mode="shadow")
    _write_runtime_progress(tmp_path)

    report = build_shadow_readiness(user="live_bot", day="2026-07-24", project_root=tmp_path)

    assert report["ready"] is True
    assert report["effective_mode"] == "shadow"
    assert report["broker_execution_disabled"] is True
    assert report["live_broker_orders_allowed"] is False
    assert report["new_real_entries_allowed"] is False
    assert report["shadow_observation_enabled"] is True
    assert report["real_order_submissions"] == 0
    assert report["real_order_submission_attempts"] == 0
    assert report["real_broker_accepted_orders"] == 0
    assert report["real_fills"] == 0
    assert report["real_positions"] == 0
    assert report["fills"] == 0
    assert report["positions"] == 0
    assert report["replay_contamination"] == 0
    assert report["shadow_decisions"] == 1
    assert report["shadow_allocator_actions"] == 1
    assert report["shadow_order_intents"] == 1
    assert report["shadow_execution_blocks"] == 1
    assert report["requested_date"] == "2026-07-24"
    assert report["artifact_date_matches_requested"] is True
    assert report["session_activity_status"] == "ACTIVE_VALIDATED"


def test_shadow_readiness_blocks_real_submissions_in_shadow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_ALPACA_KEY", "key")
    monkeypatch.setenv("TEST_ALPACA_SECRET", "secret")
    _write_project_fixture(tmp_path, mode="shadow")
    _write_runtime_progress(tmp_path)
    path = tmp_path / "data" / "trade_attribution" / "daily" / "2026-07-24_live_bot.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["orders"] = [
        {
            "timestamp": "2026-07-24T13:32:00+00:00",
            "symbol": "SPY",
            "action": "buy",
            "submitted": True,
            "status": "accepted",
            "order_id": "real-accepted",
        }
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")

    report = build_shadow_readiness(user="live_bot", day="2026-07-24", project_root=tmp_path)

    assert report["ready"] is False
    assert report["real_order_submissions"] == 1
    assert report["real_broker_accepted_orders"] == 1
    assert "real_order_submission_attempts_present:1" in report["blocking_reasons"]
    assert "real_broker_accepted_orders_present:1" in report["blocking_reasons"]


def test_shadow_readiness_blocks_when_mode_is_not_shadow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_ALPACA_KEY", "key")
    monkeypatch.setenv("TEST_ALPACA_SECRET", "secret")
    _write_project_fixture(tmp_path, mode="entries-disabled")
    _write_runtime_progress(tmp_path)

    report = build_shadow_readiness(user="live_bot", day="2026-07-24", project_root=tmp_path)

    assert report["ready"] is False
    assert "mode_not_shadow:entries-disabled" in report["blocking_reasons"]


def test_shadow_readiness_cli_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("TEST_ALPACA_KEY", "key")
    monkeypatch.setenv("TEST_ALPACA_SECRET", "secret")
    _write_project_fixture(tmp_path, mode="shadow")
    _write_runtime_progress(tmp_path)

    code = main(["--user", "live_bot", "--date", "2026-07-24", "--project-root", str(tmp_path), "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True


def test_shadow_readiness_without_date_uses_current_day_not_prior_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_ALPACA_KEY", "key")
    monkeypatch.setenv("TEST_ALPACA_SECRET", "secret")
    _write_project_fixture(tmp_path, mode="shadow")
    _write_runtime_progress(tmp_path, day="2026-07-29")
    prior = tmp_path / "data" / "trade_attribution" / "daily" / "2026-07-29_live_bot.json"
    prior.write_text((tmp_path / "data" / "trade_attribution" / "daily" / "2026-07-24_live_bot.json").read_text(encoding="utf-8"), encoding="utf-8")

    report = build_shadow_readiness(
        user="live_bot",
        project_root=tmp_path,
        now=datetime.fromisoformat("2026-07-30T10:00:00-04:00"),
    )

    assert report["date"] == "2026-07-30"
    assert report["requested_date"] == "2026-07-30"
    assert report["artifact_date"] is None
    assert report["artifact_date_matches_requested"] is False
    assert report["current_day_artifacts_missing"] is True
    assert report["prior_day_context_available"] is True
    assert report["prior_day_context_date"] == "2026-07-29"
    assert report["shadow_order_intents"] == 0
    assert report["ready"] is False
    assert "current_day_artifacts_missing" in report["blocking_reasons"]
