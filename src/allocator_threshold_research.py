"""Research-only allocator threshold and dynamic RVOL diagnostics."""

from __future__ import annotations

import gzip
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_COMPACT_DATE_RE = re.compile(r"(\d{8})")
_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^ ]+)")
_ALLOCATOR_NO_ACTION_RE = re.compile(
    r"ALLOCATOR_NO_ACTION_DETAIL\s+symbol=(?P<symbol>[A-Z][A-Z0-9.\-]{0,9})\s+"
    r"reason=(?P<reason>.+?)\s+detail=(?P<detail>.+?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)"
)
_ALLOCATOR_SKIP_RE = re.compile(
    r"\bSKIP\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,9}):\s+reason=(?P<reason>.+?)\s+"
    r"detail=(?P<detail>.+?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)"
)
_ALLOCATOR_SKIP_REASON_RE = re.compile(
    r"ALLOCATOR_SKIP_REASON\s+symbol=(?P<symbol>[A-Z][A-Z0-9.\-]{0,9})\s+"
    r"reason=(?P<reason>[^ ]+)"
)
_ENTRY_EVAL_RE = re.compile(
    r"\b(?P<symbol>[A-Z][A-Z0-9.\-]{0,9})\s+ENTRY_EVAL\s+"
    r"route=(?P<route>[^ ]+).*?\bfinal=(?P<final>[TF]|true|false|True|False)\b.*?"
    r"\breason=(?P<reason>.+?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)"
)
_DYNAMIC_REJECT_RE = re.compile(
    r"\bDYNAMIC_SCAN reject\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,9}):\s+(?P<reason>.+)$"
)


@dataclass(frozen=True)
class AllocatorThresholdPaths:
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


def _parse_kv(line: str) -> dict[str, str]:
    return {match.group(1): match.group(2).strip(",") for match in _KV_RE.finditer(line)}


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


