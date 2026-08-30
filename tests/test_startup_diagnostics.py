from __future__ import annotations

from datetime import date, datetime

from scripts import startup_diagnostics as diag


def test_parse_unit_properties_preserves_repeated_entries() -> None:
    text = """
# /etc/systemd/system/algo-start.timer
[Timer]
OnCalendar=Mon..Fri 09:25:00
Unit=algo.service
Persistent=true
"""
    props = diag.parse_unit_properties(text)

    assert props["OnCalendar"] == ["Mon..Fri 09:25:00"]
    assert props["Unit"] == ["algo.service"]
    assert props["Persistent"] == ["true"]


def test_detects_direct_timer_target_to_running_service() -> None:
    conditions = diag.detect_startup_conditions(
        timer_props={
            "OnCalendar": ["Mon..Fri 09:25:00"],
            "Unit": ["algo.service"],
            "Persistent": ["true"],
        },
        service_props={"Type": ["simple"]},
        timer_show={"LastTriggerUSec": "2026-06-10T09:25:04-0400"},
        service_show={
            "ExecMainStartTimestamp": "2026-06-10T06:45:50-0400",
            "ActiveState": "active",
        },
        journal_lines=[],
        target_date=date(2026, 6, 10),
    )

    reasons = {reason for reason, _seconds in conditions}
    assert "persistent_timer_catch_up_enabled" in reasons
    assert "timer_fired_while_service_already_active" in reasons
    assert "timer_targets_long_running_service_directly" in reasons


def test_startup_duration_uses_begin_and_ready_markers() -> None:
    begin = datetime.fromisoformat("2026-06-10T09:25:01-04:00")
    ready = datetime.fromisoformat("2026-06-10T09:25:13-04:00")

    assert diag.startup_duration_seconds(begin, ready) == 12.0


def test_render_report_includes_required_output_fields() -> None:
    report = {
        "timer_schedule": "Mon..Fri 09:25:00",
        "timer_unit": "algo.service",
        "timer_persistent": "true",
        "timer_randomized_delay": "0",
        "timer_accuracy": "default",
        "last_timer_fire": "2026-06-10T09:25:04-04:00",
        "next_timer_fire": "Thu 2026-06-11 09:25:00 EDT",
        "last_service_start": "2026-06-10T06:45:50-04:00",
        "startup_duration_seconds": None,
        "readiness_status": "degraded_no_ready_marker",
        "algo_start_service_present": False,
        "timeline": {
            "STARTUP_TIMER_FIRED": datetime.fromisoformat("2026-06-10T09:25:04-04:00"),
            "service_launched": datetime.fromisoformat("2026-06-10T06:45:50-04:00"),
            "first_DYNAMIC_SCAN": datetime.fromisoformat("2026-06-10T09:32:02-04:00"),
            "first_ENTRY_EVAL": datetime.fromisoformat("2026-06-10T09:44:49-04:00"),
        },
        "conditions": [("timer_fired_while_service_already_active", 9574.0)],
    }

    rendered = diag.render_report(report)

    assert "timer schedule: Mon..Fri 09:25:00" in rendered
    assert "last timer fire: 2026-06-10T09:25:04-04:00" in rendered
    assert "last service start: 2026-06-10T06:45:50-04:00" in rendered
    assert "startup duration: unknown" in rendered
    assert "readiness status: degraded_no_ready_marker" in rendered
    assert "STARTUP_TIMER_FIRED timestamp=2026-06-10T09:25:04-04:00" in rendered
    assert "STARTUP_DELAY reason=timer_fired_while_service_already_active seconds=9574.0" in rendered
