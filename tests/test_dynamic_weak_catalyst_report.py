from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.dynamic_weak_catalyst_report import (
    build_dynamic_weak_catalyst_report,
    format_dynamic_weak_catalyst_report,
)
from src.trade_attribution import attribution_daily_path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_dynamic_weak_catalyst_logs_are_parsed(tmp_path: Path) -> None:
    logs = "\n".join(
        [
            "2026-06-25 09:40:00 DYNAMIC_WEAK_CATALYST_CLASSIFIED symbol=ABCD news_score=0 catalyst_score=0",
            "2026-06-25 09:41:00 DYNAMIC_WEAK_CATALYST_REJECT symbol=ABCD reason=weak_catalyst",
            "2026-06-25 09:42:00 DYNAMIC_WEAK_CATALYST_SIZE_REDUCED symbol=WXYZ notional_after=450.50",
            "2026-06-25 09:43:00 ORDER_SUBMITTED symbol=WXYZ side=buy notional=450.50 weak_catalyst=true",
            "2026-06-25 09:44:00 ORDER_FILLED symbol=WXYZ side=buy order_id=wxyz-1",
        ]
    )

    report = build_dynamic_weak_catalyst_report(
        data_dir=tmp_path,
        user_id="live_bot",
        day="2026-06-25",
        log_text=logs,
    )

    assert report["classified"] == 2
    assert report["rejected"] == 1
    assert report["size_reduced"] == 1
    assert report["orders"] == 1
    assert report["fills"] == 1
    assert report["avg_notional"] == pytest.approx(450.50)
    assert report["top_rejected_weak_catalyst_names"] == [{"symbol": "ABCD", "count": 1}]


def test_dynamic_weak_catalyst_pnl_and_losers_are_summarized(tmp_path: Path) -> None:
    _write_json(
        attribution_daily_path(data_dir=tmp_path, user_id="live_bot", day="2026-06-25"),
        {
            "exits": [
                {
                    "symbol": "WXYZ",
                    "entry_route": "dynamic_momentum_override",
                    "pnl": -80.0,
                    "weak_catalyst": True,
                },
                {
                    "symbol": "ABCD",
                    "entry_route": "dynamic_momentum_override",
                    "pnl": 20.0,
                    "weak_catalyst": True,
                },
            ]
        },
    )
    logs = "\n".join(
        [
            "DYNAMIC_WEAK_CATALYST_CLASSIFIED symbol=WXYZ",
            "ORDER_SUBMITTED symbol=WXYZ side=buy weak_catalyst=true",
            "DYNAMIC_WEAK_CATALYST_CLASSIFIED symbol=ABCD",
            "ORDER_SUBMITTED symbol=ABCD side=buy weak_catalyst=true",
        ]
    )

    report = build_dynamic_weak_catalyst_report(
        data_dir=tmp_path,
        user_id="live_bot",
        day="2026-06-25",
        log_text=logs,
    )
    text = format_dynamic_weak_catalyst_report(report)

    assert report["realized_pnl"] == pytest.approx(-60.0)
    assert report["top_weak_catalyst_losers"] == [{"symbol": "WXYZ", "pnl": -80.0}]
    assert "- realized_pnl: -60.00" in text
    assert "- top_weak_catalyst_losers: WXYZ:-80.00" in text


def test_dynamic_weak_catalyst_recommendation_tightens_when_pnl_negative_with_orders(
    tmp_path: Path,
) -> None:
    exits = [
        {"symbol": f"W{i}", "entry_route": "dynamic_momentum_override", "pnl": -10.0, "weak_catalyst": True}
        for i in range(4)
    ]
    _write_json(
        attribution_daily_path(data_dir=tmp_path, user_id="live_bot", day="2026-06-25"),
        {"exits": exits},
    )
    logs = "\n".join(
        f"DYNAMIC_WEAK_CATALYST_CLASSIFIED symbol=W{i}\nORDER_SUBMITTED symbol=W{i} side=buy weak_catalyst=true"
        for i in range(4)
    )

    report = build_dynamic_weak_catalyst_report(
        data_dir=tmp_path,
        user_id="live_bot",
        day="2026-06-25",
        log_text=logs,
    )

    assert report["orders"] == 4
    assert report["realized_pnl"] == pytest.approx(-40.0)
    assert report["recommendation"] == "tighten RVOL or smaller starter"


def test_dynamic_weak_catalyst_recommendation_leaves_when_rejected_and_strong_traded(
    tmp_path: Path,
) -> None:
    _write_json(
        attribution_daily_path(data_dir=tmp_path, user_id="live_bot", day="2026-06-25"),
        {
            "exits": [
                {
                    "symbol": "STRG",
                    "entry_route": "dynamic_momentum_override",
                    "pnl": 25.0,
                    "news_score": 9,
                    "catalyst_score": 0.9,
                }
            ]
        },
    )
    logs = "DYNAMIC_WEAK_CATALYST_REJECT symbol=WEAK reason=weak_catalyst"

    report = build_dynamic_weak_catalyst_report(
        data_dir=tmp_path,
        user_id="live_bot",
        day="2026-06-25",
        log_text=logs,
    )

    assert report["strong_catalyst_trades_unchanged_count"] == 1
    assert report["recommendation"] == "leave unchanged"


def test_strong_catalyst_path_is_not_treated_as_weak_catalyst(tmp_path: Path) -> None:
    _write_json(
        attribution_daily_path(data_dir=tmp_path, user_id="live_bot", day="2026-06-25"),
        {
            "exits": [
                {
                    "symbol": "STRG",
                    "entry_route": "dynamic_momentum_override",
                    "pnl": -5.0,
                    "news_score": 8,
                    "catalyst_score": 0.9,
                }
            ]
        },
    )
    logs = "ORDER_SUBMITTED symbol=STRG side=buy catalyst_score=0.9 weak_catalyst=false"

    report = build_dynamic_weak_catalyst_report(
        data_dir=tmp_path,
        user_id="live_bot",
        day="2026-06-25",
        log_text=logs,
    )

    assert report["classified"] == 0
    assert report["orders"] == 0
    assert report["realized_pnl"] == pytest.approx(0.0)
    assert report["strong_catalyst_trades_unchanged_count"] == 1


def test_dynamic_weak_catalyst_cli_prints_review(tmp_path: Path) -> None:
    log_file = tmp_path / "weak.log"
    log_file.write_text("DYNAMIC_WEAK_CATALYST_CLASSIFIED symbol=ABCD\n", encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_dynamic_weak_catalyst_report.py"),
            "--date",
            "2026-06-25",
            "--user",
            "live_bot",
            "--data-dir",
            str(tmp_path),
            "--log-file",
            str(log_file),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        cwd=PROJECT_ROOT,
    )

    assert proc.returncode == 0, proc.stderr
    assert "DYNAMIC_WEAK_CATALYST_REVIEW" in proc.stdout
    assert "- classified: 1" in proc.stdout
