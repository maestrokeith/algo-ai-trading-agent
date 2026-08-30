"""Research-only opening-session churn report from trade attribution artifacts."""

from __future__ import annotations

import json
import math
import re
import gzip
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from src.trade_attribution import attribution_daily_path, load_daily_artifact

_ET = ZoneInfo("America/New_York")
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_COMPACT_DATE_RE = re.compile(r"(\d{8})")
_SKIP_REASON_RE = re.compile(r"\bSKIP\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,9}):\s+reason=(?P<reason>.+)$")
_ENTRY_EVAL_RE = re.compile(
    r"\b(?P<symbol>[A-Z][A-Z0-9.\-]{0,9})\s+ENTRY_EVAL\s+route=(?P<route>[^ ]+).*?"
    r"\bfinal=(?P<final>[TF]|true|false|True|False)\b.*?\breason=(?P<reason>.+?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)"
)


@dataclass(frozen=True)
class OpeningChurnPaths:
    json_path: Path
    text_path: Path


def _safe_user(user_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(user_id or "default")) or "default"


def _safe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_ET)
    return dt.astimezone(_ET)


def _date_from_compact(raw: str) -> str | None:
    if len(raw) != 8 or not raw.isdigit():
        return None
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"


def _date_from_path(path: Path) -> str | None:
    iso = _ISO_DATE_RE.search(path.name)
    if iso:
        return iso.group(1)
    compact = _COMPACT_DATE_RE.search(path.name)
    if compact:
        return _date_from_compact(compact.group(1))
    return None


