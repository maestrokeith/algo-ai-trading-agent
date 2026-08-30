#!/usr/bin/env python3
"""Render a read-only AlgoSphere operating diagnostics report."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.brokers.alpaca_client import AlpacaBroker
from src.config_loader import load_app_config
from src.debug_report_cleanup import cleanup_debug_reports
from src.premarket_readiness import PremarketReadiness, check_premarket_readiness


SERVICE_UNITS = (
    "algo.service",
    "algosphere-premarket.service",
    "algosphere-ops-premarket-ready.service",
)
PROVIDER_ORDER = (
    "alpaca",
    "sec",
    "reddit",
    "twitter",
    "newsapi",
    "earnings_overnight",
    "benzinga",
)
PREMARKET_FILES = (
    "latest_rankings.json",
    "latest_catalysts.json",
    "latest_event_feed.json",
    "provider_diagnostics_latest.json",
    "social_sentiment_latest.json",
)
IMPORTANT_LOG_PATTERNS = (
    "ERROR",
    "WARNING",
    "CRITICAL",
    "PREMARKET",
    "PROVIDER",
    "STARTUP",
    "ORDER",
    "REJECT",
    "BROKER",
    "RATE_LIMIT",
    "rate_limited",
    "missing",
    "failed",
)


def _run_command(args: Sequence[str], *, cwd: Path | None = None, timeout: float = 5.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            list(args),
            cwd=str(cwd) if cwd is not None else None,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"
    return int(proc.returncode), proc.stdout.strip(), proc.stderr.strip()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt_money(value: Any) -> str:
    return f"{_safe_float(value):.2f}"


def _fmt_count(value: Any) -> str:
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "0"


def _mapping_get(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _timestamp_text(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _age_minutes(path: Path, *, now: datetime) -> float | None:
    if not path.exists():
        return None
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return max(0.0, (now_utc.astimezone(timezone.utc) - mtime).total_seconds() / 60.0)


def collect_system(project_root: Path, config: Mapping[str, Any], user: str, now: datetime) -> dict[str, str]:
    rc, commit, _ = _run_command(("git", "rev-parse", "--short", "HEAD"), cwd=project_root)
    uptime_rc, uptime, _ = _run_command(("uptime", "-p"))
    broker_cfg = config.get("broker") if isinstance(config.get("broker"), Mapping) else {}
    paper = bool(broker_cfg.get("paper", True)) if isinstance(broker_cfg, Mapping) else True
    return {
        "timestamp": now.astimezone(timezone.utc).isoformat(),
        "git_commit": commit if rc == 0 and commit else "unknown",
        "uptime": uptime if uptime_rc == 0 and uptime else "unknown",
        "hostname": socket.gethostname(),
        "mode": "paper" if paper else "live",
        "active_user": user,
    }


def collect_services(units: Sequence[str] = SERVICE_UNITS) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for unit in units:
        rc, stdout, stderr = _run_command(
            (
                "systemctl",
                "show",
                unit,
                "--property=ActiveState,SubState,Result,ExecMainStatus,ActiveEnterTimestamp,InactiveEnterTimestamp",
                "--no-pager",
            )
        )
        if rc != 0:
            out[unit] = {
                "status": "unavailable",
                "sub_state": "unknown",
                "last_start": "unknown",
                "exit_code": "unknown",
                "result": stderr or "systemctl_unavailable",
            }
            continue
        fields = dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line)
        exit_code = fields.get("ExecMainStatus") or "unknown"
        result = fields.get("Result") or "unknown"
        out[unit] = {
            "status": fields.get("ActiveState") or "unknown",
            "sub_state": fields.get("SubState") or "unknown",
            "last_start": fields.get("ActiveEnterTimestamp") or fields.get("InactiveEnterTimestamp") or "unknown",
            "exit_code": exit_code,
            "result": result,
        }
    return out


def collect_broker(config: Mapping[str, Any]) -> dict[str, Any]:
    try:
        broker = AlpacaBroker(config)
        account = broker.get_account_snapshot() or {}
        buying_power = broker.get_buying_power()
        positions = broker.get_positions() or []
        try:
            orders = broker.list_orders(status="all") or []
        except Exception:
            orders = broker.list_orders(status="open") or []
        return {
            "available": True,
            "account": dict(account) if isinstance(account, Mapping) else {},
            "buying_power": buying_power,
            "positions": positions,
            "orders": orders,
            "error": None,
        }
    except Exception as exc:
        return {
            "available": False,
            "account": {},
            "buying_power": None,
            "positions": [],
            "orders": [],
            "error": f"{type(exc).__name__}: {exc}",
        }


def _latest_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    candidates = sorted(path.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for candidate in candidates:
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8") or "{}")
        except Exception:
            continue
        if isinstance(payload, Mapping):
            data = dict(payload)
            data["_path"] = str(candidate)
            return data
    return None


def collect_dynamic_scan(project_root: Path) -> dict[str, Any]:
    payload = _latest_json(project_root / "data" / "dynamic_scan_history")
    if payload is None:
        return {
            "available": False,
            "candidates_scanned": 0,
            "accepted": 0,
            "rejected": 0,
            "rejection_summary": {},
            "accepted_symbols": [],
            "recent_rejections": [],
        }
    counts = payload.get("counts") if isinstance(payload.get("counts"), Mapping) else {}
    accepted_rows = payload.get("accepted") if isinstance(payload.get("accepted"), list) else []
    rejected_rows = payload.get("rejected") if isinstance(payload.get("rejected"), list) else []
    rejection_summary = payload.get("analytics", {}).get("rejections") if isinstance(payload.get("analytics"), Mapping) else None
    if not isinstance(rejection_summary, Mapping):
        rejection_summary = Counter(
            str(row.get("rejection_reason") or row.get("reason") or "unknown")
            for row in rejected_rows
            if isinstance(row, Mapping)
        )
    recent_rejections = []
    for row in list(rejected_rows)[-20:]:
        if not isinstance(row, Mapping):
            continue
        recent_rejections.append(
            {
                "symbol": str(row.get("symbol") or row.get("ticker") or "unknown"),
                "reason": str(row.get("rejection_reason") or row.get("reason") or "unknown"),
            }
        )
    return {
        "available": True,
        "path": payload.get("_path"),
        "candidates_scanned": counts.get("candidates", len(accepted_rows) + len(rejected_rows)),
        "accepted": counts.get("accepted", len(accepted_rows)),
        "rejected": counts.get("rejected", len(rejected_rows)),
        "rejection_summary": dict(rejection_summary),
        "accepted_symbols": [
            str(row.get("symbol") or row.get("ticker")).upper()
            for row in accepted_rows
            if isinstance(row, Mapping) and (row.get("symbol") or row.get("ticker"))
        ][:20],
        "recent_rejections": recent_rejections,
    }


def collect_scheduler(project_root: Path) -> dict[str, str]:
    log_lines = []
    for path in sorted((project_root / "data" / "logs").glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]:
        try:
            log_lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines()[-300:])
        except OSError:
            continue
    entry = next((line for line in reversed(log_lines) if "ENTRY" in line or "entry lane" in line.lower()), "unknown")
    exit_ = next((line for line in reversed(log_lines) if "EXIT" in line or "exit lane" in line.lower()), "unknown")
    return {
        "last_entry_lane_run": entry,
        "last_exit_lane_run": exit_,
        "next_expected_entry_window": "market hours; see entries config",
        "next_expected_exit_window": "continuous while positions are open",
    }


def collect_recent_log_events(units: Sequence[str] = ("algo.service", "algosphere-premarket.service")) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for unit in units:
        rc, stdout, stderr = _run_command(("journalctl", "-u", unit, "-n", "250", "--no-pager"), timeout=8.0)
        if rc != 0:
            out[unit] = [f"unavailable: {stderr or 'journalctl_failed'}"]
            continue
        lines = [
            line
            for line in stdout.splitlines()
            if any(pattern in line for pattern in IMPORTANT_LOG_PATTERNS)
        ]
        out[unit] = lines[-50:] if lines else ["no important recent lines found"]
    return out


def collect_files(project_root: Path, now: datetime) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for name in PREMARKET_FILES:
        path = project_root / "data" / "premarket" / name
        age = _age_minutes(path, now=now)
        out[name] = {
            "path": str(path),
            "status": "present" if path.exists() else "missing",
            "age_minutes": "n/a" if age is None else f"{age:.1f}",
        }
    return out


def _position_exposures(positions: Sequence[Any]) -> tuple[float, float]:
    gross = 0.0
    net = 0.0
    for row in positions:
        mv = _safe_float(_mapping_get(row, "market_value"))
        qty = _safe_float(_mapping_get(row, "qty"))
        side = str(_mapping_get(row, "side", "long")).lower()
        signed = -abs(mv) if side == "short" or qty < 0 else mv
        gross += abs(mv)
        net += signed
    return gross, net


def _order_rows(orders: Sequence[Any]) -> list[dict[str, str]]:
    rows = []
    for order in list(orders)[-20:]:
        rows.append(
            {
                "timestamp": _timestamp_text(_mapping_get(order, "submitted_at", _mapping_get(order, "created_at"))),
                "symbol": str(_mapping_get(order, "symbol", "unknown")),
                "side": str(_mapping_get(order, "side", "unknown")),
                "qty": str(_mapping_get(order, "qty", "unknown")),
                "status": str(_mapping_get(order, "status", "unknown")),
            }
        )
    return rows


def _provider_failed(row: Mapping[str, Any]) -> bool:
    if bool(row.get("rate_limited")):
        return True
    status = row.get("http_status")
    try:
        if status is not None and int(status) >= 400:
            return True
    except (TypeError, ValueError):
        pass
    reason = str(row.get("reason") or "").lower()
    tolerated = {
        "",
        "ok",
        "disabled",
        "newsapi_disabled",
        "twitter_disabled",
        "benzinga_disabled",
        "social_disabled",
        "reddit_credentials_missing",
        "depends_on_newsapi_disabled",
        "no_articles",
        "no_mentions",
    }
    return "error" in reason or "failed" in reason or (reason not in tolerated and "missing" in reason)


def determine_health(
    *,
    services: Mapping[str, Mapping[str, str]],
    broker: Mapping[str, Any],
    readiness: PremarketReadiness,
    files: Mapping[str, Mapping[str, str]],
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    algo = services.get("algo.service") or {}
    if algo.get("status") == "unavailable":
        reasons.append("services_unavailable")
    elif algo.get("status") != "active":
        reasons.append("service_down:algo.service")
    for unit, row in services.items():
        if row.get("status") == "unavailable":
            if "services_unavailable" not in reasons:
                reasons.append("services_unavailable")
            continue
        if row.get("status") == "failed" or row.get("result") not in {None, "", "success", "unknown"}:
            reasons.append(f"service_failure:{unit}:{row.get('result')}")
    if not bool(broker.get("available")):
        reasons.append("broker_unavailable")
    if readiness.status in {"missing", "unreadable"}:
        reasons.append(f"premarket_{readiness.status}")
    missing_files = [name for name, row in files.items() if row.get("status") == "missing"]
    if missing_files:
        reasons.append("missing_artifacts:" + ",".join(missing_files))
    provider_failures = [
        name for name, row in readiness.provider_diagnostics.items()
        if isinstance(row, Mapping) and _provider_failed(row)
    ]
    if provider_failures:
        reasons.append("provider_failures:" + ",".join(sorted(provider_failures)))
    stale_diag = files.get("provider_diagnostics_latest.json", {}).get("age_minutes")
    try:
        if stale_diag not in {None, "n/a"} and float(stale_diag) > 24 * 60:
            reasons.append("stale_diagnostics")
    except (TypeError, ValueError):
        pass
    if any(reason.startswith(("service_down", "broker_unavailable", "premarket_missing", "premarket_unreadable", "missing_artifacts")) for reason in reasons):
        return "RED", reasons
    if readiness.status == "fresh_empty":
        reasons.append("premarket_fresh_empty")
    if reasons:
        return "YELLOW", reasons
    return "GREEN", ["services_running_readiness_fresh"]


def render_report(
    *,
    project_root: Path,
    config: Mapping[str, Any],
    user: str,
    now: datetime,
    system: Mapping[str, str] | None = None,
    services: Mapping[str, Mapping[str, str]] | None = None,
    broker: Mapping[str, Any] | None = None,
    readiness: PremarketReadiness | None = None,
    dynamic_scan: Mapping[str, Any] | None = None,
    scheduler: Mapping[str, str] | None = None,
    log_events: Mapping[str, Sequence[str]] | None = None,
    files: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    system = system or collect_system(project_root, config, user, now)
    services = services or collect_services()
    broker = broker or collect_broker(config)
    readiness = readiness or check_premarket_readiness(project_root, now=now)
    dynamic_scan = dynamic_scan or collect_dynamic_scan(project_root)
    scheduler = scheduler or collect_scheduler(project_root)
    log_events = log_events or collect_recent_log_events()
    files = files or collect_files(project_root, now)
    health, health_reasons = determine_health(services=services, broker=broker, readiness=readiness, files=files)

    account = broker.get("account") if isinstance(broker.get("account"), Mapping) else {}
    positions = broker.get("positions") if isinstance(broker.get("positions"), Sequence) else []
    orders = broker.get("orders") if isinstance(broker.get("orders"), Sequence) else []
    gross, net = _position_exposures(positions)

    lines: list[str] = [
        "==================================================",
        "ALGOSPHERE DIAGNOSTICS",
        "==================================================",
        "",
        "SYSTEM",
    ]
    for key in ("timestamp", "git_commit", "uptime", "hostname", "mode", "active_user"):
        lines.append(f"- {key.replace('_', ' ')}: {system.get(key, 'unknown')}")

    lines.extend(["", "SERVICES"])
    for unit in SERVICE_UNITS:
        row = services.get(unit, {})
        lines.append(
            f"- {unit}: status={row.get('status', 'unknown')} sub_state={row.get('sub_state', 'unknown')} "
            f"last_start={row.get('last_start', 'unknown')} exit_code={row.get('exit_code', 'unknown')} result={row.get('result', 'unknown')}"
        )

    lines.extend(["", "ACCOUNT"])
    if broker.get("available"):
        lines.extend(
            [
                f"- equity: {_fmt_money(account.get('equity'))}",
                f"- buying power: {_fmt_money(broker.get('buying_power'))}",
                f"- cash: {_fmt_money(account.get('cash'))}",
                f"- gross exposure: {_fmt_money(gross)}",
                f"- net exposure: {_fmt_money(net)}",
                f"- open positions count: {len(positions)}",
            ]
        )
    else:
        lines.append(f"- unavailable: {broker.get('error') or 'unknown'}")

    lines.extend(["", "POSITIONS"])
    if positions:
        for row in positions:
            lines.append(
                f"- {_mapping_get(row, 'symbol', 'unknown')}: qty={_mapping_get(row, 'qty', 'unknown')} "
                f"market_value={_fmt_money(_mapping_get(row, 'market_value'))} "
                f"unrealized_pnl={_fmt_money(_mapping_get(row, 'unrealized_pl'))}"
            )
    else:
        lines.append("- none")

    lines.extend(
        [
            "",
            "PREMARKET",
            f"- readiness status: {readiness.status}",
            f"- artifact freshness: fresh={str(readiness.fresh).lower()} max_age_minutes={readiness.max_age_minutes if readiness.max_age_minutes is not None else 'n/a'}",
            f"- ranked symbols count: {readiness.catalyst_ranked_symbols}",
            f"- catalyst count: {readiness.catalyst_count}",
            f"- event count: {readiness.event_count}",
            "",
            "PROVIDERS",
        ]
    )
    for provider in PROVIDER_ORDER:
        row = readiness.provider_diagnostics.get(provider, {})
        lines.append(
            f"- {provider}: enabled={row.get('enabled', 'unknown')} request_sent={row.get('request_sent', 'unknown')} "
            f"http_status={row.get('http_status', 'none')} raw_count={_fmt_count(row.get('raw_count'))} "
            f"filtered_count={_fmt_count(row.get('filtered_count'))} reason={row.get('reason', 'missing_diagnostics')}"
        )

    lines.extend(
        [
            "",
            "DYNAMIC SCAN",
            f"- candidates scanned: {dynamic_scan.get('candidates_scanned', 0)}",
            f"- accepted: {dynamic_scan.get('accepted', 0)}",
            f"- rejected: {dynamic_scan.get('rejected', 0)}",
            f"- rejection summary: {json.dumps(dynamic_scan.get('rejection_summary', {}), sort_keys=True)}",
            f"- latest accepted symbols: {', '.join(dynamic_scan.get('accepted_symbols', []) or []) or 'none'}",
            "",
            "ENTRY SCHEDULER",
            f"- last entry lane run: {scheduler.get('last_entry_lane_run', 'unknown')}",
            f"- last exit lane run: {scheduler.get('last_exit_lane_run', 'unknown')}",
            f"- next expected entry window: {scheduler.get('next_expected_entry_window', 'unknown')}",
            f"- next expected exit window: {scheduler.get('next_expected_exit_window', 'unknown')}",
            "",
            "RECENT ORDERS",
        ]
    )
    order_rows = _order_rows(orders)
    if order_rows:
        for row in order_rows:
            lines.append(
                f"- {row['timestamp']} {row['symbol']} side={row['side']} qty={row['qty']} status={row['status']}"
            )
    else:
        lines.append("- none")

    lines.extend(["", "RECENT REJECTIONS"])
    recent_rejections = dynamic_scan.get("recent_rejections", []) if isinstance(dynamic_scan.get("recent_rejections"), list) else []
    if recent_rejections:
        for row in recent_rejections[-20:]:
            lines.append(f"- {row.get('symbol', 'unknown')}: {row.get('reason', 'unknown')}")
    else:
        lines.append("- none")

    lines.extend(["", "RECENT LOG EVENTS"])
    for unit in ("algo.service", "algosphere-premarket.service"):
        lines.append(f"- {unit}:")
        for line in list(log_events.get(unit, []))[-50:]:
            lines.append(f"  {line}")

    lines.extend(["", "FILES"])
    for name in PREMARKET_FILES:
        row = files.get(name, {})
        lines.append(f"- {name}: status={row.get('status', 'unknown')} age_minutes={row.get('age_minutes', 'n/a')}")

    lines.extend(["", "HEALTH SUMMARY", f"HEALTH={health}", f"- reasons: {', '.join(health_reasons)}"])
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render read-only AlgoSphere diagnostics.")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--config", type=Path, default=None, help="Config path; defaults to config/default.yaml.")
    parser.add_argument("--user", default=os.getenv("ALGOSPHERE_USER") or os.getenv("ALGO_USER") or "live_bot")
    parser.add_argument("--no-artifact", action="store_true", help="Print only; do not write data/diagnostics/latest_diagnostics.txt.")
    parser.add_argument("--retention-days", type=int, default=5, help="Delete old reports/debug artifacts older than this many days.")
    parser.add_argument("--no-cleanup", action="store_true", help="Skip reports/debug retention cleanup.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = args.project_root.resolve()
    config_path = args.config or project_root / "config" / "default.yaml"
    config = load_app_config(config_path)
    now = datetime.now(timezone.utc)
    report = render_report(project_root=project_root, config=config, user=args.user, now=now)
    if not args.no_artifact:
        artifact_path = project_root / "data" / "diagnostics" / "latest_diagnostics.txt"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(report, encoding="utf-8")
    cleanup_events = cleanup_debug_reports(
        project_root,
        retention_days=args.retention_days,
        now=now,
        enabled=not args.no_cleanup,
    )
    for event in cleanup_events:
        print(event.log_line(), file=sys.stderr)
    print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
