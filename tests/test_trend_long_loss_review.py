from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.trend_long_loss_review import (
    build_trend_long_loss_review,
    render_trend_long_loss_review,
    write_trend_long_loss_review,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_bars(path: Path, *, base: float = 100.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        ("2026-07-02T10:00:00-04:00", base, base * 1.00, base * 1.00),
        ("2026-07-02T10:05:00-04:00", base * 1.01, base * 1.015, base * 0.99),
        ("2026-07-02T10:10:00-04:00", base * 0.97, base * 1.02, base * 0.965),
        ("2026-07-02T10:15:00-04:00", base * 0.96, base * 0.98, base * 0.955),
    ]
    lines = ["timestamp,open,high,low,close,volume"]
    for ts, close, high, low in rows:
        lines.append(f"{ts},{close},{high},{low},{close},100000")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_attribution(data_dir: Path) -> None:
    path = data_dir / "trade_attribution" / "daily" / "2026-07-02_live_bot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "date": "2026-07-02",
        "user_id": "live_bot",
        "orders": [
            {
                "timestamp": "2026-07-02T10:00:00-04:00",
                "symbol": "SPY",
                "action": "buy",
                "route": "trend_long",
                "source": "scoring",
                "submitted": True,
                "filled_avg_price": 100.0,
                "filled_qty": 10,
            },
            {
                "timestamp": "2026-07-02T10:20:00-04:00",
                "symbol": "SPY",
                "action": "buy",
                "route": "trend_long",
                "source": "scoring",
                "submitted": True,
                "filled_avg_price": 97.0,
                "filled_qty": 5,
            },
            {
                "timestamp": "2026-07-02T10:05:00-04:00",
                "symbol": "AAL",
                "action": "buy",
                "route": "dynamic_momentum_override",
                "submitted": True,
                "filled_avg_price": 12.0,
                "filled_qty": 10,
            },
        ],
        "exits": [
            {
                "timestamp": "2026-07-02T10:15:00-04:00",
                "symbol": "SPY",
                "qty": 10,
                "exit_price": 96.0,
                "exit_reason": "stop_loss",
                "pnl": -40.0,
                "pnl_pct": -4.0,
                "hold_minutes": 15,
                "entry_route": "trend_long",
                "entry_source": "scoring",
                "stop_distance_pct": 2.5,
            },
            {
                "timestamp": "2026-07-02T10:30:00-04:00",
                "symbol": "AAL",
                "qty": 10,
                "exit_price": 11.5,
                "exit_reason": "stop_loss",
                "pnl": -5.0,
                "entry_route": "dynamic_momentum_override",
            },
        ],
        "candidates": [],
        "allocator_candidates": [],
        "summary": {},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _sample_log() -> str:
    return (
        "2026-07-02T10:00:00-04:00 INFO ENTRY_ALIGNMENT_CONTEXT symbol=SPY route=trend_long "
        "trend=up pullback=shallow momentum_score=0.72 relative_volume=1.2 spread_pct=0.04 "
        "price=100.0 vwap=99.5 ema20=99.0 ema50=98.0\n"
        "2026-07-02T10:00:01-04:00 INFO ENTRY_EVAL_PASS symbol=SPY route=trend_long\n"
    )


def test_trend_long_loss_review_matches_entries_exits_and_context(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_attribution(data_dir)
    _write_bars(data_dir / "historical_bars" / "SPY_2026-07-02_1Min.csv")

    report = build_trend_long_loss_review(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-07-02",
        user_id="live_bot",
        log_text=_sample_log(),
    )

    assert report["research_only"] is True
    assert report["summary"]["trend_long_exits"] == 1
    assert report["summary"]["losing_trades"] == 1
    assert report["summary"]["total_loss_pnl"] == pytest.approx(-40.0)
    row = report["trend_long_trades"][0]
    assert row["symbol"] == "SPY"
    assert row["entry_price"] == pytest.approx(100.0)
    assert row["exit_price"] == pytest.approx(96.0)
    assert row["pnl"] == pytest.approx(-40.0)
    assert row["exit_reason"] == "stop_loss"
    assert row["stop_distance_pct"] == pytest.approx(2.5)
    assert row["max_favorable_excursion_pct"] == pytest.approx(2.0)
    assert row["max_adverse_excursion_pct"] == pytest.approx(-4.5)
    assert row["reentry_happened"] is True
    assert row["churn_reversal_count"] == 2
    assert row["entry_context"]["trend"] == "up"
    assert row["entry_context"]["pullback"] == "shallow"
    assert row["entry_context"]["momentum"] == pytest.approx(0.72)
    assert row["entry_context"]["relative_volume"] == pytest.approx(1.2)
    assert row["entry_context"]["spread_pct"] == pytest.approx(0.04)
    assert row["entry_context"]["vwap_distance_pct"] == pytest.approx(0.5025)
    reentry = report["reentry_analysis"]
    assert reentry["rows"][0]["symbol"] == "SPY"
    assert reentry["rows"][0]["attempted_reentry"] == "2026-07-02T10:20:00-04:00"
    assert reentry["rows"][0]["would_have_been_blocked"] is True
    assert reentry["summary"]["net_benefit"] is not None
    assert "Review trend_long re-entry/churn controls" in report["recommendations"][0]


def test_trend_long_loss_review_renders_top_losers(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_attribution(data_dir)

    report = build_trend_long_loss_review(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-07-02",
        user_id="live_bot",
        log_text=_sample_log(),
    )

    text = render_trend_long_loss_review(report)
    assert "Trend Long Loss Review 2026-07-02 user=live_bot" in text
    assert "| SPY | 2026-07-02T10:00:00-04:00 | 2026-07-02T10:15:00-04:00 | 100.0 | 96.0 | -40.0 | -4.0 | stop_loss |" in text
    assert "trend=up pullback=shallow momentum=0.72 rvol=1.2 spread=0.04" in text
    assert "## Re-entry Analysis" in text
    assert "| SPY | 2026-07-02T10:15:00-04:00 | 2026-07-02T10:20:00-04:00 |" in text
    assert "- SPY: pnl=-40.0 pnl_pct=-4.0 exit_reason=stop_loss" in text


def test_trend_long_loss_review_writes_artifacts_and_cli(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_attribution(data_dir)
    log_dir = data_dir / "review" / "2026-07-02"
    log_dir.mkdir(parents=True)
    (log_dir / "live.log").write_text(_sample_log(), encoding="utf-8")

    json_path, text_path, report = write_trend_long_loss_review(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-07-02",
        user_id="live_bot",
    )

    assert json_path == data_dir / "research_metrics" / "2026-07-02" / "trend_long_loss_review.json"
    assert text_path == data_dir / "research_metrics" / "2026-07-02" / "trend_long_loss_review.md"
    assert json.loads(json_path.read_text(encoding="utf-8"))["summary"]["losing_trades"] == 1
    assert "Trend Long Loss Review 2026-07-02 user=live_bot" in text_path.read_text(encoding="utf-8")
    assert report["debug"]["attribution_orders"] == 3

    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_trend_long_loss_review.py"),
            "--date",
            "2026-07-02",
            "--user",
            "live_bot",
            "--project-root",
            str(tmp_path),
            "--data-dir",
            str(data_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Trend Long Loss Review 2026-07-02 user=live_bot" in result.stdout
    assert "JSON:" in result.stdout
    assert "Markdown:" in result.stdout