def _read_text(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    return path.read_text(encoding="utf-8", errors="replace")


def discover_opening_churn_log_paths(
    *,
    project_root: Path | str = ".",
    day: str,
    extra_paths: Sequence[Path | str] | None = None,
) -> list[Path]:
    """Discover local logs that can enrich the research report."""
    root = Path(project_root)
    paths: list[Path] = []
    for base in (
        root / "data" / "logs",
        root / "data" / "debug_logs",
        root / "reports" / "debug",
        root,
    ):
        if not base.exists():
            continue
        iterator = base.rglob("*") if base.name == "debug_logs" else base.glob("*")
        for path in iterator:
            if not path.is_file() or path.suffix not in {".log", ".txt", ".gz"}:
                continue
            path_day = _date_from_path(path)
            if path_day not in {None, day} and day not in path.name:
                continue
            paths.append(path)
    for extra in extra_paths or []:
        path = Path(extra)
        if path.exists() and path.is_file():
            paths.append(path)
    return sorted(dict.fromkeys(paths))


def _time_label(dt: datetime | None) -> str | None:
    return dt.astimezone(_ET).isoformat() if dt is not None else None


def _same_or_after(left: datetime, right: datetime) -> bool:
    return left >= right


def _submitted_buy_orders(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("orders") if isinstance(payload.get("orders"), list) else []
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("action") or "").strip().lower() != "buy":
            continue
        if not bool(row.get("submitted")):
            continue
        sym = str(row.get("symbol") or "").strip().upper()
        ts = _parse_timestamp(row.get("timestamp"))
        if not sym or ts is None:
            continue
        key = (sym, ts.isoformat(), str(row.get("order_id") or ""))
        if key in seen:
            continue
        seen.add(key)
        item = dict(row)
        item["_entry_dt"] = ts
        out.append(item)
    return sorted(out, key=lambda row: (row["_entry_dt"], str(row.get("symbol") or "")))


def _exit_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("exits") if isinstance(payload.get("exits"), list) else []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        sym = str(row.get("symbol") or "").strip().upper()
        ts = _parse_timestamp(row.get("timestamp"))
        if not sym or ts is None:
            continue
        item = dict(row)
        item["_exit_dt"] = ts
        out.append(item)
    return sorted(out, key=lambda row: (row["_exit_dt"], str(row.get("symbol") or "")))


def _match_exits_to_entries(
    entries: Sequence[Mapping[str, Any]],
    exits: Sequence[Mapping[str, Any]],
) -> dict[int, Mapping[str, Any]]:
    """Match each entry to the next same-symbol exit after entry."""
    used_exit_indexes: set[int] = set()
    matches: dict[int, Mapping[str, Any]] = {}
    for entry_idx, entry in enumerate(entries):
        sym = str(entry.get("symbol") or "").strip().upper()
        entry_dt = entry.get("_entry_dt")
        if not sym or not isinstance(entry_dt, datetime):
            continue
        best_idx: int | None = None
        best_exit: Mapping[str, Any] | None = None
        for exit_idx, exit_row in enumerate(exits):
            if exit_idx in used_exit_indexes:
                continue
            if str(exit_row.get("symbol") or "").strip().upper() != sym:
                continue
            exit_dt = exit_row.get("_exit_dt")
            if not isinstance(exit_dt, datetime) or not _same_or_after(exit_dt, entry_dt):
                continue
            best_idx = exit_idx
            best_exit = exit_row
            break
        if best_idx is not None and best_exit is not None:
            used_exit_indexes.add(best_idx)
            matches[entry_idx] = best_exit
    return matches


def _phase_for_entry(entry_dt: datetime) -> str:
    local_time = entry_dt.astimezone(_ET).time()
    if local_time < time(9, 45):
        return "before_0945"
    if local_time < time(10, 0):
        return "between_0945_1000"
    return "at_or_after_1000"


def _hold_minutes(entry_dt: datetime, exit_row: Mapping[str, Any] | None) -> float | None:
    if exit_row is None:
        return None
    direct = _safe_float(exit_row.get("hold_minutes"))
    if direct is not None and direct > 0.0:
        return direct
    exit_dt = exit_row.get("_exit_dt")
    if isinstance(exit_dt, datetime):
        return max(0.0, (exit_dt - entry_dt).total_seconds() / 60.0)
    return direct


def _entry_record(entry: Mapping[str, Any], exit_row: Mapping[str, Any] | None) -> dict[str, Any]:
    entry_dt = entry.get("_entry_dt")
    if not isinstance(entry_dt, datetime):
        raise ValueError("entry missing parsed timestamp")
    exit_dt = exit_row.get("_exit_dt") if isinstance(exit_row, Mapping) else None
    hold = _hold_minutes(entry_dt, exit_row)
    exit_reason = str(exit_row.get("exit_reason") or "") if isinstance(exit_row, Mapping) else ""
    pnl = _safe_float(exit_row.get("pnl")) if isinstance(exit_row, Mapping) else None
    pnl_pct = _safe_float(exit_row.get("pnl_pct")) if isinstance(exit_row, Mapping) else None
    stopped = "stop" in exit_reason.lower()
    stopped_under_5 = bool(stopped and hold is not None and hold <= 5.0)
    return {
        "symbol": str(entry.get("symbol") or "").strip().upper(),
        "route": entry.get("route") or entry.get("source"),
        "source": entry.get("source"),
        "entry_time": _time_label(entry_dt),
        "fill_time": _time_label(entry_dt),
        "notional": _safe_float(entry.get("notional")),
        "qty": _safe_float(entry.get("qty")),
        "order_id": entry.get("order_id"),
        "phase": _phase_for_entry(entry_dt),
        "before_0945": entry_dt.astimezone(_ET).time() < time(9, 45),
        "before_1000": entry_dt.astimezone(_ET).time() < time(10, 0),
        "exit_time": _time_label(exit_dt) if isinstance(exit_dt, datetime) else None,
        "exit_reason": exit_reason or None,
        "hold_minutes": hold,
        "realized_pnl": pnl,
        "realized_pnl_pct": pnl_pct,
        "stopped_out_under_5m": stopped_under_5,
    }


def _phase_summary(entries: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    phases = ("before_0945", "between_0945_1000", "at_or_after_1000")
    out: dict[str, dict[str, Any]] = {}
    for phase in phases:
        rows = [row for row in entries if row.get("phase") == phase]
        holds = [float(row["hold_minutes"]) for row in rows if _safe_float(row.get("hold_minutes")) is not None]
        pnls = [float(row["realized_pnl"]) for row in rows if _safe_float(row.get("realized_pnl")) is not None]
        out[phase] = {
            "entries": len(rows),
            "exited": sum(1 for row in rows if row.get("exit_time")),
            "stopped_out_under_5m": sum(1 for row in rows if row.get("stopped_out_under_5m")),
            "stop_loss_exits": sum(1 for row in rows if "stop" in str(row.get("exit_reason") or "").lower()),
            "avg_hold_minutes": round(sum(holds) / len(holds), 4) if holds else None,
            "median_hold_minutes": round(median(holds), 4) if holds else None,
            "realized_pnl_available": len(pnls),
            "total_realized_pnl": round(sum(pnls), 4) if pnls else None,
            "symbols": [str(row.get("symbol") or "") for row in rows],
        }
    early_rows = [row for row in entries if row.get("before_1000")]
    later_rows = [row for row in entries if not row.get("before_1000")]
    early_pnls = [float(row["realized_pnl"]) for row in early_rows if _safe_float(row.get("realized_pnl")) is not None]
    out["early_before_1000_vs_later"] = {
        "early_entries": len(early_rows),
        "later_entries": len(later_rows),
        "early_stopped_out_under_5m": sum(1 for row in early_rows if row.get("stopped_out_under_5m")),
        "later_stopped_out_under_5m": sum(1 for row in later_rows if row.get("stopped_out_under_5m")),
        "early_realized_pnl_available": len(early_pnls),
        "early_wins": sum(1 for pnl in early_pnls if pnl > 0.0),
        "early_losses": sum(1 for pnl in early_pnls if pnl < 0.0),
        "early_flat": sum(1 for pnl in early_pnls if pnl == 0.0),
        "early_total_realized_pnl": round(sum(early_pnls), 4) if early_pnls else None,
    }
    return out


def _log_time_from_line(line: str, *, day: str) -> str | None:
    match = re.match(r"^[A-Z][a-z]{2}\s+\d{1,2}\s+(\d{2}:\d{2}:\d{2})\b", line)
    if match:
        try:
            return datetime.fromisoformat(f"{day}T{match.group(1)}").replace(tzinfo=_ET).isoformat()
        except ValueError:
            return None
    iso = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)", line)
    if iso:
        return _time_label(_parse_timestamp(iso.group(1)))
    return None


def _analyze_logs(*, day: str, log_paths: Sequence[Path]) -> dict[str, Any]:
    trend_skips: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "symbol": "",
            "count": 0,
            "first_seen": None,
            "last_seen": None,
            "later_became_eligible": False,
            "later_entry_eval_final_true": False,
            "later_entry_eval_reason": None,
            "last_reason": None,
        }
    )
    reentry_blocks: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"symbol": "", "count": 0, "reasons": Counter(), "first_seen": None, "last_seen": None}
    )
    used: list[str] = []
    for path in log_paths:
        try:
            text = _read_text(path)
        except OSError:
            continue
        useful = False
        for line in text.splitlines():
            ts = _log_time_from_line(line, day=day)
            skip = _SKIP_REASON_RE.search(line)
            if skip:
                symbol = skip.group("symbol").upper()
                reason = skip.group("reason").strip()
                reason_l = reason.lower()
                if "below mas (trend prefilter)" in reason_l:
                    row = trend_skips[symbol]
                    row["symbol"] = symbol
                    row["count"] = int(row["count"]) + 1
                    row["first_seen"] = row["first_seen"] or ts
                    row["last_seen"] = ts or row["last_seen"]
                    row["last_reason"] = reason
                    useful = True
                if (
                    "post-exit re-entry wait" in reason_l
                    or "cooldown after stop loss" in reason_l
                    or "cooldown" in reason_l
                ):
                    row = reentry_blocks[symbol]
                    row["symbol"] = symbol
                    row["count"] = int(row["count"]) + 1
                    row["first_seen"] = row["first_seen"] or ts
                    row["last_seen"] = ts or row["last_seen"]
                    row["reasons"][reason] += 1
                    useful = True
            entry = _ENTRY_EVAL_RE.search(line)
            if entry:
                symbol = entry.group("symbol").upper()
                final = entry.group("final").strip().lower() in {"t", "true"}
                if symbol in trend_skips and final:
                    row = trend_skips[symbol]
                    row["later_became_eligible"] = True
                    row["later_entry_eval_final_true"] = True
                    row["later_entry_eval_reason"] = entry.group("reason").strip()
                    useful = True
                reason_l = entry.group("reason").strip().lower()
                if "post-exit re-entry wait" in reason_l or "cooldown after stop loss" in reason_l:
                    row = reentry_blocks[symbol]
                    row["symbol"] = symbol
                    row["count"] = int(row["count"]) + 1
                    row["first_seen"] = row["first_seen"] or ts
                    row["last_seen"] = ts or row["last_seen"]
                    row["reasons"][entry.group("reason").strip()] += 1
                    useful = True
        if useful:
            used.append(str(path))
    trend_rows = []
    for row in trend_skips.values():
        item = dict(row)
        item["status"] = "later_eligible" if item.get("later_became_eligible") else "stayed_blocked_in_logs"
        trend_rows.append(item)
    block_rows = []
    for row in reentry_blocks.values():
        item = dict(row)
        reasons = item.get("reasons")
        item["reasons"] = dict(reasons) if isinstance(reasons, Counter) else {}
        block_rows.append(item)
    trend_rows.sort(key=lambda row: (-int(row.get("count") or 0), str(row.get("symbol") or "")))
    block_rows.sort(key=lambda row: (-int(row.get("count") or 0), str(row.get("symbol") or "")))
    return {
        "source_logs": used,
        "trend_prefilter": {
            "total_skips": sum(int(row.get("count") or 0) for row in trend_rows),
            "symbols": len(trend_rows),
            "top_skipped_symbols": trend_rows[:25],
        },
        "reentry_blocks": {
            "symbols": [row.get("symbol") for row in block_rows],
            "rows": block_rows,
        },
    }


