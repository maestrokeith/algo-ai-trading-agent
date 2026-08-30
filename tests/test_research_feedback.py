from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from src.research_feedback import (
    build_research_feedback_report,
    render_research_feedback_markdown,
    write_research_feedback_outputs,
)
from src.trade_attribution import attribution_daily_path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _seed_day(data_dir: Path, day: str, *, user: str = "live_bot") -> None:
    _write_json(
        attribution_daily_path(data_dir=data_dir, user_id=user, day=day),
        {
            "version": 1,
            "date": day,
            "user_id": user,
            "orders": [
                {"symbol": "AAPL", "action": "buy", "submitted": True},
                {"symbol": "AAPL", "action": "sell", "submitted": True},
            ],
            "exits": [
                {
                    "symbol": "AAPL",
                    "entry_route": "dynamic_momentum",
                    "pnl": 120.0,
                    "pnl_pct": 4.0,
                    "exit_reason": "profit_target",
                    "hold_minutes": 80,
                    "catalyst_type": "earnings",
                    "news_score": 8,
                    "relative_volume": 3.2,
                    "sector": "technology",
                },
                {
                    "symbol": "NVTS",
                    "entry_route": "dynamic_momentum",
                    "pnl": -40.0,
                    "pnl_pct": -2.0,
                    "exit_reason": "signal_flip",
                    "hold_minutes": 12,
                    "catalyst_type": "social",
                    "news_score": 3,
                    "relative_volume": 0.8,
                    "sector": "semiconductors",
                },
            ],
        },
    )
    catalyst_path = data_dir / "analytics" / "catalyst_outcomes.json"
    existing = json.loads(catalyst_path.read_text(encoding="utf-8")) if catalyst_path.exists() else {"outcomes": []}
    existing.setdefault("outcomes", []).append(
        {
            "user_id": user,
            "symbol": "AAPL",
            "date": day,
            "catalyst_type": "earnings",
            "news_score": 8,
            "realized_return_pct": 4.0,
            "hold_duration_minutes": 80,
            "sector": "technology",
        }
    )
    _write_json(catalyst_path, existing)
    _write_json(
        data_dir / "replay_market_session" / f"{day}_{user}.json",
        {
            "clock": {"tick_count": 5, "cycles_with_data": 3},
            "mock_orders": [{"symbol": "AAPL", "side": "buy"}],
            "route_level_pnl_estimate": {"dynamic_momentum": 100.0},
            "churn_same_day_reversal_stats": {
                "same_day_reversal_count": 1,
                "repeat_order_count": 1,
            },
        },
    )


def test_research_feedback_evaluates_dimensions_and_recommendations(tmp_path: Path) -> None:
    _seed_day(tmp_path, "2026-06-07")

    report = build_research_feedback_report(data_dir=tmp_path, user_id="live_bot", day="2026-06-07")

    assert report["inputs"]["trade_count"] == 3
    assert report["evaluations"]["catalyst_type"]["earnings"]["sample_count"] == 2
    assert report["evaluations"]["news_score"]["7-10"]["avg_return_pct"] == 4.0
    assert report["evaluations"]["relative_volume"]["<1x"]["avg_return_pct"] == -2.0
    assert report["replay_validation"]["available"] is True
    rec_text = "\n".join(row["rationale"] for row in report["recommendations"])
    assert "catalyst_type=earnings" in rec_text
    assert "Replay found 1 same-day reversals" in rec_text
    assert all(row["auto_apply"] is False for row in report["recommendations"])


def test_research_feedback_writes_markdown_and_dashboards(tmp_path: Path) -> None:
    _seed_day(tmp_path / "data", "2026-06-07")
    report = build_research_feedback_report(data_dir=tmp_path / "data", user_id="live_bot", day="2026-06-07")

    paths = write_research_feedback_outputs(report, project_root=tmp_path)

    assert paths["markdown"] == tmp_path / "reports" / "research_feedback" / "2026-06-07.md"
    assert "Research Feedback 2026-06-07 [live_bot]" in paths["markdown"].read_text(encoding="utf-8")
    assert paths["dashboard_json"].exists()
    assert paths["dashboard_html"].exists()


def test_weekly_research_feedback_uses_lookback_dates(tmp_path: Path) -> None:
    _seed_day(tmp_path, "2026-06-06")
    _seed_day(tmp_path, "2026-06-07")

    report = build_research_feedback_report(data_dir=tmp_path, user_id="live_bot", day="2026-06-07", lookback_days=2)

    assert report["period"] == "weekly"
    assert report["inputs"]["dates"] == ["2026-06-06", "2026-06-07"]
    assert report["inputs"]["trade_count"] == 6
    markdown = render_research_feedback_markdown(report)
    assert "Period: weekly" in markdown


def test_generate_research_feedback_cli_direct_execution(tmp_path: Path) -> None:
    _seed_day(tmp_path / "data", "2026-06-07")

    proc = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_research_feedback.py"),
            "2026-06-07",
            "--user",
            "live_bot",
            "--data-dir",
            str(tmp_path / "data"),
            "--project-root",
            str(tmp_path),
        ],
        cwd=tmp_path,
        env={},
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "Research feedback report:" in proc.stdout
    assert (tmp_path / "reports" / "research_feedback" / "2026-06-07.md").exists()


def test_generate_research_feedback_cli_date_flag_overrides_positional(tmp_path: Path) -> None:
    _seed_day(tmp_path / "data", "2026-06-06")
    _seed_day(tmp_path / "data", "2026-06-07")

    proc = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_research_feedback.py"),
            "2026-06-06",
            "--date",
            "2026-06-07",
            "--user",
            "live_bot",
            "--data-dir",
            str(tmp_path / "data"),
            "--project-root",
            str(tmp_path),
        ],
        cwd=tmp_path,
        env={},
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "reports" / "research_feedback" / "2026-06-07.md").exists()
    assert not (tmp_path / "reports" / "research_feedback" / "2026-06-06.md").exists()
