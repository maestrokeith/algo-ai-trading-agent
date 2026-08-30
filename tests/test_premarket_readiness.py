from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.check_premarket_readiness import main as readiness_main
from scripts.check_premarket_runtime_verify import main as runtime_verify_main
from src.premarket_readiness import (
    check_premarket_readiness,
    format_premarket_readiness,
    format_premarket_runtime_symbol,
    format_premarket_runtime_verify,
    premarket_runtime_ready,
    premarket_runtime_symbol_rows,
    premarket_runtime_symbols,
)


def _write_artifacts(project_root: Path, generated_at: datetime, *, ttl_minutes: int = 390) -> None:
    artifact_dir = project_root / "data" / "premarket"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": generated_at.isoformat(),
        "source": "test",
        "ttl_minutes": ttl_minutes,
        "symbols": ["AAPL"],
        "events": [{"symbol": "AAPL", "score": 7.0, "headline": "AAPL catalyst"}],
        "rankings": [{"symbol": "AAPL", "score": 7.0, "catalyst_type": "news"}],
        "catalysts": [{"symbol": "AAPL", "score": 7.0, "catalyst_type": "news"}],
    }
    for name in ("latest_event_feed.json", "latest_rankings.json", "latest_catalysts.json"):
        (artifact_dir / name).write_text(json.dumps(payload), encoding="utf-8")


def _write_empty_artifacts(project_root: Path, generated_at: datetime, *, ttl_minutes: int = 390) -> None:
    artifact_dir = project_root / "data" / "premarket"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": generated_at.isoformat(),
        "source": "test",
        "ttl_minutes": ttl_minutes,
        "symbols": [],
        "events": [],
        "rankings": [],
        "catalysts": [],
    }
    for name in ("latest_event_feed.json", "latest_rankings.json", "latest_catalysts.json"):
        (artifact_dir / name).write_text(json.dumps(payload), encoding="utf-8")


def test_premarket_readiness_fresh_counts_ranked_symbols(tmp_path: Path) -> None:
    now = datetime(2026, 6, 1, 13, 25, tzinfo=timezone.utc)
    _write_artifacts(tmp_path, now - timedelta(minutes=20))

    readiness = check_premarket_readiness(tmp_path, now=now)
    text = format_premarket_readiness(readiness)

    assert readiness.status == "fresh"
    assert readiness.fresh is True
    assert readiness.catalyst_ranked_symbols == 1
    assert readiness.ranking_count == 3
    assert "PREMARKET_READINESS status=fresh present=true fresh=true" in text


def test_premarket_runtime_verify_ready_line_for_fresh_artifacts(tmp_path: Path) -> None:
    now = datetime(2026, 6, 1, 13, 25, tzinfo=timezone.utc)
    _write_artifacts(tmp_path, now - timedelta(minutes=20))

    readiness = check_premarket_readiness(tmp_path, now=now)
    symbols = premarket_runtime_symbols(tmp_path)
    text = format_premarket_runtime_verify(readiness, symbols=symbols)

    assert premarket_runtime_ready(readiness) is True
    assert text == (
        "PREMARKET_RUNTIME_VERIFY ready=true reason=ok "
        "rankings=3 catalysts=3 events=3 symbols=AAPL"
    )


def test_premarket_readiness_missing_and_stale(tmp_path: Path) -> None:
    now = datetime(2026, 6, 1, 13, 25, tzinfo=timezone.utc)
    missing = check_premarket_readiness(tmp_path, now=now)
    assert missing.status == "missing"
    assert set(missing.missing) == {"events", "rankings", "catalysts"}

    _write_artifacts(tmp_path, now - timedelta(minutes=391), ttl_minutes=390)
    stale = check_premarket_readiness(tmp_path, now=now)
    assert stale.status == "stale"
    assert set(stale.stale) == {"events", "rankings", "catalysts"}


