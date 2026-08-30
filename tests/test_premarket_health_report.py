"""Tests for daily pre-market health reports."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.premarket_health_report import (
    build_premarket_health_report,
    check_account_status,
    render_premarket_health_text,
    save_premarket_health_report,
)


NOW = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)


class StubBroker:
    def get_account_snapshot(self) -> dict[str, float | str | bool]:
        return {
            "status": "ACTIVE",
            "equity": 100_000.0,
            "cash": 20_000.0,
            "buying_power": 40_000.0,
            "trading_blocked": False,
        }

    def get_open_orders(self) -> list[dict[str, object]]:
        return [{"symbol": "AAPL", "side": "buy", "qty": 2}]

    def get_positions(self) -> list[dict[str, object]]:
        return [{"symbol": "AAPL", "market_value": 10_000.0, "side": "long"}]


def _write_artifacts(root: Path, *, generated_at: datetime = NOW) -> None:
    artifact_dir = root / "data" / "premarket"
    artifact_dir.mkdir(parents=True)
    ts = generated_at.isoformat()
    (artifact_dir / "latest_event_feed.json").write_text(
        json.dumps({"generated_at": ts, "events": [{"symbol": "AAPL", "headline": "beat"}]}),
        encoding="utf-8",
    )
    (artifact_dir / "latest_rankings.json").write_text(
        json.dumps({"generated_at": ts, "rankings": [{"symbol": "AAPL", "score": 8.0}]}),
        encoding="utf-8",
    )
    (artifact_dir / "latest_catalysts.json").write_text(
        json.dumps({"generated_at": ts, "catalysts": [{"symbol": "AAPL", "score": 8.0}]}),
        encoding="utf-8",
    )


def test_build_premarket_health_report_ready(tmp_path: Path) -> None:
    _write_artifacts(tmp_path)
    report = build_premarket_health_report(
        broker=StubBroker(),
        config={"premarket_intelligence": {"enabled": True}, "dynamic_universe": {"enabled": True}},
        project_root=tmp_path,
        now=NOW,
    )

    assert report.ok is True
    by_name = {section.name: section for section in report.sections}
    assert by_name["account"].reason == "ready"
    assert by_name["news"].reason == "ready"
    assert by_name["dynamic_scan"].details["rankings"] == 1
    assert by_name["open_orders"].details["count"] == 1
    assert by_name["exposure"].details["gross"] == pytest.approx(10.0)


def test_premarket_health_report_marks_missing_artifacts_not_ready(tmp_path: Path) -> None:
    report = build_premarket_health_report(
        broker=StubBroker(),
        config={"premarket_intelligence": {"enabled": True}, "dynamic_universe": {"enabled": True}},
        project_root=tmp_path,
        now=NOW,
    )

    assert report.ok is False
    reasons = {section.name: section.reason for section in report.sections}
    assert reasons["news"] == "artifacts_missing"
    assert reasons["dynamic_scan"] == "rankings_missing"


def test_premarket_health_report_marks_stale_rankings_not_ready(tmp_path: Path) -> None:
    _write_artifacts(tmp_path, generated_at=datetime(2026, 6, 5, 1, 0, tzinfo=timezone.utc))
    report = build_premarket_health_report(
        broker=StubBroker(),
        config={"premarket_intelligence": {"enabled": True}, "dynamic_universe": {"enabled": True}},
        project_root=tmp_path,
        now=NOW,
        max_artifact_age_hours=2.0,
    )

    assert report.ok is False
    assert {section.name: section.reason for section in report.sections}["dynamic_scan"] == "rankings_stale"


def test_check_account_status_detects_blocked_account() -> None:
    broker = SimpleNamespace(
        get_account_snapshot=lambda: {
            "equity": 100.0,
            "cash": 10.0,
            "buying_power": 10.0,
            "trading_blocked": True,
        }
    )

    result = check_account_status(broker)
    assert result.ok is False
    assert result.reason == "trading_blocked"


def test_render_and_save_premarket_health_report(tmp_path: Path) -> None:
    _write_artifacts(tmp_path)
    report = build_premarket_health_report(
        broker=StubBroker(),
        config={"premarket_intelligence": {"enabled": True}, "dynamic_universe": {"enabled": True}},
        project_root=tmp_path,
        now=NOW,
    )
    text = render_premarket_health_text(report, user_label="trader1")
    assert "Pre-market" not in text
    assert "pre-market health: READY" in text
    assert "dynamic_scan" in text

    out = save_premarket_health_report(report, tmp_path / "report.html", user_label="trader1")
    html = out.read_text(encoding="utf-8")
    assert "Pre-market health: READY" in html
    assert "trader1" in html
