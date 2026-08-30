"""Tests for the live preflight safety validator."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

import pytest

from scripts import preflight_live_safety as preflight
from src.dynamic_universe import DynamicScanBatchResult, DynamicScanCandidate


def _write_rankings_artifact(project_root: Path, generated_at: datetime) -> None:
    artifact_dir = project_root / "data" / "premarket"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    rankings_payload = {
        "generated_at": generated_at.isoformat(),
        "source": "test",
        "symbols": ["XOS"],
        "rankings": [
            {
                "symbol": "XOS",
                "score": 9.0,
                "source": "test",
                "catalyst_type": "news",
                "reason": "ranked test catalyst",
            }
        ],
    }
    catalysts_payload = {
        "generated_at": generated_at.isoformat(),
        "source": "test",
        "symbols": ["XOS"],
        "catalysts": [
            {
                "symbol": "XOS",
                "score": 9.0,
                "headline": "test catalyst",
                "source": "test",
                "catalyst_type": "news",
                "event_score": 9.0,
            }
        ],
    }
    events_payload = {
        "generated_at": generated_at.isoformat(),
        "source": "test",
        "symbols": ["XOS"],
        "events": [
            {
                "symbol": "XOS",
                "score": 9.0,
                "headline": "test event",
                "source": "test",
                "catalyst_type": "news",
            }
        ],
    }
    (artifact_dir / "latest_rankings.json").write_text(json.dumps(rankings_payload))
    (artifact_dir / "latest_catalysts.json").write_text(json.dumps(catalysts_payload))
    (artifact_dir / "latest_event_feed.json").write_text(json.dumps(events_payload))


class _FakeBroker:
    def __init__(self) -> None:
        self.submit_called = False

    def get_account_snapshot(self) -> dict[str, float]:
        return {"equity": 28_800.0, "buying_power": 10_000.0, "cash": 10_000.0}

    def get_positions(self) -> list[dict[str, Any]]:
        return []

    def get_open_orders(self) -> list[dict[str, Any]]:
        return []

    def submit_order(self, *_args: Any, **_kwargs: Any) -> None:
        self.submit_called = True


def test_missing_news_artifact_fails(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="news_artifacts_missing"):
        preflight.load_and_validate_news_artifacts(
            tmp_path,
            now=datetime(2026, 6, 4, 13, 0, tzinfo=timezone.utc),
        )


def test_stale_news_artifact_fails(tmp_path: Path) -> None:
    now = datetime(2026, 6, 4, 13, 0, tzinfo=timezone.utc)
    _write_rankings_artifact(tmp_path, now - timedelta(hours=7))

    with pytest.raises(RuntimeError, match="news_artifacts_stale"):
        preflight.load_and_validate_news_artifacts(tmp_path, now=now)


def test_allocator_plan_does_not_submit_orders(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    now = datetime(2026, 6, 4, 13, 0, tzinfo=timezone.utc)
    _write_rankings_artifact(tmp_path, now - timedelta(minutes=30))
    broker = _FakeBroker()

    def _scan_candidates_batch(*_args: Any, **_kwargs: Any) -> DynamicScanBatchResult:
        candidate = DynamicScanCandidate(
            symbol="XOS",
            score=300.0,
            accepted=True,
            rejection_reason=None,
            price=6.23,
            day_gain_pct=18.0,
            volume=2_000_000,
            avg_volume=500_000,
            relative_volume=4.0,
            spread_pct=0.4,
            quality=None,
            news_score=8,
            event_score=9.0,
            catalyst_score=0.9,
            catalyst_headline="test catalyst",
            catalyst_age_minutes=30.0,
        )
        return DynamicScanBatchResult(selected=["XOS"], accepted=[candidate], rejected=[], elapsed_ms=1)

    monkeypatch.setattr(preflight, "scan_candidates_batch", _scan_candidates_batch)
    result = preflight.run_preflight(project_root=tmp_path, broker=broker, now=now)

    assert result.ok is True
    assert broker.submit_called is False


def test_broker_submit_call_raises_in_preflight() -> None:
    broker = _FakeBroker()
    preflight.install_submit_guards(broker)

    with pytest.raises(RuntimeError, match="PREFLIGHT blocked broker submit method submit_order"):
        broker.submit_order({"symbol": "XOS"})
