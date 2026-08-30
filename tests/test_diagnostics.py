from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import scripts.run_diagnostics as diag
from src.premarket_readiness import PremarketArtifactStatus, PremarketReadiness


NOW = datetime(2026, 6, 8, 13, 30, tzinfo=timezone.utc)


def _readiness(
    *,
    status: str = "fresh",
    provider_reason: str = "ok",
    provider_status: int | None = 200,
    ranked: int = 2,
) -> PremarketReadiness:
    artifacts = [
        PremarketArtifactStatus(
            kind="rankings",
            path=Path("latest_rankings.json"),
            status="fresh",
            present=True,
            age_minutes=3.0,
            ttl_minutes=390,
            rankings=ranked,
        ),
        PremarketArtifactStatus(
            kind="catalysts",
            path=Path("latest_catalysts.json"),
            status="fresh",
            present=True,
            age_minutes=3.0,
            ttl_minutes=390,
            catalysts=1 if ranked else 0,
        ),
        PremarketArtifactStatus(
            kind="event_feed",
            path=Path("latest_event_feed.json"),
            status="fresh",
            present=True,
            age_minutes=3.0,
            ttl_minutes=390,
            events=3 if ranked else 0,
        ),
    ]
    return PremarketReadiness(
        status=status,
        present=True,
        fresh=status in {"fresh", "fresh_empty"},
        missing=[],
        stale=[],
        artifacts=artifacts,
        catalyst_ranked_symbols=ranked,
        ranking_count=ranked,
        catalyst_count=1 if ranked else 0,
        event_count=3 if ranked else 0,
        max_age_minutes=3.0,
        provider_diagnostics={
            "alpaca": {
                "enabled": True,
                "request_sent": True,
                "http_status": provider_status,
                "raw_count": 8,
                "filtered_count": ranked,
                "rate_limited": False,
                "reason": provider_reason,
            },
            "newsapi": {
                "enabled": False,
                "request_sent": False,
                "http_status": None,
                "raw_count": 0,
                "filtered_count": 0,
                "rate_limited": False,
                "reason": "newsapi_disabled",
            },
        },
        provider_diagnostics_path=Path("provider_diagnostics_latest.json"),
        provider_diagnostics_present=True,
    )


def _services(status: str = "active") -> dict[str, dict[str, str]]:
    return {
        unit: {
            "status": status if unit == "algo.service" else "inactive",
            "sub_state": "running" if unit == "algo.service" else "dead",
            "last_start": "Mon 2026-06-08 09:30:00 EDT",
            "exit_code": "0",
            "result": "success",
        }
        for unit in diag.SERVICE_UNITS
    }


def _broker(available: bool = True) -> dict[str, object]:
    if not available:
        return {"available": False, "account": {}, "positions": [], "orders": [], "error": "RuntimeError: missing credentials"}
    return {
        "available": True,
        "account": {"equity": 100000.0, "cash": 25000.0},
        "buying_power": 50000.0,
        "positions": [
            {"symbol": "AAPL", "qty": 10, "market_value": 2000.0, "unrealized_pl": 120.5, "side": "long"}
        ],
        "orders": [
            {
                "submitted_at": "2026-06-08T13:15:00+00:00",
                "symbol": "AAPL",
                "side": "buy",
                "qty": "10",
                "status": "filled",
            }
        ],
        "error": None,
    }


def _files(status: str = "present") -> dict[str, dict[str, str]]:
    return {
        name: {"status": status, "age_minutes": "3.0", "path": f"/tmp/{name}"}
        for name in diag.PREMARKET_FILES
    }


