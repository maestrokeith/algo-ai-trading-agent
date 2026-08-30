#!/usr/bin/env python3
"""Read-only systemd and journal diagnostics for live startup timing."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.9 fallback
    from backports.zoneinfo import ZoneInfo  # type: ignore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
DEFAULT_TIMER = "algo-start.timer"
DEFAULT_SERVICE = "algo.service"
PREMARKET_UNITS = (
    "algosphere-premarket.timer",
    "algosphere-premarket.service",
    "algosphere-ops-premarket-ready.timer",
    "algosphere-ops-premarket-ready.service",
    "algosphere-ops-startup-validation.timer",
    "algosphere-ops-startup-validation.service",
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def _run_command(args: Sequence[str], *, timeout: float = 8.0) -> CommandResult:
    try:
        proc = subprocess.run(
            list(args),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        return CommandResult(127, "", f"{type(exc).__name__}: {exc}")
    return CommandResult(int(proc.returncode), proc.stdout.strip(), proc.stderr.strip())


def parse_systemctl_show(output: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def parse_unit_properties(unit_text: str) -> dict[str, list[str]]:
    props: dict[str, list[str]] = {}
    for raw_line in unit_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        props.setdefault(key.strip(), []).append(value.strip())
    return props


def first_prop(props: Mapping[str, Sequence[str]], key: str, default: str = "unknown") -> str:
    values = props.get(key)
    if not values:
        return default
    return str(values[0])


def parse_short_iso_timestamp(line: str) -> datetime | None:
    token = line.split(" ", 1)[0].strip()
    if not token:
        return None
    try:
        return datetime.strptime(token, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None


def timestamp_text(dt: datetime | None) -> str:
    if dt is None:
        return "unknown"
    return dt.astimezone(ET).isoformat()


def parse_systemd_timestamp(value: str | None) -> datetime | None:
    if not value or value in {"n/a", "unknown"}:
        return None
    text = value.strip()
    for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=ET)
        return parsed.astimezone(ET)
    return None


def parse_on_calendar_time(on_calendar: str) -> time | None:
    match = re.search(r"(\d{2}):(\d{2})(?::(\d{2}))?", on_calendar or "")
    if not match:
        return None
    hour, minute, second = match.groups(default="0")
    return time(int(hour), int(minute), int(second), tzinfo=ET)


def expected_fire_for_date(timer_props: Mapping[str, Sequence[str]], target_date: date) -> datetime | None:
    fire_time = parse_on_calendar_time(first_prop(timer_props, "OnCalendar", ""))
    if fire_time is None:
        return None
    return datetime.combine(target_date, fire_time).astimezone(ET)


def first_journal_timestamp(lines: Sequence[str], pattern: str) -> datetime | None:
    for line in lines:
        if pattern in line:
            return parse_short_iso_timestamp(line)
    return None


def first_journal_line_timestamp(lines: Sequence[str], patterns: Sequence[str]) -> datetime | None:
    for line in lines:
        if any(pattern in line for pattern in patterns):
            return parse_short_iso_timestamp(line)
    return None


def latest_journal_line_timestamp(
    lines: Sequence[str],
    patterns: Sequence[str],
    *,
    before: datetime | None = None,
) -> datetime | None:
    latest: datetime | None = None
    for line in lines:
        if not any(pattern in line for pattern in patterns):
            continue
        ts = parse_short_iso_timestamp(line)
        if ts is None:
            continue
        if before is not None and ts > before:
            continue
        latest = ts
    return latest


def startup_duration_seconds(begin: datetime | None, ready: datetime | None) -> float | None:
    if begin is None or ready is None:
        return None
    return max(0.0, (ready - begin).total_seconds())


def detect_startup_conditions(
    *,
    timer_props: Mapping[str, Sequence[str]],
    service_props: Mapping[str, Sequence[str]],
    timer_show: Mapping[str, str],
    service_show: Mapping[str, str],
    journal_lines: Sequence[str],
    target_date: date,
) -> list[tuple[str, float | None]]:
    findings: list[tuple[str, float | None]] = []
    expected_fire = expected_fire_for_date(timer_props, target_date)
    timer_fire = parse_systemd_timestamp(timer_show.get("LastTriggerUSec"))
    service_start = parse_systemd_timestamp(
        service_show.get("ExecMainStartTimestamp")
        or service_show.get("ActiveEnterTimestamp")
    )
    if first_prop(timer_props, "Persistent", "false").lower() == "true":
        findings.append(("persistent_timer_catch_up_enabled", None))
    if timer_fire is None:
        findings.append(("missed_timer_execution", None))
    elif expected_fire is not None and abs((timer_fire - expected_fire).total_seconds()) > 90:
        findings.append(("timer_fire_outside_expected_window", abs((timer_fire - expected_fire).total_seconds())))
    if service_start is not None and timer_fire is not None and service_start < timer_fire:
        findings.append(("timer_fired_while_service_already_active", (timer_fire - service_start).total_seconds()))
    if first_prop(timer_props, "Unit", "") == DEFAULT_SERVICE and first_prop(service_props, "Type", "simple") == "simple":
        findings.append(("timer_targets_long_running_service_directly", None))
    if first_journal_line_timestamp(journal_lines, ("waiting for lock", "lock wait", "Loop lock already held")):
        findings.append(("lock_wait_observed", None))
    if first_journal_line_timestamp(journal_lines, ("network-online.target", "dependency", "Job ", "Waiting for")):
        findings.append(("dependency_wait_observed", None))
    premarket_start = latest_journal_line_timestamp(
        journal_lines,
        ("PREMARKET_JOB_START", "PREMARKET_JOB_RUNNING"),
        before=timer_fire,
    )
    premarket_end = first_journal_line_timestamp(
        journal_lines,
        ("PREMARKET_JOB_DONE", "PREMARKET_JOB_END", "Finished algosphere-premarket.service"),
    )
    if (
        premarket_start is not None
        and timer_fire is not None
        and (premarket_end is None or premarket_end > timer_fire)
    ):
        findings.append(("premarket_job_blocking_startup", None))
    return findings


def collect_startup_report(
    *,
    target_date: date,
    timer: str = DEFAULT_TIMER,
    service: str = DEFAULT_SERVICE,
) -> dict[str, Any]:
    timer_cat = _run_command(("systemctl", "cat", timer, "--no-pager"))
    timer_props = parse_unit_properties(timer_cat.stdout) if timer_cat.returncode == 0 else {}
    service_cat = _run_command(("systemctl", "cat", service, "--no-pager"))
    service_props = parse_unit_properties(service_cat.stdout) if service_cat.returncode == 0 else {}
    start_service_cat = _run_command(("systemctl", "cat", "algo-start.service", "--no-pager"))

    timer_show = _run_command(
        (
            "systemctl",
            "show",
            timer,
            "--property=ActiveState,SubState,Result,LastTriggerUSec,NextElapseUSecRealtime,UnitFileState",
            "--no-pager",
        )
    )
    service_show = _run_command(
        (
            "systemctl",
            "show",
            service,
            "--property=ActiveState,SubState,Result,ExecMainPID,ExecMainStartTimestamp,ActiveEnterTimestamp,After,Wants,UnitFileState",
            "--no-pager",
        )
    )
    timer_show_fields = parse_systemctl_show(timer_show.stdout) if timer_show.returncode == 0 else {}
    service_show_fields = parse_systemctl_show(service_show.stdout) if service_show.returncode == 0 else {}

    since = datetime.combine(target_date, time(0, 0), tzinfo=ET).strftime("%Y-%m-%d %H:%M:%S")
    until = datetime.combine(target_date, time(23, 59, 59), tzinfo=ET).strftime("%Y-%m-%d %H:%M:%S")
    journal_units = (timer, service, *PREMARKET_UNITS)
    journal = _run_command(
        (
            "journalctl",
            *(arg for unit in journal_units for arg in ("-u", unit)),
            "--since",
            since,
            "--until",
            until,
            "--no-pager",
            "--output=short-iso",
        ),
        timeout=15.0,
    )
    journal_lines = journal.stdout.splitlines() if journal.returncode == 0 else []

    timer_fire = parse_systemd_timestamp(timer_show_fields.get("LastTriggerUSec"))
    begin = first_journal_timestamp(journal_lines, "STARTUP_SERVICE_BEGIN")
    ready = first_journal_timestamp(journal_lines, "STARTUP_SERVICE_READY")
    first_dynamic = first_journal_timestamp(journal_lines, "DYNAMIC_SCAN")
    first_entry = first_journal_timestamp(journal_lines, "ENTRY_EVAL")
    service_started = latest_journal_line_timestamp(
        journal_lines,
        (f"Started {service}",),
        before=first_dynamic or timer_fire,
    )
    if service_started is None:
        service_started = first_journal_line_timestamp(journal_lines, (f"Started {service}", f"Starting {service}"))
    if service_started is None:
        service_started = parse_systemd_timestamp(
            service_show_fields.get("ExecMainStartTimestamp")
            or service_show_fields.get("ActiveEnterTimestamp")
        )
    process_started = begin or service_started

    conditions = detect_startup_conditions(
        timer_props=timer_props,
        service_props=service_props,
        timer_show=timer_show_fields,
        service_show=service_show_fields,
        journal_lines=journal_lines,
        target_date=target_date,
    )
    if service_started is not None and timer_fire is not None and service_started < timer_fire:
        reason = "timer_fired_while_service_already_active"
        if not any(existing == reason for existing, _seconds in conditions):
            conditions.append((reason, (timer_fire - service_started).total_seconds()))

    duration = startup_duration_seconds(begin, ready)
    readiness = "ready" if ready else "degraded_no_ready_marker"
    if not journal_lines:
        readiness = "unknown_no_journal"

    return {
        "timer": timer,
        "service": service,
        "target_date": target_date.isoformat(),
        "timer_schedule": ",".join(timer_props.get("OnCalendar", [])) or "unknown",
        "timer_unit": first_prop(timer_props, "Unit"),
        "timer_persistent": first_prop(timer_props, "Persistent", "false"),
        "timer_randomized_delay": first_prop(timer_props, "RandomizedDelaySec", "0"),
        "timer_accuracy": first_prop(timer_props, "AccuracySec", "default"),
        "last_timer_fire": timestamp_text(timer_fire),
        "next_timer_fire": timer_show_fields.get("NextElapseUSecRealtime") or "unknown",
        "last_service_start": timestamp_text(
            service_started
            or parse_systemd_timestamp(
                service_show_fields.get("ExecMainStartTimestamp")
                or service_show_fields.get("ActiveEnterTimestamp")
            )
        ),
        "service_active_state": service_show_fields.get("ActiveState", "unknown"),
        "service_sub_state": service_show_fields.get("SubState", "unknown"),
        "service_dependencies": {
            "after": service_show_fields.get("After", ""),
            "wants": service_show_fields.get("Wants", ""),
        },
        "algo_start_service_present": start_service_cat.returncode == 0,
        "startup_duration_seconds": duration,
        "readiness_status": readiness,
        "timeline": {
            "STARTUP_TIMER_FIRED": timer_fire,
            "service_launched": service_started,
            "STARTUP_SERVICE_BEGIN": begin,
            "STARTUP_SERVICE_READY": ready,
            "process_started": process_started,
            "first_DYNAMIC_SCAN": first_dynamic,
            "first_ENTRY_EVAL": first_entry,
        },
        "conditions": conditions,
        "journal_error": "" if journal.returncode == 0 else journal.stderr,
        "timer_cat_error": "" if timer_cat.returncode == 0 else timer_cat.stderr,
        "service_cat_error": "" if service_cat.returncode == 0 else service_cat.stderr,
    }


def render_report(report: Mapping[str, Any]) -> str:
    duration = report.get("startup_duration_seconds")
    duration_text = "unknown" if duration is None else f"{float(duration):.1f}s"
    lines = [
        "Startup Diagnostics",
        f"timer schedule: {report.get('timer_schedule', 'unknown')}",
        f"timer unit: {report.get('timer_unit', 'unknown')}",
        f"timer Persistent: {report.get('timer_persistent', 'unknown')}",
        f"timer RandomizedDelaySec: {report.get('timer_randomized_delay', 'unknown')}",
        f"timer AccuracySec: {report.get('timer_accuracy', 'unknown')}",
        f"last timer fire: {report.get('last_timer_fire', 'unknown')}",
        f"next timer fire: {report.get('next_timer_fire', 'unknown')}",
        f"last service start: {report.get('last_service_start', 'unknown')}",
        f"startup duration: {duration_text}",
        f"readiness status: {report.get('readiness_status', 'unknown')}",
        f"algo-start.service present: {str(bool(report.get('algo_start_service_present'))).lower()}",
        "",
        "Timeline",
    ]
    timeline = report.get("timeline") if isinstance(report.get("timeline"), Mapping) else {}
    for key in (
        "STARTUP_TIMER_FIRED",
        "service_launched",
        "STARTUP_SERVICE_BEGIN",
        "STARTUP_SERVICE_READY",
        "process_started",
        "first_DYNAMIC_SCAN",
        "first_ENTRY_EVAL",
    ):
        value = timeline.get(key)
        lines.append(f"{key} timestamp={timestamp_text(value) if isinstance(value, datetime) else value or 'unknown'}")

    lines.append("")
    lines.append("Detected Conditions")
    conditions = report.get("conditions") if isinstance(report.get("conditions"), Sequence) else []
    if conditions:
        for reason, seconds in conditions:
            seconds_text = "unknown" if seconds is None else f"{float(seconds):.1f}"
            lines.append(f"STARTUP_DELAY reason={reason} seconds={seconds_text}")
    else:
        lines.append("none")
    if report.get("journal_error"):
        lines.append(f"journal warning: {report['journal_error']}")
    if report.get("timer_cat_error"):
        lines.append(f"timer unit warning: {report['timer_cat_error']}")
    if report.get("service_cat_error"):
        lines.append(f"service unit warning: {report['service_cat_error']}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        default=datetime.now(ET).date().isoformat(),
        help="Trading date to inspect, YYYY-MM-DD.",
    )
    parser.add_argument("--timer", default=DEFAULT_TIMER, help="Startup timer unit.")
    parser.add_argument("--service", default=DEFAULT_SERVICE, help="Live service unit.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        target_date = date.fromisoformat(str(args.date))
    except ValueError:
        print(f"invalid --date {args.date!r}; expected YYYY-MM-DD", file=sys.stderr)
        return 2
    report = collect_startup_report(target_date=target_date, timer=args.timer, service=args.service)
    print(render_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
