"""Read-only dynamic candidate funnel report."""

from __future__ import annotations

import ast
import gzip
import json
import math
import re
import subprocess
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^ ]+)")
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_COMPACT_DATE_RE = re.compile(r"(\d{8})")
_SYMBOL_RE = re.compile(r"\bsymbol=([A-Z][A-Z0-9.\-]{0,9})\b")
_ENTRY_RE = re.compile(
    r"\b(?P<symbol>[A-Z][A-Z0-9.\-]{0,9})\s+ENTRY_EVAL\s+"
    r"(?P<body>.*?\bfinal=(?:T|F|true|false|True|False)\b.*)$"
)
_SCAN_REJECT_RE = re.compile(r"\bDYNAMIC_SCAN reject\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,9}):\s*(?P<body>.+)$")
_SCAN_SELECTED_RE = re.compile(r"DYNAMIC_SCAN selected=(?P<body>\[.*?\])")


@dataclass(frozen=True)
class DynamicFunnelReportPaths:
    json_path: Path
    text_path: Path


def _day_text(day: date | str) -> str:
    return day.isoformat() if isinstance(day, date) else str(day)


def _compact_day(day: date | str) -> str:
    return _day_text(day).replace("-", "")


def _date_from_compact(raw: str) -> str | None:
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}" if len(raw) == 8 and raw.isdigit() else None


def _date_from_path(path: Path) -> str | None:
    for part in [path.name, *[parent.name for parent in path.parents]]:
        iso = _ISO_DATE_RE.search(part)
        if iso:
            return iso.group(1)
        compact = _COMPACT_DATE_RE.search(part)
        if compact:
            parsed = _date_from_compact(compact.group(1))
            if parsed:
                return parsed
    return None


def _line_matches_day(line: str, day: date | str) -> bool:
    day_s = _day_text(day)
    compact = _compact_day(day)
    if day_s in line or compact in line:
        return True
    return not re.search(r"\d{4}-\d{2}-\d{2}", line)


