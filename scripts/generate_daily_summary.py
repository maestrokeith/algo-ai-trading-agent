#!/usr/bin/env python3
"""Generate a lightweight read-only trading journal summary for one day."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

JOURNAL_COLUMNS = (
    "date",
    "candidates",
    "entries",
    "exits",
    "winners",
    "losers",
    "realized_pnl",
    "top_reject_reason",
    "notes",
)
OPERATIONAL_MARKERS = (
    "ERROR",
    "Traceback",
    "APIError",
    "stale",
    "missing artifact",
    "missing_artifacts",
    "missing artifacts",
    "artifact missing",
    "unreadable",
    "failed",
)


def _today_et() -> str:
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def _load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _fmt_value(value: Any) -> str:
    if value is None:
        return "not available"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _date_compact(day: str) -> str:
    return day.replace("-", "")


def _matches_day(path: Path, day: str) -> bool:
    compact = _date_compact(day)
    return day in path.name or compact in path.name


def _read_log_lines(project_root: Path, day: str) -> list[str]:
    candidates: list[Path] = []
    for root in (
        project_root / "data" / "logs",
        project_root / "logs",
        project_root / "reports" / "debug",
    ):
        if root.exists():
            candidates.extend(path for path in root.glob("*.log") if path.is_file())
    lines: list[str] = []
    for path in sorted(candidates):
        if not (_matches_day(path, day) or "latest" in path.name or path.parent.name in {"logs"}):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if day in line or _date_compact(day) in line or not re.search(r"\d{4}-\d{2}-\d{2}", line):
                lines.append(line)
    return lines


def _dynamic_scan_summary(project_root: Path, day: str) -> dict[str, Any]:
    history_dir = project_root / "data" / "dynamic_scan_history"
    if not history_dir.exists():
        return {"available": False}
    candidates = [path for path in history_dir.glob("*.json") if _matches_day(path, day)]
    if not candidates:
        return {"available": False}
    total_candidates = 0
    accepted = 0
    rejected = 0
    reject_reasons: Counter[str] = Counter()
    for path in candidates:
        payload = _load_json(path)
        if not isinstance(payload, Mapping):
            continue
        counts = payload.get("counts") if isinstance(payload.get("counts"), Mapping) else {}
        accepted_rows = payload.get("accepted") if isinstance(payload.get("accepted"), list) else []
        rejected_rows = payload.get("rejected") if isinstance(payload.get("rejected"), list) else []
        total_candidates += int(counts.get("candidates") or len(accepted_rows) + len(rejected_rows) or 0)
        accepted += int(counts.get("accepted") or len(accepted_rows) or 0)
        rejected += int(counts.get("rejected") or len(rejected_rows) or 0)
        analytics = payload.get("analytics") if isinstance(payload.get("analytics"), Mapping) else {}
        analytics_rejections = analytics.get("rejections") if isinstance(analytics.get("rejections"), Mapping) else {}
        if analytics_rejections:
            for reason, count in analytics_rejections.items():
                try:
                    reject_reasons[str(reason)] += int(count)
                except (TypeError, ValueError):
                    reject_reasons[str(reason)] += 1
        else:
            for row in rejected_rows:
                if isinstance(row, Mapping):
                    reject_reasons[str(row.get("rejection_reason") or row.get("reason") or "unknown")] += 1
    return {
        "available": True,
        "candidates": total_candidates,
        "accepted": accepted,
        "rejected": rejected,
        "top_reject_reasons": reject_reasons.most_common(5),
    }


def _daily_attribution(project_root: Path, day: str, user: str) -> Mapping[str, Any] | None:
    roots = (
        project_root / "data" / "trade_attribution" / "daily",
        project_root / "data" / "profitability_attribution" / "daily",
        project_root / "data" / "order_history",
        project_root / "data" / "orders",
    )
    for root in roots:
        if not root.exists():
            continue
        matches = sorted(path for path in root.glob("*.json") if day in path.name and user in path.name)
        if not matches:
            matches = sorted(path for path in root.glob("*.json") if day in path.name)
        for path in matches:
            payload = _load_json(path)
            if isinstance(payload, Mapping):
                return payload
    return None


def _rows(payload: Mapping[str, Any] | None, keys: Sequence[str]) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, Mapping)]
    return []


def _pnl_summary(project_root: Path, day: str, user: str, attribution: Mapping[str, Any] | None) -> dict[str, Any]:
    exits = _rows(attribution, ("exits", "trades", "filled_orders", "orders"))
    pnl_values: list[float] = []
    for row in exits:
        side = str(row.get("side") or row.get("action") or "").lower()
        if side and side not in {"sell", "exit", "closed"} and "pnl" not in row and "realized_pnl" not in row:
            continue
        pnl = _safe_float(row.get("pnl", row.get("realized_pnl")))
        if pnl is not None:
            pnl_values.append(pnl)
    daily_summary_roots = (
        project_root / "data" / "daily_summary",
        project_root / "data" / "reports",
    )
    unrealized: float | None = None
    for root in daily_summary_roots:
        if not root.exists():
            continue
        for path in sorted(root.glob(f"*{day}*{user}*.json")) + sorted(root.glob(f"*{day}*.json")):
            payload = _load_json(path)
            if isinstance(payload, Mapping):
                unrealized = _safe_float(payload.get("unrealized_pnl", payload.get("unrealized_pl")))
                if unrealized is not None:
                    break
        if unrealized is not None:
            break
    return {
        "realized_pnl": sum(pnl_values) if pnl_values else None,
        "unrealized_pnl": unrealized,
        "winners": sum(1 for value in pnl_values if value > 0) if pnl_values else None,
        "losers": sum(1 for value in pnl_values if value < 0) if pnl_values else None,
    }


def _activity_summary(attribution: Mapping[str, Any] | None, log_lines: Sequence[str]) -> dict[str, Any]:
    orders = _rows(attribution, ("orders", "trades", "filled_orders"))
    entries = 0
    exits = 0
    dynamic = 0
    core = 0
    for row in orders:
        action = str(row.get("action") or row.get("side") or "").lower()
        route = " ".join(str(row.get(key) or "") for key in ("route", "source", "entry_route", "strategy")).lower()
        if action == "buy":
            entries += 1
        if action == "sell":
            exits += 1
        if "dynamic" in route:
            dynamic += 1
        if "core" in route:
            core += 1
    if entries == 0:
        entries = sum(1 for line in log_lines if " BUY " in f" {line} " or " action=buy" in line.lower())
    if exits == 0:
        exits = sum(1 for line in log_lines if " SELL " in f" {line} " or " action=sell" in line.lower() or "EXIT_" in line)
    if dynamic == 0:
        dynamic = sum(1 for line in log_lines if "DYNAMIC_" in line or "dynamic" in line.lower())
    if core == 0:
        core = sum(1 for line in log_lines if "CORE_REBUILD" in line or "core_rebuild" in line.lower())
    return {
        "entries": entries if entries else None,
        "exits": exits if exits else None,
        "dynamic_activity": dynamic if dynamic else None,
        "core_activity": core if core else None,
    }


def _operational_issues(log_lines: Sequence[str], project_root: Path) -> list[str]:
    issues: list[str] = []
    for line in log_lines:
        lower = line.lower()
        if any(marker.lower() in lower for marker in OPERATIONAL_MARKERS):
            issues.append(line.strip())
    premarket_dir = project_root / "data" / "premarket"
    for name in ("latest_rankings.json", "latest_catalysts.json", "latest_event_feed.json"):
        path = premarket_dir / name
        if not path.exists():
            issues.append(f"missing artifact: {path}")
    return issues[:20]


def _risk_guard_summary(project_root: Path, day: str, user: str) -> Mapping[str, Any] | None:
    path = project_root / "data" / "risk_guards" / f"{day}_{user}.json"
    payload = _load_json(path)
    return payload if isinstance(payload, Mapping) else None


def build_daily_summary(project_root: Path, *, day: str, user: str) -> dict[str, Any]:
    log_lines = _read_log_lines(project_root, day)
    dynamic_scan = _dynamic_scan_summary(project_root, day)
    attribution = _daily_attribution(project_root, day, user)
    pnl = _pnl_summary(project_root, day, user, attribution)
    activity = _activity_summary(attribution, log_lines)
    issues = _operational_issues(log_lines, project_root)
    risk_guards = _risk_guard_summary(project_root, day, user)
    top_reject = None
    if dynamic_scan.get("available") and dynamic_scan.get("top_reject_reasons"):
        top_reject = dynamic_scan["top_reject_reasons"][0][0]
    return {
        "date": day,
        "user": user,
        "candidates": dynamic_scan.get("candidates") if dynamic_scan.get("available") else None,
        "entries": activity["entries"],
        "exits": activity["exits"],
        "winners": pnl["winners"],
        "losers": pnl["losers"],
        "realized_pnl": pnl["realized_pnl"],
        "unrealized_pnl": pnl["unrealized_pnl"],
        "dynamic_activity": activity["dynamic_activity"],
        "core_activity": activity["core_activity"],
        "top_reject_reasons": dynamic_scan.get("top_reject_reasons") if dynamic_scan.get("available") else None,
        "top_reject_reason": top_reject,
        "operational_issues": issues,
        "risk_guards": risk_guards,
    }


def render_daily_summary(summary: Mapping[str, Any]) -> str:
    reject_reasons = summary.get("top_reject_reasons")
    if reject_reasons:
        reject_text = "\n".join(f"- {reason}: {count}" for reason, count in reject_reasons)
    else:
        reject_text = "- not available"
    issues = summary.get("operational_issues")
    if issues:
        issue_text = "\n".join(f"- {issue}" for issue in issues)
    else:
        issue_text = "- none found"
    risk_guards = summary.get("risk_guards")
    if isinstance(risk_guards, Mapping):
        guard_text = (
            "- triggered: "
            + (", ".join(str(item) for item in risk_guards.get("triggered_guards") or []) or "none")
            + "\n"
            + f"- trend_long_blocked: {str(bool(risk_guards.get('trend_long_entries_blocked'))).lower()}\n"
            + f"- new_entries_blocked: {str(bool(risk_guards.get('new_entries_blocked'))).lower()}\n"
            + f"- flatten_risk: {str(bool(risk_guards.get('flatten_risk'))).lower()}"
        )
    else:
        guard_text = "- none triggered"
    return (
        f"# Trading Journal Daily Summary {summary['date']}\n\n"
        f"- date: {summary['date']}\n"
        f"- user: {summary.get('user', 'not available')}\n"
        f"- candidates count: {_fmt_value(summary.get('candidates'))}\n"
        f"- entries count: {_fmt_value(summary.get('entries'))}\n"
        f"- exits count: {_fmt_value(summary.get('exits'))}\n"
        f"- winners: {_fmt_value(summary.get('winners'))}\n"
        f"- losers: {_fmt_value(summary.get('losers'))}\n"
        f"- realized PnL: {_fmt_value(summary.get('realized_pnl'))}\n"
        f"- unrealized PnL: {_fmt_value(summary.get('unrealized_pnl'))}\n"
        f"- dynamic activity: {_fmt_value(summary.get('dynamic_activity'))}\n"
        f"- core activity: {_fmt_value(summary.get('core_activity'))}\n\n"
        "## Top Reject Reasons\n\n"
        f"{reject_text}\n\n"
        "## Operational Issues\n\n"
        f"{issue_text}\n\n"
        "## Risk Guards\n\n"
        f"{guard_text}\n"
    )


def append_journal_row(project_root: Path, summary: Mapping[str, Any]) -> Path:
    path = project_root / "data" / "analytics" / "trading_journal.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []
    if path.exists():
        with path.open("r", newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    row = {
        "date": str(summary["date"]),
        "candidates": _fmt_value(summary.get("candidates")),
        "entries": _fmt_value(summary.get("entries")),
        "exits": _fmt_value(summary.get("exits")),
        "winners": _fmt_value(summary.get("winners")),
        "losers": _fmt_value(summary.get("losers")),
        "realized_pnl": _fmt_value(summary.get("realized_pnl")),
        "top_reject_reason": _fmt_value(summary.get("top_reject_reason")),
        "notes": "operational issues: " + str(len(summary.get("operational_issues") or [])),
    }
    rows = [existing for existing in rows if existing.get("date") != row["date"]]
    rows.append(row)
    rows.sort(key=lambda item: item.get("date", ""))
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=JOURNAL_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a read-only daily trading journal summary.")
    parser.add_argument("--date", default=_today_et(), help="Trading date YYYY-MM-DD. Defaults to today in US/Eastern.")
    parser.add_argument("--user", default="live_bot")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        date.fromisoformat(args.date)
    except ValueError:
        print("Use --date YYYY-MM-DD", file=sys.stderr)
        return 2
    project_root = args.project_root.resolve()
    summary = build_daily_summary(project_root, day=args.date, user=args.user)
    out_path = project_root / "reports" / "daily" / f"{args.date}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_daily_summary(summary), encoding="utf-8")
    journal_path = append_journal_row(project_root, summary)
    print(f"DAILY_SUMMARY path={out_path}")
    print(f"TRADING_JOURNAL path={journal_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
