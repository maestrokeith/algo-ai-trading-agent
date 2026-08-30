from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from src.dynamic_spread_tier_research import (
    build_dynamic_spread_tier_research_report,
    render_dynamic_spread_tier_research_report,
    write_dynamic_spread_tier_research_report,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _config() -> dict:
    return {
        "universe": {"symbols": ["SPY", "AAPL", "QQQ"]},
        "execution": {"large_cap_symbols": ["TSLA"]},
    }


def _write_history(path: Path) -> None:
    payload = {
        "generated_at": "2026-07-01T14:00:00+00:00",
        "user_id": "live_bot",
        "candidates": [
            {
                "symbol": "PENNY",
                "timestamp": "2026-07-01T14:00:01+00:00",
                "accepted": False,
                "rejection_reason": "spread too wide",
                "price": 0.82,
                "volume": 0,
                "spread_pct": 22.0,
                "catalyst_score": 0,
                "news_score": 0,
                "event_score": 0,
                "article_count": 0,
            },
            {
                "symbol": "SMALL",
                "timestamp": "2026-07-01T14:01:01+00:00",
                "accepted": False,
                "rejection_reason": "spread_too_wide",
                "price": 3.4,
                "volume": 8_000,
                "spread_pct": 8.5,
                "catalyst_score": 0,
                "news_score": 0,
                "event_score": 0,
                "article_count": 0,
            },
            {
                "symbol": "MID",
                "timestamp": "2026-07-01T14:02:01+00:00",
                "accepted": False,
                "rejection_reason": "spread too wide",
                "price": 11.0,
                "volume": 50_000,
                "spread_pct": 6.0,
                "catalyst_score": 0,
                "news_score": 0,
                "event_score": 0,
                "article_count": 0,
            },
            {
                "symbol": "QUAL",
                "timestamp": "2026-07-01T14:03:01+00:00",
                "accepted": False,
                "rejection_reason": "spread too wide",
                "price": 44.0,
                "volume": 700_000,
                "spread_pct": 4.8,
                "catalyst_score": 0.72,
                "news_score": 0,
                "event_score": 0,
                "article_count": 2,
            },
            {
                "symbol": "AAPL",
                "timestamp": "2026-07-01T14:04:01+00:00",
                "accepted": False,
                "rejection_reason": "spread too wide",
                "price": 210.0,
                "volume": 1_500_000,
                "spread_pct": 3.8,
                "catalyst_score": 0,
                "news_score": 8,
                "event_score": 0,
                "article_count": 1,
            },
            {
                "symbol": "RIGHT.RT",
                "timestamp": "2026-07-01T14:05:01+00:00",
                "accepted": False,
                "rejection_reason": "spread too wide",
                "price": 24.0,
                "volume": 900_000,
                "spread_pct": 4.0,
                "catalyst_score": 0.9,
                "news_score": 0,
                "event_score": 0,
                "article_count": 1,
            },
            {
                "symbol": "TSLA",
                "timestamp": "2026-07-01T14:06:01+00:00",
                "accepted": False,
                "rejection_reason": "spread too wide",
                "price": 101.0,
                "volume": 1_200_000,
                "spread_pct": 4.5,
                "catalyst_score": 0,
                "news_score": 7,
                "event_score": 0,
                "article_count": 1,
            },
            {
                "symbol": "OTHER",
                "timestamp": "2026-07-01T14:07:01+00:00",
                "accepted": False,
                "rejection_reason": "relative volume",
                "price": 50.0,
                "volume": 900_000,
                "spread_pct": 2.0,
            },
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _sample_log() -> str:
    return (
        "2026-07-01T10:06:00 INFO DYNAMIC_SCAN reject LOGSYM: spread too wide "
        "price=26.00 volume=600000 spread=4.90% catalyst_score=0.8 news_score=0 event_score=0 article_count=1"
    )


def test_spread_tier_research_groups_and_candidates(tmp_path: Path) -> None:
    history_dir = tmp_path / "data" / "dynamic_scan_history"
    history_dir.mkdir(parents=True)
    _write_history(history_dir / "20260701T140000000000Z_live_bot.json")

    report = build_dynamic_spread_tier_research_report(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="2026-07-01",
        user_id="live_bot",
        log_text=_sample_log(),
        config=_config(),
    )

    summary = report["summary"]
    assert summary["total_spread_too_wide_rejects"] == 8
    assert summary["price_buckets"] == {
        "$20-$100": 3,
        ">$100": 2,
        "<$1": 1,
        "$1-$5": 1,
        "$5-$20": 1,
    }
    assert summary["volume_buckets"] == {
        "100k-1M": 3,
        ">1M": 2,
        "zero": 1,
        "<10k": 1,
        "10k-100k": 1,
    }
    assert summary["symbol_types"] == {
        "scanner-only": 5,
        "rights/warrants/RT symbols": 1,
        "large-cap": 1,
        "ETF/core list": 1,
    }
    assert summary["catalyst_vs_no_catalyst"] == {"catalyst": 5, "no_catalyst": 3}

    candidates = {row["symbol"]: row for row in report["future_safe_exception_candidates"]}
    assert set(candidates) == {"QUAL", "AAPL", "TSLA", "LOGSYM"}
    assert "RIGHT.RT" not in candidates
    assert candidates["QUAL"]["future_safe_exception_candidate"] is True
    assert candidates["LOGSYM"]["source"] == "logs"


def test_spread_tier_research_symbol_types_and_junk(tmp_path: Path) -> None:
    history_dir = tmp_path / "data" / "dynamic_scan_history"
    history_dir.mkdir(parents=True)
    _write_history(history_dir / "20260701T140000000000Z_live_bot.json")

    report = build_dynamic_spread_tier_research_report(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="2026-07-01",
        user_id="live_bot",
        log_text="",
        config=_config(),
    )

    by_symbol = {row["symbol"]: row for row in report["events"]}
    assert by_symbol["AAPL"]["symbol_type"] == "ETF/core list"
    assert by_symbol["TSLA"]["symbol_type"] == "large-cap"
    assert by_symbol["RIGHT.RT"]["symbol_type"] == "rights/warrants/RT symbols"
    assert by_symbol["PENNY"]["volume_bucket"] == "zero"
    assert report["top_repeated_junk_symbols"][0]["symbol"] == "RIGHT.RT"


def test_spread_tier_research_writes_artifacts_and_cli(tmp_path: Path) -> None:
    history_dir = tmp_path / "data" / "dynamic_scan_history"
    history_dir.mkdir(parents=True)
    _write_history(history_dir / "20260701T140000000000Z_live_bot.json")
    config_path = tmp_path / "config"
    config_path.mkdir()
    (config_path / "default.yaml").write_text(
        "universe:\n  symbols: [SPY, AAPL, QQQ]\nexecution:\n  large_cap_symbols: [TSLA]\n",
        encoding="utf-8",
    )
    log_dir = tmp_path / "data" / "review" / "2026-07-01"
    log_dir.mkdir(parents=True)
    (log_dir / "live.log").write_text(_sample_log(), encoding="utf-8")

    json_path, text_path, report = write_dynamic_spread_tier_research_report(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="2026-07-01",
        user_id="live_bot",
        log_text=_sample_log(),
        config=_config(),
    )

    assert json_path == tmp_path / "data" / "research_metrics" / "2026-07-01" / "dynamic_spread_tier_research.json"
    assert text_path == tmp_path / "data" / "research_metrics" / "2026-07-01" / "dynamic_spread_tier_research.md"
    assert json.loads(json_path.read_text(encoding="utf-8"))["summary"]["future_safe_exception_candidate_count"] == 4
    text = text_path.read_text(encoding="utf-8")
    assert "Dynamic Spread Tier Research 2026-07-01 user=live_bot" in text
    assert "## Price Buckets" in text
    assert "| QUAL | 44.0 | 700000.0 | 4.8 |" in text
    assert "future safe exception candidates: 4" in render_dynamic_spread_tier_research_report(report)

    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_dynamic_spread_tier_research.py"),
            "--date",
            "2026-07-01",
            "--user",
            "live_bot",
            "--project-root",
            str(tmp_path),
            "--data-dir",
            str(tmp_path / "data"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Dynamic Spread Tier Research 2026-07-01 user=live_bot" in result.stdout
    assert "JSON:" in result.stdout
    assert "Markdown:" in result.stdout
