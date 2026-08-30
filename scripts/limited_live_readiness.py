#!/usr/bin/env python3
"""Read-only bounded live equity pilot readiness check."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_config  # noqa: E402
from src.controlled_live_equity import (  # noqa: E402
    PREMARKET_PENDING_FIRST_EXIT_CYCLE,
    controlled_live_limit_blockers,
    controlled_live_limits,
    premarket_pending_first_exit_cycle_allowed,
    runtime_profile,
)
from src.limited_live_pilot import classify_broker_positions, load_pilot_state, pilot_limits  # noqa: E402
from src.options_config import options_enabled, options_live_pilot_enabled  # noqa: E402
from src.pilot_exit_management import load_exit_status, position_management_status_report  # noqa: E402
from src.trading_control import resolve_trading_mode, strategy_states  # noqa: E402
from src.trading_diagnostics import build_canonical_day  # noqa: E402
from src.user_manager import UserManager  # noqa: E402


def _run_git(root: Path, args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(["git", *args], cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    return proc.returncode, proc.stdout.strip()


def _git_state(root: Path) -> dict[str, Any]:
    rc_status, status = _run_git(root, ["status", "--porcelain"])
    rc_head, head = _run_git(root, ["rev-parse", "HEAD"])
    rc_sync, sync = _run_git(root, ["rev-list", "--left-right", "--count", "origin/main...main"])
    return {
        "head": head if rc_head == 0 else "unknown",
        "working_tree_clean": rc_status == 0 and status == "",
        "origin_sync": sync if rc_sync == 0 else "unknown",
        "origin_synced": rc_sync == 0 and sync.strip() == "0\t0",
    }


def _safe_count(rows: Any) -> int | str:
    if rows is None:
        return "unknown"
    try:
        return len(rows)
    except TypeError:
        return "unknown"


def _normalize_account_status(raw: Any) -> str:
    value = getattr(raw, "value", raw)
    text = str(value or "unknown").strip()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text.upper()


def _state_count(state: Mapping[str, Any], key: str) -> int:
    try:
        return max(0, int(float(state.get(key, 0) or 0)))
    except (TypeError, ValueError):
        return 0


def _raw_pilot_state(root: Path, user: str, day: str) -> dict[str, Any]:
    safe_user = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(user or "default"))
    path = root / "data" / "limited_live_pilot" / f"{day}_{safe_user}.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"state_unreadable": True}
    return payload if isinstance(payload, dict) else {"state_malformed": True}


def _historical_pilot_symbols(root: Path, user: str, day: str) -> list[str]:
    safe_user = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(user or "default"))
    base = root / "data" / "limited_live_pilot"
    if not base.exists():
        return []
    out: list[str] = []
    for path in sorted(base.glob(f"*_{safe_user}.json"), reverse=True):
        state_day = path.name[:10]
        if state_day >= str(day):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        try:
            dispatch_attempts = int(float(payload.get("broker_dispatch_attempts", 0) or 0))
        except (TypeError, ValueError):
            dispatch_attempts = 0
        has_dispatch_lineage = bool(payload.get("broker_dispatch_attempted")) or dispatch_attempts > 0 or bool(payload.get("consumed_submission"))
        if not has_dispatch_lineage:
            continue
        for symbol in payload.get("submitted_symbols") or []:
            sym = str(symbol or "").strip().upper()
            if sym and sym not in out:
                out.append(sym)
    return out


def _broker_checks(root: Path, user: str, config: Mapping[str, Any], pilot_state: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "broker_environment": "unknown",
        "broker_authenticated": False,
        "account_status": "unknown",
        "account_trading_blocked": "unknown",
        "open_broker_orders": "unknown",
        "broker_positions": "unknown",
        "broker_positions_total": "unknown",
        "preexisting_allowed_positions": 0,
        "pilot_managed_positions": 0,
        "unknown_positions": "unknown",
        "preexisting_allowed_notional": 0.0,
        "pilot_deployed_notional": float(pilot_state.get("deployed_notional", 0.0) or 0.0),
        "total_broker_notional": "unknown",
        "position_classifications": [],
        "blocking_reasons": [],
    }
    try:
        base = load_config(root / "config" / "default.yaml")
        mgr = UserManager(base, users_path=root / "config" / "users.yaml", selected_user_id=user)
        ctx = mgr.get_user(user)
        broker = mgr.get_broker(user)
        out["broker_environment"] = "paper" if ctx.paper else "live"
        if ctx.paper:
            out["blocking_reasons"].append("broker_environment_not_live")
        account = getattr(broker, "_trading").get_account()
        out["broker_authenticated"] = True
        out["account_status"] = _normalize_account_status(getattr(account, "status", "unknown"))
        blocked = bool(getattr(account, "trading_blocked", False) or getattr(account, "account_blocked", False))
        out["account_trading_blocked"] = blocked
        if out["account_status"] != "ACTIVE":
            out["blocking_reasons"].append("account_not_active")
        if blocked:
            out["blocking_reasons"].append("account_trading_blocked")
        orders = broker.list_orders(status="open")
        positions = broker.get_positions()
        out["open_broker_orders"] = _safe_count(orders)
        position_report = classify_broker_positions(config, positions, pilot_state=pilot_state)
        out.update(position_report)
        out["broker_positions"] = position_report["broker_positions_total"]
        if _safe_count(orders) != 0:
            out["blocking_reasons"].append("open_broker_orders_present")
        if position_report["unknown_positions"] == "unknown":
            out["blocking_reasons"].append("broker_state_unknown:positions")
        elif int(position_report["unknown_positions"] or 0) > 0:
            out["blocking_reasons"].append("unknown_positions")
    except Exception as exc:
        out["blocking_reasons"].append(f"broker_state_unknown:{type(exc).__name__}")
    return out


def build_limited_live_readiness(*, root: Path, user: str, day: str) -> dict[str, Any]:
    base = load_config(root / "config" / "default.yaml")
    try:
        mgr = UserManager(base, users_path=root / "config" / "users.yaml", selected_user_id=user)
        ctx = mgr.get_user(user)
        config = ctx.config
        paper = ctx.paper
    except Exception:
        config = base
        paper = False
    mode = resolve_trading_mode(config, paper=paper, live_operation=not bool(paper))
    profile = runtime_profile(config)
    limits = pilot_limits(config)
    controlled_limits = controlled_live_limits(config)
    states = {name: row.state for name, row in strategy_states(config).items()}
    live_strategies = sorted(name for name, state in states.items() if state == "LIVE")
    options_active = bool(options_enabled(config) or options_live_pilot_enabled(config))
    git = _git_state(root)
    canonical = build_canonical_day(root=root, day=day, user_id=user)
    counts = canonical.get("counts") if isinstance(canonical.get("counts"), Mapping) else {}
    pilot_state = load_pilot_state(root / "data", user, day)
    raw_state = _raw_pilot_state(root, user, day)
    state_record_trading_date = pilot_state.get("trading_date") if not pilot_state.get("prior_or_ambiguous_state_ignored") else pilot_state.get("ignored_state_record_trading_date")
    current_day_state_loaded = bool(
        raw_state
        and not pilot_state.get("prior_or_ambiguous_state_ignored")
        and str(pilot_state.get("trading_date") or "") == str(day)
    )
    prior_day_state_detected = bool(pilot_state.get("prior_or_ambiguous_state_ignored"))
    ambiguous_legacy_reservations = (
        _state_count(raw_state, "active_submission_reservations")
        if raw_state and not raw_state.get("trading_date")
        else 0
    )
    ambiguous_legacy_lock = bool(raw_state and not raw_state.get("trading_date") and raw_state.get("entry_locked"))
    stale_prior_day_reservations = (
        _state_count(raw_state, "active_submission_reservations")
        if prior_day_state_detected
        else 0
    )
    broker_dispatch_attempts = int(pilot_state.get("broker_dispatch_attempts", 0) or 0)
    legacy_entry_submissions = int(pilot_state.get("entry_submissions", 0) or 0)
    local_accepted_entry_orders = int(pilot_state.get("accepted_entry_orders", 0) or 0)
    local_entry_fills = int(pilot_state.get("entry_fills", 0) or 0)
    local_open_positions = int(pilot_state.get("open_positions", 0) or 0)
    reconciled_accepted_orders = int(counts.get("broker_accepted_orders_reconciled", 0) or 0)
    reconciled_fill_events = int(counts.get("broker_reconciled_fill_events", 0) or 0)
    reconciled_positions = int(counts.get("broker_reconciled_positions_today", 0) or 0)
    accepted_entry_orders = max(local_accepted_entry_orders, int(counts.get("broker_accepted_orders", 0) or 0))
    entry_fills = max(local_entry_fills, int(counts.get("completed_fills", 0) or 0))
    open_positions = max(local_open_positions, int(counts.get("opened_positions", 0) or 0))
    active_reservations = int(pilot_state.get("active_submission_reservations", 0) or 0)
    released_reservations = int(pilot_state.get("released_reservations", 0) or 0)
    order_intents = int(pilot_state.get("order_intents", 0) or 0)
    lock_reasons = [str(item) for item in (pilot_state.get("lock_reasons") or [])]
    false_or_stale_lock = bool(
        (
            pilot_state.get("entry_locked")
            and broker_dispatch_attempts == 0
            and accepted_entry_orders == 0
            and entry_fills == 0
            and open_positions == 0
            and legacy_entry_submissions > 0
            and set(lock_reasons).issubset({"first_submission_reserved", "broker_dispatch_attempt_reserved"})
        )
        or stale_prior_day_reservations > 0
        or ambiguous_legacy_reservations > 0
        or ambiguous_legacy_lock
    )
    submissions_today = broker_dispatch_attempts
    if broker_dispatch_attempts == 0 and legacy_entry_submissions > 0 and not false_or_stale_lock:
        submissions_today = legacy_entry_submissions
    broker_state = dict(pilot_state)
    submitted_symbols = [str(sym).strip().upper() for sym in (broker_state.get("submitted_symbols") or []) if str(sym).strip()]
    historical_symbols = _historical_pilot_symbols(root, user, day)
    for sym in historical_symbols:
        if sym not in submitted_symbols:
            submitted_symbols.append(sym)
    if submitted_symbols:
        broker_state["submitted_symbols"] = submitted_symbols
    broker = _broker_checks(root, user, config, broker_state)
    exit_status = position_management_status_report(
        config=config,
        data_dir=root / "data",
        user_id=user,
        day=day,
        positions=[],
    )
    if isinstance(broker.get("position_classifications"), list):
        class_rows = broker.get("position_classifications") or []
        managed_syms = sorted(
            str(row.get("symbol") or "").strip().upper()
            for row in class_rows
            if isinstance(row, Mapping) and str(row.get("classification") or "").strip().upper() == "PILOT_MANAGED"
        )
        protected_syms = sorted(
            str(row.get("symbol") or "").strip().upper()
            for row in class_rows
            if isinstance(row, Mapping) and str(row.get("classification") or "").strip().upper() == "PREEXISTING_ALLOWED"
        )
        raw_exit_status = load_exit_status(root / "data", user, day)
        exit_rows = raw_exit_status.get("positions") if isinstance(raw_exit_status.get("positions"), Mapping) else {}
        registered = sorted(
            sym
            for sym in managed_syms
            if isinstance(exit_rows.get(sym), Mapping) and exit_rows[sym].get("last_exit_eval_at")
        )
        last_by_symbol = {
            sym: (exit_rows.get(sym) or {}).get("last_exit_eval_at")
            for sym in registered
        }
        missing = sorted(sym for sym in managed_syms if sym not in registered)
        exit_status = {
            "managed_positions_registered_for_exit": registered,
            "managed_positions_missing_exit_registration": missing,
            "last_exit_cycle_at": max([v for v in last_by_symbol.values() if v] or [None]),
            "last_exit_eval_by_symbol": last_by_symbol,
            "last_iwm_exit_eval_at": last_by_symbol.get("IWM"),
            "exit_manager_healthy": not missing,
            "eod_flatten_registration": all(bool((exit_rows.get(sym) or {}).get("eod_flatten_registration")) for sym in managed_syms) if managed_syms else True,
            "protected_preexisting_positions": protected_syms,
            "exit_management_status": "healthy" if not missing else "stale_or_missing",
        }
    if isinstance(broker.get("pilot_managed_positions"), int):
        open_positions = max(open_positions, int(broker.get("pilot_managed_positions") or 0))
    premarket_exit_pending = False
    if exit_status.get("managed_positions_missing_exit_registration"):
        managed_symbols = [
            str(row.get("symbol") or "").strip().upper()
            for row in broker.get("position_classifications") or []
            if isinstance(row, Mapping) and str(row.get("classification") or "").strip().upper() == "PILOT_MANAGED"
        ]
        premarket_exit_pending = profile == "controlled_live_equity" and premarket_pending_first_exit_cycle_allowed(
            requested_day=day,
            managed_symbols=managed_symbols,
            unknown_positions=broker.get("unknown_positions"),
            open_broker_orders=broker.get("open_broker_orders"),
        )
        if premarket_exit_pending:
            exit_status["exit_management_status"] = PREMARKET_PENDING_FIRST_EXIT_CYCLE
    blocking: list[str] = []
    if mode.mode != "live":
        blocking.append(f"mode_not_live:{mode.mode}")
    if profile == "bounded_live_pilot" and not limits.enabled:
        blocking.append("live_pilot_disabled")
    if mode.mode != "live" and not limits.enabled:
        blocking.append("live_pilot_disabled")
    if mode.mode == "live":
        if profile == "controlled_live_equity":
            blocking.extend(controlled_live_limit_blockers(config))
        elif profile != "bounded_live_pilot":
            blocking.append(f"runtime_profile_not_controlled_or_bounded:{profile}")
    if live_strategies != ["trend_long"]:
        blocking.append("live_strategy_set_not_trend_long_only")
    if states.get("options_live") != "DISABLED" or states.get("options_paper") != "DISABLED":
        blocking.append("options_strategy_state_not_disabled")
    if options_active:
        blocking.append("options_active")
    if not git["working_tree_clean"]:
        blocking.append("dirty_working_tree")
    if not git["origin_synced"]:
        blocking.append(f"origin_not_synced:{git['origin_sync']}")
    for key in ("unresolved_contamination", "runtime_exception_count", "integrity_incident_count"):
        if int(counts.get(key, 0) or 0) > 0:
            blocking.append(f"{key}_present")
    if profile == "bounded_live_pilot" and active_reservations > 0:
        blocking.append("active_submission_reservation_present")
    if profile == "bounded_live_pilot" and submissions_today > 0:
        blocking.append("pilot_submission_already_used")
    if profile == "bounded_live_pilot" and entry_fills > 0:
        blocking.append("pilot_fill_already_used")
    if profile == "bounded_live_pilot" and open_positions > 0:
        blocking.append("pilot_open_position_present")
    for sym in exit_status.get("managed_positions_missing_exit_registration") or []:
        if not premarket_exit_pending:
            blocking.append(f"pilot_position_exit_management_stale:{sym}")
    blocking.extend(str(reason) for reason in broker.get("blocking_reasons", []))
    derived_entry_lock = bool(pilot_state.get("entry_locked") or submissions_today > 0 or accepted_entry_orders > 0 or entry_fills > 0 or open_positions > 0)
    entry_lock_reason = pilot_state.get("entry_lock_reason") or ",".join(lock_reasons)
    if not entry_lock_reason and open_positions > 0:
        entry_lock_reason = "pilot_open_position_present"
    elif not entry_lock_reason and submissions_today > 0:
        entry_lock_reason = "pilot_submission_already_used"
    return {
        "ready": not blocking,
        "user": user,
        "date": day,
        "requested_trading_date": day,
        "state_record_trading_date": state_record_trading_date,
        "current_day_state_loaded": current_day_state_loaded,
        "prior_day_state_detected": prior_day_state_detected,
        "stale_prior_day_reservations": stale_prior_day_reservations,
        "ambiguous_legacy_reservations": ambiguous_legacy_reservations,
        "ambiguous_legacy_lock_detected": ambiguous_legacy_lock,
        "configured_mode": str(((config.get("trading_control") or {}).get("mode")) or "missing"),
        "effective_mode": mode.mode,
        "runtime_profile": profile,
        "broker_environment": broker["broker_environment"],
        "broker_authenticated": broker["broker_authenticated"],
        "account_status": broker["account_status"],
        "account_trading_blocked": broker["account_trading_blocked"],
        "open_broker_orders": broker["open_broker_orders"],
        "broker_positions": broker["broker_positions"],
        "broker_positions_total": broker["broker_positions_total"],
        "preexisting_allowed_positions": broker["preexisting_allowed_positions"],
        "pilot_managed_positions": broker["pilot_managed_positions"],
        "unknown_positions": broker["unknown_positions"],
        "preexisting_allowed_notional": broker["preexisting_allowed_notional"],
        "pilot_deployed_notional": broker["pilot_deployed_notional"],
        "total_broker_notional": broker["total_broker_notional"],
        "position_classifications": broker["position_classifications"],
        "managed_positions_registered_for_exit": exit_status["managed_positions_registered_for_exit"],
        "managed_positions_missing_exit_registration": exit_status["managed_positions_missing_exit_registration"],
        "last_exit_cycle_at": exit_status["last_exit_cycle_at"],
        "last_exit_eval_by_symbol": exit_status["last_exit_eval_by_symbol"],
        "last_iwm_exit_eval_at": exit_status["last_iwm_exit_eval_at"],
        "exit_manager_healthy": exit_status["exit_manager_healthy"],
        "eod_flatten_registration": exit_status["eod_flatten_registration"],
        "protected_preexisting_positions": exit_status["protected_preexisting_positions"],
        "exit_management_status": exit_status["exit_management_status"],
        "premarket_exit_health_state": PREMARKET_PENDING_FIRST_EXIT_CYCLE if premarket_exit_pending else "",
        "historical_pilot_symbols": historical_symbols,
        "local_broker_reconciliation": (
            "clean"
            if broker["open_broker_orders"] == 0
            and broker["unknown_positions"] == 0
            and broker["broker_positions_total"] != "unknown"
            else "blocked_or_unknown"
        ),
        "current_git_commit": git["head"],
        "origin_synchronization": git["origin_sync"],
        "working_tree_clean": git["working_tree_clean"],
        "live_enabled_strategies": live_strategies,
        "strategy_states": states,
        "options_active": options_active,
        "order_intents_today": order_intents,
        "active_submission_reservations": active_reservations,
        "current_day_active_reservations": active_reservations,
        "released_reservations_today": released_reservations,
        "broker_dispatch_attempts_today": broker_dispatch_attempts,
        "current_day_dispatch_attempts": broker_dispatch_attempts,
        "submissions_today": submissions_today,
        "current_day_submissions": submissions_today,
        "submitted_orders_today": int(counts.get("submitted_orders", counts.get("unique_submitted_orders", 0)) or 0),
        "broker_accepted_orders_local": int(counts.get("broker_accepted_orders_local", 0) or 0),
        "broker_accepted_orders_reconciled": reconciled_accepted_orders,
        "accepted_entry_orders_today": accepted_entry_orders,
        "local_fill_events": int(counts.get("raw_local_fill_events", 0) or 0),
        "broker_reconciled_fill_events": reconciled_fill_events,
        "fills_today": entry_fills,
        "local_positions_today": int(counts.get("local_positions_today", 0) or 0),
        "broker_reconciled_positions_today": reconciled_positions,
        "positions_today": open_positions,
        "entry_lock_state": derived_entry_lock,
        "entry_lock_reason": entry_lock_reason,
        "false_or_stale_lock_detected": false_or_stale_lock,
        "rollover_status": (
            "stale_prior_day_reservation_classified"
            if stale_prior_day_reservations
            else "ambiguous_legacy_state_classified"
            if ambiguous_legacy_reservations
            else "current_day_state"
            if current_day_state_loaded
            else "no_current_day_state"
        ),
        "lifecycle_evidence_summary": {
            "legacy_entry_submissions": legacy_entry_submissions,
            "broker_dispatch_attempts": broker_dispatch_attempts,
            "accepted_entry_orders": accepted_entry_orders,
            "fills": entry_fills,
            "positions": open_positions,
            "raw_submitted_order_events": int(counts.get("raw_submitted_order_events", 0) or 0),
            "unique_submitted_orders": int(counts.get("unique_submitted_orders", 0) or 0),
            "raw_broker_accepted_order_events": int(counts.get("raw_broker_accepted_order_events", 0) or 0),
            "raw_fill_events": int(counts.get("raw_fill_events", 0) or 0),
            "recovered_broker_order_snapshots": int(counts.get("recovered_broker_order_snapshots", 0) or 0),
            "recovered_broker_fill_events": int(counts.get("recovered_broker_fill_events", 0) or 0),
        },
        "lifecycle_reconciliation_status": "broker_reconciled" if reconciled_fill_events or reconciled_positions else "local_only",
        "missing_local_lifecycle_evidence": bool(broker.get("pilot_managed_positions") and not local_entry_fills),
        "broker_recovery_performed": bool(reconciled_fill_events or reconciled_positions),
        "broker_recovery_method": "broker_reconciliation" if reconciled_fill_events or reconciled_positions else "",
        "unresolved_contamination": int(counts.get("unresolved_contamination", 0) or 0),
        "runtime_exceptions": int(counts.get("runtime_exception_count", 0) or 0),
        "integrity_incidents": int(counts.get("integrity_incident_count", 0) or 0),
        "max_trades": limits.max_trades_per_day,
        "max_positions": limits.max_open_positions,
        "max_notional": limits.max_notional_per_trade,
        "total_deployed_notional_cap": limits.max_total_deployed_notional,
        "daily_loss_cap": limits.max_daily_loss_usd,
        "normal_max_managed_positions": controlled_limits.max_managed_positions,
        "normal_per_order_max_notional": controlled_limits.per_order_max_notional,
        "normal_per_order_max_pct": controlled_limits.per_order_max_pct,
        "normal_per_symbol_max_pct": controlled_limits.per_symbol_max_pct,
        "normal_strategy_allocation_cap_pct": controlled_limits.strategy_allocation_cap_pct,
        "normal_portfolio_exposure_cap_pct": controlled_limits.portfolio_exposure_cap_pct,
        "normal_stock_capital_pct": controlled_limits.stock_capital_pct,
        "normal_min_cash_reserve_pct": controlled_limits.min_cash_reserve_pct,
        "normal_daily_loss_limit_pct": controlled_limits.daily_loss_limit_pct,
        "eod_flatten_required": limits.eod_flatten_required,
        "fail_closed_enabled": True,
        "blocking_reasons": sorted(set(blocking)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default="live_bot")
    parser.add_argument("--date", required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args(argv)
    report = build_limited_live_readiness(root=args.project_root, user=args.user, day=args.date)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"Limited Live Readiness - {report['date']} user={report['user']}")
        for key, value in report.items():
            if key in {"user", "date"}:
                continue
            if isinstance(value, (dict, list)):
                value = json.dumps(value, sort_keys=True)
            print(f"- {key}: {value}")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
