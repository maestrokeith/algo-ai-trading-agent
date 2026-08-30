#!/usr/bin/env python3
"""Generate a lightweight weekly review from daily trading journal reports."""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def _week_start_today_et() -> str:
    today = datetime.now(ZoneInfo("America/New_York")).date()
    monday = today - timedelta(days=today.weekday())
    return monday.isoformat()


def _extract_value(text: str, label: str) -> str:
    match = re.search(rf"^- {re.escape(label)}: (.+)$", text, flags=re.MULTILINE)
    return match.group(1).strip() if match else "not available"


def _to_int(value: str) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_daily_reports(project_root: Path, week_start: date) -> list[tuple[date, str]]:
    daily_dir = project_root / "reports" / "daily"
    if not daily_dir.exists():
        return []
    week_end = week_start + timedelta(days=6)
    rows: list[tuple[date, str]] = []
    for path in sorted(daily_dir.glob("*.md")):
        try:
            day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if week_start <= day <= week_end:
            try:
                rows.append((day, path.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
    return rows


def build_weekly_review(project_root: Path, *, week_start: str) -> dict[str, object]:
    start = date.fromisoformat(week_start)
    reports = _read_daily_reports(project_root, start)
    total_entries = 0
    total_exits = 0
    total_candidates = 0
    realized_pnl = 0.0
    realized_available = False
    dynamic_activity = 0
    core_activity = 0
    issues: Counter[str] = Counter()
    reject_reasons: Counter[str] = Counter()
    missing_count = 0
    for _, text in reports:
        for label, accumulator in (
            ("entries count", "entries"),
            ("exits count", "exits"),
            ("candidates count", "candidates"),
            ("dynamic activity", "dynamic"),
            ("core activity", "core"),
        ):
            value = _to_int(_extract_value(text, label))
            if value is None:
                missing_count += 1
                continue
            if accumulator == "entries":
                total_entries += value
            elif accumulator == "exits":
                total_exits += value
            elif accumulator == "candidates":
                total_candidates += value
            elif accumulator == "dynamic":
                dynamic_activity += value
            elif accumulator == "core":
                core_activity += value
        pnl = _to_float(_extract_value(text, "realized PnL"))
        if pnl is not None:
            realized_available = True
            realized_pnl += pnl
        in_rejects = False
        in_issues = False
        for line in text.splitlines():
            if line.strip() == "## Top Reject Reasons":
                in_rejects = True
                in_issues = False
                continue
            if line.strip() == "## Operational Issues":
                in_issues = True
                in_rejects = False
                continue
            if line.startswith("## "):
                in_rejects = False
                in_issues = False
            if in_rejects and line.startswith("- ") and "not available" not in line:
                reason = line[2:].split(":", 1)[0].strip()
                reject_reasons[reason] += 1
            if in_issues and line.startswith("- ") and line.strip() != "- none found":
                issue = line[2:].strip()
                normalized = re.sub(r"\s+", " ", issue)[:120]
                issues[normalized] += 1
    top_issues = [issue for issue, _ in issues.most_common(3)]
    while len(top_issues) < 3:
        if missing_count:
            top_issues.append("Improve daily artifact availability and completeness")
            missing_count = 0
        elif not reports:
            top_issues.append("Generate daily summaries before weekly review")
        else:
            top_issues.append("No additional issue found")
    return {
        "week_start": week_start,
        "days": len(reports),
        "total_entries": total_entries if reports else None,
        "total_exits": total_exits if reports else None,
        "total_candidates": total_candidates if reports else None,
        "realized_pnl": realized_pnl if realized_available else None,
        "dynamic_activity": dynamic_activity if reports else None,
        "core_activity": core_activity if reports else None,
        "top_reject_reasons": reject_reasons.most_common(5),
        "top_issues": top_issues[:3],
    }


def _fmt(value: object) -> str:
    if value is None:
        return "not available"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def render_weekly_review(review: Mapping[str, object]) -> str:
    reject_reasons = review.get("top_reject_reasons")
    if reject_reasons:
        reject_text = "\n".join(f"- {reason}: {count}" for reason, count in reject_reasons)
    else:
        reject_text = "- not available"
    issues = review.get("top_issues")
    issue_text = "\n".join(f"{idx}. {issue}" for idx, issue in enumerate(issues or [], start=1))
    return (
        f"# Weekly Trading Review week_{str(review['week_start']).replace('-', '_')}\n\n"
        "## Reliability Summary\n\n"
        f"- daily reports found: {_fmt(review.get('days'))}\n"
        f"- operational issue focus: {(issues or ['not available'])[0]}\n\n"
        "## Trading Summary\n\n"
        f"- candidates: {_fmt(review.get('total_candidates'))}\n"
        f"- entries: {_fmt(review.get('total_entries'))}\n"
        f"- exits: {_fmt(review.get('total_exits'))}\n"
        f"- realized PnL: {_fmt(review.get('realized_pnl'))}\n\n"
        "## Dynamic Engine Summary\n\n"
        f"- dynamic activity: {_fmt(review.get('dynamic_activity'))}\n"
        f"- core activity: {_fmt(review.get('core_activity'))}\n"
        "- top reject reasons:\n"
        f"{reject_text}\n\n"
        "## Options Paper Summary\n\n"
        "- not available\n\n"
        "## Top 3 Issues To Fix Next Week\n\n"
        f"{issue_text}\n"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a weekly trading journal review from daily summaries.")
    parser.add_argument("--week-start", default=_week_start_today_et(), help="Week start date YYYY-MM-DD. Defaults to current Monday in US/Eastern.")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        date.fromisoformat(args.week_start)
    except ValueError:
        print("Use --week-start YYYY-MM-DD", file=sys.stderr)
        return 2
    project_root = args.project_root.resolve()
    review = build_weekly_review(project_root, week_start=args.week_start)
    out_path = project_root / "reports" / "weekly" / f"week_{args.week_start.replace('-', '_')}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_weekly_review(review), encoding="utf-8")
    print(f"WEEKLY_REVIEW path={out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
