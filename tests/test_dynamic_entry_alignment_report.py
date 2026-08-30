from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from src.dynamic_entry_alignment_report import (
    build_dynamic_entry_alignment_report,
    render_dynamic_entry_alignment_report,
    write_dynamic_entry_alignment_report,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _row(symbol: str, timestamp: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "symbol": symbol,
        "timestamp": timestamp,
        "accepted": False,
        "rejection_reason": (
            "entry_alignment: need 5m breakout OR new intraday high OR strong green 1m "
            "OR opening-range breakout (got breakout=False nh=False green=False orb=False)"
        ),
        "price": 24.0,
        "gain_pct": 12.0,
        "volume": 1_200_000,
        "relative_volume": 1.4,
        "spread_pct": 0.4,
        "vwap_distance_pct": 0.8,
        "above_ema20": False,
        "above_ema50": True,
        "volume_confirmation": True,
        "momentum_score": 8.0,
        "quality": {
            "price_above_vwap": True,
            "five_min_trend_aligned": False,
            "five_min_up_streak": 0,
            "atr_expansion_ratio": 1.2,
        },
    }
    row.update(overrides)
    return row


def _write_history(path: Path) -> None:
    payload = {
        "generated_at": "2026-07-01T14:00:00+00:00",
        "user_id": "live_bot",
        "candidates": [
            _row("ALIGN", "2026-07-01T14:00:00+00:00"),
            _row("ALIGN", "2026-07-01T14:10:00+00:00", volume=900_000),
            _row(
                "VWAPFAIL",
                "2026-07-01T14:05:00+00:00",
                quality={"price_above_vwap": False, "five_min_trend_aligned": False},
                above_ema20=True,
                above_ema50=False,
                volume_confirmation=False,
                relative_volume=0.4,
                momentum_score=0.0,
            ),
            _row(
                "LATE",
                "2026-07-01T14:15:00+00:00",
                price=32.0,
                volume=1_500_000,
                relative_volume=2.0,
                spread_pct=0.2,
                above_ema20=True,
                above_ema50=True,
            ),
            {
                "symbol": "OTHER",
                "timestamp": "2026-07-01T14:20:00+00:00",
                "accepted": False,
                "rejection_reason": "below_min_price",
            },
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _sample_log() -> str:
    return "\n".join(
        [
            "2026-07-01T14:00:00+00:00 INFO ENTRY_ALIGNMENT_CONTEXT symbol=ALIGN outcome=fail reason=need_breakout "
            "momentum_score=8.2500 ema20=24.1000 ema50=23.9000 price=24.0000 vwap=23.7500 "
            "vwap_distance_pct=1.0526 ema_distance_pct=0.0003 breakout=false higher_high=false orb=false "
            "strong_green=false trend_strength=-0.5000 slope=-0.1000 five_min_trend_direction=down atr=0.6500 "
            "relative_volume=1.4000 gain_pct=12.0000 spread_pct=0.4000 above_vwap=true above_ema20=false "
            "above_ema50=true volume_confirmation=true",
            "2026-07-01T14:45:00+00:00 INFO ENTRY_ALIGNMENT_CONTEXT symbol=LATE outcome=pass reason=ok "
            "momentum_score=18.5000 ema20=31.0000 ema50=30.5000 price=32.0000 vwap=31.2500 "
            "vwap_distance_pct=2.4000 ema_distance_pct=3.2460 breakout=true higher_high=true orb=false "
            "strong_green=false trend_strength=1.2000 slope=0.3000 five_min_trend_direction=up atr=0.7500 "
            "relative_volume=2.0000 gain_pct=14.0000 spread_pct=0.2000 above_vwap=true above_ema20=true "
            "above_ema50=true volume_confirmation=true",
            "2026-07-01T14:45:00+00:00 INFO ENTRY_EVAL_PASS symbol=LATE route=dynamic_momentum_override reason=ok allocator_on=true",
        ]
    )


def test_entry_alignment_report_extracts_failed_subchecks_and_summary(tmp_path: Path) -> None:
    history_dir = tmp_path / "data" / "dynamic_scan_history"
    history_dir.mkdir(parents=True)
    _write_history(history_dir / "20260701T140000000000Z_live_bot.json")

    report = build_dynamic_entry_alignment_report(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="2026-07-01",
        user_id="live_bot",
        log_text=_sample_log(),
    )

    assert report["summary"]["entry_alignment_rejections"] == 4
    counts = report["summary"]["failure_counts_by_sub_check"]
    assert counts["feature_unavailable"] == 2
    assert counts["breakout"] == 2
    assert counts["higher_high"] == 2
    assert counts["strong_green_bar"] == 2
    assert counts["above_ema20"] == 2
    assert "above_vwap" not in counts
    assert "momentum" not in counts

    events = {(event["symbol"], event["timestamp"]): event for event in report["events"]}
    first_align = events[("ALIGN", "2026-07-01T14:00:00+00:00")]
    assert first_align["alignment_subchecks"]["above_vwap"] is True
    assert first_align["alignment_subchecks"]["above_ema20"] is False
    assert first_align["failed_checks"] == ["above_ema20", "breakout", "higher_high", "strong_green_bar"]
    assert first_align["momentum_score"] == 8.25
    assert first_align["ema20"] == 24.1
    assert first_align["ema50"] == 23.9
    assert first_align["vwap"] == 23.75
    assert first_align["vwap_distance"] == 1.0526
    assert first_align["five_min_trend"] == "down"
    assert first_align["trend_strength"] == -0.5
    assert first_align["slope"] == -0.1
    assert first_align["atr"] == 0.65
    assert report["summary"]["symbols_repeatedly_failing_same_sub_check"][0]["symbol"] == "ALIGN"
    assert report["summary"]["classification_counts"]["data_quality_block"] == 2
    assert report["summary"]["missing_feature_counts"]["vwap"] == 2
    averages = report["summary"]["alignment_context_averages"]
    assert averages["pass"]["momentum_avg"] == 18.5
    assert averages["fail"]["momentum_avg"] == 8.25
    assert averages["diff"]["momentum_avg"] == 10.25


def test_entry_alignment_report_passed_later_and_high_liquidity(tmp_path: Path) -> None:
    history_dir = tmp_path / "data" / "dynamic_scan_history"
    history_dir.mkdir(parents=True)
    _write_history(history_dir / "20260701T140000000000Z_live_bot.json")

    report = build_dynamic_entry_alignment_report(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="2026-07-01",
        user_id="live_bot",
        log_text=_sample_log(),
    )

    late = next(event for event in report["events"] if event["symbol"] == "LATE")
    assert late["passed_later"] is True
    assert late["delay_until_alignment_passed_minutes"] == 30.0
    assert report["summary"]["candidates_passed_later_count"] == 1
    assert report["summary"]["average_delay_until_alignment_passed_minutes"] == 30.0
    assert {event["symbol"] for event in report["high_liquidity_candidates"]} == {"ALIGN", "VWAPFAIL", "LATE"}


def test_entry_alignment_report_writes_artifacts_and_cli(tmp_path: Path) -> None:
    history_dir = tmp_path / "data" / "dynamic_scan_history"
    history_dir.mkdir(parents=True)
    _write_history(history_dir / "20260701T140000000000Z_live_bot.json")
    log_dir = tmp_path / "data" / "review" / "2026-07-01"
    log_dir.mkdir(parents=True)
    (log_dir / "live.log").write_text(_sample_log(), encoding="utf-8")

    json_path, text_path, report = write_dynamic_entry_alignment_report(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="2026-07-01",
        user_id="live_bot",
        log_text=_sample_log(),
    )

    assert json_path == tmp_path / "data" / "research_metrics" / "2026-07-01" / "dynamic_entry_alignment.json"
    assert text_path == tmp_path / "data" / "research_metrics" / "2026-07-01" / "dynamic_entry_alignment.md"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["summary"]["entry_alignment_rejections"] == 4
    text = text_path.read_text(encoding="utf-8")
    assert "Dynamic Entry Alignment Report 2026-07-01 user=live_bot" in text
    assert "## Alignment Context Averages" in text
    assert "| momentum avg | 18.5 | 8.25 | 10.25 |" in text
    assert "classification: data_quality_block" in text
    assert "feature_unavailable" in text
    assert "EMA20: 24.1" in text
    assert "✗ above_ema20" in text
    assert "✓ above_vwap" in render_dynamic_entry_alignment_report(report)

    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_dynamic_entry_alignment_report.py"),
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

    assert "Dynamic Entry Alignment Report 2026-07-01 user=live_bot" in result.stdout
    assert "JSON:" in result.stdout
    assert "Markdown:" in result.stdout
