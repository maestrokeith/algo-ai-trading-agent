#!/usr/bin/env python3
"""Read-only shadow-mode readiness report."""

from __future__ import annotations

import argparse
import grp
import json
import pwd
import sys
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.artifact_writability import ArtifactWriteError, artifact_target_diagnostics, check_atomic_writability  # noqa: E402
from src.config_loader import deep_merge, load_config  # noqa: E402
from src.risk_book_mode import apply_risk_book_mode  # noqa: E402
from src.runtime_progress import load_runtime_progress, summarize_session_activity, trading_day_et  # noqa: E402
from src.trading_control import resolve_trading_mode  # noqa: E402
from src.trading_diagnostics import build_canonical_day  # noqa: E402


def _latest_day(data_dir: Path, user: str) -> str | None:
    daily = data_dir / "trade_attribution" / "daily"
    if not daily.exists():
        return None
    suffix = f"_{user}.json"
    days = sorted(path.name[:10] for path in daily.glob(f"*{suffix}") if len(path.name) >= 10)
    return days[-1] if days else None


def _latest_prior_day(data_dir: Path, user: str, before: str) -> str | None:
    daily = data_dir / "trade_attribution" / "daily"
    if not daily.exists():
        return None
    suffix = f"_{user}.json"
    days = sorted(path.name[:10] for path in daily.glob(f"*{suffix}") if len(path.name) >= 10 and path.name[:10] < before)
    return days[-1] if days else None


def _safe_user(user: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(user or "default")) or "default"


def _artifact_paths(project_root: Path, data_dir: Path, day: str, user: str) -> list[tuple[str, Path]]:
    safe_user = _safe_user(user)
    return [
        ("research-bars-status", data_dir / "research" / "bars_status"),
        ("research-bars-consistency", data_dir / "research" / "bars_consistency"),
        ("signal-expectancy-report", data_dir / "research_metrics" / day),
        ("day-review-report", project_root / "reports" / "day_review"),
        ("research-bars-status-json", data_dir / "research" / "bars_status" / f"{day}_{safe_user}.json"),
        ("research-bars-consistency-json", data_dir / "research" / "bars_consistency" / f"{day}_{safe_user}.json"),
        ("signal-expectancy-json", data_dir / "research_metrics" / day / "signal_expectancy_report.json"),
    ]


def _repo_identity(project_root: Path) -> tuple[str, str]:
    st = project_root.stat()
    return pwd.getpwuid(st.st_uid).pw_name, grp.getgrgid(st.st_gid).gr_name


def _load_user_config_without_secrets(project_root: Path, base_config: dict[str, Any], user: str) -> tuple[dict[str, Any], bool]:
    users_path = project_root / "config" / "users.yaml"
    if not users_path.exists():
        return dict(base_config), bool((base_config.get("broker") or {}).get("paper", True))
    raw = yaml.safe_load(users_path.read_text(encoding="utf-8")) or {}
    for entry in raw.get("users") or []:
        if not isinstance(entry, dict) or str(entry.get("id") or "") != user:
            continue
        merged = deep_merge(base_config, entry.get("overrides") or {})
        apply_risk_book_mode(merged)
        return merged, bool(entry.get("paper"))
    raise KeyError(f"Unknown user_id '{user}' in {users_path}")


