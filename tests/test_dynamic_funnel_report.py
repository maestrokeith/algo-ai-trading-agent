from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from src.dynamic_funnel_report import (
    build_dynamic_funnel_report,
    render_dynamic_funnel_report,
    write_dynamic_funnel_report,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _sample_log() -> str:
    return "\n".join(
        [
            "2026-07-01T09:35:00 INFO DYNAMIC_SCAN selected=['ABCD', 'EFGH']",
            "2026-07-01T09:35:01 INFO QUOTE_RETRY_START symbol=WIDE reason=unstable_quote attempt=1",
            "2026-07-01T09:35:02 INFO QUOTE_RETRY_SUCCESS symbol=WIDE attempt=1",
            "2026-07-01T09:35:03 INFO QUOTE_RETRY_START symbol=NOISY reason=unstable_quote attempt=1",
            "2026-07-01T09:35:04 INFO QUOTE_RETRY_FAILED symbol=NOISY attempts=2",
            (
                "2026-07-01T09:35:05 INFO DYNAMIC_REJECT_FUNNEL reason=unstable quote "
                "symbol=NOISY stage=scanner gain_pct=18.5 relative_volume=2.4 spread_pct=44.0 "
                "price_above_vwap=false signal_score=7.2 news_score=0 catalyst_score=0 event_score=0 article_count=0"
            ),
            (
                "2026-07-01T09:36:00 INFO ABCD ENTRY_EVAL route=dynamic_momentum_override "
                "final=T reason=ok relative_volume=1.8 spread_pct=0.4 signal_score=8.5"
            ),
            (
                "2026-07-01T09:36:10 INFO EFGH ENTRY_EVAL route=dynamic_momentum_override "
                "final=F reason=dynamic_vwap_extension relative_volume=0.9 spread_pct=0.8 signal_score=6.0"
            ),
            (
                "2026-07-01T09:37:00 INFO ALLOCATOR_ACTION_CREATED symbol=ABCD action=buy "
                "notional=1000.00 route=dynamic_momentum_override"
            ),
            (
                "2026-07-01T09:37:03 INFO ALLOCATOR_ACTION_SUBMITTED symbol=ABCD action=buy "
                "notional=1000.00 order_id=o1 route=dynamic_momentum_override"
            ),
            (
                "2026-07-01T09:37:05 INFO ORDER_SUBMITTED symbol=ABCD side=buy notional=1000.00 "
                "source=capital_allocator route=dynamic_momentum_override order_id=o1 status=accepted"
            ),
            "2026-07-01T09:37:10 INFO ORDER_FILLED symbol=ABCD side=buy filled_qty=50 order_id=o1 route=dynamic_momentum_override",
            (
                "2026-07-01T09:38:00 INFO ORDER_SKIP symbol=EFGH reason=weak_catalyst_dynamic_non_exceptional_live "
                "source=capital_allocator route=dynamic_momentum_override gain_pct=21.0 relative_volume=0.62 "
                "spread_pct=1.4 signal_score=5.5 news_score=0 catalyst_score=0.1 event_score=0 article_count=1"
            ),
            (
                "2026-07-01T09:38:01 INFO ALLOCATOR_DISPATCH_SKIPPED symbol=EFGH "
                "reason=weak_catalyst_dynamic_non_exceptional_live route=dynamic_momentum_override"
            ),
            "2026-07-01T09:39:00 INFO ORDER_CANCELLED symbol=ABCD order_id=o1 route=dynamic_momentum_override",
        ]
    )


def test_dynamic_funnel_report_sections_and_missed_opportunities() -> None:
    report = build_dynamic_funnel_report(
        project_root=PROJECT_ROOT,
        data_dir=PROJECT_ROOT / "data",
        day="2026-07-01",
        user_id="live_bot",
        log_text=_sample_log(),
    )

    assert report["scanner"]["total_scanned"] == 3
    assert report["scanner"]["accepted"] == 2
    assert report["scanner"]["rejected"] == 1
    assert report["scanner"]["rejection_counts_by_reason"] == {"unstable quote": 1}
    assert report["scanner"]["unstable_quote_retries"] == 2
    assert report["scanner"]["retry_successes"] == 1
    assert report["scanner"]["retry_failures"] == 1

    assert report["entry"]["entry_evaluations"] == 2
    assert report["entry"]["passed"] == 1
    assert report["entry"]["failed"] == 1
    assert report["entry"]["reasons"] == {"dynamic_vwap_extension": 1}

    assert report["allocator"]["actions_created"] == 1
    assert report["allocator"]["actions_dispatched"] == 1
    assert report["allocator"]["dispatch_skips"] == 2
    assert report["allocator"]["skip_reasons"]["weak_catalyst_dynamic_non_exceptional_live"] == 2

    assert report["execution"] == {"submitted": 1, "filled": 1, "cancelled": 1}
    assert report["weak_catalyst"]["candidates_blocked"] == 1
    assert report["weak_catalyst"]["symbols"] == ["EFGH"]
    assert report["weak_catalyst"]["average_RVOL"] == 0.62
    assert report["weak_catalyst"]["average_gain_pct"] == 21.0
    assert report["weak_catalyst"]["average_spread"] == 1.4
    assert report["weak_catalyst"]["average_signal_score"] == 5.5

    rows = {row["symbol"]: row for row in report["missed_opportunities"]}
    assert rows["NOISY"]["reason"] == "unstable quote"
    assert rows["NOISY"]["RVOL"] == 2.4
    assert rows["EFGH"]["reason"] == "weak_catalyst_dynamic_non_exceptional_live"
    assert rows["EFGH"]["article_count"] == 1.0

    text = render_dynamic_funnel_report(report)
    assert "# Dynamic Funnel Report 2026-07-01 user=live_bot" in text
    assert "## Missed Opportunity Table" in text
    assert "| EFGH | weak_catalyst_dynamic_non_exceptional_live |" in text
    assert "### Top 5 Rejection Reasons" in text


def test_dynamic_funnel_report_writes_artifacts_and_cli(tmp_path: Path) -> None:
    log_dir = tmp_path / "data" / "review" / "2026-07-01"
    log_dir.mkdir(parents=True)
    log_path = log_dir / "live_full.log"
    log_path.write_text(_sample_log(), encoding="utf-8")

    json_path, text_path, report = write_dynamic_funnel_report(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="2026-07-01",
        user_id="live_bot",
    )

    assert json_path == tmp_path / "data" / "research_metrics" / "2026-07-01" / "dynamic_funnel_live.json"
    assert text_path == tmp_path / "data" / "research_metrics" / "2026-07-01" / "dynamic_funnel_live.md"
    assert report["scanner"]["retry_successes"] == 1
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["allocator"]["actions_created"] == 1
    assert "Dynamic Funnel Report 2026-07-01" in text_path.read_text(encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_dynamic_funnel_report.py"),
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

    assert "Dynamic Funnel Report 2026-07-01 user=live_bot" in result.stdout
    assert "JSON:" in result.stdout
    assert "Markdown:" in result.stdout
