from __future__ import annotations

from datetime import datetime

import pytest

from scripts import run_self_heal


def _logs(*lines: str) -> str:
    return "\n".join(lines)


@pytest.mark.parametrize(
    ("name", "terminal_line", "expected_state"),
    [
        (
            "normal accepted trade",
            "2026-06-24T10:00:04 INFO ENTRY_EVAL_PASS symbol=WEN route=dynamic_momentum_override",
            "entry_eval_completed",
        ),
        (
            "rejected at entry evaluation",
            "2026-06-24T10:00:04 INFO DYNAMIC_ENTRY_EVAL_DROPPED symbol=WEN reason=alignment",
            "entry_eval_rejected",
        ),
        (
            "rejected for short history",
            "2026-06-24T10:00:04 INFO DYNAMIC_ENTRY_CANDIDATE_SKIPPED symbol=WEN reason=short_history",
            "short_history",
        ),
        (
            "rejected for spread",
            "2026-06-24T10:00:04 INFO DYNAMIC_ENTRY_CANDIDATE_SKIPPED symbol=WEN reason=spread_too_wide",
            "spread_too_wide",
        ),
        (
            "rejected for unstable quote",
            "2026-06-24T10:00:04 INFO DYNAMIC_ENTRY_CANDIDATE_SKIPPED symbol=WEN reason=unstable_quote",
            "unstable_quote",
        ),
        (
            "rejected at allocator",
            "2026-06-24T10:00:04 INFO ALLOCATOR_REJECT WEN reason=no_catalyst",
            "allocator_rejected",
        ),
        (
            "rejected at dispatch",
            "2026-06-24T10:00:04 INFO ALLOCATOR_DISPATCH_SKIPPED symbol=WEN reason=dynamic_relative_volume",
            "dispatch_rejected",
        ),
        (
            "successful order submission",
            "2026-06-24T10:00:04 INFO ORDER_SUBMITTED symbol=WEN side=buy",
            "order_submitted",
        ),
    ],
)
def test_legitimate_dynamic_terminal_states_do_not_trigger_self_heal(
    name: str,
    terminal_line: str,
    expected_state: str,
) -> None:
    del name
    status = run_self_heal._dynamic_entry_flow_status(
        _logs(
            "2026-06-24T10:00:00 INFO DYNAMIC_SCAN selected=['WEN']",
            "2026-06-24T10:00:01 INFO DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=WEN source=scanner_selected",
            terminal_line,
        ).splitlines()
    )

    assert status.status == "observed"
    assert status.symbols == ("WEN",)
    assert any(f"terminal_state={expected_state}" in row for row in status.states)


def test_missing_terminal_state_triggers_self_heal() -> None:
    logs = _logs(
        "2026-06-24T10:00:00 INFO DYNAMIC_SCAN selected=['WEN']",
        "2026-06-24T10:00:01 INFO DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=WEN source=scanner_selected",
        "2026-06-24T10:04:30 INFO heartbeat healthy",
    )

    evidence = run_self_heal.detect_failure(logs, "LIVE", "30 minutes ago")

    assert evidence is not None
    assert evidence.short_failure == "missing_terminal_state"
    assert "WEN" in evidence.actual_missing_step


def test_mixed_legitimate_terminal_states_do_not_trigger_self_heal() -> None:
    logs = _logs(
        "2026-06-24T10:00:00 INFO DYNAMIC_SCAN selected=['RUN', 'WEN', 'ABSI']",
        "2026-06-24T10:00:01 INFO DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=RUN source=scanner_selected",
        "2026-06-24T10:00:01 INFO DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=WEN source=scanner_selected",
        "2026-06-24T10:00:01 INFO DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=ABSI source=scanner_selected",
        "2026-06-24T10:00:04 INFO DYNAMIC_ENTRY_CANDIDATE_SKIPPED symbol=RUN reason=short_history",
        "2026-06-24T10:00:05 INFO ALLOCATOR_DISPATCH_SKIPPED symbol=WEN reason=dynamic_relative_volume",
        "2026-06-24T10:00:06 INFO ORDER_SUBMITTED symbol=ABSI side=buy",
        "2026-06-24T10:00:07 INFO ORDER_STATUS symbol=ABSI status=accepted",
    )

    assert run_self_heal.detect_failure(logs, "LIVE", "30 minutes ago") is None


def test_stale_entry_eval_pending_becomes_missing_terminal_state() -> None:
    logs = _logs(
        "2026-07-06T10:00:00 INFO DYNAMIC_SCAN selected=['OPEN']",
        "2026-07-06T10:00:01 INFO DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=OPEN source=scanner_selected",
    )

    status = run_self_heal._dynamic_entry_flow_status(
        logs.splitlines(),
        now=datetime.fromisoformat("2026-07-06T16:15:00"),
    )
    evidence = run_self_heal.detect_failure(
        logs,
        "LIVE",
        "since market open",
        now=datetime.fromisoformat("2026-07-06T16:15:00"),
    )

    assert status.status == "failure"
    assert status.symbols == ("OPEN",)
    assert evidence is not None
    assert evidence.short_failure == "missing_terminal_state"


def test_recent_entry_eval_pending_still_blocks_during_market_hours() -> None:
    logs = _logs(
        "2026-07-06T10:00:00 INFO DYNAMIC_SCAN selected=['OPEN']",
        "2026-07-06T10:00:01 INFO DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=OPEN source=scanner_selected",
    )

    status = run_self_heal._dynamic_entry_flow_status(
        logs.splitlines(),
        now=datetime.fromisoformat("2026-07-06T10:01:00"),
    )
    evidence = run_self_heal.detect_failure(
        logs,
        "LIVE",
        "30 minutes ago",
        now=datetime.fromisoformat("2026-07-06T10:01:00"),
    )

    assert status.status == "pending"
    assert status.symbols == ("OPEN",)
    assert evidence is None