def build_shadow_readiness(
    *,
    user: str,
    day: str | None = None,
    project_root: Path = PROJECT_ROOT,
    now: Any | None = None,
) -> dict[str, Any]:
    data_dir = project_root / "data"
    config = load_config(project_root / "config" / "default.yaml")
    user_config, paper = _load_user_config_without_secrets(project_root, config, user)
    mode = resolve_trading_mode(user_config, paper=paper, live_operation=not bool(paper))
    requested_date = day or trading_day_et(now)
    day_s = requested_date
    prior_day = _latest_prior_day(data_dir, user, requested_date)
    runtime_user, runtime_group = _repo_identity(project_root)

    checks: list[dict[str, Any]] = []
    blocking: list[str] = []
    warnings: list[str] = []

    if mode.mode != "shadow":
        blocking.append(f"mode_not_shadow:{mode.mode}")
    if mode.broker_orders_allowed:
        blocking.append("broker_orders_allowed")
    if mode.live_orders_allowed:
        warnings.append("live_orders_allowed_flag_true_but_broker_orders_disabled")
    if not mode.new_entries_allowed:
        warnings.append("shadow_observation_entries_not_enabled")

    artifact_rows: list[dict[str, Any]] = []
    if day_s:
        for name, path in _artifact_paths(project_root, data_dir, day_s, user):
            directory = path if path.suffix == "" else path.parent
            diag = artifact_target_diagnostics(path, runtime_user=runtime_user, runtime_group=runtime_group)
            row = {"name": name, "path": str(path), "diagnostics": diag}
            if path.suffix == "":
                if directory.exists():
                    try:
                        row["atomic"] = check_atomic_writability(
                            directory,
                            filename=f".{day_s}_{_safe_user(user)}_{name}.shadow-readiness.check",
                            runtime_user=runtime_user,
                            runtime_group=runtime_group,
                        )
                    except ArtifactWriteError as exc:
                        row["atomic"] = exc.as_dict()
                        blocking.append(f"artifact_not_writable:{name}")
                elif diag.get("can_create_directory"):
                    row["atomic"] = {
                        "ok": True,
                        "atomic_create": None,
                        "atomic_rename": None,
                        "cleanup": True,
                        "reason": "directory_missing_parent_creatable",
                    }
                else:
                    row["atomic"] = {
                        "ok": False,
                        "reason": "directory_missing_parent_not_creatable",
                    }
                    blocking.append(f"artifact_not_writable:{name}")
            missing_but_creatable = (not directory.exists()) and bool(diag.get("can_create_directory"))
            if not diag.get("target_user_writable") and not missing_but_creatable:
                blocking.append(f"artifact_target_user_not_writable:{name}")
            artifact_rows.append(row)
    else:
        warnings.append("no_lifecycle_day_available_for_artifact_check")

    counts: dict[str, Any] = {}
    attribution_path = data_dir / "trade_attribution" / "daily" / f"{day_s}_{_safe_user(user)}.json" if day_s else None
    runtime_progress = load_runtime_progress(data_dir, day=day_s, user_id=user) if day_s else None
    current_day_artifacts_present = bool((attribution_path and attribution_path.exists()) or runtime_progress)
    if day_s:
        try:
            canonical = build_canonical_day(root=project_root, day=day_s, user_id=user)
            counts = dict(canonical.get("counts") or {})
        except Exception as exc:
            warnings.append(f"canonical_lifecycle_unavailable:{type(exc).__name__}")

    real_submission_attempts = int(counts.get("real_order_submission_attempts", counts.get("unique_submitted_orders")) or 0)
    real_order_count = int(counts.get("unique_submitted_orders") or 0)
    real_broker_accepted = int(counts.get("real_broker_accepted_orders", counts.get("unique_broker_accepted_orders")) or 0)
    fill_count = int(counts.get("unique_fills") or 0)
    position_count = int(counts.get("unique_opened_positions") or 0) + int(counts.get("unique_still_open_positions") or 0)
    replay_contamination = int(counts.get("synthetic_or_replay_order_events") or 0)
    unresolved_contamination = int(counts.get("unresolved_contamination", replay_contamination) or 0)
    if mode.mode == "shadow" and real_submission_attempts:
        blocking.append(f"real_order_submission_attempts_present:{real_submission_attempts}")
    if mode.mode == "shadow" and real_broker_accepted:
        blocking.append(f"real_broker_accepted_orders_present:{real_broker_accepted}")
    if fill_count:
        blocking.append(f"fills_present:{fill_count}")
    if position_count:
        blocking.append(f"positions_present:{position_count}")
    if unresolved_contamination:
        blocking.append(f"unresolved_contamination_present:{unresolved_contamination}")

    options_cfg = user_config.get("options") if isinstance(user_config.get("options"), dict) else {}
    nested_live_pilot = options_cfg.get("live_pilot") if isinstance(options_cfg.get("live_pilot"), dict) else {}
    if options_cfg.get("live_pilot_enabled") or nested_live_pilot.get("enabled"):
        warnings.append("options_live_config_present_but_shadow_broker_guard_blocks_submission")

    entries_cfg = user_config.get("entries") if isinstance(user_config.get("entries"), dict) else {}
    raw_after = entries_cfg.get("avoid_new_entries_after_et") or entries_cfg.get("avoid_new_entries_after")
    entry_cutoff = None
    if raw_after:
        try:
            parts = str(raw_after).replace(":", " ").split()
            if len(parts) >= 2:
                from datetime import time as _time

                entry_cutoff = _time(int(parts[0]), int(parts[1]))
        except Exception:
            entry_cutoff = None
    session_activity = summarize_session_activity(
        runtime_progress,
        now=now,
        cadence_minutes=10.0,
        grace_minutes=5.0,
        entry_cutoff=entry_cutoff,
        shadow_intents_current_day=int(counts.get("shadow_order_intents") or 0),
    )
    if not current_day_artifacts_present:
        blocking.append("current_day_artifacts_missing")
    if session_activity.get("session_activity_status") not in {"ACTIVE_VALIDATED", "EXPECTED_NO_ACTIVITY_AFTER_ENTRY_CUTOFF", "EXPECTED_NO_ACTIVITY", "MARKET_CLOSED"}:
        blocking.append(f"session_activity_not_validated:{session_activity.get('session_activity_status')}")
    if session_activity.get("session_activity_status") in {"SCANNER_STALLED", "ACCOUNT_CONNECTIVITY_FAILURE"}:
        blocking.append(str(session_activity.get("session_activity_status")).lower())

    ready = not blocking
    return {
        "report": "shadow_readiness",
        "research_only": True,
        "ready": ready,
        "blocking_reasons": sorted(set(blocking)),
        "warnings": sorted(set(warnings)),
        "user": user,
        "date": day_s,
        "requested_date": requested_date,
        "resolved_trading_date": day_s,
        "artifact_date": day_s if current_day_artifacts_present else None,
        "artifact_date_matches_requested": bool(current_day_artifacts_present and day_s == requested_date),
        "current_day_artifacts_present": current_day_artifacts_present,
        "current_day_artifacts_missing": not current_day_artifacts_present,
        "prior_day_context_available": bool(prior_day),
        "prior_day_context_date": prior_day,
        "runtime_progress": runtime_progress,
        "session_activity": session_activity,
        "session_activity_status": session_activity.get("session_activity_status"),
        "configured_mode": str((user_config.get("trading_control") or {}).get("mode") or "missing"),
        "effective_mode": mode.mode,
        "broker_execution_disabled": not mode.broker_orders_allowed,
        "live_broker_orders_allowed": bool(mode.broker_orders_allowed and not paper),
        "new_real_entries_allowed": bool(mode.broker_orders_allowed and mode.new_entries_allowed and not paper),
        "hypothetical_entries_allowed": bool(mode.mode == "shadow" and mode.new_entries_allowed and not mode.broker_orders_allowed),
        "paper_entries_allowed": bool(mode.mode == "paper" and mode.broker_orders_allowed and bool(paper)),
        "live_entries_allowed": bool(mode.mode == "live" and mode.broker_orders_allowed and not paper),
        "broker_submission_allowed": bool(mode.broker_orders_allowed),
        "shadow_observation_enabled": bool(mode.mode == "shadow" and mode.new_entries_allowed and not mode.broker_orders_allowed),
        "paper": bool(paper),
        "artifact_writability": artifact_rows,
        "counts": counts,
        "real_order_submissions": real_submission_attempts,
        "real_order_submission_attempts": real_submission_attempts,
        "real_submitted_orders": real_order_count,
        "real_broker_accepted_orders": real_broker_accepted,
        "real_fills": fill_count,
        "real_positions": position_count,
        "fills": fill_count,
        "positions": position_count,
        "replay_contamination": replay_contamination,
        "unresolved_contamination": unresolved_contamination,
        "shadow_decisions": int(counts.get("shadow_decisions") or 0),
        "shadow_allocator_actions": int(counts.get("shadow_allocator_actions") or 0),
        "shadow_order_intents": int(counts.get("shadow_order_intents") or 0),
        "shadow_execution_blocks": int(counts.get("shadow_execution_blocks") or 0),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default="live_bot")
    parser.add_argument("--date", default=None)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = build_shadow_readiness(user=args.user, day=args.date, project_root=args.project_root)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print("# Shadow Readiness")
        print(f"- ready: {bool(report['ready'])}")
        print(f"- user: {report['user']}")
        print(f"- date: {report.get('date') or 'unavailable'}")
        print(f"- requested_date: {report.get('requested_date') or 'unavailable'}")
        print(f"- resolved_trading_date: {report.get('resolved_trading_date') or 'unavailable'}")
        print(f"- artifact_date_matches_requested: {report['artifact_date_matches_requested']}")
        print(f"- current_day_artifacts_present: {report['current_day_artifacts_present']}")
        print(f"- prior_day_context_available: {report['prior_day_context_available']}")
        print(f"- session_activity_status: {report.get('session_activity_status')}")
        print(f"- configured_mode: {report['configured_mode']}")
        print(f"- effective_mode: {report['effective_mode']}")
        print(f"- broker_execution_disabled: {report['broker_execution_disabled']}")
        print(f"- real_order_submissions: {report['real_order_submissions']}")
        print(f"- real_order_submission_attempts: {report['real_order_submission_attempts']}")
        print(f"- real_broker_accepted_orders: {report['real_broker_accepted_orders']}")
        print(f"- real_fills: {report['real_fills']}")
        print(f"- real_positions: {report['real_positions']}")
        print(f"- fills: {report['fills']}")
        print(f"- positions: {report['positions']}")
        print(f"- replay_contamination: {report['replay_contamination']}")
        print(f"- unresolved_contamination: {report['unresolved_contamination']}")
        print(f"- hypothetical_entries_allowed: {report['hypothetical_entries_allowed']}")
        print(f"- paper_entries_allowed: {report['paper_entries_allowed']}")
        print(f"- live_entries_allowed: {report['live_entries_allowed']}")
        print(f"- broker_submission_allowed: {report['broker_submission_allowed']}")
        print(f"- shadow_decisions: {report['shadow_decisions']}")
        print(f"- shadow_allocator_actions: {report['shadow_allocator_actions']}")
        print(f"- shadow_order_intents: {report['shadow_order_intents']}")
        print(f"- shadow_execution_blocks: {report['shadow_execution_blocks']}")
        print(f"- blocking_reasons: {', '.join(report['blocking_reasons']) or 'none'}")
        print(f"- warnings: {', '.join(report['warnings']) or 'none'}")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
