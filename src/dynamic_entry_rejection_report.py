"""Read-only dynamic entry rejection explainability report."""

from __future__ import annotations

import gzip
import json
import os
import re
import subprocess
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Sequence

_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_COMPACT_DATE_RE = re.compile(r"(\d{8})")

REJECTION_BUCKETS = (
    "trend_filter",
    "volume_filter",
    "ema_slope",
    "portfolio_cap",
    "replacement_logic",
    "cooldown",
    "momentum_rank",
    "gain_threshold",
    "no_decision",
)

REJECTION_CLASSES = (
    "hard_block",
    "minor_rule_failure",
    "ranked_out",
    "risk_block",
    "data_quality_block",
    "cooldown_block",
)


def _day_text(day: date | str) -> str:
    return day.isoformat() if isinstance(day, date) else str(day)


def _compact_day(day: date | str) -> str:
    return _day_text(day).replace("-", "")


def _date_from_path(path: Path) -> str | None:
    for part in [path.name, *[parent.name for parent in path.parents]]:
        iso = _ISO_DATE_RE.search(part)
        if iso:
            return iso.group(1)
        compact = _COMPACT_DATE_RE.search(part)
        if compact:
            raw = compact.group(1)
            return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return None


def _line_matches_day(line: str, day: str) -> bool:
    compact = day.replace("-", "")
    if day in line or compact in line:
        return True
    return not re.search(r"\d{4}-\d{2}-\d{2}", line)


def _read_text(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    return path.read_text(encoding="utf-8", errors="replace")


def _discover_log_paths(project_root: Path, *, data_dir: Path, day: str, extra_paths: Sequence[Path | str] | None) -> list[Path]:
    paths: list[Path] = []
    for root in (
        data_dir / "review" / day,
        data_dir / "logs",
        data_dir / "debug_logs",
        project_root / "logs",
        project_root / "reports" / "debug",
    ):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".log", ".txt", ".out", ".gz"}:
                continue
            path_day = _date_from_path(path)
            if path_day == day or day in str(path) or _compact_day(day) in str(path):
                paths.append(path)
    for raw in extra_paths or []:
        path = Path(raw)
        if path.exists() and path.is_file():
            paths.append(path)
    return sorted(dict.fromkeys(paths))


def _service_for_user(user_id: str) -> str:
    if user_id == "live_bot":
        return os.environ.get("ALGO_LIVE_SERVICE", "algo.service")
    if user_id == "paper_bot":
        return os.environ.get("ALGO_PAPER_SERVICE", "paper.service")
    return os.environ.get("ALGO_LIVE_SERVICE", "algo.service")