def build_opening_churn_report(
    *,
    data_dir: Path | str,
    user_id: str,
    day: date | str,
    project_root: Path | str = ".",
    log_paths: Sequence[Path | str] | None = None,
) -> dict[str, Any]:
    """Build the research-only opening churn report."""
    day_s = day.isoformat() if isinstance(day, date) else str(day)
    path = attribution_daily_path(data_dir=data_dir, user_id=user_id, day=day_s)
    payload = load_daily_artifact(path)
    entries = _submitted_buy_orders(payload)
    exits = _exit_rows(payload)
    matches = _match_exits_to_entries(entries, exits)
    entry_records = [_entry_record(entry, matches.get(idx)) for idx, entry in enumerate(entries)]
    flagged = [row for row in entry_records if row.get("stopped_out_under_5m")]
    discovered_logs = discover_opening_churn_log_paths(
        project_root=project_root,
        day=day_s,
        extra_paths=log_paths,
    )
    log_analysis = _analyze_logs(day=day_s, log_paths=discovered_logs)
    reentry_symbols = [str(sym) for sym in log_analysis.get("reentry_blocks", {}).get("symbols", [])]
    phase_summary = _phase_summary(entry_records)
    return {
        "version": 1,
        "date": day_s,
        "user_id": str(user_id or "default"),
        "source_artifact": str(path),
        "summary": {
            "entries_total": len(entry_records),
            "entries_before_0945": sum(1 for row in entry_records if row.get("before_0945")),
            "entries_before_1000": sum(1 for row in entry_records if row.get("before_1000")),
            "entries_at_or_after_1000": sum(1 for row in entry_records if not row.get("before_1000")),
            "stopped_out_under_5m": len(flagged),
            "symbols_stopped_out_under_5m": [str(row.get("symbol") or "") for row in flagged],
            "early_entry_pnl_available": phase_summary["early_before_1000_vs_later"]["early_realized_pnl_available"],
            "early_entry_wins": phase_summary["early_before_1000_vs_later"]["early_wins"],
            "early_entry_losses": phase_summary["early_before_1000_vs_later"]["early_losses"],
            "early_entry_flat": phase_summary["early_before_1000_vs_later"]["early_flat"],
            "early_entry_total_realized_pnl": phase_summary["early_before_1000_vs_later"]["early_total_realized_pnl"],
            "symbols_with_reentry_blocked": reentry_symbols,
        },
        "phase_summary": phase_summary,
        "entries": entry_records,
        "stopped_out_under_5m": flagged,
        "log_analysis": log_analysis,
    }