def _read_text(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    return path.read_text(encoding="utf-8", errors="replace")


def _json_load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _percentiles(values: Sequence[float]) -> dict[str, float | None]:
    vals = sorted(float(v) for v in values if math.isfinite(float(v)))
    if not vals:
        return {"count": 0, "min": None, "p25": None, "median": None, "p75": None, "max": None, "avg": None}

    def pct(q: float) -> float:
        if len(vals) == 1:
            return vals[0]
        pos = (len(vals) - 1) * q
        lo = int(pos)
        hi = min(lo + 1, len(vals) - 1)
        frac = pos - lo
        return vals[lo] * (1.0 - frac) + vals[hi] * frac

    return {
        "count": len(vals),
        "min": round(vals[0], 4),
        "p25": round(pct(0.25), 4),
        "median": round(median(vals), 4),
        "p75": round(pct(0.75), 4),
        "max": round(vals[-1], 4),
        "avg": round(sum(vals) / len(vals), 4),
    }


def discover_research_log_paths(
    *,
    project_root: Path | str = ".",
    day: str | None = None,
    extra_paths: Sequence[Path | str] | None = None,
) -> list[Path]:
    """Discover local log/replay files without requiring journalctl access."""
    root = Path(project_root)
    paths: list[Path] = []
    patterns = ("*.log", "*.txt", "*.log.gz")
    roots = [
        root,
        root / "data" / "logs",
        root / "data" / "debug_logs",
        root / "reports" / "debug",
    ]
    for base in roots:
        if not base.exists():
            continue
        for pattern in patterns:
            for path in base.rglob(pattern) if base.name in {"debug_logs"} else base.glob(pattern):
                if not path.is_file():
                    continue
                path_day = _date_from_path(path)
                if day and path_day not in {None, day} and day not in path.name:
                    continue
                paths.append(path)
    for raw in extra_paths or []:
        path = Path(raw)
        if path.exists() and path.is_file():
            paths.append(path)
    return sorted(dict.fromkeys(paths))


def _iter_scan_history_paths(data_dir: Path, *, day: str | None, user_id: str, recent_days: int) -> list[Path]:
    roots = [data_dir / "dynamic_scan_history", data_dir / "dynamic_scan_history" / "daily"]
    safe_user = _safe_user(user_id)
    paths: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.glob("*.json"):
            path_day = _date_from_path(path)
            if day and path_day and path_day > day:
                continue
            if path.name.endswith(f"_{safe_user}.json") or path.name.endswith("_default.json"):
                paths.append(path)
    if recent_days > 0:
        distinct_days = sorted({d for p in paths if (d := _date_from_path(p))}, reverse=True)[:recent_days]
        day_set = set(distinct_days)
        paths = [p for p in paths if _date_from_path(p) in day_set]
    return sorted(dict.fromkeys(paths))


def _scan_candidate_rows(path: Path) -> list[dict[str, Any]]:
    payload = _json_load(path)
    rows: list[dict[str, Any]] = []
    if not isinstance(payload, Mapping):
        return rows
    day = _date_from_path(path)
    if isinstance(payload.get("candidates"), list):
        for raw in payload["candidates"]:
            if isinstance(raw, Mapping):
                row = dict(raw)
                row["_source_file"] = str(path)
                row["_date"] = day
                rows.append(row)
    # Daily summaries usually do not include every candidate; keep rejection counts separately.
    return rows


def _scan_daily_rejection_counts(path: Path) -> Counter[str]:
    payload = _json_load(path)
    if not isinstance(payload, Mapping):
        return Counter()
    raw = payload.get("rejection_counts")
    if not isinstance(raw, Mapping):
        return Counter()
    counts: Counter[str] = Counter()
    for key, value in raw.items():
        try:
            counts[str(key)] += int(value)
        except (TypeError, ValueError):
            continue
    return counts


def _line_source(path: Path, index: int, line: str) -> dict[str, Any]:
    return {"source_file": str(path), "line_number": index, "line": line.strip()}


def _parse_allocator_no_action_line(path: Path, index: int, line: str) -> dict[str, Any] | None:
    if (
        "ALLOCATOR_NO_ACTION_DETAIL" not in line
        and "ALLOCATOR_SKIP_REASON" not in line
        and not ("SKIP " in line and "minimum_cash_to_deploy" in line)
    ):
        return None
    match = _ALLOCATOR_NO_ACTION_RE.search(line)
    if match is None:
        match = _ALLOCATOR_SKIP_RE.search(line)
    skip_reason_match = _ALLOCATOR_SKIP_REASON_RE.search(line)
    kv = _parse_kv(line)
    symbol = (
        match.group("symbol")
        if match
        else skip_reason_match.group("symbol")
        if skip_reason_match
        else kv.get("symbol") or ""
    ).strip().upper()
    if not symbol:
        return None
    reason = (
        match.group("reason")
        if match
        else skip_reason_match.group("reason")
        if skip_reason_match
        else kv.get("reason") or ""
    ).strip()
    detail = (match.group("detail") if match else "").strip()
    if not detail:
        detail_match = re.search(r"detail=(.+?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)", line)
        detail = detail_match.group(1).strip() if detail_match else ""
    trade_size_match = re.search(r"trade_size\s+\$?([0-9]+(?:\.[0-9]+)?)", detail)
    minimum_match = re.search(r"minimum_cash_to_deploy\s+([0-9]+(?:\.[0-9]+)?)", detail)
    row = {
        **_line_source(path, index, line),
        "date": _date_from_path(path),
        "symbol": symbol,
        "reason": reason,
        "detail": detail,
        "minimum_cash_to_deploy_block": "minimum_cash_to_deploy" in detail,
        "trade_size": _safe_float(kv.get("final_trade_size")) or _safe_float(kv.get("candidate_notional")) or _safe_float(trade_size_match.group(1) if trade_size_match else None),
        "minimum_cash_to_deploy": _safe_float(kv.get("minimum_cash_to_deploy")) or _safe_float(minimum_match.group(1) if minimum_match else None),
        "available_cash": _safe_float(kv.get("available_cash")),
        "gross_headroom": _safe_float(kv.get("gross_headroom")),
        "candidate_notional_requested": _safe_float(kv.get("candidate_notional_requested")),
        "candidate_notional": _safe_float(kv.get("candidate_notional")),
        "limiting_cap": kv.get("limiting_cap"),
        "min_order_notional": _safe_float(kv.get("min_order_notional")),
        "source": kv.get("source"),
        "score": _safe_float(kv.get("score")),
        "last_removal_stage": kv.get("last_removal_stage"),
        "trade_cycle_allowed": kv.get("trade_cycle_allowed"),
    }
    return row


def _parse_entry_eval_line(path: Path, index: int, line: str) -> dict[str, Any] | None:
    match = _ENTRY_EVAL_RE.search(line)
    if not match:
        return None
    final_raw = match.group("final").strip().lower()
    return {
        **_line_source(path, index, line),
        "date": _date_from_path(path),
        "symbol": match.group("symbol").strip().upper(),
        "route": match.group("route").strip(),
        "final": final_raw in {"t", "true"},
        "reason": match.group("reason").strip(),
    }


def _parse_dynamic_reject_line(path: Path, index: int, line: str) -> dict[str, Any] | None:
    match = _DYNAMIC_REJECT_RE.search(line)
    if not match:
        return None
    return {
        **_line_source(path, index, line),
        "date": _date_from_path(path),
        "symbol": match.group("symbol").strip().upper(),
        "reason": match.group("reason").strip(),
    }


def _ingest_logs(paths: Sequence[Path]) -> dict[str, Any]:
    allocator_rows: list[dict[str, Any]] = []
    entry_rows: list[dict[str, Any]] = []
    dynamic_reject_rows: list[dict[str, Any]] = []
    used: list[str] = []
    for path in paths:
        try:
            text = _read_text(path)
        except OSError:
            continue
        useful = False
        for index, line in enumerate(text.splitlines(), start=1):
            row = _parse_allocator_no_action_line(path, index, line)
            if row is not None:
                allocator_rows.append(row)
                useful = True
            entry = _parse_entry_eval_line(path, index, line)
            if entry is not None:
                entry_rows.append(entry)
                useful = True
            reject = _parse_dynamic_reject_line(path, index, line)
            if reject is not None:
                dynamic_reject_rows.append(reject)
                useful = True
        if useful:
            used.append(str(path))
    return {
        "allocator_no_action_rows": allocator_rows,
        "entry_eval_rows": entry_rows,
        "dynamic_reject_rows": dynamic_reject_rows,
        "used_log_paths": used,
    }


def _sqlite_allocator_rows(data_dir: Path, *, day: str | None, user_id: str) -> list[dict[str, Any]]:
    db_path = data_dir / "algo_live.db"
    if not db_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        con = sqlite3.connect(db_path)
    except sqlite3.Error:
        return []
    try:
        query = (
            "select ts,user_id,symbol,route,stage,reason,payload_json "
            "from entry_terminal_outcomes where stage='allocator_no_action'"
        )
        args: list[Any] = []
        if user_id:
            query += " and user_id=?"
            args.append(user_id)
        if day:
            query += " and substr(ts,1,10)=?"
            args.append(day)
        for ts, uid, symbol, route, stage, reason, payload_json in con.execute(query, args):
            payload: dict[str, Any] = {}
            try:
                parsed = json.loads(payload_json or "{}")
                if isinstance(parsed, Mapping):
                    payload = dict(parsed)
            except (TypeError, ValueError):
                payload = {}
            detail = str(payload.get("detail") or payload.get("reason") or reason or "")
            rows.append(
                {
                    "source_file": str(db_path),
                    "line_number": None,
                    "line": "",
                    "date": str(ts)[:10] if ts else day,
                    "timestamp": ts,
                    "user_id": uid,
                    "symbol": str(symbol or "").upper(),
                    "route": route,
                    "reason": reason,
                    "detail": detail,
                    "minimum_cash_to_deploy_block": "minimum_cash_to_deploy" in detail
                    or str(reason) == "minimum_cash_to_deploy",
                    "trade_size": _safe_float(payload.get("final_trade_size"))
                    or _safe_float(payload.get("candidate_notional")),
                    "minimum_cash_to_deploy": _safe_float(payload.get("minimum_cash_to_deploy")),
                    "available_cash": _safe_float(payload.get("available_cash")),
                    "gross_headroom": _safe_float(payload.get("gross_headroom")),
                    "candidate_notional_requested": _safe_float(payload.get("candidate_notional_requested")),
                    "candidate_notional": _safe_float(payload.get("candidate_notional")),
                    "limiting_cap": payload.get("limiting_cap"),
                    "min_order_notional": _safe_float(payload.get("min_order_notional")),
                    "source": payload.get("source"),
                    "score": _safe_float(payload.get("score")),
                }
            )
    except sqlite3.Error:
        return rows
    finally:
        con.close()
    return rows


def _sqlite_entry_eval_rows(data_dir: Path, *, day: str | None, user_id: str) -> list[dict[str, Any]]:
    db_path = data_dir / "algo_live.db"
    if not db_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        con = sqlite3.connect(db_path)
    except sqlite3.Error:
        return []
    try:
        query = "select ts,user_id,symbol,route,final,reason,payload_json from entry_evaluations"
        args: list[Any] = []
        clauses: list[str] = []
        if user_id:
            clauses.append("user_id=?")
            args.append(user_id)
        if day:
            clauses.append("substr(ts,1,10)=?")
            args.append(day)
        if clauses:
            query += " where " + " and ".join(clauses)
        for ts, uid, symbol, route, final, reason, payload_json in con.execute(query, args):
            payload: dict[str, Any] = {}
            try:
                parsed = json.loads(payload_json or "{}")
                if isinstance(parsed, Mapping):
                    payload = dict(parsed)
            except (TypeError, ValueError):
                payload = {}
            rows.append(
                {
                    "source_file": str(db_path),
                    "line_number": None,
                    "line": "",
                    "date": str(ts)[:10] if ts else day,
                    "timestamp": ts,
                    "user_id": uid,
                    "symbol": str(symbol or "").upper(),
                    "route": route,
                    "final": bool(final),
                    "reason": reason,
                    "payload": payload,
                }
            )
    except sqlite3.Error:
        return rows
    finally:
        con.close()
    return rows


def _allocator_trace(
    *,
    symbols: Sequence[str],
    allocator_rows: Sequence[Mapping[str, Any]],
    entry_rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    by_symbol_alloc: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_symbol_entry: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in allocator_rows:
        by_symbol_alloc[str(row.get("symbol") or "").upper()].append(row)
    for row in entry_rows:
        by_symbol_entry[str(row.get("symbol") or "").upper()].append(row)
    for raw_sym in symbols:
        sym = raw_sym.upper()
        alloc = list(by_symbol_alloc.get(sym, []))
        entries = list(by_symbol_entry.get(sym, []))
        latest_alloc = alloc[-1] if alloc else None
        latest_entry = entries[-1] if entries else None
        out[sym] = {
            "entry_eval": latest_entry,
            "allocator_no_action": latest_alloc,
            "allocator_no_action_count": len(alloc),
            "root_cause": (
                "minimum_cash_to_deploy_after_gross_headroom_clip"
                if latest_alloc and latest_alloc.get("minimum_cash_to_deploy_block")
                else "not_observed_in_local_allocator_no_action_evidence"
            ),
        }
    return out


def _allocator_minimum_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    minimum_rows = [row for row in rows if row.get("minimum_cash_to_deploy_block")]
    by_symbol = Counter(str(row.get("symbol") or "").upper() for row in minimum_rows)
    by_date = Counter(str(row.get("date") or "unknown") for row in minimum_rows)
    examples = [
        {
            "date": row.get("date"),
            "symbol": row.get("symbol"),
            "trade_size": row.get("trade_size"),
            "minimum_cash_to_deploy": row.get("minimum_cash_to_deploy"),
            "available_cash": row.get("available_cash"),
            "gross_headroom": row.get("gross_headroom"),
            "limiting_cap": row.get("limiting_cap"),
            "detail": row.get("detail"),
            "source_file": row.get("source_file"),
            "line_number": row.get("line_number"),
        }
        for row in minimum_rows[:25]
    ]
    return {
        "minimum_cash_to_deploy_blocks": len(minimum_rows),
        "symbols": dict(sorted(by_symbol.items())),
        "by_date": dict(sorted(by_date.items())),
        "trade_size_distribution": _percentiles([float(v) for row in minimum_rows if (v := row.get("trade_size")) is not None]),
        "minimum_cash_to_deploy_distribution": _percentiles(
            [float(v) for row in minimum_rows if (v := row.get("minimum_cash_to_deploy")) is not None]
        ),
        "examples": examples,
    }


def _entry_eval_passed_rows(rows: Sequence[Mapping[str, Any]], *, day: str) -> list[Mapping[str, Any]]:
    return [
        row
        for row in rows
        if bool(row.get("final")) and (not day or row.get("date") in {None, day})
    ]


def _dynamic_candidate_symbols(candidate_rows: Sequence[Mapping[str, Any]], *, day: str) -> set[str]:
    out: set[str] = set()
    for row in candidate_rows:
        if day and row.get("_date") not in {None, day}:
            continue
        sym = str(row.get("symbol") or "").strip().upper()
        if sym:
            out.add(sym)
    return out


def _candidate_type_for_symbol(
    symbol: str,
    *,
    route: Any,
    dynamic_symbols: set[str],
) -> str:
    route_l = str(route or "").strip().lower()
    sym_u = str(symbol or "").strip().upper()
    if "dynamic" in route_l or sym_u in dynamic_symbols:
        return "dynamic"
    return "core"


def _annotated_allocator_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    entry_rows: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]],
    day: str,
) -> list[dict[str, Any]]:
    by_symbol_entry: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for entry in entry_rows:
        if day and entry.get("date") not in {None, day}:
            continue
        by_symbol_entry[str(entry.get("symbol") or "").upper()].append(entry)
    dynamic_symbols = _dynamic_candidate_symbols(candidate_rows, day=day)
    out: list[dict[str, Any]] = []
    for row in rows:
        if day and row.get("date") not in {None, day} and day not in str(row.get("source_file") or ""):
            continue
        sym = str(row.get("symbol") or "").strip().upper()
        latest_entry = (by_symbol_entry.get(sym) or [None])[-1]
        route = row.get("route") or (latest_entry.get("route") if isinstance(latest_entry, Mapping) else None) or row.get("source")
        reason = str(row.get("reason") or "").strip()
        detail = str(row.get("detail") or "").strip()
        reason_skipped = reason
        if row.get("minimum_cash_to_deploy_block"):
            reason_skipped = "minimum_cash_to_deploy"
        elif "min_realloc_leg" in detail:
            reason_skipped = "min_realloc_leg"
        elif "gross_headroom" in detail or row.get("limiting_cap") == "gross_headroom":
            reason_skipped = "gross_headroom"
        elif "trade_size" in detail and ("too small" in detail or "below" in detail or "<" in detail):
            reason_skipped = "trade_size_too_small"
        elif "no_trade_cycle_allowed" in reason:
            reason_skipped = "no_trade_cycle_allowed"
        out.append(
            {
                "symbol": sym,
                "route": route,
                "proposed_trade_size": row.get("trade_size"),
                "minimum_cash_to_deploy": row.get("minimum_cash_to_deploy"),
                "gross_headroom": row.get("gross_headroom"),
                "available_cash": row.get("available_cash"),
                "min_realloc_leg": row.get("min_order_notional"),
                "reason_skipped": reason_skipped,
                "raw_reason": reason,
                "detail": detail,
                "candidate_type": _candidate_type_for_symbol(
                    sym,
                    route=route,
                    dynamic_symbols=dynamic_symbols,
                ),
                "source_file": row.get("source_file"),
                "line_number": row.get("line_number"),
            }
        )
    return out


