from __future__ import annotations

import csv
import json
from pathlib import Path

import scripts.generate_daily_summary as daily
import scripts.generate_weekly_review as weekly


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_generate_daily_summary_reads_fake_inputs_and_appends_journal(
    tmp_path: Path,
    capsys,
) -> None:
    _write_json(
        tmp_path / "data" / "dynamic_scan_history" / "20260608T130000000000Z_live_bot.json",
        {
            "counts": {"candidates": 4, "accepted": 1, "rejected": 3},
            "accepted": [{"symbol": "ABAT"}],
            "rejected": [{"symbol": "SUNE", "rejection_reason": "gain_filter"}],
            "analytics": {"rejections": {"gain_filter": 2, "spread too wide": 1}},
        },
    )
    _write_json(
        tmp_path / "data" / "trade_attribution" / "daily" / "2026-06-08_live_bot.json",
        {
            "orders": [
                {"symbol": "ABAT", "action": "buy", "source": "dynamic_universe"},
                {"symbol": "ABAT", "action": "sell", "source": "dynamic_universe"},
                {"symbol": "AAPL", "action": "buy", "source": "core_rebuild"},
            ],
            "exits": [
                {"symbol": "ABAT", "pnl": 12.5},
                {"symbol": "MSFT", "pnl": -4.0},
            ],
        },
    )
    _write_json(
        tmp_path / "data" / "daily_summary" / "2026-06-08_live_bot.json",
        {"unrealized_pnl": 8.25},
    )
    log_path = tmp_path / "data" / "logs" / "algo_2026-06-08.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        "\n".join(
            [
                "2026-06-08 DYNAMIC_SCAN selected=['ABAT']",
                "2026-06-08 APIError insufficient qty available",
                "2026-06-08 PREMARKET_STARTUP_ARTIFACTS status=stale",
            ]
        ),
        encoding="utf-8",
    )

    rc = daily.main(["--date", "2026-06-08", "--user", "live_bot", "--project-root", str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "DAILY_SUMMARY path=" in out
    report = (tmp_path / "reports" / "daily" / "2026-06-08.md").read_text(encoding="utf-8")
    assert "- candidates count: 4" in report
    assert "- entries count: 2" in report
    assert "- exits count: 1" in report
    assert "- realized PnL: 8.50" in report
    assert "- unrealized PnL: 8.25" in report
    assert "- dynamic activity: 2" in report
    assert "- core activity: 1" in report
    assert "- gain_filter: 2" in report
    assert "APIError insufficient qty available" in report
    journal = tmp_path / "data" / "analytics" / "trading_journal.csv"
    with journal.open("r", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows == [
        {
            "date": "2026-06-08",
            "candidates": "4",
            "entries": "2",
            "exits": "1",
            "winners": "1",
            "losers": "1",
            "realized_pnl": "8.50",
            "top_reject_reason": "gain_filter",
            "notes": "operational issues: 5",
        }
    ]


def test_generate_daily_summary_missing_files_writes_not_available(tmp_path: Path) -> None:
    rc = daily.main(["--date", "2026-06-08", "--project-root", str(tmp_path)])

    assert rc == 0
    report = (tmp_path / "reports" / "daily" / "2026-06-08.md").read_text(encoding="utf-8")
    assert "- candidates count: not available" in report
    assert "- entries count: not available" in report
    assert "- exits count: not available" in report
    assert "- realized PnL: not available" in report
    assert "missing artifact:" in report


def test_daily_summary_rerun_replaces_same_journal_date(tmp_path: Path) -> None:
    assert daily.main(["--date", "2026-06-08", "--project-root", str(tmp_path)]) == 0
    assert daily.main(["--date", "2026-06-08", "--project-root", str(tmp_path)]) == 0

    with (tmp_path / "data" / "analytics" / "trading_journal.csv").open("r", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-06-08"


def test_generate_weekly_review_reads_daily_markdown(tmp_path: Path, capsys) -> None:
    daily_dir = tmp_path / "reports" / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-06-08.md").write_text(
        """# Trading Journal Daily Summary 2026-06-08

- date: 2026-06-08
- candidates count: 4
- entries count: 2
- exits count: 1
- realized PnL: 8.50
- dynamic activity: 2
- core activity: 1

## Top Reject Reasons

- gain_filter: 2

## Operational Issues

- APIError insufficient qty available
""",
        encoding="utf-8",
    )
    (daily_dir / "2026-06-09.md").write_text(
        """# Trading Journal Daily Summary 2026-06-09

- date: 2026-06-09
- candidates count: 3
- entries count: 1
- exits count: 1
- realized PnL: -2.00
- dynamic activity: 1
- core activity: 0

## Top Reject Reasons

- spread too wide: 1

## Operational Issues

- none found
""",
        encoding="utf-8",
    )

    rc = weekly.main(["--week-start", "2026-06-08", "--project-root", str(tmp_path)])

    assert rc == 0
    assert "WEEKLY_REVIEW path=" in capsys.readouterr().out
    report = (tmp_path / "reports" / "weekly" / "week_2026_06_08.md").read_text(encoding="utf-8")
    assert "## Reliability Summary" in report
    assert "- daily reports found: 2" in report
    assert "- candidates: 7" in report
    assert "- entries: 3" in report
    assert "- realized PnL: 6.50" in report
    assert "- dynamic activity: 3" in report
    assert "1. APIError insufficient qty available" in report


def test_generate_weekly_review_missing_daily_reports_does_not_fail(tmp_path: Path) -> None:
    rc = weekly.main(["--week-start", "2026-06-08", "--project-root", str(tmp_path)])

    assert rc == 0
    report = (tmp_path / "reports" / "weekly" / "week_2026_06_08.md").read_text(encoding="utf-8")
    assert "- daily reports found: 0" in report
    assert "- candidates: not available" in report
    assert "Generate daily summaries before weekly review" in report
