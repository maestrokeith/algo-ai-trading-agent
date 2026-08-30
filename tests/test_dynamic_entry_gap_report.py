from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from src.dynamic_entry_gap_report import (
    build_dynamic_entry_gap_report,
    latest_dynamic_entry_gap_date,
    render_dynamic_entry_gap_report,
    write_dynamic_entry_gap_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_fixture(tmp_path: Path) -> tuple[Path, Path]:
    data_dir = tmp_path / "data"
    history_dir = data_dir / "dynamic_scan_history"
    review_dir = data_dir / "review" / "2026-06-12"
    history_dir.mkdir(parents=True)
    review_dir.mkdir(parents=True)
    selected = [
        {"symbol": "RKLZ", "score": 91.0},
        {"symbol": "DSY", "score": 83.0},
        {"symbol": "INTC", "score": 80.0},
        {"symbol": "NOK", "score": 77.0},
        {"symbol": "RZLV", "score": 74.0},
        {"symbol": "ASTN", "score": 95.0},
        {"symbol": "CIIT", "score": 89.0},
    ]
    payload = {
        "user_id": "default",
        "generated_at": "2026-06-12T14:00:00+00:00",
        "accepted": selected,
        "selected": selected,
    }
    (history_dir / "20260612T140000000000Z_default.json").write_text(json.dumps(payload), encoding="utf-8")
    log_path = review_dir / "paper_full.log"
    log_path.write_text(
        "\n".join(
            [
                "Jun 12 10:00:00 host python[1]: DYNAMIC_SELECTED symbol=RKLZ score=91.00 news_score=0",
                "Jun 12 10:01:00 host python[1]: DYNAMIC_SELECTED symbol=DSY score=83.00 news_score=0",
                "Jun 12 10:02:00 host python[1]: DYNAMIC_SELECTED symbol=INTC score=80.00 news_score=0",
                "Jun 12 10:03:00 host python[1]: DYNAMIC_SELECTED symbol=NOK score=77.00 news_score=0",
                "Jun 12 10:04:00 host python[1]: DYNAMIC_SELECTED symbol=RZLV score=74.00 news_score=0",
                "Jun 12 10:05:00 host python[1]: DYNAMIC_SELECTED symbol=ASTN score=95.00 news_score=0",
                "Jun 12 10:06:00 host python[1]: DYNAMIC_SELECTED symbol=CIIT score=89.00 news_score=0",
                "Jun 12 10:07:00 host python[1]: DYNAMIC_UNIVERSE: base=20 added=['RKLZ','DSY','INTC','NOK','RZLV','ASTN','CIIT'] total=27",
                "Jun 12 10:08:00 host python[1]: DYNAMIC_SELECTED_DROPPED symbol=RKLZ reason=not_in_scoring_top_n_candidates",
                "Jun 12 10:08:01 host python[1]: DYNAMIC_SELECTED_ENTRY_SKIPPED symbol=RKLZ reason=not_in_scoring_top_n_candidates",
                "Jun 12 10:09:00 host python[1]: DYNAMIC_HIGH_CONVICTION_TREND_PREFILTER_BLOCKED symbol=DSY reason=below_ma score=83.00 catalyst_score=0.00 event_score=0.00 news_score=0.00 rvol=2.10 sentiment=0.00 age_minutes=n/a",
                "Jun 12 10:10:00 host python[1]: DYNAMIC_NOT_TRADABLE symbol=INTC reason=not enough bars (got 87, need 200) detail=insufficient_history news_score=0.00 event_score=0.00 catalyst_score=0.00",
                "Jun 12 10:10:01 host python[1]: DYNAMIC_SELECTED_DROPPED symbol=INTC reason=short_history",
                "Jun 12 10:11:00 host python[1]: DYNAMIC_SELECTED_ENTRY_SKIPPED symbol=NOK reason=cooldown",
                "Jun 12 10:12:00 host python[1]: DYNAMIC_SELECTED_ENTRY_TRACE symbol=ASTN in_universe=true will_evaluate=true reason=ok",
                "Jun 12 10:12:01 host python[1]: ASTN ENTRY_EVAL route=dynamic_momentum final=F reason=ATR volatility cap",
                "Jun 12 10:13:00 host python[1]: DYNAMIC_SELECTED_ENTRY_TRACE symbol=CIIT in_universe=true will_evaluate=true reason=ok",
                "Jun 12 10:13:01 host python[1]: CIIT ENTRY_EVAL route=dynamic_momentum final=F reason=ATR volatility cap",
            ]
        ),
        encoding="utf-8",
    )
    return data_dir, log_path


def test_dynamic_entry_gap_report_classifies_pre_entry_gaps(tmp_path: Path) -> None:
    data_dir, log_path = _write_fixture(tmp_path)

    report = build_dynamic_entry_gap_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-06-12",
        user_id="paper_bot",
        log_paths=[log_path],
    )

    assert report["research_only"] is True
    assert report["summary"]["selected_dynamic_symbols"] == 7
    assert report["summary"]["symbols_added_to_dynamic_universe"] == 7
    assert report["summary"]["symbols_reaching_entry_eval"] == 2
    assert report["summary"]["symbols_lost_before_entry_eval"] == 5
    assert report["summary"]["drop_counts_by_inferred_reason"]["not_in_scoring_top_n_candidates"] == 1
    assert report["summary"]["drop_counts_by_inferred_reason"]["below_ma"] == 1
    assert report["summary"]["drop_counts_by_inferred_reason"]["short_history"] == 1
    assert report["summary"]["drop_counts_by_inferred_reason"]["cooldown"] == 1
    assert report["summary"]["drop_counts_by_inferred_reason"]["missing_logging_after_dynamic_universe_added"] == 1

    focus = report["focus_symbols"]
    assert focus["RKLZ"]["removed_by_top_n_scoring"] is True
    assert focus["RKLZ"]["dynamic_symbols_not_in_scoring_candidate_set"] is True
    assert focus["DSY"]["skipped_by_trend_prefilter"] is True
    assert focus["INTC"]["skipped_by_history_bars"] is True
    assert focus["NOK"]["skipped_by_cooldown_or_position"] is True
    assert focus["RZLV"]["final_inferred_reason"] == "missing_logging_after_dynamic_universe_added"
    assert focus["ASTN"]["reached_entry_eval"] is True
    assert focus["CIIT"]["entry_eval_reject_reason"] == "ATR volatility cap"


