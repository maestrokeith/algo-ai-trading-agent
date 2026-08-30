from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from src.allocator_dispatch_mismatch_report import (
    build_allocator_dispatch_mismatch_report,
    latest_allocator_dispatch_mismatch_date,
    render_allocator_dispatch_mismatch_report,
    write_allocator_dispatch_mismatch_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_fixture(tmp_path: Path) -> Path:
    review_dir = tmp_path / "data" / "review" / "2026-06-12"
    review_dir.mkdir(parents=True)
    log_path = review_dir / "paper_full.log"
    log_path.write_text(
        "\n".join(
            [
                "Jun 12 10:00:00 host python[1]: INTC ENTRY_EVAL route=dynamic_momentum_override price=30.00 final=T reason=ok",
                (
                    "Jun 12 10:00:01 host python[1]: ENTRY_TO_ALLOCATOR_TRACE symbol=INTC "
                    "route=dynamic_momentum_override decision_present=true order_request_present=true ohlcv_present=true"
                ),
                "Jun 12 10:00:02 host python[1]: AMD ENTRY_EVAL route=dynamic_momentum_override price=160.00 final=T reason=ok",
                (
                    "Jun 12 10:00:03 host python[1]: ENTRY_TO_ALLOCATOR_TRACE symbol=AMD "
                    "route=dynamic_momentum_override decision_present=true order_request_present=true ohlcv_present=true"
                ),
                "Jun 12 10:00:04 host python[1]: IWM ENTRY_EVAL route=trend_long price=200.00 final=T reason=ok",
                (
                    "Jun 12 10:00:05 host python[1]: ENTRY_TO_ALLOCATOR_TRACE symbol=IWM "
                    "route=trend_long decision_present=true order_request_present=true ohlcv_present=true"
                ),
                "Jun 12 10:00:06 host python[1]: ranked: ['AMD', 'INTC']",
                "Jun 12 10:00:07 host python[1]: selected: ['AMD', 'INTC']",
                (
                    "Jun 12 10:00:08 host python[1]: SKIP INTC: reason=size = 0 "
                    "detail=trade_size $54.60 + buffer $5 < minimum_cash_to_deploy 3469 "
                    "available_cash=69000.00 gross_headroom=54.60 final_trade_size=54.60 "
                    "limiting_cap=gross_headroom source=dynamic_universe"
                ),
                (
                    "Jun 12 10:00:09 host python[1]: ALLOCATOR ACTIONS: "
                    "[{'action': 'buy', 'symbol': 'AMD', 'notional': 4625.39, 'source': 'dynamic_universe'}]"
                ),
                (
                    "Jun 12 10:00:10 host python[1]: ALLOCATOR_DISPATCH_START "
                    "symbol=AMD action=buy notional=4625.39 source=dynamic_universe"
                ),
                (
                    "Jun 12 10:00:10 host python[1]: DISPATCH_DYNAMIC_RVOL_CHECK symbol=AMD "
                    "route=dynamic_momentum_override source=dynamic_universe dynamic_candidate=true "
                    "rel_volume=0.882 base_min_rel_volume=1.000 effective_min_rel_volume=1.000 "
                    "override_active=false news_score=8.00 catalyst_score=0.90 event_score=7.00 "
                    "catalyst_type=news catalyst_age_minutes=12.00 scanner_effective_min_rel_volume=0.350 "
                    "entry_eval_route=dynamic_momentum_override decision_allowed=true "
                    "dispatch_result=skipped dispatch_reason=dynamic_relative_volume"
                ),
                (
                    "Jun 12 10:00:10 host python[1]: DISPATCH_DYNAMIC_RVOL_SKIP_DETAIL symbol=AMD "
                    "threshold_used=1.000 base_min_rel_volume=1.000 rel_volume=0.882 "
                    "override_active=false override_reason=not_applied missing_fields=none "
                    "dispatch_reason=dynamic_relative_volume"
                ),
                "Jun 12 10:00:11 host python[1]: ALLOCATOR_DISPATCH_SKIPPED symbol=AMD reason=dynamic_relative_volume",
                "Jun 12 10:00:12 host python[1]: ALLOCATOR_ORDER_INTENT symbol=IWM side=buy notional=1000.00 qty=5",
            ]
        ),
        encoding="utf-8",
    )
    return log_path


def test_allocator_dispatch_mismatch_report_explains_intc_and_amd(tmp_path: Path) -> None:
    _write_fixture(tmp_path)

    report = build_allocator_dispatch_mismatch_report(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="2026-06-12",
        user_id="paper_bot",
    )

    summary = report["summary"]
    assert summary["total_entry_eval_passed"] == 3
    assert summary["total_allocator_reached"] == 3
    assert summary["total_allocator_selected"] == 2
    assert summary["total_dispatch_started"] == 1
    assert summary["total_dispatch_skipped"] == 1
    assert summary["dispatch_skip_reasons"] == {"dynamic_relative_volume": 1}
    assert summary["mismatch_counts_by_category"]["allocator_selected_dispatch_skipped"] == 1
    assert summary["mismatch_counts_by_category"]["entry_eval_passed_allocator_min_cash_skip"] == 1
    assert summary["mismatch_counts_by_category"]["scanner_override_not_honored_at_dispatch"] == 1
    assert summary["mismatch_counts_by_category"]["selected_size_clipped_below_min_cash"] == 1

    rows = {row["symbol"]: row for row in report["candidates"]}
    intc = rows["INTC"]
    assert intc["route"] == "dynamic_momentum_override"
    assert intc["dynamic_candidate"] is True
    assert intc["allocator_selected"] is True
    assert intc["trade_size"] == 54.6
    assert intc["minimum_cash_to_deploy"] == 3469.0
    assert intc["gross_headroom"] == 54.6
    assert intc["available_cash"] == 69000.0
    assert intc["limiting_cap"] == "gross_headroom"
    assert intc["final_pipeline_outcome"] == "allocator_min_cash_skip"
    assert "selected_size_clipped_below_min_cash" in intc["mismatch_categories"]
    assert "limiting_cap=gross_headroom" in report["explanations"]["INTC"]

    amd = rows["AMD"]
    assert amd["source"] == "dynamic_universe"
    assert amd["proposed_notional"] == 4625.39
    assert amd["dispatch_result"] == "skipped"
    assert amd["dispatch_skip_reason"] == "dynamic_relative_volume"
    assert amd["final_pipeline_outcome"] == "dispatch_skipped:dynamic_relative_volume"
    assert "scanner_override_not_honored_at_dispatch" in amd["mismatch_categories"]
    assert amd["dispatch_dynamic_rvol_check"]["news_score"] == 8.0
    assert amd["dispatch_dynamic_rvol_check"]["catalyst_score"] == 0.9
    assert amd["dispatch_dynamic_rvol_check"]["scanner_effective_min_rel_volume"] == 0.35
    assert amd["dispatch_dynamic_skip_detail"]["threshold_used"] == 1.0
    assert report["dynamic_rvol_consistency"]["dispatch_rechecked_dynamic_relative_volume"] is True
    assert report["dynamic_rvol_consistency"]["affected_symbols"] == ["AMD"]
    assert report["dynamic_rvol_consistency"]["metadata_by_symbol"]["AMD"]["received_news_score"] is True
    assert report["dynamic_rvol_consistency"]["metadata_by_symbol"]["AMD"]["received_catalyst_score"] is True
    assert report["dynamic_rvol_consistency"]["metadata_by_symbol"]["AMD"]["received_effective_min_rel_volume"] is True
    assert (
        report["dynamic_rvol_consistency"]["metadata_by_symbol"]["AMD"]["applied_same_rvol_override_as_scanner"]
        is False
    )

    text = render_allocator_dispatch_mismatch_report(report)
    assert "INTC passed entry evaluation" in text
    assert "AMD was selected by allocator" in text
    assert "Dispatch Metadata by Symbol" in text


def test_allocator_dispatch_mismatch_report_writes_artifacts_and_cli(tmp_path: Path) -> None:
    _write_fixture(tmp_path)

    assert (
        latest_allocator_dispatch_mismatch_date(
            project_root=tmp_path,
            data_dir=tmp_path / "data",
            user_id="paper_bot",
        )
        == "2026-06-12"
    )

    json_path, text_path, report = write_allocator_dispatch_mismatch_report(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="latest",
        user_id="paper_bot",
    )
    assert json_path.exists()
    assert text_path.exists()
    assert report["date"] == "2026-06-12"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["summary"]["total_dispatch_skipped"] == 1

    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_allocator_dispatch_mismatch_report.py"),
            "--date",
            "2026-06-12",
            "--user",
            "paper_bot",
            "--project-root",
            str(tmp_path),
            "--data-dir",
            str(tmp_path / "data"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Allocator Dispatch Mismatch Report - 2026-06-12 user=paper_bot" in result.stdout
    assert "JSON:" in result.stdout