def _journalctl_lines(day: str, *, service: str) -> list[str]:
    try:
        proc = subprocess.run(
            ["journalctl", "-u", service, "--since", f"{day} 00:00:00", "--until", f"{day} 23:59:59", "--no-pager"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return []
    return proc.stdout.splitlines() if proc.returncode == 0 else []


def load_dynamic_entry_rejection_lines(
    *,
    project_root: Path,
    data_dir: Path,
    day: str,
    log_text: str | None = None,
    log_files: Sequence[Path | str] | None = None,
    user_id: str = "live_bot",
) -> list[str]:
    if log_text is not None:
        raw_lines = log_text.splitlines()
    else:
        raw_lines = []
        for path in _discover_log_paths(project_root, data_dir=data_dir, day=day, extra_paths=log_files):
            try:
                raw_lines.extend(_read_text(path).splitlines())
            except OSError:
                continue
        if not raw_lines:
            raw_lines.extend(_journalctl_lines(day, service=_service_for_user(user_id)))
    return [line for line in raw_lines if _line_matches_day(line, day)]


def classify_dynamic_entry_rejection(line: str) -> str | None:
    text = line.lower()
    if not any(marker in text for marker in ("entry_eval", "dynamic", "order_skip", "skip ", "allocator", "portfolio")):
        return None
    if "20 ema slope" in text or "ema_slope" in text:
        return "ema_slope"
    if "trend filter" in text and "ema slope" in text:
        return "ema_slope"
    if "soft_cap" in text or "no buy headroom" in text or "portfolio caps" in text:
        return "portfolio_cap"
    if "portfolio replacement" in text or "replacement_logic" in text:
        return "replacement_logic"
    if "cooldown" in text:
        return "cooldown"
    if "momentum_rank" in text or "top_n" in text or "rank" in text and "momentum" in text:
        return "momentum_rank"
    if "gain_pct" in text or "below_min_day_gain" in text or "min_day_gain" in text:
        return "gain_threshold"
    if "trend=f" in text or "trend filter" in text or "five_min_trend" in text:
        return "trend_filter"
    if (
        "relative_volume" in text
        or " rel_volume" in text
        or " rvol" in text
        or "rvol " in text
        or "vol=f" in text
        or "volume_confirmation" in text
    ):
        return "volume_filter"
    if "no_decision" in text:
        return "no_decision"
    if "final=f" in text or "rejected" in text or "skip" in text:
        return "no_decision"
    return None


def classify_dynamic_entry_rejection_class(line: str) -> str:
    text = line.lower()
    if any(token in text for token in ("bad_quote", "stale", "invalid quote", "market data", "halt")):
        return "data_quality_block"
    if any(token in text for token in ("daily_loss", "portfolio cap", "soft_cap", "no buy headroom", "position_limit", "buying_power")):
        return "risk_block"
    if "cooldown" in text:
        return "cooldown_block"
    if any(token in text for token in ("momentum rank", "ranked_out", "not in top")):
        return "ranked_out"
    if any(token in text for token in ("rvol", "relative_volume", "vwap distance", "ema slope", "sector", "entry_alignment")):
        return "minor_rule_failure"
    if any(token in text for token in ("spread", "market_closed", "unsupported", "broker")):
        return "hard_block"
    return "hard_block"


def build_dynamic_entry_rejection_report(
    *,
    project_root: Path,
    data_dir: Path,
    day: date | str,
    user_id: str = "live_bot",
    log_text: str | None = None,
    log_files: Sequence[Path | str] | None = None,
) -> dict[str, Any]:
    day_s = _day_text(day)
    lines = load_dynamic_entry_rejection_lines(
        project_root=project_root,
        data_dir=data_dir,
        day=day_s,
        log_text=log_text,
        log_files=log_files,
        user_id=user_id,
    )
    counts: Counter[str] = Counter()
    class_counts: Counter[str] = Counter()
    sensitivity_counts: Counter[str] = Counter()
    one_minor_rule_candidates = 0
    examples: dict[str, list[str]] = {bucket: [] for bucket in REJECTION_BUCKETS}
    for line in lines:
        low = line.lower()
        if "dynamic_entry_relaxed_accept" in low:
            sensitivity_counts["relaxed"] += 1
            continue
        if "dynamic_entry_adaptive_config" in low:
            if "mode=relaxed" in low:
                sensitivity_counts["relaxed_context"] += 1
            elif "mode=tight" in low:
                sensitivity_counts["tight_context"] += 1
            elif "mode=normal" in low:
                sensitivity_counts["normal_context"] += 1
        bucket = classify_dynamic_entry_rejection(line)
        if bucket is None:
            continue
        counts[bucket] += 1
        cls = classify_dynamic_entry_rejection_class(line)
        class_counts[cls] += 1
        if cls == "minor_rule_failure":
            one_minor_rule_candidates += 1
        if len(examples[bucket]) < 5:
            examples[bucket].append(line.strip())

    total = sum(counts.values())
    rows = []
    for bucket in REJECTION_BUCKETS:
        count = int(counts.get(bucket, 0))
        rows.append(
            {
                "reason": bucket,
                "count": count,
                "pct": round((100.0 * count / total), 2) if total else 0.0,
                "examples": examples[bucket],
            }
        )
    return {
        "date": day_s,
        "user": user_id,
        "total_rejections": total,
        "counts": {bucket: int(counts.get(bucket, 0)) for bucket in REJECTION_BUCKETS},
        "counts_by_class": {bucket: int(class_counts.get(bucket, 0)) for bucket in REJECTION_CLASSES},
        "counts_by_sensitivity_mode": dict(sorted(sensitivity_counts.items())),
        "candidates_failed_exactly_one_minor_rule": one_minor_rule_candidates,
        "candidates_would_pass_under_relaxed_mode": one_minor_rule_candidates,
        "missed_opportunity_analysis": "available_only_when_forward_prices_are_present",
        "rows": rows,
    }


def render_dynamic_entry_rejection_report(report: dict[str, Any]) -> str:
    lines = [
        f"# Dynamic Entry Rejection Explainability {report.get('date')}",
        "",
        f"user: {report.get('user')}",
        f"total_rejections: {int(report.get('total_rejections') or 0)}",
        "",
        "| Reason | Count | Percent |",
        "| --- | ---: | ---: |",
    ]
    for row in report.get("rows", []):
        lines.append(f"| {row['reason']} | {row['count']} | {row['pct']:.2f}% |")
    lines.append("")
    lines.append("## Adaptive Sensitivity")
    lines.append(f"counts_by_class: {report.get('counts_by_class')}")
    lines.append(f"counts_by_sensitivity_mode: {report.get('counts_by_sensitivity_mode')}")
    lines.append(f"candidates_failed_exactly_one_minor_rule: {report.get('candidates_failed_exactly_one_minor_rule')}")
    lines.append(f"candidates_would_pass_under_relaxed_mode: {report.get('candidates_would_pass_under_relaxed_mode')}")
    lines.append(f"missed_opportunity_analysis: {report.get('missed_opportunity_analysis')}")
    lines.append("")
    lines.append("## Examples")
    for row in report.get("rows", []):
        if not row.get("examples"):
            continue
        lines.append("")
        lines.append(f"### {row['reason']}")
        for example in row["examples"]:
            lines.append(f"- `{example}`")
    lines.append("")
    return "\n".join(lines)


def write_dynamic_entry_rejection_report(
    *,
    project_root: Path,
    data_dir: Path,
    day: date | str,
    user_id: str = "live_bot",
    log_text: str | None = None,
    log_files: Sequence[Path | str] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    report = build_dynamic_entry_rejection_report(
        project_root=project_root,
        data_dir=data_dir,
        day=day,
        user_id=user_id,
        log_text=log_text,
        log_files=log_files,
    )
    out_dir = data_dir / "research_metrics" / str(report["date"])
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "dynamic_entry_rejections.json"
    text_path = out_dir / "dynamic_entry_rejections.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    text_path.write_text(render_dynamic_entry_rejection_report(report), encoding="utf-8")
    return json_path, text_path, report