def _allocator_suppression_summary(
    *,
    entry_rows: Sequence[Mapping[str, Any]],
    allocator_rows: Sequence[Mapping[str, Any]],
    day: str,
) -> dict[str, Any]:
    entry_passed = _entry_eval_passed_rows(entry_rows, day=day)
    target_allocator_rows = [
        row
        for row in allocator_rows
        if row.get("date") in {None, day} or day in str(row.get("source_file") or "")
    ]
    skipped_by_min_cash = [row for row in target_allocator_rows if row.get("minimum_cash_to_deploy_block")]
    symbol_counts = Counter(str(row.get("symbol") or "").upper() for row in target_allocator_rows)
    trade_values = [
        float(v)
        for row in skipped_by_min_cash
        if (v := _safe_float(row.get("trade_size"))) is not None
    ]
    floor_values = [
        float(v)
        for row in skipped_by_min_cash
        if (v := _safe_float(row.get("minimum_cash_to_deploy"))) is not None
    ]
    avg_trade = sum(trade_values) / len(trade_values) if trade_values else None
    avg_floor = sum(floor_values) / len(floor_values) if floor_values else None
    return {
        "total_entry_eval_passed": len(entry_passed),
        "entry_eval_passed_symbols": sorted({str(row.get("symbol") or "").upper() for row in entry_passed}),
        "total_allocator_no_action": len(target_allocator_rows),
        "total_skipped_by_min_cash": len(skipped_by_min_cash),
        "symbols_most_often_skipped": symbol_counts.most_common(10),
        "average_proposed_trade_size": round(avg_trade, 4) if avg_trade is not None else None,
        "average_minimum_cash_to_deploy": round(avg_floor, 4) if avg_floor is not None else None,
        "average_trade_size_to_minimum_ratio": (
            round(avg_trade / avg_floor, 4)
            if avg_trade is not None and avg_floor not in (None, 0)
            else None
        ),
    }


