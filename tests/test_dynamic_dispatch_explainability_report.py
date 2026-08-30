from __future__ import annotations

import subprocess
from pathlib import Path

from src.dynamic_dispatch_explainability_report import (
    build_dynamic_dispatch_explainability_report,
    classify_dispatch_rule,
    render_dynamic_dispatch_explainability_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_fixture(tmp_path: Path) -> Path:
    log_path = tmp_path / "dispatch.log"
    log_path.write_text(
        "\n".join(
            [
                (
                    "Jul 07 10:00:00 host algo[1]: ALLOCATOR ACTIONS: "
                    "[{'action': 'buy', 'symbol': 'FHTX', 'notional': 1200.0, "
                    "'source': 'capital_allocator', 'route': 'dynamic_momentum_override', "
                    "'scanner_score': 0.91, 'dynamic_score': 0.88, 'gain_pct': 14.2, "
                    "'day_gain_pct': 14.2, 'relative_volume': 2.1, 'news_score': 0.0, "
                    "'catalyst_score': 0.1, 'catalyst_fastlane_active': False, "
                    "'weak_catalyst_dynamic': True, 'entry_eval_final': True}, "
                    "{'action': 'buy', 'symbol': 'MSFT', 'notional': 736.0, "
                    "'source': 'capital_allocator', 'route': 'news_catalyst', "
                    "'scanner_score': 0.95, 'dynamic_score': 0.93, 'gain_pct': 3.2, "
                    "'relative_volume': 3.5, 'news_score': 0.9, 'catalyst_score': 0.95, "
                    "'catalyst_fastlane_active': True, 'weak_catalyst_dynamic': False, "
                    "'entry_eval_final': True}, "
                    "{'action': 'buy', 'symbol': 'CORE', 'notional': 1500.0, "
                    "'source': 'capital_allocator', 'route': 'trend_long', 'entry_eval_final': True}]"
                ),
                (
                    "Jul 07 10:00:01 host algo[1]: ALLOCATOR_DISPATCH_START "
                    "symbol=FHTX action=buy notional=1200.00 source=capital_allocator"
                ),
                (
                    "Jul 07 10:00:02 host algo[1]: ORDER_SKIP symbol=FHTX "
                    "reason=weak_catalyst_dynamic_non_exceptional_live source=capital_allocator"
                ),
                (
                    "Jul 07 10:00:02 host algo[1]: ALLOCATOR_DISPATCH_END "
                    "symbol=FHTX result=skipped reason=weak_catalyst_dynamic_non_exceptional_live"
                ),
                (
                    "Jul 07 10:01:01 host algo[1]: ALLOCATOR_DISPATCH_START "
                    "symbol=MSFT action=buy notional=736.00 source=capital_allocator"
                ),
                (
                    "Jul 07 10:01:02 host algo[1]: DYNAMIC_DISPATCH_EXPLAINABILITY "
                    "symbol=MSFT source=capital_allocator route=news_catalyst notional=736.00 "
                    "scanner_score=0.95 dynamic_score=0.93 gain_pct=3.2 day_gain_pct=3.2 "
                    "relative_volume=3.5 news_score=0.9 catalyst_score=0.95 "
                    "catalyst_fastlane_active=true weak_catalyst_dynamic=false "
                    "entry_eval_final=true dispatcher_result=skipped "
                    "dispatcher_skip_reason=dynamic_price_below_minimum rule_class=safety_rule"
                ),
                (
                    "Jul 07 10:01:03 host algo[1]: ORDER_SKIP symbol=MSFT "
                    "reason=dynamic_price_below_minimum source=capital_allocator"
                ),
                (
                    "Jul 07 10:01:04 host algo[1]: ALLOCATOR_DISPATCH_END "
                    "symbol=MSFT result=skipped reason=dynamic_price_below_minimum"
                ),
                (
                    "Jul 07 10:02:01 host algo[1]: ALLOCATOR_DISPATCH_START "
                    "symbol=CORE action=buy notional=1500.00 source=capital_allocator"
                ),
                (
                    "Jul 07 10:02:02 host algo[1]: ORDER_SUBMITTED symbol=CORE side=buy "
                    "notional=1500.00 source=capital_allocator order_id=abc123 status=accepted"
                ),
                (
                    "Jul 07 10:02:03 host algo[1]: ALLOCATOR_DISPATCH_END "
                    "symbol=CORE result=submitted reason=submitted"
                ),
            ]
        ),
        encoding="utf-8",
    )
    return log_path


def test_parses_order_skip_and_joins_allocator_action_context(tmp_path: Path) -> None:
    log_path = _write_fixture(tmp_path)

    report = build_dynamic_dispatch_explainability_report(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="2026-07-07",
        user_id="live_bot",
        log_paths=[log_path],
    )

    rows = {row["symbol"]: row for row in report["rows"]}
    fhtx = rows["FHTX"]
    assert fhtx["allocator_action"] is True
    assert fhtx["dispatcher_result"] == "skipped"
    assert fhtx["dispatcher_skip_reason"] == "weak_catalyst_dynamic_non_exceptional_live"
    assert fhtx["rule_class"] == "strategy_rule"
    assert fhtx["notional"] == 1200.0
    assert fhtx["route"] == "dynamic_momentum_override"
    assert fhtx["scanner_score"] == 0.91
    assert fhtx["weak_catalyst_dynamic"] is True
    assert fhtx["entry_eval_final"] is True
    assert fhtx["missed_opportunity"] == {
        "plus_5m": None,
        "plus_15m": None,
        "plus_30m": None,
        "end_of_day": None,
    }


def test_classifies_dispatcher_rules() -> None:
    assert classify_dispatch_rule("weak_catalyst_dynamic_non_exceptional_live") == "strategy_rule"
    assert classify_dispatch_rule("dynamic_price_below_minimum") == "safety_rule"
    assert classify_dispatch_rule("dynamic_spread_cap") == "safety_rule"
    assert classify_dispatch_rule("bad_quote") == "safety_rule"


def test_report_summary_counts_and_blocker_groups(tmp_path: Path) -> None:
    log_path = _write_fixture(tmp_path)

    report = build_dynamic_dispatch_explainability_report(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="2026-07-07",
        user_id="live_bot",
        log_paths=[log_path],
    )

    summary = report["summary"]
    assert summary["total_allocator_actions"] == 3
    assert summary["submitted_orders"] == 1
    assert summary["skipped_or_blocked_orders"] == 2
    assert summary["top_dispatcher_skip_reasons"] == {
        "weak_catalyst_dynamic_non_exceptional_live": 1,
        "dynamic_price_below_minimum": 1,
    }
    assert summary["symbols_most_frequently_blocked_after_allocation"] == {"FHTX": 1, "MSFT": 1}
    assert summary["high_score_dynamic_blocks"] == 2
    assert summary["catalyst_fastlane_blocks"] == 1
    assert summary["weak_catalyst_dynamic_blocks"] == 1
    assert report["rules_blocking_weak_catalyst_dynamic_candidates"] == [
        {
            "symbol": "FHTX",
            "reason": "weak_catalyst_dynamic_non_exceptional_live",
            "rule_class": "strategy_rule",
        }
    ]

    text = render_dynamic_dispatch_explainability_report(report)
    assert "total_allocator_actions: 3" in text
    assert "dispatcher_skip_reason=weak_catalyst_dynamic_non_exceptional_live" in text
    assert "rule_class=strategy_rule" in text


def test_cli_command_works_from_bin_algo(tmp_path: Path) -> None:
    log_path = _write_fixture(tmp_path)

    result = subprocess.run(
        [
            str(PROJECT_ROOT / "bin" / "algo"),
            "dynamic-dispatch-explainability-report",
            "--date",
            "2026-07-07",
            "--user",
            "live_bot",
            "--project-root",
            str(tmp_path),
            "--data-dir",
            str(tmp_path / "data"),
            "--log-file",
            str(log_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "DYNAMIC DISPATCH EXPLAINABILITY REPORT" in result.stdout
    assert "total_allocator_actions: 3" in result.stdout
    assert "JSON:" in result.stdout
