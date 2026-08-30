from __future__ import annotations

import json
import subprocess
from pathlib import Path

from src.growth_diagnostic_report import build_growth_diagnostic_report, write_growth_diagnostic_report


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _sample_root(tmp_path: Path) -> Path:
    day = "2026-07-08"
    _write_json(
        tmp_path / "data" / "research_metrics" / day / "end_day_live.json",
        {
            "context": {
                "account_summary": {
                    "stdout": (
                        "PnL: realized=$-139.73 unrealized=$0.00 total=$-139.73\n"
                        "Activity: submitted_orders=6 buys=6 sells=11 exits=11 pnl_missing_exits=7\n"
                        "Risk guards: triggered=intraday_loss_stop_entries trend_long_blocked=false\n"
                    )
                },
                "positions": {
                    "stdout": "[LIVE] Equity: $27,508.97\nTOTAL                     $   2,935.74  $      +8.66     +0.30%\n"
                },
            },
            "logs": {
                "dynamic": {"selected_count": 7, "rejected_reasons": {"unknown": 6209}},
                "entry_lane": {
                    "pass_symbols": ["AAPL", "PENG"],
                    "allocator_trace_symbols": ["AAPL", "PENG"],
                    "by_route": {
                        "trend_long": {"total": 100, "pass": 31, "fail": 69},
                        "news_catalyst": {"total": 2, "pass": 1},
                    },
                },
                "allocator": {"actions_count": 45, "reject_reasons": {"PENG:min_trade_dollars": 2}},
                "orders": {"confirmation_count": 10, "filled_count": 4},
            },
        },
    )
    _write_json(
        tmp_path / "data" / "profitability_attribution" / "daily" / f"{day}_live_bot.json",
        {
            "overall_pnl": {"realized": -139.73, "unrealized": 0.0, "total": -139.73},
            "pnl_by_route": {"trend_long": -113.64, "dynamic_momentum": 0.0},
            "route_stats": {
                "trend_long": {"trades": 3, "wins": 0, "losses": 3},
                "dynamic_momentum": {"trades": 0, "wins": 0, "losses": 0},
            },
            "exit_reason_stats": {"stop_loss": {"exits": 4}},
            "top_losers": [{"symbol": "IWM", "route": "trend_long", "pnl": -39.93, "reason": "stop_loss"}],
        },
    )
    _write_json(
        tmp_path / "data" / "positions_live_bot.json",
        {
            "positions": {
                "OLD": {"entry_time": "2026-07-01T10:00:00-04:00", "pnl_pct": -0.03},
            }
        },
    )
    return tmp_path


def test_growth_diagnostic_report_opportunity_funnel_and_expectancy(tmp_path: Path) -> None:
    root = _sample_root(tmp_path)

    report = build_growth_diagnostic_report(root, end_date="latest", lookback_days=10, user_id="live_bot")

    assert report["account_equity_change"]["total_pnl"] == -139.73
    assert report["opportunity_funnel"]["dynamic_candidates"] == 7
    assert report["opportunity_funnel"]["allocator_actions"] == 45
    assert report["route_expectancy"]["trend_long"]["expectancy"] == -37.88
    assert "reporting_defect" in report["defect_classes"]
    assert report["sizing_blocks"] == ["PENG:min_trade_dollars"]
    assert report["stale_positions"][0]["symbol"] == "OLD"


def test_growth_diagnostic_report_writes_json_and_html(tmp_path: Path) -> None:
    root = _sample_root(tmp_path)

    artifacts = write_growth_diagnostic_report(root, end_date="latest", lookback_days=10, user_id="live_bot")

    assert artifacts.json_path.exists()
    assert artifacts.html_path.exists()
    assert json.loads(artifacts.json_path.read_text(encoding="utf-8"))["confidence"] == "low"
    assert "Growth Diagnostic Report" in artifacts.html_path.read_text(encoding="utf-8")


def test_growth_diagnostic_cli_works(tmp_path: Path) -> None:
    root = _sample_root(tmp_path)

    proc = subprocess.run(
        [
            "python",
            str(PROJECT_ROOT / "scripts" / "generate_growth_diagnostic_report.py"),
            "--project-root",
            str(root),
            "--date",
            "latest",
            "--lookback-days",
            "10",
            "--user",
            "live_bot",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert proc.returncode == 0
    assert "GROWTH_DIAGNOSTIC_JSON path=" in proc.stdout
    assert "Total P/L: $-139.73" in proc.stdout