def render_opening_churn_report(report: Mapping[str, Any]) -> str:
    """Render a stable text report for operator review."""
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    phase_summary = report.get("phase_summary") if isinstance(report.get("phase_summary"), Mapping) else {}
    entries = report.get("entries") if isinstance(report.get("entries"), list) else []
    flagged = report.get("stopped_out_under_5m") if isinstance(report.get("stopped_out_under_5m"), list) else []
    lines = [
        f"Opening Churn Research - {report.get('date')} user={report.get('user_id')}",
        "",
        "Summary:",
        f"- entries_total: {summary.get('entries_total', 0)}",
        f"- entries_before_0945: {summary.get('entries_before_0945', 0)}",
        f"- entries_before_1000: {summary.get('entries_before_1000', 0)}",
        f"- entries_at_or_after_1000: {summary.get('entries_at_or_after_1000', 0)}",
        f"- stopped_out_under_5m: {summary.get('stopped_out_under_5m', 0)}",
        f"- early_entry_pnl_available: {summary.get('early_entry_pnl_available', 0)}",
        f"- early_entry_wins: {summary.get('early_entry_wins', 0)}",
        f"- early_entry_losses: {summary.get('early_entry_losses', 0)}",
    ]
    symbols = summary.get("symbols_stopped_out_under_5m") or []
    if symbols:
        lines.append("- symbols_stopped_out_under_5m: " + ", ".join(str(sym) for sym in symbols))
    blocked_symbols = summary.get("symbols_with_reentry_blocked") or []
    if blocked_symbols:
        lines.append("- symbols_with_reentry_blocked: " + ", ".join(str(sym) for sym in blocked_symbols))
    lines.extend(["", "Early vs later:"])
    for phase in ("before_0945", "between_0945_1000", "at_or_after_1000"):
        row = phase_summary.get(phase) if isinstance(phase_summary.get(phase), Mapping) else {}
        lines.append(
            "- %s: entries=%s exited=%s stop_under_5m=%s avg_hold=%s symbols=%s"
            % (
                phase,
                row.get("entries", 0),
                row.get("exited", 0),
                row.get("stopped_out_under_5m", 0),
                "n/a" if row.get("avg_hold_minutes") is None else f"{float(row['avg_hold_minutes']):.2f}m",
                ",".join(str(sym) for sym in row.get("symbols", []) or []) or "none",
            )
        )
    lines.extend(["", "Entries:", "| time | symbol | route | notional | qty | exit_time | exit_reason | hold_min | realized_pnl | flag |", "| --- | --- | --- | ---: | ---: | --- | --- | ---: | ---: | --- |"])
    for row in entries:
        pnl = row.get("realized_pnl")
        lines.append(
                "| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |"
            % (
                str(row.get("entry_time") or "n/a"),
                str(row.get("symbol") or ""),
                str(row.get("route") or ""),
                "n/a" if row.get("notional") is None else f"{float(row['notional']):.2f}",
                "n/a" if row.get("qty") is None else f"{float(row['qty']):.4f}",
                str(row.get("exit_time") or "open/no exit"),
                str(row.get("exit_reason") or "n/a"),
                "n/a" if row.get("hold_minutes") is None else f"{float(row['hold_minutes']):.2f}",
                "not available" if pnl is None else f"{float(pnl):.2f}",
                "STOP_UNDER_5M" if row.get("stopped_out_under_5m") else "",
            )
        )
    if flagged:
        lines.extend(["", "Stopped out within 5 minutes:"])
        for row in flagged:
            lines.append(
                "- %s %s route=%s hold=%.2fm exit_reason=%s"
                % (
                    row.get("symbol"),
                    row.get("entry_time"),
                    row.get("route"),
                    float(row.get("hold_minutes") or 0.0),
                    row.get("exit_reason"),
                )
            )
    log_analysis = report.get("log_analysis") if isinstance(report.get("log_analysis"), Mapping) else {}
    reentry = log_analysis.get("reentry_blocks") if isinstance(log_analysis.get("reentry_blocks"), Mapping) else {}
    reentry_rows = reentry.get("rows") if isinstance(reentry.get("rows"), list) else []
    if reentry_rows:
        lines.extend(["", "Re-entry blocked by cooldown/post-exit wait:"])
        for row in reentry_rows[:20]:
            lines.append(
                "- %s: count=%s reasons=%s"
                % (row.get("symbol"), row.get("count", 0), row.get("reasons", {}))
            )
    trend = log_analysis.get("trend_prefilter") if isinstance(log_analysis.get("trend_prefilter"), Mapping) else {}
    top_skips = trend.get("top_skipped_symbols") if isinstance(trend.get("top_skipped_symbols"), list) else []
    lines.extend(["", "Trend prefilter skips:"])
    lines.append(f"- total_skips: {trend.get('total_skips', 0)}")
    lines.append(f"- symbols: {trend.get('symbols', 0)}")
    for row in top_skips[:15]:
        lines.append(
            "- %s: count=%s status=%s first=%s last=%s"
            % (
                row.get("symbol"),
                row.get("count", 0),
                row.get("status"),
                row.get("first_seen") or "n/a",
                row.get("last_seen") or "n/a",
            )
        )
    return "\n".join(lines) + "\n"