def test_render_report_includes_required_sections_and_green_health(tmp_path: Path) -> None:
    report = diag.render_report(
        project_root=tmp_path,
        config={"broker": {"paper": False}},
        user="live_bot",
        now=NOW,
        system={
            "timestamp": NOW.isoformat(),
            "git_commit": "abc1234",
            "uptime": "up 1 day",
            "hostname": "node-1",
            "mode": "live",
            "active_user": "live_bot",
        },
        services=_services(),
        broker=_broker(),
        readiness=_readiness(),
        dynamic_scan={
            "candidates_scanned": 12,
            "accepted": 2,
            "rejected": 10,
            "rejection_summary": {"below_min_relative_volume": 7},
            "accepted_symbols": ["AAPL", "NVDA"],
            "recent_rejections": [{"symbol": "MSFT", "reason": "below_min_relative_volume"}],
        },
        scheduler={
            "last_entry_lane_run": "ENTRY_DECISION_SUMMARY options_attempted=0",
            "last_exit_lane_run": "EXIT_EVAL tp_hit=false",
            "next_expected_entry_window": "market hours",
            "next_expected_exit_window": "continuous",
        },
        log_events={
            "algo.service": ["INFO PREMARKET_STARTUP_ARTIFACTS status=fresh"],
            "algosphere-premarket.service": ["INFO PREMARKET_COLLECTION_RESULT ranked=2"],
        },
        files=_files(),
    )

    assert "ALGOSPHERE DIAGNOSTICS" in report
    for section in (
        "SYSTEM",
        "SERVICES",
        "ACCOUNT",
        "POSITIONS",
        "PREMARKET",
        "PROVIDERS",
        "DYNAMIC SCAN",
        "ENTRY SCHEDULER",
        "RECENT ORDERS",
        "RECENT REJECTIONS",
        "RECENT LOG EVENTS",
        "FILES",
        "HEALTH SUMMARY",
    ):
        assert section in report
    assert "- mode: live" in report
    assert "- AAPL: qty=10 market_value=2000.00 unrealized_pnl=120.50" in report
    assert "- alpaca: enabled=True request_sent=True http_status=200 raw_count=8 filtered_count=2 reason=ok" in report
    assert "HEALTH=GREEN" in report


def test_health_yellow_for_fresh_empty_and_provider_failure() -> None:
    health, reasons = diag.determine_health(
        services=_services(),
        broker=_broker(),
        readiness=_readiness(status="fresh_empty", provider_reason="rate_limited", provider_status=429, ranked=0),
        files=_files(),
    )

    assert health == "YELLOW"
    assert "premarket_fresh_empty" in reasons
    assert any(reason.startswith("provider_failures:") for reason in reasons)


def test_health_red_for_missing_artifacts_or_broker_unavailable() -> None:
    health, reasons = diag.determine_health(
        services=_services(),
        broker=_broker(available=False),
        readiness=_readiness(status="missing"),
        files=_files(status="missing"),
    )

    assert health == "RED"
    assert "broker_unavailable" in reasons
    assert any(reason.startswith("missing_artifacts:") for reason in reasons)


def test_collect_dynamic_scan_summarizes_latest_artifact(tmp_path: Path) -> None:
    history = tmp_path / "data" / "dynamic_scan_history"
    history.mkdir(parents=True)
    (history / "20260608T130000000000Z_live_bot.json").write_text(
        json.dumps(
            {
                "counts": {"candidates": 3, "accepted": 1, "rejected": 2},
                "accepted": [{"symbol": "NVDA"}],
                "rejected": [
                    {"symbol": "MSFT", "rejection_reason": "below_min_relative_volume"},
                    {"symbol": "TSLA", "rejection_reason": "spread too wide"},
                ],
                "analytics": {"rejections": {"below_min_relative_volume": 1, "spread too wide": 1}},
            }
        ),
        encoding="utf-8",
    )

    summary = diag.collect_dynamic_scan(tmp_path)

    assert summary["candidates_scanned"] == 3
    assert summary["accepted"] == 1
    assert summary["accepted_symbols"] == ["NVDA"]
    assert summary["recent_rejections"][-1] == {"symbol": "TSLA", "reason": "spread too wide"}


def test_main_writes_optional_artifact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.yaml").write_text("broker:\n  paper: true\n", encoding="utf-8")

    def fake_render(**_: object) -> str:
        return "==================================================\nALGOSPHERE DIAGNOSTICS\n==================================================\nHEALTH=GREEN\n"

    monkeypatch.setattr(diag, "render_report", fake_render)

    rc = diag.main(["--project-root", str(tmp_path), "--user", "live_bot"])

    assert rc == 0
    assert "HEALTH=GREEN" in capsys.readouterr().out
    assert (tmp_path / "data" / "diagnostics" / "latest_diagnostics.txt").read_text(encoding="utf-8").endswith("HEALTH=GREEN\n")