def _allocator_threshold_what_if(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    mcd_rows = [row for row in rows if row.get("minimum_cash_to_deploy_block")]

    def scenario(label: str, *, floor_mult: float | None = None, min_leg_only: bool = False, no_floor: bool = False) -> dict[str, Any]:
        passed: list[str] = []
        blocked: list[str] = []
        for row in mcd_rows:
            sym = str(row.get("symbol") or "").upper()
            trade = _safe_float(row.get("trade_size"))
            if trade is None:
                blocked.append(sym)
                continue
            if no_floor:
                threshold = 0.0
            elif min_leg_only:
                threshold = _safe_float(row.get("min_order_notional")) or 0.0
            else:
                current_floor = _safe_float(row.get("minimum_cash_to_deploy")) or 0.0
                threshold = current_floor * float(floor_mult if floor_mult is not None else 1.0)
            if trade + 5.0 >= threshold:
                passed.append(sym)
            else:
                blocked.append(sym)
        return {
            "label": label,
            "evaluated": len(mcd_rows),
            "would_clear_threshold_count": len(passed),
            "would_remain_blocked_count": len(blocked),
            "would_clear_symbols": sorted(set(passed)),
            "would_remain_blocked_symbols": sorted(set(blocked)),
            "note": "diagnostic_only_threshold_math_not_a_trading_decision",
        }

    return {
        "current_threshold": scenario("current_threshold", floor_mult=1.0),
        "threshold_75pct": scenario("75% of threshold", floor_mult=0.75),
        "threshold_50pct": scenario("50% of threshold", floor_mult=0.50),
        "min_realloc_leg_only": scenario("min_realloc_leg only", min_leg_only=True),
        "no_minimum_deployment_floor": scenario("no minimum deployment floor", no_floor=True),
    }


def _normalize_reason(reason: Any) -> str:
    text = str(reason or "").strip().lower().replace(" ", "_")
    if "relative_volume" in text or "rel_volume" in text:
        return "below_min_relative_volume"
    if "unstable" in text:
        return "unstable_quote"
    if "day_gain" in text or "min_gain" in text:
        return "below_min_day_gain"
    if "min_price" in text:
        return "below_min_price"
    if "entry_alignment" in text or "breakout" in text or "new_high" in text:
        return "entry_alignment"
    if "trend" in text or "ema" in text:
        return "trend_filter"
    return text or "unknown"


def _dynamic_rvol_analysis(candidate_rows: Sequence[Mapping[str, Any]], daily_counts: Counter[str]) -> dict[str, Any]:
    rejected = [
        row
        for row in candidate_rows
        if row.get("accepted") is False or str(row.get("accepted")).lower() in {"false", "0"}
    ]
    reason_counts = Counter(_normalize_reason(row.get("rejection_reason")) for row in rejected)
    reason_counts.update(daily_counts)
    rvol_rows = [
        row
        for row in rejected
        if _normalize_reason(row.get("rejection_reason")) == "below_min_relative_volume"
    ]
    rvol_values = [
        float(v)
        for row in rvol_rows
        if (v := _safe_float(row.get("relative_volume", row.get("rel_volume")))) is not None
    ]
    forward_returns = [
        float(v)
        for row in rvol_rows
        if (v := _safe_float(row.get("later_same_day_return_pct"))) is not None
    ]
    thresholds: dict[str, dict[str, Any]] = {}
    for threshold in (1.0, 0.75, 0.50):
        would_pass_rvol = [row for row in rvol_rows if (_safe_float(row.get("relative_volume", row.get("rel_volume"))) or 0.0) >= threshold]
        thresholds[f"{threshold:.2f}"] = {
            "threshold": threshold,
            "rvol_rejects_at_or_above_threshold": len(would_pass_rvol),
            "symbols": sorted({str(row.get("symbol") or "").upper() for row in would_pass_rvol})[:25],
            "note": "diagnostic_only_counts_rvol_gate_not_full_entry_stack",
        }
    return {
        "rejected_candidates_with_rows": len(rejected),
        "rejection_counts": dict(sorted(reason_counts.items())),
        "relative_volume_rejections": len(rvol_rows),
        "relative_volume_distribution": _percentiles(rvol_values),
        "relative_volume_forward_returns": {
            "count": len(forward_returns),
            "distribution": _percentiles(forward_returns),
        },
        "hypothetical_thresholds": thresholds,
        "examples": [
            {
                "symbol": row.get("symbol"),
                "relative_volume": _safe_float(row.get("relative_volume", row.get("rel_volume"))),
                "day_gain_pct": _safe_float(row.get("day_gain_pct", row.get("gain_pct"))),
                "later_same_day_return_pct": _safe_float(row.get("later_same_day_return_pct")),
                "source_file": row.get("_source_file"),
            }
            for row in rvol_rows[:20]
        ],
    }


def _candidate_quality(
    *,
    symbols: Sequence[str],
    candidate_rows: Sequence[Mapping[str, Any]],
    dynamic_reject_rows: Sequence[Mapping[str, Any]],
    entry_rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    by_symbol_candidates: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_symbol_rejects: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_symbol_entries: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in candidate_rows:
        by_symbol_candidates[str(row.get("symbol") or "").upper()].append(row)
    for row in dynamic_reject_rows:
        by_symbol_rejects[str(row.get("symbol") or "").upper()].append(row)
    for row in entry_rows:
        by_symbol_entries[str(row.get("symbol") or "").upper()].append(row)
    for raw in symbols:
        sym = raw.upper()
        candidates = by_symbol_candidates.get(sym, [])
        rejects = by_symbol_rejects.get(sym, [])
        entries = by_symbol_entries.get(sym, [])
        latest_candidate = candidates[-1] if candidates else None
        latest_reject = rejects[-1] if rejects else None
        latest_entry = entries[-1] if entries else None
        reason = None
        source = "not_observed"
        if latest_entry and not bool(latest_entry.get("final")):
            reason = latest_entry.get("reason")
            source = "entry_eval"
        elif latest_candidate and latest_candidate.get("accepted") is False:
            reason = latest_candidate.get("rejection_reason")
            source = "dynamic_scan_history"
        elif latest_reject:
            reason = latest_reject.get("reason")
            source = "log"
        elif latest_candidate and latest_candidate.get("accepted") is True:
            reason = "accepted_by_dynamic_scanner"
            source = "dynamic_scan_history"
        elif latest_entry:
            reason = "entry_eval_final_true"
            source = "entry_eval"
        out[sym] = {
            "source": source,
            "final_blocker": _normalize_reason(reason),
            "raw_reason": reason,
            "latest_candidate": latest_candidate,
            "latest_entry_eval": latest_entry,
            "would_trade_count_change_if_relaxed": (
                "possible_only_if_this_was_the_last_blocker"
                if reason and _normalize_reason(reason) in {"below_min_relative_volume", "entry_alignment", "trend_filter"}
                else "unlikely_from_this_gate_alone"
            ),
        }
    return out


def build_allocator_threshold_research_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: str | date,
    user_id: str = "paper_bot",
    log_paths: Sequence[Path | str] | None = None,
    recent_days: int = 5,
    allocator_symbols: Sequence[str] = ("IWM", "XLF", "JPM"),
    focus_symbols: Sequence[str] = ("ASTN", "NOK", "INTC", "POEL", "VRA", "AVGO", "GOOGL"),
) -> dict[str, Any]:
    """Build a research-only report explaining allocator and RVOL blockers."""
    day_s = day.isoformat() if isinstance(day, date) else str(day)
    root = Path(project_root)
    data = Path(data_dir)
    logs = discover_research_log_paths(project_root=root, day=day_s, extra_paths=log_paths)
    log_data = _ingest_logs(logs)
    sqlite_rows = _sqlite_allocator_rows(data, day=None, user_id=user_id)
    sqlite_entry_rows = _sqlite_entry_eval_rows(data, day=day_s, user_id=user_id)
    allocator_rows = [*log_data["allocator_no_action_rows"], *sqlite_rows]
    entry_rows = [*log_data["entry_eval_rows"], *sqlite_entry_rows]
    scan_paths = _iter_scan_history_paths(data, day=day_s, user_id=user_id, recent_days=recent_days)
    candidate_rows: list[dict[str, Any]] = []
    daily_counts: Counter[str] = Counter()
    for path in scan_paths:
        candidate_rows.extend(_scan_candidate_rows(path))
        daily_counts.update(_scan_daily_rejection_counts(path))
    target_day_allocator_rows = [
        row for row in allocator_rows if row.get("date") in {None, day_s} or day_s in str(row.get("source_file") or "")
    ]
    allocator_trace = _allocator_trace(
        symbols=allocator_symbols,
        allocator_rows=target_day_allocator_rows or allocator_rows,
        entry_rows=entry_rows,
    )
    mcd_rows = [row for row in allocator_rows if row.get("minimum_cash_to_deploy_block")]
    minimum_cash_examples = _allocator_minimum_summary(mcd_rows)
    annotated_allocator_rows = _annotated_allocator_rows(
        target_day_allocator_rows or allocator_rows,
        entry_rows=entry_rows,
        candidate_rows=candidate_rows,
        day=day_s,
    )
    allocator_summary = _allocator_suppression_summary(
        entry_rows=entry_rows,
        allocator_rows=allocator_rows,
        day=day_s,
    )
    allocator_what_if = _allocator_threshold_what_if(target_day_allocator_rows or allocator_rows)
    dynamic_rvol = _dynamic_rvol_analysis(candidate_rows, daily_counts)
    candidate_quality = _candidate_quality(
        symbols=focus_symbols,
        candidate_rows=candidate_rows,
        dynamic_reject_rows=log_data["dynamic_reject_rows"],
        entry_rows=entry_rows,
    )
    mcd_pct_sources = [
        {
            "path": "config/default.yaml allocator.minimum_cash_to_deploy_pct",
            "value": 0.05,
            "note": "operator alias; config_loader maps this to portfolio overlay only when portfolio value is absent",
        },
        {
            "path": "config/default.yaml portfolio.capital_allocator.minimum_cash_to_deploy_pct",
            "value": 0.03,
            "note": "active portfolio allocator default passed to CapitalAllocator",
        },
    ]
    recommendations = [
        "No trading-rule change made by this report.",
        "Allocator no-action rows show the floor is configuration driven: minimum_cash_to_deploy = equity * minimum_cash_to_deploy_pct, then compared after gross-headroom and per-candidate caps clip trade_size.",
        "If IWM/XLF/JPM rows show trade_size near 1312 and minimum_cash_to_deploy near 3469, the allocator is intentionally suppressing small residual deployment legs under the current floor.",
        "RVOL threshold comparisons are diagnostic only; candidates counted at 0.75 or 0.50 still need spread, quote quality, price, trend, entry alignment, allocator, and risk checks.",
        "Investigate lowering or route-scoping minimum_cash_to_deploy only after reviewing historical frequency and missed-return evidence; do not change it based on a single zero-trade session.",
    ]
    return {
        "version": 1,
        "date": day_s,
        "user_id": user_id,
        "research_only": True,
        "source_files": {
            "logs": log_data["used_log_paths"],
            "dynamic_scan_history": [str(path) for path in scan_paths],
            "sqlite_event_store": str(data / "algo_live.db") if (data / "algo_live.db").exists() else None,
        },
        "allocator_analysis": {
            "symbols": allocator_trace,
            "candidate_rows": annotated_allocator_rows,
            "summary": allocator_summary,
            "what_if": allocator_what_if,
            "minimum_cash_to_deploy_derivation": {
                "code_path": "src/capital_allocator_loop.py passes ca_cfg.minimum_cash_to_deploy_pct to CapitalAllocator; src/portfolio/allocator_planner.py computes equity * fraction and skips if trade_size + $5 buffer is below it after clipping.",
                "config_sources": mcd_pct_sources,
                "classification": "configuration_driven_intentional_floor",
            },
            "historical_minimum_cash_to_deploy": minimum_cash_examples,
        },
        "dynamic_rvol_analysis": dynamic_rvol,
        "dynamic_candidate_quality": candidate_quality,
        "recommended_actions": recommendations,
    }


def render_allocator_threshold_research_report(report: Mapping[str, Any]) -> str:
    """Render a concise operator-facing text report."""
    lines = [
        f"Allocator Threshold Research - {report.get('date')} user={report.get('user_id')}",
        "Research-only. No trading behavior, config, sizing, risk, or scanner thresholds changed.",
        "",
        "Root cause:",
    ]
    derivation = (
        report.get("allocator_analysis", {})
        .get("minimum_cash_to_deploy_derivation", {})
        if isinstance(report.get("allocator_analysis"), Mapping)
        else {}
    )
    lines.append(f"- classification: {derivation.get('classification', 'unknown')}")
    lines.append(f"- path: {derivation.get('code_path', 'n/a')}")
    lines.append("")
    lines.append("Allocator trace:")
    symbols = (
        report.get("allocator_analysis", {}).get("symbols", {})
        if isinstance(report.get("allocator_analysis"), Mapping)
        else {}
    )
    for sym, row in symbols.items():
        alloc = row.get("allocator_no_action") if isinstance(row, Mapping) else None
        entry = row.get("entry_eval") if isinstance(row, Mapping) else None
        if isinstance(alloc, Mapping):
            lines.append(
                "- {sym}: root={root} trade_size={trade} minimum_cash_to_deploy={floor} "
                "available_cash={cash} gross_headroom={gross} limiting_cap={cap} detail={detail}".format(
                    sym=sym,
                    root=row.get("root_cause"),
                    trade=alloc.get("trade_size"),
                    floor=alloc.get("minimum_cash_to_deploy"),
                    cash=alloc.get("available_cash"),
                    gross=alloc.get("gross_headroom"),
                    cap=alloc.get("limiting_cap"),
                    detail=alloc.get("detail"),
                )
            )
        elif isinstance(entry, Mapping):
            lines.append(f"- {sym}: entry_eval observed but allocator minimum-deploy row not found in local logs")
        else:
            lines.append(f"- {sym}: no local allocator trace found")
    lines.append("")
    hist = (
        report.get("allocator_analysis", {}).get("historical_minimum_cash_to_deploy", {})
        if isinstance(report.get("allocator_analysis"), Mapping)
        else {}
    )
    lines.append("Historical minimum-deploy impact:")
    lines.append(f"- blocks: {hist.get('minimum_cash_to_deploy_blocks', 0)}")
    lines.append(f"- by_date: {hist.get('by_date', {})}")
    lines.append(f"- symbols: {hist.get('symbols', {})}")
    lines.append(f"- trade_size_distribution: {hist.get('trade_size_distribution', {})}")
    lines.append("")
    summary = (
        report.get("allocator_analysis", {}).get("summary", {})
        if isinstance(report.get("allocator_analysis"), Mapping)
        else {}
    )
    lines.append("Allocator minimum-deployment summary:")
    lines.append(f"- total_entry_eval_passed: {summary.get('total_entry_eval_passed', 0)}")
    lines.append(f"- total_allocator_no_action: {summary.get('total_allocator_no_action', 0)}")
    lines.append(f"- total_skipped_by_min_cash: {summary.get('total_skipped_by_min_cash', 0)}")
    lines.append(f"- symbols_most_often_skipped: {summary.get('symbols_most_often_skipped', [])}")
    lines.append(
        "- average_proposed_trade_size: {trade} average_minimum_cash_to_deploy: {floor} ratio: {ratio}".format(
            trade=summary.get("average_proposed_trade_size"),
            floor=summary.get("average_minimum_cash_to_deploy"),
            ratio=summary.get("average_trade_size_to_minimum_ratio"),
        )
    )
    lines.append("")
    candidate_rows = (
        report.get("allocator_analysis", {}).get("candidate_rows", [])
        if isinstance(report.get("allocator_analysis"), Mapping)
        else []
    )
    lines.append("Skipped allocator candidates:")
    for row in candidate_rows[:50]:
        if not isinstance(row, Mapping):
            continue
        lines.append(
            "- {symbol}: route={route} candidate_type={candidate_type} trade_size={trade} "
            "minimum_cash_to_deploy={floor} gross_headroom={gross} available_cash={cash} "
            "reason_skipped={reason}".format(
                symbol=row.get("symbol"),
                route=row.get("route"),
                candidate_type=row.get("candidate_type"),
                trade=row.get("proposed_trade_size"),
                floor=row.get("minimum_cash_to_deploy"),
                gross=row.get("gross_headroom"),
                cash=row.get("available_cash"),
                reason=row.get("reason_skipped"),
            )
        )
    if not candidate_rows:
        lines.append("- none observed in local logs/events")
    lines.append("")
    what_if = (
        report.get("allocator_analysis", {}).get("what_if", {})
        if isinstance(report.get("allocator_analysis"), Mapping)
        else {}
    )
    lines.append("What-if analysis:")
    for key in (
        "current_threshold",
        "threshold_75pct",
        "threshold_50pct",
        "min_realloc_leg_only",
        "no_minimum_deployment_floor",
    ):
        row = what_if.get(key, {}) if isinstance(what_if, Mapping) else {}
        lines.append(
            "- {key}: evaluated={evaluated} would_clear={clear} would_remain_blocked={blocked} "
            "clear_symbols={symbols}".format(
                key=key,
                evaluated=row.get("evaluated", 0),
                clear=row.get("would_clear_threshold_count", 0),
                blocked=row.get("would_remain_blocked_count", 0),
                symbols=row.get("would_clear_symbols", []),
            )
        )
    lines.append("")
    rvol = report.get("dynamic_rvol_analysis") if isinstance(report.get("dynamic_rvol_analysis"), Mapping) else {}
    lines.append("Dynamic RVOL analysis:")
    lines.append(f"- rejection_counts: {rvol.get('rejection_counts', {})}")
    lines.append(f"- relative_volume_rejections: {rvol.get('relative_volume_rejections', 0)}")
    lines.append(f"- rvol_distribution: {rvol.get('relative_volume_distribution', {})}")
    thresholds = rvol.get("hypothetical_thresholds") if isinstance(rvol.get("hypothetical_thresholds"), Mapping) else {}
    for key in ("1.00", "0.75", "0.50"):
        row = thresholds.get(key, {})
        lines.append(
            f"- threshold {key}: rvol_rejects_at_or_above_threshold={row.get('rvol_rejects_at_or_above_threshold', 0)} symbols={row.get('symbols', [])}"
        )
    lines.append("")
    lines.append("Focus symbol blockers:")
    quality = report.get("dynamic_candidate_quality") if isinstance(report.get("dynamic_candidate_quality"), Mapping) else {}
    for sym, row in quality.items():
        if not isinstance(row, Mapping):
            continue
        lines.append(
            f"- {sym}: blocker={row.get('final_blocker')} source={row.get('source')} reason={row.get('raw_reason')}"
        )
    lines.append("")
    lines.append("Recommended actions:")
    for item in report.get("recommended_actions") or []:
        lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def allocator_threshold_paths(*, data_dir: Path | str, user_id: str, day: str | date) -> AllocatorThresholdPaths:
    day_s = day.isoformat() if isinstance(day, date) else str(day)
    root = Path(data_dir) / "research" / "allocator_threshold_research"
    stem = f"{day_s}_{_safe_user(user_id)}"
    return AllocatorThresholdPaths(json_path=root / f"{stem}.json", text_path=root / f"{stem}.txt")


def write_allocator_threshold_research_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: str | date,
    user_id: str = "paper_bot",
    log_paths: Sequence[Path | str] | None = None,
    recent_days: int = 5,
) -> tuple[Path, Path, dict[str, Any]]:
    report = build_allocator_threshold_research_report(
        project_root=project_root,
        data_dir=data_dir,
        day=day,
        user_id=user_id,
        log_paths=log_paths,
        recent_days=recent_days,
    )
    paths = allocator_threshold_paths(data_dir=data_dir, user_id=user_id, day=day)
    paths.json_path.parent.mkdir(parents=True, exist_ok=True)
    paths.json_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    paths.text_path.write_text(render_allocator_threshold_research_report(report), encoding="utf-8")
    return paths.json_path, paths.text_path, report


def latest_allocator_threshold_date(*, data_dir: Path | str, user_id: str) -> str | None:
    paths = _iter_scan_history_paths(Path(data_dir), day=None, user_id=user_id, recent_days=0)
    dates = sorted({d for path in paths if (d := _date_from_path(path))})
    return dates[-1] if dates else None


__all__ = [
    "build_allocator_threshold_research_report",
    "render_allocator_threshold_research_report",
    "write_allocator_threshold_research_report",
    "latest_allocator_threshold_date",
    "allocator_threshold_paths",
]
