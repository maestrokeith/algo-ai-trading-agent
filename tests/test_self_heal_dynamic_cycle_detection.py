from __future__ import annotations

from scripts import run_self_heal


def test_dynamic_scan_open_cycle_does_not_create_issue() -> None:
    logs = "\n".join(
        [
            "INFO DYNAMIC_SCAN selected=['EHGO', 'WEN', 'ABSI']",
            "INFO DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=EHGO source=scanner_selected",
            "INFO DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=WEN source=scanner_selected",
            "INFO DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=ABSI source=scanner_selected",
        ]
    )

    evidence = run_self_heal.detect_failure(logs, "LIVE", "30 minutes ago")

    assert evidence is None


def test_dynamic_scan_closed_cycle_still_reports_missing_eval() -> None:
    logs = "\n".join(
        [
            "2026-06-24T10:00:00 INFO DYNAMIC_SCAN selected=['EHGO', 'WEN', 'ABSI']",
            "2026-06-24T10:00:01 INFO DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=EHGO source=scanner_selected",
            "2026-06-24T10:00:01 INFO DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=WEN source=scanner_selected",
            "2026-06-24T10:00:01 INFO DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=ABSI source=scanner_selected",
            "2026-06-24T10:04:00 INFO ENTRY_LANE_DECISION user=live_bot now_et=10:04 entries_on=true entry_scan_allowed=true",
        ]
    )

    evidence = run_self_heal.detect_failure(logs, "LIVE", "30 minutes ago")

    assert evidence is not None
    assert "ABSI" in evidence.actual_missing_step