def test_dynamic_entry_gap_report_writes_artifacts_and_cli(tmp_path: Path) -> None:
    data_dir, log_path = _write_fixture(tmp_path)

    assert latest_dynamic_entry_gap_date(data_dir=data_dir, user_id="paper_bot") == "2026-06-12"
    json_path, text_path, report = write_dynamic_entry_gap_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="latest",
        user_id="paper_bot",
        log_paths=[log_path],
    )

    assert json_path == data_dir / "research" / "dynamic_entry_gap" / "2026-06-12_paper_bot.json"
    assert text_path == data_dir / "research" / "dynamic_entry_gap" / "2026-06-12_paper_bot.txt"
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved["report"] == "dynamic_entry_gap"
    text = text_path.read_text(encoding="utf-8")
    assert "Dynamic Entry Gap Report - 2026-06-12 user=paper_bot" in text
    assert "Instrumentation Recommendations" in render_dynamic_entry_gap_report(report)

    proc = subprocess.run(
        [
            str(PROJECT_ROOT / "bin" / "algo"),
            "dynamic-entry-gap-report",
            "--date",
            "2026-06-12",
            "--user",
            "paper_bot",
            "--data-dir",
            str(data_dir),
            "--project-root",
            str(tmp_path),
            "--log-path",
            str(log_path),
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "Dynamic Entry Gap Report - 2026-06-12 user=paper_bot" in proc.stdout
    assert "JSON:" in proc.stdout


def test_dynamic_entry_gap_report_uses_existing_conversion_artifact(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    artifact_dir = data_dir / "research" / "dynamic_conversion"
    review_dir = data_dir / "review" / "2026-06-12"
    artifact_dir.mkdir(parents=True)
    review_dir.mkdir(parents=True)
    (artifact_dir / "2026-06-12_paper_bot.json").write_text(
        json.dumps(
            {
                "report": "dynamic_conversion",
                "date": "2026-06-12",
                "user_id": "paper_bot",
                "symbols": [
                    {
                        "symbol": "RKLZ",
                        "selection_count": 2,
                        "dynamic_score": 91.0,
                        "appeared_in_dynamic_universe": True,
                        "reached_entry_eval": False,
                        "events": [
                            {"stage": "DYNAMIC_SELECTED", "timestamp": "2026-06-12T10:00:00-04:00"},
                            {"stage": "DYNAMIC_UNIVERSE_ADDED", "timestamp": "2026-06-12T10:01:00-04:00"},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    log_path = review_dir / "paper_full.log"
    log_path.write_text(
        "Jun 12 10:02:00 host python[1]: DYNAMIC_SELECTED_ENTRY_SKIPPED symbol=RKLZ reason=not_in_scoring_top_n_candidates\n",
        encoding="utf-8",
    )

    report = build_dynamic_entry_gap_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="latest",
        user_id="paper_bot",
        log_paths=[log_path],
    )

    assert report["source_files"]["conversion_report_source"] == "existing_dynamic_conversion_artifact"
    assert report["focus_symbols"]["RKLZ"]["removed_by_top_n_scoring"] is True
