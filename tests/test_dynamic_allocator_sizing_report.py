from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from src.dynamic_allocator_sizing_report import (
    build_dynamic_allocator_sizing_report,
    latest_dynamic_allocator_sizing_date,
    render_dynamic_allocator_sizing_report,
    write_dynamic_allocator_sizing_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_fixture(tmp_path: Path) -> Path:
    review_dir = tmp_path / "data" / "review" / "2026-06-12"
    review_dir.mkdir(parents=True)
    log_path = review_dir / "paper_full.log"
    log_path.write_text(
        "\n".join(
            [
                "Jun 12 10:00:00 host python[1]: INTC ENTRY_EVAL route=dynamic_momentum_override final=T reason=ok",
                (
                    "Jun 12 10:00:01 host python[1]: ENTRY_TO_ALLOCATOR_TRACE symbol=INTC "
                    "route=dynamic_momentum_override decision_present=true order_request_present=true ohlcv_present=true"
                ),
                "Jun 12 10:00:02 host python[1]: AMD ENTRY_EVAL route=dynamic_momentum_override final=T reason=ok",
                (
                    "Jun 12 10:00:03 host python[1]: ENTRY_TO_ALLOCATOR_TRACE symbol=AMD "
                    "route=dynamic_momentum_override decision_present=true order_request_present=true ohlcv_present=true"
                ),
                "Jun 12 10:00:04 host python[1]: AKTS ENTRY_EVAL route=dynamic_momentum_override final=T reason=ok",
                (
                    "Jun 12 10:00:05 host python[1]: ENTRY_TO_ALLOCATOR_TRACE symbol=AKTS "
                    "route=dynamic_momentum_override decision_present=true order_request_present=true ohlcv_present=true"
                ),
                "Jun 12 10:00:06 host python[1]: IWM ENTRY_EVAL route=trend_long final=T reason=ok",
                (
                    "Jun 12 10:00:07 host python[1]: ENTRY_TO_ALLOCATOR_TRACE symbol=IWM "
                    "route=trend_long decision_present=true order_request_present=true ohlcv_present=true"
                ),
                "Jun 12 10:00:08 host python[1]: ranked: ['INTC', 'AMD', 'AKTS', 'IWM']",
                "Jun 12 10:00:09 host python[1]: selected: ['INTC']",
                (
                    "Jun 12 10:00:09 host python[1]: ALLOCATOR_SIZE_TRACE symbol=INTC "
                    "route=dynamic_momentum_override source=dynamic_universe dynamic_candidate=true "
                    "candidate_rank=0 score=9.500000 strength=9.500000 account_equity=100000.00 "
                    "available_cash=69000.00 gross_headroom=50000.00 raw_target_notional=5000.00 "
                    "target_pct=0.050000 dynamic_sleeve_cap=12000.00 core_sleeve_cap=88000.00 "
                    "sector_cap_remaining=90000.00 symbol_cap_remaining=25000.00 "
                    "position_cap_remaining=25000.00 per_trade_cap=1312.50 max_trade_size=1312.50 "
                    "after_sleeve_cap=1312.50 after_sector_cap=1312.50 after_symbol_cap=1312.50 "
                    "after_position_cap=1312.50 after_gross_headroom=1312.50 final_trade_size=1312.50 "
                    "minimum_cash_to_deploy=3469.00 min_realloc_leg=300.00 "
                    "skipped_by_min_deploy=true skip_reason=minimum_cash_to_deploy"
                ),
                (
                    "Jun 12 10:00:10 host python[1]: ALLOCATOR_NO_ACTION_DETAIL symbol=INTC reason=size = 0 "
                    "detail=trade_size $1312.50 + buffer $5 < minimum_cash_to_deploy 3469 "
                    "target_allocation=25000.00 available_cash=69000.00 cash_reserve=0.00 "
                    "current_dynamic_sleeve_usage=4000.00 dynamic_sleeve_cap=12000.00 "
                    "candidate_notional_requested=1312.50 candidate_notional=1312.50 tranche_min=1312.50 "
                    "candidate_requested_notional=1312.50 candidate_notional_cap=5000.00 "
                    "base_requested_notional=1312.50 final_trade_size=1312.50 limiting_cap=none "
                    "min_order_notional=300.00 max_single_dynamic_notional=25000.00 "
                    "position_already_held=False rebalance_deploy_mode=normal "
                    "rebalance_fund_from_weakest=False max_positions=5 weakest_symbol=n/a "
                    "weakest_score=n/a weakest_value=n/a source=dynamic_universe score=9.5"
                ),
                (
                    "Jun 12 10:00:11 host python[1]: ALLOCATOR_NO_ACTION_DETAIL symbol=AMD reason=size = 0 "
                    "detail=trade_size $900.00 + buffer $5 < minimum_cash_to_deploy 3469 "
                    "target_allocation=25000.00 available_cash=69000.00 cash_reserve=0.00 "
                    "current_dynamic_sleeve_usage=4000.00 dynamic_sleeve_cap=12000.00 "
                    "candidate_notional_requested=1500.00 candidate_notional=900.00 tranche_min=1312.50 "
                    "candidate_requested_notional=1500.00 candidate_notional_cap=900.00 "
                    "base_requested_notional=1500.00 final_trade_size=900.00 limiting_cap=candidate_notional_cap "
                    "min_order_notional=300.00 max_single_dynamic_notional=25000.00 "
                    "position_already_held=False rebalance_deploy_mode=normal "
                    "rebalance_fund_from_weakest=False max_positions=5 weakest_symbol=n/a "
                    "weakest_score=n/a weakest_value=n/a source=dynamic_universe score=8.5"
                ),
                (
                    "Jun 12 10:00:12 host python[1]: SKIP AKTS: reason=size = 0 "
                    "detail=trade_size $500.00 + buffer $5 < minimum_cash_to_deploy 3469"
                ),
                (
                    "Jun 12 10:00:13 host python[1]: ALLOCATOR_NO_ACTION_DETAIL symbol=IWM reason=size = 0 "
                    "detail=trade_size $1312.50 + buffer $5 < minimum_cash_to_deploy 3469 "
                    "candidate_notional_requested=1312.50 candidate_notional=1312.50 "
                    "base_requested_notional=1312.50 final_trade_size=1312.50 "
                    "limiting_cap=none min_order_notional=300.00 source=trend_long score=4.0"
                ),
            ]
        ),
        encoding="utf-8",
    )
    return log_path


def test_dynamic_allocator_sizing_report_explains_intc_min_deploy(tmp_path: Path) -> None:
    _write_fixture(tmp_path)

    report = build_dynamic_allocator_sizing_report(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="2026-06-12",
        user_id="paper_bot",
    )

    summary = report["summary"]
    assert summary["dynamic_candidates_reaching_allocator"] == 3
    assert summary["skipped_by_min_deploy_floor"] == 3
    assert summary["average_trade_size"] == 904.1667
    assert summary["median_trade_size"] == 900.0
    assert summary["average_minimum_cash_to_deploy"] == 3469.0
    assert summary["most_common_clipping_source"] == "minimum_cash_to_deploy_floor_check"

    rows = {row["symbol"]: row for row in report["dynamic_candidates"]}
    intc = rows["INTC"]
    assert intc["entry_eval_final"] is True
    assert intc["ranked"] is True
    assert intc["selected"] is True
    assert intc["raw_desired_notional"] == 1312.5
    assert intc["final_trade_size"] == 1312.5
    assert intc["minimum_cash_to_deploy"] == 3469.0
    assert intc["available_cash"] == 69000.0
    assert intc["candidate_rank"] == 0
    assert intc["account_equity"] == 100000.0
    assert intc["raw_target_notional"] == 5000.0
    assert intc["target_pct"] == 0.05
    assert intc["after_sleeve_cap"] == 1312.5
    assert intc["after_gross_headroom"] == 1312.5
    assert intc["position_cap"] == 25000.0
    assert intc["per_trade_cap"] == 1312.5
    assert intc["sleeve_allocation_cap"] == 12000.0
    assert intc["sizing_formula_inference"] == "dynamic_sleeve_cap"
    assert intc["first_clipping_stage"] == "dynamic_sleeve_cap"
    assert intc["first_below_min_deploy_stage"] == "dynamic_sleeve_cap"
    assert "dynamic_sleeve_cap" in intc["clipping_steps_detected"]
    assert "minimum_cash_to_deploy_floor_check" in intc["clipping_steps_detected"]
    assert intc["final_skip_reason"] == "minimum_cash_to_deploy"
    assert intc["trade_size_to_minimum_cash_ratio"] == 0.3784

    amd = rows["AMD"]
    assert amd["sizing_formula_inference"] == "candidate_notional_cap"
    assert "candidate_notional_cap" in amd["clipping_steps_detected"]

    assert report["focus_symbols"]["IWM"]["dynamic_candidate"] is False
    assert "INTC final_trade_size=1312.5" in report["explanations"]["INTC"]

    what_if = report["what_if"]
    assert what_if["current_behavior"]["would_clear_count"] == 0
    assert what_if["lower_minimum_cash_to_deploy_75pct"]["would_clear_symbols"] == ["AMD", "INTC"]
    assert what_if["min_realloc_leg_only"]["would_clear_count"] == 3
    assert what_if["no_minimum_deployment_floor"]["would_clear_count"] == 3
    assert (
        what_if["raise_dynamic_per_trade_target_enough_to_clear_floor"]["minimum_extra_trade_size_needed"]
        == 2964.0
    )

    text = render_dynamic_allocator_sizing_report(report)
    assert "Dynamic Allocator Sizing Report - 2026-06-12 user=paper_bot" in text
    assert "first_clip=dynamic_sleeve_cap" in text


def test_dynamic_allocator_sizing_report_writes_artifacts_and_cli(tmp_path: Path) -> None:
    _write_fixture(tmp_path)

    assert (
        latest_dynamic_allocator_sizing_date(
            project_root=tmp_path,
            data_dir=tmp_path / "data",
            user_id="paper_bot",
        )
        == "2026-06-12"
    )
    json_path, text_path, report = write_dynamic_allocator_sizing_report(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="latest",
        user_id="paper_bot",
    )
    assert json_path.exists()
    assert text_path.exists()
    assert report["date"] == "2026-06-12"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["summary"]["dynamic_candidates_reaching_allocator"] == 3

    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_dynamic_allocator_sizing_report.py"),
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
    assert "Dynamic Allocator Sizing Report - 2026-06-12 user=paper_bot" in result.stdout
    assert "JSON:" in result.stdout