def _read_text(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    return path.read_text(encoding="utf-8", errors="replace")


def _parse_kv(line: str) -> dict[str, str]:
    return {match.group(1): match.group(2).strip(",;") for match in _KV_RE.finditer(line)}


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().strip("$,;%")
    if text.lower() in {"", "none", "n/a", "nan"}:
        return None
    try:
        out = float(text)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _round(value: Any, ndigits: int = 4) -> float | None:
    number = _safe_float(value)
    return round(number, ndigits) if number is not None else None


def _symbol_from_line(line: str) -> str:
    match = _SYMBOL_RE.search(line)
    if match:
        return match.group(1).upper()
    entry = _ENTRY_RE.search(line)
    if entry:
        return entry.group("symbol").upper()
    scan = _SCAN_REJECT_RE.search(line)
    if scan:
        return scan.group("symbol").upper()
    return ""


def _timestamp_from_line(line: str) -> str:
    text = line.strip()
    iso = re.search(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.:\-+Z]+\b", text)
    if iso:
        return iso.group(0)
    syslog = re.match(r"([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})", text)
    if syslog:
        return syslog.group(1)
    return text[:24]


def _reason_from_dynamic_reject(line: str) -> tuple[str, str] | None:
    if "DYNAMIC_REJECT_FUNNEL" in line:
        symbol_match = re.search(r"\bsymbol=([A-Z][A-Z0-9.\-]{0,9})\b", line)
        reason_match = re.search(r"\breason=(?P<reason>.+?)\s+symbol=", line)
        if symbol_match and reason_match:
            return symbol_match.group(1).upper(), reason_match.group("reason").strip()
    scan_match = _SCAN_REJECT_RE.search(line)
    if scan_match:
        body = scan_match.group("body").strip()
        reason = body.split(" ", 1)[0].strip()
        return scan_match.group("symbol").upper(), reason
    return None


def _reason_from_entry(line: str, kv: Mapping[str, str]) -> str:
    match = re.search(r"\breason=(?P<reason>.+?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)", line)
    if match:
        return match.group("reason").strip()
    return str(kv.get("reason") or "unknown")


def _is_dynamic_line(line: str, kv: Mapping[str, str] | None = None) -> bool:
    kv_map = kv or _parse_kv(line)
    route = str(kv_map.get("route") or kv_map.get("source") or "").lower()
    flags = " ".join(str(kv_map.get(key) or "").lower() for key in ("dynamic_candidate", "dynamic_symbol"))
    return "dynamic" in line.lower() or "dynamic" in route or "true" in flags


def _selected_symbols(line: str) -> list[str]:
    match = _SCAN_SELECTED_RE.search(line)
    if not match:
        return []
    try:
        parsed = ast.literal_eval(match.group("body"))
    except Exception:
        parsed = None
    if isinstance(parsed, list):
        return [str(item).strip().upper() for item in parsed if str(item).strip()]
    return []


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _order_status_counts_from_attribution(payload: Mapping[str, Any]) -> dict[str, int]:
    counts = {
        "submitted": 0,
        "accepted": 0,
        "partially_filled": 0,
        "filled": 0,
        "cancelled": 0,
        "rejected": 0,
        "attributed": 0,
        "duplicate_records": 0,
    }
    seen_ids: set[str] = set()
    rows: list[Mapping[str, Any]] = []
    for key in ("orders", "order_events", "fills", "executions"):
        value = payload.get(key)
        if isinstance(value, list):
            rows.extend(row for row in value if isinstance(row, Mapping))
    for row in rows:
        oid = str(row.get("broker_order_id") or row.get("order_id") or row.get("client_order_id") or row.get("fill_id") or "")
        if oid and oid in seen_ids:
            counts["duplicate_records"] += 1
            continue
        if oid:
            seen_ids.add(oid)
        status = str(row.get("status") or row.get("order_status") or row.get("fill_status") or "").lower()
        if "partial" in status:
            counts["partially_filled"] += 1
        elif "fill" in status or row.get("filled_at"):
            counts["filled"] += 1
        elif "cancel" in status:
            counts["cancelled"] += 1
        elif "reject" in status:
            counts["rejected"] += 1
        elif "accept" in status:
            counts["accepted"] += 1
        elif "submit" in status:
            counts["submitted"] += 1
    exits = payload.get("exits")
    if isinstance(exits, list):
        counts["attributed"] += len([row for row in exits if isinstance(row, Mapping)])
    route_stats = payload.get("route_stats")
    if isinstance(route_stats, Mapping):
        counts["attributed"] += sum(
            int(_safe_float(row.get("trades")) or 0)
            for row in route_stats.values()
            if isinstance(row, Mapping)
        )
    return counts


def _candidate_row(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "reason": None,
        "gain_pct": None,
        "RVOL": None,
        "spread": None,
        "VWAP": None,
        "signal_score": None,
        "news_score": None,
        "catalyst_score": None,
        "event_score": None,
        "article_count": None,
        "time_first_seen": None,
        "time_last_seen": None,
        "count": 0,
    }


def _candidate(rows: dict[str, dict[str, Any]], symbol: str) -> dict[str, Any]:
    sym = str(symbol or "").strip().upper()
    if sym not in rows:
        rows[sym] = _candidate_row(sym)
    return rows[sym]


def _update_seen(row: dict[str, Any], *, ts: str, reason: str | None, kv: Mapping[str, str]) -> None:
    row["count"] = int(row.get("count") or 0) + 1
    if row.get("time_first_seen") is None:
        row["time_first_seen"] = ts
    row["time_last_seen"] = ts
    if reason:
        row["reason"] = reason
    field_map = {
        "gain_pct": ("gain_pct", "day_gain_pct", "gain"),
        "RVOL": ("relative_volume", "rel_volume", "rvol", "observed_rvol"),
        "spread": ("spread_pct", "spread"),
        "VWAP": ("vwap", "price_above_vwap", "vwap_above"),
        "signal_score": ("signal_score", "scanner_score", "score", "strength"),
        "news_score": ("news_score",),
        "catalyst_score": ("catalyst_score",),
        "event_score": ("event_score",),
        "article_count": ("article_count", "articles"),
    }
    for target, keys in field_map.items():
        if row.get(target) is not None:
            continue
        for key in keys:
            if key in kv:
                value: Any = kv[key]
                if target != "VWAP":
                    value = _round(value, 4)
                row[target] = value
                break


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


def _journalctl_lines(day: str, *, service: str = "algo.service") -> list[str]:
    try:
        proc = subprocess.run(
            [
                "journalctl",
                "-u",
                service,
                "--since",
                f"{day} 09:00:00",
                "--until",
                f"{day} 23:59:59",
                "--no-pager",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    return proc.stdout.splitlines()


def _load_log_lines(
    *,
    project_root: Path,
    data_dir: Path,
    day: str,
    log_text: str | None,
    log_files: Sequence[Path | str] | None,
) -> list[str]:
    if log_text is not None:
        return [line for line in log_text.splitlines() if _line_matches_day(line, day)]
    lines: list[str] = []
    for path in _discover_log_paths(project_root, data_dir=data_dir, day=day, extra_paths=log_files):
        try:
            lines.extend(_read_text(path).splitlines())
        except OSError:
            continue
    if not lines:
        lines.extend(_journalctl_lines(day))
    return [line for line in lines if _line_matches_day(line, day)]


def build_dynamic_funnel_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: date | str,
    user_id: str,
    log_text: str | None = None,
    log_files: Sequence[Path | str] | None = None,
) -> dict[str, Any]:
    """Build a read-only dynamic funnel report from logs."""
    day_s = _day_text(day)
    root = Path(project_root)
    data = Path(data_dir)
    lines = _load_log_lines(
        project_root=root,
        data_dir=data,
        day=day_s,
        log_text=log_text,
        log_files=log_files,
    )

    selected_symbols: list[str] = []
    scanner_rejects: Counter[str] = Counter()
    entry_reasons: Counter[str] = Counter()
    allocator_skip_reasons: Counter[str] = Counter()
    blocked_symbols: Counter[str] = Counter()
    missed: dict[str, dict[str, Any]] = {}
    weak_rows: dict[str, dict[str, Any]] = {}
    unmatched_events: list[dict[str, Any]] = []

    scanner_reject_seen: set[tuple[str, str, str]] = set()
    quote_retry_start = quote_retry_success = quote_retry_failed = 0
    entry_evaluations = entry_passed = entry_failed = 0
    allocator_actions_created = allocator_actions_dispatched = dispatch_skips = 0
    submitted = filled = cancelled = 0

    for raw in lines:
        line = str(raw)
        ts = _timestamp_from_line(line)
        kv = _parse_kv(line)
        symbol = _symbol_from_line(line)
        if "DYNAMIC_SCAN selected=" in line:
            selected_symbols.extend(sym for sym in _selected_symbols(line) if sym)
        if "QUOTE_RETRY_START" in line and "reason=unstable_quote" in line:
            quote_retry_start += 1
        if "QUOTE_RETRY_SUCCESS" in line:
            quote_retry_success += 1
        if "QUOTE_RETRY_FAILED" in line:
            quote_retry_failed += 1

        reject = _reason_from_dynamic_reject(line)
        if reject is not None:
            sym, reason = reject
            dedupe = (ts, sym, reason)
            if dedupe not in scanner_reject_seen:
                scanner_reject_seen.add(dedupe)
                scanner_rejects[reason] += 1
                blocked_symbols[sym] += 1
                _update_seen(_candidate(missed, sym), ts=ts, reason=reason, kv=kv)

        entry = _ENTRY_RE.search(line)
        if entry and _is_dynamic_line(line, kv):
            sym = entry.group("symbol").upper()
            reason = _reason_from_entry(line, kv)
            final = str(kv.get("final") or "").lower() in {"t", "true"}
            entry_evaluations += 1
            if final:
                entry_passed += 1
            else:
                entry_failed += 1
                entry_reasons[reason] += 1
                blocked_symbols[sym] += 1
                _update_seen(_candidate(missed, sym), ts=ts, reason=reason, kv=kv)

        if "ALLOCATOR_ACTION_CREATED" in line and _is_dynamic_line(line, kv):
            allocator_actions_created += 1
        if "ALLOCATOR_ACTION_SUBMITTED" in line and _is_dynamic_line(line, kv):
            allocator_actions_dispatched += 1
        if ("ALLOCATOR_DISPATCH_SKIPPED" in line or "DISPATCH_SKIP" in line) and _is_dynamic_line(line, kv):
            dispatch_skips += 1
            reason = str(kv.get("reason") or "unknown")
            allocator_skip_reasons[reason] += 1
            if symbol:
                blocked_symbols[symbol] += 1
                _update_seen(_candidate(missed, symbol), ts=ts, reason=reason, kv=kv)
        if "ORDER_SKIP" in line and _is_dynamic_line(line, kv):
            dispatch_skips += 1
            reason = str(kv.get("reason") or "unknown")
            allocator_skip_reasons[reason] += 1
            if symbol:
                blocked_symbols[symbol] += 1
                _update_seen(_candidate(missed, symbol), ts=ts, reason=reason, kv=kv)

        if "ORDER_SUBMITTED" in line and _is_dynamic_line(line, kv):
            submitted += 1
        if "ORDER_FILLED" in line and _is_dynamic_line(line, kv):
            filled += 1
        if ("ORDER_CANCELLED" in line or "ORDER_CANCELED" in line) and _is_dynamic_line(line, kv):
            cancelled += 1

        if "weak_catalyst_dynamic_non_exceptional_live" in line or "DYNAMIC_WEAK_CATALYST_REJECT" in line:
            if symbol:
                weak = _candidate(weak_rows, symbol)
                _update_seen(weak, ts=ts, reason=str(kv.get("reason") or "weak_catalyst"), kv=kv)
                blocked_symbols[symbol] += 1
                _update_seen(_candidate(missed, symbol), ts=ts, reason=str(kv.get("reason") or "weak_catalyst"), kv=kv)

    selected_unique = sorted(set(selected_symbols))
    rejected_count = sum(scanner_rejects.values())
    accepted_count = len(selected_symbols)
    total_scanned = accepted_count + rejected_count
    weak_values = list(weak_rows.values())

    def avg(key: str) -> float | None:
        values = [_safe_float(row.get(key)) for row in weak_values]
        clean = [value for value in values if value is not None]
        return round(sum(clean) / len(clean), 4) if clean else None

    artifact_reconciliation_enabled = log_text is None
    alignment_payload = (
        _load_json(data / "research_metrics" / day_s / "dynamic_entry_alignment.json")
        if artifact_reconciliation_enabled
        else {}
    )
    alignment_summary = alignment_payload.get("summary") if isinstance(alignment_payload.get("summary"), Mapping) else {}
    alignment_events = alignment_payload.get("events") if isinstance(alignment_payload.get("events"), list) else []
    alignment_rejections = int(_safe_float(alignment_summary.get("entry_alignment_rejections")) or 0)
    alignment_symbols = sorted(
        {
            str(event.get("symbol") or "").upper()
            for event in alignment_events
            if isinstance(event, Mapping) and str(event.get("symbol") or "").strip()
        }
    )
    if alignment_rejections:
        if entry_evaluations < alignment_rejections:
            unmatched_events.append(
                {
                    "stage": "entry_alignment",
                    "reason": "alignment_artifact_has_more_rejections_than_log_entry_eval_lines",
                    "artifact_count": alignment_rejections,
                    "log_count": entry_evaluations,
                }
            )
        for event in alignment_events:
            if not isinstance(event, Mapping):
                continue
            sym = str(event.get("symbol") or "").upper()
            if sym:
                row = _candidate(missed, sym)
                if int(row.get("count") or 0) <= 0:
                    _update_seen(row, ts=str(event.get("timestamp") or ""), reason="entry_alignment", kv={})
        if not selected_unique and alignment_symbols:
            selected_unique = alignment_symbols
            accepted_count = len(selected_unique)
        if rejected_count < alignment_rejections:
            entry_reasons["entry_alignment"] += alignment_rejections - rejected_count
            rejected_count = alignment_rejections
        entry_evaluations = max(entry_evaluations, alignment_rejections)
        entry_failed = max(entry_failed, alignment_rejections)
        total_scanned = max(total_scanned, accepted_count + rejected_count)

    attrib_payload = (
        _load_json(data / "profitability_attribution" / "daily" / f"{day_s}_{user_id}.json")
        if artifact_reconciliation_enabled
        else {}
    )
    order_counts = _order_status_counts_from_attribution(attrib_payload)
    attributed_records = int(order_counts.get("attributed", 0) or 0)
    effective_filled = max(filled, int(order_counts.get("filled", 0) or 0))
    if attributed_records and not filled:
        unmatched_events.append(
            {
                "stage": "execution",
                "reason": "attribution_records_present_but_log_fill_events_missing",
                "artifact_count": attributed_records,
                "log_count": filled,
            }
        )
    missed_rows = sorted(
        (dict(row) for row in missed.values()),
        key=lambda row: (-int(row.get("count") or 0), str(row.get("symbol") or "")),
    )
    top_reasons = Counter()
    top_reasons.update(scanner_rejects)
    top_reasons.update(entry_reasons)
    top_reasons.update(allocator_skip_reasons)

    return {
        "date": day_s,
        "user_id": str(user_id or "default"),
        "log_line_count": len(lines),
        "scanner": {
            "total_scanned": total_scanned,
            "accepted": accepted_count,
            "accepted_unique": len(selected_unique),
            "accepted_symbols": selected_unique,
            "rejected": rejected_count,
            "rejection_counts_by_reason": dict(scanner_rejects.most_common()),
            "unstable_quote_retries": quote_retry_start,
            "retry_successes": quote_retry_success,
            "retry_failures": quote_retry_failed,
        },
        "entry": {
            "entry_evaluations": entry_evaluations,
            "passed": entry_passed,
            "failed": entry_failed,
            "reasons": dict(entry_reasons.most_common()),
        },
        "allocator": {
            "actions_created": allocator_actions_created,
            "actions_dispatched": allocator_actions_dispatched,
            "dispatch_skips": dispatch_skips,
            "skip_reasons": dict(allocator_skip_reasons.most_common()),
        },
        "execution": {
            "submitted": submitted,
            "filled": filled,
            "cancelled": cancelled,
        },
        "order_attribution_reconciliation": {
            "submitted": submitted + int(order_counts.get("submitted", 0) or 0),
            "accepted": int(order_counts.get("accepted", 0) or 0),
            "partially_filled": int(order_counts.get("partially_filled", 0) or 0),
            "filled": effective_filled,
            "cancelled": cancelled + int(order_counts.get("cancelled", 0) or 0),
            "rejected": int(order_counts.get("rejected", 0) or 0),
            "attributed": attributed_records,
            "missing_attribution": max(0, submitted - attributed_records),
            "duplicate_records": int(order_counts.get("duplicate_records", 0) or 0),
        },
        "reconciliation": {
            "scanner_events": total_scanned,
            "selected_candidates": accepted_count,
            "entry_evaluations": entry_evaluations,
            "alignment_rejections": alignment_rejections,
            "allocator_actions": allocator_actions_created,
            "dispatched_actions": allocator_actions_dispatched,
            "submitted_orders": submitted,
            "fills": effective_filled,
            "exits": attributed_records,
            "attribution_records": attributed_records,
            "unmatched_events": unmatched_events,
        },
        "weak_catalyst": {
            "candidates_blocked": len(weak_values),
            "symbols": sorted(weak_rows),
            "average_RVOL": avg("RVOL"),
            "average_gain_pct": avg("gain_pct"),
            "average_spread": avg("spread"),
            "average_signal_score": avg("signal_score"),
        },
        "missed_opportunities": missed_rows,
        "summary": {
            "top_5_rejection_reasons": [
                {"reason": reason, "count": count} for reason, count in top_reasons.most_common(5)
            ],
            "top_10_blocked_symbols": [
                {"symbol": symbol, "count": count} for symbol, count in blocked_symbols.most_common(10)
            ],
        },
    }


def render_dynamic_funnel_report(report: Mapping[str, Any]) -> str:
    """Render a Markdown dynamic funnel report."""
    scanner = report.get("scanner") if isinstance(report.get("scanner"), Mapping) else {}
    entry = report.get("entry") if isinstance(report.get("entry"), Mapping) else {}
    allocator = report.get("allocator") if isinstance(report.get("allocator"), Mapping) else {}
    execution = report.get("execution") if isinstance(report.get("execution"), Mapping) else {}
    reconciliation = report.get("reconciliation") if isinstance(report.get("reconciliation"), Mapping) else {}
    order_recon = report.get("order_attribution_reconciliation") if isinstance(report.get("order_attribution_reconciliation"), Mapping) else {}
    weak = report.get("weak_catalyst") if isinstance(report.get("weak_catalyst"), Mapping) else {}
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    lines = [
        f"# Dynamic Funnel Report {report.get('date')} user={report.get('user_id')}",
        "",
        "## Scanner",
        f"- total scanned: {scanner.get('total_scanned', 0)}",
        f"- accepted: {scanner.get('accepted', 0)}",
        f"- rejected: {scanner.get('rejected', 0)}",
        f"- unstable quote retries: {scanner.get('unstable_quote_retries', 0)}",
        f"- retry successes: {scanner.get('retry_successes', 0)}",
        f"- retry failures: {scanner.get('retry_failures', 0)}",
        "- rejection counts by reason:",
    ]
    for reason, count in dict(scanner.get("rejection_counts_by_reason") or {}).items():
        lines.append(f"  - {reason}: {count}")
    lines.extend(
        [
            "",
            "## Entry",
            f"- entry evaluations: {entry.get('entry_evaluations', 0)}",
            f"- passed: {entry.get('passed', 0)}",
            f"- failed: {entry.get('failed', 0)}",
            "- reasons:",
        ]
    )
    for reason, count in dict(entry.get("reasons") or {}).items():
        lines.append(f"  - {reason}: {count}")
    lines.extend(
        [
            "",
            "## Allocator",
            f"- actions created: {allocator.get('actions_created', 0)}",
            f"- actions dispatched: {allocator.get('actions_dispatched', 0)}",
            f"- dispatch skips: {allocator.get('dispatch_skips', 0)}",
            "- skip reasons:",
        ]
    )
    for reason, count in dict(allocator.get("skip_reasons") or {}).items():
        lines.append(f"  - {reason}: {count}")
    lines.extend(
        [
            "",
            "## Execution",
            f"- submitted: {execution.get('submitted', 0)}",
            f"- filled: {execution.get('filled', 0)}",
            f"- cancelled: {execution.get('cancelled', 0)}",
            "",
            "## Reconciliation",
            "DYNAMIC_FUNNEL_RECONCILIATION "
            f"scanner_events={reconciliation.get('scanner_events', 0)} "
            f"selected_candidates={reconciliation.get('selected_candidates', 0)} "
            f"entry_evaluations={reconciliation.get('entry_evaluations', 0)} "
            f"alignment_rejections={reconciliation.get('alignment_rejections', 0)} "
            f"allocator_actions={reconciliation.get('allocator_actions', 0)} "
            f"submitted_orders={reconciliation.get('submitted_orders', 0)} "
            f"fills={reconciliation.get('fills', 0)} "
            f"attribution_records={reconciliation.get('attribution_records', 0)} "
            f"unmatched_events={len(reconciliation.get('unmatched_events') or [])}",
            "ORDER_ATTRIBUTION_RECONCILIATION "
            f"submitted={order_recon.get('submitted', 0)} "
            f"accepted={order_recon.get('accepted', 0)} "
            f"partially_filled={order_recon.get('partially_filled', 0)} "
            f"filled={order_recon.get('filled', 0)} "
            f"cancelled={order_recon.get('cancelled', 0)} "
            f"rejected={order_recon.get('rejected', 0)} "
            f"attributed={order_recon.get('attributed', 0)} "
            f"missing_attribution={order_recon.get('missing_attribution', 0)} "
            f"duplicate_records={order_recon.get('duplicate_records', 0)}",
            "- unmatched events:",
        ]
    )
    unmatched = reconciliation.get("unmatched_events") or []
    if unmatched:
        for row in unmatched:
            lines.append(
                f"  - stage={row.get('stage')} reason={row.get('reason')} "
                f"artifact_count={row.get('artifact_count')} log_count={row.get('log_count')}"
            )
    else:
        lines.append("  - none")
    lines.extend(
        [
            "",
            "## Weak Catalyst",
            f"- candidates blocked: {weak.get('candidates_blocked', 0)}",
            f"- symbols: {', '.join(weak.get('symbols') or []) or 'none'}",
            f"- average RVOL: {weak.get('average_RVOL')}",
            f"- average gain%: {weak.get('average_gain_pct')}",
            f"- average spread: {weak.get('average_spread')}",
            f"- average signal score: {weak.get('average_signal_score')}",
            "",
            "## Missed Opportunity Table",
            "| symbol | reason | gain_pct | RVOL | spread | VWAP | signal_score | news_score | catalyst_score | event_score | article_count | first_seen | last_seen | count |",
            "| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: |",
        ]
    )
    for row in report.get("missed_opportunities") or []:
        lines.append(
            "| {symbol} | {reason} | {gain_pct} | {RVOL} | {spread} | {VWAP} | {signal_score} | {news_score} | {catalyst_score} | {event_score} | {article_count} | {time_first_seen} | {time_last_seen} | {count} |".format(
                symbol=row.get("symbol") or "",
                reason=row.get("reason") or "",
                gain_pct=row.get("gain_pct"),
                RVOL=row.get("RVOL"),
                spread=row.get("spread"),
                VWAP=row.get("VWAP"),
                signal_score=row.get("signal_score"),
                news_score=row.get("news_score"),
                catalyst_score=row.get("catalyst_score"),
                event_score=row.get("event_score"),
                article_count=row.get("article_count"),
                time_first_seen=row.get("time_first_seen") or "",
                time_last_seen=row.get("time_last_seen") or "",
                count=row.get("count") or 0,
            )
        )
    lines.extend(["", "## Summary", "### Top 5 Rejection Reasons"])
    for row in summary.get("top_5_rejection_reasons") or []:
        lines.append(f"- {row.get('reason')}: {row.get('count')}")
    lines.append("")
    lines.append("### Top 10 Most Frequently Blocked Symbols")
    for row in summary.get("top_10_blocked_symbols") or []:
        lines.append(f"- {row.get('symbol')}: {row.get('count')}")
    return "\n".join(lines).rstrip() + "\n"


def write_dynamic_funnel_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: date | str,
    user_id: str,
    log_text: str | None = None,
    log_files: Sequence[Path | str] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    """Build and write JSON/Markdown dynamic funnel artifacts."""
    data = Path(data_dir)
    day_s = _day_text(day)
    report = build_dynamic_funnel_report(
        project_root=project_root,
        data_dir=data,
        day=day_s,
        user_id=user_id,
        log_text=log_text,
        log_files=log_files,
    )
    out_dir = data / "research_metrics" / day_s
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "dynamic_funnel_live.json"
    text_path = out_dir / "dynamic_funnel_live.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    text_path.write_text(render_dynamic_funnel_report(report), encoding="utf-8")
    return json_path, text_path, report