def test_premarket_readiness_fresh_empty_distinct_from_stale_and_missing(tmp_path: Path) -> None:
    now = datetime(2026, 6, 1, 13, 25, tzinfo=timezone.utc)
    _write_empty_artifacts(tmp_path, now - timedelta(minutes=5))
    diag_path = tmp_path / "data" / "premarket" / "provider_diagnostics_latest.json"
    diag_path.write_text(
        json.dumps(
            {
                "generated_at": now.isoformat(),
                "providers": {
                    "newsapi": {
                        "enabled": True,
                        "request_sent": True,
                        "http_status": 429,
                        "raw_count": 0,
                        "filtered_count": 0,
                        "rate_limited": True,
                        "duration_ms": 12.5,
                        "reason": "rate_limited",
                    },
                    "alpaca": {
                        "enabled": True,
                        "request_sent": True,
                        "http_status": 200,
                        "raw_count": 0,
                        "filtered_count": 0,
                        "rate_limited": False,
                        "duration_ms": 8.0,
                        "reason": "ok",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    readiness = check_premarket_readiness(tmp_path, now=now)
    text = format_premarket_readiness(readiness)

    assert readiness.status == "fresh_empty"
    assert readiness.fresh is True
    assert readiness.catalyst_ranked_symbols == 0
    assert "PREMARKET_READINESS status=fresh_empty present=true fresh=true" in text
    assert "PREMARKET_PROVIDER_DIAGNOSTICS present=true" in text
    assert "PREMARKET_PROVIDER_STATUS provider=newsapi" in text
    assert "http_status=429" in text
    assert "rate_limited=true" in text
    assert premarket_runtime_ready(readiness) is False
    assert (
        format_premarket_runtime_verify(readiness, symbols=premarket_runtime_symbols(tmp_path))
        == "PREMARKET_RUNTIME_VERIFY ready=false reason=empty_premarket_artifacts rankings=0 catalysts=0 events=0 symbols=none"
    )


def test_premarket_readiness_hints_when_newsapi_disabled_blocks_earnings(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 6, 1, 13, 25, tzinfo=timezone.utc)
    _write_artifacts(tmp_path, now - timedelta(minutes=5))
    diag_path = tmp_path / "data" / "premarket" / "provider_diagnostics_latest.json"
    diag_path.write_text(
        json.dumps(
            {
                "generated_at": now.isoformat(),
                "providers": {
                    "newsapi": {
                        "enabled": False,
                        "request_sent": False,
                        "reason": "newsapi_disabled",
                    },
                    "earnings_overnight": {
                        "enabled": True,
                        "request_sent": False,
                        "reason": "depends_on_newsapi_disabled",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    readiness = check_premarket_readiness(tmp_path, now=now)
    text = format_premarket_readiness(readiness)

    assert "PREMARKET_READY_HINT reason=newsapi_disabled_earnings_overnight_skipped" in text
    assert "earnings_overnight depends on NewsAPI" in text


def test_check_premarket_readiness_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    now = "2026-06-01T09:25:00-04:00"
    assert readiness_main(["--project-root", str(tmp_path), "--now", now]) == 1
    assert "status=missing" in capsys.readouterr().out

    _write_artifacts(tmp_path, datetime(2026, 6, 1, 9, 15, tzinfo=timezone.utc))
    assert readiness_main(["--project-root", str(tmp_path), "--now", now]) == 0
    assert "status=fresh" in capsys.readouterr().out

    _write_empty_artifacts(tmp_path, datetime(2026, 6, 1, 9, 20, tzinfo=timezone.utc))
    assert readiness_main(["--project-root", str(tmp_path), "--now", now]) == 1
    assert "status=fresh_empty" in capsys.readouterr().out


def test_premarket_runtime_verify_cli_exit_codes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert runtime_verify_main([]) == 2
    assert "reason=live_flag_required" in capsys.readouterr().out

    assert runtime_verify_main(["--live", "--project-root", str(tmp_path)]) == 1
    assert "ready=false reason=missing_or_stale_premarket_artifacts" in capsys.readouterr().out


def test_premarket_runtime_verify_verbose_prints_symbol_metadata(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime.now(timezone.utc)
    _write_artifacts(tmp_path, now - timedelta(minutes=5))

    assert runtime_verify_main(["--live", "--verbose", "--project-root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "PREMARKET_RUNTIME_VERIFY ready=true reason=ok" in out
    assert "PREMARKET_RUNTIME_SYMBOL symbol=AAPL" in out
    assert "news_score=7.00" in out
    assert "event_score=7.00" in out
    assert "catalyst_score=0.70" in out
    assert "headline=AAPL catalyst" in out


def test_premarket_runtime_symbol_formatter_includes_metadata(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _write_artifacts(tmp_path, now - timedelta(minutes=5))

    rows = premarket_runtime_symbol_rows(tmp_path, now=now)
    assert rows
    line = format_premarket_runtime_symbol(rows[0])

    assert line.startswith("PREMARKET_RUNTIME_SYMBOL symbol=AAPL")
    assert "news_score=7.00" in line
    assert "catalyst_score=0.70" in line


def test_live_cycle_startup_validation_logs_artifact_status(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import logging
    import src.app.live_cycle as live_cycle

    caplog.set_level(logging.INFO)
    now = datetime(2026, 6, 1, 13, 25, tzinfo=timezone.utc)
    _write_artifacts(tmp_path, now - timedelta(minutes=10))

    live_cycle._log_premarket_artifact_startup_validation(project_root=tmp_path, now=now)

    assert "PREMARKET_STARTUP_ARTIFACTS status=fresh present=true fresh=true" in caplog.text
    assert "catalyst_ranked_symbols=1" in caplog.text
    assert "PREMARKET_STARTUP_ARTIFACT kind=rankings status=fresh" in caplog.text