def opening_churn_paths(*, data_dir: Path | str, user_id: str, day: date | str) -> OpeningChurnPaths:
    day_s = day.isoformat() if isinstance(day, date) else str(day)
    out_dir = Path(data_dir) / "research" / "opening_churn"
    stem = f"{day_s}_{_safe_user(user_id)}"
    return OpeningChurnPaths(json_path=out_dir / f"{stem}.json", text_path=out_dir / f"{stem}.txt")


def write_opening_churn_report(
    *,
    data_dir: Path | str,
    user_id: str,
    day: date | str,
    project_root: Path | str = ".",
    log_paths: Sequence[Path | str] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    report = build_opening_churn_report(
        data_dir=data_dir,
        user_id=user_id,
        day=day,
        project_root=project_root,
        log_paths=log_paths,
    )
    paths = opening_churn_paths(data_dir=data_dir, user_id=user_id, day=day)
    paths.json_path.parent.mkdir(parents=True, exist_ok=True)
    paths.json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths.text_path.write_text(render_opening_churn_report(report), encoding="utf-8")
    return paths.json_path, paths.text_path, report


def latest_opening_churn_date(*, data_dir: Path | str, user_id: str) -> str | None:
    root = Path(data_dir) / "trade_attribution" / "daily"
    if not root.exists():
        return None
    suffix = f"_{_safe_user(user_id)}.json"
    dates = sorted(path.name[:10] for path in root.glob(f"*{suffix}") if len(path.name) >= 10)
    return dates[-1] if dates else None


__all__ = [
    "build_opening_churn_report",
    "discover_opening_churn_log_paths",
    "latest_opening_churn_date",
    "opening_churn_paths",
    "render_opening_churn_report",
    "write_opening_churn_report",
]
